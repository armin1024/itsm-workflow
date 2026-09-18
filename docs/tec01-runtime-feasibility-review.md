# tec01控制面与itsm-workflow运行时可行性评审

## 评审结论

结论为**有条件可行，建议实施**。

目标分工合理：tec01负责Channel、MCP和全部生产事实；itsm-workflow负责Compiler、Node Registry、Executor和Studio。但现有实现不能直接拆服务，必须先完成Registry驱动、Port/Adapter解耦和远程状态事务协议。

必须满足的前置条件：

1. tec01提供版本化内部API、revision CAS、幂等、租约和事务化节点提交。
2. itsm-workflow将节点Schema、校验、UI描述和Handler收口到统一Registry。
3. Production Executor不再直接使用SQLAlchemy业务模型，而是通过`StatePort/ArtifactPort/InterruptPort/CheckpointPort`访问tec01。
4. checkpoint和节点状态采用staged + atomic commit协议，不能分开对外生效。
5. LLM/HITL反馈采用受控、有最大次数的refinement edge，不开放任意循环。
6. Studio SQLite与生产tec01数据物理隔离，localStorage不保存敏感信息。

## 当前代码事实

| 领域 | 当前实现 | 与目标差距 |
|---|---|---|
| 节点注册 | `app/node_types.py`中的简单字典 | 缺少完整Schema、UI Schema、Handler版本、执行模式和兼容治理 |
| 节点校验 | `app/workflow.py`硬编码`sql_read/condition`规则 | 新节点需要修改核心校验器，不是插件式 |
| 节点执行 | `app/engine.py`内部Handler字典并直接访问SQLAlchemy | Handler与数据库、加密artifact、事件和凭据强耦合 |
| 图结构 | 只允许DAG，非condition节点只能一条出线 | 不支持受控LLM/HITL refinement回边 |
| 条件分支 | LangGraph conditional edge +本地artifact读取 | 可复用，但需改为ArtifactPort和远程状态 |
| checkpoint | `AsyncPostgresSaver`直接连接本地PostgreSQL | 目标需要tec01 Remote Checkpointer或等价状态存储 |
| 草稿提取 | `app/extraction.py`同时取AOPS证据、调用LLM并写Knowledge | Compiler计算和业务持久化未分离 |
| 计划与运行 | `app/runs.py`直接写WorkflowRun、Credential和Event | 目标由tec01创建计划和状态机CAS |
| MCP | Python Adapter调用本地FastAPI | 目标迁到tec01 |
| 前端编辑器 | `WorkflowEditor.tsx`按节点类型硬编码表单 | 目标通过Manifest和UI Schema通用渲染 |
| Studio存储 | 与生产业务库共用 | 目标使用独立TTL SQLite测试库 |

## 分项可行性

### 1. MCP迁到tec01

**可行性：高。**

MCP工具主要是知识/运行控制面的协议包装。tec01已拥有Channel Principal和生产数据，迁入后可以直接处理匹配、计划、确认、wait、中断和结果读取。

风险：

- 需要保持现有MCP工具名、参数和错误语义，避免Hermes Prompt同时大改。
- `workflow_run_wait`必须保留增量事件游标和`displayText`约束。
- 迁移期间不能让Python MCP和tec01 MCP同时修改同一运行。

结论：先兼容实现、双读验证，再切换唯一写入口。

### 2. Compiler无存储化

**可行性：高。**

现有审计过滤、SQL解析、参数化和LLM提炼逻辑可以提取为纯函数/服务。tec01传入`ticketInfo.data`和`auditTimeline.data[]`，Compiler返回DraftProposal和diagnostics。

必须修改：

- 移除Compiler对`AsyncSession/Knowledge/KnowledgeUser`的依赖。
- AOPS证据获取迁到tec01；Compiler默认不持有AOPS用户凭据。
- LLM调用经ModelPort，便于生产Gateway和Studio Fixture切换。
- 生成节点必须从Registry查询，不再手工拼`sql_read`JSON。

### 3. 统一Node Registry

**可行性：高，但属于基础重构。**

Node Registry可以统一Compiler、Executor和Studio，但当前前后端均硬编码节点类型。必须先建立Manifest和Handler协议，再迁移现有节点。

建议Registry作为itsm-workflow源码和构建产物的一部分，不做运行期任意插件上传。tec01只同步签名Catalog和Schema快照。

### 4. tec01保存全部生产状态

**可行性：中高，关键在事务协议。**

Executor可以无本地业务数据库，但节点完成涉及artifact、attempt、event、checkpoint和run revision。若通过多个HTTP请求分别提交，会出现部分成功。

推荐协议：

1. Executor上传artifact/checkpoint为`STAGED`，获得内容哈希。
2. Executor调用`attempts/{id}/commit`，携带staged引用、期望revision和结果。
3. tec01在同一数据库事务中：校验租约与revision、转正artifact/checkpoint、完成attempt、更新节点/run、追加event。
4. 未被commit引用的STAGED对象按TTL清理。

这是生产拆分的硬性条件。

### 5. Remote Checkpointer

**可行性：中等，是主要技术风险。**

LangGraph会在superstep边界调用checkpointer。tec01可以保存opaque checkpoint，但需实现`get_tuple/list/put/put_writes/delete_thread`及格式版本。

风险：

- LangGraph checkpoint写入时机与业务attempt commit不是天然同一事务。
- HTTP延迟会放大每个superstep耗时。
- Python runtime升级可能改变序列化兼容性。

处理方式：

- Remote Checkpointer先写STAGED checkpoint。
- 业务节点commit引用并激活checkpoint。
- 恢复只读取`COMMITTED` checkpoint。
- checkpoint保存`runtimeVersion/checkpointFormatVersion/runRevision`。
- 通过批量pending writes和连接池控制HTTP开销。

### 6. 单节点调试

**可行性：高。**

前提是Handler依赖Port而非直接访问数据库。Studio创建`test_debug_run`，注入TEST/SIMULATION Adapter，直接调用一个Handler。

限制合理：

- 不改变tec01生产运行。
- 生产artifact只能授权后复制为脱敏快照。
- 正式生产失败重试继续走tec01 MCP。
- SQLite仅用于单实例Studio；多实例时迁移到独立测试数据库。

### 7. LLM节点

**可行性：高。**

需要ModelPort、批准的model/prompt profile、输入投影、输出JSON Schema和幂等缓存。LLM只产生结构化候选，不直接控制图路由。

主要风险是敏感数据和非确定性。通过字段白名单、行数/字节限制、temperature=0、输入/响应哈希及已保存结果复用控制。

### 8. HITL节点与反馈循环

**可行性：中高，需要扩展图协议。**

当前WorkflowDefinition禁止任何循环，Engine也假设普通节点只有一条前向边。实现`REFINE`需要增加一种受控回边，而不是开放任意循环。

推荐：

- WorkflowEdge增加`kind=NORMAL|CONDITION|REFINEMENT`。
- `REFINEMENT`只能从`hitl_select`指向声明的上游`llm_extract`。
- 发布校验忽略REFINEMENT边进行DAG拓扑检查，再单独验证回路安全。
- runtime state保存interaction iteration和feedback history。
- 达到`maxIterations`后禁用REFINE。
- 每轮重新执行LLM/HITL并新增attempt，当前outputRef指向最新artifact，历史保留在attempt中。

LangGraph支持循环图，但当前项目的编译器和状态模型需要显式适配；不能只在前端画一条回线。

### 9. Studio SQLite和localStorage

**可行性：高。**

服务端SQLite适合单机临时调试、事件和artifact。localStorage只适合UI偏好。该选择符合安全和可恢复要求。

必须设置：

- 独立数据库文件和迁移链。
- `test_` ID前缀和`TEST_ONLY`字段。
- 默认24小时TTL、容量上限和周期清理。
- 不保存Token、生产凭据、原始生产artifact或工单敏感原文。

## 需要修订的架构细节

### tec01与itsm-workflow职责

- tec01拥有生产知识和运行事实。
- itsm-workflow拥有Node Registry、Compiler和执行实现。
- Node Catalog由itsm-workflow发布，tec01保存不可变快照。
- 生产计划由tec01创建，但计划内容必须调用itsm-workflow Plan Builder校验和渲染。

### checkpoint与业务状态

原设计中独立`PUT checkpoint`容易形成双事实源。正式设计改为：

```text
upload staged artifact/checkpoint
        ↓
attempt commit(expectedRevision, leaseToken, hashes)
        ↓ tec01单事务
artifact/checkpoint COMMITTED
attempt SUCCEEDED/FAILED
node/run transition
event append
revision increment
```

### HITL refinement

refinement是平台级受控边，不是通用循环功能。第一版只支持`llm_extract → hitl_select → llm_extract`模式，不支持循环中出现生产写操作。

## 风险分级

| 风险 | 等级 | 缓解措施 |
|---|---:|---|
| tec01内部API未定导致双方反复修改 | 高 | 先冻结OpenAPI/JSON Schema和契约测试 |
| checkpoint和节点状态不一致 | 高 | STAGED + atomic attempt commit |
| Python当前代码数据库耦合较重 | 高 | 先Ports/Adapters，再切远程存储 |
| HITL回路产生无限循环 | 中高 | 受控REFINEMENT边和最大迭代数 |
| LLM输入泄露业务数据 | 中高 | 投影、脱敏、Model Profile和审计 |
| 通用UI Schema覆盖不了复杂节点 | 中 | 通用表单优先，受控内置Renderer扩展 |
| SQLite被误用于生产 | 中 | 独立配置、启动校验和TEST_ONLY命名空间 |
| MCP切换影响Hermes | 中 | 工具兼容、影子调用和快速回滚 |

## Go/No-Go条件

满足以下条件后进入生产实现：

- tec01团队确认生产数据模型和内部API负责人。
- 确认服务间认证、Credential Broker和Model Gateway方案。
- STAGED artifact/checkpoint与attempt commit事务原型通过。
- Node Registry可加载现有`sql_read/condition/end`并通过原测试。
- Studio SQLite明确只用于测试且具备TTL清理。

若tec01不能提供事务化attempt commit或凭据Broker，则不能移除当前生产PostgreSQL和凭据存储；可以先完成Registry、Compiler和Studio重构，但生产执行仍保留现状。

## 评审结论

两份设计文档的产品方向合理，核心能力均可实现。实施顺序必须是“契约与Registry优先、Studio和Compiler其次、远程生产状态最后”，不能先删除现有数据库或直接把Worker改成HTTP轮询。按本评审修正后可以进入详细实施规划。
