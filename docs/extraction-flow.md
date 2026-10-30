# 草稿提取流程图

适用：itsm-workflow 0.10.1与tec01生产对接。tec01侧的页面、存储、审核和发布为下游需承接的职责；本仓库不包含tec01实现。

```mermaid
flowchart TD
    A[用户：提交工单提取请求] --> B[tec01：获取工单详情和操作记录<br/>创建提取记录与知识ID]
    B --> C[tec01主动提交JSON证据<br/>ticketInfo对象 + auditTimeline数组]
    C --> D{执行器：请求与工单ID校验}
    D -->|不通过| E[返回请求错误<br/>tec01展示原因，不启动提取]
    D -->|通过| F[立即返回已受理<br/>后台开始提取]

    F --> G[过滤操作记录<br/>仅保留成功、安全的SQL读操作]
    G --> H[按数据库与SQL去重<br/>提取数据库路径、参数化SQL]
    H --> I[调用内网LLM<br/>总结中文名称、摘要、匹配规则和参数语义]
    I --> J[组装结构化Workflow草稿<br/>SQL节点、输入、绑定和连线]
    J --> K[执行器：校验Workflow<br/>DAG、节点配置、只读SQL、参数依赖]
    K --> L[回调提取成功<br/>草稿 + 校验摘要 + 过滤明细]
    L --> M[tec01：保存草稿并展示可视化流程]
    M --> N[用户：编辑参数、数据库路径、节点与连线]
    N --> O[tec01：提交执行器做发布前校验]
    O --> P{校验是否通过}
    P -->|否| N
    P -->|是| Q[tec01：完成审核并保存不可变发布版本<br/>规范化定义 + 内容哈希 + 节点目录摘要]

    G -. 无有效操作 .-> X[回调提取失败<br/>错误类别与安全说明]
    H -. 参数化异常 .-> X
    I -. 模型不可用或结构化结果无效 .-> X
    J -. 草稿组装失败 .-> X
    K -. 校验失败 .-> X
    X --> Y[tec01：保存失败状态<br/>用户修正输入或配置后重新提取]

    R[执行器回报阶段与实际处理进度<br/>分析阶段只在进入时回报，不发送心跳] -.-> B
    F -. 提取期间 .-> R
```

## 图中交互约定

- **谁发起**：tec01主动传入工单JSON，不是执行器轮询或拉取tec01任务。
- **输入格式**：`ticketInfo=info.data`对象，`auditTimeline=audit_timeline.data`数组；`ticketId`必须为正整数且与`ticketInfo.id`一致。tec01预先生成`knowledgeId`和`requestId`。
- **提取接口**：`POST /internal/v1/extractions/{knowledgeId}`，202表示受理，不表示成功。
- **回调接口**：执行器调用tec01的`POST /internal/v1/extractions/{knowledgeId}/callbacks`；`PROGRESS`传阶段，`FINISHED`传最终成功或失败。
- **过滤顺序**：仅`sql_exec_read`、result明确成功、details/SQL可解析且为安全单条只读SQL的操作进入候选；按时间整理，参数化阶段再去重。失败、无效及重复操作记录原因。
- **当前自动草稿能力**：提取成功的SQL读步骤，生成线性SQL流程并追加结束节点；不会自动推断业务条件或自动生成完整HITL分支。需要时由用户在审核编辑中补充`condition/hitl_form`及结果绑定。
- **LLM用途**：只辅助草稿分析和通用语义提炼，不执行SQL，也不是运行时节点。模型分析失败不自动生成可发布草稿。
- **重试**：同知识ID、同requestId、同证据的重复提交不重复编译；提取失败后重新分析，使用同知识ID与新的requestId。当前去重记录是有限期进程内缓存，tec01仍需持久化并做幂等控制。
- **回调未确认**：提取结果与同步状态分开处理，不能把回调网络失败直接视为提取失败；终态回调可在缓存仍有效时按原请求重新投递。tec01按幂等键接收，不重复创建草稿。

## 独立Studio调试的区别

使用Studio时，浏览器代替tec01保存本地提取结果和知识，后台仍无业务数据库。默认不回调tec01，页面读取临时事件缓存；开启同步后才发送上述回调。Studio可直接粘贴JSON，也可临时提供`AOPS_API_KEY`，由CLI根据工单ID获取证据。

接口详情：[执行器提供的API](itsm-workflow-to-tec01-api.md)、[tec01回调API](tec01-to-itsm-workflow-api.md)。
