# tec01与itsm-workflow拆分设计

## 结论

采用简单的两层架构：

- **tec01**：用户消息渠道、Hermes接入、MCP、知识管理、计划与运行状态、页面和生产数据。
- **itsm-workflow**：Node Registry、草稿提取、Workflow校验、节点执行和`aops-cli`适配。

tec01主动把完整Workflow下发给Executor。Executor按tec01给出的节点状态继续执行，并把每个节点的开始、结果、失败或等待状态返回tec01。

## 总体结构

```mermaid
flowchart LR
    U[用户] <--> C[tec01消息渠道]
    C <--> H[Hermes]
    H --> M[tec01 MCP]
    U --> W[tec01 Web页面]
    M --> T[tec01运行控制]
    W --> T
    T --> D[(tec01生产数据)]
    T -->|下发完整Workflow| E[itsm-workflow Executor]
    E --> A[aops-cli / AOPS]
    E -->|逐节点返回状态和结果| T
```

用户无论从Hermes消息渠道还是tec01页面操作，最终都进入同一个tec01运行控制接口。Executor不直接向用户发送消息。

## 职责

### tec01

- 通过AOPS Channel与用户交互，并承载Hermes和MCP。
- 保存知识、Workflow版本、执行计划和全部运行状态。
- 从已发布版本创建计划，检查当前用户权限、工单ID和运行参数，并记录用户确认。
- 向用户展示计划、节点进度、结果、失败原因和HITL表单。
- 用户确认后主动向Executor下发完整Workflow。
- 主动下发暂停和取消命令。
- 保存每个节点返回的状态和结果。

### itsm-workflow

- 管理节点类型、Schema和Handler。
- 根据工单详情和操作记录生成Workflow草稿。
- 在审核发布时校验节点配置、只读SQL、DAG、条件分支和输出绑定；执行前复核下发的版本。
- 接收tec01下发的完整Workflow并执行。
- 调用真实`aops-cli`，解析SSE结果。
- 生成条件判断、HITL候选和HITL表单。
- 每个节点结束后立即把状态和结果返回tec01。

## 计划创建与校验分工

**计划由tec01创建。** 参考`codex/list-pagination-search`中的发布与计划流程：先校验草稿并固化不可变版本，创建运行时读取该版本、填入当前工单ID和参数、保存计划快照及确认哈希。拆分后，tec01沿用这个流程；节点执行规则仍由itsm-workflow定义。

| 时机 | 负责方 | 检查内容 |
|---|---|---|
| 编辑草稿 | itsm-workflow | 可选实时检查节点Schema、只读SQL、图结构、条件默认边、HITL配置和前置结果绑定，便于页面即时纠错 |
| 审核发布 | itsm-workflow校验，tec01保存 | **必须**调用Runtime校验；tec01保存返回的规范化Workflow、Catalog版本和内容哈希，再发布不可变版本 |
| 创建执行计划 | tec01 | 从已发布版本生成计划；检查用户能否使用经验、工单ID、当前输入的必填项和类型；保存快照与`planHash`，向用户展示 |
| 用户确认 | tec01 | 对照原`planHash`确认；计划或参数变化后要求重新展示与确认 |
| 下发执行 | itsm-workflow | 重新核对Workflow内容哈希和执行安全规则；节点Handler版本兼容检查列入联调验收 |

创建计划时不必再次请求Runtime做完整DAG/SQL校验，因为tec01读取的是发布时已经校验且之后不可变的版本。tec01也无需复制Python的SQL解析、节点Handler或条件解释器。`/internal/v1/runtime/workflows/plan`保留为预览与联调接口，tec01可用它生成展示材料，但它不是每次创建计划的必经步骤。

tec01在创建计划时可使用已同步的Node Catalog确认发布版本仍有可用Handler；这属于版本兼容检查，不是重新解释Workflow。当前Runtime对历史Catalog/Handler版本的兼容校验还不完整，需在tec01生产联调前补齐，否则旧版本可能在下发时因版本不匹配而失败。

发布前能检查的是绑定来源确实位于上游、JSON Pointer格式合法；SQL执行后某个字段是否真的存在，要等节点返回结果时检查。tec01创建计划时负责校验用户当下输入的类型和必填项，不能把运行时才得到的SQL结果或HITL值当作已知参数。

HITL值与前置SQL结果只有运行到对应节点后才知道。计划只展示“此处将要求用户选择/输入哪些字段”，不能在确认计划时预先填入候选值，也不能由Hermes猜测。

```mermaid
sequenceDiagram
    participant U as 用户
    participant T as tec01页面/Hermes
    participant R as itsm-workflow

    T->>R: 发布前校验草稿
    R-->>T: 通过并返回版本信息
    U->>T: 输入工单ID和已有参数
    T-->>U: 展示完整计划与HITL步骤
    U->>T: 确认计划
    T->>R: 下发已发布Workflow
    R-->>T: 逐节点返回真实状态
```

## 草稿提取

tec01取得工单详情和操作记录后，主动调用Compiler。Compiler不再自行拉取任务，也不主动访问AOPS。

```mermaid
sequenceDiagram
    participant U as 用户/管理员
    participant T as tec01页面与数据
    participant C as itsm-workflow Compiler

    U->>T: 根据工单生成草稿
    T->>T: 获取工单详情和操作记录
    T->>C: 提交提取任务和工单数据
    C-->>T: 正在过滤有效SQL
    C-->>T: 正在参数化SQL
    C-->>T: 正在生成并校验Workflow
    C-->>T: 返回草稿或失败原因
    T-->>U: 页面持续显示进度和最终草稿
```

提取进度由Compiler回调tec01。tec01保存进度，再通过页面SSE或Hermes消息展示。Compiler不直接连接浏览器。

建议进度保持简单：

```text
已接收
正在过滤操作记录
正在生成步骤和参数
正在校验Workflow
已完成 / 失败
```

## 生产执行

tec01下发：

- 完整不可变Workflow。
- 工单ID和运行参数。
- 每个节点当前状态。
- 已完成节点的结果引用。
- 需要恢复的HITL输入。

Executor返回`202 Accepted`后在后台执行，不占用tec01的HTTP请求。

```mermaid
sequenceDiagram
    participant U as 用户
    participant T as tec01页面/Hermes
    participant E as itsm-workflow Executor
    participant A as AOPS

    U->>T: 确认执行计划
    T->>E: 下发完整Workflow
    E-->>T: 已接受
    E->>T: 节点1开始
    E->>A: 执行aops-cli
    A-->>E: 返回结果
    E->>T: 节点1完成和结果
    T-->>U: 展示节点1完成
    E->>T: 节点2开始
    T-->>U: 展示当前进度
```

Executor每完成一个节点就回报，不等整个Workflow结束后再一次性返回。

## 执行一半后恢复

完整Workflow可以已经执行了一部分。tec01再次下发时同时提供节点状态：

```text
SUCCEEDED  已完成，Executor跳过
SKIPPED    未命中分支，Executor跳过
READY      下一步可以执行
PENDING    等待前置节点
WAITING    等待用户输入
FAILED     等待用户决定是否重试
```

Executor只从`READY`节点继续，不重复执行`SUCCEEDED`节点。完整计划可以缓存在Executor内存，但tec01保存的Workflow、节点状态和结果才是最终事实。

## HITL节点

生产Workflow的参数处理使用条件判断和人在回路：

- `hitl_select`：从前置SQL结果中选择一条或多条。
- `hitl_form`：用户手工填写一个或多个参数。

### 从SQL结果选择

SQL可以返回很多字段，但HITL只展示Workflow配置允许的字段：

```json
{
  "type": "hitl_select",
  "config": {
    "title": "请选择客户",
    "selectionMode": "SINGLE",
    "idPath": "/customer_id",
    "labelTemplate": "{{customer_name}} / {{customer_id}}",
    "displayFields": [
      {"name":"customer_name","label":"客户姓名","path":"/customer_name"},
      {"name":"customer_id","label":"客户编号","path":"/customer_id"}
    ],
    "outputFields": [{"name":"customer_id","path":"/customer_id"}]
  },
  "inputs": [{
    "name": "rows", "type": "array",
    "source": {"kind":"NODE_OUTPUT","nodeId":"sql-1","jsonPointer":"/data"}
  }]
}
```

用户只看到配置的姓名和编号；选择后，下游只得到配置的`customer_id`。Agent提交`candidateId`，不能直接修改候选真实值。需要多个参数时，在`outputFields`中逐项声明。

候选很多时，tec01页面和MCP分页显示并支持关键词筛选。

### 用户手工输入

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

用户可以在Hermes对话或tec01页面填写。两种入口使用同一个tec01接口，提交后重新下发Workflow继续执行。

### HITL泳道图

```mermaid
sequenceDiagram
    participant E as Executor
    participant T as tec01/Hermes消息渠道
    participant U as 用户

    E->>T: 需要用户选择或输入
    T-->>U: Hermes消息或页面展示表单
    U->>T: 提交选择/参数
    T->>E: 下发Workflow并携带用户输入
    E->>T: HITL完成，继续下一节点
```

等待用户期间Executor释放该运行，不占用执行槽位。

## 暂停、取消和失败

tec01主动向当前Executor发送命令：

```mermaid
sequenceDiagram
    participant U as 用户
    participant T as tec01页面/Hermes
    participant E as Executor

    U->>T: 暂停或取消
    T->>E: 下发命令
    E->>E: 到安全位置暂停或停止aops-cli
    E->>T: 返回PAUSED/CANCELLED/UNKNOWN
    T-->>U: 展示真实结果
```

- 暂停：当前节点到达安全位置后保存状态并释放运行。
- 取消：停止本地进程；如果请求可能已经到达AOPS，返回`UNKNOWN`。
- 失败：返回失败原因并释放运行，等待用户决定是否重试。
- 完成：返回最终结果并释放运行。

## 页面设计

tec01生产页面至少包含：

1. **执行计划**：节点卡片、数据库、SQL摘要、参数来源和风险说明。
2. **执行进度**：当前节点、已完成节点、失败原因和每步结果入口。
3. **HITL面板**：候选分页、字段表单、确认和取消。
4. **运行控制**：暂停、继续、取消和失败重试。
5. **消息同步**：Hermes中的状态与页面一致，用户可以在任一入口继续处理。

itsm-workflow Studio只用于Node管理、草稿预览和真实调试，不承担生产运行页面。

## 当前规模的约束

- 第一版只有一个Executor服务，最多处理几十个并发运行。
- Executor设置最大运行数和最大`aops-cli`进程数，满载时返回“暂时无容量”，tec01稍后重试。
- 使用`dispatchId`防止tec01重复下发同一运行。
- 每个节点必须先回报tec01，再执行下一个节点。
- AOPS API Key不放进Workflow；Executor按当前运行临时获取并只传给`aops-cli`。
