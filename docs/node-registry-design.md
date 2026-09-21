# Node Registry与节点设计

## 目标

Node Registry由itsm-workflow维护，统一决定：

- 节点怎样配置。
- 输入和输出是什么。
- 节点怎样执行。
- 是否需要用户交互。
- 失败后是否允许重试。

tec01保存Workflow中的节点配置和版本，但不实现Python Handler。

## 第一版节点

```text
sql_read            真实执行aops-cli db read
condition           根据前置结果选择分支
hitl_select         从已有候选中选择
hitl_form           手工输入一个或多个参数
end                 汇总并结束
```

运行时取消`llm_extract`节点。Workflow草稿生成阶段仍可由Compiler内部使用LLM生成中文名称、摘要和步骤，但生产执行不依赖LLM判断参数。

## Node Manifest

每种节点在代码中注册：

```json
{
  "type": "sql_read",
  "schemaVersion": 1,
  "handlerVersion": "1.0.0",
  "name": "SQL只读查询",
  "configSchema": {},
  "inputSchema": {},
  "outputSchema": {},
  "riskLevel": "LOW",
  "approvalPolicy": "PLAN"
}
```

Studio中的Node管理只允许修改启停、显示名称、说明和调试默认值，不允许上传代码或修改Handler安全规则。

## SQL读

标准输出：

```json
{
  "status": 0,
  "rowCount": 2,
  "columns": [
    {"name":"customer_id","type":"varchar","comment":"客户编号"}
  ],
  "data": [
    {"customer_id":"C001","customer_name":"王五"}
  ]
}
```

SQL必须是单条只读语句。参数只能来自运行输入、HITL输出或前置节点输出，不能由Agent猜测。

页面中的“调试”固定调用真实Handler。SQL读调试必须提供当前工单ID和`AOPS_API_KEY`，并真实执行系统`aops-cli`。

## 条件判断

判断SQL是否返回数据：

```json
{
  "path": "/nodes/sql-1/output/rowCount",
  "op": "gt",
  "value": 0
}
```

支持：

```text
eq ne gt gte lt lte in contains exists empty
```

每个条件节点必须有默认分支。Executor返回命中的边，tec01在页面上高亮实际路径，并把其他分支显示为`SKIPPED`。

## HITL选择

`hitl_select`直接把前置SQL行映射为候选，不需要LLM。

```json
{
  "type": "hitl_select",
  "config": {
    "selectionMode": "SINGLE",
    "source": {
      "nodeId": "sql-1",
      "jsonPointer": "/data"
    },
    "idPath": "/customer_id",
    "labelTemplate": "{{customer_name}} / {{customer_id}}",
    "displayFields": [
      {"name":"customer_name","label":"客户姓名","path":"/customer_name"},
      {"name":"customer_id","label":"客户编号","path":"/customer_id"},
      {"name":"status","label":"状态","path":"/status"}
    ],
    "outputFields": [
      {"name":"customer_id","path":"/customer_id"},
      {"name":"account_id","path":"/account_id"}
    ]
  }
}
```

SQL可以返回全部字段，但只有`displayFields`发送给页面和Hermes。用户提交`candidateId`后，Executor根据已保存候选产生：

```json
{
  "selected": [
    {
      "candidateId": "candidate-1",
      "values": {
        "customer_id": "C001",
        "account_id": "A001"
      }
    }
  ]
}
```

下游绑定：

```text
/selected/0/values/customer_id
/selected/0/values/account_id
```

多选时`selected`包含多项。大量候选保存在tec01并分页展示，不放进一条Hermes消息。

## HITL表单

`hitl_form`用于用户手工输入：

```json
{
  "type": "hitl_form",
  "config": {
    "title": "填写后续查询参数",
    "fields": [
      {"name":"customer_id","label":"客户编号","type":"string","required":true},
      {"name":"limit","label":"返回条数","type":"integer","required":true,"minimum":1,"maximum":100}
    ]
  }
}
```

输出：

```json
{
  "values": {
    "customer_id": "C001",
    "limit": 20
  }
}
```

下游绑定：

```text
/values/customer_id
/values/limit
```

tec01根据字段Schema校验。Hermes逐项向用户询问，收集完整后汇总确认；Agent不能自行填写。

## 执行流程

Executor收到完整Workflow和节点状态：

1. 跳过`SUCCEEDED`和`SKIPPED`节点。
2. 找到`READY`节点。
3. 告知tec01节点开始。
4. 执行Handler。
5. 告知tec01节点结果和下一条边。
6. 遇到HITL、暂停、取消、失败或完成时释放运行。

每个节点执行结果都必须先返回tec01，才能进入下一个节点。

## 页面和人的交互

### Studio

- 管理Node启停和调试默认值。
- 可视化编辑Workflow。
- 草稿生成后直接渲染DAG。
- 使用真实`aops-cli`单步或整流程调试。

### tec01生产页面

- 展示用户确认过的完整计划。
- 实时高亮当前节点和已执行路径。
- 展示每个节点的结果或失败原因。
- 展示HITL候选、分页和表单。
- 提供暂停、继续、取消和重试。

### Hermes消息渠道

- tec01通过MCP把相同运行状态交给Hermes。
- Hermes向用户展示计划、进度和HITL问题。
- 用户可在消息中选择，也可打开tec01页面处理。
- 两个入口读取同一状态，不会产生两份HITL回复。

## 新增节点

新增节点只需要：

1. 定义配置、输入和输出Schema。
2. 实现Handler。
3. 注册到Node Registry。
4. 添加真实调试样例和自动测试。
5. 发布新的Catalog版本。

tec01同步新Catalog后即可在Workflow编辑器中使用，不需要修改调度核心。
