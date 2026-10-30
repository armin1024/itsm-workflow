# 版本说明

## 0.13.3：异常SQL逐条隔离与提取完整性诊断

- 解析、参数化、单节点校验及单条SQL超长异常逐条隔离，剩余有效节点继续提取；全部无效时返回NO_VALID_OPERATIONS及已完成的分类诊断，不调用LLM。
- 成功终态和同步预览新增extractionOutcome/publishable；diagnostics增加failedOperationCount/failedOperations/blockingIssues，Workflow结构、五阶段及回报确认协议不变。
- 非法模型依赖和绑定明确提示待核实，不自动替换前置节点；整体流程校验或LLM失败仍整体失败。
- 部分成功独立记录WARNING，可检索原因码与请求；Studio显示异常清单，本地知识保存及导出保留原始诊断。
- 新增extractionFinished/extractionDiagnostics JSON Schema。tec01须配套保存诊断和实施完整性审核，执行器不代替正式发布控制。

## 0.13.2：具体提取失败原因与可检索日志

- 模型字段结构在组装前校验，null数组、缺失/错误类型、非中文内容返回具体安全说明；模型超时、HTTP异常、JSON格式及Workflow规则分别提供reasonCode。
- 失败响应新增errorId及details（阶段、字段、类型、安全代码位置）；未知异常不再回显可能含敏感值的异常原文。
- 发生失败即独立写COMPILER_FAILED/ERROR，不依赖tec01同步；终态同步异常另记事件并保留原错误。无有效SQL时返回过滤计数。
- 日志支持中文原因片段及errorCode/reasonCode/errorField/errorId/stage精确筛选；页面直接显示失败说明，提取页可按错误编号跳到日志。
- 不记录原始SQL/结果/模型内容/凭据，不补造历史缺失诊断；不改变执行、回报重试和人工确认规则。

## 0.13.1：SQL读SSE错误诊断修复

- 显式error/failed/failure事件优先于done校验，缺少done不再掩盖服务真实错误；错误事件与!ok并存仍按失败处理。
- JSON业务失败提取安全错误说明，HTML/XML及其他非SSE输出使用NON_SSE_RESPONSE，不伪装成缺少done；普通日志可检索对应稳定错误码。
- 真正缺少done仍为INVALID_SSE并提示结果不完整、不自动重试；保留退出码、脱敏stdout/stderr，失败不会进入下一节点。
- 保持正常JSON、空行结果、CRLF及无尾部空行SSE兼容；不修改tec01回报或内网aops-cli二进制，不伪造流完成。

## 0.13.0：有界资源与分档部署

- 增加提取admission、实际请求字节/审计/候选/模型输入预检；超限明确413/422，容量不足429，幂等不重复占提取名额。
- 重解析、校验、编码与计费移到有界数据线程，取消等待真实工作完成才归还资源；单运行及全局数据额度包含恢复资料和临时缓存。
- 复用独立tec01/LLM连接池；CLI元数据按文件指纹缓存，读取输出时即检查20MiB/1MiB；日志查询扫描不持写锁。
- IndexedDB v2拆分摘要、详情与输出，逐条事务迁移和只读失败回退；2/5秒活动轮询、四并发、100/200行预览及256MiB软预算，错误暂停重试，清理需人工确认。
- 提供四档性能配置和安全模拟基准；保留单worker、无业务数据库、原回报确认/重试/五阶段及不自动重放SQL规则。

## 0.12.3：tec01回报颗粒度与确认协议对齐

- 提取五阶段各在进入时回报一次，取消起止重复、参数化逐项回报和LLM分析定时心跳；完整过滤账本仍由终态返回，LLM请求超时、回报重试与缓存机制不变。
- 按2026-10-23确认文档采用HTTP200、整数status=200及data.runId匹配的AopsResult，不再要求saved及节点身份回显；保留409立即停止、不重试、不再远程释放回报的机制。
- 终止回报status/runStatus统一TERMINATED，Studio同步显示、筛选和统计该状态；旧CLI错误码和浏览器记录仍可读取，但不再发送CANCELLED状态。
- 增加tec01提取与执行对齐交付文档，统一保存确认、尝试隔离、序号水位、总等待与异常释放约定。

## 0.12.2：日志筛选与后台原文、中文接口文档

- 检索改为填写后统一查询，新增事件/操作中文选项、时间范围和本地时间选择器；空日期不再触发解析错误，字段错误明确显示。
- 新增应用JSONL原文与本服务journalctl入口，固定unit、不使用shell/sudo、不自动扩大主机权限；对原文继续脱敏并限制时间/输出量。
- API摘要、分组、参数说明和校验消息中文化，保留稳定英文路径、字段键和错误码，兼容既有接入。

## 0.12.1：日志调用配对与可读性

- 日志默认合并同次调用的开始和结束，保留原始事件模式；关联事件时间线支持跨列表筛选查看。
- 入站请求和每次外部尝试使用spanId关联，回报重试共用callId；新增精确关联过滤，避免按requestId错误合并不同步骤。
- 增加受控中文操作名称及说明，区分通信结果、业务状态和重试；没有收到响应不伪造状态码，CLI退出码不冒充HTTP。
- 提取终态记录受控businessErrorCode，支持检索LLM_ANALYSIS_FAILED；不补造历史缺失字段、不记录敏感报文。

## 0.12.0：调用日志与自动清理

- 新增只读日志菜单、管理会话保护的游标查询与存储状态API，沿用Carbon界面，不将日志保存到浏览器。
- tec01入站及回报、LLM和AOPS CLI记录安全元数据、耗时、重试与确认结果，不保存正文、SQL、参数或凭据。
- 默认滚动文件7天、最多512MiB、单文件16MiB，启动/轮转/定期清理及写前容量检查；有界后台队列与错误计数避免影响业务。
- 安装器配置日志目录并保留已有日志与显式配置；新增日志运维手册，说明systemd journal独立保留边界。

## 0.11.2：tec01回报协议对齐

- 仅调整回报协议，不改变执行器、提取器、调度、暂停恢复、存储或本地调试架构。
- 运行回报对5xx、连接异常和超时默认最多尝试6次，沿用70秒总等待限制；严格保存确认、4xx和无效确认立即停止规则保持不变。
- 提取回报携带原requestId并纳入幂等键，严格校验AopsResult业务状态和knowledgeId；保留现有序号、心跳、缓存和终态补发机制。
- tec01需先配套保存确认、旧调度409与提取尝试隔离；既有显式配置不自动覆盖。

## 0.11.1

- tec01执行与提取全部回报接口支持路径模板配置，默认保持文档中的接口，保留现有回报与保存确认协议。
- 新增管理会话保护的只读“运行配置”页面，展示生效地址、超时、尝试次数和浏览器存储方式，不展示令牌。
- 升级安装清理旧数据库路径配置；不引入服务端数据库，不删除历史数据文件，本地业务记录继续由浏览器IndexedDB保存。

## 0.11.0

- 对齐内存连续执行：每节点回报获得明确保存确认后继续；生产回调不传整份执行快照。
- 运行回调严格校验HTTP 200、业务status=200、saved=true与运行/调度/节点身份；明确失败停止，仅超时有限重试，默认总3次、总等待70秒。
- 回调失败后不再启动另一轮停止通知重试，释放活动任务并交由tec01核实未知状态；不引入SQLite或退出后的结果补报。
- 增加受鉴权的活动任务查询，仅返回存活与控制信息，供恢复前核实旧执行已结束。
- 本地计划确认与执行分离：无需AOPS Key即可核对确认，确认记录保存在浏览器；执行时验证确认摘要与当前计划一致。
- 新增严格回调、保存确认等待、暂停、活动核实及计划摘要防过期回归测试，补充对齐情况与tec01待实现约定。

## 0.10.1

- 调试统一使用Workflow：从“SQL读→结束”的最小流程开始，经保存知识、创建运行、确认计划后真实执行。
- 移除专用节点调试页面、Node管理页面及对应API，删除独立单节点执行接口和其请求/响应Schema。
- 保留内部Node Registry、Catalog与Workflow/下发/抽取JSON Schema查询，供画布与tec01生成表单和校验请求。
- Studio保留知识抽取、手工新建、可视化编排、浏览器多条知识与计划管理、暂停继续及取消。
- 同步更新独立调试教程、两向tec01接口文档、架构与Linux安装示例。

## 0.10.0

- 按双向接口文档统一经验提取与运行回调，补齐knowledgeId任务、阶段心跳、终态校验摘要、固定幂等键和重试。
- 调试知识、计划、结果与Node显示设置改为浏览器IndexedDB保存，删除服务端SQLite及其依赖和配置。
- 保留多条本地知识与计划管理，支持抽取后编辑、手工编排、导入导出、参数化计划确认和真实执行。
- 统一独立调试与生产下发执行核心，支持逐节点进度、暂停继续、取消移除、失败核实与HITL表单恢复。
- 调试可选同步tec01；未开启时不调用tec01回调。
- 恢复执行中心、创建运行、知识与版本的同级导航和分页检索，保留API文档模块。
- 移除人工候选选择节点，运行节点为SQL读、条件判断、HITL表单与结束。
- 新增JSON Schema查询和下游单节点真实执行接口，固定请求ID在当前进程中幂等。

## 0.9.0

- tec01改为主动提交Compiler任务并主动下发完整Workflow。
- Executor按tec01传入的节点状态继续执行，并逐节点回调状态和结果。
- 增加暂停、取消、容量限制、重复调度保护和执行释放接口。
- 删除旧Compiler Worker、claim客户端、LangGraph依赖和运行时LLM节点。
- 增加SQL行映射型`hitl_select`和多字段`hitl_form`节点。
- Studio画布、Node管理和真实调试同步支持新的HITL节点。

## 0.8.1

- 增加`STUDIO_ADMIN_TOKEN`登录和HttpOnly管理会话。
- 增加Node管理页面，支持已注册节点的Studio启停、名称、说明和调试默认值维护。
- 草稿提取后直接渲染可编辑DAG，并支持选中节点单步调试和整流程调试。
- 增加整流程测试执行器，解析运行输入、节点输出绑定、条件分支和逐节点状态。
- `/docs`改为本地OpenAPI渲染页面，不再依赖公网Swagger资源。

## 0.8.0

- 将服务收敛为Node Registry、Compiler、Runtime Adapter、tec01 Client和独立Studio。
- 删除本地生产知识/检索/运行数据库、MCP、登录权限、Alembic和旧Worker。
- Studio无需管理员登录，提供节点单步调试、可视化DAG校验和工单草稿提取。
- SQL读TEST模式按请求接收`X-AOPS-Api-Key`，凭据不落盘、不进入调试记录。
- 安装包只保留API与可选Compiler Worker，并在升级时停用旧MCP/Worker/Migrate unit。

## 0.7.0

- 增加统一Node Registry、完整Manifest、节点Schema、执行模式和Catalog摘要。
- 增加Runtime内部Catalog、Workflow校验、计划渲染和Compiler预览API。
- 增加独立Studio页面、TEST_ONLY SQLite、TTL临时数据和单节点调试。
- 增加`llm_extract`与`hitl_select`节点、结构化输出、候选选择和受控REFINEMENT回边。
- 增加tec01版本化HTTP客户端和无存储Compiler Worker。
- 保留Monolith兼容模式；未启用tec01时现有知识、MCP和Worker行为不变。

## 0.6.3

- 发布流程显式排除 `._*`、`.DS_Store`、`.AppleDouble` 和 `__MACOSX`，关闭 tar xattr、ACL和 SELinux扩展元数据。
- 安装器在 `/opt/itsm-workflow` 再次清理 AppleDouble文件，防止 Alembic误加载 `._*.py`。
- 发布验收会扫描 macOS元数据；发现污染时直接阻止产包。
- 导入包使用规范化后的 `effectiveContentHash`做重复检测；来源哈希不一致只产生可见告警，不再阻止经人工核实的 JSON包导入。

## 0.6.2

- 修复导出 JSON包重新导入时报 `items[0]内容哈希校验失败`的问题。
- 预检响应增加 `sourceContentHash`、`effectiveContentHash` 和 `contentHashMismatch`。
- 保留人工修改路径、授权UID和通用描述的能力。

## 0.6.1

- 将 Alembic revision缩短为 `0007_diagnostics_transfer`，适配默认 `alembic_version.version_num VARCHAR(32)`。
- 增加 revision长度自动测试，并通过 PostgreSQL 17真实迁移验证。

## 0.6.0

- 增加节点失败诊断、字段级精确检索、知识生命周期、版本清单和原生 DAG跨环境导入导出。
- 增加数据库路径去重映射、快速替换、导入重复识别和迁移审计。
- 此版本的 `0007` revision长度不兼容 PostgreSQL默认 Alembic版本表，不应继续部署，请直接升级到 `0.6.3`。

## 兼容性

- 目标系统：Linux x86_64、glibc 2.17及以上、systemd。
- 持久化：Studio知识、计划和结果保存在浏览器IndexedDB；后台无业务数据库，生产数据和运行状态由tec01持有。
- `aops-cli`由目标机提供，不包含在离线包中。
- 旧 `aops-workflow-knowledge-export/schemaVersion=1` 知识包仍可导入；新包使用 `itsm-workflow-export/schemaVersion=2`。
