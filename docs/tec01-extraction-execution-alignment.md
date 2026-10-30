# tec01 提取与执行协议对齐交付文档

日期：2026-10-23。适用itsm-workflow 0.12.3，已按同日确认文档对齐阶段颗粒度、AopsResult及TERMINATED；旧安装包需升级部署才生效。

本文供 tec01 开发、前端、Hermes 渠道及联调人员使用。目标是对齐现有提取器和执行器，不要求迁移架构，也不新增业务数据库、LLM 运行节点或 MCP 执行实现。本仓库不包含 tec01 服务端，以下 tec01 行为需要对端实施并联调确认。

## 1. 必须同步的差异

相对于此前桌面交付的两份双向接口文档，按以下约定修正：

| 项目 | tec01 应采用的约定 | 原因 |
|---|---|---|
| 提取颗粒度 | 五个阶段各在进入时回报一次，不发逐项进度和定时心跳 | 避免重复阶段消息 |
| 运行确认 | HTTP 200、整数 status=200、data.runId匹配；不要求saved和节点身份回显 | 200表示事务提交并采纳本次回调 |
| 终止状态 | NODE_COMPLETED、runStatus和RUN_RELEASED统一使用TERMINATED | tec01不接受CANCELLED状态 |
| 旧调度回报 | HTTP 409，不返回放行旧执行器的成功确认 | 阻止过期执行器继续执行 |
| 提取尝试 | 每条消息携带原提交 requestId；按 knowledgeId + requestId 隔离 | 防止迟到消息污染重新提取 |
| 提取幂等键 | PROGRESS、FINISHED 均包含 requestId，格式见第5节 | 不把不同分析尝试误判为重复 |
| 进度水位 | 使用本次请求最大已接受 seq，不用收到的事件条数 | 允许丢失进度后的序号间隙 |
| 运行回报总等待 | 默认70秒，先达到时间或次数上限即停止 | 不保证所有六次尝试都会发生 |
| 退出通知 | 回报链路失败、进程关闭或崩溃时，不保证最后 RUN_RELEASED | 必须有失联核查，不能无限等终态 |

保留：tec01 主动下发、执行器内存连续执行、逐节点确认保存、HITL 表单、暂停/取消/恢复、浏览器独立调试。LLM请求超时、回报网络重试和临时缓存机制不变。节点输出中的CLI技术错误码CANCELLED可保留作为诊断，但对外状态统一TERMINATED。

## 2. 接口、认证与数据职责

### tec01 调用 itsm-workflow

请求头为 `Authorization: Bearer <RUNTIME_SERVICE_TOKEN>`。真实 SQL 执行额外传 `X-AOPS-Api-Key: <当前用户 AOPS_API_KEY>`；JSON 证据提取不需要该用户 Key，不会执行历史 SQL。

| 方法与路径 | 用途 |
|---|---|
| GET /internal/v1/runtime/catalog | 获取支持的节点及目录摘要 |
| GET /internal/v1/runtime/schemas | 获取 JSON 约束 |
| POST /internal/v1/runtime/workflows/validate | 草稿、发布和执行校验；返回规范化定义及内容哈希 |
| POST /internal/v1/runtime/workflows/plan | 可选计划预览，不执行 SQL |
| POST /internal/v1/extractions/{knowledgeId} | 提交证据，202异步受理 |
| POST /internal/v1/executions/dispatch | 下发完整固定计划，200受理 |
| GET /internal/v1/executions/{runId} | 查询当前实例是否仍有活动执行，不是历史结果查询 |
| POST /internal/v1/executions/{runId}/commands | PAUSE或CANCEL；继续使用新的dispatch重新下发 |

完整字段及约束见[Workflow 全流程接口文档](workflow-full-api-guide-0.12.0.md)、[Workflow 逐字段解释](workflow-definition-field-reference-0.12.0.md)及[可用 JSON 示例](examples/workflow-api-examples-0.12.0.json)。提取、执行不提供单节点调试接口，单节点场景可构造小流程测试。

### itsm-workflow 回报 tec01

| 默认路径 | type |
|---|---|
| POST /internal/v1/extractions/{knowledgeId}/callbacks | PROGRESS、FINISHED |
| POST /internal/v1/runs/{runId}/callbacks | NODE_STARTED、NODE_COMPLETED、RUN_RELEASED |

请求头为 `Authorization: Bearer <TEC01_SERVICE_TOKEN>`、`Content-Type: application/json`、`X-Request-Id: <幂等键>`。两个路径可配置，不改变回报正文和确认规则。

tec01 保存生产草稿、版本、运行、节点结果、人工交互和事件；保存后推送页面 SSE 与 Hermes 渠道。执行器只保留活动计划及有限临时数据，不保存生产业务数据库。调用日志不能代替执行事实。

## 3. 提取流程与 tec01 处理

### 3.1 提交

tec01 先创建经验与本次提取请求记录，设为 EXTRACTING，再提交；必须在发请求前准备好接收回报，不假设202响应一定早于首条回报。

`POST /internal/v1/extractions/knw_100173`

```json
{
  "requestId":"extract_100173_1",
  "ticketId":100173,
  "ticketInfo":{
    "id":100173,
    "incident_id":"INC-2026-0001",
    "event_title":"客户信息查询",
    "incident_description":"查询客户基础信息"
  },
  "auditTimeline":[
    {
      "id":10897,
      "operation":"sql_exec_read",
      "result":"{\"r\":true,\"e\":\"\"}",
      "details":{"sql":{"serverid":124,"dbid":"aops-t","dbname":"aops-t","dbuser":"readonly","service_name":"aops-t","command":"SELECT id FROM users LIMIT 10"}},
      "created_at":1786433939
    }
  ]
}
```

ticketInfo 必须是 info.data 对象，auditTimeline 必须是 audit_timeline.data 对象数组；不接收完整响应信封。ticketId 为正整数且与 ticketInfo.id 一致。details、result 可按审计来源传 JSON 字符串或已解析对象。

requestId 为1至160字符，只允许字母、数字、下划线、点、冒号、短横线。同知识下原 requestId 与相同证据重复提交不重新分析；不同证据返回冲突。明确提取失败后重新分析使用新的 requestId；已有成功记录或任务仍活动时，不能通过随意换 requestId 再次启动。

### 3.2 阶段及频率

| stage | 操作 | 回报频率 |
|---|---|---|
| FILTERING_OPERATIONS | 过滤失败、无效和非安全只读操作 | 进入时一次 |
| PARAMETERIZING_SQL | 按数据库和SQL去重，将历史查询条件参数化 | 进入时一次 |
| ANALYZING_EVIDENCE | LLM提炼中文名称、摘要、匹配规则、参数语义和依赖 | 进入时一次，不发送定时心跳 |
| BUILDING_WORKFLOW | 组装结构化SQL流程草稿 | 进入时一次 |
| VALIDATING_WORKFLOW | 校验、归一化、生成哈希 | 进入时一次 |

正常成功为五条PROGRESS加一条FINISHED；失败只回报实际到达的阶段和失败终态，不补齐未执行阶段。进入过滤和参数化时progress可为0/N，它表示阶段开始，不再逐项更新；完整数量和过滤明细统一看终态diagnostics。当前自动提取以线性SQL读节点及end为主，condition和hitl_form按审核需求补充。

进度示例：

```json
{"type":"PROGRESS","requestId":"extract_100173_1","seq":2,"stage":"PARAMETERIZING_SQL","message":"正在参数化 SQL","progress":{"current":0,"total":2},"occurredAt":"2026-10-23T02:00:00+00:00"}
```

进入分析后的等待期间没有新回报是正常现象。页面显示“分析中”和等待时长，不伪造百分比，不使用“15秒没心跳”判定失败。LLM请求超时仍按原服务配置；异常最终回报FAILED，但停机或回报丢失时可能没有终态。

### 3.3 成功、失败与保存

FINISHED共有字段：type、requestId、seq、occurredAt、status。

| status | 附加字段 | tec01处理 |
|---|---|---|
| SUCCEEDED | proposal、validation、diagnostics | 同事务保存草稿和过滤账本，转DRAFT；不是自动发布 |
| FAILED | error.code、message、retryable | 保存提取失败与事件，展示安全原因 |

proposal包含name、summary、matchPhrases、negativePhrases、systemKeys、完整workflowDefinition；validation包含catalogDigest、workflowContentHash、valid。diagnostics保留提取器账本，包括原始条数、保留条数、去重和过滤原因，不在外层重新计算覆盖。

失败示例：

```json
{"type":"FINISHED","requestId":"extract_100173_1","seq":4,"status":"FAILED","error":{"code":"LLM_ANALYSIS_FAILED","message":"模型未返回有效的结构化结果，请检查内网模型配置后重试","retryable":true},"occurredAt":"2026-10-23T02:00:30+00:00"}
```

稳定失败码为NO_VALID_OPERATIONS、LLM_ANALYSIS_FAILED、COMPILER_FAILED。错误中的retryable是业务重新分析建议，不表示回报HTTP请求会自动重新分析。

提取回报确认必须是HTTP 200、整数status=200，knowledgeId与路径相同：

```json
{"status":200,"msg":"success","data":{"knowledgeId":"knw_100173","status":"EXTRACTING"}}
```

data.status可返回tec01实际知识状态；执行器只验证status与knowledgeId，不要求saved或回显requestId。tec01仍应提交事务后才确认。

### 3.4 隔离与失联

- 按knowledgeId + requestId建立本次进度水位和终态去重范围。新请求不沿用旧请求的拒收水位。
- 当前请求的PROGRESS只接受比已保存最大seq更大的事件；允许有序号间隙。FINISHED还需按本次请求终态去重，不能仅按经验ID永久拒收。
- 旧requestId的迟到回报、重复或更旧进度、重复终态：不覆盖当前事实，返回对应knowledgeId的正常提取确认。
- 进度回报失败不终止提取；业务成功与同步成功分开。最终确认失败时，临时缓存有效可按相同提交补发原终态，不重新调用LLM。
- 缓存不是消息队列：完成记录默认最多64条、约一小时，重启丢失；不能承诺任何时候可找回未保存结果。
- tec01对长期无终态进行核查并提示中断或同步待核实，不自动显示成功，也不能仅因用户等得久就并发启动相同任务。

## 4. 执行流程与 tec01 处理

### 4.1 下发与连续推进

tec01保存审核发布的不可变定义及哈希，完成用户参数收集与执行确认后，创建runId和新的dispatchId再下发。

dispatch请求包含dispatchId、runId、ticketId、workflowContentHash、完整workflowSnapshot；按恢复需要传runInputs、nodeStates、nodeOutputs、nodeResults、currentNodeId、selectedRoutes、resumePayload。首次可使用空状态；恢复必须保留已确认事实，不能把所有节点重置后重放。

执行器在内存保存整份计划，沿路线逐节点执行。每个实际节点执行顺序为：

```text
NODE_STARTED → tec01保存并确认 → 解析输入、执行节点
             → NODE_COMPLETED → tec01保存并确认
             → 成功且无停止条件：继续下一节点
             → 结束/暂停/取消/失败/HITL：RUN_RELEASED并退出
```

不是执行一步就释放并等待新计划；只有退出当前活动任务后，恢复或重试才由tec01生成新dispatchId重新下发。条件分支未选中的节点不执行，不逐个产生执行回报。

### 4.2 三类运行回报

NODE_STARTED：

```json
{"type":"NODE_STARTED","dispatchId":"dispatch_1","attemptId":"attempt_1","nodeId":"sql-1","startedAt":"2026-10-23T02:10:00+00:00"}
```

tec01保存节点开始与attempt事实。此时不能声称SQL已经发出；执行器获得开始确认后才执行。

NODE_COMPLETED：

```json
{"type":"NODE_COMPLETED","dispatchId":"dispatch_1","attemptId":"attempt_1","nodeId":"sql-1","status":"SUCCEEDED","runStatus":"RUNNING","safeSummary":"查询返回1行","result":{"status":0,"rowCount":1,"data":[{"customer_id":"C001"}]},"diagnostic":null,"interaction":null,"selectedEdgeId":"edge-1","skippedNodeIds":[]}
```

tec01同事务保存节点结果、诊断、attempt状态、条件选中路线、跳过节点和人工交互，并追加事件。status是节点结论，runStatus只是当时观察，不能将单节点成功直接判为流程成功。

RUN_RELEASED：

```json
{"type":"RUN_RELEASED","dispatchId":"dispatch_1","status":"PAUSED","currentNodeId":"sql-1","reason":"当前节点已完成，运行暂停"}
```

tec01保存最终停止状态、清理活动归属并追加事件。执行器发送停止回报后正常等待确认，再完成活动清理；回报并不意味着清理已在对端收到之前完成。不得立即并发下发恢复，遇到仍活动的409需先核查并等待原活动释放。

### 4.3 AopsResult保存与采纳确认

NODE_STARTED、NODE_COMPLETED及RUN_RELEASED均采用相同的AopsResult结构，HTTP200表示本次请求已提交并采纳：

```json
{"status":200,"msg":"success","data":{"runId":"run_100173","status":"RUNNING","lastEventId":1}}
```

RUN_RELEASED确认示例：

```json
{"status":200,"data":{"runId":"run_100173","status":"PAUSED","lastEventId":2}}
```

执行器验证HTTP200、整数status=200、data对象及匹配的runId，不再要求saved或dispatchId/type/nodeId/attemptId回显。data.status和lastEventId供参考，不用于决定节点跳转。tec01必须在事务提交并采纳本次消息后才返回200，不可提前入队就确认。

处理顺序为：先拒绝旧dispatchId；再对当前调度已保存、正文相同的消息确认200，即使运行已终态；最后拒绝新的终态冲突。重复确认不重复插入或回退状态，同身份不同正文不能按正常重复放行。旧调度返回HTTP409，不得以200忽略。此顺序由tec01实现，执行器不能从响应直接证明数据库事实。

### 4.4 人工输入、条件和控制

| 场景 | 对齐行为 |
|---|---|
| 前置结果绑定 | tec01保存前置结果；后续节点按NODE_OUTPUT及JSON Pointer解析，恢复时携带必要结果 |
| condition | 保存selectedEdgeId对应路线与skippedNodeIds；默认边必须存在，判断不调用LLM |
| hitl_form | NODE_COMPLETED为WAITING_INPUT，带interaction；保存表单待办后确认，再接收RUN_RELEASED |
| 用户回复HITL | tec01/Hermes收集并保存values；新dispatch携带原定义、必要状态与resumePayload后恢复 |
| 暂停 | PAUSE命令受理不等于生效；当前节点到安全边界后回报PAUSED并释放 |
| 取消 | CANCEL命令尽力终止CLI；回报status及runStatus统一TERMINATED，不再发送CANCELLED状态 |
| 失败重试 | 展示具体diagnostic；用户确认后生成新dispatch，从明确允许重试的节点继续 |

HITL恢复载荷示例（是dispatch中的resumePayload字段，不是独立恢复接口）：

```json
{"nodeId":"input-1","action":"SUBMIT","values":{"customer_id":"C001"}}
```

interaction.kind为HITL_FORM，包含nodeId、title、description、fields、context，供页面或Hermes提示用户参考结果填写一个或多个参数；不是自动选择，也不允许agent猜测参数。

TERMINATED只表示本地活动终止，不保证AOPS外部请求回滚。进程失联或SQL已发出却未确认结果时，tec01保留已确认事实，未知部分等待人工核实，不把终止成功解释为从未执行。

## 5. 幂等、重试与异常闭环

| 回报 | X-Request-Id |
|---|---|
| PROGRESS | `{knowledgeId}:{requestId}:{seq}` |
| FINISHED | `{knowledgeId}:{requestId}:finished` |
| NODE_STARTED | `{attemptId}:started` |
| NODE_COMPLETED | `{attemptId}:completed` |
| RUN_RELEASED | `{dispatchId}:released` |

重试保持原正文、seq、occurredAt及幂等键；只重发消息，不重新执行SQL或LLM。

| 条件 | 当前处理 |
|---|---|
| 连接异常、超时、5xx | 最多六次请求，额外退避1/2/4/8/16秒 |
| 4xx，包括旧调度409 | 不自动重试 |
| 202/204、非JSON、业务status不合法、缺少或不匹配runId | 不自动重试无效确认，不放行后续执行 |
| 单条运行回报 | 尝试次数可配置，默认6；总等待默认70秒，含HTTP及退避，先到即停止 |
| 单条提取回报 | 当前固定最多6次，不套用运行回报70秒总等待 |
| 单次HTTP超时 | 默认20秒，实际封顶30秒 |

错误响应正文可保留code/message/retryable，但执行器按HTTP类别确定网络重试，不能用正文retryable覆盖4xx/5xx规则。HTTP200本身不够，必须通过相应确认校验。

### 运行失联必须处理的三个位置

| 失联位置 | 已知事实与tec01处理 |
|---|---|
| NODE_STARTED没有确认 | 执行器尚未进入该节点操作；不得把它显示为已执行成功 |
| SQL完成后NODE_COMPLETED没有确认 | tec01可能保存但响应丢失，也可能没保存；核查账本及AOPS审计，不自动重放 |
| RUN_RELEASED没有确认或进程崩溃 | 已保存节点事实仍有效；核查原实例活动，不能永久卡在运行中或重置成功节点 |

一旦运行回报耗尽或确认无效，执行器停止并释放，不再启动另一轮“报告刚才回报失败”的远程重试，也没有退出后结果补发存储。tec01负责在必要时标记UNKNOWN并提示用户核实；不能依赖总会收到UNKNOWN或RUN_RELEASED。

活动查询只说明当前执行器实例是否仍活动；active=false不代表成功或未执行。保留受理返回的executorInstanceId并核对原实例，服务重启后的新实例不能证明旧进程产生过什么外部效果。未知节点不能直接作为RUNNING/UNKNOWN重新下发；先核实并修正事实，再决定恢复。

## 6. 页面、Hermes与配置交付

- tec01提交事务后再推送页面SSE和Hermes；两端展示同一份已保存状态，推送失败不回滚保存确认，可通过事件重连补读。
- 分析中显示阶段及等待时间，不要求定时心跳；查询中显示当前节点，不伪造进度。节点失败展示诊断，不只展示NON_ZERO_EXIT。
- 待人工输入时创建表单/消息待办；用户确认输入后再恢复。暂停取消按钮显示请求中，收到最终事实或完成失联核查后再更新。
- 日志的“第1/6次尝试”是该消息首次发送，不是阶段第一次执行；现在五个阶段各一次，原始开始/结束日志仍可能成对显示。
- 生产资料与凭据不得写入普通调用日志；Key不放Workflow、回报或浏览器持久数据。SQL结果属于需鉴权的业务数据，由tec01按自身权限策略保存。

执行器service.env相关配置：

```dotenv
TEC01_BASE_URL=http://tec01:3421
TEC01_RUN_CALLBACK_PATH=/internal/v1/runs/{runId}/callbacks
TEC01_EXTRACTION_CALLBACK_PATH=/internal/v1/extractions/{knowledgeId}/callbacks
TEC01_TIMEOUT_SECONDS=20
TEC01_CALLBACK_MAX_ATTEMPTS=6
TEC01_CALLBACK_WAIT_SECONDS=70
```

另外按环境安全注入TEC01_SERVICE_TOKEN和RUNTIME_SERVICE_TOKEN，不把实际值放进文档或日志。既有配置不自动覆盖；安装升级后重启并在只读运行配置页核对生效值。取消心跳不新增开关，不修改LLM_TIMEOUT_SECONDS。

## 7. 双方联调验收

1. 正常提取按五阶段推进，长时间分析只出现一次分析进度，最终成功或失败正常回报。
2. 多条操作仅回报一次参数化阶段，去重与过滤统计完整保留在diagnostics；进度缺号仍按最大seq更新。
3. 同请求重复提交不重复调用LLM；终态补发幂等；新请求与旧请求迟到进度、终态相互隔离。
4. 两步SQL及A到B结果绑定正常；每节点开始和结果等待AopsResult确认，不要求saved；错误业务status或runId立即停止。
5. 条件路线只执行选中分支；HITL能通过页面和Hermes收集values，重新下发后继续。
6. 当前调度重复回报返回相同确认；旧调度409且不继续；5xx/超时有限重试，4xx不重试。
7. 回报总等待停止、不保证六次全部发生；重试只重发回报，不产生重复SQL或LLM调用。
8. 暂停在安全边界生效，取消不宣称外部回滚；失败、HITL、暂停均释放活动，可按确认事实恢复。
9. 人为断开结果确认、丢失退出通知或杀进程：tec01保留成功事实，未知部分人工核查，不等待不存在的可靠补发。
10. 未开启同步的Studio仍可独立提取和执行，不访问tec01；开启同步后先在tec01建立对应知识、请求和运行。

本次按确认文档改为每阶段一次进度、TERMINATED对外状态和AopsResult确认。提取requestId隔离、原幂等键重试、409停止及异常释放机制保留；已通过本地协议模拟测试，tec01数据库及真实联调仍需对端验收。
