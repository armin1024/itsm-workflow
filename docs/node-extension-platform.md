# 通用节点与卡片扩展平台设计

## 1. 结论

支持多类别卡片完全可行，但不建议允许管理员在控制台上传任意 Python、Shell、JavaScript或命令模板。

推荐拆成三层：

```text
节点类别 Category
  └── 仅用于导航、图标、颜色和权限分组

节点能力 Capability / Handler
  └── 由受信代码包注册，真正决定校验、执行、恢复和风险语义

节点模板 Template
  └── 管理员在控制台基于已注册能力创建，可配置名称、表单、默认值和适用范围
```

控制台可以快速创建“不同类别、不同用途”的卡片模板，但不能凭数据库中的一段文本获得任意执行能力。新增底层能力仍需安装经过测试和签名校验的 Handler包。

## 2. 为什么不能只做一张万能卡片

一张接受任意命令、参数和返回值的“通用执行卡片”虽然开发快，但会破坏当前平台的关键保证：

- 无法在发布前判断是否为只读或高风险操作。
- 无法确定取消是否安全、失败能否重试。
- 无法为 `UNKNOWN` 实现正确的协调逻辑。
- 任意命令容易产生 Shell注入、SSRF和凭据泄漏。
- Agent无法理解输入输出 Schema，不能可靠收集参数或绑定前置结果。
- 管理员修改模板可能改变历史运行语义。

因此“通用”应该体现在统一协议、统一管理和 Schema驱动 UI，而不是任意执行。

## 3. 目标模型

### 3.1 Category：类别

类别是管理和展示元数据：

```json
{
  "categoryKey": "data-query",
  "name": "数据查询",
  "description": "读取数据库、缓存或内部查询接口",
  "icon": "database",
  "colorToken": "teal",
  "sortOrder": 10,
  "enabled": true
}
```

管理员可以通过控制台增删改查类别。类别不包含执行代码，不影响历史版本。

建议初始类别：

| 类别 | 示例节点 |
|---|---|
| 数据查询 | SQL读、Redis读、日志检索、指标查询 |
| AOPS操作 | 事件查询、文件分发、巡检、资源操作 |
| 流程控制 | 条件、并行、汇聚、循环、结束 |
| 数据处理 | JSON提取、映射、过滤、模板转换 |
| 人工协作 | 补充输入、计划审批、风险审批 |
| 通知输出 | IM通知、邮件、工单时间线、结果归档 |

### 3.2 Capability：执行能力

Capability由代码注册，是安全边界：

```json
{
  "capabilityKey": "aops.sql.read",
  "handlerKey": "sql_read",
  "handlerVersion": "1.0.0",
  "riskLevel": "LOW",
  "sideEffect": "READ_ONLY",
  "retryPolicy": "MANUAL",
  "cancelMode": "PROCESS_GROUP",
  "unknownPolicy": "RECONCILE_OR_MANUAL",
  "configSchema": {},
  "inputSchema": {},
  "outputSchema": {}
}
```

Capability必须实现：

```text
validate_definition()
validate_runtime_inputs()
prepare()
execute()
summarize()
reconcile()
redact()
```

职责：

- `validate_definition`：保存和发布前检查配置。
- `validate_runtime_inputs`：执行前检查类型、范围和权限。
- `prepare`：生成只读的安全执行计划。
- `execute`：调用受控 SDK、HTTP客户端或无 Shell子进程。
- `summarize`：生成不泄漏敏感结果的节点摘要。
- `reconcile`：Worker丢失后查询外部系统，判断成功、失败或 UNKNOWN。
- `redact`：定义日志、事件和 UI中的脱敏规则。

### 3.3 Node Type：节点类型版本

节点类型将 Capability包装成可被工作流引用的不可变版本：

```json
{
  "nodeTypeKey": "customer-sql-query",
  "version": 3,
  "categoryKey": "data-query",
  "capabilityKey": "aops.sql.read",
  "name": "客户信息查询",
  "description": "通过审核后的只读SQL查询客户资料",
  "rendererKey": "schema-form",
  "configSchema": {},
  "uiSchema": {},
  "inputSchema": {},
  "outputSchema": {},
  "status": "PUBLISHED"
}
```

工作流节点必须固定引用：

```text
nodeTypeKey + nodeTypeVersion
```

不能只引用可变的 `type` 字符串。管理员发布新版本后，历史工作流继续使用旧版本。

### 3.4 Template：节点模板

模板是管理员可无代码创建的卡片：

```json
{
  "templateKey": "crm-customer-by-name",
  "nodeTypeKey": "customer-sql-query",
  "nodeTypeVersion": 3,
  "name": "按姓名查询客户",
  "defaults": {
    "databaseRef": "124/crm/crm/readonly/customer-service",
    "sqlTemplate": "SELECT user_id FROM users WHERE name={{customer_name}}"
  },
  "lockedFields": ["databaseRef"],
  "systemKeys": ["crm"],
  "allowedCreatorUids": ["S000001"]
}
```

管理员可以创建多个模板复用同一个安全 Handler。例如 SQL读能力可以派生：

- 按姓名查询客户。
- 查询订单状态。
- 查询账号锁定情况。
- 查询服务配置。

模板只提供受 Schema约束的配置和默认值。

## 4. 数据模型建议

### `node_categories`

```text
key PK
name
description
icon_key
color_token
sort_order
enabled
created_by
created_at
updated_at
```

### `node_capabilities`

```text
key PK
handler_key
handler_version
risk_level
side_effect
retry_policy
cancel_mode
unknown_policy
manifest_hash
installed_at
enabled
```

Capability记录来自启动时的代码注册表，控制台只能启用或停用，不能修改 Handler代码。

### `node_type_versions`

```text
id PK
type_key
version
category_key FK
capability_key FK
name
description
renderer_key
config_schema JSONB
ui_schema JSONB
input_schema JSONB
output_schema JSONB
risk_policy JSONB
status
content_hash
created_by
created_at
published_by
published_at
```

唯一约束：

```text
(type_key, version)
```

### `node_templates`

```text
id PK
template_key
node_type_version_id FK
name
description
default_config JSONB
locked_fields JSONB
system_keys JSONB
visibility
status
created_by
created_at
updated_at
```

### `node_template_users`

与知识 UID语义一致：没有记录表示所有已认证用户可见；有记录表示指定 UID共享。

### `node_type_audits`

记录创建、编辑、测试、发布、停用、恢复和模板导入导出。

## 5. Manifest契约

当前 `NodeTypeManifest` 建议升级为：

```python
class NodeCapability(Protocol):
    key: str
    version: str
    risk_level: RiskLevel
    side_effect: SideEffect
    config_schema: dict
    input_schema: dict
    output_schema: dict

    def validate_definition(self, config: dict) -> None: ...
    def validate_runtime_inputs(self, values: dict) -> None: ...
    async def prepare(self, context, config, inputs) -> PreparedAction: ...
    async def execute(self, context, prepared) -> NodeResult: ...
    async def reconcile(self, context, attempt) -> ReconcileResult: ...
    def summarize(self, result) -> SafeSummary: ...
    def redact(self, value) -> dict: ...
```

统一执行结果：

```json
{
  "status": "SUCCEEDED",
  "data": {},
  "metrics": {
    "itemCount": 1,
    "durationMs": 2300
  },
  "safeSummary": "查询完成，返回1行",
  "externalReference": "request-uuid",
  "retriable": false
}
```

Handler不能自行修改运行状态；它只返回结果，Engine统一写 attempt、artifact、事件和 checkpoint。

## 6. Schema驱动的通用卡片 UI

### 6.1 不加载数据库中的任意前端代码

控制台不允许上传或保存可执行 JavaScript。页面使用内置 renderer：

```text
schema-form        通用JSON Schema表单
sql-editor         SQL编辑器和参数同步
condition-builder  条件规则编辑器
mapping-editor     JSON Pointer映射
approval-editor    审批策略
connector-form     已注册连接器选择
```

`rendererKey` 只能从前端编译时注册表选择。

### 6.2 uiSchema

`configSchema` 决定数据是否合法，`uiSchema` 只决定如何展示：

```json
{
  "layout": ["databaseRef", "sqlTemplate", "timeoutSeconds"],
  "widgets": {
    "databaseRef": "database-picker",
    "sqlTemplate": "sql-editor",
    "timeoutSeconds": "duration-input"
  },
  "help": {
    "sqlTemplate": "只允许单条只读SQL"
  }
}
```

前端保存前做 JSON Schema校验，服务端再执行同一 Schema和 Handler校验。

### 6.3 卡片视觉规范

所有节点保持一致外框和高度，不因配置内容改变画布尺寸。卡片固定展示：

```text
类别图标 / 节点类型 / 风险标识
节点标题
一行安全摘要
输入数 / 数据绑定数 / 审批策略
输入端口 / 输出端口
```

详细配置只在右侧检查器展示。不同类型使用设计 Token，不允许模板保存任意 CSS。

## 7. 控制台功能设计

新增一级菜单：

```text
节点中心
```

二级页面：

### 类别管理

- 类别增删改查和排序。
- 图标、颜色 Token和说明。
- 被类型引用的类别不能直接删除，只能停用。

### 能力清单

- 展示已安装 Handler版本、哈希、风险等级和健康状态。
- 查看配置、输入、输出 Schema。
- 启用/停用。
- 查看依赖的节点类型和工作流版本。
- 不提供在线代码编辑。

### 节点类型

- 选择 Capability和类别。
- 编辑名称、说明、Schema、uiSchema和审批下限。
- 在线校验示例配置。
- 保存草稿、提交审核、发布新版本、弃用。

### 节点模板

- 基于已发布节点类型创建。
- 编辑默认配置和锁定字段。
- 设置系统范围、UID可见性和创建权限。
- 预览真实画布卡片和右侧表单。
- 导入导出。

### 节点调试

- 输入模拟运行参数和前置输出。
- 只执行 `validate` 和 `prepare`。
- 默认不调用生产外部系统。
- 测试环境可在二次确认后执行 Handler。
- 展示准备结果、脱敏摘要和输出 Schema校验。

## 8. 生命周期

节点类型版本：

```text
DRAFT → TESTING → PENDING_REVIEW → PUBLISHED → DEPRECATED → DISABLED
```

- `DRAFT`：仅编辑者可见。
- `TESTING`：允许使用模拟输入验证。
- `PENDING_REVIEW`：配置冻结，等待审核。
- `PUBLISHED`：可在工作流中使用。
- `DEPRECATED`：旧工作流可继续运行，新工作流不能新增。
- `DISABLED`：禁止创建新运行；是否终止已有运行由风险策略决定。

模板生命周期：

```text
DRAFT → PUBLISHED → DISABLED
```

## 9. 发布校验

节点类型发布前检查：

- Capability已安装且版本匹配。
- JSON Schema合法且无外部网络 `$ref`。
- 默认配置通过 Schema和 Handler校验。
- 输出 Schema和绑定路径规则兼容。
- 风险等级不能低于 Capability声明。
- 有副作用能力不能取消节点审批下限。
- 凭据只引用服务端 Connector ID，不包含明文。
- rendererKey存在于前端注册表。
- Manifest和内容哈希已生成。

工作流发布前新增检查：

- 所有节点固定到已发布版本。
- 不引用已禁用类型。
- 数据绑定的源输出 Schema包含目标路径。
- 目标输入类型与源输出类型兼容。
- 条件操作符适用于路径类型。
- 并行分支的汇聚语义明确。

## 10. 权限与风险

角色建议：

```text
NODE_CATEGORY_ADMIN
NODE_TEMPLATE_EDITOR
NODE_TYPE_REVIEWER
NODE_CAPABILITY_ADMIN
WORKFLOW_EDITOR
WORKFLOW_OPERATOR
```

风险等级：

| 等级 | 示例 | 最低审批 |
|---|---|---|
| LOW | SQL读、指标查询、JSON转换 | 计划确认 |
| MEDIUM | 文件读取、配置查询、通知 | 计划确认或节点确认 |
| HIGH | 文件分发、服务操作、生产变更 | 节点审批 |
| CRITICAL | 批量变更、数据写入、流量切换 | 双人审批与时间窗口 |

模板不能降低 Capability规定的风险和审批下限。

## 11. Connector与凭据

未来非 SQL节点通常需要连接器。增加：

```text
connector_types
connectors
connector_permissions
encrypted_connector_credentials
```

工作流节点只保存：

```json
{"connectorId":"conn_crm_prod"}
```

不保存 URL中的密码、Token、私钥或完整 Header。Handler执行前按当前 UID、环境和节点类型检查连接器权限。

通用 HTTP能力必须限制：

- URL来自已注册 Connector，模板不能填写任意 Host。
- 方法和路径有 allowlist。
- 禁止访问 loopback、metadata和内部管理网段。
- Header来自连接器配置，不允许用户动态注入 Authorization。
- 响应大小、超时和重定向受限。

## 12. 执行与恢复

所有节点继续使用统一 attempt ledger：

```text
PREPARED → STARTED → SUCCEEDED / FAILED / UNKNOWN
```

每个 Capability必须声明：

- 是否幂等。
- 是否允许自动重试。
- 如何取消。
- 如何查询外部请求状态。
- Worker丢失后如何 reconcile。

默认规则：

- 所有生产节点均不自动重试。
- 无 reconcile实现的外部节点在 Worker丢失后进入 UNKNOWN。
- `READ_ONLY` 也保留人工重试，避免重复审计。
- 控制节点和纯转换节点可在显式声明 `PURE` 后安全重放。

## 13. 首批扩展建议

### 第一组：无副作用控制节点

优先实现：

- `json_extract`：通过 JSON Pointer提取值。
- `data_mapping`：字段映射和类型转换。
- `condition` 增强：可视化 all/any/not。
- `merge`：汇聚条件分支。
- `human_input` 完整表单化。
- `approval` 完整风险信息展示。

这些节点风险低，适合先验证通用 Schema和 UI框架。

### 第二组：只读查询节点

- Redis读。
- HTTP/API读。
- 日志检索。
- 指标查询。
- AOPS事件、资产、任务状态查询。

这些节点复用当前 SQL读的结果加密、摘要和人工重试语义。

### 第三组：受控副作用节点

- 工单时间线追加。
- IM通知。
- 文件分发。
- 服务重启。
- SQL写审核提交。

必须先完成 Connector、节点审批、幂等键、reconcile和双人审核能力。

### 第四组：高级控制流

- 并行分支和显式 `join`。
- 有上限的 `foreach`。
- 有最大次数的 `loop`。
- 子工作流节点。
- 补偿节点。

不能直接允许任意循环；必须配置最大迭代、最大节点执行数、总体超时和取消传播。

## 14. 导入导出

节点类型和模板使用版本化 JSON包：

```json
{
  "schema": "itsm-workflow-node-package",
  "schemaVersion": 1,
  "sourceEnvironment": "test",
  "categories": [],
  "nodeTypes": [],
  "templates": []
}
```

不导出：

- Handler代码和 Python包。
- Connector凭据。
- 环境实际 Token。
- 运行数据和 artifact。

目标环境必须已经安装匹配 `capabilityKey + handlerVersion + manifestHash` 的 Handler，否则只能预览，不能发布。

## 15. MCP与 Agent

新增只读工具：

```text
node_category_list
node_type_list
node_template_list
```

Agent创建计划时不需要理解 Handler内部实现，只使用节点输入 Schema收集 `RUN_INPUT`。MCP返回：

- 节点类型、版本和类别。
- 风险等级和审批要求。
- 输入来源及类型。
- 安全计划摘要。
- 完整工作流节点和边。

Agent不能通过 MCP创建或发布节点类型；管理类能力继续使用 Web和管理 REST，避免生产 Agent扩大权限。

## 16. 向后兼容

当前节点自动映射：

```text
sql_read     → capability aops.sql.read / type builtin.sql_read@1
condition    → capability control.condition / type builtin.condition@1
human_input  → capability human.input / type builtin.human_input@1
approval     → capability human.approval / type builtin.approval@1
end          → capability control.end / type builtin.end@1
```

迁移时：

1. 创建内置类别、Capability和节点类型版本。
2. 为历史工作流版本补充 nodeTypeKey和版本。
3. 保留原 `type` 字段作为兼容读取字段。
4. 内容哈希和历史运行快照不重写。
5. 新发布工作流必须使用版本化引用。

## 17. 实施阶段

### 阶段 A：注册表持久化

- 新增类别、Capability、节点类型版本、模板和审计表。
- 启动时将代码 Manifest与数据库登记信息对账。
- 内置节点自动初始化。
- 提供只读注册表 API。

### 阶段 B：Schema通用表单

- 实现 `schema-form` renderer。
- 建立 rendererKey白名单。
- 控制台增加节点中心。
- 完成类别和模板 CRUD。
- 支持模板预览和双重校验。

### 阶段 C：版本与审核

- 节点类型草稿、测试、审核、发布、弃用和停用。
- 工作流固定 nodeTypeVersion。
- 增加输入输出 Schema静态绑定检查。
- 导入导出和环境兼容预检。

### 阶段 D：首批新节点

- JSON提取、映射、增强条件、人工输入和审批。
- Redis读、HTTP/API读、日志和指标查询。
- 统一结果 Schema、摘要和 reconcile测试。

### 阶段 E：高风险与高级流程

- Connector和加密凭据。
- 双人审批、执行窗口和策略引擎。
- 并行、join、有限循环、子工作流和补偿。

## 18. 验收标准

- 管理员能创建类别和基于已安装 Capability的模板。
- 控制台无法注入任意代码、命令、Authorization Header或未注册 URL。
- 节点类型和模板修改不影响已发布工作流版本。
- 工作流发布能静态验证前后节点的数据类型和 JSON Pointer。
- Handler失败、超时、取消、Worker丢失和重试均产生标准事件。
- 禁用 Capability后不能创建新运行，历史审计仍可查看。
- Agent可基于 Schema收集参数并展示风险，但不能管理节点类型。
- 不安装对应 Handler的环境无法发布导入模板。
- 新类型接入不需要修改调度核心和通用画布。

## 19. 长期展望

### 企业节点市场

建立内部节点包仓库，节点包包含：

- 签名 Manifest。
- Handler代码。
- JSON Schema和示例。
- 数据脱敏声明。
- 权限与网络访问声明。
- 单元、契约和故障恢复测试。

安装前执行签名、依赖、许可证和危险权限扫描。

### 策略即代码

在发布和运行前执行组织策略：

```text
生产写操作必须双人审批
夜间禁止流量切换
某系统只能由指定UID操作
跨环境导入必须重新绑定Connector
```

策略结果是确定性允许或拒绝，不交给 LLM决定。

### 自动经验提炼

工单审计记录可识别多种已注册 Capability：

```text
sql_exec_read → SQL读节点
redis_read    → Redis读节点
http_query    → API读节点
file_transfer → 文件分发节点
```

程序先按操作类型提取不可变证据，LLM只负责名称、摘要、参数语义、步骤关系和模板推荐。无法映射到已安装 Capability的操作只展示为未支持证据，不生成可执行节点。

### 可观察工作流资产

后续可按节点类型统计：

- 调用量、成功率和 P95耗时。
- 等待审批和补参时长。
- UNKNOWN比例和 reconcile成功率。
- 模板命中率和人工修改率。
- 不同版本的失败趋势。

指标用于改进模板和 Handler，不自动改变生产工作流。
