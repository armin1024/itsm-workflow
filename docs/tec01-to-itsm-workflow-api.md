# tec01提供给itsm-workflow的回调API

tec01提供两个统一回调入口，先保存数据再向页面SSE和Hermes消息渠道推送。默认地址 `http://tec01:3421`。

## 认证、重试与响应

```http
Authorization: Bearer <TEC01_SERVICE_TOKEN>
Content-Type: application/json
X-Request-Id: <稳定的回调幂等键>
```

成功返回HTTP200与AopsResult：

```json
{"status":200,"msg":"success","data":{"runId":"run_1","status":"RUNNING","lastEventId":1}}
```

业务失败用HTTP状态表达，字段错误422、身份错误401、资源不存在404、处理失败5xx。错误结构为 `{code,message,retryable}`。不得在HTTP200中使用body.status表示失败。

执行侧对5xx和连接异常/超时进行最多5次额外重试，退避1/2/4/8/16秒；重试始终使用相同X-Request-Id。4xx不自动重试。重复回调必须返回200且不覆盖已经提交的结果。

| 回调 | X-Request-Id | tec01处理 |
|---|---|---|
| NODE_STARTED | `{attemptId}:started` | 相同attempt已存在则忽略 |
| NODE_COMPLETED | `{attemptId}:completed` | attempt已终态则忽略 |
| RUN_RELEASED | `{dispatchId}:released` | 必须对应当前dispatchId，旧调度回调忽略 |
| PROGRESS | `{knowledgeId}:{seq}` | 同次提取中重复/更旧seq忽略 |
| FINISHED | `{knowledgeId}:finished` | 同次提取仅接受第一个终态 |

节点开始与完成共用attemptId，但幂等键后缀不同。

## 接口索引

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/internal/v1/runs/{runId}/callbacks` | 节点开始、完成、运行释放 |
| POST | `/internal/v1/extractions/{knowledgeId}/callbacks` | 提取进度、成功、失败 |

## 运行回调

所有消息都有 `type/dispatchId`；节点回调还有 `attemptId/nodeId`。结果直接置于result，SQL输出上限20MiB，tec01负责持久化。

### NODE_STARTED

```json
{
  "type":"NODE_STARTED",
  "dispatchId":"dispatch_1",
  "attemptId":"attempt_1",
  "nodeId":"sql-1",
  "startedAt":"2026-10-16T10:00:00+08:00"
}
```

tec01在事务中插入STARTED attempt、设置当前节点及节点RUNNING，并追加事件。首个节点开始时运行转RUNNING。

### NODE_COMPLETED

```json
{
  "type":"NODE_COMPLETED",
  "dispatchId":"dispatch_1",
  "attemptId":"attempt_1",
  "nodeId":"sql-1",
  "status":"SUCCEEDED",
  "safeSummary":"查询返回2行",
  "result":{"status":0,"rowCount":2,"data":[{"customer_id":"C001"},{"customer_id":"C002"}]},
  "diagnostic":null,
  "interaction":null,
  "selectedEdgeId":null,
  "skippedNodeIds":[]
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| status | 是 | SUCCEEDED/FAILED/WAITING_INPUT/PAUSED/CANCELLED/UNKNOWN |
| safeSummary | 否 | 不包含凭据的可读摘要 |
| result | 否 | 完整节点结构化输出 |
| diagnostic | 否 | 脱敏错误码、消息、退出码、stdout/stderr诊断 |
| interaction | WAITING_INPUT时 | 人工表单，见下文 |
| selectedEdgeId | 否 | 条件节点选中的边 |
| skippedNodeIds | 否 | 未命中路径的节点 |

tec01在同一个事务内保存结果/诊断、完成attempt、更新节点与输出映射、保存interaction和追加对应事件。成功后执行器才进入下一节点。

| 节点status | 运行处理 |
|---|---|
| SUCCEEDED | 继续后续节点 |
| FAILED | 运行FAILED，等待人工重试 |
| WAITING_INPUT | 保存人工交互，运行WAITING_INPUT |
| PAUSED | 运行PAUSED |
| CANCELLED | 运行CANCELLED |
| UNKNOWN | 保留未知事实，等待用户核实 |

### RUN_RELEASED

```json
{
  "type":"RUN_RELEASED",
  "dispatchId":"dispatch_1",
  "status":"WAITING_INPUT",
  "currentNodeId":"input-1",
  "reason":"等待用户填写后续查询参数"
}
```

status可为SUCCEEDED/FAILED/WAITING_INPUT/PAUSED/CANCELLED/UNKNOWN。tec01首先比较当前dispatchId，避免旧执行回调覆盖新状态，再保存运行状态、清理活动执行归属并追加事件。先提交事务，再向页面/Hermes推送。

释放表示执行侧清除了该运行活动上下文，不删除tec01历史记录。继续时tec01生成新dispatchId并传回完整快照、已完成状态和输出。

## 人工表单Interaction

当前人工交互只有HITL_FORM。用户可以参考前置SQL结果后填写值，或在指定节点补充一个或多个参数。

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

fields每项包含name、label、type、required；支持description、minimum/maximum、minLength/maxLength、enum。type为string/integer/number/boolean。context是可选上下文，tec01按数据权限展示需要的结果。

Hermes通过tec01 MCP取得相同交互，展示说明并收集用户输入。字段完整后汇总确认，不能自动猜测。用户也可直接在tec01页面填写。

tec01保存用户回复后，重新调用dispatch，携带：

```json
{
  "currentNodeId":"input-1",
  "resumePayload":{
    "nodeId":"input-1",
    "action":"SUBMIT",
    "values":{"customer_id":"C001","limit":20}
  }
}
```

其余快照、工单ID、节点状态、输出和新dispatchId也必须传入。Executor校验并输出 `{"values":{...}}`，下游通过 `/values/customer_id` 取值。

## 提取回调

路径 `POST /internal/v1/extractions/{knowledgeId}/callbacks`。knowledgeId同时是经验ID和提取任务ID。tec01在提交提取前先创建EXTRACTING记录。

所有消息必须含 `type/seq/occurredAt`；seq由同一个上报器分配，阶段、心跳和终态共享单调序列。

### PROGRESS

```json
{
  "type":"PROGRESS",
  "seq":4,
  "stage":"ANALYZING_EVIDENCE",
  "message":"正在分析工单和操作记录",
  "progress":{"current":null,"total":null},
  "occurredAt":"2026-10-16T10:00:12+08:00"
}
```

| stage | 页面文案 | progress |
|---|---|---|
| FILTERING_OPERATIONS | 正在过滤有效SQL | 操作记录当前数/总数 |
| PARAMETERIZING_SQL | 正在参数化SQL | 已处理有效SQL数/有效SQL总数 |
| ANALYZING_EVIDENCE | 正在分析工单和操作记录 | 不可估计时current/total为null |
| BUILDING_WORKFLOW | 正在生成Workflow草稿 | 通常为null |
| VALIDATING_WORKFLOW | 正在校验Workflow | 通常为null |

进入ANALYZING_EVIDENCE立即发一条PROGRESS，随后每15秒心跳。tec01写生命周期进度事件后推送；重复/更早序号丢弃，不必报错。

### FINISHED成功

```json
{
  "type":"FINISHED",
  "seq":9,
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

workflowDefinition实际为完整DRAFT校验后的规范化DAG，包含节点、条件与绑定。名称和摘要为中文。diagnostics取Compiler自身账本，必须包含去重过滤，不得被外层重新计算覆盖。

过滤原因包括unsupported_operation、missing_result、invalid_result、failed_result、missing_sql、missing_database_ref、invalid_read_sql及duplicate_read_operation。

tec01在一个事务中保存proposal、validation、diagnostics，将经验转DRAFT并写CREATED事件，再推送成功。

### FINISHED失败

```json
{
  "type":"FINISHED",
  "seq":9,
  "status":"FAILED",
  "error":{"code":"LLM_ANALYSIS_FAILED","message":"模型未返回有效的结构化结果","retryable":true},
  "occurredAt":"2026-10-16T10:00:30+08:00"
}
```

无成功只读操作时返回NO_VALID_OPERATIONS；模型不可用/结构不正确时返回LLM_ANALYSIS_FAILED；其他编译或校验失败为COMPILER_FAILED。错误消息不包含Token、模型网关响应正文或历史实际SQL参数值。

tec01在同一事务记录失败状态与EXTRACTION_FAILED事件，再向用户展示。

### 失败后重试与终态去重

失败后仍使用同一knowledgeId。显式重试时使用新的requestId，并在tec01把记录重新设为EXTRACTING、重置该次提取的终态幂等窗口。FINISHED“只接受第一次”以一次提取为范围，不能永久屏蔽该知识后续重试。

同requestId与同证据重发只返回duplicate，不重复调用LLM；同requestId不同证据返回409。若执行侧仍保留未确认的终态，重复提交可重发该终态，seq和X-Request-Id不变。

seq只保证当前临时任务内单调；服务重启后如tec01显式重启提取，不应拿旧任务seq作为永久拒收条件。

## 实现对齐状态与联调

执行侧已统一两类回调入口，运行nodeId位于body，稳定幂等键和5xx/超时重试共用同一客户端。草稿提取使用knowledgeId，补齐五阶段、分析心跳、校验摘要和完整过滤账本。

生产联调仍需tec01实现上述事务、去重窗口、权限、数据持久化以及SSE/Hermes推送。Studio默认仅把数据存浏览器；勾选同步后使用相同回调，tec01需提前创建对应记录。

## 相关文档

- [itsm-workflow对外API](itsm-workflow-to-tec01-api.md)
- [集成约定](tec01-integration-contract.md)
- [总体拆分设计](tec01-control-plane-split.md)
- [独立Studio手册](standalone-studio-guide.md)
