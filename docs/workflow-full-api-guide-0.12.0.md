# Workflow全流程接口文档：节点、连线、计划、执行与回报

适用：itsm-workflow **0.12.0**，主要供tec01后端、前端及Hermes适配开发使用。本文只描述已实现能力，不新增接口或改变运行架构。

交付文件：

- [完整Schema集合](json-schema-0.12.0.json)
- [节点与连线完整示例](examples/frontend-workflow-0.12.0.json)
- [全流程请求/响应示例集合](examples/workflow-api-examples-0.12.0.json)：完整validate、plan、首次dispatch和HITL恢复，哈希由当前执行器生成，不是手工填写。
- [前端JSON约束与交互指南](frontend-json-contract-0.12.0.md)：表单、参数树、画布和错误提示设计。
- [tec01回报契约](tec01-to-itsm-workflow-api.md)：当前AopsResult采纳确认、五阶段单次进度及提取隔离规则。

## 1. 五分钟LLM超时：当前配置边界

当前代码约束为`5 ≤ LLM_TIMEOUT_SECONDS ≤ 180`，默认30秒。**当前0.12.0不能直接配置5分钟；写300会在启动读取配置时校验失败。** 本文不修改代码。

当前可用的最大配置：

```dotenv
# /etc/itsm-workflow/service.env
LLM_TIMEOUT_SECONDS=180
```

```bash
sudo systemctl restart itsm-workflow-api
sudo systemctl status itsm-workflow-api --no-pager
sudo journalctl -u itsm-workflow-api -n 100 --no-pager
```

将来只有升级到明确支持至少300秒上限的版本，才能改为`LLM_TIMEOUT_SECONDS=300`后重启；不要在当前包中直接使用此值。也不建议直接修改已安装代码绕过配置校验，这会造成安装包与实际运行版本不一致。

该配置是本服务HTTP客户端的连接/读取等超时，不是整个提取任务严格总时限。`LLM_RESPONSE_FORMAT=auto`可能先请求json_object、失败后再请求plain，所以总耗时可超过单次配置。网关、反向代理及模型服务自身的超时仍需分别核对，客户端配置不能覆盖对端限制。

不要与以下配置混淆：

| 配置 | 作用 | 当前默认 |
|---|---|---|
| LLM_TIMEOUT_SECONDS | 调用LLM网关的网络超时 | 30秒，上限180 |
| 节点timeoutSeconds | 单个SQL等节点执行超时 | 600秒，范围1～7200 |
| TEC01_TIMEOUT_SECONDS | 回报tec01的单次HTTP超时 | 20秒，实际封顶30 |
| TEC01_CALLBACK_WAIT_SECONDS | 一条运行回报含重试的总等待 | 70秒，不用于提取回报或LLM |

## 2. 职责、认证及数据边界

| itsm-workflow负责 | tec01负责 |
|---|---|
| Node Registry、Schema、图和SQL校验 | 知识CRUD、权限、草稿和不可变发布版本 |
| 从工单证据提取Workflow草稿 | 来源工单数据、计划保存与用户确认 |
| 内存执行、节点调用、回报及本地进程控制 | 全部生产状态、结果、人工待办与恢复事实 |
| 独立Studio与安全运维日志 | UI、MCP与Hermes消息渠道 |

**没有“创建节点类型”“新增连线”“保存生产经验”这样的执行器CRUD接口。** 前端在tec01侧编辑整个workflowDefinition，节点实例与边属于这份定义；执行器通过validate校验整图，通过dispatch执行整图。新增节点类型需由执行器发布Node Registry代码，不允许通过JSON上传任意代码。

默认执行器地址：`http://itsm-workflow:8089`。存在反向代理上下文时，所有路径补上相应前缀。请求和响应为UTF-8 JSON。

tec01调用执行器：

```http
Authorization: Bearer <RUNTIME_SERVICE_TOKEN>
Content-Type: application/json
X-AOPS-Api-Key: <当前用户AOPS_API_KEY，仅真实SQL执行需要>
```

执行器回报tec01使用另一枚令牌：`Authorization: Bearer <TEC01_SERVICE_TOKEN>`，不可混用。服务令牌由tec01后端保管，不应交给生产前端。AOPS Key临时放请求头，不放JSON、URL、Workflow、日志或浏览器持久化存储。

## 3. 接口总览与顺序

| 方向 | 方法与路径 | 用途 | 成功状态 |
|---|---|---|---|
| tec01→执行器 | GET /internal/v1/runtime/catalog | 获取节点定义及目录摘要 | 200/304 |
| tec01→执行器 | GET /internal/v1/runtime/schemas | 获取全部JSON约束 | 200 |
| tec01→执行器 | GET /internal/v1/runtime/schemas/{schemaName} | 获取单份约束 | 200 |
| tec01→执行器 | POST /internal/v1/extractions/{knowledgeId} | 可选：从工单证据提取草稿 | 202，仅受理 |
| tec01→执行器 | POST /internal/v1/runtime/workflows/validate | 草稿/发布/执行语义校验 | 200 |
| tec01→执行器 | POST /internal/v1/runtime/workflows/plan | 可选：联调计划预览，不执行 | 200 |
| tec01→执行器 | POST /internal/v1/executions/dispatch | 首次执行、继续、重试、HITL回复 | 200，仅受理 |
| tec01→执行器 | GET /internal/v1/executions/{runId} | 查询原实例活动情况 | 200 |
| tec01→执行器 | POST /internal/v1/executions/{runId}/commands | 暂停或取消活动执行 | 200，需读accepted |
| 执行器→tec01 | POST /internal/v1/extractions/{knowledgeId}/callbacks | 提取进度与终态 | 200 |
| 执行器→tec01 | POST /internal/v1/runs/{runId}/callbacks | 节点开始/完成、运行释放 | 200且严格确认 |

```text
获取Catalog/Schema → 手工编排或工单提取 → 编辑节点/连线/绑定
→ validate → tec01保存固定版本 → 创建计划、补运行参数、用户确认
→ dispatch一次 → 节点开始回报 → 节点执行 → 节点结果回报
→ tec01确认保存 → 执行器自行进入下一节点
→ HITL/暂停/失败/完成时释放；需要恢复则tec01以新dispatchId下发
```

## 4. 节点目录与JSON Schema

### 4.1 GET /internal/v1/runtime/catalog

无请求体；可传`If-None-Match: "<catalogDigest>"`，命中返回304，无响应正文。200会返回ETag。

主要响应字段：

| 字段 | 说明 |
|---|---|
| protocolVersion | 当前目录协议版本 |
| runtimeVersion | 执行器程序版本 |
| catalogVersion / catalogDigest | 目录短版本与完整摘要 |
| generatedAt / testOnly | 接口生成时间、是否测试入口 |
| nodes[] | 每种已注册类型与版本的完整元数据 |

nodes中包含type、schemaVersion、handlerVersion、name、category、description、riskLevel、approvalPolicy、configSchema、inputSchema、outputSchema、inputTypes、uiSchema、supportedModes、idempotencyClass、resumeSemantics和resultSensitivity。

前端按`type + schemaVersion`查找配置Schema；uiSchema只提供图标/编辑器提示，不是可执行代码。当前类型为sql_read、condition、hitl_form、end，版本均为1。不提供独立单节点执行接口、hitl_select或LLM节点。

### 4.2 GET /internal/v1/runtime/schemas[/schemaName]

集合返回runtimeVersion、jsonSchemaDialect、schemas、nodes。schemaName仅支持：

- workflowDefinition：整图定义。
- dispatch：执行下发，workflowSnapshot已经引用整图Schema。
- extraction：提取请求。

未知名称返回404/SCHEMA_NOT_FOUND。标准为Draft 2020-12；保留$defs与$ref，不应为解析本地引用而访问外网。整图Schema没有穷尽节点config分派和跨字段语义，必须另校验各节点config，并在发布前调用validate。

## 5. Workflow：节点、连线和数据绑定

### 5.1 顶层及公共属性

| 位置 | 字段及约束 |
|---|---|
| 顶层 | schemaVersion=1或2，规范化为2；entryNodeId必填且存在；nodes为1～500项；edges默认[]、最多1000项；catalogDigest由服务规范化 |
| 节点 | id唯一且匹配`^[A-Za-z][A-Za-z0-9_-]{0,119}$`；type必填；title为1～200字符；schemaVersion默认1；handlerVersion由规范化补齐 |
| 节点配置 | config默认{}，必须通过该类型configSchema；inputs默认[]；approvalPolicy为NONE/PLAN/NODE，默认PLAN；timeoutSeconds为1～7200、默认600 |
| 布局 | uiPosition可空，建议{x:数字,y:数字}；不要把组件选中状态等未知字段混入业务定义 |
| 边 | id/source/target必填；kind=NORMAL或CONDITION；label默认空；default默认false；condition为规则对象或null |

控制图必须可达、无环、无自连；普通节点最多一条出边，条件节点恰好一条默认边，非默认出边必须有条件。前端还应主动保证边ID唯一，不依赖当前公共Schema自动完成这一检查。

### 5.2 参数来源

```json
{"name":"customer_id","type":"string","description":"客户编号","required":true,"source":{"kind":"NODE_OUTPUT","nodeId":"input-1","jsonPointer":"/values/customer_id"}}
```

name为1～80字符，同节点唯一；description最多500字符；type为string/number/integer/boolean/object/array；required默认true。

| kind | 所需字段 | 说明 |
|---|---|---|
| RUN_INPUT | key | 从本次runInputs取值 |
| NODE_OUTPUT | nodeId、jsonPointer | 从已完成前置节点的输出取值 |
| LITERAL | value | 固定值，保持真实JSON类型 |

JSON Pointer相对于来源节点输出：`/data/0/id`、`/rowCount`、`/values/customer_id`。数组从0开始，字段中的`/`、`~`分别用`~1`、`~0`。连线不会自动建立输入绑定，绑定也不会代替执行连线。跨互斥分支引用可能导致运行时缺值，即使图上存在可达关系也不能保证该输出产生。

### 5.3 四种config

| 类型 | config与行为 |
|---|---|
| sql_read | databaseRef、sqlTemplate必填且不允许额外配置；路径非空≤1000字符；SQL为单条安全只读；占位符集合与inputs.name完全一致；参数只支持四种标量类型 |
| condition | 完整流程建议config:{}，条件存于出边；按edges顺序选首个匹配分支，否则默认边 |
| hitl_form | title、fields必填，fields为1～50项，每项name/label/type/required必填；string/integer/number/boolean，可配长度、范围、enum |
| end | 建议config:{}、inputs:[]、approvalPolicy:NONE且无出边，显式表示结束 |

SQL占位符示例：`WHERE name={{customer_name}}`。不要在占位符外另加字符串引号，不允许参数充当表名、列名或SQL片段。实际comment固定为`#uatu-<ticketId>`。

条件示例：

```json
{"path":"/nodes/sql-1/output/rowCount","op":"gt","value":0}
```

条件路径相对于运行状态，与输入绑定Pointer的根不同。支持eq/ne/gt/gte/lt/lte/in/contains/exists/empty，all/any为非空规则数组，not为单个规则；exists/empty不需要value。不要把所有命中分支都执行，也不要自行添加循环。

HITL字段enum可渲染成下拉框，但仍属于hitl_form，不是独立选择节点。HITL输出为`{"values":{...}}`，下游绑定`/values/<字段名>`。字段名应唯一、范围上下限合理，前端提交前需本地检查。

## 6. POST /internal/v1/runtime/workflows/validate

请求：

| 字段 | 必填 | 说明 |
|---|---|---|
| workflowDefinition | 是 | 完整图，不是单个节点或增量 |
| validationMode | 否 | DRAFT/PUBLISH/EXECUTE/TEST，默认DRAFT |
| targetCatalogDigest | 否 | 与当前目录不符返回409 |

```json
{"validationMode":"PUBLISH","workflowDefinition":{"schemaVersion":2,"entryNodeId":"done","nodes":[{"id":"done","type":"end","title":"完成","config":{},"inputs":[],"approvalPolicy":"NONE"}],"edges":[]}}
```

200响应字段：

| 字段 | 用途 |
|---|---|
| valid | 通过时true；语义失败通常直接返回422，不是200+false |
| normalizedDefinition | 完整规范化图，默认值、Handler版本及目录摘要已补齐 |
| workflowContentHash | 规范化定义内容哈希 |
| catalogDigest | 当前节点目录摘要 |
| requiredRuntimeVersion | 所需运行时兼容范围 |
| warnings | 非阻断警告，如PUBLISH时固定Handler与当前版本不一致 |

tec01保存响应中的规范化定义与哈希，再建立不可变版本。不要由前端重算哈希，不要编辑定义后继续使用旧哈希。DRAFT同样检查合法图，不是容纳任意未完成画布的保存接口。验证通过不证明目标库权限、表和字段有效，也不执行SQL。

## 7. POST /internal/v1/runtime/workflows/plan

只用于联调/预览，不执行SQL；生产计划保存、planHash及用户确认由tec01负责。

| 请求字段 | 必填 | 说明 |
|---|---|---|
| workflowVersionId | 是 | tec01固定版本ID |
| workflowContentHash | 是 | validate返回值 |
| workflowSnapshot | 是 | 与哈希对应的完整规范化定义 |
| ticketId | 是 | 正整数 |
| parameters | 否 | 当前运行参数，默认{} |
| actorUid | 否 | 1～120字符，默认studio，仅用于计划材料 |

完整请求及响应见示例集合中的plan。200响应包含valid、missingInputs、renderedPlan、planMaterialHash、catalogDigest、runtimeVersion。

- missingInputs非空时valid=false，前端应补参数；参数类型错误或哈希错误返回422。
- renderedPlan含nodes、edges、requiredInputs、riskSummary；requiredInputs按RUN_INPUT.key聚合，含type、description、required、usedBy。
- NODE_OUTPUT和HITL输入允许到运行时才解析，不要为创建计划伪造值。
- 内部接口目前返回SQL模板与输入来源，**不保证包含renderedSql**。不要因字段名renderedPlan就假定所有SQL已经解析成最终语句。
- planMaterialHash与workflowContentHash不是同一概念；也不能直接替代tec01自己的确认协议。
- approvalPolicy=NODE仅是策略声明，不代表当前已有完整的自动节点审批能力。

## 8. POST /internal/v1/executions/dispatch

含任意sql_read节点时，必须额外传当前用户的X-AOPS-Api-Key，即使这些SQL节点已经执行过。首次、继续、HITL回复、失败重试都使用此接口。

| 字段 | 必填 | 类型与说明 |
|---|---|---|
| dispatchId / runId | 是 | string，1～160字符，`^[A-Za-z0-9_.:-]+$` |
| ticketId | 是 | 正整数 |
| workflowContentHash | 是 | validate返回的哈希，1～128字符 |
| workflowSnapshot | 是 | 完整规范化图，每次都传 |
| runInputs | 否 | 本次运行参数object，默认{} |
| nodeStates | 否 | 节点ID→状态，默认{} |
| nodeOutputs | 否 | 节点ID→真实输出object，默认{} |
| nodeResults | 否 | 调试/交互结果，生产可省略 |
| currentNodeId | 否 | 当前恢复起点；省略时依次取resumePayload.nodeId、entryNodeId |
| selectedRoutes | 否 | 条件节点ID→已选边ID |
| resumePayload | 否 | 人工回复等，默认null |

完整首次请求见示例集合dispatch.request，避免使用空workflowSnapshot代替实际图。

200受理响应：

```json
{"dispatchId":"dispatch_demo_1","runId":"run_demo_1","accepted":true,"executorId":"executor-01","executorInstanceId":"<本次实例ID>","acceptedAt":"2026-10-22T10:00:00+08:00","status":"RUNNING"}
```

**受理不代表SQL已发出，更不代表成功。** 执行器在后台串行运行，每个节点开始/结果回报得到有效保存确认后继续；正常推进无需tec01每步再次下发。

幂等与失败：同dispatchId同正文在本进程有限缓存内不启动第二次，不同正文409；同runId仍活动409；容量满429并带Retry-After；图、哈希、版本、状态等校验错误422。重复请求返回缓存记录，状态可能已经更新，不要把它当作一次新的执行。服务重启或缓存清理后不保证历史幂等。

恢复注意：

- RUNNING/UNKNOWN不能直接传入nodeStates，需先核实；SUCCEEDED不重放，SKIPPED不执行。
- 已成功condition必须提供selectedRoutes；引用过的已成功结果应在nodeOutputs中保留。
- FAILED、PAUSED、WAITING可按业务场景恢复；SKIPPED/TERMINATED不能被当作普通重放起点。
- 部分绑定、输入及恢复问题到达节点时才检查，可能表现为200受理后的失败回报，不能假设受理已经验证了所有运行期值。
- 快照事实由tec01保存，不允许前端凭空将未知节点标成功或填入假输出。

## 9. 节点回报与保存确认

默认由执行器调用：

```http
POST {TEC01_BASE_URL}/internal/v1/runs/{runId}/callbacks
Authorization: Bearer <TEC01_SERVICE_TOKEN>
X-Request-Id: <稳定幂等键>
```

路径可配置为TEC01_RUN_CALLBACK_PATH。所有回报带dispatchId；节点回报还带attemptId和nodeId。

| type | 内容 | 幂等键 |
|---|---|---|
| NODE_STARTED | 节点ID、attempt、startedAt | `{attemptId}:started` |
| NODE_COMPLETED | status、runStatus、safeSummary、result、diagnostic、interaction、selectedEdgeId、skippedNodeIds | `{attemptId}:completed` |
| RUN_RELEASED | status、currentNodeId、reason | `{dispatchId}:released` |

节点完成示例：

```json
{"type":"NODE_COMPLETED","dispatchId":"dispatch_demo_1","attemptId":"attempt_demo_1","nodeId":"sql-1","status":"SUCCEEDED","runStatus":"RUNNING","safeSummary":"查询返回2行","result":{"status":0,"rowCount":2,"data":[{"id":"C001"},{"id":"C002"}]},"diagnostic":null,"interaction":null,"selectedEdgeId":"e-query-check","skippedNodeIds":[]}
```

节点成功不表示流程成功，条件节点还会回报选中的边和跳过节点；HITL会返回WAITING_INPUT及interaction。生产回报不附整份运行快照，tec01逐条保存事实。

tec01事务提交并采纳当前消息后必须返回HTTP200、整数status=200及AopsResult确认：

```json
{"status":200,"msg":"success","data":{"runId":"run_demo_1","status":"RUNNING","lastEventId":1}}
```

三类运行回报使用同一确认格式；执行器校验runId，不再要求saved、dispatchId、type或节点身份回显。空data、202、204、错误业务status或runId不符均不能放行下一节点。对外终止状态统一TERMINATED，不再发送CANCELLED。

先拒绝旧dispatchId；再对当前调度已保存的完全相同回报确认200，即使运行已终态；最后拒绝新的终态冲突409。不能用普通200忽略旧调度。回报默认对5xx、连接异常、超时最多6次，退避1/2/4/8/16秒，总等待默认70秒；4xx及无效确认不重试。

最终未确认则停止并释放，不保证再另发一次RUN_RELEASED。未收到结果不代表SQL没执行，tec01应核实UNKNOWN，不自动重放。日志HTTP200/acknowledged=true只说明回报被确认，businessStatus=FAILED仍表示节点或提取失败。

## 10. 活动查询、暂停、取消和恢复

### 10.1 GET /internal/v1/executions/{runId}

返回runId、executorId、executorInstanceId、active、dispatchId、currentNodeId、phase、pauseRequested、cancelRequested、lastAcknowledgedNodeId、observedAt，不返回SQL、参数或结果。

phase包括QUEUED、EXECUTING、WAITING_START_ACK、WAITING_RESULT_ACK、WAITING_STOP_ACK、BETWEEN_NODES、INACTIVE。无活动返回200、active=false、phase=INACTIVE。

active=false不证明从未执行；404、连接失败也不能当作旧执行安全结束。恢复需查询原服务实例，实例重启、网络异常或外部结果不明应由tec01提示人工核实。

### 10.2 POST /internal/v1/executions/{runId}/commands

```json
{"commandId":"command_pause_demo_1","type":"PAUSE"}
```

type仅允许PAUSE/CANCEL，commandId为1～160字符。活动中返回：

```json
{"commandId":"command_pause_demo_1","accepted":true,"runId":"run_demo_1","currentNodeId":"sql-1"}
```

无活动时仍为HTTP200，但返回accepted=false、reason=RUN_NOT_ACTIVE。前端先显示请求中，最终按回报事实更新；暂停在安全边界生效，取消尽力停止本地CLI，不能撤销已到达AOPS的请求。

继续不使用RESUME命令，而是生成新dispatchId再dispatch。前置SUCCEEDED及输出保留，从暂停/失败/人工节点恢复，不能把原整图全部当作未执行重放。

### 10.3 HITL完整交互

1. 节点完成回报status=WAITING_INPUT，interaction包含kind=HITL_FORM、nodeId、title、description、fields和context。
2. tec01保存人工待办并通知UI/Hermes；执行器随后回报RUN_RELEASED/WAITING_INPUT并释放活动运行。
3. UI依据fields展示类型、必填、范围和enum，按权限展示context里的前置结果。不要自动选择多行结果的第一条，也不要猜测缺值。
4. 用户填写后，tec01保存回复，并使用新dispatchId和可信事实重新下发。

```json
{"resumePayload":{"nodeId":"input-1","action":"SUBMIT","values":{"customer_id":"C001","query_limit":20}}}
```

完整恢复请求见示例集合hitlResume.request。执行器校验后输出values，下游SQL通过NODE_OUTPUT绑定。数字20与字符串"20"不同；optional字段未填可省略，false/0不等于缺失。非法回复可能导致该次节点FAILED，不承诺自动停留WAITING_INPUT。

## 11. 草稿提取及全部阶段回报

### 11.1 POST /internal/v1/extractions/{knowledgeId}

tec01先建立EXTRACTING记录，再提交：

```json
{"requestId":"extract_100173_1","ticketId":100173,"ticketInfo":{"id":100173,"incident_id":"INC-2026-0001","event_title":"客户查询"},"auditTimeline":[]}
```

requestId为1～160字符、匹配`^[A-Za-z0-9_.:-]+$`。ticketInfo是info.data对象，auditTimeline是操作记录data对象数组，不传完整信封；ticketId与ticketInfo.id必须一致。生产摄入不需要AOPS Key、不执行aops-cli取证或SQL；空操作数组可受理但会提取失败。

返回202：`{"knowledgeId":"knw_100173","accepted":true,"duplicate":false}`，仅表示受理。当前自动提取生成串行SQL节点及结束节点，条件/HITL可在后续人工编辑时增加。

### 11.2 POST /internal/v1/extractions/{knowledgeId}/callbacks

对端路径由TEC01_EXTRACTION_CALLBACK_PATH配置。每条含type、原requestId、seq、occurredAt。

| 顺序 | stage/type | 当前发送时点 |
|---|---|---|
| 1 | FILTERING_OPERATIONS | 过滤前一次，0/总数，不再发送完成进度 |
| 2 | PARAMETERIZING_SQL | 参数化前一次，0/总数，不再发送逐项进度 |
| 3 | ANALYZING_EVIDENCE | 分析前一次，等待期间不发送定时心跳；请求超时机制不变 |
| 4 | BUILDING_WORKFLOW | 组装前一次 |
| 5 | VALIDATING_WORKFLOW | 校验前一次 |
| 6 | FINISHED | 成功或失败终态，网络重试可能重发 |

```json
{"type":"PROGRESS","requestId":"extract_100173_1","seq":1,"stage":"FILTERING_OPERATIONS","message":"正在过滤有效 SQL","progress":{"current":0,"total":5},"occurredAt":"2026-10-22T10:00:00+08:00"}
```

成功终态带status=SUCCEEDED、proposal（中文名称/摘要/短语/系统/完整图）、validation（valid/catalogDigest/workflowContentHash）、diagnostics（工单编号、总数、接受数、忽略原因等）。tec01保存为DRAFT，不自动发布。

```json
{"type":"FINISHED","requestId":"extract_100173_1","seq":9,"status":"FAILED","error":{"code":"LLM_ANALYSIS_FAILED","message":"模型未返回有效的结构化结果，请检查内网模型配置后重试","retryable":true},"occurredAt":"2026-10-22T10:00:30+08:00"}
```

失败码主要为NO_VALID_OPERATIONS、LLM_ANALYSIS_FAILED、COMPILER_FAILED；失败后不发送尚未执行阶段。LLM_ANALYSIS_FAILED是本服务生成的业务错误，既可能来自网络/结构化解析失败，也可能是中文名称摘要或匹配短语缺失，不能当作HTTP状态码解释。

tec01成功响应：

```json
{"status":200,"msg":"success","data":{"knowledgeId":"knw_100173","status":"EXTRACTING"}}
```

检查HTTP200、整数status=200、knowledgeId一致，不要求saved或响应回显requestId。提取回报对5xx、连接失败及超时最多6次，退避1/2/4/8/16秒，不使用运行回报70秒总上限。

幂等键：进度`{knowledgeId}:{requestId}:{seq}`，终态`{knowledgeId}:{requestId}:finished`。同次回报重试正文/身份不变；tec01按knowledgeId+requestId隔离尝试、按最大已接受seq维护水位，忽略旧请求迟到结果。

进度回报最终失败不会直接停止提取，后续阶段仍可能回报；页面允许跳阶段和序号间隙。同步失败与提取失败分开展示。相同提交在内存记录有效时不重新调用LLM，未确认终态可通过同请求重发触发补发；失败后真正重新分析用新requestId。

当前已结束提取记录临时保留最多64条、约1小时，通过提交/查询/结束等时机清理，不跨服务重启；不能把该缓存当生产持久化。

## 12. 错误、前端展示与联调清单

内部接口错误为扁平`{code,message,retryable}`，可带details；不是Studio接口常见的detail外层。

| HTTP | 场景 |
|---|---|
| 401 | RUNTIME_SERVICE_TOKEN无效或未配置 |
| 404 | Schema或接口不存在；不是业务成功标记 |
| 409 | 目录变化、重复ID不同内容、同运行仍活动等冲突 |
| 422 | 请求结构、图、SQL、参数、哈希或恢复状态校验失败 |
| 429 | 执行器容量不足，按Retry-After退避 |
| 503 | 必要回报配置缺失等服务不可用 |

结构错误可带details.fields数组，条目为field/message，例如body.ticketId；图/SQL语义错误当前主要是message，不保证机器可解析的nodeId/fieldPath。前端字段定位优先使用本地Schema检查，不把中文错误文本当稳定协议。

需要分开展示：HTTP提交是否受理、节点/提取业务是否成功、回报是否被确认。不能用HTTP200或日志CALL_COMPLETED当作整个业务成功。生产运行事实、结果及人工待办从tec01读取，不靠日志猜测；执行器日志是安全元数据，未完整记录回报正文中的所有业务错误字段。

联调至少验证：

1. Catalog/Schema获取、版本与目录变更，未知节点只读保护。
2. 卡片编辑、连线、入口、默认分支、参数来源及删除引用提示。
3. validate保存规范化定义与真实哈希；plan缺参数和类型错误。
4. 首次执行及每节点严格确认；旧调度409；结果回报失败不继续SQL。
5. 条件只走一条分支，未命中节点SKIPPED，恢复保留选中边。
6. SQL多行结果→HITL多字段→下一SQL绑定，不自动替用户选值。
7. 暂停/取消请求中与最终状态区分；恢复前查旧活动；未知结果需人工核实。
8. 提取每阶段、分析期间无定时心跳、失败终态、重复提交及新旧requestId隔离。
9. 凭据只走受控请求头，不进入前端持久化、URL、日志及导出定义。

独立Studio另使用`/api/v1/studio/*`与HttpOnly管理会话，接口参数不与上述生产内部契约混用。其业务数据留在浏览器；生产知识CRUD、计划保存、SSE/MCP等业务接口由tec01另行提供，本文不虚构其路径。
