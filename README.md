# ITSM Workflow

当前稳定版本：`0.6.3`。

面向 AOPS 生产操作的可观测、可中断、可恢复工作流平台。知识、不可变工作流版本、运行状态、LangGraph checkpoint、审批与审计统一保存在 PostgreSQL。

当前实现：

- 版本化 DAG与发布校验。
- `sql_read`、`condition`、`human_input`、`approval`、`end` 节点。
- 系统 `aops-cli db read` 安全执行。
- 计划哈希确认、节点级中断、恢复、暂停、取消和未知结果处理。
- AES-256-GCM运行凭据与节点结果加密。
- PostgreSQL LangGraph加密 checkpoint；SQLite只用于本地测试。
- SSE节点事件、Prometheus格式运行指标。
- AOPS身份和 UID allowlist。
- React + Carbon + React Flow可编辑编排画布及只读执行画布，移动端自动切换节点时间线。
- 兼容旧知识服务 `schemaVersion=1` JSON导入包。
- Dify式多节点经验编排与流程详情视图。
- 通过工单 ID读取详情和审计记录，由程序过滤后调用 LLM生成中文草稿。
- 操作员可摄入并编辑自己的草稿、提交管理员审核；独立服务审核 Token支持三方平台审核和发布。
- 知识支持二次确认软删除；已发布经验可由管理员创建新草稿并审核发布为下一不可变版本。
- 执行中心和知识清单支持权限感知的后端分页、关键词查询、状态筛选及可恢复URL查询状态。
- 创建运行时根据经验中的 `RUN_INPUT` 动态生成类型化表单；节点输出绑定参数不会重复向用户索要。
- `FAILED` 节点支持人工重试；`UNKNOWN` 节点要求操作者选择重试或标记失败。
- 独立 Streamable HTTP MCP Adapter，提供经验匹配、计划确认、紧凑事实短等待、节点结果读取、中断和恢复等13个工具。
- 节点失败记录可读错误原因，并为运行发起人和管理员提供加密、脱敏的 stdout/stderr诊断。
- 知识、运行与不可变版本支持权限感知的字段级精确筛选和生命周期时间线。
- 原生 DAG v2跨环境导入导出支持数据库路径映射、重复检测、待审核导入、快速路径替换和迁移审计。

## 经验创建

- **从工单提取**：使用当前登录人的 AOPS API Key调用系统 `aops-cli event-center info --id <id>` 和 `aops-cli event-center audit_timeline --id <id>`。程序只保留明确成功、数据库路径完整且通过只读校验的 `sql_exec_read`，再由内网 LLM生成中文名称、摘要、匹配短语、参数语义和依赖。
- **手工编排**：在可拖动、可连线的流程画布中添加 SQL读、条件判断和结束节点。点击节点编辑；点击条件出线配置 JSON Pointer、运算符、比较值和默认分支。SQL参数既可来自运行输入，也可通过 `来源节点 + JSON Pointer` 绑定图上任意可达前置节点的输出。

两种方式均先创建草稿。管理员在流程详情页核对、编辑后发布；发布生成不可变版本，既有运行不受后续编辑影响。

编辑画布和运行/经验查看画布均支持全屏。控制边使用带闭合箭头的贝塞尔曲线；按 `Esc` 可退出全屏。

## 本地开发

```bash
cp .env.example .env
uv sync --extra dev
cd frontend && npm install && npm run build && cd ..
uv run uvicorn app.main:app --host 127.0.0.1 --port 8089
```

另一个终端启动 Worker：

```bash
uv run python -m app.worker
```

开发身份桩和 CLI 桩：

```bash
uv run uvicorn tools.fake_aops:app --host 127.0.0.1 --port 8090
export AOPS_BASE_URL=http://127.0.0.1:8090
export AOPS_CLI_PATH=$PWD/tools/fake-aops-cli
```

## 关键配置

```dotenv
DATABASE_URL=postgresql+asyncpg://user:password@postgres.internal:5432/itsm_workflow
LANGGRAPH_DATABASE_URL=postgresql://user:password@postgres.internal:5432/itsm_workflow
AOPS_BASE_URL=https://aops.internal/aops/api
AOPS_CA_BUNDLE=/etc/pki/tls/certs/ca-bundle.crt
AOPS_CLI_PATH=/usr/local/bin/aops-cli
AOPS_CLI_VERSION_REQUIREMENT=
EMBEDDING_BASE_URL=http://embedding.internal/v1
EMBEDDING_MODEL=BAAI/bge-m3
RERANK_BASE_URL=http://rerank.internal/v1
RERANK_MODEL=BAAI/bge-reranker-v2-m3
RERANK_PATH=/rerank
RERANK_API_FORMAT=auto
WORKFLOW_API_TOKEN=replace-with-long-random-token
WORKFLOW_PUBLIC_URL=http://workflow.internal:8089
MCP_INTERNAL_API_URL=http://127.0.0.1:8089/api/v1
MCP_BIND_HOST=127.0.0.1
MCP_PORT=8090
MCP_PATH=/mcp
MCP_ALLOWED_HOSTS=workflow.internal:*,127.0.0.1:*,localhost:*
MCP_WAIT_MAX_SECONDS=15
WORKFLOW_MASTER_KEY=<Base64编码的32字节密钥>
WORKFLOW_ADMIN_UIDS=S000001,S000002
WORKFLOW_OPERATOR_UIDS=S000001,S000002,S000003
WORKFLOW_BASE_PATH=
KNOWLEDGE_ENVIRONMENT_NAME=生产
```

MCP进程实际读取独立的 `/etc/itsm-workflow/mcp.env`，其中只保留 MCP配置和 `WORKFLOW_API_TOKEN`；不要把数据库密码或 `WORKFLOW_MASTER_KEY` 写入该文件。

部署到 `/aops/itsm-workflow` 等域名子路径时，设置 `WORKFLOW_BASE_PATH=/aops/itsm-workflow`，并使用部署文档中带尾斜杠的 `proxy_pass` 配置。

生成主密钥：

```bash
python3 -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'
```

详细说明：

- [REST API](docs/api.md)
- [部署与运维](docs/deployment.md)
- [架构和恢复语义](docs/architecture.md)
- [服务职责、总体架构与关键流程泳道图](docs/services-and-swimlanes.md)
- [tec01控制面与itsm-workflow计算执行平台设计](docs/tec01-control-plane-split.md)
- [MCP 与 Agent 接入指南](docs/mcp-agent-integration.md)
- [MCP 与 Agent 接入实施计划](docs/mcp-implementation-plan.md)
- [通用节点与卡片扩展平台设计](docs/node-extension-platform.md)
- [统一Node Registry与节点扩展设计](docs/node-registry-design.md)
- [0.6.x版本说明](CHANGELOG.md)
