# tec01控制面与itsm-workflow计算执行平台设计

## 最终结论

取消Java执行器计划。目标架构固定为：

- **tec01（Java控制面）**：AOPS Channel网关、Hermes MCP、生产身份与权限、知识和版本、计划与运行状态、中断、审批、事件、artifact、checkpoint及审计的唯一生产事实源。
- **itsm-workflow（Python计算执行平台）**：Workflow草稿编译、节点定义与Schema、DAG校验、计划渲染、节点执行、`aops-cli`适配、LLM/HITL运行语义及节点扩展。
- **itsm-workflow Studio**：独立开发调试页面，使用服务端轻量级SQLite保存有TTL的临时草稿和测试运行；浏览器`localStorage`只保存无敏感信息的UI偏好。

这比同时维护两套Executor实现更简单，也能确保“草稿提取生成的节点”和“生产实际执行的节点”使用同一套定义、校验器和Handler。

## 总体架构

```mermaid
flowchart LR
    User[AOPS Channel用户] --> Channel[tec01 Channel Gateway]
    Channel <--> Hermes[Hermes Agent]
    Hermes --> MCP[tec01 MCP Server]

    MCP --> Control[tec01 Workflow Control]
    MCP --> Knowledge[tec01 Knowledge/Retrieval]
    Control --> Prod[(tec01生产存储)]
    Knowledge --> Prod

    Control -->|Compiler/Executor协议| Runtime[itsm-workflow Runtime]
    Runtime --> Compiler[Workflow Compiler]
    Runtime --> Registry[Node Registry]
    Runtime --> Executor[Python Executor]
    Executor --> CLI[aops-cli]
    CLI --> AOPS[AOPS API/DB Read]

    Developer[开发/管理员] --> Studio[itsm-workflow Studio]
    Studio --> Registry
    Studio --> Compiler
    Studio --> Executor
    Studio --> Temp[(临时SQLite)]
    Studio -->|提交候选草稿| Knowledge
```

## 服务职责

### 先用一句话理解两边如何协作

```text
tec01负责“记账、排队和对外展示”，itsm-workflow Executor负责“领任务和真正干活”。
```

Executor采用**主动领取（pull）**模式，而不是等待tec01反向调用：

1. tec01把已确认的运行或可继续的节点放入待执行队列。
2. Executor向tec01发起长轮询，主动领取一项任务；没有任务时等待，超时后再次领取。
3. tec01返回运行快照、待执行节点、已提交checkpoint、恢复输入和有时限的`leaseToken`。
4. Executor先向tec01登记“本次节点尝试已经开始”，再调用LLM、`aops-cli`或其他外部系统。
5. Executor执行过程中发送租约心跳，并主动查询暂停/取消等控制命令；tec01不会从服务端反向连接Executor。
6. Executor把结果、诊断和checkpoint暂存到tec01，再提交节点完成事务。
7. tec01事务提交成功后，节点的新状态才成为事实，并通过MCP、SSE或Channel供Hermes和用户查看。

因此，“任务由谁发起”需要分两层理解：用户或Hermes在tec01创建/控制运行；Executor主动从tec01领取具体执行任务。tec01决定什么可以执行，Executor决定何时有空领取并报告实际结果。

### 为什么不由tec01直接推送完整任务

第一版固定采用“Executor长轮询claim”，不采用`tec01 -> Executor`的HTTP回调：

- Executor最清楚自己的空闲槽位、支持的Catalog/Handler版本和健康状态，可以按实际能力领取。
- tec01无需反向访问不同网段内的Executor，减少防火墙、服务发现、负载均衡和双向证书配置。
- 租约、心跳、幂等和过期接管都围绕claim建立；Executor宕机后其他实例可以重新领取。
- HTTP回调“发送成功”不等于节点已经获得合法租约，更不等于节点执行成功，tec01最终仍需维护队列与领取协议。

如果以后长轮询数量或调度延迟成为问题，可以增加Kafka、Redis Stream等通知通道，但采用**通知推送、任务领取**的混合模式：

```text
tec01 ──推送 RUN_AVAILABLE 唤醒信号──> Executor
Executor ──主动 claim 并取得 leaseToken──> tec01
```

唤醒消息不携带完整Workflow、凭据或执行结果，也不改变运行状态；消息即使重复或丢失，Executor仍可通过长轮询claim恢复。tec01数据库及其claim事务继续是唯一任务事实源。暂停/取消通知也可以通过消息通道加速，但Executor最终必须使用心跳或`GET commands`读取权威命令。

### 图中两个容易误解的tec01模块

| 图中名称 | 通俗名称 | 做什么 | 不做什么 |
|---|---|---|---|
| `tec01 Control` | 运行控制中心 | 排队、分配租约、保存运行/节点状态、校验状态转换、处理中断/暂停/取消/重试 | 不执行SQL，不调用节点Handler，不保存大块结果正文 |
| `tec01 Artifact` | 运行结果仓库 | 加密保存SQL结果、LLM候选、HITL选择、失败诊断等较大或敏感内容，返回`artifactId`供状态记录引用 | 不调度节点，不决定运行状态 |

生产状态表里通常只保存“节点成功、输出引用为`artifactId=...`”；真正的多行SQL结果放在Artifact结果仓库中。这样列表、事件和MCP状态查询可以保持轻量，也能对结果单独加密、授权和按期清理。

### tec01

| 模块 | 职责 |
|---|---|
| Channel Gateway | AOPS用户消息接入、Channel会话和消息发送 |
| MCP Server | 知识匹配、计划、确认、状态等待、补参、暂停、继续、取消、重试和结果读取 |
| Identity & Policy | UID映射、管理员/操作员权限、知识可见范围和运行访问控制 |
| Knowledge & Retrieval | 草稿、审核、发布、不可变版本、生命周期、全文/向量/rerank和导入导出 |
| Workflow Control | `planHash`、运行状态机、revision CAS、幂等请求、审批和中断 |
| Lease & Attempt Ledger | 任务领取、租约、心跳、attempt和UNKNOWN协调 |
| Event & Notification | 事件sequence、MCP wait、Web SSE和Channel主动通知去重 |
| Artifact & Checkpoint | 生产SQL结果、LLM结果、HITL选择、诊断和恢复checkpoint |
| Credential Broker | 托管AOPS凭据并向有效Executor租约发放短期执行凭据 |
| Production API/UI | 生产知识管理、运行中心、审核、查询和审计 |

### itsm-workflow

| 模块 | 职责 |
|---|---|
| Workflow Compiler | 过滤工单操作、只读SQL分析、去重、参数化、LLM中文提炼和DAG草稿生成 |
| Node Registry | 节点Manifest、配置/输入/输出Schema、风险级别、兼容版本和Handler注册 |
| Plan Builder | 对不可变DAG进行二次校验，解析运行输入并生成安全计划材料 |
| Executor | DAG调度、节点边界checkpoint、条件分支、输出绑定、interrupt和恢复 |
| Node Handlers | `sql_read/condition/llm_extract/hitl_select/human_input/approval/end`及后续扩展 |
| CLI Adapter | 参数数组调用`aops-cli`、SSE解析、超时、取消、输出限制和诊断脱敏 |
| Remote State Client | 通过tec01协议提交attempt、事件、artifact、checkpoint和租约心跳 |
| Studio API/UI | 节点浏览、DAG编排、草稿提取调试、模拟执行和测试结果查看 |
| Temporary Store | 仅保存非生产临时草稿和测试运行，TTL到期自动清理 |

### 明确边界

- tec01拥有全部生产业务数据，itsm-workflow不建立生产业务数据库。
- 节点定义和执行代码以itsm-workflow Node Registry为源；tec01保存发布版本所引用的Manifest快照。
- itsm-workflow不能直接发布知识，只能向tec01提交候选草稿。
- Studio临时测试状态不能被Hermes生产MCP匹配，也不能成为生产运行事实。
- itsm-workflow的SQLite故障不能影响tec01中已经发布的知识或生产运行状态。

## 生产计划和状态保存在哪里

全部保存在tec01：

| 实体 | 内容 |
|---|---|
| `workflow_versions` | 不可变DAG、Node Catalog版本、Manifest快照和内容哈希 |
| `workflow_runs` | 计划快照、工单ID、输入、`planHash`、状态、revision |
| `workflow_node_states` | 每个节点的状态、当前attempt和输出引用 |
| `workflow_attempts` | STARTED、Handler版本、Executor、退出码、错误和完成时间 |
| `workflow_interrupts` | HITL、补参和审批请求及用户响应 |
| `workflow_events` | 单调sequence、进度、安全摘要和通知状态 |
| `workflow_artifacts` | SQL输出、LLM候选、HITL选择和失败诊断 |
| `workflow_checkpoints` | Python执行游标、pending writes和格式版本 |
| `workflow_credentials` | 加密凭据或短期凭据引用 |

计划创建时tec01保存完整快照，并初始化所有节点为`PENDING`。Executor只领取快照和短期租约。节点完成时，tec01必须在一个事务中提交：

```text
attempt完成
节点状态变化
artifact正式引用
workflow event
checkpoint引用
run revision递增
```

Executor内存中的状态不是事实。只有tec01提交成功，Web、Hermes和Channel才能向用户展示该节点已经完成。

## 节点定义由itsm-workflow管理

### Node Manifest

每个节点由itsm-workflow注册：

```text
type
schemaVersion
handlerVersion
configSchema
inputSchema
outputSchema
uiSchema
riskLevel
approvalPolicy
idempotencyClass
resumeSemantics
resultSensitivity
```

示例：

```json
{
  "type": "llm_extract",
  "schemaVersion": 1,
  "handlerVersion": "1.0.0",
  "riskLevel": "LOW",
  "idempotencyClass": "REPLAY_WITH_STORED_RESULT",
  "resumeSemantics": "CHECK_RESULT_THEN_RETRY",
  "configSchema": {},
  "inputSchema": {},
  "outputSchema": {},
  "uiSchema": {}
}
```

tec01定期同步Node Catalog并保存：

```text
catalogVersion
nodeType
schemaVersion
handlerVersion范围
Schema内容哈希
状态：ACTIVE / DEPRECATED / DISABLED
```

知识发布时，tec01调用itsm-workflow校验完整DAG，并将使用的Catalog/Schema快照固化到工作流版本。生产执行时itsm-workflow确认自己仍支持该快照；不支持时运行不能入队。

### 新节点扩展流程

```mermaid
sequenceDiagram
    autonumber
    participant D as 节点开发者
    participant CI as 契约/故障测试
    participant R as itsm-workflow Registry
    participant T as tec01 Node Catalog
    participant A as 管理员

    D->>CI: Handler + Manifest + Schema + UI Schema
    CI->>CI: 成功/失败/取消/恢复/UNKNOWN测试
    CI-->>R: 部署并注册节点版本
    R->>T: 同步Catalog和Schema哈希
    A->>T: 发布使用新节点的Workflow
    T->>R: validate workflow
    alt 版本兼容
        R-->>T: validated + requiredRuntimeVersion
        T->>T: 固化Manifest快照并发布
    else 不兼容
        R-->>T: 缺失能力或Schema错误
        T-->>A: 阻止发布并展示原因
    end
```

约束：

- 节点代码随itsm-workflow部署，禁止从数据库加载任意Python代码或Shell模板。
- 兼容修改只能增加可选字段；破坏性修改必须增加`schemaVersion`。
- `handlerVersion`升级不能静默改变已发布版本的输入输出语义。
- 下线旧Handler前必须确认没有活跃或可重试运行引用它。
- 高风险节点审批策略由平台强制，知识作者不能关闭。

## Workflow草稿提取

草稿提取由itsm-workflow Compiler执行，tec01负责证据获取、任务状态和草稿保存。

### 职责分配

| 环节 | 责任方 |
|---|---|
| 用户请求、权限、创建人和授权UID | tec01 |
| 获取`ticketInfo`和`auditTimeline` | tec01 AOPS Gateway |
| 过滤失败记录、SQL只读校验、去重与参数化 | itsm-workflow Compiler |
| LLM结构化中文提炼和依赖识别 | Compiler通过受控Model Gateway |
| DAG和Node Manifest校验 | Compiler第一次校验，tec01保存前再次调用Runtime校验 |
| extraction job、DRAFT、生命周期 | tec01 |
| 编辑、审核、发布和索引 | tec01 |

### 草稿编译协议

tec01先保存extraction job和证据，Compiler Worker使用租约领取，避免LLM长耗时占用同步HTTP请求：

```text
POST /internal/v1/compiler/claims
POST /internal/v1/compiler/claims/{leaseToken}/heartbeat
POST /internal/v1/compiler/extractions/{extractionId}/complete
POST /internal/v1/compiler/extractions/{extractionId}/fail
```

claim返回`ticketInfo`、`auditTimeline`、evidenceHash、targetCatalogDigest和promptVersion。Compiler完成后提交候选定义和诊断，不写数据库：

```json
{
  "compilerVersion": "1.0.0",
  "evidenceHash": "sha256...",
  "name": "客户信息查询",
  "summary": "根据工单条件查询客户信息",
  "matchPhrases": ["客户信息查询"],
  "negativePhrases": [],
  "systemKeys": ["crm"],
  "workflowDefinition": {},
  "diagnostics": {
    "auditOperationCount": 5,
    "acceptedOperationCount": 2,
    "ignoredOperations": []
  }
}
```

tec01保存`workflow_extraction_jobs`，相同`ticketId + evidenceHash + compilerVersion + promptVersion`使用幂等结果。LLM失败、没有有效操作或DAG不合法时只保存失败诊断，不生成兜底草稿。

### 提取泳道图

```mermaid
sequenceDiagram
    autonumber
    participant U as 操作员
    participant T as tec01
    participant A as AOPS Gateway
    participant C as Workflow Compiler
    participant L as Model Gateway
    participant S as tec01 Knowledge Store

    U->>T: 从工单提取Workflow
    T->>S: 创建QUEUED extraction job
    T->>A: 获取ticketInfo和auditTimeline
    A-->>T: 工单证据
    C->>T: claim extraction job
    T-->>C: 证据 + lease + targetCatalogVersion
    C->>C: 过滤、只读校验、去重和参数化
    alt 无有效操作
        C-->>T: NO_VALID_OPERATIONS + diagnostics
        T->>S: job FAILED
    else 有效操作
        C->>L: 受控Prompt + 结构化证据
        L-->>C: 中文语义与依赖
        C->>C: 生成并校验DAG
        C->>T: complete DraftProposal + diagnostics
        T->>C: 按当前Catalog二次validate
        C-->>T: validated
        T->>S: 单事务创建DRAFT、生命周期和完成job
        T-->>U: 打开草稿编辑页面
    end
```

## SQL、LLM和HITL扩展场景

推荐DAG：

```text
sql_read → llm_extract → hitl_select → downstream_node
```

### `llm_extract`

- 输入前置SQL artifact的受限行列投影。
- 只引用批准的`modelProfile`和`promptTemplateId`，知识定义不能填写模型URL或凭据。
- 使用JSON Schema强制输出`candidates[]`。
- 保存输入哈希、模型版本、提示词版本和响应哈希。
- 自然语言输出未经Schema校验不得传给下游。

示例输出：

```json
{
  "candidates": [
    {"id":"candidate-1","label":"客户A / 0001","value":"0001","reason":"姓名和手机号一致"},
    {"id":"candidate-2","label":"客户A / 0002","value":"0002","reason":"姓名一致"}
  ]
}
```

### `hitl_select`

| 候选数量 | 行为 |
|---:|---|
| 0 | 根据`zeroCandidatePolicy`请求人工输入或失败 |
| 1 | 自动选择并记录`AUTO_SELECTED_SINGLE`事件 |
| 多个 | 在tec01创建OPEN interrupt，运行进入`WAITING_INPUT`并释放Executor租约 |

用户通过Channel选择`candidateId`后，Hermes调用tec01 MCP。tec01保存interrupt response并重新入队；Executor恢复HITL checkpoint，提交：

```json
{
  "selected": {
    "id": "candidate-1",
    "value": "0001"
  }
}
```

下游节点通过`/selected/value`读取已确认参数。

HITL响应不仅支持选择：

```text
SELECT        选择候选
REFINE        用户补充条件，重新执行声明的上游llm_extract节点
MANUAL_VALUE  用户直接填写最终值并通过valueSchema校验
CANCEL        取消本次交互或运行
```

`REFINE`使用Registry定义的受控迭代关系，不允许任意DAG回边。默认最多3轮，每轮创建新的LLM attempt和HITL interrupt，并在tec01保存feedback history。到达上限后只能选择、手工输入或取消。完整协议见 [统一Node Registry与节点扩展设计](node-registry-design.md)。

### 运行泳道图

```mermaid
sequenceDiagram
    autonumber
    participant U as 用户
    participant H as Hermes
    participant M as tec01 MCP入口
    participant T as tec01运行控制中心（状态/队列）
    participant S as tec01运行结果仓库（Artifact）
    participant E as itsm-workflow执行器
    participant L as tec01模型网关
    participant X as aops-cli/外部系统

    U->>H: 确认完整执行计划
    H->>M: 批准运行(planHash)
    M->>T: 运行改为QUEUED并入队
    loop 执行器空闲时主动长轮询
        E->>T: claim：有没有我支持的待执行任务？
        T-->>E: 返回运行快照、节点、checkpoint、leaseToken
    end
    E->>T: 登记SQL节点attempt=STARTED
    T-->>E: 返回attemptId和新revision
    E->>X: 调用aops-cli执行SQL读
    X-->>E: 返回SSE结果
    E->>S: 暂存多行SQL结果（STAGED）
    E->>T: 原子提交SQL成功、结果引用和checkpoint
    T-->>E: COMMITTED；SQL节点=SUCCEEDED
    E->>S: 按授权读取SQL结果的受限投影
    E->>T: 登记LLM节点attempt=STARTED
    E->>L: 结构化提取(responseSchema)
    L-->>E: 返回candidates[]
    E->>S: 暂存LLM候选结果
    E->>T: 原子提交LLM成功、结果引用和checkpoint
    T-->>E: COMMITTED；LLM节点=SUCCEEDED
    alt 单候选
        E->>T: 提交HITL自动选择和checkpoint
    else 多候选
        E->>T: 提交WAITING_INPUT、候选interrupt和checkpoint，释放租约
        H->>M: wait/status查询运行状态
        M-->>H: 返回候选选择请求
        H-->>U: 在Channel展示候选
        alt 用户SELECT
            U-->>H: 选择candidateId
            H->>M: 回复SELECT
            M->>T: 保存回复，运行重新QUEUED
            E->>T: 再次claim并取得checkpoint + resumePayload
            E->>T: 提交HITL选择结果和新checkpoint
        else 用户REFINE
            U-->>H: 补充具体条件
            H->>M: 回复REFINE
            M->>T: 保存反馈，iteration+1，重新QUEUED
            E->>T: 再次claim恢复运行
            E->>L: 原始投影 + 旧候选 + feedback
            L-->>E: 新candidates[]
            E->>T: 提交新LLM attempt和新候选interrupt
        else 用户MANUAL_VALUE
            U-->>H: 输入明确值
            H->>M: 回复MANUAL_VALUE
            M->>T: 校验valueSchema并重新QUEUED
            E->>T: 再次claim并提交人工值
        else 用户CANCEL
            U-->>H: 取消
            H->>M: 回复CANCEL
            M->>T: 按策略提交CANCELLED
        end
    end
    Note over T,S: 只有tec01提交成功的状态和Artifact才会展示给用户
```

恢复规则：

- LLM使用`runId + nodeId + inputHash + promptVersion`作为幂等键。
- 已保存模型响应时复用原结果，避免重试产生不同候选。
- HITL interrupt和用户回复由tec01持久化，等待期间不占用Executor并发。
- 重复回复返回原结果；过期revision或非法candidateId返回冲突。

## 生产执行与控制

### 计划、确认和执行

```mermaid
sequenceDiagram
    autonumber
    participant U as AOPS用户
    participant H as Hermes
    participant M as tec01 MCP
    participant T as tec01 Control
    participant R as itsm-workflow Runtime
    participant E as Executor

    H->>M: workflow_plan
    M->>T: 读取不可变WorkflowVersion
    T->>R: validate + render plan
    R-->>T: 安全计划和requiredRuntimeVersion
    T->>T: 保存WAITING_PLAN_APPROVAL快照
    T-->>M: 完整计划和planHash
    M-->>H: 展示计划
    H-->>U: 请求确认
    U-->>H: 明确确认
    H->>M: workflow_run_approve
    M->>T: CAS为QUEUED
    loop Executor主动长轮询领取
        E->>T: claim兼容运行
        T-->>E: 快照、checkpoint和leaseToken
    end
```

这里的箭头`T-->>E`只是Executor这次HTTP长轮询的响应，不表示tec01主动向Executor推送。

### 每个节点的状态由谁改变

| 阶段 | 发起方 | tec01中保存的典型状态 | 说明 |
|---|---|---|---|
| 创建运行 | tec01 | `WAITING_PLAN_APPROVAL` | 计划已生成，尚未确认 |
| 用户确认 | tec01 MCP/Control | 运行`QUEUED`，入口节点`READY` | 加入可领取队列 |
| 领取任务 | Executor主动claim | 仍由tec01保存；同时产生租约 | claim不是tec01反向推送 |
| 开始节点 | Executor请求，tec01校验后提交 | 节点`RUNNING`、attempt`STARTED` | 提交成功后才允许调用外部系统 |
| 节点执行中 | Executor心跳/进度上报 | 节点`RUNNING` | 进度是辅助信息，不替代状态事务 |
| 节点完成/失败 | Executor提交建议，tec01事务校验 | `SUCCEEDED`或`FAILED` | 同时提交artifact、checkpoint、事件和revision |
| 需要用户输入 | Executor提交interrupt | `WAITING_INPUT`等 | 释放租约，不占Executor并发 |
| 用户回复 | tec01 MCP/Control | 运行重新`QUEUED` | 下次由任意兼容Executor主动claim恢复 |
| 暂停/取消 | 用户经tec01发起，Executor轮询到命令 | 先`*_REQUESTED`，到安全边界后变终态 | 防止把“已收到请求”误报成“已完成动作” |

tec01是状态的唯一权威，但并不是所有状态变化都由tec01凭空决定：Executor报告执行事实，tec01负责验证租约、revision、幂等键和合法转换后持久化。Web、Hermes和用户只读取tec01，不读取Executor内存。

### 中断、继续、取消和重试

```mermaid
sequenceDiagram
    autonumber
    participant U as 用户
    participant H as Hermes
    participant M as tec01 MCP
    participant T as tec01 Control
    participant E as Executor

    alt HITL/补参/审批
        E->>T: OPEN interrupt并释放租约
        M-->>H: WAITING_INPUT或WAITING_NODE_APPROVAL
        H-->>U: 请求输入
        U-->>H: 回复
        H->>M: interrupt_reply
        M->>T: 保存响应并QUEUED
    else 暂停/继续
        H->>M: pause
        M->>T: 保存PAUSE_REQUESTED
        E->>T: 心跳或GET commands主动查询
        T-->>E: 在查询响应中返回PAUSE命令
        E->>T: 当前节点到达安全边界后提交PAUSED
        H->>M: resume
        M->>T: PAUSED直接转QUEUED，等待Executor再次claim
    else 取消
        H->>M: cancel
        M->>T: 保存CANCEL_REQUESTED
        E->>T: 心跳或GET commands主动查询
        T-->>E: 在查询响应中返回CANCEL命令
        E->>E: 终止CLI进程组
        E->>T: 提交CANCELLED或UNKNOWN
    else FAILED/UNKNOWN
        T-->>M: 错误或未知结果
        M-->>H: 禁止自动重试
        U-->>H: 明确选择
        H->>M: node_retry
        M->>T: 新attempt或标记失败
    end
```

### 为什么有`PAUSE_REQUESTED`，又有`PAUSED`

这两个状态表达的不是“队列方式不同”，而是**请求已经受理**和**动作已经真正完成**的区别：

| 状态 | 中文含义 | 此时实际发生了什么 |
|---|---|---|
| `PAUSE_REQUESTED` | 已请求暂停 | tec01已经记录请求，但正在执行的节点可能仍在调用CLI；用户不能被告知“已经暂停” |
| `PAUSED` | 已暂停 | Executor已到达节点安全边界、提交checkpoint并释放租约；此时没有节点继续执行 |
| `CANCEL_REQUESTED` | 已请求取消 | tec01已记录请求，Executor正在尝试终止本地进程；外部请求可能已经到达AOPS |
| `CANCELLED` | 已取消 | Executor已完成本地停止和状态提交；不代表能够撤销已经送达外部系统的操作 |
| `WAITING_INPUT` | 等待用户输入 | Executor已经安全提交interrupt/checkpoint并释放租约，因此它本身就是稳定等待状态，无需额外`INPUT_REQUESTED` |
| `QUEUED` | 等待领取 | 运行已经可以继续，等待某个兼容Executor主动claim |

`resume`没有单独设计`RESUME_REQUESTED`，是因为`PAUSED`状态下已经没有正在执行的节点。tec01可以在一个本地事务中立即把它改回`QUEUED`；真正恢复仍要等Executor下一次主动claim。若未来恢复过程包含外部异步动作，再增加`RESUME_REQUESTED`也不迟。

## 独立Studio页面

建议保留并独立部署itsm-workflow Studio，用于：

- 查看Node Catalog和节点Schema。
- 图形化编排临时DAG。
- 输入`ticketInfo/auditTimeline`调试草稿提取。
- 使用脱敏样例执行节点和条件分支。
- 模拟LLM结构化输出和HITL候选选择。
- 选择任意Registry节点进行单节点调试，手工输入或引用测试artifact，不启动整张DAG。
- 查看临时事件、artifact、checkpoint和失败诊断。
- 将审核后的候选草稿提交到tec01进入正式审核。

Studio使用tec01 SSO或短期开发JWT。生产Channel用户不会访问Studio，Studio也不能直接将临时测试运行标为生产成功。“模拟LLM/HITL/SQL”表示从统一Node Registry加载正式节点Handler并注入Simulation Adapter，不是另外维护模拟节点定义。

单节点调试只允许`TEST/SIMULATION/DRY_RUN`，每次创建独立`test_debug_run`和attempt并写临时SQLite。若需要分析生产失败节点，只能把经过授权和脱敏的artifact复制为临时快照；不能从Studio重试或修改tec01生产运行。正式重试仍通过tec01 MCP状态机完成。

## 临时测试存储选择

### 推荐：服务端SQLite为主，localStorage为辅

| 方案 | 优点 | 缺点 | 使用建议 |
|---|---|---|---|
| 服务端SQLite | 支持多页面恢复、共享测试、事件查询、artifact和TTL清理；更接近生产状态模型 | 需要文件目录、迁移和清理任务 | **作为Studio临时测试主存储** |
| `localStorage` | 实现简单、无需服务端表 | 容量小、同步阻塞、浏览器可读、不可共享、清理和审计弱 | 只保存UI偏好和无敏感未提交快照 |
| IndexedDB | 容量和结构优于localStorage | 仍局限单浏览器，安全边界与localStorage相同 | 可选保存大型非敏感画布草稿，不作为运行状态源 |
| 内存 | 无残留、速度快 | 刷新或重启即丢失 | 单次节点单元测试 |

### SQLite保存内容

建议表：

```text
studio_workspaces
studio_drafts
studio_test_runs
studio_test_node_states
studio_test_events
studio_test_artifacts
studio_extraction_jobs
studio_node_debug_runs
```

规则：

- 明确标记`TEST_ONLY`，ID使用`test_`前缀。
- 默认TTL 24小时，可配置到72小时；后台定期清理。
- 设置单workspace、单artifact和数据库总容量限制。
- 不保存AOPS API Key、Authorization Header或生产凭据。
- 默认使用脱敏工单证据和模拟CLI；连接真实测试环境必须二次确认。
- SQLite文件与tec01生产数据库没有同步或复制关系。
- 多实例Studio不能共享单机SQLite；需要多实例时改用独立测试数据库，不影响Executor协议。

### localStorage允许内容

```text
画布缩放和面板布局
最近选择的节点类型
主题和每页数量
未提交且确认不含敏感值的表单快照
```

禁止保存：

```text
AOPS API Key或任何Token
ticketInfo和auditTimeline原文
SQL真实查询结果
LLM输入输出中的客户数据
生产runId对应的状态副本
加密密钥或凭据引用
```

localStorage不是可信状态源；浏览器中的内容只能作为编辑便利，提交时必须由Studio API重新校验。

### Studio测试泳道图

```mermaid
sequenceDiagram
    autonumber
    participant D as 开发/管理员
    participant UI as Studio页面
    participant API as Studio API
    participant DB as 临时SQLite
    participant R as Compiler/Executor
    participant T as tec01

    D->>UI: 编排节点或输入脱敏工单证据
    UI->>API: 保存临时workspace
    API->>DB: TEST_ONLY + expiresAt
    D->>UI: 运行提取/模拟执行
    UI->>API: 创建test run
    API->>R: compile或execute sandbox
    R-->>API: 临时节点事件和artifact
    API->>DB: 保存测试状态
    API-->>UI: 展示画布、结果和诊断
    D->>UI: 提交候选草稿
    UI->>API: promote request
    API->>R: 最终DAG校验
    R-->>API: validated
    API->>T: 创建正式DRAFT请求
    T-->>UI: production knowledgeId
    API->>DB: 标记PROMOTED，等待TTL清理
```

“提交候选草稿”不是发布：tec01仍创建`DRAFT`，后续必须走正式编辑、审核和发布流程。

## itsm-workflow内部模块结构

建议单仓库、共享Schema包、分进程运行：

```text
workflow-schema
  node manifests
  JSON schemas
  compatibility rules

workflow-compiler
  audit filtering
  SQL parameterization
  LLM draft extraction

workflow-executor
  DAG runtime
  handlers
  remote state client
  aops-cli adapter

workflow-studio
  Studio API
  React UI
  temporary SQLite
```

Compiler、Executor和Studio共享`workflow-schema`。Executor以`PRODUCTION`模式加载Handler；Studio调用同一Handler时只能使用`TEST/SIMULATION/DRY_RUN`模式或sandbox上下文。

统一Node Registry的Manifest、Handler、Port/Adapter、执行模式和版本兼容细节见 [统一Node Registry与节点扩展设计](node-registry-design.md)。

## 内部接口

### tec01调用itsm-workflow

```text
GET  /internal/v1/runtime/catalog
POST /internal/v1/runtime/workflows/validate
POST /internal/v1/runtime/workflows/plan
```

### itsm-workflow调用tec01

```text
POST /internal/v1/execution/claims
POST /internal/v1/execution/claims/{leaseToken}/heartbeat
GET  /internal/v1/execution/claims/{leaseToken}/commands
POST /internal/v1/execution/claims/{leaseToken}/commands/{commandId}/ack
POST /internal/v1/runs/{runId}/attempts
POST /internal/v1/runs/{runId}/staged-artifacts
PUT  /internal/v1/runs/{runId}/staged-artifacts/{uploadId}/content
PUT  /internal/v1/runs/{runId}/staged-checkpoints/{checkpointId}
POST /internal/v1/runs/{runId}/staged-checkpoints/{checkpointId}/writes
POST /internal/v1/runs/{runId}/attempts/{attemptId}/commit
POST /internal/v1/compiler/claims
POST /internal/v1/compiler/claims/{leaseToken}/heartbeat
POST /internal/v1/compiler/extractions/{extractionId}/complete
POST /internal/v1/compiler/extractions/{extractionId}/fail
```

artifact和checkpoint先以`STAGED`上传；interrupt作为WAITING attempt commit的一部分提交。只有`attempt commit`可以在tec01同一事务中转正它们，并同时完成attempt、节点状态、interrupt、事件和run revision。恢复只读取`COMMITTED` checkpoint，避免checkpoint和业务状态形成双事实源。

所有生产写请求携带：

```text
leaseToken
expectedRevision
idempotencyKey
runtimeVersion
handlerVersion
payloadHash
```

## 分阶段落地

### 阶段1：tec01接管MCP和生产存储

- 在tec01实现现有MCP工具和生产状态机。
- Hermes只连接tec01 MCP。
- 知识、版本、运行、事件、artifact和checkpoint迁入tec01。

### 阶段2：拆分Workflow Schema与Compiler

- 从当前服务提取Node Manifest和兼容校验包。
- 将草稿提取改为无存储Compiler协议。
- tec01保存extraction job和DRAFT。

### 阶段3：Executor无数据库化

- 接入tec01租约、attempt、artifact和Remote Checkpointer协议。
- 新运行写tec01；旧运行排空并转只读。
- 验证中断、继续、取消、重试和UNKNOWN语义。

### 阶段4：Studio和临时SQLite

- 保留独立编排与调试页面。
- 实现Registry节点的单节点调试API、输入来源选择、独立attempt和诊断展示。
- 临时数据明确`TEST_ONLY`并启用TTL和容量限制。
- 建立“提交到tec01 DRAFT”流程，不允许直接发布。

### 阶段5：扩展LLM/HITL节点

- 实现`llm_extract`结构化输出、幂等和数据策略。
- 实现`hitl_select`单候选自动选择、多候选持久化interrupt以及`SELECT/REFINE/MANUAL_VALUE/CANCEL`响应。
- 实现最多1至5轮的受控LLM/HITL refinement loop和feedback history。
- 通过Channel端到端验证长时间等待和恢复。

## 验收标准

- tec01是所有生产计划和运行状态的唯一事实源。
- itsm-workflow删除生产数据库配置后，Compiler和Executor仍能完整工作。
- 草稿提取、DAG校验和生产Executor引用同一Node Manifest版本。
- SQL多行结果可以经过LLM结构化，并在多候选时通过Channel完成HITL选择。
- 用户可以补充条件让LLM重新生成候选，直到选择、手工输入、取消或达到迭代上限。
- 任意Registry节点都可以在Studio中单步调试，且不会改变tec01生产运行状态。
- Hermes、Executor或Studio重启不会丢失tec01中的生产中断和运行状态。
- SQLite删除或损坏只影响临时Studio测试，不影响生产知识和运行。
- localStorage中搜索不到Token、工单证据、SQL结果或客户数据。
- 新节点在契约、恢复和故障测试完成前不能进入生产Catalog。

## 最终建议

采用“**tec01生产控制面与唯一存储 + itsm-workflow统一计算执行平台**”。itsm-workflow同时管理Workflow Compiler、Node Registry和Python Executor，从而保证提取、定义和执行语义一致；tec01负责MCP、生产状态与数据治理。保留独立Studio页面，并使用带TTL的服务端SQLite作为临时测试主存储，localStorage只保存无敏感UI偏好。该方案比维护Java/Python双Executor更简单，也更适合持续增加LLM、HITL及其他节点类型。
