# itsm-workflow提供给tec01的API（0.11.0）

现行0.12.0完整字段、节点/连线约束、计划执行和回报示例，请使用[Workflow全流程接口文档](workflow-full-api-guide-0.12.0.md)。本页保留接口概要。

当前源码五个提取阶段各回报一次，不发送逐项进度或定时心跳；运行确认采用AopsResult，终止回报TERMINATED。tec01需配套的采纳确认、幂等及异常处理见[提取与执行对齐交付文档](tec01-extraction-execution-alignment.md)。

单服务、单进程部署。默认地址`http://itsm-workflow:8089`，UTF-8 JSON。配套见[tec01回调API](tec01-to-itsm-workflow-api.md)和[对齐与待补充约定](runtime-alignment-0.11.0.md)。

## 认证与错误

内部接口要求`Authorization: Bearer <RUNTIME_SERVICE_TOKEN>`。真实SQL执行另传`X-AOPS-Api-Key: <当前用户AOPS_API_KEY>`；Key不写入JSON或日志。

错误使用非2xx HTTP状态及`{code,message,retryable}`：401身份失败，409冲突，422校验失败，429容量不足，503必要回调未配置。429可带`Retry-After`，不表示SQL已经开始。

## 接口索引

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/internal/v1/runtime/catalog` | 节点目录，支持ETag/304 |
| GET | `/internal/v1/runtime/schemas` | Workflow、dispatch、extraction及各节点Schema |
| GET | `/internal/v1/runtime/schemas/{schemaName}` | 单份Schema |
| POST | `/internal/v1/runtime/workflows/validate` | 归一化与发布校验 |
| POST | `/internal/v1/runtime/workflows/plan` | 联调计划预览，不执行SQL |
| POST | `/internal/v1/extractions/{knowledgeId}` | 提交工单证据，202异步受理 |
| POST | `/internal/v1/executions/dispatch` | 一次下发、后台连续执行，200受理 |
| GET | `/internal/v1/executions/{runId}` | 查询本实例是否仍有活动任务 |
| POST | `/internal/v1/executions/{runId}/commands` | 暂停/取消，受理不等于生效 |

## 发布校验与计划

`validate`接收`workflowDefinition`、`validationMode:DRAFT|PUBLISH|EXECUTE|TEST`及可选`targetCatalogDigest`。返回规范化定义、目录摘要、内容哈希、警告及兼容版本。目录不符返回409。

`plan`接收`workflowVersionId/workflowContentHash/workflowSnapshot/ticketId/parameters/actorUid`，返回渲染节点、必填参数、风险摘要、缺失输入与`planMaterialHash`。SQL结果绑定和人工输入可延迟到运行时，不能猜测值。

tec01保存不可变发布版本，并在执行前保存对应计划和用户确认。`workflowSnapshot`是保留的字段名，内容为固定Workflow定义，不表示正常每步来回传整份执行状态。

## 下发执行

```json
{
  "dispatchId":"dispatch_1",
  "runId":"run_100173",
  "ticketId":100173,
  "workflowContentHash":"<validate返回的哈希>",
  "workflowSnapshot":{},
  "runInputs":{"customerName":"当前用户确认的参数"},
  "nodeStates":{},
  "nodeOutputs":{},
  "nodeResults":{},
  "currentNodeId":null,
  "selectedRoutes":{},
  "resumePayload":null
}
```

`workflowSnapshot`必须填完整规范化定义，上例空对象仅表示该位置，不能作为合法流程执行。节点类型为`sql_read/condition/hitl_form/end`。工单ID为正整数，SQL注释固定`#uatu-<ticketId>`。

200响应示例：

```json
{
  "accepted":true,
  "runId":"run_100173",
  "dispatchId":"dispatch_1",
  "executorId":"executor-01",
  "executorInstanceId":"<本次服务启动标识>",
  "status":"RUNNING",
  "acceptedAt":"2026-10-19T10:00:00+08:00"
}
```

这只代表受理，SQL可能还未开始。执行器将计划放入内存，每个节点结果获得tec01的AopsResult保存及采纳确认后，直接推进下一节点，无需tec01重新下发。

同dispatchId、同内容在本进程有限受理记录内返回原结果；内容不一致409。同runId仍活动时拒绝另一份下发。重启后不保证历史去重，不能透明重放结果未知的请求。

## 活动查询与恢复前核实

```json
{
  "runId":"run_100173",
  "executorId":"executor-01",
  "executorInstanceId":"<本次服务启动标识>",
  "active":true,
  "dispatchId":"dispatch_1",
  "currentNodeId":"sql-1",
  "phase":"WAITING_RESULT_ACK",
  "pauseRequested":false,
  "cancelRequested":false,
  "lastAcknowledgedNodeId":null,
  "observedAt":"2026-10-19T10:00:00+08:00"
}
```

phase为`QUEUED/EXECUTING/WAITING_START_ACK/WAITING_RESULT_ACK/WAITING_STOP_ACK/BETWEEN_NODES/INACTIVE`。此接口仅返回活动信息，不含SQL、参数、Key、输出或完整计划。

无活动时返回200与`active:false,phase:INACTIVE`，其余活动字段为null或false。**不表示历史成功、不表示从未执行，也不证明AOPS远端请求已撤销。** 必须查询原服务实例；实例变化说明重启，不能因此自动重跑SQL。

恢复前先确认旧活动已停止；仍活动则请求暂停/取消并再次查询。服务不可达或SQL事实不明时，tec01提示未知，由用户核对审计。单进程实例不支持被随机负载均衡成多个独立执行器。

## 暂停、取消与恢复

控制请求：`{"commandId":"command_1","type":"PAUSE"}`或`CANCEL`。返回accepted表示控制意图已记录，最终以执行器停止事实为准；不承诺点击即生效。

暂停在安全边界生效。取消尽力终止CLI，不能撤销已经到达AOPS的请求。控制遇到无活动返回accepted=false，仍不代表历史SQL未执行。

HITL、暂停、失败或未知核实后，以新dispatchId重新下发固定计划，以及必要的已确认状态和前置输出。SUCCEEDED节点不重放；RUNNING/UNKNOWN必须先人工核实并修正，不能原样恢复。成功条件节点需保留selectedRoutes，防止恢复时重新改变分支。

人工回复示例：

```json
{"nodeId":"input-1","action":"SUBMIT","values":{"customer_id":"C001","limit":10}}
```

将该对象放在`resumePayload`；同时填写当前节点、必要输出及原计划定义。只传“从第几步开始”而缺少后续绑定使用的数据会失败。

## 草稿提取

```json
{
  "requestId":"extract_100173_1",
  "ticketId":100173,
  "ticketInfo":{"id":100173,"incident_id":"INC-1","event_title":"客户查询"},
  "auditTimeline":[]
}
```

ticketInfo为info.data对象，auditTimeline为操作记录data数组，ticketId需一致。空数组可验证无有效操作的失败路径，不能提取有效草稿。tec01先建立知识/提取记录，服务端通过PROGRESS/FINISHED回调进度与结果。 PROGRESS和FINISHED均回传原提交requestId，回报幂等键包含该requestId；tec01按knowledgeId+requestId隔离提取尝试，旧请求迟到回报不得覆盖新请求，具体确认格式见回调API文档。

不依赖tec01任务领取或轮询。发布前校验、人工审核、知识存储由tec01负责；LLM只用于提取。
## 提取完整性扩展（0.13.3）

成功FINISHED回报新增`extractionOutcome=SUCCESS|PARTIAL_SUCCESS`、`publishable`，并扩展diagnostics。tec01须保存完整诊断后按原协议确认；部分成功不能直接发布，仍需人工处理遗漏及校验。失败时已完成的分类位于`error.details.diagnostics`。详见[字段、示例与审核规则](extraction-partial-success-addendum.md)，现有回报路径、幂等和重试规则不变。
