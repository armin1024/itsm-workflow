# Node Registry与节点设计

Node Registry由itsm-workflow维护，统一定义节点配置、输入输出JSON Schema、Handler版本、校验和执行规则。tec01保存Workflow中的节点定义，使用Catalog及Schema生成表单并调用执行侧。

## 当前四种节点

| 类型 | 用途 |
|---|---|
| `sql_read` | 真实执行aops-cli只读SQL，返回行数和行对象 |
| `condition` | 确定性条件判断并选择分支 |
| `hitl_form` | 等待用户填写一个或多个参数 |
| `end` | 汇总并结束 |

运行时不调用LLM。Compiler可以使用内网LLM生成中文草稿；执行时参数由用户输入或前置结果绑定提供。

## 获取JSON约束

```http
GET /internal/v1/runtime/catalog
GET /internal/v1/runtime/schemas
GET /internal/v1/runtime/schemas/workflowDefinition
```

Catalog包含节点名称、类型、Schema/Handler版本和UI信息。schemas端点返回完整Workflow、dispatch、extraction、singleNode约束，并逐节点返回 `configSchema/inputSchema/outputSchema`，便于tec01或其他平台二次开发。

```json
{
  "type":"sql_read",
  "schemaVersion":1,
  "handlerVersion":"1.0.0",
  "name":"SQL只读查询",
  "configSchema":{},
  "inputSchema":{},
  "outputSchema":{},
  "riskLevel":"LOW"
}
```

示例空对象代表实际完整Schema。结构字段通过Schema检查；DAG可达性、上游依赖、只读SQL等由Workflow校验接口进一步检查。

Studio的Node管理只调整当前浏览器中的启停、显示名称、说明与调试默认值，不修改服务端Handler或Catalog。正式新增节点需要开发、注册、测试和发布服务。

## SQL读与条件分支

标准SQL输出：

```json
{
  "status":0,
  "rowCount":2,
  "data":[
    {"customer_id":"C001","customer_name":"王五"},
    {"customer_id":"C002","customer_name":"王五"}
  ]
}
```

原始CLI的SSE被适配为上述对象。SQL必须是安全单条只读语句，工单ID与当前API Key必填。

判断是否有结果：

```json
{"path":"/nodes/sql-1/output/rowCount","op":"gt","value":0}
```

支持 `eq/ne/gt/gte/lt/lte/in/contains/exists/empty` 以及 `all/any/not`。条件节点必须有默认边，未命中路径标为SKIPPED，共用的后续节点仍可执行。

推荐流程：

```text
SQL读 → 条件 rowCount > 0 → 人工表单 → 后续SQL
                       └ 默认 → 无数据结束
```

## 人工表单

用户可参考前置SQL结果后填写值，也可在特定步骤直接补充新参数。

```json
{
  "id":"input-1",
  "type":"hitl_form",
  "title":"填写后续查询参数",
  "config":{
    "title":"填写后续查询参数",
    "description":"参考前置查询结果，确认用于下一步的客户编号与返回条数。",
    "fields":[
      {"name":"customer_id","label":"客户编号","type":"string","required":true,"minLength":1,"maxLength":64},
      {"name":"limit","label":"返回条数","type":"integer","required":true,"minimum":1,"maximum":100}
    ]
  },
  "inputs":[
    {"name":"queryRows","type":"array","source":{"kind":"NODE_OUTPUT","nodeId":"sql-1","jsonPointer":"/data"}}
  ]
}
```

上下文只用于页面/消息展示，字段的最终值必须由用户确认。避免向Hermes或不需要该数据的用户发送整份敏感SQL结果。

运行到该节点后返回 `WAITING_INPUT` 和 `HITL_FORM`，并释放执行资源。用户回复：

```json
{"nodeId":"input-1","action":"SUBMIT","values":{"customer_id":"C001","limit":20}}
```

输出：

```json
{"values":{"customer_id":"C001","limit":20}}
```

下游参数绑定：

```json
{
  "name":"customer_id",
  "type":"string",
  "source":{"kind":"NODE_OUTPUT","nodeId":"input-1","jsonPointer":"/values/customer_id"}
}
```

表单支持字符串、整数、数字、布尔值及必填、长度、数值范围和枚举约束。tec01页面/Hermes收集完整后展示确认，Executor恢复时再次校验值。

## 单节点执行

`POST /internal/v1/runtime/nodes/execute`接收node和已解析inputs，只执行该节点；不自动运行上游，也不替调用方持久化结果。

条件节点单步测试使用 `config.rule` 和传入的上下文。表单第一次执行返回等待交互；使用新requestId提交 `resumePayload` 后返回values。SQL节点使用真实CLI，必须传当前工单ID和请求头API Key。

## 新增节点

1. 定义config/input/output Schema。
2. 实现Handler及安全校验。
3. 注册到Node Registry并提供调试默认值。
4. 验证单节点、Workflow依赖和中断恢复语义。
5. 发布新Catalog，tec01重新获取约束并展示配置表单。

JSON定义不能包含Python代码、任意shell命令或凭据。
