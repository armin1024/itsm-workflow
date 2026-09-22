# 工作流节点配置使用说明书

本文面向知识管理员、审核人员和二次开发人员，说明如何在“知识与版本 → 手工编排”页面配置当前 `0.7.x` 支持的节点、连线和参数依赖。

## 1. 从零创建一条经验

1. 登录管理页面，进入“知识与版本”。
2. 点击“手工编排”。
3. 填写经验名称、摘要、匹配短语、排除短语、系统标识和授权UID。
4. 在流程画布添加节点，从节点右侧端口拖到下一节点左侧端口完成连线。
5. 点击节点卡片，在右侧面板配置节点。
6. 点击连线配置条件分支；普通节点只能有一条出线，条件节点可以有多条出线。
7. 保存草稿，进入详情页检查流程，再提交审核并发布。
8. 发布后生成不可变版本；已有运行不会被之后的草稿修改影响。

建议先搭建控制顺序，再配置数据绑定：

```text
SQL读 → 人工选择 → SQL读 → 条件判断 → SQL读/结束
```

## 2. 两种依赖必须分清

### 2.1 控制依赖：连线

连线决定哪个节点先执行、下一个执行谁。没有从入口可达的节点不能发布。

```text
sql-1 ──► choose-customer ──► sql-2 ──► done
```

### 2.2 数据依赖：节点输入

数据绑定决定当前节点从哪里取得参数：

| 来源 | 用途 | 示例 |
|---|---|---|
| `RUN_INPUT` | 创建运行时由用户填写 | 客户姓名、手机号、日期范围 |
| `NODE_OUTPUT` | 使用前置节点真实结果 | `/data/0/id` |
| `LITERAL` | 固定字面量，通常由API定义 | 固定状态值 |

`NODE_OUTPUT`只能引用控制流上可到达当前节点的前置节点。

## 3. JSON Pointer填写规则

JSON Pointer以 `/` 开头，每一级使用 `/` 分隔：

```json
{
  "data": [
    {"customer_id":"C001","status":"ACTIVE"}
  ]
}
```

对应路径：

```text
/data                 整个结果数组
/data/0               第一行
/data/0/customer_id   第一行客户编号
/data/0/status        第一行状态
```

字段名包含 `/` 时使用 `~1`，包含 `~` 时使用 `~0`。数组下标从0开始。

不同位置的路径根节点不同：

| 配置位置 | 路径相对对象 | 示例 |
|---|---|---|
| 节点的 `NODE_OUTPUT` 输入 | 来源节点的 `output` | `/data/0/customer_id` |
| HITL选择的字段路径 | 单条候选记录 | `/customer_id` |
| 条件分支路径 | 全局运行文档 | `/nodes/sql-1/output/data/0/status` |

## 4. SQL读节点 `sql_read`

### 适用场景

使用系统安装的 `aops-cli db read` 执行一条只读SQL。运行时自动增加：

```text
--comment '#uatu-<当前工单ID>'
```

### 页面配置

| 字段 | 说明 |
|---|---|
| 节点标题 | 面向用户的步骤名称，例如“查询客户基础信息” |
| 数据库路径 | `serverid/dbid/dbname/dbuser/service_name` |
| SQL模板 | 单条只读SQL，参数使用 `{{参数名}}` |
| 超时 | 默认600秒 |
| 审批 | 通常使用计划级审批 `PLAN` |

示例：

```sql
SELECT customer_id, customer_name, status
FROM customers
WHERE customer_name = {{customer_name}}
```

点击“从SQL同步输入”后，页面会为 `customer_name` 创建输入。

### 输入来自用户

```json
{
  "name":"customer_name",
  "type":"string",
  "description":"从当前工单上下文确认的查询条件，对应客户姓名",
  "source":{"kind":"RUN_INPUT","key":"customer_name"}
}
```

创建运行时，页面和Agent只会向用户询问 `RUN_INPUT`。

### 输入来自前置节点

```json
{
  "name":"customer_id",
  "type":"string",
  "source":{
    "kind":"NODE_OUTPUT",
    "nodeId":"choose-customer",
    "jsonPointer":"/selected/0/values/customer_id"
  }
}
```

系统会按参数类型安全渲染SQL值，不通过Shell拼接命令。

### 限制

- 只允许单条 `SELECT/SHOW/DESCRIBE/UNION`只读语句。
- 禁止插入、更新、删除、建表、删表和多语句。
- SQL中的所有占位符必须与节点输入完全一致。
- `object/array`不能直接作为SQL参数。

## 5. 条件判断节点 `condition`

### 适用场景

根据运行输入或前置节点输出选择一条分支，例如客户状态为ACTIVE时继续查询，否则直接结束。

条件配置在“条件节点的出线”上，不配置在节点卡片本身。

### 配置步骤

1. 将前置节点连接到条件节点。
2. 从条件节点连接两个或多个目标节点。
3. 点击其中一条出线，勾选“默认分支”。
4. 点击其他出线，填写路径、运算符和比较值。
5. 每个条件节点必须且只能有一条默认分支。

示例条件：

```json
{
  "path":"/nodes/sql-1/output/data/0/status",
  "op":"eq",
  "value":"ACTIVE"
}
```

支持的运算符：

| 运算符 | 含义 |
|---|---|
| `eq` / `ne` | 等于 / 不等于 |
| `gt/gte/lt/lte` | 数值或可比较值的大小关系 |
| `in` | 当前值存在于目标集合 |
| `contains` | 当前数组或字符串包含目标值 |
| `exists` | 路径存在 |
| `empty` | 路径不存在或值为空 |

未命中的分支节点会标记为 `SKIPPED`，不会执行。

## 6. 人工候选选择节点 `hitl_select`

### 适用场景

SQL返回多条可能记录，需要用户明确选择一条或多条，再将已确认字段传给后续节点。

即使只有一个候选也必须人工确认。候选为0条时节点以 `NO_HITL_CANDIDATES`失败。

### 候选来源

节点必须且只能有一个名为 `rows` 的 `array`输入，并绑定前置节点输出：

```json
{
  "name":"rows",
  "type":"array",
  "source":{
    "kind":"NODE_OUTPUT",
    "nodeId":"sql-1",
    "jsonPointer":"/data"
  }
}
```

### 基本字段

| 字段 | 说明 |
|---|---|
| 交互标题 | 页面和Hermes向用户展示的问题 |
| 选择模式 | `SINGLE`单选或`MULTIPLE`多选 |
| 最少/最多选择 | 多选时1至100；单选固定为1 |
| 候选唯一值路径 | 相对于单条SQL结果，例如 `/customer_id` |
| 候选标题模板 | 只能引用展示字段，例如 `{{customer_name}} / {{customer_id}}` |

### 展示字段与输出字段

展示字段决定页面和Hermes能看到什么：

```json
[
  {"name":"customer_name","label":"客户姓名","path":"/customer_name"},
  {"name":"customer_id","label":"客户编号","path":"/customer_id"},
  {"name":"status","label":"状态","path":"/status"}
]
```

输出字段决定下游能读取什么：

```json
[
  {"name":"customer_id","path":"/customer_id"},
  {"name":"account_id","path":"/account_id"}
]
```

SQL结果中的其他字段不会发送给页面或MCP。完整候选和隐藏输出字段加密保存。

选择完成后的输出：

```json
{
  "selected":[
    {
      "candidateId":"candidate_x",
      "values":{"customer_id":"C001","account_id":"A001"}
    }
  ]
}
```

后续SQL节点绑定：

```text
/selected/0/values/customer_id
```

多选时 `selected` 包含多项。当前SQL模板通常只绑定单项；如需批量SQL，应新增专用安全节点，不能手工拼接 `IN (...)`。

### 运行交互

- 页面支持关键词过滤、每页20条候选、上一页和下一页。
- Hermes通过 `workflow_interaction_options`读取相同候选。
- 用户选择后仍需在确认框确认。
- Web和Hermes同时回复时只有第一个成功，另一个收到已处理冲突。
- 最大候选数为5000条。

## 7. 人工表单节点 `hitl_form`

### 适用场景

流程执行到中途需要用户补充一个或多个参数，例如客户编号、查询条数或是否包含停用数据。

### 页面配置

| 字段 | 说明 |
|---|---|
| 表单标题 | 用户看到的问题 |
| 填写说明 | 填写口径和用途 |
| 字段名 | 下游绑定使用的稳定英文名 |
| 字段标签 | 用户看到的中文名称 |
| 类型 | `string/integer/number/boolean` |
| 必填 | 未填写时禁止提交 |
| 最小/最大值 | 数字字段约束 |
| 最短/最长长度 | 字符串约束 |
| 枚举值 | 字符串允许值，页面使用逗号分隔 |

示例：

```json
{
  "title":"填写后续查询参数",
  "description":"请根据当前工单确认",
  "fields":[
    {"name":"customer_id","label":"客户编号","type":"string","required":true,"minLength":2,"maxLength":64},
    {"name":"limit","label":"返回条数","type":"integer","required":true,"minimum":1,"maximum":100},
    {"name":"include_disabled","label":"包含停用数据","type":"boolean","required":true}
  ]
}
```

输出：

```json
{
  "values":{
    "customer_id":"C001",
    "limit":20,
    "include_disabled":false
  }
}
```

下游绑定：

```text
/values/customer_id
/values/limit
/values/include_disabled
```

表单值只保存在加密artifact中；运行恢复状态、事件、日志和MCP状态摘要不保存明文值。

Hermes必须逐项询问，收集完成后汇总给用户确认，不能根据上下文猜测未填写字段。

## 8. 结束节点 `end`

结束节点表示一条执行路径正常完成：

- 不调用外部系统。
- 不需要配置输入。
- 没有后续出线。
- 每条可能执行的路径最终都应到达某个终点。

节点标题建议写明结果，例如“完成客户状态核对”。

## 9. 平台内部节点

以下节点运行时已保留，但当前管理画布不开放直接创建：

| 节点 | 用途 |
|---|---|
| `human_input` | 旧版缺失输入兼容；新流程优先使用 `hitl_form` |
| `approval` | 高风险操作到达节点时二次批准；当前只有SQL读，计划确认已满足要求 |

不要通过导入包手工构造未开放节点。后续开放新操作类型时，应通过Node Registry、Schema、Handler和发布校验统一升级。

## 10. 完整配置示例

场景：按姓名查询候选客户，由用户确认后查询客户订单。

```text
sql-candidates → choose-customer → sql-orders → done
```

关键绑定：

```json
{
  "sql-candidates": {
    "sqlTemplate":"SELECT customer_id,customer_name,status FROM customers WHERE customer_name={{customer_name}}",
    "input":"RUN_INPUT customer_name"
  },
  "choose-customer": {
    "rows":"sql-candidates /data",
    "outputFields":[{"name":"customer_id","path":"/customer_id"}]
  },
  "sql-orders": {
    "sqlTemplate":"SELECT order_id,status FROM orders WHERE customer_id={{customer_id}}",
    "customer_id":"choose-customer /selected/0/values/customer_id"
  }
}
```

执行过程：

1. 用户填写工单ID和 `customer_name`。
2. 用户确认完整计划。
3. 第一条SQL返回候选。
4. 运行进入 `WAITING_INPUT`，Worker释放执行槽。
5. 用户从页面或Hermes选择候选并确认。
6. Worker从LangGraph checkpoint恢复。
7. 第二条SQL读取已确认的 `customer_id`。
8. 结束节点完成，页面和Agent读取本次运行真实结果。

## 11. 发布前检查清单

- [ ] 入口节点正确，所有节点从入口可达。
- [ ] 普通节点只有一条出线。
- [ ] 每个条件节点有且仅有一条默认边。
- [ ] SQL只有一条只读语句。
- [ ] SQL占位符与输入名称完全一致。
- [ ] `NODE_OUTPUT`只引用可达的前置节点。
- [ ] JSON Pointer使用正确的根对象。
- [ ] HITL展示字段不包含不需要暴露的信息。
- [ ] HITL输出字段只包含后续步骤需要的值。
- [ ] HITL表单字段类型、必填和范围约束完整。
- [ ] 每条路径均能到达结束节点。
- [ ] 使用测试工单运行并逐节点检查结果后再发布生产版本。

## 12. 常见错误

| 错误 | 原因与处理 |
|---|---|
| `节点只能绑定其前置节点输出` | 缺少控制连线，或来源节点位于当前节点之后 |
| `SQL占位符必须与inputs完全一致` | SQL中的 `{{name}}` 与输入名称不一致 |
| `JSON Pointer无效` | 路径未以 `/` 开头，或层级/数组下标错误 |
| `NO_HITL_CANDIDATES` | 前置结果为空；检查SQL条件，不会自动跳过确认 |
| `HITL_MAPPING_INVALID` | `idPath/displayFields/outputFields`无法从某一行取值 |
| `HITL_CANDIDATE_LIMIT` | 候选超过5000条，应收紧SQL查询条件 |
| `HITL表单输入无效` | 类型、必填、枚举、长度或数值范围不满足Schema |
| `人工交互已经处理或正在恢复` | 页面或Agent已提交；刷新运行状态，不要重复回复 |
| `条件节点必须且只能有一条默认边` | 调整条件节点的出线配置 |

## 13. MCP与Hermes配置原则

HITL无需新增Agent侧状态库。Hermes只读取平台事实：

1. `workflow_run_wait`返回 `FETCH_INTERACTION_OPTIONS` 时调用 `workflow_interaction_options`。
2. 展示候选名称和允许显示的字段。
3. 用户确认后调用 `workflow_hitl_select_reply(run_id, candidate_ids)`；程序自动补充当前interrupt、固定action和幂等键。
4. 表单场景收到 `ASK_USER_FOR_FIELDS` 后逐项询问并汇总确认，再调用 `workflow_hitl_form_reply(run_id, values)`；不要传payload。
5. 提交后继续 `workflow_run_wait`，不能自行宣称节点成功。

完整工具参数和Hermes提示词见 [MCP与Agent接入指南](mcp-agent-integration.md)。
