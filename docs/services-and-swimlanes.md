# 服务职责、总体架构与关键流程泳道图

本文面向平台运维、二次开发、工作流设计人员和 Agent接入方。图中的“提交”均表示事务已经写入 PostgreSQL；只有已提交状态才允许 UI、REST或 MCP对外宣称为事实。

## 服务与外部组件职责

| 组件 | 运行形式 | 核心职责 | 持有或访问的数据 | 故障影响 |
|---|---|---|---|---|
| `itsm-workflow-migrate` | systemd oneshot | 启动前执行 Alembic迁移，保证表、列和索引与程序版本一致 | PostgreSQL Schema、`alembic_version` | API和 Worker通过 `Requires`阻止启动，避免新代码访问旧表结构 |
| `itsm-workflow-api` | FastAPI + systemd | 登录鉴权、知识管理、检索匹配、计划创建、审批、中断控制、结果读取、SSE、管理页面和静态资源 | 业务表、加密凭据、加密 artifact；不直接领取运行任务 | 页面、REST和内部 MCP调用不可用；已经运行的 Worker可继续当前节点 |
| `itsm-workflow-worker` | Python常驻进程 + systemd | 领取 `QUEUED`运行、维护租约、驱动 LangGraph、执行节点、调用 `aops-cli`、写 attempt/checkpoint/event、清理过期敏感数据 | 运行、节点尝试、凭据引用、artifact、LangGraph checkpoint | 新运行不执行；外部调用期间丢失 Worker时节点转为 `UNKNOWN`，禁止自动重放 |
| `itsm-workflow-mcp` | Streamable HTTP MCP Adapter + systemd | 把 Agent工具调用转换为内部 REST调用，传递身份头，生成适合 Agent展示的事实摘要 | 不直接访问数据库，不保存 API Key，不执行 CLI | Hermes等 Agent无法调用；Web和REST仍可正常使用 |
| PostgreSQL | 内网基础设施 | 唯一生产持久化源：知识、版本、运行、事件、租约、审批、迁移审计、加密凭据和 checkpoint | 所有生产事实 | 整个平台停止读写；不得降级为生产 SQLite |
| `aops-cli` | 目标机系统可执行文件 | 获取工单证据，执行 `db read`，把用户 AOPS API Key放在子进程环境中 | 仅运行期接收 API Key；不由安装包携带 | 工单提取或 SQL节点失败，平台记录可读诊断 |
| AOPS | 外部内网服务 | 用户身份、工单详情、审计时间线和数据库只读操作 | AOPS业务数据和审计记录 | 登录、草稿提取或节点执行不可用 |
| 内网 LLM | HTTP服务 | 把已过滤的工单证据提炼成中文名称、摘要、匹配短语、参数语义和依赖 | 只接收提取所需的工单语义与安全操作定义 | 不生成低质量兜底草稿，返回 `LLM_ANALYSIS_FAILED` |
| BGE-M3 Embedding | HTTP服务 | 为已发布知识和查询意图生成向量 | 摘要优先检索文本 | 匹配降级为全文召回，禁止可靠自动选择 |
| Rerank | HTTP服务 | 对融合候选重排 | 查询意图与摘要优先文档 | 返回融合排序并要求用户选择 |
| Nginx | 可选反向代理 | TLS、子路径转发、SSE关闭缓冲、MCP路由 | 不持有业务数据 | 配置错误会造成静态资源404、SSE延迟或 MCP不可达 |
| Web控制台 | React静态页面 | 知识编排、计划确认、运行画布、诊断、导入导出和人工中断处理 | 只保存短期页面状态；身份在 HttpOnly会话中 | 不影响后台运行，恢复页面后从数据库和 SSE重建状态 |
| Hermes或其他 Agent | MCP客户端 | 匹配经验、展示计划、获得用户确认、短轮询状态、展示结果和处理中断 | 不应缓存为事实源，不直接调用 `aops-cli` | Agent断开不影响运行；重连后从 `lastEventId`继续观察 |

## 总体架构

```mermaid
flowchart LR
    User[用户/管理员] --> Browser[Web控制台]
    User --> Agent[Hermes或其他Agent]
    Browser --> Nginx[Nginx/内网入口]
    Agent --> Nginx
    Nginx --> API[itsm-workflow-api]
    Nginx --> MCP[itsm-workflow-mcp]
    MCP -->|内部REST| API

    Migrate[itsm-workflow-migrate] -->|Alembic| PG[(PostgreSQL)]
    API --> PG
    Worker[itsm-workflow-worker] --> PG
    Worker --> Checkpoint[LangGraph Checkpointer]
    Checkpoint --> PG

    API --> LLM[内网LLM]
    API --> Embed[BGE-M3 Embedding]
    API --> Rerank[Rerank]
    API --> CLI[aops-cli]
    Worker --> CLI
    CLI --> AOPS[AOPS API/DB Read]

    PG -.已提交事件.-> API
    API -.SSE/短等待.-> Browser
    API -.事实快照.-> MCP
```

核心边界：

- API负责“接收、校验、授权和记录控制意图”，Worker负责“实际执行”。
- MCP只适配协议，不复制业务状态机，不直接访问数据库或 CLI。
- PostgreSQL是唯一事实源；SSE、MCP wait和页面刷新读取的都是已提交状态。
- LangGraph checkpoint恢复图状态，attempt ledger协调外部 CLI调用的非 exactly-once风险。

## 1. 安装、迁移与服务启动

```mermaid
sequenceDiagram
    autonumber
    participant O as 运维人员
    participant I as install.sh
    participant M as migrate.service
    participant P as PostgreSQL
    participant A as api.service
    participant W as worker.service
    participant C as mcp.service

    O->>I: 安装0.7.1 --no-start
    I->>I: 校验Linux x86_64和服务账号
    I->>I: 替换程序并保留service.env
    I->>I: 清理._* / .DS_Store / __MACOSX
    I->>M: 写入并reload systemd unit
    O->>M: restart migrate
    M->>P: alembic upgrade head
    alt 迁移成功
        P-->>M: version=0007_diagnostics_transfer
        M-->>O: active exited
        O->>A: start API
        O->>W: start Worker
        A-->>C: API就绪后允许MCP启动
    else 迁移失败
        P-->>M: 回滚事务DDL
        M-->>A: Requires失败，API不启动
        M-->>W: Requires失败，Worker不启动
        O->>M: journalctl -u itsm-workflow-migrate
    end
```

## 2. 登录与请求鉴权

```mermaid
sequenceDiagram
    autonumber
    participant U as 用户/三方调用方
    participant API as Workflow API
    participant AOPS as AOPS /v2/user/self
    participant PG as PostgreSQL/会话

    alt Web登录
        U->>API: POST /auth/session + AOPS API Key
        API->>AOPS: Authorization Bearer API Key
        AOPS-->>API: data.uid
        API->>API: 校验UID allowlist
        API-->>U: HttpOnly加密会话Cookie
    else REST或MCP
        U->>API: Workflow Token + X-AOPS-Api-Key
        API->>AOPS: 解析当前UID
        AOPS-->>API: data.uid
        API->>API: 校验管理员/操作员权限
    end
    API->>PG: 只查询当前UID有权访问的数据
    PG-->>API: 权限过滤后的结果
    API-->>U: 响应
```

API Key不进入 URL、命令行参数、日志、MCP工具参数或普通响应。

## 3. 从工单提取、审核并发布知识

```mermaid
sequenceDiagram
    autonumber
    participant O as 操作员
    participant API as Workflow API
    participant CLI as aops-cli
    participant AOPS as AOPS
    participant LLM as 内网LLM
    participant PG as PostgreSQL
    participant E as Embedding
    participant R as 管理员/审核服务

    O->>API: POST /knowledge/extract(ticketId,uids)
    API->>CLI: event-center info / audit_timeline
    CLI->>AOPS: 获取工单详情与操作记录
    AOPS-->>CLI: info JSON + timeline JSON
    CLI-->>API: 工单证据
    API->>API: 过滤失败结果、非sql_exec_read、非只读SQL和重复步骤
    alt 没有有效操作
        API-->>O: 422 NO_VALID_OPERATIONS + 过滤原因
    else 有有效操作
        API->>LLM: 结构化中文提炼
        alt LLM无有效结构
            LLM-->>API: 无效/超时
            API-->>O: LLM_ANALYSIS_FAILED，不创建草稿
        else 分析成功
            LLM-->>API: 名称、摘要、短语、参数语义、依赖
            API->>PG: 创建DRAFT + CREATED生命周期
            PG-->>O: 可编辑DAG草稿
            O->>API: 编辑并提交审核
            API->>PG: PENDING_REVIEW + SUBMITTED事件
            R->>API: 审核并发布
            API->>E: 生成摘要优先向量
            E-->>API: embedding
            API->>PG: 写不可变WorkflowVersion、PUBLISHED事件和索引
            PG-->>R: workflowVersionId
        end
    end
```

## 4. 经验匹配、计划确认与执行

```mermaid
sequenceDiagram
    autonumber
    participant U as 用户
    participant H as Hermes/控制台
    participant MCP as MCP Adapter
    participant API as Workflow API
    participant V as Embedding/Rerank
    participant PG as PostgreSQL
    participant W as Worker
    participant CLI as aops-cli
    participant AOPS as AOPS DB Read

    U->>H: 工单ID + 工单描述
    H->>MCP: knowledge_match
    MCP->>API: POST /knowledge/match
    API->>PG: 当前UID可见的PUBLISHED知识
    API->>V: 全文+向量召回后重排
    V-->>API: 候选和真实分数
    API-->>MCP: 候选摘要/诊断
    MCP-->>H: 单候选或多候选
    H-->>U: 展示候选并要求选择
    U->>H: 选择经验并提供参数
    H->>MCP: workflow_plan
    MCP->>API: 创建计划
    API->>PG: 保存版本快照、运行输入、凭据引用、planHash
    API-->>H: 完整节点、边、SQL计划和planHash
    H-->>U: 展示完整计划
    U->>H: 明确确认
    H->>MCP: workflow_run_approve(planHash)
    MCP->>API: 批准计划
    API->>PG: WAITING_PLAN_APPROVAL → QUEUED
    W->>PG: 加锁领取运行并写租约
    W->>CLI: db read --comment #uatu-ticketId
    CLI->>AOPS: 只读SQL
    AOPS-->>CLI: SSE结果
    CLI-->>W: done=!ok + 行数据
    W->>PG: 加密结果、attempt、事件、checkpoint
```

Hermes不能跳过计划展示，也不能直接调用 `aops-cli`代替平台执行。

## 5. 前置结果绑定与条件分支

```mermaid
sequenceDiagram
    autonumber
    participant W as Worker/LangGraph
    participant PG as Artifact存储
    participant A as SQL节点A
    participant C as Condition节点
    participant B as SQL节点B
    participant D as 默认分支

    W->>A: 使用RUN_INPUT执行
    A-->>PG: 加密保存output.data
    W->>PG: 按NODE_OUTPUT + JSON Pointer读取A结果
    PG-->>W: /data/0/customer_id
    W->>C: 用确定性规则判断A输出
    alt 条件命中B
        C-->>W: route=B
        W->>B: 将A输出绑定为B参数
        W->>PG: 非命中分支标记SKIPPED
    else 无条件命中
        C-->>W: route=默认分支
        W->>D: 执行默认节点
        W->>PG: B及其独占后继标记SKIPPED
    end
```

条件只使用 JSON Pointer和受限运算符，不交给 LLM临场判断；参数只能来自运行输入、人工输入、字面量或可达前置节点输出。

## 6. 实时观察、中断、恢复、取消与重试

```mermaid
sequenceDiagram
    autonumber
    participant U as 用户
    participant H as Web/Hermes
    participant API as Workflow API
    participant PG as PostgreSQL事件
    participant W as Worker
    participant CLI as aops-cli进程组

    loop 运行未结束
        H->>API: SSE Last-Event-ID 或 workflow_run_wait
        API->>PG: 读取sequence之后的已提交事件
        PG-->>API: 事件+事实快照
        API-->>H: 进度、当前节点、等待原因
        H-->>U: 先展示displayText
    end
    alt 用户请求暂停
        U->>H: 暂停
        H->>API: POST /pause
        API->>PG: PAUSE_REQUESTED
        W->>PG: 当前节点结束后的安全边界转PAUSED
    else 节点需要输入或批准
        W->>PG: WAITING_INPUT / WAITING_NODE_APPROVAL
        H-->>U: 展示interrupt请求
        U->>H: 输入或批准
        H->>API: interrupt resume
        API->>PG: QUEUED + resume payload
    else 用户取消
        U->>H: 取消
        H->>API: POST /cancel
        API->>PG: CANCEL_REQUESTED
        W->>CLI: 终止进程组
        W->>PG: CANCELLED
    else 节点FAILED
        W->>PG: errorCode/errorMessage + 加密诊断
        H-->>U: 展示失败原因
        U->>H: 明确选择重试
        H->>API: node retry
        API->>PG: 节点PENDING，运行QUEUED，新建attempt
    else Worker在外部调用期间丢失
        W-xPG: 未能持久化最终结果
        PG-->>API: 租约过期 + STARTED attempt
        API->>PG: UNKNOWN
        H-->>U: 提示请求可能已到达AOPS
        U->>H: 选择重试或标记失败
    end
```

完整诊断只对运行发起人和管理员开放；普通事件和 MCP状态只包含脱敏安全摘要。

## 7. 跨环境导出、导入与路径替换

```mermaid
sequenceDiagram
    autonumber
    participant A as 管理员
    participant UI as 导入导出工作台
    participant API as Workflow API
    participant PG as PostgreSQL
    participant F as 本地JSON文件

    alt 导出
        A->>UI: 选择非删除知识
        UI->>API: export preview
        API->>PG: 读取草稿或当前不可变版本
        API-->>UI: 去重databaseRef、引用位置和阻止项
        A->>UI: 核实并修改目标路径
        UI->>API: confirmed export
        API->>PG: 记录EXPORT审计
        API-->>UI: DAG v2 JSON
        UI->>F: 浏览器下载文件
    else 导入
        A->>UI: 上传v2或旧v1 JSON
        UI->>API: import preview
        API->>API: 校验DAG、只读SQL、绑定、UID和路径
        API->>PG: 查询来源+effectiveContentHash重复项
        API-->>UI: CREATE/SKIP/COPY建议、路径和告警
        A->>UI: 修改路径、创建人、授权UID并确认
        UI->>API: confirmed import
        API->>PG: 单事务创建PENDING_REVIEW + IMPORT审计
        PG-->>UI: 新knowledgeId或跳过项
    else 快速路径替换
        A->>UI: 选择知识和精确路径映射
        UI->>API: replace preview
        API-->>UI: 受影响知识、节点及状态变化
        A->>UI: 二次确认
        UI->>API: confirmed replace
        API->>PG: 单事务替换；PUBLISHED退回PENDING_REVIEW
        API->>PG: PATH_REPLACED生命周期和迁移审计
    end
```

REST导入导出始终传递 JSON对象；只有本地管理页面负责文件上传和下载。包不包含向量、凭据、运行数据、checkpoint或原始审计结果。

## 8. 敏感数据保留与自动清理

```mermaid
sequenceDiagram
    autonumber
    participant W as Worker维护循环
    participant PG as PostgreSQL
    participant CP as LangGraph Checkpoint
    participant O as 管理员

    loop 每30秒维护
        W->>PG: 删除已过期EncryptedArtifact
        W->>PG: 删除已过期RunCredential
        W->>PG: 清空超过保留期的run_inputs/output_refs
        W->>CP: 删除对应thread checkpoint
    end
    O->>PG: 查看长期保留的状态、耗时、审批和事件元数据
    Note over PG: API Key和Authorization Header永不进入日志或普通审计
```

默认敏感数据保留期为30天，可通过 `RESULT_RETENTION_DAYS`调整；生命周期、审批、迁移审计和非敏感运行元数据长期保留。
