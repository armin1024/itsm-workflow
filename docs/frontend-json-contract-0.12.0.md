# 前端接入：Workflow JSON约束与交互设计指南

若需要逐一理解定义中的每个字段，而不是接口或交互流程，请优先阅读[Workflow定义逐字段解释](workflow-definition-field-reference-0.12.0.md)，其中包含完整字段表和全图逐字段注释。

适用版本：**itsm-workflow 0.12.0**。适用对象：tec01前端、后端及其他工作流编排客户端。本文描述当前已经实现的契约，交互建议不代表执行器新增了相应接口或能力。

配套交付：

- [完整机器可读Schema](json-schema-0.12.0.json)：当前服务实时生成的快照，含三个请求Schema及四类节点配置、输入、输出Schema。
- [完整画布示例](examples/frontend-workflow-0.12.0.json)：SQL读→行数判断→人工输入→SQL读→结束，另含无记录分支及画布坐标。
- [下游完整校验说明](json-validation-contract.md)：字段规则、发布校验、执行下发和提取约束。
- [回报协议](tec01-to-itsm-workflow-api.md)：保存确认、回报重试及提取requestId隔离。

## 1. 前端要区分的四份数据

| 数据 | 作用 | 前端处理 |
|---|---|---|
| Catalog与Schema | 当前支持什么节点、配置和类型 | 构造节点选择器和编辑表单，按类型与版本缓存 |
| workflowDefinition | 节点、连线、参数来源、布局 | 编辑画布；不在其中存用户凭据或运行结果 |
| 运行参数与执行计划 | 当前工单、用户本次输入、待确认内容 | 展示动态输入框和完整计划，由tec01保存、确认 |
| 节点结果与人工待办 | 本次实际执行状态、输出与用户回复 | 从tec01读取；不直接改写成Workflow定义 |

**连线和绑定是两种关系。** A连到B，只表示B在A后执行；B需要A的结果时，还必须在B的inputs中配置NODE_OUTPUT。仅配置绑定也不会自动生成控制连线。

生产前端通过**tec01后端**获取Schema、调用执行器和保存数据；不要把RUNTIME_SERVICE_TOKEN或TEC01_SERVICE_TOKEN下发到浏览器。AOPS_API_KEY不能放进Workflow、运行参数、URL或浏览器持久化记录。

## 2. 获取约束及校验分层

以下为tec01后端调用执行器的接口，前端不应携带服务令牌直连：

```http
GET /internal/v1/runtime/catalog
GET /internal/v1/runtime/schemas
GET /internal/v1/runtime/schemas/workflowDefinition
Authorization: Bearer <RUNTIME_SERVICE_TOKEN>
```

Schema集合结构：

```text
runtimeVersion
jsonSchemaDialect
schemas.workflowDefinition
schemas.dispatch
schemas.extraction
nodes[]
  type + schemaVersion
  configSchema / inputSchema / outputSchema
  uiSchema / inputTypes / approvalPolicy / handlerVersion
```

集合不是一个Workflow Schema，验证定义时应取`schemas.workflowDefinition`。保留完整`$defs/$ref`，节点config另取对应`nodes[].configSchema`校验。Catalog还返回名称、分类、目录摘要及UI提示，可用于节点卡片；`uiSchema.editor`是提示，不是可下载代码。

使用支持JSON Schema Draft 2020-12的本地校验器；禁止为解析Schema而访问外网。不要自动把字符串数字/布尔值转成合法输入，也不要让本地校验器静默补默认值或删除未知字段。

三层检查：

1. **编辑即时检查**：必填、类型、长度、重复名称、连线和绑定的明显错误。
2. **本地Schema检查**：Workflow结构、每类节点config，以及提交表单的真实值类型。
3. **执行器语义校验**：发布前调用validate，检查图、SQL、参数占位符和引用，保存返回的规范化定义及哈希。

编辑中允许暂时存在未配置或未连线卡片，不要每敲一个字就请求远端validate。用户点击“校验/提交/发布”再检查完整定义。执行器的DRAFT校验也要求有效图，不能把它理解成保存任意残缺画布的接口；草稿持久化由tec01自己的业务接口负责。

## 3. 顶层、节点与连线字段

### Workflow

| 字段 | 约束 | 推荐交互 |
|---|---|---|
| schemaVersion | 1或2，输入默认1、规范化为2 | 新建固定2，不让用户填写 |
| entryNodeId | 必填，必须对应已有节点 | 画布标记唯一入口，提供“设为入口” |
| nodes | 必填，1～500个节点 | 节点卡片集合 |
| edges | 默认[]，最多1000条 | 画布连线集合 |
| catalogDigest | 可空，执行器规范化生成 | 隐藏技术字段，不手工计算 |

### 节点公共属性

| 字段 | 当前约束 | 推荐交互 |
|---|---|---|
| id | 唯一，`^[A-Za-z][A-Za-z0-9_-]{0,119}$` | 自动生成稳定ID，用户改标题不改ID |
| type | sql_read / condition / hitl_form / end | 从Catalog选，不允许自由输入类型 |
| schemaVersion | 默认1，当前均为1 | 由Catalog填充，不与Workflow版本混淆 |
| handlerVersion | 可空，最长40；规范化补齐当前版本 | 技术详情只读展示 |
| title | 必填，1～200字符 | 卡片标题输入框 |
| config | 默认{}，按节点configSchema验证 | 侧栏表单，不要求用户写JSON |
| inputs | 默认[]，同节点name不能重复 | 参数列表，分别配置类型、说明和来源 |
| approvalPolicy | NONE / PLAN / NODE，默认PLAN | SQL使用PLAN，其他节点建议NONE |
| timeoutSeconds | 1～7200，默认600 | 数字框，高级配置 |
| uiPosition | 可空，字典值为数字 | 保存`{"x":80,"y":120}`，移动卡片更新 |

NODE只是当前定义中的策略声明，**不等于执行器自动提供了独立审批节点**。前端不要据此设计已实现的节点审批功能。正式用户确认由tec01完成；当前人在回路节点只有hitl_form。

### 连线

`id/source/target`必填；kind为NORMAL或CONDITION，默认NORMAL；label默认空；condition可空；default默认false。

画布限制：

- 节点不可自连，起止节点必须存在；不支持循环、普通节点并行扇出、不可达节点。
- 普通节点最多一条出边；condition使用多条条件出边，并且恰好一条默认边。
- 每条非默认条件出边必须有规则；默认边建议`condition:null, default:true`。
- 条件出边按edges数组顺序匹配，第一条命中即采用。UI需要保留优先级，调整顺序要同步数组顺序。
- 推荐普通边用NORMAL、条件边用CONDITION；路由实际依据源节点类型、规则及default，不是仅看kind。
- UI必须生成唯一边ID，执行恢复通过边ID识别分支。当前公共Schema未完整表达边ID唯一性，不要只依赖Schema。
- 删除节点前提示将删除的连线及被引用的参数；若被后续节点绑定，先修正绑定，不能悄悄把它改成运行参数。
- 建议显式使用end且不连出边；当前后端也允许其他类型无出边结束，不能把这个行为误当成所有节点都必须连到同一个end。

## 4. 三种参数来源与数据选择器

每个输入的`name`为1～80字符，description最多500字符，type允许string/number/integer/boolean/object/array，required默认true。

```json
{
  "name":"customer_id",
  "type":"string",
  "description":"用户确认的客户编号",
  "required":true,
  "source":{"kind":"NODE_OUTPUT","nodeId":"input-1","jsonPointer":"/values/customer_id"}
}
```

| 来源 | JSON | 推荐控件 |
|---|---|---|
| RUN_INPUT | `{"kind":"RUN_INPUT","key":"customerName"}` | “运行时填写”，编辑参数key及类型 |
| NODE_OUTPUT | `{"kind":"NODE_OUTPUT","nodeId":"sql-1","jsonPointer":"/data/0/id"}` | 选前置节点，再用结果树选字段 |
| LITERAL | `{"kind":"LITERAL","value":20}` | 按type渲染固定值输入框 |

切换来源时只发送该来源所需字段，避免遗留旧nodeId/key/value。字符串、数字、布尔值均须发送真实JSON类型。编号、手机号等有前导零的数据应使用string，而不是number。

`NODE_OUTPUT.jsonPointer`相对于**来源节点输出对象**：

| 需要的数据 | Pointer |
|---|---|
| SQL行数 | `/rowCount` |
| SQL全部数据行 | `/data` |
| SQL第一行客户编号 | `/data/0/id` |
| 人工输入客户编号 | `/values/customer_id` |

数组索引从0开始；字段名的`/`和`~`分别转义为`~1`和`~0`。输入绑定当前要求以`/`开头，不能用空路径绑定整个根对象。UI推荐加强检查非法转义及负数组索引，不依赖后端宽松行为。

来源选择器只列前置可达节点。后端目前检查“来源节点存在且可到达当前节点”，这**不保证它在所有分支都执行过**；UI要警告跨互斥分支的必填绑定，并优先展示当前路径上确定可用的输出。

SQL的data行结构是运行期产生的，静态outputSchema不能预知所有列名。结果树可以使用已授权的真实运行样例辅助选字段，但样例值不能写回定义。无样例时允许高级Pointer输入，并提示“执行时才能确认该字段存在”。

**不要默认选第一行。** 多行查询需要人判断时，设计HITL表单供用户填值，不自动把`/data/0/id`解释成唯一正确答案。

## 5. 四种节点的表单设计

### SQL读 sql_read

config仅允许两个字段：

```json
{"databaseRef":"124/aops-t/aops-t/readonly/aops-t","sqlTemplate":"SELECT id FROM users WHERE name={{customer_name}}"}
```

- databaseRef必填、非空、最多1000字符。作为不透明完整字符串编辑，不自行假定固定分段校验。
- sqlTemplate必填；只能单条安全只读SQL，不能靠SELECT前缀代替执行器AST检查。
- 占位符匹配`{{parameter_name}}`，名称为`[A-Za-z_][A-Za-z0-9_]*`。占位符集合必须与该节点inputs[].name集合一致，重复使用同名占位符允许。
- 推荐SQL编辑器识别占位符后提示新增/移除参数；改名或删除前确认已有绑定，不自动覆盖。
- SQL参数仅支持string/number/integer/boolean，不能绑定整行对象、数组或任意SQL片段。
- 不要在字符串占位符外加引号，执行器按类型转义。例如`name={{customer_name}}`，不要写`name='{{customer_name}}'`。
- SQL中有占位符就需要执行时提供值，UI默认设为必填；即使required=false，也不代表缺值时执行器能渲染该SQL。
- 输出形状为`{"status":0,"data":[{"id":"C001"}],"rowCount":1}`，CLI的SSE由执行器解析，前端消费JSON。

### 条件判断 condition

节点config建议{}、inputs建议[]。规则存于它的**出边condition**，不要另造config.rule作为完整流程路由配置。

```json
{"path":"/nodes/sql-1/output/rowCount","op":"gt","value":0}
```

条件路径相对于运行状态，包含`/nodes/<id>/output`；和输入绑定Pointer不是同一种根：

```text
输入绑定：选sql-1 + /rowCount
条件判断：/nodes/sql-1/output/rowCount
运行参数：/inputs/customerName
```

| 操作符 | UI含义 | value |
|---|---|---|
| eq / ne | 等于 / 不等于 | 与目标字段类型一致 |
| gt / gte / lt / lte | 大于 / 大于等于 / 小于 / 小于等于 | 行数等场景使用数字 |
| in | 实际值属于集合 | 数组 |
| contains | 实际集合或字符串包含 | 被包含的值 |
| exists / empty | 存在 / 为空 | 不需要，隐藏值输入框 |

支持all/any非空数组，以及not单个对象；同一层只使用一种组合键。建议以“全部满足/任一满足/不满足”规则组编辑器呈现，默认使用简单规则，复杂嵌套放高级项。

缺失路径、类型不兼容的具体结果按操作符而定，不能假设所有比较都会抛错或都会返回false；例如empty可用于缺失值。UI应优先使用exists/empty判断可用性，并保持比较类型一致，不依赖隐式转换。

### 人工输入 hitl_form

config的title和fields必填；fields为1～50项，每项必须有name、label、type、required。

```json
{
  "title":"填写后续查询参数",
  "description":"请参考查询结果填写客户编号",
  "fields":[
    {"name":"customer_id","label":"客户编号","type":"string","required":true,"minLength":1,"maxLength":64},
    {"name":"query_limit","label":"查询条数","type":"integer","required":true,"minimum":1,"maximum":100}
  ]
}
```

字段名匹配`^[A-Za-z_][A-Za-z0-9_]*$`；类型为string/integer/number/boolean；可选description、minimum、maximum、minLength、maxLength和enum。

| 字段约束 | 推荐运行时控件 |
|---|---|
| string | 文本框，展示长度要求 |
| integer / number | 数字框，检查范围、有限数；integer必须为整数 |
| boolean | 必填时用明确的是/否单选，区分“未填写”和false |
| enum | 同类型的下拉/单选控件，保留原始JSON值类型 |

enum是**表单字段约束**，不是新增hitl_select节点。前端应保证字段name唯一、minimum≤maximum、minLength≤maxLength，以及enum与类型一致；这些跨字段规则未全部在当前后端Schema中表达。

把上游结果通过该节点inputs传入，例如`queryRows`绑定SQL的`/data`；执行器返回interaction.context.queryRows，tec01按权限展示参考表格。前端可以分页预览，建议每页100行，但不能修改或裁剪实际持久化结果来冒充完整数据。

到达节点返回WAITING_INPUT和HITL_FORM交互。用户填写多个字段并确认，tec01保存后重新下发：

```json
{"resumePayload":{"nodeId":"input-1","action":"SUBMIT","values":{"customer_id":"C001","query_limit":20}}}
```

只提交定义中的字段；optional字段未填写可省略，不自动填空字符串、null或0；false和0不等于未填写。输出固定为`{"values":{...}}`，下游绑定`/values/<字段名>`。若可选字段被后续必填SQL参数引用，UI应提示配置冲突。

回复提交前应在页面完成字段校验并保留用户输入；执行器收到不合法的resumePayload时可能将该次节点执行记为FAILED，不能假设它会自动留在WAITING_INPUT等待重填。

### 结束 end

建议config:{}、inputs:[]、approvalPolicy:NONE，无出边。当前只有以上四类节点；不支持LLM节点、独立选择节点、任意脚本或任意命令。

## 6. 从定义生成运行输入框

不要把所有inputs都渲染为创建运行时的输入框：

1. 只取source.kind=RUN_INPUT的项，按source.key聚合；一个key显示一次，并列出引用节点。
2. 同key类型必须一致；任一引用required=true则整体必填。description作为说明，不用参数示例中的历史实际值作默认值。
3. NODE_OUTPUT执行到节点时解析，不要求创建计划时填写；HITL字段到达人工节点时才询问。
4. LITERAL属于流程定义，不作为用户本次运行参数重复填写。
5. 传parameters/runInputs时保留JSON类型，不把所有输入都序列化成字符串。

工单ticketId为必填正整数，表单独立展示；JS前端应拒绝无法无损表示的整数，不悄悄舍入。comment由执行器固定生成`#uatu-<ticketId>`，不是另一个运行参数。

## 7. 发布校验、计划确认和错误展示

发布前由tec01后端调用：

```http
POST /internal/v1/runtime/workflows/validate
Authorization: Bearer <RUNTIME_SERVICE_TOKEN>
Content-Type: application/json
```

```json
{"validationMode":"PUBLISH","workflowDefinition":{"schemaVersion":2,"entryNodeId":"done","nodes":[{"id":"done","type":"end","title":"完成","config":{},"inputs":[],"approvalPolicy":"NONE"}],"edges":[]}}
```

validationMode允许DRAFT/PUBLISH/EXECUTE/TEST，默认DRAFT。成功返回valid、normalizedDefinition、catalogDigest、workflowContentHash、requiredRuntimeVersion和warnings。发布保存**返回的规范化定义与哈希**，不要由前端自行补默认值、计算哈希或固定Handler版本。

targetCatalogDigest可检测目录变化，冲突返回409；刷新Catalog后重新检查，不能静默移除未知节点或覆盖配置。PUBLISH的Handler版本不一致可能仅返回warning，执行下发仍会拒绝不可用版本；UI需让审核者看见警告。

计划创建、保存、planHash与用户确认由tec01负责；执行器的workflows/plan是可选联调辅助，不替代生产确认。计划展示节点顺序、分支、数据库、SQL模板、输入来源、待运行时解析项及HITL等待点；确认后参数或定义变化必须重新确认。

错误分层展示：

| 场景 | 当前返回 | UI行为 |
|---|---|---|
| 本地结构错误 | 校验器字段路径 | 字段下方中文提示并定位卡片 |
| 请求结构错误 | HTTP422，REQUEST_VALIDATION_FAILED，details.fields[] | 按field路径映射表单，无法映射的放摘要区 |
| 图/SQL语义错误 | HTTP422，WORKFLOW_VALIDATION_FAILED，message | 保留编辑内容，显示摘要；按明确节点ID辅助定位 |
| Catalog/下发冲突 | HTTP409 | 提示刷新目录或运行事实，不直接重新执行 |
| 容量不足 | HTTP429及Retry-After | 显示繁忙，tec01按既有调度策略处理 |
| 已受理后的业务失败 | 节点/提取回报 | 在运行或提取详情展示，不能当成表单提交失败 |

内部接口结构错误示例：

```json
{"code":"REQUEST_VALIDATION_FAILED","message":"请求字段校验失败","retryable":false,"details":{"fields":[{"field":"body.ticketId","message":"Input should be greater than 0"}]}}
```

Studio接口错误外层通常是`{"detail":...}`，不要与内部接口的扁平code/message混淆。语义错误目前未统一返回可机器定位的nodeId/fieldPath，**不要假设message是稳定可解析协议**；精确定位以本地校验结果为主，后端message作为安全补充。

Schema或validate成功不证明数据库路径可用、权限正确或列存在；不能仅因通过校验就在UI显示“生产验证成功”。

## 8. 执行与恢复JSON边界

完整下发按schemas.dispatch校验，必填dispatchId/runId/ticketId/workflowContentHash/workflowSnapshot。请求ID和运行ID为1～160字符且匹配`^[A-Za-z0-9_.:-]+$`；其他恢复字段由tec01填充，不应暴露为任意JSON编辑框让普通用户修改。

- nodeStates/nodeOutputs/selectedRoutes只能来自已保存事实；不得由前端把UNKNOWN改成PENDING来绕过核实。
- 下发RUNNING/UNKNOWN会被拒绝；已完成节点不自动重放，CANCELLED不自动重执行。
- WAITING_INPUT提交人工回复，PAUSED由tec01重新下发继续；失败重试也使用新的dispatchId。
- 已完成condition必须保留选中边，后续绑定需要的前置输出也必须保留。
- 暂停/取消按钮先显示“请求中”，命令accepted=true不是最终状态；最终状态读tec01事件。
- UI与Hermes使用同一份tec01事实和人工待办，避免前端自造第二套运行状态。

当前Schema集合只包含workflowDefinition、dispatch、extraction，不包含回报、所有REST响应或单节点执行Schema。不要据此自行暴露不存在的接口。

## 9. 示例与前端验收

完整示例见配套JSON。图形含义：

```text
查询候选 → 行数 > 0？ ──有记录──→ 人工填写客户编号和条数 → 查询详情 → 完成
                         └─否则──→ 未查到客户，结束
```

示例数据库路径是演示值；先调用validate确认结构，配置真实环境路径、完成授权和用户确认后才能用于实际执行。示例不包含运行结果或凭据。

前端至少验收：四类卡片、自动ID及布局保存、删节点引用提示、连线防环、条件默认边及优先级、三种参数来源、数组和转义Pointer、跨分支绑定警告、多行结果不自动选第一行、HITL多字段及false/0、必填/类型/范围、RUN_INPUT聚合、警告与错误定位、未知节点只读保护、参数变化重新确认、刷新后从tec01恢复真实状态。
