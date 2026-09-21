# 统一Node Registry与节点扩展设计

## 设计结论

以下能力全部是Node Registry管理的节点类型：

```text
sql_read
condition
llm_extract
hitl_select
human_input
approval
end
未来新增的其他生产操作节点
```

底层契约测试可使用“LLM模拟”“HITL模拟”“SQL模拟”，它们不是新的节点类型，而是同一个节点Handler在不同执行模式下使用不同基础设施Adapter。Studio页面上的“调试”固定使用`TEST`真实Handler；SQL读必须提供工单ID和`AOPS_API_KEY`并执行真实`aops-cli`：

```text
PRODUCTION  生产执行
TEST        连接测试环境执行
SIMULATION  使用固定样例或Mock Adapter
DRY_RUN     只校验和渲染计划，不产生外部调用
```

Workflow Compiler、生产Executor和Studio必须消费同一个Node Registry，避免分别维护节点Schema、参数规则和运行语义。

需要区分两个阶段：Compiler为了“生成草稿”而调用LLM属于编译器内部处理，此时工作流尚不存在，所以它不是DAG节点；草稿或生产工作流中出现的LLM分析必须表示为正式`llm_extract`节点，并由Registry管理。HITL、SQL读和条件判断只要出现在DAG中，也全部是正式节点。

## Node Registry在架构中的位置

```mermaid
flowchart LR
    Registry[itsm-workflow Node Registry]
    Compiler[Workflow Compiler]
    Executor[Production Executor]
    Studio[Studio Test Runtime]
    Catalog[tec01 Catalog Snapshot]
    Version[tec01 Workflow Version]

    Registry --> Compiler
    Registry --> Executor
    Registry --> Studio
    Registry -->|签名Catalog| Catalog
    Catalog -->|发布校验并固化快照| Version

    Executor --> ProdAdapters[Production Adapters]
    Studio --> TestAdapters[Test/Simulation Adapters]
```

Registry是节点定义和执行实现的源。tec01不重新定义节点，只保存用于生产治理和历史复现的Catalog快照。

## 节点包结构

每个节点由Manifest、Schema、Handler、计划渲染器和测试资产组成：

```text
nodes/
  sql_read/
    manifest.yaml
    config.schema.json
    input.schema.json
    output.schema.json
    ui.schema.json
    handler.py
    planner.py
    fixtures/
  llm_extract/
  hitl_select/
  condition/
```

节点代码必须随itsm-workflow发布，不允许从数据库、知识定义或导入包加载任意Python代码。

## Node Manifest

统一Manifest建议如下：

```yaml
type: llm_extract
schemaVersion: 1
handlerVersion: 1.0.0
display:
  name: LLM结构化提取
  category: transform
  description: 将前置节点结果格式化为符合JSON Schema的结构化候选
riskLevel: LOW
approvalPolicy: PLAN
idempotencyClass: REPLAY_WITH_STORED_RESULT
resumeSemantics: CHECK_RESULT_THEN_RETRY
supportedModes:
  - PRODUCTION
  - TEST
  - SIMULATION
  - DRY_RUN
debugPolicy:
  allowSingleNode: true
  allowedModes: [TEST, SIMULATION, DRY_RUN]
  productionArtifactReplay: REDACTED_COPY_ONLY
configSchema: config.schema.json
inputSchema: input.schema.json
outputSchema: output.schema.json
uiSchema: ui.schema.json
resultSensitivity: BUSINESS_DATA
handler: nodes.llm_extract.handler:LlmExtractHandler
planner: nodes.llm_extract.planner:LlmExtractPlanner
```

字段含义：

| 字段 | 作用 |
|---|---|
| `type` | DAG中稳定的节点类型标识 |
| `schemaVersion` | 持久化配置和输入输出协议版本 |
| `handlerVersion` | 执行实现版本，写入计划、attempt和审计 |
| `display` | Studio和tec01通用卡片需要的名称、分类和说明 |
| `riskLevel` | 风险策略和审批下限 |
| `idempotencyClass` | 是否可安全重放、需复用结果或不可自动重试 |
| `resumeSemantics` | Worker丢失后的恢复或UNKNOWN规则 |
| `supportedModes` | 节点允许在哪些执行上下文运行 |
| `debugPolicy` | 是否允许单节点调试、允许模式及生产artifact复用限制 |
| Schema字段 | 配置、输入、输出和通用表单渲染协议 |
| `resultSensitivity` | artifact脱敏、权限和保留策略 |

## 节点Handler接口

所有节点实现同一生命周期：

```python
class NodeHandler(Protocol):
    def validate(self, definition, catalog_context): ...
    def plan(self, definition, resolved_inputs, context): ...
    async def execute(self, prepared, ports, context): ...
    async def resume(self, checkpoint, resume_payload, ports, context): ...
    async def cancel(self, execution_handle, ports, context): ...
    async def reconcile(self, started_attempt, ports, context): ...
    def summarize(self, result): ...
```

Handler不直接访问tec01、SQLite、模型HTTP或`aops-cli`路径，而是依赖标准Port：

```text
StatePort
ArtifactPort
CredentialPort
AopsDbReadPort
ModelPort
HumanInteractionPort
EventPort
ClockPort
```

执行环境负责注入具体Adapter。

## 执行模式与Adapter

| Port | PRODUCTION | TEST | SIMULATION | DRY_RUN |
|---|---|---|---|---|
| `StatePort` | tec01运行状态/租约/checkpoint | Studio SQLite | Studio SQLite | 不保存或只存计划 |
| `ArtifactPort` | tec01加密artifact | SQLite临时artifact | SQLite固定样例 | 不产生业务artifact |
| `CredentialPort` | tec01短期凭据Broker | 测试环境凭据 | 禁止真实凭据 | 禁止凭据 |
| `AopsDbReadPort` | 系统`aops-cli`生产地址 | 测试AOPS/测试库 | FixtureDbReadAdapter | 只渲染命令 |
| `ModelPort` | 批准的内网模型配置 | 测试模型Profile | FixtureModelAdapter | 只校验Prompt和Schema |
| `HumanInteractionPort` | tec01持久化interrupt + Channel | Studio测试interrupt | Studio本地候选选择 | 只展示可能的interrupt |

因此自动化契约测试中的“LLM/HITL/SQL模拟”是节点真实Handler在`SIMULATION`上下文运行，而不是另一套模拟节点代码；它不等同于页面的真实调试入口。

## 内置节点定义

### `sql_read`

- 配置：`databaseRef`、`sqlTemplate`、超时和最大行数。
- 输入：运行参数、字面量或前置节点JSON Pointer。
- 输出：`data[]`、列定义、行数和SSE元数据。
- 生产Adapter调用`aops-cli db read`；模拟Adapter返回符合相同输出Schema的Fixture。
- `idempotencyClass=READ_SAFE`，但为避免重复AOPS审计，FAILED/UNKNOWN仍要求人工确认重试。

### `condition`

- 配置：受限JSON Pointer规则和默认边。
- 不调用外部系统，所有模式执行相同确定性逻辑。
- 输出：命中边、规则摘要和SKIPPED集合。
- 禁止由LLM直接决定路由。

### `llm_extract`

- 配置：批准的`modelProfile`、`promptTemplateId`、`responseSchema`、输入限制和数据策略。
- 输入：前置artifact的受限投影。
- 输出：通过Schema校验的结构化对象，例如`candidates[]`。
- 生产Adapter调用tec01 Model Gateway；模拟Adapter读取固定响应Fixture。
- 使用`runId + nodeId + inputHash + promptVersion`做幂等，保存响应后重试必须复用。

### `hitl_select`

- 输入：候选数组。
- 0个候选：按策略请求手工输入或失败。
- 1个候选：自动选择并记录事件。
- 多个候选：通过`HumanInteractionPort`创建持久化interrupt。
- 生产Adapter把interrupt写tec01并通过Channel交互；Studio Adapter在本地页面展示相同选择卡片。
- 用户响应支持`SELECT`、`REFINE`、`MANUAL_VALUE`和`CANCEL`。`REFINE`只能回到Manifest声明的上游`llm_extract`节点，并受最大迭代次数约束。

### `human_input`

- 用于没有预定义候选的人工补参。
- 必须声明字段Schema、说明、是否敏感和校验规则。
- 用户响应经过Schema校验后才成为节点输出。

### `approval`

- 用于高风险节点到达时的二次审批。
- 输出审批决定、审批人、时间和计划哈希。
- 不能由知识配置降低平台规定的最低审批级别。

### `end`

- 汇总运行输出和安全摘要。
- 不执行外部操作。

## Compiler、Executor和Studio如何共用Registry

### Workflow Compiler

- LLM只能从当前Catalog允许的节点类型和Schema中生成DAG。
- 生成后调用Registry完整校验，不能直接拼接未知节点JSON。
- 参数化SQL使用`sql_read`的输入和输出Schema。
- 草稿中需要LLM/HITL时生成正式`llm_extract/hitl_select`节点，而不是嵌入自由提示词步骤。

### Production Executor

- 按工作流版本固化的`type + schemaVersion + handlerVersion`加载Handler。
- 执行前再次校验节点定义和输入。
- 使用生产Ports把状态、artifact、interrupt和checkpoint写入tec01。
- 找不到精确兼容Handler时拒绝领取，不尝试使用最新版猜测执行。

### Studio

- 从Registry生成节点面板、配置表单、输入绑定和结果视图。
- 使用同一Handler，但注入TEST/SIMULATION Ports。
- 保存到SQLite的数据始终标记`TEST_ONLY`。
- 提交到tec01前重新通过Registry执行生产级静态校验。

## 单节点调试

Studio必须支持选择一个节点单独执行，而无需启动整张DAG。单节点调试仍使用正式Handler和Schema，只是把依赖输入与Port替换为测试上下文。

### 输入来源

调试者可以为节点输入选择：

```text
MANUAL_VALUE       手工填写并通过Input Schema校验
FIXTURE             Registry随节点提供的脱敏Fixture
TEST_ARTIFACT       当前Studio workspace中前置测试节点的artifact
REDACTED_SNAPSHOT   经授权复制并脱敏的生产artifact快照
```

禁止单节点调试直接引用可变的生产artifact地址。`REDACTED_SNAPSHOT`必须复制到Studio临时存储、记录来源哈希并应用字段脱敏，不能反向修改生产结果。

### 调试API

```http
POST /api/v1/studio/node-debug-runs
GET  /api/v1/studio/node-debug-runs/{debugRunId}
POST /api/v1/studio/node-debug-runs/{debugRunId}/interrupts/reply
```

创建请求：

```json
{
  "workspaceId": "test_ws_xxx",
  "node": {
    "id": "llm-extract",
    "type": "llm_extract",
    "schemaVersion": 1,
    "config": {}
  },
  "mode": "SIMULATION",
  "inputs": {
    "rows": {
      "source": "TEST_ARTIFACT",
      "artifactId": "test_art_xxx",
      "jsonPointer": "/data"
    }
  }
}
```

每次调试创建独立`debugRunId`和attempt，保存到Studio SQLite：

```text
node definition snapshot
resolved inputs hash
mode
handlerVersion
status
events
temporary artifacts
diagnostic
expiresAt
```

单节点调试不允许改变tec01生产运行状态。生产节点失败后的正式重试仍必须通过tec01 MCP和原运行状态机；Studio只能建立诊断副本。

### 调试泳道图

```mermaid
sequenceDiagram
    autonumber
    participant D as 开发/管理员
    participant UI as Studio
    participant R as Node Registry
    participant H as Node Handler
    participant A as Test/Simulation Adapters
    participant DB as 临时SQLite

    D->>UI: 选择单节点和执行模式
    UI->>R: 获取Manifest、Schema和debugPolicy
    R-->>UI: 配置表单和允许输入来源
    D->>UI: 手工输入或选择测试artifact
    UI->>H: validate + execute单节点
    H->>A: 调用测试DB/模型/HITL Port
    A-->>H: 标准结果或interrupt
    H-->>UI: NodeResult/diagnostic
    UI->>DB: 保存TEST_ONLY debug run
    UI-->>D: 展示输入、输出、事件和诊断
```

## HITL反馈与LLM重新提取

`hitl_select`收到多候选时，用户不仅可以选择，也可以补充信息让上游LLM重新生成候选，或直接输入明确值。

### 用户动作

```json
{"action":"SELECT","candidateId":"candidate-2"}
```

```json
{"action":"REFINE","feedback":"手机号后四位是8821，请按手机号重新判断"}
```

```json
{"action":"MANUAL_VALUE","value":"C000244","reason":"用户已确认客户编号"}
```

```json
{"action":"CANCEL"}
```

### 受控Refinement Loop

不允许在DAG中配置任意回边。Registry提供受控的LLM/HITL refinement关系：

```yaml
type: hitl_select
config:
  refinement:
    enabled: true
    targetNodeId: llm-extract
    maxIterations: 3
    feedbackInputName: user_feedback
    includePreviousCandidates: true
```

校验规则：

- `targetNodeId`必须是当前HITL直接声明的上游`llm_extract`节点。
- 回路内不能包含SQL写、消息发送等有副作用节点。
- `maxIterations`必须为1至5，默认3。
- 每次REFINE创建新的LLM attempt和新的HITL interrupt，旧interrupt关闭且不能再次回复。
- LLM输入包含原始数据投影、上一轮候选和用户补充，但不允许Prompt指令覆盖系统Schema或安全策略。
- 达到上限后只允许`SELECT`、`MANUAL_VALUE`或`CANCEL`。
- `MANUAL_VALUE`必须通过节点`valueSchema`校验，并记录用户、理由和时间。

tec01保存`interactionSession`：

```text
interactionSessionId
runId
llmNodeId
hitlNodeId
iteration
maxIterations
feedbackHistory
llmAttemptIds
interruptIds
status: WAITING / RESOLVED / CANCELLED / LIMIT_REACHED
selectedArtifactId
```

### 迭代泳道图

```mermaid
sequenceDiagram
    autonumber
    participant E as Executor
    participant T as tec01 Control
    participant L as LLM Node
    participant H as HITL Node
    participant M as tec01 MCP
    participant U as 用户

    E->>L: iteration=1，原始SQL投影
    L-->>E: candidates v1
    E->>H: candidates v1
    H->>T: OPEN interrupt v1
    M-->>U: 展示候选
    alt 用户选择
        U->>M: SELECT candidateId
        M->>T: 保存响应并QUEUED
        E->>H: resume并输出selected
    else 用户补充信息
        U->>M: REFINE feedback
        M->>T: 关闭interrupt v1，iteration=2，QUEUED
        E->>L: 原始投影 + candidates v1 + feedback
        L-->>E: candidates v2
        E->>H: candidates v2
        H->>T: OPEN interrupt v2
        M-->>U: 展示新候选和剩余次数
    else 用户直接输入
        U->>M: MANUAL_VALUE
        M->>T: Schema校验并保存响应
        E->>H: resume并输出manual selected
    else 用户取消
        U->>M: CANCEL
        M->>T: interaction CANCELLED
        T-->>E: 运行取消或按流程终止
    end
```

在Studio单节点调试中，同一循环使用SQLite `HumanInteractionPort`和`FixtureModelAdapter`；生产中使用tec01持久化interrupt和Model Gateway，但节点输入输出Schema完全相同。

## 节点测试流程

```mermaid
sequenceDiagram
    autonumber
    participant D as 开发/管理员
    participant UI as Studio
    participant R as Node Registry
    participant H as Node Handler
    participant A as Simulation Adapters
    participant DB as 临时SQLite

    D->>UI: 选择sql_read/llm_extract/hitl_select
    UI->>R: 获取Manifest和UI Schema
    R-->>UI: 通用表单和端口定义
    D->>UI: 配置节点并启动模拟
    UI->>H: execute(mode=SIMULATION)
    H->>A: 调用Mock DB/Model/HITL Port
    A-->>H: 符合生产Schema的模拟结果
    H-->>UI: 标准NodeResult
    UI->>DB: 保存TEST_ONLY事件和artifact
    UI-->>D: 展示与生产相同的节点卡片和诊断
```

## Catalog同步与工作流发布

```mermaid
sequenceDiagram
    autonumber
    participant CI as itsm-workflow发布流程
    participant R as Node Registry
    participant T as tec01 Catalog
    participant A as 管理员
    participant K as tec01 Knowledge

    CI->>R: 部署节点包
    R->>R: 加载Manifest并执行契约测试
    R->>T: 发布Catalog版本、Schema和摘要哈希
    A->>K: 提交Workflow审核发布
    K->>T: 校验每个节点版本
    T->>R: validate完整DAG
    alt 校验通过
        R-->>T: requiredRuntimeVersion + catalogDigest
        T->>K: 固化Manifest快照
        K-->>A: 发布不可变WorkflowVersion
    else 校验失败
        R-->>K: 未知节点/Schema不兼容/Handler不可用
        K-->>A: 阻止发布
    end
```

## 版本兼容

- 工作流版本固定`catalogDigest`和每个节点的`schemaVersion`。
- 生产attempt记录实际`handlerVersion`和runtime版本。
- 修复实现但不改变协议时可以提升patch版本，并通过兼容矩阵声明可执行旧版本。
- 行为、输入或输出变化必须增加`schemaVersion`并保留旧Handler，直到相关运行和版本退出保留期。
- `DEPRECATED`节点允许查看和运行已有版本，但不能创建新草稿。
- `DISABLED`只用于存在安全风险的节点；tec01阻止新运行，并明确列出受影响工作流。

## 安全边界

- Registry只加载随软件发布并通过签名/哈希校验的节点包。
- `uiSchema`只能描述通用控件，不能下发任意JavaScript。
- 模型URL、数据库凭据、Authorization Header不能成为节点自由配置字段。
- Studio模拟默认禁止生产凭据和生产数据库路径。
- 节点日志只允许安全摘要；完整结果按artifact敏感级别加密存储。
- Handler不能绕过StatePort直接宣称节点成功。

## 建议实现顺序

1. 从现有`app/node_types.py`提取完整Manifest和Registry接口。
2. 将`sql_read/condition/end`迁入Registry并保持现有行为。
3. 让Compiler和Studio从Registry生成、编辑和校验节点。
4. 引入Port/Adapter层，把tec01生产存储和Studio SQLite隔离。
5. 实现`llm_extract`和`hitl_select`，完成SQL多结果选择场景。
6. 增加节点契约、恢复、取消、幂等和安全测试模板。
7. tec01同步Catalog并在知识发布时固化快照。

## 验收标准

- Compiler、Executor和Studio中不存在重复维护的节点Schema。
- 同一DAG在SIMULATION和PRODUCTION模式下具有相同输入输出结构。
- Studio模拟不会访问生产AOPS、生产模型凭据或生产状态。
- 未注册、版本不兼容或被禁用节点无法发布或执行。
- LLM自然语言响应未通过JSON Schema时节点明确失败。
- HITL等待、用户选择、恢复和重复回复均可从tec01事实恢复。
- 新节点只需实现标准Handler、Manifest、Schema和契约测试，不修改调度核心。
