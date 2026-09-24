# ITSM Workflow Runtime

`0.9.0`开始，本仓库按tec01主动调度架构承担Python计算执行侧：

- Node Registry与版本化Schema。
- 发布前Workflow DAG校验和计划预览。
- 工单证据过滤、草稿编译与中文摘要提炼。
- `sql_read`、`condition`、`hitl_select`、`hitl_form`等节点执行适配器。
- 与tec01交互的Compiler/Executor协议客户端。
- 使用管理Token登录、可独立运行的TEST_ONLY Studio。

生产知识、权限、MCP、运行状态、Artifact、Checkpoint和审计属于tec01，本服务不再保存这些数据，也不再提供MCP Server。

## 本地启动

```bash
cp .env.example .env
uv sync
cd frontend && npm ci && npm run build && cd ..
uv run uvicorn app.main:app --host 127.0.0.1 --port 8089
```

打开：

```text
http://127.0.0.1:8089/studio
```

进入Studio需要`STUDIO_ADMIN_TOKEN`。登录后可以管理Registry Node的Studio启停和调试默认值。只有TEST模式真实执行SQL读或按工单ID提取时才填写`AOPS_API_KEY`；浏览器只把它放入当前请求的`X-AOPS-Api-Key`头，服务端只传入本次`aops-cli`子进程环境，不写SQLite、配置或日志。

## 保留的进程

| 服务 | 默认 | 职责 |
|---|---:|---|
| `itsm-workflow-api` | 启用 | Studio、Catalog、校验、计划、Compiler Preview和静态页面 |

生产架构由tec01主动提交草稿提取任务、下发完整Workflow和暂停/取消命令；Executor逐节点返回状态和结果。tec01同时承担Hermes消息渠道、MCP和Web页面，所有入口共享同一运行事实。

正式计划由tec01从已发布版本创建；itsm-workflow在发布前校验节点和DAG，下发执行时复核版本。运行期间使用条件判断与HITL完成参数交互。内网LLM只用于草稿提取阶段。

旧的`itsm-workflow-mcp`、`itsm-workflow-worker`和`itsm-workflow-migrate`已删除。升级安装时安装器会停用并移除这些旧unit。

## 文档

- [独立调试教程](docs/standalone-studio-guide.md)
- [部署手册](docs/deployment.md)
- [REST API](docs/api.md)
- [精简后架构](docs/architecture.md)
- [tec01控制面拆分设计](docs/tec01-control-plane-split.md)
- [tec01集成契约](docs/tec01-integration-contract.md)
- [Node Registry与节点扩展](docs/node-registry-design.md)

## 验证

```bash
uv run pytest -q
cd frontend && npm run build
```
