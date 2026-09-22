# MCP 与 Agent 接入指南

## 当前状态

当前版本提供 Web管理端、REST API、SSE事件流、API Worker、LangGraph执行引擎和独立 `/mcp` Streamable HTTP端点。MCP是现有 REST API 的薄适配层，不在 MCP Server中复制知识检索、状态机、LangGraph或 `aops-cli` 执行逻辑。

具体开发阶段、数据库迁移、工具 Schema、幂等要求和上线顺序见 [MCP 与 Agent 接入实施计划](mcp-implementation-plan.md)。

这样 Hermes 或其他 Agent 框架可以替换，而运行记录、审批、暂停、恢复和审计语义保持一致。

## 总体调用关系

```text
用户 ───────────────► Hermes / 其他 Agent
 │                         │ MCP短调用
 │                         ▼
 │                  Workflow MCP Adapter
 │                         │ 调用现有REST服务
 │                         ▼
 │                  itsm-workflow-api
 │                         │ PostgreSQL队列/checkpoint
 │                         ▼
 │                  itsm-workflow-worker ──► aops-cli ──► AOPS
 │
 └── Web执行详情页 ──► SSE事件流 + 暂停/取消/审批/重试
```

MCP 负责“理解与控制”，Worker 负责“执行”。MCP 工具本身不得启动子进程。

## Agent 能感知哪些运行事实

可以感知，但事实来源必须限定为 PostgreSQL 中已经提交的运行快照、节点 attempt、审批记录和事件序列。Agent 不能根据等待时间、模型推理或对话上下文猜测节点是否完成。

### 可以直接陈述的事实

| 字段或事件 | Agent 可以陈述的事实 |
|---|---|
| `run.status=QUEUED` | 运行已进入队列，但尚不能声称 Worker 已开始执行 |
| `NODE_STARTED` | Worker 已持久化本次 attempt，并开始处理该节点 |
| `nodeStatus=RUNNING` | 节点正在执行；不代表已经取得结果，也不能推算完成百分比 |
| `NODE_SUCCEEDED` | 节点成功条件已经满足，结果 artifact 已持久化 |
| `NODE_FAILED` | 本次 attempt 已明确失败，可展示错误类别和安全摘要 |
| `NODE_SKIPPED` | 条件分支未选择该节点，节点没有执行 |
| `WAITING_*` | 平台正在等待计划确认、参数、节点审批或新凭据 |
| `PAUSED` | 工作流已经到达安全边界并暂停 |
| `SUCCEEDED` | 工作流全部选中路径已经完成 |
| `FAILED` | 工作流已明确失败，不会自行继续 |
| `CANCELLED` | 本地运行已终止；不等于外部系统已经回滚 |
| `UNKNOWN` | 无法确认外部调用最终结果，禁止 Agent 自动重试或宣称成功/失败 |

### 不能直接陈述的事实

- `PAUSE_REQUESTED` 只能说“已请求暂停”，不能说“已经暂停”。正在运行的 CLI 会继续到安全边界。
- `CANCEL_REQUESTED` 只能说“正在请求取消”。即使本地进程被终止，请求也可能已经到达 AOPS。
- SQL节点执行期间没有可靠的百分比。Agent只能展示开始时间、经过时间和已完成节点数。
- 没有 `NODE_SUCCEEDED` 时，不能根据部分 stdout、SSE `open/uuid/title/message` 事件推断成功；SQL读必须等到 `event:done / data:!ok`。
- `workflow_run_wait` 超时且没有新事件表示“没有观察到新事实”，不表示任务卡住或失败。
- Agent不能把缓存的旧快照当成当前状态；每次回答前应比较 `revision/lastEventId`。

### MCP 事实快照格式

后续 MCP Adapter 的 `workflow_run_get` 和 `workflow_run_wait` 应统一返回事实元数据：

```json
{
  "runId": "run_xxx",
  "status": "RUNNING",
  "currentNodeId": "sql-2",
  "progress": {"current": 1, "total": 4},
  "revision": 37,
  "lastEventId": 1821,
  "observedAt": "2026-09-13T10:30:00+08:00",
  "source": "POSTGRES_COMMITTED_STATE",
  "stale": false,
  "terminal": false,
  "events": []
}
```

- `revision` 每次运行状态发生持久化变化时递增，避免 Agent覆盖较新的状态。
- `lastEventId` 是已经返回给 Agent 的最大事件序号，用于增量等待。
- `observedAt` 是服务端读取事实的时间，不使用 Agent本地时间代替。
- `stale=true` 表示 Adapter 无法取得最新数据库状态；此时 Agent只能报告“状态暂不可确认”。
- `source` 固定说明事实来源，不能把 LLM判断混入状态字段。
- `terminal=true` 仅对应 `SUCCEEDED/FAILED/CANCELLED`。

REST和 MCP响应均提供运行状态、节点状态、attempt、事件序号、`revision`、`observedAt`、`stale` 和 `terminal`。

## 建议的 MCP 工具

| 工具 | 对应 REST | 用途 | 是否需要用户确认 |
|---|---|---|---|
| `knowledge_match` | `POST /knowledge/match` | 按工单描述匹配已发布经验 | 否 |
| `knowledge_get` | `GET /knowledge/{id}` | 获取经验、节点和运行参数定义 | 否 |
| `workflow_plan` | `POST /runs/plan` | 创建不可变执行计划 | 否，不会执行 |
| `workflow_run_approve` | `POST /runs/{id}/approve` | 使用 `planHash` 确认并入队 | **是** |
| `workflow_run_get` | `GET /runs/{id}` | 获取完整运行快照 | 否 |
| `workflow_run_wait` | SSE事件流的短等待适配 | 等待新事件，建议 10 秒 | 否 |
| `workflow_run_pause` | `POST /runs/{id}/pause` | 在节点安全边界暂停 | 用户提出即可 |
| `workflow_run_resume` | `POST /runs/{id}/resume` | 继续已暂停运行 | **是** |
| `workflow_run_cancel` | `POST /runs/{id}/cancel` | 请求终止运行或当前 CLI 进程组 | **是** |
| `workflow_interrupt_reply` | `POST /runs/{id}/interrupts/{interruptId}/resume` | 补参或批准风险节点 | **是** |
| `workflow_interaction_options` | `GET /runs/{id}/interrupts/{interruptId}/options` | 分页读取允许展示的HITL候选 | 否 |
| `workflow_hitl_form_reply` | 自动定位当前OPEN交互后调用恢复接口 | 只传runId和表单values，其余协议字段由程序补齐 | **是** |
| `workflow_hitl_select_reply` | 同上 | 只传runId和candidateIds，其余协议字段由程序补齐 | **是** |
| `workflow_run_credential_refresh` | `POST /runs/{id}/credential` | 从新的连接Header刷新运行凭据 | 用户先更新连接凭据 |
| `workflow_node_result_get` | `GET /runs/{id}/nodes/{nodeId}/artifact` | 分页读取本次运行节点的真实结果 | 否，但必须遵守运行权限 |
| `workflow_node_retry` | `POST /runs/{id}/nodes/{nodeId}/retry` | 重试失败/未知节点 | **是** |

工具返回统一包含：

```json
{
  "runId": "run_xxx",
  "status": "RUNNING",
  "currentNodeId": "sql-2",
  "progress": {"current": 1, "total": 4},
  "waitingReason": null,
  "lastEventId": 1821,
  "runUrl": "http://workflow.internal:8089/runs/run_xxx"
}
```

`workflow_run_wait` 不应无限阻塞。建议参数：

```json
{"runId":"run_xxx","afterEventId":1821,"waitSeconds":10}
```

服务最多等待 10–15 秒；有事件立即返回，没有事件返回当前状态。这样 Agent 能持续输出进展，也能及时收到用户的暂停或取消指令。

## 身份与 Hermes 配置

API Key 不应作为每次工具调用的参数出现。MCP Adapter 从连接 Header 读取身份：

```yaml
mcp_servers:
  itsm-workflow:
    url: http://workflow.internal:8089/mcp
    headers:
      Authorization: Bearer ${WORKFLOW_API_TOKEN}
      X-AOPS-Api-Key: ${AOPS_API_KEY}
    connect_timeout: 10
    timeout: 30
    tools:
      include:
        - knowledge_match
        - knowledge_get
        - workflow_plan
        - workflow_run_approve
        - workflow_run_get
        - workflow_run_wait
        - workflow_run_pause
        - workflow_run_resume
        - workflow_run_cancel
        - workflow_interrupt_reply
        - workflow_interaction_options
        - workflow_hitl_form_reply
        - workflow_hitl_select_reply
        - workflow_run_credential_refresh
        - workflow_node_result_get
        - workflow_node_retry
```

MCP Adapter会把两个 Header传给 REST身份层，但不会记录或返回 `X-AOPS-Api-Key`。如果反向代理没有保留尾部路径，请将 URL配置成最终可访问且不发生跨域重定向的地址。

## Agent 标准操作流程

### 1. 匹配经验

Agent 从用户描述中取得工单 ID和查询意图，调用 `knowledge_match`：

```json
{"ticketId":100173,"query":"查询客户王五信息","targetSystems":["crm"],"limit":3}
```

- 没有工单 ID：先向用户索要。
- 一个高置信候选：展示摘要，但仍不能直接执行。
- 多个候选：展示名称、摘要、系统范围和分数，让用户选择。
- 没有可靠候选：停止，不自行拼装生产命令。

### 2. 收集参数并创建计划

读取选中经验，只询问 `source.kind=RUN_INPUT` 的参数。`NODE_OUTPUT` 参数由运行引擎从前置节点结果中解析，不向用户询问。

调用 `workflow_plan` 后完整展示：

- 当前工单 ID和 `#uatu-<id>`。
- 工作流版本和计划哈希。
- 全部节点、数据库路径、渲染 SQL、安全级别。
- 条件分支规则。
- 每个输入来自运行参数还是前置节点 JSON Pointer。

此时运行状态必须为 `WAITING_PLAN_APPROVAL`，Worker 尚未执行。

### 3. 用户确认后批准

只有用户明确表达“确认执行”后，Agent 才能调用 `workflow_run_approve`，并回传原计划的 `planHash`。确认前不得把创建计划等同于执行授权。

批准后立即告诉用户：

```text
运行已创建：run_xxx
当前状态：排队中
进度：0/4
详情：http://workflow.internal:8089/runs/run_xxx
你可以随时说“暂停”“取消”，也可以直接在详情页操作。
```

### 4. 展示实时进度

Agent 循环调用 `workflow_run_wait`，每次最长等待10秒。每次返回包含紧凑事实快照、增量事件和 `displayText`；Agent必须先把 `displayText`反馈给用户，再调用下一次等待：

```text
[1/4] 查询客户编号：完成，返回 1 行
[2/4] 查询客户状态：执行中
```

不要伪造百分比。整体进度使用终态节点数量除以总节点数；正在运行的 SQL 只显示经过时间。

### 5. 处理中断

| 状态 | Agent 行为 |
|---|---|
| `WAITING_INPUT` | 展示缺失字段并向用户提问，提交 `workflow_interrupt_reply` |
| `WAITING_INPUT / HITL_SELECT` | 先调用 `workflow_interaction_options`，展示候选；确认后调用 `workflow_hitl_select_reply` |
| `WAITING_INPUT / HITL_FORM` | 按字段Schema逐项收集并汇总确认，再调用 `workflow_hitl_form_reply`；不得猜测字段值 |
| `WAITING_NODE_APPROVAL` | 展示风险节点完整计划，等待明确批准 |
| `WAITING_CREDENTIAL` | 要求用户重新认证；API Key 不出现在对话或日志中 |
| `PAUSED` | 告知已在安全边界暂停，等待继续或取消 |
| `FAILED` | 展示 `errorCode`、事件 `payload.errorMessage` 和安全摘要，询问是否重试失败节点；完整脱敏 stdout/stderr 由运行发起人在控制台“查看诊断”中读取 |
| `UNKNOWN` | 明确提示可能已经到达 AOPS，让用户先查审计记录，再选择重试或标记失败 |

### 6. 完成

完成后，对 `resultAvailableNodes` 中与用户请求相关的 SQL节点调用 `workflow_node_result_get`，分页读取本次运行的加密 artifact，再输出每个节点的实际行数、安全业务摘要、分支选择和总耗时。严禁使用历史记忆、旧工单结果或直接调用 `aops-cli` 代替本次 artifact。完整敏感结果应在有权限的 Web详情页查看，不复制到公共聊天。

## 用户如何随时中断

平台提供两条互不依赖的控制通道：

1. **Web 控制台**：运行详情页持续订阅 SSE，即使 Agent正在等待工具返回，用户仍能点击暂停或取消。
2. **Agent 指令**：Agent 每次 MCP事件等待不超过 10–15 秒，收到“暂停”“停止”“取消”后立即调用对应控制工具。

暂停只在节点安全边界生效，不冻结正在执行的外部进程。取消会终止当前 CLI 进程组，但如果请求已经到达 AOPS，平台不会声称已回滚操作；这种情况通过审计和 `UNKNOWN` 语义处理。

推荐用户语义映射：

| 用户说法 | 操作 |
|---|---|
| “暂停、先等等” | `workflow_run_pause` |
| “继续、恢复” | `workflow_run_resume` |
| “取消、停止执行” | 先复述影响，再调用 `workflow_run_cancel` |
| “重试失败步骤” | `workflow_node_retry(decision=retry)` |
| “确认这个未知步骤没成功” | `workflow_node_retry(decision=mark_failed)` |

## Agent 系统提示词建议

```text
你通过 ITSM Workflow MCP 管理生产工作流。你不能直接执行 aops-cli。
先匹配经验；多候选必须让用户选择。收集工单 ID和所有 RUN_INPUT参数后创建计划。
完整展示计划、数据库、SQL、条件分支、数据绑定和 #uatu 工单备注，并等待明确确认。
只有确认后才能批准 planHash。批准后返回 runId和运行详情链接。
使用最长10秒的事件等待持续报告节点变化；每次wait返回后先展示displayText，再调用下一次wait，不要连续静默调用。
用户要求暂停或取消时立即调用相应工具。WAITING_INPUT和节点审批必须向用户提问。
运行成功后调用workflow_node_result_get读取本次真实结果，禁止用记忆或历史结果代替。FAILED只能在用户确认后重试；UNKNOWN必须警告请求可能已经到达AOPS，禁止自动重放。
不要输出、记录或要求用户在聊天中粘贴AOPS API Key。
HITL_SELECT必须先读取并展示候选，只能通过workflow_hitl_select_reply提交服务返回的candidateId；HITL_FORM必须逐项收集并在提交前汇总确认，然后通过workflow_hitl_form_reply提交values。顶层参数是values而不是payload。单候选也必须确认。取消HITL统一调用workflow_run_cancel。不要猜测参数包装结构，不要在对话中复述未声明的隐藏候选字段。
```

## MCP Adapter 验收要求

- MCP 返回的候选、计划、状态和事件与 REST 完全一致。
- 创建计划和批准计划必须是两个独立工具。
- 所有变更工具可重试但必须具备请求幂等键，避免 Agent网络重试造成重复动作。
- `workflow_run_wait` 有严格的最大等待时间，客户端断开后立即取消等待。
- 每个响应都返回 `runUrl` 和最新 `lastEventId`。
- 未授权用户对私有经验和运行统一得到 `NOT_FOUND`。
- Header、API Key、凭据密文不进入日志、事件和 MCP 响应。
- MCP Adapter异常不能影响 API和 Worker进程。
- Web和MCP并发回复同一HITL时只能有一个成功；另一方收到已处理冲突并刷新运行事实。
