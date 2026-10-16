# itsm-workflow提供给tec01的接口文档

本文描述执行侧对外提供的现行API。默认地址 `http://itsm-workflow:8089`，接口使用UTF-8 JSON。配套回调见[tec01提供给itsm-workflow](tec01-to-itsm-workflow-api.md)。

## 认证与错误

所有内部接口必须传：

```http
Authorization: Bearer <RUNTIME_SERVICE_TOKEN>
```

真实SQL执行额外传 `X-AOPS-Api-Key: <当前用户AOPS_API_KEY>`。该Key只用于当前CLI子进程，不进入Workflow、回调、日志或浏览器持久化数据。

统一错误结构：

```json
{"code":"WORKFLOW_VALIDATION_FAILED","message":"节点配置校验失败","retryable":false,"details":{}}
```

401为服务身份失败；409为内容或活动状态冲突；422为请求/Workflow校验失败；429为并发容量不足；503为未配置必要回调或依赖不可用。

## 接口索引

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/internal/v1/runtime/catalog` | Node Catalog |
| GET | `/internal/v1/runtime/schemas` | 全部JSON Schema与节点约束 |
| GET | `/internal/v1/runtime/schemas/{schemaName}` | 单份JSON Schema |
| POST | `/internal/v1/runtime/workflows/validate` | 校验并规范化Workflow |
| POST | `/internal/v1/runtime/workflows/plan` | 预览计划，供联调使用 |
| POST | `/internal/v1/runtime/nodes/execute` | 同步真实执行单个节点 |
| POST | `/internal/v1/extractions/{knowledgeId}` | 异步提交草稿提取 |
| POST | `/internal/v1/executions/dispatch` | 下发完整Workflow |
| POST | `/internal/v1/executions/{runId}/commands` | 暂停或取消 |

## 获取Node Catalog

`GET /internal/v1/runtime/catalog`支持 `If-None-Match`，匹配Catalog摘要返回304。200响应：

```json
{
  "catalogDigest":"<sha256>",
  "runtimeVersion":"0.10.0",
  "generatedAt":"2026-10-16T10:00:00+08:00",
  "testOnly":false,
  "nodes":[
    {
      "type":"sql_read",
      "name":"SQL只读查询",
      "schemaVersion":1,
      "handlerVersion":"1.0.0",
      "description":"使用aops-cli执行单条安全只读SQL",
      "configSchema":{},
      "inputSchema":{},
      "outputSchema":{},
      "uiSchema":{},
      "riskLevel":"LOW"
    }
  ]
}
```

节点类型为 `sql_read/condition/hitl_form/end`。示例Schema空对象仅为简写，实际返回完整约束。Node类型与Handler由执行侧代码发布管理。

## 获取JSON Schema

`GET /internal/v1/runtime/schemas`返回：

```json
{
  "runtimeVersion":"0.10.0",
  "jsonSchemaDialect":"https://json-schema.org/draft/2020-12/schema",
  "schemas":{
    "workflowDefinition":{},
    "dispatch":{},
    "extraction":{},
    "singleNode":{}
  },
  "nodes":[
    {"type":"sql_read","schemaVersion":1,"configSchema":{},"inputSchema":{},"outputSchema":{}}
  ]
}
```

`GET /internal/v1/runtime/schemas/{schemaName}`读取单份Schema，schemaName可用 `workflowDefinition/dispatch/extraction/singleNode`；另提供`singleNodeResponse`用于校验单节点响应。未知名称返回404。

tec01可用这些约束生成配置表单、检查必填项与类型。只读SQL、DAG、上游依赖与条件默认边通过后续validate接口检查，不仅依赖前端Schema。

## 校验并规范化Workflow

```http
POST /internal/v1/runtime/workflows/validate
```

```json
{
  "workflowDefinition":{
    "schemaVersion":2,
    "entryNodeId":"sql-1",
    "nodes":[
      {
        "id":"sql-1",
        "type":"sql_read",
        "schemaVersion":1,
        "title":"查询客户",
        "config":{"databaseRef":"124/aops-t/aops-t/readonly/aops-t","sqlTemplate":"SELECT id FROM users WHERE name={{customer_name}}"},
        "inputs":[{"name":"customer_name","type":"string","source":{"kind":"RUN_INPUT","key":"customer_name"},"required":true}],
        "approvalPolicy":"PLAN",
        "timeoutSeconds":600
      },
      {"id":"done","type":"end","title":"完成","config":{},"inputs":[]}
    ],
    "edges":[{"id":"next","source":"sql-1","target":"done","kind":"NORMAL"}]
  },
  "validationMode":"PUBLISH"
}
```

可选 `targetCatalogDigest` 指定预期目录，变化返回409。validationMode为 `DRAFT/PUBLISH/EXECUTE/TEST`，默认DRAFT。成功200：

```json
{
  "valid":true,
  "normalizedDefinition":{},
  "catalogDigest":"<sha256>",
  "workflowContentHash":"<sha256>",
  "requiredRuntimeVersion":">=0.10.0,<1.0.0",
  "warnings":[]
}
```

tec01发布前必须调用，保存规范化Workflow、Catalog摘要与内容哈希。创建计划时从该不可变版本生成快照，检查权限、工单ID及当前输入，保存planHash并请用户确认，无需每次重新校验整张DAG。

## 同步单节点执行

```http
POST /internal/v1/runtime/nodes/execute
X-AOPS-Api-Key: <SQL读时必填>
```

```json
{
  "requestId":"single_sql_001",
  "ticketId":100173,
  "node":{
    "id":"sql-1",
    "type":"sql_read",
    "title":"查询客户",
    "config":{"databaseRef":"124/aops-t/aops-t/readonly/aops-t","sqlTemplate":"SELECT id FROM users WHERE name={{customer_name}}"},
    "inputs":[{"name":"customer_name","type":"string","source":{"kind":"RUN_INPUT","key":"customer_name"}}]
  },
  "inputs":{"customer_name":"王五"},
  "resumePayload":null
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| requestId | string | 是 | 当前进程内幂等标识 |
| ticketId | 正整数 | SQL读时是 | comment使用当前工单ID |
| node | object | 是 | 完整单节点定义 |
| inputs | object | 否 | 已解析的真实输入，默认空对象 |
| resumePayload | object | 否 | 人工表单回复 |

结果同步200：

```json
{
  "requestId":"single_sql_001",
  "nodeId":"sql-1",
  "status":"SUCCEEDED",
  "output":{"status":0,"rowCount":1,"data":[{"id":"C001"}]},
  "diagnostic":null,
  "interaction":null,
  "handlerVersion":"1.0.0"
}
```

不自动执行前置节点，调用方应按绑定规则取好inputs。相同requestId同内容返回原结果，同ID不同内容返回409；重试或HITL回复使用新requestId。幂等记录只在当前进程内暂存，不跨服务重启。节点执行失败返回FAILED与脱敏diagnostic，调用方自行保存结果；该接口不创建完整运行回调。

表单首次执行返回WAITING_INPUT；新requestId携带：

```json
{"resumePayload":{"nodeId":"input-1","action":"SUBMIT","values":{"customer_id":"C001","limit":20}}}
```

## 异步草稿提取

```http
POST /internal/v1/extractions/knw_100173
```

```json
{
  "requestId":"extract_100173_1",
  "ticketId":100173,
  "ticketInfo":{"id":100173,"incident_id":"INC-1","event_title":"客户查询"},
  "auditTimeline":[
    {
      "id":19,
      "operation":"sql_exec_read",
      "result":"{\"r\":true,\"e\":\"\"}",
      "details":"{\"sql\":{\"serverid\":124,\"dbid\":\"aops-t\",\"dbname\":\"aops-t\",\"dbuser\":\"readonly\",\"service_name\":\"aops-t\",\"command\":\"SELECT id FROM users\"}}"
    }
  ]
}
```

tec01先生成knowledgeId并建EXTRACTING记录，再提交。knowledgeId就是任务ID，不再有独立jobId。ticketInfo必须为info.data对象，auditTimeline必须为timeline.data对象数组；不接受完整响应信封。ticketId正整数且必须与ticketInfo.id一致。

响应202：

```json
{"knowledgeId":"knw_100173","accepted":true,"duplicate":false}
```

同knowledgeId、requestId、证据内容重复提交不重复提取；内容冲突返回409。失败后显式重试使用同knowledgeId和新requestId。tec01应把该记录重新设为EXTRACTING，重置本次提取的终态去重窗口，避免前一次FAILED屏蔽本次结果。

后台阶段为过滤、参数化、分析、生成、校验；分析阶段每15秒心跳。成功回调含中文草稿、校验摘要与完整过滤账本。所有回调到 `/internal/v1/extractions/{knowledgeId}/callbacks`。

## 下发完整Workflow

```http
POST /internal/v1/executions/dispatch
X-AOPS-Api-Key: <含SQL读时必填>
```

```json
{
  "dispatchId":"dispatch_100173_1",
  "runId":"run_100173",
  "ticketId":100173,
  "workflowContentHash":"<validate返回的哈希>",
  "workflowSnapshot":{},
  "runInputs":{},
  "nodeStates":{},
  "nodeOutputs":{},
  "currentNodeId":null,
  "selectedRoutes":{},
  "resumePayload":null
}
```

workflowSnapshot必须填完整规范化Workflow。nodeStates保存 `PENDING/RUNNING/WAITING/SUCCEEDED/SKIPPED/FAILED/UNKNOWN` 等当前事实；nodeOutputs为已完成节点的真实输出，selectedRoutes为已选条件路径。可附nodeResults保留调试/交互结果。

每次首次、继续或重试生成新的dispatchId。相同dispatchId与相同内容重发返回原受理结果；相同ID不同内容、同runId已活动返回409。并发已满返回429并给Retry-After。

响应200仅表示受理：

```json
{"dispatchId":"dispatch_100173_1","runId":"run_100173","accepted":true,"executorId":"executor-01","acceptedAt":"2026-10-16T10:00:00+08:00"}
```

真实状态按逐节点回调判断。已成功/跳过节点不重放；tec01提供半途运行的状态与结果，执行器继续当前未完成节点。未知外部执行结果必须先人工核实。

| 恢复场景 | currentNodeId | resumePayload |
|---|---|---|
| 暂停后继续 | 当前节点 | `{"action":"continue"}` |
| 人工表单回复 | 表单节点 | `{"nodeId":"input-1","action":"SUBMIT","values":{...}}` |
| 失败后明确重试 | 失败节点 | null或保留的表单恢复载荷 |

## 暂停与取消

```http
POST /internal/v1/executions/run_100173/commands

{"commandId":"cmd_1","type":"PAUSE"}
```

type只能PAUSE或CANCEL，继续走新的dispatch。200响应：

```json
{"commandId":"cmd_1","runId":"run_100173","accepted":true,"currentNodeId":"sql-1"}
```

accepted=false时HTTP仍为200，并给reason。受理不代表已经暂停；当前SQL安全结束后回调PAUSED。取消会终止本地进程，如外部结果不能确认，返回UNKNOWN。每次释放都通过RUN_RELEASED回调，tec01保存最终状态。

## Workflow关键结构

- 定义：schemaVersion为1或2，entryNodeId、nodes、edges；可有catalogDigest。
- 节点：id、type、title、config、inputs、schemaVersion、handlerVersion、approvalPolicy、timeoutSeconds、uiPosition。
- 输入：name、type、description、required、source。type为string/number/integer/boolean/object/array。
- source：RUN_INPUT使用key，NODE_OUTPUT使用nodeId与jsonPointer，LITERAL使用value。
- 边：id、source、target、kind（NORMAL/CONDITION）、label、condition、default。
- 条件支持eq/ne/gt/gte/lt/lte/in/contains/exists/empty及all/any/not，条件节点必须有默认边。
- 人工表单输出固定为 `{"values":{...}}`，下游绑定 `/values/<字段名>`。

获取上述结构的精确JSON约束请调用schemas接口。
