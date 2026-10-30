# Workflow定义逐字段解释

适用版本：**itsm-workflow 0.12.0**。本文只解释`workflowDefinition`这个JSON对象本身，不讲接口调用步骤。它在执行下发时放在`workflowSnapshot`字段内，二者承载的是同一类固定流程定义。

阅读约定：“必填”指调用方是否需要显式填写；“默认值”指执行器读取并规范化时补齐的值，不表示任意JSON Schema校验器会自动补值。以下“建议”是前端编排约束，不等于当前后端已经逐项拒绝不符合建议的数据。

## 1. 先理解整份定义

一份Workflow定义回答五个问题：

1. 从哪里开始：`entryNodeId`。
2. 有哪些操作：`nodes`。
3. 接下来执行哪一个：`edges`。
4. 每个操作需要什么值、从哪取：`nodes[].inputs`。
5. 分支如何选择：条件节点出边的`condition/default`。

`nodes`数组中的位置不是执行顺序，画布上的左右位置也不是执行顺序；**执行顺序由入口和连线决定**。只有条件节点的多条出边，才按`edges`数组中的顺序表达匹配优先级。

控制关系和数据关系分开：A→B的边让B在A之后执行；B的`NODE_OUTPUT`绑定才表示“从A结果中取一个参数”。只有其中之一不够。

## 2. 顶层字段：全部5个

| 字段 | 类型 | 必填/默认 | 含义及使用方式 |
|---|---|---|---|
| `schemaVersion` | integer | 否，默认1 | **整份流程定义的结构版本**，当前允许1或2，规范化后写成2。新建建议填2，不是经验发布版本号 |
| `catalogDigest` | string/null | 否，默认null | **节点目录指纹**，说明定义对应哪份Node Registry目录。普通编辑时交由执行器生成，不让用户手工编造 |
| `entryNodeId` | string | 是 | **入口节点ID**，必须与某个`nodes[].id`完全相同，首次执行从这里开始 |
| `nodes` | object[] | 是，1～500项 | **节点实例集合**；同一种节点类型可以创建多个实例，每个实例有自己的id、配置和输入 |
| `edges` | object[] | 否，默认[]，最多1000项 | **控制连线集合**，定义先后顺序和条件分支；只有一个结束节点的流程可以没有边 |

`catalogDigest`在DRAFT/PUBLISH等规范化中使用当前目录摘要；EXECUTE模式可保留定义已有的摘要。它不包含本次运行参数，不是`workflowContentHash`，也不是tec01的`planHash`。

### 版本字段不要混淆

| 位置 | 示例 | 意义 |
|---|---|---|
| 顶层schemaVersion | 2 | Workflow结构版本 |
| 节点schemaVersion | 1 | 某种节点配置的结构版本 |
| 节点handlerVersion | 1.0.0 | 节点执行实现版本 |
| 外部runtimeVersion | 0.12.0 | 执行器程序版本，不属于Workflow顶层 |
| 外部workflowVersionId | wfv_1 | tec01发布版本ID，不属于Workflow顶层 |

## 3. nodes[]公共字段：全部10个

| 字段 | 类型 | 必填/默认 | 含义及影响 |
|---|---|---|---|
| `id` | string | 是 | **这个节点实例的稳定ID**。全图唯一，格式`^[A-Za-z][A-Za-z0-9_-]{0,119}$`。连线、参数绑定、条件路径都可能引用它；改标题不应改ID |
| `type` | string | 是，1～80字符 | **节点类型**。当前为sql_read、condition、hitl_form、end，必须在Registry中注册；不能填写任意命令名 |
| `schemaVersion` | integer | 否，默认1，至少1 | **该类型配置的版本**。当前四类均为1；type相同但schemaVersion不同也不能直接当成同一种配置 |
| `handlerVersion` | string/null | 否，默认null，最多40字符 | **执行实现版本**。未填时由规范化补齐，当前为1.0.0；实际执行要求该实现可用 |
| `title` | string | 是，1～200字符 | **卡片显示标题**。例如“查询客户候选”，不参与节点引用定位 |
| `config` | object | 否，默认{} | **该节点做什么**，结构随type变化；SQL放库路径与模板，人工节点放字段定义 |
| `inputs` | object[] | 否，默认[] | **该节点拿到什么输入**，每个参数单独声明名称、类型和来源，不是查询结果 |
| `approvalPolicy` | string | 否，默认PLAN | **确认策略声明**，允许NONE/PLAN/NODE，详见下表；不是用户已经批准的记录 |
| `timeoutSeconds` | integer | 否，默认600；1～7200 | **节点操作的超时配置**。当前SQL Handler使用此值控制CLI执行，不控制工单提取LLM，也不是整个Workflow总时限 |
| `uiPosition` | object/null | 否，默认null | **画布布局**，推荐{x:80,y:120}。不决定执行顺序、不表示运行状态 |

### approvalPolicy每个值

| 值 | 意义 | 当前实现边界 |
|---|---|---|
| NONE | 不声明额外确认要求 | 常用于condition、hitl_form、end；不绕过tec01整体权限 |
| PLAN | 执行前确认整体计划 | 常用于SQL；tec01负责保存计划和用户确认 |
| NODE | 声明节点级确认意图 | 当前会进入风险计划信息，**不保证执行器自动产生节点审批中断**，不能仅设置它就认为有完整审批功能 |

公共模型默认PLAN，不能假设切换type后执行器一定自动改成NONE；前端新建卡片时可参考Catalog的推荐策略。

### uiPosition里面的字段

| 字段 | 类型 | 说明 |
|---|---|---|
| x | number | 画布横坐标，由前端绘图库使用 |
| y | number | 画布纵坐标，由前端绘图库使用 |

当前模型接受数值字典，未强制只允许x/y；前端建议只发送这两个布局键，不把选中状态、宽高或临时组件对象混入。即使只是移动位置，也会改变定义内容；发布后保存与确认应使用对应的新规范化定义和哈希。

## 4. inputs[]字段：全部5个

| 字段 | 类型 | 必填/默认 | 意义 |
|---|---|---|---|
| `name` | string | 是，1～80字符 | **当前节点内部使用的参数名**。SQL占位符引用这个名字；同一节点不能重复 |
| `type` | string | 否，默认string | **取到的真实值类型**，不是来源类型；允许string/number/integer/boolean/object/array |
| `description` | string | 否，默认空串，最多500字符 | **参数说明**，用于给人理解和填写，不应该包含历史工单的实际值 |
| `source` | object | 是 | **取值规则**，决定从运行输入、前置输出还是固定值获得参数 |
| `required` | boolean | 否，默认true | **运行到本节点时是否必须能取到此参数**。false不代表SQL模板缺值就可以渲染，也不代表null/错误类型一定被接受 |

### type每个值

| type | 正确值示例 | 说明 |
|---|---|---|
| string | `"C001"` | 字符串；手机号、客户号及有前导零的标识建议使用此类型 |
| number | `12.5` | 有限数字，不是字符串`"12.5"` |
| integer | `20` | 整数，不应接受小数或布尔值 |
| boolean | `true` | 布尔值，不是`"true"`；false是有效值 |
| object | `{"id":"C001"}` | JSON对象；SQL参数不允许此类型 |
| array | `[{"id":"C001"}]` | JSON数组；可用于HITL参考数据，SQL参数不允许此类型 |

例如SQL占位符叫`customer_name`，本次运行的字段叫`customerName`，两者可以不同：

```json
{"name":"customer_name","type":"string","description":"客户姓名","required":true,"source":{"kind":"RUN_INPUT","key":"customerName"}}
```

意思是“从本次runInputs.customerName取值，在这个SQL节点里命名为customer_name”。

## 5. inputs[].source字段：全部5个

| 字段 | 类型 | 默认 | 意义 |
|---|---|---|---|
| `kind` | string | 必填 | **来源类别**：RUN_INPUT、NODE_OUTPUT或LITERAL |
| `key` | string/null | null | RUN_INPUT时，运行输入对象中的字段名 |
| `nodeId` | string/null | null | NODE_OUTPUT时，**提供结果的前置节点ID**，不是当前节点ID |
| `jsonPointer` | string/null | null | NODE_OUTPUT时，从来源输出中取值的路径 |
| `value` | 任意JSON值 | null | LITERAL时，固定值本身；类型应与inputs.type一致 |

### 哪种来源需要哪些字段

| kind | 需要填写 | 不应拿来替代它的字段 |
|---|---|---|
| RUN_INPUT | 非空key | 不用nodeId/jsonPointer/value表示运行参数 |
| NODE_OUTPUT | 已存在的前置nodeId、以`/`开头的jsonPointer | 不用key作为结果路径 |
| LITERAL | 明确value | 不从运行上下文或其他节点猜值 |

切换来源后，推荐移除不适用字段；规范化JSON可能把这些可空字段补成null，含义仍由kind决定。LITERAL缺省value会成为null，但不等于该输入类型接受null。

三种完整示例：

```json
{"kind":"RUN_INPUT","key":"customerName"}
```

```json
{"kind":"NODE_OUTPUT","nodeId":"sql-1","jsonPointer":"/data/0/id"}
```

```json
{"kind":"LITERAL","value":20}
```

### jsonPointer怎么理解

假设sql-1的输出为：

```json
{"status":0,"rowCount":2,"data":[{"id":"C001","name":"演示客户甲"},{"id":"C002","name":"演示客户乙"}]}
```

| jsonPointer | 取到的值 |
|---|---|
| /rowCount | 数字2 |
| /data | 两行数据组成的数组 |
| /data/0/id | 字符串C001 |
| /data/1/name | 字符串演示客户乙 |

路径相对于该来源节点的输出，**不要加`/nodes/sql-1/output`前缀**。数组从0开始，字段名里的`/`写`~1`、`~`写`~0`。当前输入绑定不允许空Pointer表示整个根对象。前端应禁止非法转义或负索引，不依赖宽松解析行为。

不要求只能引用紧邻上一步，可以引用更早的前置节点；但来源必须在实际执行路径中已经成功产生输出。跨互斥分支绑定即使图上可达，也可能运行时缺值。多行结果不应默认选第0行作为业务正确答案，必要时通过HITL让用户填写。

## 6. sql_read.config：全部2个字段

| 字段 | 类型 | 必填/约束 | 含义 |
|---|---|---|---|
| `databaseRef` | string | 是，非空，最多1000字符 | **aops-cli的目标数据库路径**，执行时用于--db；按完整字符串处理，不是连接密码或任意URL |
| `sqlTemplate` | string | 是，非空 | **SQL模板**，运行到节点时用inputs值渲染，随后执行单条安全只读SQL |

此config不接受额外字段。示例：

```json
{"databaseRef":"124/aops-t/aops-t/readonly/aops-t","sqlTemplate":"SELECT id FROM users WHERE name={{customer_name}} LIMIT {{query_limit}}"}
```

- `{{customer_name}}`、`{{query_limit}}`引用本节点inputs的name，不直接引用RUN_INPUT.key。
- 占位符格式为`{{[A-Za-z_][A-Za-z0-9_]*}}`，其名称集合必须与inputs.name集合完全一致；同名占位符可重复使用。
- 参数表示值，不表示表名、列名或任意SQL片段。字符串占位符外不要另加引号，执行器按类型转义。
- config里没有ticketId、comment或AOPS_API_KEY。工单ID属于本次运行，comment由执行器生成，凭据走受控请求头。
- inputs只允许string/number/integer/boolean。声明required=false不代表模板中可以省略该参数。
- 输出的status/data/rowCount不是定义字段；后续节点可通过NODE_OUTPUT引用这些实际运行结果。

## 7. hitl_form.config：全部3个字段

| 字段 | 类型 | 必填/约束 | 含义 |
|---|---|---|---|
| `title` | string | 是，非空 | **到达节点时给用户展示的表单标题**；不同于nodes[].title的画布卡片标题 |
| `description` | string | 否，未填时表单运行展示为空串 | **用户操作说明**，告诉用户如何参考数据填写 |
| `fields` | object[] | 是，1～50项 | **用户需要填写的字段定义**；不是提前填好的答案 |

此config只定义以上三个字段，不应把用户提交值存入config。前置参考数据使用节点inputs绑定，运行时形成interaction.context。

### fields[]：全部10个已声明字段

| 字段 | 类型 | 必填/默认 | 含义 |
|---|---|---|---|
| `name` | string | 是 | **用户回复中的键名**，格式`^[A-Za-z_][A-Za-z0-9_]*$`；表单执行时要求唯一，前端也应提前检查 |
| `label` | string | 是 | **用户看到的字段名称**，例如“客户编号”；不参与参数引用 |
| `type` | string | 是 | string/integer/number/boolean，决定用户回复值类型 |
| `required` | boolean | 是 | 是否必须提交该键；false不是空值，0也不是空值 |
| `description` | string | 否，无额外默认值 | 字段的填写帮助文案 |
| `minimum` | number | 否 | 数值最小值，包含边界 |
| `maximum` | number | 否 | 数值最大值，包含边界 |
| `minLength` | integer | 否，至少0 | 字符串最短长度，包含边界 |
| `maxLength` | integer | 否，至少1 | 字符串最长长度，包含边界 |
| `enum` | array | 否 | 允许的值集合；保持与type一致，不把数字枚举变成字符串 |

没有自动答案default字段，也没有当前实现会消费的placeholder字段。

例如：

```json
{"name":"query_limit","label":"最多返回条数","type":"integer","required":true,"description":"请输入1到100之间的整数","minimum":1,"maximum":100}
```

意思是：页面显示“最多返回条数”，用户提交的键叫query_limit，值必须是整数且在1～100之间。

字段name唯一、最小值不大于最大值、enum符合type，建议前端显式检查；这些关系没有全部在当前发布校验中表达。required只要求键存在，string若还要求非空，应使用minLength:1。

### 人工输入如何传给后续节点

假设用户提交：

```json
{"customer_id":"C001","query_limit":20}
```

HITL节点成功输出：

```json
{"values":{"customer_id":"C001","query_limit":20}}
```

下一个SQL节点分别绑定source.nodeId=input-1、jsonPointer=/values/customer_id，以及/values/query_limit。用户答案属于运行数据，不写回fields定义。

enum可在页面呈现下拉框，它仍然是hitl_form的字段约束；不是独立hitl_select节点。HITL等待用户时会释放本次活动执行，timeoutSeconds不是让用户必须在600秒内填完表单的计时器。

## 8. condition.config与end.config

| 节点 | config | 含义 |
|---|---|---|
| condition | 建议{} | 完整Workflow的路由规则放在**出边condition**，不是config.rule |
| end | 建议{} | 当前没有必须配置的专属业务字段，表示完成当前路径 |

这两类config的Schema允许额外属性，但不代表执行器会理解任意自定义字段。不要把width、颜色、运行状态、脚本或LLM提示词塞进去期待它们执行。

condition可无inputs，直接通过出边规则访问运行状态。end建议无inputs、无出边；当前引擎也允许其他节点无出边结束，前端建议显式添加end以消除歧义。

## 9. edges[]字段：全部7个

| 字段 | 类型 | 必填/默认 | 含义 |
|---|---|---|---|
| `id` | string | 是 | **这条连线的稳定ID**。前端应保证唯一；条件执行和恢复会记录选中了哪条边 |
| `source` | string | 是 | **起点节点ID**，必须对应nodes[].id；与inputs.source不是一回事 |
| `target` | string | 是 | **终点节点ID**，必须存在且不能等于source |
| `kind` | string | 否，默认NORMAL | NORMAL为普通连线，CONDITION为条件出边；实际路由还依据源节点type、condition和default |
| `label` | string | 否，默认空串 | **画布边上的显示文字**，例如“有记录”；不是可执行表达式 |
| `condition` | object/null | 否，默认null | **这条分支的判断规则**，条件节点非默认边必须有；普通边不要放规则期待它生效 |
| `default` | boolean | 否，默认false | **是否为兜底分支**；条件节点恰好一条为true，所有非默认分支都不满足时使用 |

普通节点最多一条出边；条件节点允许多条出边，但每次只选一条，不能并行扇出。图不能自连、不能有环，从入口必须能到达所有节点。

## 10. edges[].condition：原子条件全部3个字段

| 字段 | 类型 | 何时需要 | 含义 |
|---|---|---|---|
| `path` | string | 原子条件需要，以`/`开头 | **从运行状态哪里取待比较值** |
| `op` | string | 原子条件需要 | **比较方式**，见下表 |
| `value` | 任意JSON值 | 二元比较时需要；exists/empty不需要 | **和实际值比较的目标值**，不是参数来源，不会自动解析成变量 |

例子：

```json
{"path":"/nodes/sql-1/output/rowCount","op":"gt","value":0}
```

逐字段读法：从sql-1实际输出里找到rowCount，判断是否大于数字0。

### path的根是什么

规则运行时读取的状态形状：

```json
{"inputs":{"customerName":"演示客户"},"nodes":{"sql-1":{"output":{"status":0,"rowCount":2,"data":[{"id":"C001"},{"id":"C002"}]}}}}
```

| 条件path | 含义 |
|---|---|
| /inputs/customerName | 本次运行输入customerName |
| /nodes/sql-1/output/rowCount | sql-1返回的行数 |
| /nodes/input-1/output/values/customer_id | HITL节点产出的用户确认值 |

与输入绑定对比：输入绑定先指定nodeId，所以只写`/rowCount`；条件没有单独的来源nodeId，所以path要写完整的`/nodes/sql-1/output/rowCount`。

### op的全部10个值

| op | 含义 | 例子 |
|---|---|---|
| eq | 等于 | 状态等于字符串ACTIVE |
| ne | 不等于 | 状态不等于字符串DELETED |
| gt | 大于 | 行数>0 |
| gte | 大于等于 | 金额≥100 |
| lt | 小于 | 行数<10 |
| lte | 小于等于 | 行数≤100 |
| in | 实际值属于value集合 | value填["ACTIVE","PENDING"] |
| contains | 实际集合或字符串包含value | 实际数组包含某个元素 |
| exists | path指向的值存在 | 不需要value；存在但值为null仍算存在 |
| empty | 值缺失或为null、空串、空数组、空对象 | 不需要value；false和0不算空 |

不依赖隐式类型转换，行数用数字0，不用字符串"0"。缺失路径、比较类型不兼容的表现与操作符相关，不能假设所有情况都会抛异常或都返回false。当前eq/ne遵循运行实现的值比较规则，编排时应使用相同类型。

### 组合条件：全部3种字段形式

| 字段 | 类型 | 含义 |
|---|---|---|
| `all` | 非空条件数组 | 所有子条件都成立 |
| `any` | 非空条件数组 | 任一子条件成立 |
| `not` | 单个条件对象 | 对该子条件取反 |

同一层只选一种形式，不混写all/any/not；原子条件与组合条件也应分开构造。允许组合递归嵌套。

```json
{"all":[{"path":"/nodes/sql-1/output/rowCount","op":"gt","value":0},{"not":{"path":"/inputs/customerName","op":"empty"}}]}
```

含义是“查到记录，并且本次客户姓名不为空”。

### 多个分支怎么执行

按edges数组顺序检查当前条件节点的非默认边，第一条匹配即选择，不再执行其他分支；全部不匹配才走default:true的边。默认边不是“并行的另一路”，未命中的独占路径会被标记SKIPPED。前端分支排序必须反映到数组顺序。

## 11. 不属于Workflow定义的字段

下列字段属于经验、计划、运行或环境配置，**不要加到workflowDefinition顶层当作执行器支持的字段**：

| 字段 | 实际归属 |
|---|---|
| name、summary、matchPhrases、negativePhrases、systemKeys | 经验元数据 |
| creatorUid、uids、visibility | tec01权限和审计 |
| knowledgeId、workflowVersionId | tec01经验与固定版本标识 |
| runId、dispatchId、ticketId、runInputs | 本次运行/调度上下文 |
| nodeStates、nodeOutputs、selectedRoutes、resumePayload | 已保存运行事实和恢复输入 |
| status、diagnostic、interaction、attemptId | 运行状态、诊断和人工待办 |
| workflowContentHash、planHash、planMaterialHash | 定义或计划材料的内容校验 |
| AOPS_API_KEY、Authorization、service token | 凭据，不进入定义 |
| LLM_TIMEOUT_SECONDS | 服务端提取模型配置，不由节点timeoutSeconds控制 |

未知字段在部分模型中可能被忽略，不能把“没有报错”理解为字段已生效。也不能通过给JSON增加代码、节点类型或callbackUrl改变执行器能力。

## 12. 完整示例：每一处引用串起来读

本例是“查询客户→判断是否有记录→人工输入客户编号和条数→查询详情→结束”，没有记录走另一结束节点。数据库路径为演示值，不应直接用于生产执行。完整标准JSON见[示例文件](examples/frontend-workflow-0.12.0.json)。

| 定义位置 | 本例实际意义 |
|---|---|
| entryNodeId=sql-1 | 从查询候选节点开始 |
| sql-1.inputs.customer_name | 取本次运行的customerName，供SQL占位符customer_name使用 |
| e-query-check | 查询结束后进入check-1 |
| e-found.condition | sql-1.rowCount>0时进入人工输入 |
| e-empty.default=true | 没有命中正向条件时进入empty-end |
| input-1.inputs.queryRows | 把sql-1的/data数组作为用户参考数据，而不是默认选第一行 |
| input-1.config.fields | 要求用户填写customer_id字符串和query_limit整数 |
| sql-2.inputs | 从input-1的/values/customer_id和/values/query_limit取两个参数 |
| e-query-done | 第二条SQL完成后进入done |

下面是同一完整定义的逐字段注释版，采用JSONC；**注释仅用于解释，调用接口时使用标准JSON文件，不要直接发送带注释文本。**

```jsonc
{
  // 整份Workflow的结构版本，新建使用2
  "schemaVersion": 2,
  // 节点目录指纹，编辑时可为空，发布时由服务规范化生成
  "catalogDigest": null,
  // 入口节点，首次执行从sql-1开始
  "entryNodeId": "sql-1",
  // 节点实例集合；数组顺序不决定执行顺序
  "nodes": [
    {
      // 节点实例ID，供连线和数据引用使用
      "id": "sql-1",
      // 已注册节点类型，决定config和执行逻辑
      "type": "sql_read",
      // 该节点类型的配置结构版本，不是顶层流程版本
      "schemaVersion": 1,
      // 节点实现版本；编辑时也可省略，由规范化补齐
      "handlerVersion": "1.0.0",
      // 画布卡片标题，不用于节点定位
      "title": "查询客户候选",
      // 节点专属配置，按type解释
      "config": {
        // 目标数据库路径，作为完整字符串使用，不是连接密码
        "databaseRef": "124/aops-t/aops-t/readonly/aops-t",
        // 单条只读SQL模板，占位符与本节点inputs.name一一对应
        "sqlTemplate": "SELECT id, name FROM users WHERE name={{customer_name}}"
      },
      // 本节点需要的数据输入及其来源
      "inputs": [
        {
          // 当前节点内参数名，SQL占位符引用它
          "name": "customer_name",
          // 实际输入值类型，必须与所取结果匹配
          "type": "string",
          // 给人阅读的通用参数说明
          "description": "客户姓名",
          // 运行到此节点时是否必须取得该参数
          "required": true,
          // 取值来源，与控制连线的source不同
          "source": {
            // RUN_INPUT取运行输入；NODE_OUTPUT取前置结果；LITERAL取固定值
            "kind": "RUN_INPUT",
            // 运行输入对象的字段名，只有RUN_INPUT使用
            "key": "customerName"
          }
        }
      ],
      // 确认策略声明；实际用户确认由tec01负责
      "approvalPolicy": "PLAN",
      // 节点操作超时秒数；SQL使用此值，不是HITL用户填写倒计时
      "timeoutSeconds": 600,
      // 画布位置，不决定执行先后
      "uiPosition": {
        // 画布横坐标
        "x": 80,
        // 画布纵坐标
        "y": 180
      }
    },
    {
      // 节点实例ID，供连线和数据引用使用
      "id": "check-1",
      // 已注册节点类型，决定config和执行逻辑
      "type": "condition",
      // 该节点类型的配置结构版本，不是顶层流程版本
      "schemaVersion": 1,
      // 节点实现版本；编辑时也可省略，由规范化补齐
      "handlerVersion": "1.0.0",
      // 画布卡片标题，不用于节点定位
      "title": "是否存在客户记录",
      // 节点专属配置，按type解释
      "config": {},
      // 本节点需要的数据输入及其来源
      "inputs": [],
      // 确认策略声明；实际用户确认由tec01负责
      "approvalPolicy": "NONE",
      // 节点操作超时秒数；SQL使用此值，不是HITL用户填写倒计时
      "timeoutSeconds": 600,
      // 画布位置，不决定执行先后
      "uiPosition": {
        // 画布横坐标
        "x": 400,
        // 画布纵坐标
        "y": 180
      }
    },
    {
      // 节点实例ID，供连线和数据引用使用
      "id": "input-1",
      // 已注册节点类型，决定config和执行逻辑
      "type": "hitl_form",
      // 该节点类型的配置结构版本，不是顶层流程版本
      "schemaVersion": 1,
      // 节点实现版本；编辑时也可省略，由规范化补齐
      "handlerVersion": "1.0.0",
      // 画布卡片标题，不用于节点定位
      "title": "人工确认后续参数",
      // 节点专属配置，按type解释
      "config": {
        // 人工表单标题，与画布节点title分开
        "title": "填写后续查询参数",
        // 告诉用户如何填写表单的说明
        "description": "请参考候选结果，输入需要继续查询的客户编号及条数。",
        // 用户填写的字段定义，不是用户答案
        "fields": [
          {
            // 用户回复的JSON键名，后续通过/values/此名称引用
            "name": "customer_id",
            // 表单显示名称，不参与执行绑定
            "label": "客户编号",
            // 用户提交值类型，不把整数变成字符串
            "type": "string",
            // 是否必须提交此键；false和0可以是有效答案
            "required": true,
            // 字符串最少长度，含边界
            "minLength": 1,
            // 字符串最大长度，含边界
            "maxLength": 64
          },
          {
            // 用户回复的JSON键名，后续通过/values/此名称引用
            "name": "query_limit",
            // 表单显示名称，不参与执行绑定
            "label": "最多返回条数",
            // 用户提交值类型，不把整数变成字符串
            "type": "integer",
            // 是否必须提交此键；false和0可以是有效答案
            "required": true,
            // 数字最小值，含边界
            "minimum": 1,
            // 数字最大值，含边界
            "maximum": 100
          }
        ]
      },
      // 本节点需要的数据输入及其来源
      "inputs": [
        {
          // 当前节点内参数名，SQL占位符引用它
          "name": "queryRows",
          // 实际输入值类型，必须与所取结果匹配
          "type": "array",
          // 给人阅读的通用参数说明
          "description": "前置查询返回的候选记录",
          // 运行到此节点时是否必须取得该参数
          "required": true,
          // 取值来源，与控制连线的source不同
          "source": {
            // RUN_INPUT取运行输入；NODE_OUTPUT取前置结果；LITERAL取固定值
            "kind": "NODE_OUTPUT",
            // 提供数据的前置节点ID，只有NODE_OUTPUT使用
            "nodeId": "sql-1",
            // 相对来源节点输出对象的取值路径，只有NODE_OUTPUT使用
            "jsonPointer": "/data"
          }
        }
      ],
      // 确认策略声明；实际用户确认由tec01负责
      "approvalPolicy": "NONE",
      // 节点操作超时秒数；SQL使用此值，不是HITL用户填写倒计时
      "timeoutSeconds": 600,
      // 画布位置，不决定执行先后
      "uiPosition": {
        // 画布横坐标
        "x": 720,
        // 画布纵坐标
        "y": 80
      }
    },
    {
      // 节点实例ID，供连线和数据引用使用
      "id": "sql-2",
      // 已注册节点类型，决定config和执行逻辑
      "type": "sql_read",
      // 该节点类型的配置结构版本，不是顶层流程版本
      "schemaVersion": 1,
      // 节点实现版本；编辑时也可省略，由规范化补齐
      "handlerVersion": "1.0.0",
      // 画布卡片标题，不用于节点定位
      "title": "按人工确认的客户编号查询",
      // 节点专属配置，按type解释
      "config": {
        // 目标数据库路径，作为完整字符串使用，不是连接密码
        "databaseRef": "124/aops-t/aops-t/readonly/aops-t",
        // 单条只读SQL模板，占位符与本节点inputs.name一一对应
        "sqlTemplate": "SELECT id, name FROM users WHERE id={{customer_id}} LIMIT {{query_limit}}"
      },
      // 本节点需要的数据输入及其来源
      "inputs": [
        {
          // 当前节点内参数名，SQL占位符引用它
          "name": "customer_id",
          // 实际输入值类型，必须与所取结果匹配
          "type": "string",
          // 给人阅读的通用参数说明
          "description": "用户确认的客户编号",
          // 运行到此节点时是否必须取得该参数
          "required": true,
          // 取值来源，与控制连线的source不同
          "source": {
            // RUN_INPUT取运行输入；NODE_OUTPUT取前置结果；LITERAL取固定值
            "kind": "NODE_OUTPUT",
            // 提供数据的前置节点ID，只有NODE_OUTPUT使用
            "nodeId": "input-1",
            // 相对来源节点输出对象的取值路径，只有NODE_OUTPUT使用
            "jsonPointer": "/values/customer_id"
          }
        },
        {
          // 当前节点内参数名，SQL占位符引用它
          "name": "query_limit",
          // 实际输入值类型，必须与所取结果匹配
          "type": "integer",
          // 给人阅读的通用参数说明
          "description": "用户确认的查询条数",
          // 运行到此节点时是否必须取得该参数
          "required": true,
          // 取值来源，与控制连线的source不同
          "source": {
            // RUN_INPUT取运行输入；NODE_OUTPUT取前置结果；LITERAL取固定值
            "kind": "NODE_OUTPUT",
            // 提供数据的前置节点ID，只有NODE_OUTPUT使用
            "nodeId": "input-1",
            // 相对来源节点输出对象的取值路径，只有NODE_OUTPUT使用
            "jsonPointer": "/values/query_limit"
          }
        }
      ],
      // 确认策略声明；实际用户确认由tec01负责
      "approvalPolicy": "PLAN",
      // 节点操作超时秒数；SQL使用此值，不是HITL用户填写倒计时
      "timeoutSeconds": 600,
      // 画布位置，不决定执行先后
      "uiPosition": {
        // 画布横坐标
        "x": 1040,
        // 画布纵坐标
        "y": 80
      }
    },
    {
      // 节点实例ID，供连线和数据引用使用
      "id": "done",
      // 已注册节点类型，决定config和执行逻辑
      "type": "end",
      // 该节点类型的配置结构版本，不是顶层流程版本
      "schemaVersion": 1,
      // 节点实现版本；编辑时也可省略，由规范化补齐
      "handlerVersion": "1.0.0",
      // 画布卡片标题，不用于节点定位
      "title": "查询完成",
      // 节点专属配置，按type解释
      "config": {},
      // 本节点需要的数据输入及其来源
      "inputs": [],
      // 确认策略声明；实际用户确认由tec01负责
      "approvalPolicy": "NONE",
      // 节点操作超时秒数；SQL使用此值，不是HITL用户填写倒计时
      "timeoutSeconds": 600,
      // 画布位置，不决定执行先后
      "uiPosition": {
        // 画布横坐标
        "x": 1360,
        // 画布纵坐标
        "y": 80
      }
    },
    {
      // 节点实例ID，供连线和数据引用使用
      "id": "empty-end",
      // 已注册节点类型，决定config和执行逻辑
      "type": "end",
      // 该节点类型的配置结构版本，不是顶层流程版本
      "schemaVersion": 1,
      // 节点实现版本；编辑时也可省略，由规范化补齐
      "handlerVersion": "1.0.0",
      // 画布卡片标题，不用于节点定位
      "title": "未查到客户，结束",
      // 节点专属配置，按type解释
      "config": {},
      // 本节点需要的数据输入及其来源
      "inputs": [],
      // 确认策略声明；实际用户确认由tec01负责
      "approvalPolicy": "NONE",
      // 节点操作超时秒数；SQL使用此值，不是HITL用户填写倒计时
      "timeoutSeconds": 600,
      // 画布位置，不决定执行先后
      "uiPosition": {
        // 画布横坐标
        "x": 720,
        // 画布纵坐标
        "y": 340
      }
    }
  ],
  // 控制连线集合；条件出边的数组顺序决定匹配优先级
  "edges": [
    {
      // 连线稳定ID，条件恢复时按此值识别所选分支
      "id": "e-query-check",
      // 起点节点ID
      "source": "sql-1",
      // 终点节点ID
      "target": "check-1",
      // 连线分类提示，实际路由还看源节点type、condition和default
      "kind": "NORMAL",
      // 画布显示文案，不是判断表达式
      "label": "",
      // 本分支的判断规则；默认边和普通边通常为空
      "condition": null,
      // true表示条件兜底；条件节点恰好一条默认边
      "default": false
    },
    {
      // 连线稳定ID，条件恢复时按此值识别所选分支
      "id": "e-found",
      // 起点节点ID
      "source": "check-1",
      // 终点节点ID
      "target": "input-1",
      // 连线分类提示，实际路由还看源节点type、condition和default
      "kind": "CONDITION",
      // 画布显示文案，不是判断表达式
      "label": "有记录",
      // 本分支的判断规则；默认边和普通边通常为空
      "condition": {
        // 条件从运行状态取值的完整Pointer，包含/nodes/节点ID/output
        "path": "/nodes/sql-1/output/rowCount",
        // 比较操作；gt表示大于
        "op": "gt",
        // 比较目标值，数字0不是字符串0
        "value": 0
      },
      // true表示条件兜底；条件节点恰好一条默认边
      "default": false
    },
    {
      // 连线稳定ID，条件恢复时按此值识别所选分支
      "id": "e-empty",
      // 起点节点ID
      "source": "check-1",
      // 终点节点ID
      "target": "empty-end",
      // 连线分类提示，实际路由还看源节点type、condition和default
      "kind": "CONDITION",
      // 画布显示文案，不是判断表达式
      "label": "否则",
      // 本分支的判断规则；默认边和普通边通常为空
      "condition": null,
      // true表示条件兜底；条件节点恰好一条默认边
      "default": true
    },
    {
      // 连线稳定ID，条件恢复时按此值识别所选分支
      "id": "e-input-query",
      // 起点节点ID
      "source": "input-1",
      // 终点节点ID
      "target": "sql-2",
      // 连线分类提示，实际路由还看源节点type、condition和default
      "kind": "NORMAL",
      // 画布显示文案，不是判断表达式
      "label": "",
      // 本分支的判断规则；默认边和普通边通常为空
      "condition": null,
      // true表示条件兜底；条件节点恰好一条默认边
      "default": false
    },
    {
      // 连线稳定ID，条件恢复时按此值识别所选分支
      "id": "e-query-done",
      // 起点节点ID
      "source": "sql-2",
      // 终点节点ID
      "target": "done",
      // 连线分类提示，实际路由还看源节点type、condition和default
      "kind": "NORMAL",
      // 画布显示文案，不是判断表达式
      "label": "",
      // 本分支的判断规则；默认边和普通边通常为空
      "condition": null,
      // true表示条件兜底；条件节点恰好一条默认边
      "default": false
    }
  ]
}
```

## 13. 保存与规范化注意

新建时可不填写catalogDigest、handlerVersion等技术字段，由执行器规范化补齐；发布时保存返回的完整normalizedDefinition。正常化可能补入source中不适用的null字段以及边的默认值，不代表出现新语义。

四类节点的config另受各自Schema限制，整图Schema还不能替代可达性、无环、前置绑定和SQL语义校验。本说明解释的是当前字段，不承诺数据库权限、实际列结构或某条执行路径上的输出一定存在。
