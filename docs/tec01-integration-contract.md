# tec01与itsm-workflow接口约定

tec01主动提交工单数据、下发Workflow和控制命令。itsm-workflow通过统一回调返回提取进度、节点开始/完成和运行释放。详细字段以两份方向API文档为准：

- [itsm-workflow提供给tec01](itsm-workflow-to-tec01-api.md)
- [tec01提供给itsm-workflow](tec01-to-itsm-workflow-api.md)

## 认证

tec01请求本服务使用 `Authorization: Bearer <RUNTIME_SERVICE_TOKEN>`。本服务回调tec01使用 `Authorization: Bearer <TEC01_SERVICE_TOKEN>` 及稳定 `X-Request-Id`。真实SQL执行额外使用当前用户的 `X-AOPS-Api-Key`，执行器只在当前执行内存及CLI环境使用。

## 节点目录与JSON约束

```http
GET /internal/v1/runtime/catalog
GET /internal/v1/runtime/schemas
GET /internal/v1/runtime/schemas/{schemaName}
```

schemas返回 `runtimeVersion/jsonSchemaDialect/schemas/nodes`。`schemas`含 `workflowDefinition/dispatch/extraction/singleNode`，另有`singleNodeResponse`响应约束；`nodes`含各节点 `configSchema/inputSchema/outputSchema`。单Schema请求使用相应名称。

当前节点为SQL读、条件判断、人工表单和结束。tec01可按Schema生成配置输入框；DAG、依赖和只读SQL仍调用发布校验。

## 发布与计划创建

```http
POST /internal/v1/runtime/workflows/validate

{"workflowDefinition":{},"validationMode":"PUBLISH"}
```

正式发布前必须调用，返回 `normalizedDefinition/catalogDigest/workflowContentHash`。tec01固化不可变版本，创建计划时检查权限、当前工单ID与运行输入，并保存展示内容的 `planHash`。

用户在页面或Hermes确认相同planHash后执行。参数或计划修改需要重新展示确认。`/internal/v1/runtime/workflows/plan`可用于联调预览，正式计划不需要每次远程校验整张图。

未产生的SQL结果和人工参数不要求创建计划时输入；卡片应说明这些值将在什么步骤获得。

## 草稿提取

```http
POST /internal/v1/extractions/knw_100173
Content-Type: application/json

{
  "requestId":"extract_100173_1",
  "ticketId":100173,
  "ticketInfo":{"id":100173,"incident_id":"INC-1","event_title":"客户查询"},
  "auditTimeline":[]
}
```

tec01先创建EXTRACTING记录，再传真实的info.data对象和audit_timeline.data数组。示例空数组可用于验证无有效操作的失败路径。响应202仅代表受理：

```json
{"knowledgeId":"knw_100173","accepted":true,"duplicate":false}
```

同知识ID、requestId与证据内容重复提交不重复提取；内容冲突返回409。失败后仍用相同knowledgeId，显式重试使用新的requestId。

回调：

```http
POST /internal/v1/extractions/{knowledgeId}/callbacks
```

阶段 `FILTERING_OPERATIONS → PARAMETERIZING_SQL → ANALYZING_EVIDENCE → BUILDING_WORKFLOW → VALIDATING_WORKFLOW`。分析阶段进入时立即上报，之后每15秒心跳。

```json
{
  "type":"PROGRESS",
  "seq":4,
  "stage":"ANALYZING_EVIDENCE",
  "message":"正在分析工单和操作记录",
  "progress":{"current":null,"total":null},
  "occurredAt":"2026-10-16T10:00:00+08:00"
}
```

终态为 `FINISHED/SUCCEEDED`，带proposal、validation、diagnostics；失败为 `FINISHED/FAILED`，带稳定error。成功草稿的名称和摘要使用中文，完整筛选账本包含重复SQL。

**失败重试约定：** tec01在接受新的requestId进行显式重试时，将同一knowledgeId重新置为EXTRACTING，并重置该次提取的终态去重窗口。FINISHED“只接受第一次”仅适用于同一次提取，避免上一次FAILED挡住下一次SUCCEEDED。暂存记录仍存在时，执行侧seq继续递增；tec01不得假定跨进程重启后seq仍连续。

## 完整Workflow下发

```http
POST /internal/v1/executions/dispatch
X-AOPS-Api-Key: <AOPS_API_KEY>
```

```json
{
  "dispatchId":"dispatch_2",
  "runId":"run_100173",
  "ticketId":100173,
  "workflowContentHash":"<校验接口返回的哈希>",
  "workflowSnapshot":{},
  "runInputs":{},
  "nodeStates":{"sql-1":"SUCCEEDED","input-1":"WAITING","sql-2":"PENDING"},
  "nodeOutputs":{"sql-1":{"status":0,"rowCount":2,"data":[]}},
  "currentNodeId":"input-1",
  "selectedRoutes":{},
  "resumePayload":{"nodeId":"input-1","action":"SUBMIT","values":{"customer_id":"C001","limit":20}}
}
```

实际workflowSnapshot填完整规范化定义。响应200表示受理，后台逐节点执行。首次、暂停继续、HITL回复和失败重试都使用新dispatchId。相同ID相同内容返回原受理结果，冲突返回409；已活动runId不允许并发下发；容量满返回429。

tec01应传当前节点及已提交结果。执行器跳过SUCCEEDED/SKIPPED节点，失败节点仅在明确重试时继续，RUNNING/UNKNOWN等不确定状态不能直接重放。暂停继续可传 `{"action":"continue"}`；HITL使用SUBMIT表单值。

## 节点与运行回调

所有运行回调统一到：

```http
POST /internal/v1/runs/{runId}/callbacks
```

```json
{"type":"NODE_STARTED","dispatchId":"dispatch_2","attemptId":"attempt_1","nodeId":"sql-2","startedAt":"2026-10-16T10:01:00+08:00"}
```

```json
{
  "type":"NODE_COMPLETED",
  "dispatchId":"dispatch_2",
  "attemptId":"attempt_1",
  "nodeId":"sql-2",
  "status":"SUCCEEDED",
  "safeSummary":"查询返回2行",
  "result":{"status":0,"rowCount":2,"data":[]},
  "diagnostic":null,
  "interaction":null,
  "selectedEdgeId":null,
  "skippedNodeIds":[]
}
```

```json
{"type":"RUN_RELEASED","dispatchId":"dispatch_2","status":"SUCCEEDED","currentNodeId":"done","reason":"执行完成"}
```

结果直接置于result，沿用SQL读20MiB上限；tec01负责保存结果与诊断。tec01必须先提交节点状态和事件事务，再推SSE或Hermes消息。执行器收到成功回调确认后才执行下一节点；回调无法确认时停止并释放。

| 回调 | X-Request-Id |
|---|---|
| NODE_STARTED | `{attemptId}:started` |
| NODE_COMPLETED | `{attemptId}:completed` |
| RUN_RELEASED | `{dispatchId}:released` |
| PROGRESS | `{knowledgeId}:{seq}` |
| FINISHED | `{knowledgeId}:finished` |

成功HTTP200，响应使用 `{status:200,msg:"success",data:{...}}`。HTTP5xx/超时首次失败后重试5次，退避1/2/4/8/16秒，始终复用同一请求ID；4xx不重试。重复回调返回200且不覆盖已提交状态。

## 人工表单与Hermes

HITL等待回调的interaction：

```json
{
  "kind":"HITL_FORM",
  "title":"填写后续参数",
  "description":"请参考上一步SQL结果，确认客户编号。",
  "fields":[
    {"name":"customer_id","label":"客户编号","type":"string","required":true},
    {"name":"limit","label":"返回条数","type":"integer","required":true,"minimum":1,"maximum":100}
  ],
  "context":{}
}
```

tec01保存交互、显示前置结果与表单，通过消息渠道向Hermes提供同样信息。用户填写一个或多个值并确认后，tec01保存回复，携带 `resumePayload`重新下发。表单输出为 `{"values":{...}}`，下游使用 `/values/customer_id`。

Hermes的MCP工具由tec01维护，可包括匹配、计划、确认、状态、等待、人工回复、暂停、继续、取消和重试；全部调用tec01相同运行服务。消息与Web页面共享同一状态，用户不需要重复填写。

## 暂停、取消与释放

```http
POST /internal/v1/executions/{runId}/commands

{"commandId":"cmd_1","type":"PAUSE"}
```

取消使用CANCEL。接受命令HTTP200不代表已暂停；以RUN_RELEASED的最终状态为准。暂停在安全边界生效，取消终止CLI；外部结果不确定时标为UNKNOWN。WAITING_INPUT、PAUSED、FAILED、CANCELLED、UNKNOWN、SUCCEEDED都会释放执行资源。

## 单节点接入

```http
POST /internal/v1/runtime/nodes/execute

{"requestId":"single_1","ticketId":100173,"node":{},"inputs":{},"resumePayload":null}
```

传完整node及已解析inputs，返回 `requestId/nodeId/status/output/diagnostic/interaction/handlerVersion`。SQL读额外需要API Key；HITL回复使用新requestId。结果同步返回，调用方自行保存。该接口不自动执行上游或生成完整运行回调。

## Studio与存储

Studio用管理Token登录，知识、计划、结果、Node默认值存浏览器IndexedDB；后台无数据库。默认不访问tec01，勾选同步才回调既有知识/运行ID。暂停保存本地快照，继续提交新dispatchId；取消从本地计划列表移除。Key不入浏览器存储或导出文件。

服务内存只用于当前任务和短期幂等/事件记录。重启后由tec01或浏览器重新下发最近已知状态；它不能把未确认的外部SQL当作未执行。
