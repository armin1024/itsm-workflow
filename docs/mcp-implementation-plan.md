# MCP 与 Agent 接入实施计划

## 1. 目标与实施状态

计划中的事实快照、紧凑短等待 REST接口、幂等账本、独立 Streamable HTTP MCP服务、16个 Agent工具、强类型HITL回复、候选分页、节点结果读取、systemd单元和协议测试已经实现。保留本文作为实现结构、验收和后续扩展依据。

在不改变现有知识、LangGraph、Worker 和 `aops-cli` 执行语义的前提下，新增独立 MCP Adapter，使 Hermes 或其他 Agent 能够完成：

```text
经验匹配 → 候选选择 → 参数收集 → 创建计划 → 用户确认
→ 批准执行 → 感知节点进展 → 暂停/取消/补参/审批/重试 → 完成汇总
```

MCP Adapter 只调用现有 REST API，不直接连接业务数据库、不读取 checkpoint、不执行 `aops-cli`。

## 2. 实施边界

### 本期包含

- 独立 Streamable HTTP MCP服务。
- 知识匹配、知识详情、计划创建和批准工具。
- 运行事实查询、短等待、暂停、恢复和取消工具。
- 中断回复、失败重试和未知结果处理工具。
- 运行 revision、事件游标、事实读取时间和运行详情链接。
- 变更请求幂等控制。
- Hermes 配置、Agent系统提示词、接口文档和离线安装。

### 本期不包含

- MCP Server直接执行 CLI。
- 在 MCP中复制 LangGraph。
- 要求所有 Agent宿主支持自定义 React画布。
- 依赖永久 MCP连接完成运行监控。
- 自动重试 `FAILED` 或 `UNKNOWN` 节点。
- 让 LLM判断条件分支或生产操作是否成功。

## 3. 目标架构

```text
Hermes / Agent Host
        │ Streamable HTTP
        │ Authorization + X-AOPS-Api-Key
        ▼
itsm-workflow-mcp.service :8090
        │ loopback REST
        ▼
itsm-workflow-api.service :8089
        │
        ├── PostgreSQL committed state / events
        └── queue
             ▼
itsm-workflow-worker.service
             ▼
          aops-cli
```

采用单独 systemd进程的原因：

- MCP协议、会话或客户端异常不影响管理页面和 REST API。
- Adapter 可以独立升级和回滚。
- 所有生产状态变化仍经过同一 REST权限与审计逻辑。
- 后续替换 Hermes 或 MCP SDK时不影响 Worker。

## 4. 技术选型

- SDK：官方 `modelcontextprotocol/python-sdk` v2稳定线，依赖锁定为 `mcp>=2,<3`。
- 传输：Streamable HTTP；不新建旧版 HTTP+SSE传输。
- 模式：stateless HTTP，普通工具优先返回 JSON响应。
- 地址：MCP服务监听 `127.0.0.1:8090`，由内网反向代理暴露 `/mcp/`。
- 内部调用：`httpx.AsyncClient` 调用 `http://127.0.0.1:8089/api/v1`。
- 身份：从每个 MCP请求的 Header提取服务 Token和用户 AOPS API Key，原样传给 REST身份层。
- 状态事实：只使用 PostgreSQL中已提交的运行快照、attempt和事件。

官方 SDK 的 Streamable HTTP ASGI应用可独立部署，并兼容新旧协议协商；生产代理必须配置允许的 Host/Origin，避免 DNS rebinding。

## 5. 数据模型调整

### `workflow_runs`

新增：

```text
revision BIGINT NOT NULL DEFAULT 1
```

每次 `emit_event()` 前对当前运行执行：

```text
revision = revision + 1
```

事件和快照在同一事务提交，保证 Agent不会看到事件已更新但运行状态仍旧的组合。

### `workflow_control_requests`

新增幂等审计表：

```text
id
idempotency_key
run_id
action
actor_uid
request_hash
response_status
response_payload
created_at
```

唯一约束：

```text
(actor_uid, idempotency_key, action)
```

相同键、相同请求返回第一次结果；相同键、不同请求返回 `409 IDEMPOTENCY_CONFLICT`。

## 6. REST事实接口补强

### 运行快照

现有 `GET /runs/{runId}` 增加：

```json
{
  "revision": 37,
  "lastEventId": 1821,
  "observedAt": "2026-09-13T10:30:00+08:00",
  "source": "POSTGRES_COMMITTED_STATE",
  "stale": false,
  "terminal": false,
  "runPath": "/runs/run_xxx"
}
```

`terminal=true` 只对应 `SUCCEEDED/FAILED/CANCELLED`。

### 短等待接口

新增：

```http
GET /api/v1/runs/{runId}/wait?afterEventId=1821&timeoutSeconds=10
```

行为：

1. 校验当前 UID是否有权查看运行。
2. 查询 `sequence > afterEventId` 的事件。
3. 有事件立即返回。
4. 没有事件时等待数据库通知或超时。
5. 最长等待 15 秒，参数超限返回 422。
6. 返回增量事件和同一时刻的运行事实快照。
7. 客户端断开时立即结束等待。

第一版允许使用 250–500ms有界查询实现；稳定后切换 PostgreSQL `LISTEN/NOTIFY`，外部响应 Schema保持不变。

### 幂等请求

下列 REST接口支持 `Idempotency-Key`：

- 计划创建。
- 计划批准。
- 暂停、恢复和取消。
- 中断回复。
- 节点重试或标记失败。

## 7. MCP 工具清单

### 7.1 `knowledge_match`

输入：

```json
{
  "ticket_id": 100173,
  "query": "查询客户王五信息",
  "target_systems": ["crm"],
  "limit": 3
}
```

输出候选摘要、检索诊断和 `selectionRequired`。只返回当前 UID可见且已发布的经验。

### 7.2 `knowledge_get`

输入：

```json
{"knowledge_id":"knw_xxx","ticket_id":100173}
```

输出完整不可变工作流定义、节点、边、运行参数 Schema、数据绑定、条件规则和 `#uatu-100173`。

MCP路径即使当前用户为管理员，也不能返回未发布草稿。

### 7.3 `workflow_plan`

输入：

```json
{
  "knowledge_id": "knw_xxx",
  "ticket_id": 100173,
  "parameters": {"customer_name":"王五"},
  "idempotency_key": "agent-turn-uuid"
}
```

创建 `WAITING_PLAN_APPROVAL` 运行，不执行节点。输出完整计划、`planHash`、参数来源和运行详情链接。

### 7.4 `workflow_run_approve`

输入：

```json
{
  "run_id":"run_xxx",
  "plan_hash":"...",
  "idempotency_key":"agent-turn-uuid"
}
```

只有 Agent已取得用户明确确认时才能调用。哈希不一致返回 `PLAN_CHANGED`。

### 7.5 `workflow_run_get`

返回完整事实快照，不进行等待。

### 7.6 `workflow_run_wait`

输入：

```json
{"run_id":"run_xxx","after_event_id":1821,"wait_seconds":10}
```

输出增量事件、最新快照和新的 `lastEventId`。无新事件不是错误，返回 `events=[]`。

### 7.7 运行控制工具

```text
workflow_run_pause
workflow_run_resume
workflow_run_cancel
```

全部要求 `run_id + idempotency_key`。返回真实状态，例如暂停请求可能先返回 `PAUSE_REQUESTED`，不能伪装为 `PAUSED`。

### 7.8 `workflow_interrupt_reply`

用于：

- `WAITING_INPUT` 补参。
- `WAITING_NODE_APPROVAL` 批准或拒绝。
- `WAITING_CREDENTIAL` 只引导用户重新认证，不把 API Key放进普通工具参数。

### 7.9 `workflow_node_retry`

输入：

```json
{
  "run_id":"run_xxx",
  "node_id":"sql-2",
  "decision":"retry",
  "idempotency_key":"agent-turn-uuid"
}
```

`mark_failed` 只允许 `UNKNOWN`。两种决定都必须记录操作者和事件。

### 7.10 `workflow_run_credential_refresh`

用户更新 MCP连接 Header中的 `X-AOPS-Api-Key` 并重新连接后调用。Adapter从请求 Header读取新凭据并转给运行凭据接口；API Key不出现在工具输入 Schema、Agent对话或工具结果中。

### 7.11 `workflow_node_result_get`

按 `run_id + node_id + offset + limit` 分页读取当前运行节点的加密 artifact。默认100行、最多200行；返回列、当前页、总行数和 `hasMore`。Agent只能用本次运行结果生成业务结论，不能引用记忆或历史工单代替。

## 8. MCP 输出约定

每个工具同时提供：

- `structuredContent`：Agent和自定义 UI用于确定性解析。
- 简短文本摘要：不支持结构化渲染的终端 Agent使用。
- `runUrl` 或资源链接：打开统一 Web执行详情。

不得只返回自然语言。节点和事件必须保留稳定 ID，Agent UI按 ID合并状态，而不是重新让 LLM理解整张图。

事实快照统一字段：

```text
runId / status / revision / lastEventId / observedAt
source / stale / terminal / currentNodeId / progress
nodeStatuses / events / runUrl
```

## 9. Agent运行循环

```python
matches = knowledge_match(ticket_id, query)
knowledge = user_select(matches)
parameters = ask_only_run_inputs(knowledge)

plan = workflow_plan(knowledge.id, ticket_id, parameters, new_idempotency_key())
show_full_plan(plan)
wait_for_explicit_confirmation()

run = workflow_run_approve(plan.run_id, plan.plan_hash, new_idempotency_key())
cursor = run.last_event_id
show_run_url(run.run_url)

while not run.terminal:
    update = workflow_run_wait(run.run_id, cursor, wait_seconds=10)
    cursor = update.last_event_id
    render_changed_nodes(update.events)
    if update.status in WAITING_STATES:
        ask_user_and_resume(update)
        break
    run = update
```

Agent等待调用必须短于 Hermes工具超时，并周期性回到对话处理用户输入。

## 10. 用户中断与进度展示

### 双通道控制

1. Agent通过 MCP控制工具暂停或取消。
2. Web详情页通过 SSE独立显示和控制同一个运行。

即使 Agent正在等待 MCP响应，Web页面仍能操作。下一次 `workflow_run_wait` 会读取 Web操作产生的新 revision和事件。

### Agent UI渲染

支持自定义 renderer 的 Agent：

- 首次使用 `workflow` 中的 nodes、edges、uiPosition绘制画布。
- 后续按 `nodeStatuses[nodeId]` 更新卡片颜色。
- 按事件增量追加时间线。
- 控制按钮调用对应 MCP工具。

不支持自定义 renderer 的 Agent显示文本进度，并提供 `runUrl`：

```text
✓ 1/4 查询客户编号
● 2/4 查询客户状态
○ 3/4 条件判断
○ 4/4 完成
```

## 11. 身份与安全

- MCP请求必须同时携带 `Authorization: Bearer <WORKFLOW_API_TOKEN>` 和 `X-AOPS-Api-Key`。
- Adapter不落库、不缓存、不记录 AOPS API Key。
- Adapter日志只记录 UID、工具名、runId、耗时和结果类别。
- 工具发现和工具调用都必须认证。
- MCP进程只访问 loopback API，不持有 PostgreSQL凭据和 `WORKFLOW_MASTER_KEY`。
- 生产 Host/Origin使用精确 allowlist。
- 内部 REST连接设置连接、读取和总超时；超时返回 `FACTS_UNAVAILABLE`，不得返回旧状态冒充最新事实。

## 12. 配置与进程

新增配置：

```dotenv
WORKFLOW_PUBLIC_URL=http://workflow.internal:8089
MCP_INTERNAL_API_URL=http://127.0.0.1:8089/api/v1
MCP_BIND_HOST=127.0.0.1
MCP_PORT=8090
MCP_PATH=/mcp
MCP_ALLOWED_HOSTS=workflow.internal:*,127.0.0.1:*,localhost:*
MCP_ALLOWED_ORIGINS=http://workflow.internal
MCP_WAIT_MAX_SECONDS=15
```

这些配置和 `WORKFLOW_API_TOKEN` 写入独立 `/etc/itsm-workflow/mcp.env`。MCP systemd单元不读取主 `service.env`，因此不会获得 PostgreSQL URL或 `WORKFLOW_MASTER_KEY`。

新增：

```text
itsm-workflow-mcp.service
```

它依赖网络和 API健康检查，但不作为 API或 Worker的启动依赖。

## 13. 实施阶段

### 阶段 A：事实快照基础

- 增加 `workflow_runs.revision` 和控制请求幂等表。
- `emit_event()` 同事务递增 revision。
- 扩展运行序列化字段。
- 实现 REST短等待接口。
- 为所有控制接口增加幂等键。

验收：并发读写、断线续读、重复请求和事件/快照一致性测试通过。

### 阶段 B：只读 MCP

- 引入官方 MCP SDK。
- 建立独立 Streamable HTTP进程和 Header认证中间件。
- 实现 `knowledge_match`、`knowledge_get`、`workflow_run_get`、`workflow_run_wait`。
- 使用 MCP Inspector和 Hermes验证工具发现。

验收：用户只能读取自己可见的已发布知识和运行；等待工具严格按游标增量返回。

### 阶段 C：计划与控制 MCP

- 实现计划创建和计划批准。
- 实现暂停、恢复、取消、中断回复和节点重试。
- 接入幂等键和操作审计。
- 验证 Agent网络重试不会重复创建运行或重复批准。

验收：所有生产状态变化与直接 REST调用结果一致。

### 阶段 D：Hermes交互

- 配置 MCP Header和工具 include清单。
- 加入 Agent系统提示词。
- 实现多候选选择、动态参数问答、完整计划确认和10秒事件循环。
- 在每次运行开始后返回 `runUrl`。

验收：完整走通匹配、确认、执行、条件分支、补参、暂停、恢复、失败重试和结果未知处理。

### 阶段 E：UI渲染与发布

- 为支持结构化工具结果的 Agent提供节点卡片 renderer契约。
- 不支持 renderer 时验证文本降级。
- 增加 MCP systemd、Nginx和离线包配置。
- 完成安全、负载、故障注入和升级回滚测试。

## 14. 测试计划

### 协议与兼容性

- MCP工具发现和输入 Schema。
- Streamable HTTP新旧客户端协商。
- Hermes configured/tool-count状态验证。
- Header缺失、错误 Token、过期 AOPS Key。

### 事实一致性

- 每个事件与 run revision同事务提交。
- `afterEventId` 边界、重复游标和断线重连。
- wait超时返回空事件但包含最新快照。
- API不可用时返回 `stale=true/FACTS_UNAVAILABLE`，不伪造状态。

### 控制与幂等

- 计划创建重复调用只生成一个 runId。
- 批准、暂停、恢复、取消和重试重复调用结果一致。
- 相同幂等键配不同请求返回冲突。
- `UNKNOWN` 不自动重试，`mark_failed` 仅限 UNKNOWN。

### Agent完整流程

- 单候选、多候选和无候选。
- 参数按类型追问，`NODE_OUTPUT` 不追问。
- 用户未确认时绝不批准。
- 运行中通过 Agent暂停以及通过 Web暂停。
- WAITING_INPUT、审批、凭据过期和失败重试。
- Agent重启后通过 runId和 lastEventId恢复观察。

### 安全与性能

- 日志、MCP响应、事件和数据库中无明文 API Key。
- 未授权 UID无法通过 ID枚举运行或私有知识。
- 100个并发 wait调用不耗尽数据库连接池。
- 单工具调用严格受最大超时和响应大小限制。

## 15. 上线和回滚

上线顺序：

1. 备份 PostgreSQL。
2. 发布数据库迁移和 REST事实字段。
3. 先保持 MCP服务关闭，验证现有 UI/Worker无回归。
4. 启动 MCP服务，仅开放只读工具。
5. Hermes灰度接入只读匹配和观察。
6. 开放计划工具，但保持批准工具关闭。
7. 最后开放批准和运行控制工具。

回滚时先从 Hermes移除变更工具，再停止 MCP服务。API和 Worker不依赖 MCP，因此已有运行继续执行和展示；新增数据库字段保留，不执行破坏性降级。

## 16. 完成标准

- Hermes能稳定显示 MCP工具数量并完成工具调用。
- Agent从匹配到完成全流程不直接执行 `aops-cli`。
- 用户确认前不会进入 `QUEUED`。
- Agent和 Web看到相同 revision、状态和事件。
- 用户可从 Agent或 Web暂停、取消和恢复。
- Agent重连后不会遗漏或重复展示事件。
- `FAILED` 和 `UNKNOWN` 均遵守人工确认规则。
- MCP故障不影响 API、Worker及已有运行。

## 17. 技术参考

- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [Python SDK：在现有 ASGI应用中部署](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/run/asgi.md)
- [Python SDK：Streamable HTTP运行方式](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/run/index.md)
- [MCP Streamable HTTP规范](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/basic/transports/streamable-http.mdx)
