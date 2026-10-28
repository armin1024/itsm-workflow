# itsm-workflow 回调 tec01 的接口

> 调用方向：itsm-workflow（执行侧）→ tec01
>
> 配套接口：[itsm-workflow 提供给 tec01 的接口](itsm-workflow-to-tec01-api.md)
>
> 总体设计：[tec01 与 itsm-workflow 拆分设计](tec01-control-plane-split.md)

文件名沿用历史命名 `tec01-to-itsm-workflow-api.md`；按调用方向，本文描述的是 itsm-workflow 回调 tec01 的接口。

## 1. 定位与调用方向

tec01 提供两个统一回调入口，接收执行侧的提取进度、节点开始/完成和运行释放。tec01 先在同一事务内保存状态、结果、诊断和事件，提交后再向页面 SSE 与 Hermes 消息渠道推送。

执行侧只负责上报事实；tec01 是运行状态、节点状态、结果和人工交互的唯一权威。`HTTP 200` 表示 tec01 已提交并**采纳**本次回调，执行器收到 200 后才能继续下一节点；`409` 表示本次回调**未被采纳**（旧调度已失效，或运行已进入终态），执行器必须立即停止，不得再执行后续节点，也不得重试。

```text
提取：itsm-workflow --PROGRESS/FINISHED--> tec01
运行：itsm-workflow --NODE_STARTED/NODE_COMPLETED/RUN_RELEASED--> tec01
```

默认地址为 `http://tec01:3421`，所有路径均相对于该地址；请求与响应使用 UTF-8 JSON。

## 2. 通用约定

### 2.1 认证与请求头

```http
Authorization: Bearer <TEC01_SERVICE_TOKEN>
Content-Type: application/json
X-Request-Id: <稳定的回调幂等键>
```

`X-Request-Id` 在重试期间必须保持不变，正文也保持不变。运行回调按 `runId + dispatchId + type + attemptId`（释放回调无需 attemptId）识别同一事实；提取回调按 `knowledgeId + requestId` 隔离提取尝试，再按 `seq` 或本次终态识别重复。复用同一 `X-Request-Id` 便于两侧排查同一次上报。

### 2.2 成功响应

运行回调成功返回 `HTTP 200` 与 `AopsResult`：

```json
{"status":200,"msg":"success","data":{"runId":"run_100173","status":"RUNNING","lastEventId":1}}
```

`data.lastEventId` 是该运行已提交的最新事件序号，可作为运行侧 SSE 的起始游标。

> 首次被采纳的运行回调，以及当前调度内已保存的完全相同回调重试，返回 `AopsResult`。后者即使运行已进入终态，也应返回 200 确认原事实，不重复落库。旧调度仍优先返回 409；未保存的迟到节点回调、终态冲突等未采纳回调也返回 409，执行器收到后应停止而不是重试。处理优先级见 4.4。

提取回调成功返回：

```json
{"status":200,"msg":"success","data":{"knowledgeId":"knw_100173","status":"EXTRACTING"}}
```

`data.status` 是 tec01 当前的知识状态。

### 2.3 错误响应

业务失败用 HTTP 状态表达：

| HTTP 状态 | 含义 |
|---|---|
| 401 | 服务身份无效 |
| 404 | 运行或知识不存在 |
| 409 | 回调未被采纳：旧调度已失效（`CALLBACK_DISPATCH_STALE`）或运行已进入终态（`CALLBACK_RUN_TERMINAL`）；执行器应停止执行，不要重试 |
| 422 | 字段缺失、类型错误或枚举非法 |
| 5xx | tec01 处理失败，可重试 |

错误结构：

```json
{"code":"CALLBACK_STATUS_INVALID","message":"status 取值不合法","retryable":false}
```

不得在 `HTTP 200` 中使用响应体的 `status` 表示失败。执行器只看 HTTP 状态与 `retryable`，不解析 `msg` 做业务判断。

### 2.4 重试与幂等

- 执行侧对 5xx、连接异常和超时进行最多 5 次额外重试，退避 1/2/4/8/16 秒。
- 重试始终使用相同的 `X-Request-Id`；4xx 不自动重试。
- 重复回调在**当前调度且对应消息已保存、正文一致**时返回 200，即使运行已进入终态也不按迟到回调拒绝；不得重复追加事件、覆盖结果或回退状态。
- `NODE_STARTED` 与 `NODE_COMPLETED` 共用 `attemptId`，但幂等键后缀不同；同一节点的同一次尝试不会被重复开始或重复完成。
- **旧调度**（`dispatchId` 与当前运行最近一次下发不一致）返回 **409 `CALLBACK_DISPATCH_STALE`**，不改变数据。执行器把 4xx 当不可重试错误，收到后必须立即停止，不能继续执行后续节点——这是阻止“被新下发接管的旧执行器继续跑 SQL”的机制。
- **终态冲突**（排除已保存且完全相同的重试后，运行已 `SUCCEEDED`/`FAILED`/`TERMINATED`/`UNKNOWN`，又收到未保存的迟到节点回调，或状态不一致的 `RUN_RELEASED`）返回 **409 `CALLBACK_RUN_TERMINAL`**，不覆盖更权威的终态。必须先判断旧调度，再识别已保存重复，最后判断终态冲突。

| 回调 | X-Request-Id | tec01 处理 |
|---|---|---|
| NODE_STARTED | `{attemptId}:started` | 当前调度中相同开始消息已保存则确认200；旧调度/未保存的终态冲突返回409 |
| NODE_COMPLETED | `{attemptId}:completed` | 当前调度中相同完成消息已保存则确认200；旧调度/未保存的终态冲突返回409 |
| RUN_RELEASED | `{dispatchId}:released` | 当前调度中相同释放消息已保存则确认200；首次释放需不与终态冲突 |
| PROGRESS | `{knowledgeId}:{requestId}:{seq}` | 按本次requestId的最大已接受seq维护水位，重复或更旧进度忽略 |
| FINISHED | `{knowledgeId}:{requestId}:finished` | 同次requestId只接受第一个终态；不同请求互相隔离 |

### 2.5 时间、大小与安全

- `occurredAt` 使用带时区偏移的 ISO-8601 时间，tec01 换算成服务器本地时区后保存；缺失或无法解析时使用 tec01 当前时间。
- `result` 直接放在回调中，SQL 读结果上限 20 MiB；tec01 负责持久化，执行器不保存生产结果。
- `diagnostic` 必须是脱敏后的错误码、消息、退出码和截断后的 stdout/stderr；不得包含 Token、API Key、模型网关响应正文或历史实际 SQL 参数值。
- `safeSummary` 是可读摘要，不得包含凭据。
- 回调中不得出现 AOPS API Key、Authorization Header 或其他生产凭据。

## 3. 接口索引

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/internal/v1/runs/{runId}/callbacks` | 节点开始、节点完成、运行释放 |
| POST | `/internal/v1/extractions/{knowledgeId}/callbacks` | 提取进度、成功、失败 |

## 4. 运行回调

所有运行回调都携带 `type` 与 `dispatchId`。节点回调还必须携带 `attemptId` 和 `nodeId`；`dispatchId` 缺失返回 422，不能按“旧调度”猜测处理。

同一运行的节点回调按执行顺序串行发送：先 `NODE_STARTED`，后 `NODE_COMPLETED`；第一版不支持并行扇出，不存在同一运行的并发节点回调。

| 字段 | 必填 | 说明 |
|---|---|---|
| type | 是 | `NODE_STARTED`/`NODE_COMPLETED`/`RUN_RELEASED` |
| dispatchId | 是 | 本次下发 ID，用于挡过期回调 |
| attemptId | 节点回调是 | 执行器生成的节点尝试 ID |
| nodeId | 节点回调是 | 节点 ID |
| status | 完成/释放是 | 节点或运行状态 |
| runStatus | 否 | 执行器视角的运行状态参考值 |
| safeSummary | 否 | 不含凭据的可读摘要 |
| result | 否 | 完整节点结构化输出 |
| diagnostic | 否 | 脱敏错误码、消息、退出码、stdout/stderr |
| interaction | WAITING_INPUT 时是 | 人工表单，见第 5 节 |
| selectedEdgeId | 否 | 条件节点命中的出边 |
| skippedNodeIds | 否 | 未命中路径的节点 |
| startedAt | NODE_STARTED 否 | 节点开始时间 |
| currentNodeId | RUN_RELEASED 否 | 释放时所在节点 |
| reason | RUN_RELEASED 否 | 释放原因 |

### 4.1 NODE_STARTED

```json
{
  "type":"NODE_STARTED",
  "dispatchId":"dispatch_100173_1",
  "attemptId":"attempt_1",
  "nodeId":"sql-1",
  "startedAt":"2026-10-16T10:00:00+08:00"
}
```

tec01 处理：

1. 锁定运行并校验 `dispatchId` 是当前下发；旧调度返回 **409 `CALLBACK_DISPATCH_STALE`**，不落任何数据。
2. 插入 `STARTED` attempt；相同 `attemptId` 已存在则忽略。
3. 设置节点 `RUNNING`、当前节点为该节点；首次收到节点回调时运行从 `QUEUED` 转为 `RUNNING`。
4. 续租并追加 `NODE_STARTED` 事件，事务提交后再推送页面/Hermes。

节点开始回调只表示“开始执行”，不代表 SQL 已经发出或成功。

### 4.2 NODE_COMPLETED

```json
{
  "type":"NODE_COMPLETED",
  "dispatchId":"dispatch_100173_1",
  "attemptId":"attempt_1",
  "nodeId":"sql-1",
  "status":"SUCCEEDED",
  "runStatus":null,
  "safeSummary":"查询返回2行",
  "result":{"status":0,"rowCount":2,"data":[{"customer_id":"C001"},{"customer_id":"C002"}]},
  "diagnostic":null,
  "interaction":null,
  "selectedEdgeId":null,
  "skippedNodeIds":[]
}
```

`status` 取值：

- `SUCCEEDED`
- `FAILED`
- `WAITING_INPUT`
- `PAUSED`
- `TERMINATED`
- `UNKNOWN`

| status | tec01 节点状态 | tec01 运行处理 |
|---|---|---|
| SUCCEEDED | SUCCEEDED | 继续后续节点；条件节点记录 `selectedEdgeId` 和 `skippedNodeIds` |
| FAILED | FAILED | 运行 FAILED，等待人工重试 |
| WAITING_INPUT | WAITING | 运行 WAITING_INPUT，创建人工交互并释放资源 |
| PAUSED | PAUSED | 运行 PAUSED，释放资源 |
| TERMINATED | TERMINATED | 运行 TERMINATED，释放资源 |
| UNKNOWN | UNKNOWN | 节点保留 UNKNOWN，本次执行按失败结束；等待人工核实后重试 |

`runStatus` 是执行器在节点结束时观察到的运行状态，只作为参考：

- 为空或无法识别时忽略，不阻断回调。
- 只对 `PAUSED`、`PAUSE_REQUESTED`、`TERMINATE_REQUESTED`、`TERMINATED` 等控制态生效；节点结果和 `RUN_RELEASED` 仍是主要依据。
- `WAITING_INPUT` 必须由节点状态和 `interaction` 推导，不能只靠 `runStatus`，否则会产生没有交互记录的等待运行。

tec01 在同一事务内完成：

1. 将 `result`、`diagnostic` 存入结果大对象，只保留引用。
2. 将 attempt 置为终态；没有 `STARTED` 记录时补一条，避免回调丢失导致明细缺失。
3. 更新节点状态、输出引用、条件分支 `selectedRoutes` 和 `SKIPPED` 节点。
4. `WAITING_INPUT` 时创建 `interruptId` 并保存交互。
5. 追加节点事件、续租或清租约。

事务提交后才向页面/Hermes 推送；执行器收到 200 后才进入下一节点，收到 409 则立即停止。

### 4.3 RUN_RELEASED

```json
{
  "type":"RUN_RELEASED",
  "dispatchId":"dispatch_100173_1",
  "status":"WAITING_INPUT",
  "currentNodeId":"input-1",
  "reason":"等待用户填写后续查询参数"
}
```

`status` 可为：

- `SUCCEEDED`
- `FAILED`
- `WAITING_INPUT`
- `PAUSED`
- `TERMINATED`
- `UNKNOWN`

每次下发结束时（成功、失败、等待输入、暂停、终止或未知）都必须发送 `RUN_RELEASED`，由它释放活动上下文并给出最终状态。即使 `NODE_COMPLETED` 已经把运行置为终态，释放回调仍用于补写释放事件和清理活动归属。

tec01 处理：

1. 校验 `dispatchId` 是当前下发；旧调度返回 **409 `CALLBACK_DISPATCH_STALE`**，不落任何数据。
2. 先按4.4识别已保存的完全相同释放消息：直接确认200，不重复追加事件。如果运行已经是终态且与首次释放的 `status` 不一致，返回 **409 `CALLBACK_RUN_TERMINAL`**，不覆盖更权威的终态；一致时只补一条尚未记录的释放事件。
3. 保存运行状态、当前节点和 `reason`，清理活动执行归属与租约。
4. 追加运行事件，事务提交后再推送。

释放表示执行侧清除了该运行的活动上下文，不删除 tec01 的历史记录。`UNKNOWN` 是“等待人工核实”，不写 `finishedAt`；只有 `SUCCEEDED`、`FAILED`、`TERMINATED` 写入 `finishedAt`。终止由执行侧停止本地 CLI 后上报 `TERMINATED`；如果外部结果无法确认，上报 `UNKNOWN`。

继续、重试和 HITL 回复都不使用释放回调，而是由 tec01 生成新的 `dispatchId` 重新下发完整快照。

### 4.4 迟到与重复回调

在通过鉴权和字段校验后，按以下顺序处理；判断与写入应在同一事务内完成：

1. **先挡旧调度。** `dispatchId` 已不是当前调度时，返回 **409 `CALLBACK_DISPATCH_STALE`**，即使该消息曾保存过也不放行旧执行器。
2. **再确认已保存重复。** 当前调度内，同一消息身份及正文已成功保存，直接返回200确认原事实；即使运行已终态，也不返回终态冲突，不重复写结果、追加事件或回退状态。仅attempt存在不等于该类型消息已保存；相同消息身份却改变正文也不能按正常重复放行，应返回409且不覆盖原事实。
3. **再判断终态冲突。** 不属于上述重复、运行已终态的迟到节点回调返回 **409 `CALLBACK_RUN_TERMINAL`**，不修改数据，也不追加 `late:true` 事件。
4. **最后处理首次有效回调。** 正常保存并提交后返回200。首次 `RUN_RELEASED` 若与已知终态一致，可补写一次释放事件并清理活动归属；状态不一致则返回409。

例如：最后一个节点的 `NODE_COMPLETED` 已提交、运行已成功，但200响应在网络中丢失。执行器用原幂等键与正文重试时，tec01应按第2步返回200，让执行器继续正常释放；不能仅因运行已成功就返回409。

## 5. 人工表单 Interaction

当前人工交互只有 `HITL_FORM`。执行器在需要用户填写参数时，通过 `NODE_COMPLETED` 的 `WAITING_INPUT` 返回：

```json
{
  "kind":"HITL_FORM",
  "title":"填写后续查询参数",
  "description":"请参考上一步结果，输入需要继续查询的客户编号和返回条数。",
  "fields":[
    {"name":"customer_id","label":"客户编号","type":"string","required":true,"minLength":1,"maxLength":64},
    {"name":"limit","label":"返回条数","type":"integer","required":true,"minimum":1,"maximum":100}
  ],
  "context":{"queryRows":[{"customer_id":"C001"},{"customer_id":"C002"}]}
}
```

字段规则：

- `name`、`label`、`type`、`required` 必填。
- `type` 为 `string`/`integer`/`number`/`boolean`。
- 可选 `description`、`minimum`/`maximum`、`minLength`/`maxLength`、`enum`。
- `context` 是前置结果的可选上下文；tec01 按数据权限决定展示哪些内容。
- `WAITING_INPUT` 必须携带 `interaction`；没有交互的等待运行无法被用户处理。

处理流程：

1. tec01 收到 `WAITING_INPUT`，创建 `interruptId`，保存交互，运行置为 `WAITING_INPUT` 并释放执行资源。
2. tec01 页面与 Hermes 通过 MCP 读取同一份交互，展示字段说明和前置结果。
3. 用户在任一入口填写一个或多个字段并确认；Hermes 必须展示说明并请用户确认，不能猜测答案。
4. tec01 保存用户回复和 `resumePayload`，运行转为 `QUEUED`，生成新的 `dispatchId` 重新下发。
5. 新下发携带完整快照、`nodeStates`、`nodeOutputs`、`currentNodeId` 和：

```json
{
  "nodeId":"input-1",
  "action":"SUBMIT",
  "values":{"customer_id":"C001","limit":20}
}
```

6. 执行器校验字段类型与范围，输出 `{"values":{...}}`；下游通过 `/values/customer_id` 绑定参数。

页面与 Hermes 共享同一个 `interruptId` 和同一份运行事实，不产生两套状态。重复回复由 tec01 的交互状态和事务保证幂等。

## 6. 提取回调

路径：

```http
POST /internal/v1/extractions/{knowledgeId}/callbacks
```

`knowledgeId` 是经验标识，`requestId` 是本次提取尝试标识；两者共同定位一次提取任务。tec01 在提交前先创建 `EXTRACTING` 记录并保存当前 `requestId`；执行侧只上报，不写 tec01 的生产记录。

所有消息必须包含：

- `type`：`PROGRESS` 或 `FINISHED`
- `requestId`：原提交提取请求中的 requestId，所有阶段与终态必须原样携带
- `seq`：单调递增序号
- `occurredAt`：带时区偏移的 ISO-8601 时间

`seq` 由同一个上报器分配，阶段和终态共享同一序列。tec01按 `knowledgeId + requestId` 保存独立的**最大已接受seq**作为水位，不使用已接收事件条数，也不要求序号连续。当前请求的进度seq大于水位时保存并更新水位；重复或更旧进度忽略并返回200。终态另按本次requestId去重，不能仅因seq不大于进度水位就丢弃尚未保存的终态。

例如先收到seq=1，seq=2回报失败后收到seq=3，应接受3并将水位置为3；之后迟到的seq=2忽略，而不是把水位记为“已接收2条”。`seq`只保证当前临时任务内单调，新请求不要求从1开始；重启后显式开始新的提取尝试，应使用新requestId，不沿用旧尝试的水位拒收。

### 6.1 PROGRESS

```json
{
  "type":"PROGRESS",
  "requestId":"extract_100173_1",
  "seq":3,
  "stage":"ANALYZING_EVIDENCE",
  "message":"正在分析工单和操作记录",
  "progress":{"current":null,"total":null},
  "occurredAt":"2026-10-16T10:00:12+08:00"
}
```

| stage | 页面文案 | progress |
|---|---|---|
| FILTERING_OPERATIONS | 正在过滤有效 SQL | 操作记录当前数/总数 |
| PARAMETERIZING_SQL | 正在参数化 SQL | 已处理有效 SQL 数/有效 SQL 总数 |
| ANALYZING_EVIDENCE | 正在分析工单和操作记录 | 不可估计时 current/total 为 null |
| BUILDING_WORKFLOW | 正在生成 Workflow 草稿 | 通常为 null |
| VALIDATING_WORKFLOW | 正在校验 Workflow | 通常为 null |

`requestId`、`seq`、`stage`、`message` 必填；`progress` 可选，不可估计时 `current`/`total` 为 null。五个阶段各发一条 `PROGRESS`（不重复、不发心跳）。tec01 写入生命周期进度事件后推送；按本次请求最大seq忽略重复/更早进度，不报错。旧requestId或知识不在 `EXTRACTING` 状态时，进度回调忽略并返回当前知识确认，不覆盖状态。

### 6.2 FINISHED 成功

```json
{
  "type":"FINISHED",
  "requestId":"extract_100173_1",
  "seq":6,
  "status":"SUCCEEDED",
  "proposal":{
    "name":"客户信息查询",
    "summary":"根据工单条件查询客户基础信息与状态。",
    "matchPhrases":["客户信息查询"],
    "negativePhrases":[],
    "systemKeys":["crm"],
    "workflowDefinition":{}
  },
  "validation":{
    "catalogDigest":"<sha256>",
    "workflowContentHash":"<sha256>",
    "valid":true
  },
  "diagnostics":{
    "ticketNo":"INC-1",
    "auditOperationCount":4,
    "acceptedOperationCount":2,
    "ignoredOperationCount":2,
    "ignoredOperations":[
      {"operationId":"op-2","reason":"failed_result"},
      {"operationId":"op-4","reason":"duplicate_read_operation"}
    ],
    "llmMode":"structured"
  },
  "occurredAt":"2026-10-16T10:00:30+08:00"
}
```

必填：

- `proposal.name`、`proposal.summary` 为中文。
- `proposal.workflowDefinition` 为完整 DRAFT 校验后的规范化 DAG，不能为空。
- `validation` 必须存在，包含 `catalogDigest`、`workflowContentHash`、`valid`。
- `diagnostics` 取 Compiler 自身账本，必须包含去重过滤，不能被外层重新计算覆盖。

可选：

- `proposal.matchPhrases`、`negativePhrases`、`systemKeys`。
- `diagnostics.ticketNo`、`auditOperationCount`、`acceptedOperationCount`、`ignoredOperationCount`、`ignoredOperations`、`llmMode`。

过滤原因包括：

- `unsupported_operation`
- `missing_result`
- `invalid_result`
- `failed_result`
- `missing_sql`
- `missing_database_ref`
- `invalid_read_sql`
- `duplicate_read_operation`

tec01 在同一事务中保存 `proposal`、`validation`、`diagnostics`，将经验转为 `DRAFT` 并写 `EXTRACTION_SUCCEEDED` 事件，再推送成功。

### 6.3 FINISHED 失败

0.13.2起error保留code/message/retryable，新增可选errorId及details（stage、reasonCode、field、类型及安全代码位置）。message现在说明具体原因，例如parameters应为数组但实际null；tec01应原样保存并向页面/Hermes展示，严格DTO需允许新增字段。完整示例和原因码见[失败诊断与日志检索](compiler-failure-diagnostics.md)。回报确认及重试不变。

```json
{
  "type":"FINISHED",
  "requestId":"extract_100173_1",
  "seq":4,
  "status":"FAILED",
  "error":{
    "code":"LLM_ANALYSIS_FAILED",
    "message":"模型未返回有效的结构化结果",
    "retryable":true
  },
  "occurredAt":"2026-10-16T10:00:30+08:00"
}
```

`error` 必填。稳定错误码：

| code | 含义 | retryable |
|---|---|---|
| NO_VALID_OPERATIONS | 没有成功且安全的 SQL 读操作 | false |
| LLM_ANALYSIS_FAILED | 模型不可用或未返回有效结构化结果 | true |
| COMPILER_FAILED | 其他编译或校验失败 | false |

错误消息不得包含 Token、模型网关响应正文或历史实际 SQL 参数值。tec01 在同一事务记录失败状态与 `EXTRACTION_FAILED` 事件，再向用户展示。

### 6.4 失败重试与终态去重

- 失败后仍使用同一 `knowledgeId`；显式重试时使用新的 `requestId`。
- tec01 在接受新 `requestId` 时把记录重新设为 `EXTRACTING`，并重置该次提取的终态去重窗口。
- 原requestId必须出现在每条PROGRESS和FINISHED正文及相应幂等键中。新请求建立独立的最大seq水位，不能沿用上次请求的进度计数或终态标志。
- 旧requestId的迟到进度与终态不覆盖当前尝试，即使seq更大也应忽略；返回HTTP200和对应knowledgeId的AopsResult，不用旧尝试的失败结果覆盖新草稿。
- `FINISHED` 的“只接受第一次”以一次提取为范围，不能永久屏蔽该知识后续重试。
- 同一 `requestId` 与相同证据重发只返回 `duplicate`，不重复调用 LLM；同一 `requestId` 不同证据返回 409。
- 执行侧仍保留未确认终态时，重复提交可重发该终态，`requestId`、`seq`、`occurredAt`、正文和 `X-Request-Id` 均不变，不重新调用LLM。
- `seq` 只保证当前临时任务内单调；服务重启后如 tec01 显式重启提取，不应拿旧任务 `seq` 作为永久拒收条件。

tec01 知识状态：

| 状态 | 含义 |
|---|---|
| EXTRACTING | 提取中，只接受进度和本次终态 |
| DRAFT | 提取成功，草稿待审核/发布 |
| EXTRACTION_FAILED | 提取失败，可显式重试 |

如果知识已是 `DRAFT`、`EXTRACTION_FAILED` 或 `PUBLISHED`，重复的 `FINISHED` 返回 200 并忽略；其他不允许接收的状态返回 409。

上述知识状态判断针对当前requestId；旧requestId始终按迟到消息忽略，不参与当前请求的状态转换。FINISHED按每次请求最多保存一个终态，首次成功示例seq=6、分析失败示例seq=4仅为示例，接收端不得硬编码终态序号。

## 7. tec01 的处理与推送

- 所有回调先落库：状态、结果、诊断、交互、输出引用和事件在同一事务内提交。
- 事务提交后才向页面 SSE 与 Hermes 推送；页面和 Hermes 读到的是同一份已提交事件。
- 运行状态、节点状态和结果只以 tec01 为准；执行侧内存和短期事件缓存不构成生产事实。
- 节点结果和诊断按大对象保存，回调响应只回 `lastEventId` 或知识状态，不回传大结果。
- 终止在 tec01 内部统一为 `TERMINATED`；**不再兼容旧值 `CANCELLED`**，回调收到 `CANCELLED` 会按 `422 CALLBACK_STATUS_INVALID` 拒绝。
- 旧调度与终态冲突的回调返回 `409`，执行器必须立即停止，不能继续执行后续节点。
- 回调失败不会把执行侧结果自动当作未执行；执行侧在无法确认外部结果时应上报 `UNKNOWN`，tec01 保存为未知事实并等待用户核实，不会自动重放。

## 8. 联调检查清单

- 所有运行回调都带 `dispatchId`；节点回调还带 `attemptId` 和 `nodeId`。
- 当前调度已保存的完全相同消息重试优先返回200，不因运行已终态而拒绝；旧调度仍优先409，同身份不同正文不得覆盖原事实。
- `NODE_COMPLETED` 的 `status` 在允许枚举内；终止只能上报 `TERMINATED`（旧值 `CANCELLED` 已不接受，会返回 422）。
- 生产模式下等待 tec01 返回 200 后再执行下一节点；收到 409（`CALLBACK_DISPATCH_STALE` / `CALLBACK_RUN_TERMINAL`）必须立即停止，不要重试，也不要再发释放回调；不要让节点并行越过未确认的完成回调。
- 5xx/超时按 1/2/4/8/16 秒重试，始终复用同一 `X-Request-Id`；4xx 不重试。
- 提取 `seq` 在同一临时任务内单调递增；服务重启后如 tec01 显式重启提取，不能把旧任务的 `seq` 当作永久拒收条件。
- PROGRESS和FINISHED均携带原requestId，幂等键包含requestId；不同提取请求相互隔离，进度水位使用最大已接受seq而非事件条数，允许缺号。
- `FINISHED` 成功必须带非空 `workflowDefinition` 与 `validation`；失败必须带 `error`。
- HITL 只提交 `resumePayload` 中的 `values`，字段类型与约束由执行器校验；Hermes 不得猜测答案。
- 回调中不得出现 API Key、Token、模型响应正文或未脱敏 SQL 参数。

## 相关文档

- [itsm-workflow 提供给 tec01 的接口](itsm-workflow-to-tec01-api.md)
- [集成约定](tec01-integration-contract.md)
- [总体拆分设计](tec01-control-plane-split.md)
- [独立 Studio 手册](standalone-studio-guide.md)
## 提取完整性扩展（0.13.3）

提取结果新增`extractionOutcome`和`publishable`，诊断新增异常操作和依赖阻断项；部分成功继续使用原SUCCEEDED状态，不新增任务状态或回报。下游须保存并展示诊断，发布时结合人工处理和当前Workflow校验。详细字段与上线顺序见[提取完整性补充说明](extraction-partial-success-addendum.md)。执行下发及确认协议不变。
