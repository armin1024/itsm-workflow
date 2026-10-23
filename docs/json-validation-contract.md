# Workflow JSON约束与下游校验接入说明

每个定义字段的中文含义与完整注释图见[Workflow定义逐字段解释](workflow-definition-field-reference-0.12.0.md)。

适用版本：itsm-workflow `0.12.0`。接收方：tec01或其他需要保存、编辑、发布、下发Workflow的服务。前端卡片、表单、连线及错误交互设计另见[前端JSON约束与交互指南](frontend-json-contract-0.12.0.md)。

配套机器可读文件：[json-schema-0.12.0.json](json-schema-0.12.0.json)。此文件由当前服务的Schema接口直接生成，包含Workflow、执行下发、草稿提取三个完整JSON Schema，以及四种节点的配置、输入和输出Schema。不是删减示例。运行回调的严格保存确认及提取requestId隔离另见[tec01回调API](tec01-to-itsm-workflow-api.md)。

## 1. 下游应该怎样校验

采用两层校验，不要只检查JSON能否解析。

1. 下游本地校验：使用JSON Schema Draft 2020-12检查字段、类型、枚举和长度；再按节点`type + schemaVersion`选取对应`configSchema`校验每个节点的`config`。
2. 发布或固定版本前：调用执行器`POST /internal/v1/runtime/workflows/validate`，检查DAG、只读SQL、参数占位符、上游引用等跨字段规则，并取得规范化定义和内容哈希。

JSON Schema通过不等于Workflow可以执行。`config`按类型分派、ID唯一性、图可达性、无环、SQL安全性和输入来源关系没有全部内嵌在Workflow Schema里。

下游负责用户权限、知识可见范围、版本不可变、计划保存、用户执行确认以及恢复快照的真实性。执行器仍在实际下发前复核Workflow和哈希。

## 2. 获取完整JSON Schema

### 离线交付

读取配套JSON文件。根对象是Schema集合，**不能把整个集合直接当成Workflow的Schema**：

| 要校验的JSON | 取出的Schema |
|---|---|
| Workflow定义 | `schemas.workflowDefinition` |
| 完整执行下发请求 | `schemas.dispatch` |
| 工单JSON提取请求 | `schemas.extraction` |
| 某种节点的配置 | `nodes[]`中对应`type + schemaVersion`的`configSchema` |
| 已解析的节点输入/节点结果 | 对应节点的`inputSchema` / `outputSchema` |

`$defs`与`$ref`必须原样保留。单独复制`properties`会丢失引用，导致校验不完整。`$schema`中的URI用于声明标准，不要求内网访问该网站；本地解析器应支持Draft 2020-12并在本地解析文件内引用。

### 从运行服务获取

服务地址以下用`http://itsm-workflow:8089`示例。若经过反向代理，替换为包含正确上下文前缀的地址。

```bash
# 全部Schema及Node Catalog
curl --fail-with-body 'http://itsm-workflow:8089/internal/v1/runtime/schemas' \
  -H 'Authorization: Bearer <RUNTIME_SERVICE_TOKEN>'

# 只获取Workflow Schema
curl --fail-with-body 'http://itsm-workflow:8089/internal/v1/runtime/schemas/workflowDefinition' \
  -H 'Authorization: Bearer <RUNTIME_SERVICE_TOKEN>'
```

单份Schema名称目前为`workflowDefinition`、`dispatch`、`extraction`。未知名称返回404。查询Schema和校验Workflow不需要`AOPS_API_KEY`，也不会执行SQL。

建议下游缓存Schema及Catalog；升级执行器或准备发布新版本时重新获取。配套文件是0.12.0快照，不是后续所有版本的永久约束。

## 3. Workflow顶层结构

| 字段 | 类型 | 必填 | 当前约束/默认值 |
|---|---|---|---|
| `schemaVersion` | integer | 否 | `1`或`2`；默认1，规范化后为2 |
| `catalogDigest` | string/null | 否 | 执行器生成的节点目录摘要，不建议人工构造 |
| `entryNodeId` | string | 是 | 必须对应某个节点ID |
| `nodes` | object[] | 是 | 1～500个节点 |
| `edges` | object[] | 否 | 默认空数组，最多1000条边 |

字段是否必填以Schema中的`required`为准；`default`只是默认值说明，通常不会由JSON Schema验证器自动补齐。最终默认值由执行器规范化填充。

当前Pydantic模型对部分未声明字段会忽略，不应把未知字段当作已支持的扩展。下游建议拒绝拼写错误或未知业务字段；`config`是否允许扩展字段以各节点的`additionalProperties`为准。

### 节点公共字段

| 字段 | 类型 | 必填 | 当前约束/默认值 |
|---|---|---|---|
| `id` | string | 是 | 全图唯一；语义校验规则`^[A-Za-z][A-Za-z0-9_-]{0,119}$` |
| `type` | string | 是 | `sql_read`、`condition`、`hitl_form`、`end` |
| `schemaVersion` | integer | 否 | 默认1，至少1；当前四类节点均注册为1 |
| `handlerVersion` | string/null | 否 | 最多40字符；未传时规范化补齐，目前为`1.0.0` |
| `title` | string | 是 | 1～200字符 |
| `config` | object | 否 | 默认`{}`，必须再通过该节点`configSchema`校验 |
| `inputs` | object[] | 否 | 默认`[]`，同一节点的输入名称不能重复 |
| `approvalPolicy` | string | 否 | `NONE`、`PLAN`、`NODE`；默认`PLAN` |
| `timeoutSeconds` | integer | 否 | 1～7200秒；默认600 |
| `uiPosition` | object/null | 否 | 画布坐标，建议`{"x":80,"y":120}`，值为数字 |

`approvalPolicy`是定义/计划中的策略声明；下游必须真正完成相应用户确认，不能把写入该字段本身当作已经审批。当前集成中的用户交互使用`hitl_form`，不要假定设置`NODE`就自动获得完整审批流程。

### 连线字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | 是 | 下游应生成唯一的边ID；恢复时按该值定位已选分支 |
| `source` / `target` | string | 是 | 起点/终点节点ID，不能相同，必须存在 |
| `kind` | string | 否 | `NORMAL`或`CONDITION`，默认`NORMAL` |
| `label` | string | 否 | 显示说明，默认空字符串 |
| `condition` | object/null | 否 | 条件节点非默认出边的判断规则 |
| `default` | boolean | 否 | 默认false；条件节点必须且只能有一条默认出边 |

推荐普通边使用`NORMAL`，条件出边使用`CONDITION`。当前路由实际依据源节点类型、`condition`和`default`，不能仅依赖`kind`表达分支。

## 4. 节点输入与前置结果绑定

每项`inputs[]`结构如下：

```json
{
  "name": "customer_id",
  "type": "string",
  "description": "客户编号",
  "required": true,
  "source": {
    "kind": "NODE_OUTPUT",
    "nodeId": "input-1",
    "jsonPointer": "/values/customer_id"
  }
}
```

`name`为1～80字符，`description`最多500字符，`type`为`string/number/integer/boolean/object/array`，默认`string`。`required`默认true。SQL输入只允许前四种标量类型。

| 来源 | 必需信息 | 示例 |
|---|---|---|
| `RUN_INPUT` | 非空`key` | `{"kind":"RUN_INPUT","key":"customerName"}` |
| `NODE_OUTPUT` | 存在的前置`nodeId`及以`/`开头的`jsonPointer` | `{"kind":"NODE_OUTPUT","nodeId":"sql-1","jsonPointer":"/data/0/id"}` |
| `LITERAL` | 明确的`value`，其类型应符合输入类型 | `{"kind":"LITERAL","value":10}` |

`NODE_OUTPUT.jsonPointer`相对于**该节点输出对象**，不带`/nodes/<id>/output`前缀。数组从0开始；字段名中的`/`和`~`按JSON Pointer分别编码成`~1`和`~0`。

当前服务会校验来源节点存在且在图上可到达当前节点，但“图上存在路径”不保证每条条件分支都会产生该输出。下游编排时还要确保实际执行路径上来源节点已经成功执行；不要跨互斥分支绑定必填结果。

## 5. 四种节点的配置

### 5.1 SQL读：`sql_read`

```json
{
  "databaseRef": "124/aops-t/aops-t/readonly/aops-t",
  "sqlTemplate": "SELECT id, name FROM users WHERE name={{customer_name}}"
}
```

- 两个字段必填，配置不允许其他字段；数据库路径非空且最多1000字符，按不透明完整字符串处理。
- 占位符格式`{{parameter_name}}`，名称匹配`[A-Za-z_][A-Za-z0-9_]*`。
- SQL占位符名称集合必须与该节点`inputs[].name`集合完全一致；重复使用同一占位符允许，无参数SQL使用`inputs:[]`。
- 参数代表值，不代表表名、列名或任意SQL片段。字符串占位符外不要自行加引号，执行器按类型转义并加引号。
- 只允许单条只读SQL，不能只靠`SELECT`前缀或正则判断。当前服务用MySQL方言AST检查`SELECT/SHOW/DESCRIBE/UNION`等允许的根节点，并排除写入类节点；最终以服务校验结果为准。
- `AOPS_API_KEY`不进入配置或输入。实际执行通过请求头临时传入，并固定使用`--comment '#uatu-<ticketId>'`。

SQL输出示例：`{"status":0,"data":[{"id":"C001"}],"rowCount":1}`。CLI的SSE由执行器解析，下游使用JSON结果。

### 5.2 条件：`condition`

完整Workflow中建议`config:{}`、`inputs:[]`。判断规则定义在它的出边上，而不是另造一个`config.rule`。

```json
{
  "path": "/nodes/sql-1/output/rowCount",
  "op": "gt",
  "value": 0
}
```

条件路径相对于`{"inputs":运行参数,"nodes":{"节点ID":{"output":该节点输出}}}`。与节点输入绑定的相对路径不同。

支持`eq/ne/gt/gte/lt/lte/in/contains/exists/empty`：`in`表示实际值在`value`集合中，`contains`表示实际集合或字符串包含`value`；`exists/empty`不需要`value`。比较类型应兼容，例如行数比较使用数字0，而不是字符串`"0"`。

组合条件使用以下一种形式，不要在同一对象混用多个组合键：

```json
{
  "all": [
    {"path":"/nodes/sql-1/output/rowCount","op":"gt","value":0},
    {"not":{"path":"/inputs/customerName","op":"empty"}}
  ]
}
```

`all/any`为非空条件数组，`not`为一个条件对象。按`edges[]`顺序取第一条命中的非默认出边，没有命中时走默认边；不是并行执行所有命中分支。缺失路径、比较类型不兼容时的具体行为由相应操作符决定，下游不应依赖隐式类型转换。

### 5.3 人工输入：`hitl_form`

```json
{
  "title": "填写后续查询参数",
  "description": "参考前置查询结果后填写客户编号",
  "fields": [
    {"name":"customer_id","label":"客户编号","type":"string","required":true,"minLength":1},
    {"name":"limit","label":"查询上限","type":"integer","required":false,"minimum":1,"maximum":100}
  ]
}
```

`title`与`fields`必填；字段数量1～50，每项必须有`name/label/type/required`。字段名匹配`^[A-Za-z_][A-Za-z0-9_]*$`，应保持唯一；类型为`string/integer/number/boolean`。

可选约束：`minimum/maximum`用于数字，`minLength/maxLength`用于字符串，`enum`为允许值数组。下游应检查约束与类型适配、上下界合法、枚举值类型一致，不要仅因为外层Schema接受字段就认为表单可用。

到达节点后释放执行上下文，返回`WAITING_INPUT`和表单交互信息；tec01/Hermes收集用户输入。恢复时重新下发完整快照并附：

```json
{"nodeId":"input-1","action":"SUBMIT","values":{"customer_id":"C001","limit":10}}
```

`values`必须为对象，符合字段类型和必填/范围约束，不允许未定义字段。节点输出为`{"values":{"customer_id":"C001","limit":10}}`。用户取消时使用运行取消流程，而不是假定`resumePayload.action=CANCEL`是受支持的表单回复。

### 5.4 结束：`end`

建议`config:{}`、`inputs:[]`、`approvalPolicy:"NONE"`，不连接出边。当前实现也允许非结束类型无出边而终止流程；推荐显式使用结束节点，使计划和界面含义清晰。

当前不支持`llm_extract`、`hitl_select`或任意自定义脚本/命令节点。新增类型由执行器发布Node Registry后才能使用。

## 6. 发布前语义校验接口

```http
POST /internal/v1/runtime/workflows/validate
Authorization: Bearer <RUNTIME_SERVICE_TOKEN>
Content-Type: application/json
```

请求结构：

```json
{
  "validationMode": "PUBLISH",
  "workflowDefinition": {
    "schemaVersion": 2,
    "entryNodeId": "sql-1",
    "nodes": [
      {
        "id": "sql-1",
        "type": "sql_read",
        "schemaVersion": 1,
        "title": "查询客户",
        "config": {
          "databaseRef": "124/aops-t/aops-t/readonly/aops-t",
          "sqlTemplate": "SELECT id FROM users WHERE name={{customer_name}}"
        },
        "inputs": [
          {"name":"customer_name","type":"string","source":{"kind":"RUN_INPUT","key":"customerName"}}
        ]
      },
      {"id":"done","type":"end","title":"完成","config":{},"approvalPolicy":"NONE"}
    ],
    "edges": [{"id":"next","source":"sql-1","target":"done"}]
  }
}
```

`validationMode`允许`DRAFT/PUBLISH/EXECUTE/TEST`，默认DRAFT；各模式仍做结构与图校验，不意味着草稿模式允许任意残缺Workflow。可选`targetCatalogDigest`表达预期Catalog，不一致返回409。

成功返回200，含`valid:true`、`normalizedDefinition`、`catalogDigest`、`workflowContentHash`、`requiredRuntimeVersion`和`warnings`。下游应保存**返回的规范化定义和哈希**，而不是原始编辑JSON；处理警告后再固定发布版本。

主要语义检查包括：节点ID格式与唯一性、入口存在、边引用合法、不自连、全图可达、无环、普通节点不能多路扇出、条件默认边及规则合法、输入名称唯一、上游绑定有效、节点配置、只读SQL及占位符集合。

注意：发布校验对Handler版本不一致可能返回警告，并非所有问题都返回错误；执行下发会拒绝不可用Handler。返回成功也不证明数据库权限、表列存在或真实数据路径有效，这些只有实际执行时才能验证。

错误示例（HTTP 422）：

```json
{"code":"WORKFLOW_VALIDATION_FAILED","message":"条件节点 check-1 必须且只能有一条默认边","retryable":false}
```

请求字段类型错误同样返回422，但`code`为`REQUEST_VALIDATION_FAILED`，错误位置在`details.fields[]`中。接口不会回显凭据。

## 7. 执行下发与提取请求校验

### 执行下发：`schemas.dispatch`

`POST /internal/v1/executions/dispatch`。必填字段：

| 字段 | 约束 |
|---|---|
| `dispatchId` / `runId` | 1～160字符，`^[A-Za-z0-9_.:-]+$` |
| `ticketId` | 正整数，用于当前工单及comment |
| `workflowContentHash` | 校验接口返回的内容哈希，禁止随意填写 |
| `workflowSnapshot` | 已规范化的完整Workflow；dispatch Schema已引用Workflow结构，但仍需节点配置与语义校验 |

可选：`runInputs`、`nodeStates`、`nodeOutputs`、`nodeResults`、`currentNodeId`、`selectedRoutes`、`resumePayload`，字段结构见配套Schema。首次执行通常省略恢复字段，由tec01保存并在后续恢复时传回最近可信快照。

恢复额外检查：

- `nodeStates`的键必须为图中节点；允许状态为`PENDING/READY/RUNNING/WAITING/WAITING_INPUT/SUCCEEDED/SKIPPED/FAILED/PAUSED/CANCELLED/UNKNOWN`，但下发时`RUNNING/UNKNOWN`必须先核实处理，不能直接恢复。
- `currentNodeId`、`resumePayload.nodeId`必须存在；已跳过节点不可作为恢复起点，已取消节点不会自动重执行。
- `selectedRoutes`按`节点ID→边ID`传入，边必须属于该源节点；已成功的条件节点必须保留选中的边。
- 已完成结果必须来自已确认回调，不能由用户伪造；下游负责快照归属、完整性及当前用户权限。
- 工作流内容哈希、Handler兼容性和API Key要求由下发接口继续复核。API Key通过`X-AOPS-Api-Key`传递，不写入JSON。

### 草稿提取：`schemas.extraction`

`POST /internal/v1/extractions/{knowledgeId}`，必填`requestId/ticketId/ticketInfo/auditTimeline`。

```json
{
  "requestId":"extract_100173_1",
  "ticketId":100173,
  "ticketInfo":{"id":100173,"incident_id":"INC-2026-0001","event_title":"客户信息查询"},
  "auditTimeline":[]
}
```

`requestId`为1～160字符且匹配`^[A-Za-z0-9_.:-]+$`。`ticketInfo`必须为info响应的`data`对象，其中`id`必须为正整数并与`ticketId`一致；`auditTimeline`必须为操作记录响应的`data`对象数组，禁止传完整响应信封。

Schema允许空操作数组，但不代表能提取草稿：没有成功的只读SQL时，后续提取失败；这属于业务过滤结果，不是JSON格式错误。

## 8. 下游实现清单

1. 选择支持Draft 2020-12、`$defs/$ref`的本地验证器；关闭不必要的远程Schema抓取。
2. 对原始JSON做类型检查，不把字符串数字、字符串布尔值默默转换为合法值。
3. 按配套文件取正确根Schema，再逐节点用`type + schemaVersion`匹配`configSchema`。找不到注册类型或版本就拒绝。
4. 展示字段路径及中文错误，区分结构错误、图/SQL语义错误和执行时业务失败。
5. 发布时调用validate；保存规范化定义、目录摘要、哈希和警告。不要在Java端自行复刻Python默认值补齐和哈希算法。
6. 创建计划校验运行参数，确保同一个运行参数在各节点的类型一致，并完成用户确认。SQL相关输入以真实类型传JSON值。
7. 下发时使用已固定版本与可信恢复快照，不把Workflow JSON当作任意代码，也不把服务身份Token当作用户AOPS Key。

验收至少覆盖：缺字段、非法类型、未知节点/版本、重复节点ID、缺默认分支、环与不可达节点、非前置结果绑定、SQL写操作/多语句、占位符不一致、HITL必填/范围校验、错误哈希以及未知状态恢复。
