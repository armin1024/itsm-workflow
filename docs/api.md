# REST API

Base URL示例：

```text
http://itsm-workflow.internal:8089/api/v1
```

三方请求必须同时传：

```http
Authorization: Bearer <WORKFLOW_API_TOKEN>
X-AOPS-Api-Key: <当前用户AOPS_API_KEY>
```

UI使用 `/auth/session` 建立 HttpOnly加密会话。

## MCP Streamable HTTP

独立 MCP服务默认监听：

```text
http://127.0.0.1:8090/mcp
```

反向代理后建议使用：

```text
http://workflow.internal:8089/mcp
```

连接必须携带与 REST相同的 `Authorization` 和 `X-AOPS-Api-Key`。当前提供13个工具，包含紧凑增量等待和节点结果分页读取；完整 Schema、Hermes配置和操作约束见 [MCP 与 Agent 接入指南](mcp-agent-integration.md)。健康检查为 `GET http://127.0.0.1:8090/health`。

## 知识与版本

```text
POST /knowledge
PATCH /knowledge/{knowledgeId}
DELETE /knowledge/{knowledgeId}
POST /knowledge/extract
GET  /knowledge
GET  /knowledge/{knowledgeId}
POST /knowledge/{knowledgeId}/publish
POST /knowledge/match
POST /knowledge/import-legacy
GET  /workflow-versions/{workflowVersionId}
GET  /workflow-versions
GET  /node-types
POST /transfers/export/preview
POST /transfers/export
POST /transfers/import/preview
POST /transfers/import
POST /database-paths/replace/preview
POST /database-paths/replace
GET  /transfer-audits
```

### 知识列表分页

```http
GET /api/v1/knowledge?page=1&pageSize=20&keyword=客户查询&status=PENDING_REVIEW
```

- `page` 从1开始，越界时返回最后一个有效页。
- `pageSize` 只允许20、50、100。
- `status` 允许空值、`DRAFT`、`PENDING_REVIEW`、`PUBLISHED`。
- 关键词精确匹配知识ID、来源工单ID和事件编号，模糊匹配名称、摘要、匹配短语和创建人UID。
- 权限过滤在数据库分页前完成；`DELETED` 永不出现在列表。
- 列表项是轻量摘要，只包含 `nodeCount`，完整 `workflowDefinition` 通过详情接口读取。

统一分页响应：

```json
{"items":[],"page":1,"pageSize":20,"total":138,"totalPages":7}
```

### 草稿提交与服务审核

已认证操作员可以通过工单摄入生成草稿，并管理自己创建的 `DRAFT`：

```http
POST /api/v1/knowledge/extract
POST /api/v1/knowledge/{knowledgeId}/submit-review
```

提交后状态变为 `PENDING_REVIEW`，发布前不参与 MCP匹配。普通操作员不能编辑已经提交的草稿。

管理员可以使用登录会话审核；三方审核平台使用独立服务端审核 Token，不需要 `X-AOPS-Api-Key`：

```http
Authorization: Bearer <WORKFLOW_REVIEW_TOKEN>
```

审核队列：

```bash
curl -H 'Authorization: Bearer <WORKFLOW_REVIEW_TOKEN>' \
  'http://workflow.internal:8089/api/v1/reviews/knowledge'
```

查看待审核详情：

```http
GET /api/v1/reviews/knowledge/{knowledgeId}
```

审核通过并发布：

```bash
curl -X POST \
  -H 'Authorization: Bearer <WORKFLOW_REVIEW_TOKEN>' \
  -H 'Content-Type: application/json' \
  -d '{}' \
  'http://workflow.internal:8089/api/v1/knowledge/{knowledgeId}/publish'
```

退回草稿：

```bash
curl -X POST \
  -H 'Authorization: Bearer <WORKFLOW_REVIEW_TOKEN>' \
  -H 'Content-Type: application/json' \
  -d '{"reason":"参数语义需要修正"}' \
  'http://workflow.internal:8089/api/v1/reviews/knowledge/{knowledgeId}/reject'
```

服务审核记录使用服务器配置的 `WORKFLOW_REVIEW_ACTOR_UID` 作为 `reviewedBy`。审核 Token只能访问上述审核接口及发布接口，不能创建运行、读取节点结果或控制执行。

### 从工单提取草稿

```http
POST /api/v1/knowledge/extract
Authorization: Bearer <WORKFLOW_API_TOKEN>
X-AOPS-Api-Key: <当前用户AOPS_API_KEY>
Content-Type: application/json
```

```json
{"ticketId":100173,"uids":["S000123","S000456"]}
```

服务使用该用户凭据调用系统安装的 `aops-cli` 获取工单详情和操作记录。`uids=[]` 表示公开。成功响应包含 `knowledge` 草稿和 `extraction` 过滤统计；没有有效 SQL读记录或 LLM失败时返回 422，且不创建草稿。

### 更新草稿与节点注册表

`PATCH /knowledge/{knowledgeId}` 的请求体与 `POST /knowledge` 相同。保存后知识回到 `DRAFT`，旧发布版本保持不变。

管理员可以重新编辑已发布经验。保存会生成新的工作草稿并暂时退出匹配，经过 `submit-review` 后再次发布会创建递增的不可变版本；历史运行继续使用原版本快照。

删除经验：

```http
DELETE /api/v1/knowledge/{knowledgeId}
```

采用可审计软删除：经验从清单、审核队列、全文/向量检索和 MCP匹配中消失，但历史工作流版本与运行记录保留。管理员可删除任意经验；普通操作员只能删除自己尚未提交的 `DRAFT`。独立审核 Token没有删除权限。

`GET /node-types` 返回运行时节点注册表的配置、输入和输出 Schema。`enabledForAuthoring` 表示当前 UI是否开放创建该类型。首版 UI开放 `sql_read`、`condition` 和 `end`；引擎还支持由 API 定义的 `human_input`、`approval`。

管理画布当前开放 `sql_read`、`condition` 和 `end`。控制流由 `edges[]` 表示；数据依赖由节点输入单独表示：

```json
{"name":"customer_id","type":"integer","source":{"kind":"NODE_OUTPUT","nodeId":"sql-1","jsonPointer":"/data/0/id"}}
```

服务端要求 `nodeId` 在控制流上确实能到达当前节点。条件节点的非默认出线使用：

```json
{"source":"condition-1","target":"sql-active","label":"客户有效","condition":{"path":"/nodes/sql-1/output/data/0/status","op":"eq","value":"ACTIVE"}}
```

同一条件节点必须且只能有一条 `default: true` 的默认出线。

`/knowledge/match` 使用摘要优先的中文词项召回、BGE-M3向量和 rerank重排；响应包含 `lexicalScore`、`vectorScore`、`rerankScore` 及降级诊断。`RERANK_API_FORMAT=auto` 会依次兼容 OpenAI、TEI和旧版 documents对象格式。

创建经验：

```json
{
  "name":"客户状态查询",
  "summary":"按客户姓名查询客户当前状态",
  "matchPhrases":["客户状态查询"],
  "negativePhrases":[],
  "systemKeys":["crm"],
  "uids":[],
  "workflowDefinition":{
    "schemaVersion":1,
    "entryNodeId":"sql-1",
    "nodes":[{
      "id":"sql-1",
      "type":"sql_read",
      "title":"读取客户状态",
      "config":{
        "databaseRef":"124/crm/crm/readonly/customer-service",
        "sqlTemplate":"SELECT id,status FROM customers WHERE name={{customer_name}}"
      },
      "inputs":[{
        "name":"customer_name",
        "type":"string",
        "source":{"kind":"RUN_INPUT","key":"customer_name"}
      }],
      "approvalPolicy":"PLAN",
      "timeoutSeconds":600
    }],
    "edges":[]
  }
}
```

发布会生成新的不可变 `workflowVersionId`；运行始终保存版本快照。

## 运行

运行列表分页：

```http
GET /api/v1/runs?page=1&pageSize=20&keyword=100173&statusGroup=ACTIVE
```

`statusGroup` 支持 `ALL/ACTIVE/WAITING/SUCCEEDED/FAILED`。纯数字关键词精确匹配工单ID，`run_`开头匹配运行ID，其他文本匹配运行ID、经验名称和发起人UID。列表只返回状态和进度摘要，不包含workflow、attempt、interrupt或事件。

```text
POST /runs/plan
POST /runs/{runId}/approve
GET  /runs
GET  /runs/{runId}
GET  /runs/{runId}/events
GET  /runs/{runId}/wait
POST /runs/{runId}/pause
POST /runs/{runId}/resume
POST /runs/{runId}/cancel
POST /runs/{runId}/credential
POST /runs/{runId}/interrupts/{interruptId}/resume
POST /runs/{runId}/nodes/{nodeId}/retry
GET  /runs/{runId}/nodes/{nodeId}/artifact
```

生成计划：

```json
{
  "knowledgeId":"knw_xxx",
  "ticketId":100173,
  "parameters":{"customer_name":"王五"}
}
```

确认计划：

```json
{"planHash":"<plan响应中的64位哈希>"}
```

恢复人工输入：

```json
{"payload":{"inputs":{"customer_id":7021}}}
```

批准节点：

```json
{"payload":{"decision":"approve"}}
```

失败节点重试：

```json
{"decision":"retry"}
```

节点必须为 `FAILED` 或 `UNKNOWN`。重试仅重新调度当前节点，保留已经成功的节点结果，并新增一次 attempt。结果未知的节点还可提交 `{"decision":"mark_failed"}`；平台不会自动重放未知的外部操作。

SSE支持 `Last-Event-ID`，事件包含运行、节点、attempt、状态、安全摘要和进度：

```http
GET /api/v1/runs/run_xxx/events
Last-Event-ID: 1820
Accept: text/event-stream
```

```text
id: 1821
event: NODE_SUCCEEDED
data: {"runId":"run_xxx","nodeId":"sql-1","status":"SUCCEEDED","safeSummary":"查询客户编号执行成功","progressCurrent":1,"progressTotal":4}
```

浏览器断线后使用最新事件 ID继续订阅。三方 Agent不建议永久占用 SSE连接；MCP Adapter通过最长10–15秒的 `workflow_run_wait` 返回新事件和当前运行快照。
三方 Agent使用 `GET /runs/{runId}/wait?afterEventId=<sequence>&timeoutSeconds=10`；最长等待15秒，返回增量事件和同一时刻的最新事实快照。

## 创建运行页面参数

管理页面根据已发布经验的节点输入动态生成运行参数表单：

- 只展示 `source.kind=RUN_INPUT` 的参数。
- 相同运行参数键跨节点复用时只显示一次。
- `NODE_OUTPUT` 和 `LITERAL` 不要求用户填写。
- `string/integer/number/boolean` 使用对应控件。
- `object/array` 使用单字段 JSON编辑器并在提交前解析。

## 运行状态

```text
WAITING_PLAN_APPROVAL / QUEUED / RUNNING / PAUSE_REQUESTED / PAUSED
WAITING_INPUT / WAITING_NODE_APPROVAL / WAITING_CREDENTIAL
SUCCEEDED / FAILED / CANCEL_REQUESTED / CANCELLED / UNKNOWN
```

`UNKNOWN` 表示 Worker可能已将请求发送给 AOPS，但未能持久化结果；平台不会自动重试。

## 运维

```text
GET  /api/v1/health
GET  /metrics
POST /api/v1/admin/retention/run
```

## Runtime内部API与节点Studio

Runtime内部接口使用独立`RUNTIME_SERVICE_TOKEN`：

```text
GET  /internal/v1/runtime/catalog
POST /internal/v1/runtime/workflows/validate
POST /internal/v1/runtime/workflows/plan
POST /internal/v1/compiler/preview
```

Catalog返回Node Manifest、配置/输入/输出Schema、UI Schema、Handler版本和四种执行模式。`If-None-Match`支持Catalog摘要缓存。

Studio接口仅管理员可用：

```text
GET  /api/v1/studio/catalog
GET  /api/v1/studio/workspaces
POST /api/v1/studio/workspaces
POST /api/v1/studio/node-debug-runs
GET  /api/v1/studio/node-debug-runs/{debugRunId}
POST /api/v1/studio/node-debug-runs/{debugRunId}/interrupts/reply
```

单节点调试只允许`TEST/SIMULATION/DRY_RUN`，结果标记`testOnly=true`并写入独立TTL SQLite，不修改生产运行。

生产DAG支持`llm_extract`、`hitl_select`和受控`REFINEMENT`边。REFINEMENT只能从HITL回到上游LLM，要求1至5次最大迭代和可选的反馈RUN_INPUT。

## 精确查询接口

列表接口先应用身份与可见范围，再统计和分页。不同字段按 `AND` 组合；同一字段可以重复传递或用逗号分隔，按 `IN` 匹配。时间使用 ISO 8601，`From` 包含边界，`To` 不包含边界。响应中的 `appliedFilters` 表示服务实际采用的条件。

知识查询支持：

```text
knowledgeId status nameExact summaryExact matchPhrase negativePhrase
creatorUid authorizedUid visibility sourceType sourceTicketId sourceTicketNo
publishedVersionId systemKey createdFrom createdTo updatedFrom updatedTo
submittedFrom submittedTo publishedFrom publishedTo keyword page pageSize
```

其中 `authorizedUid` 仅管理员可用。示例：

```bash
curl -G 'http://workflow.internal:8089/api/v1/knowledge' \
  -H 'Authorization: Bearer <WORKFLOW_API_TOKEN>' \
  -H 'X-AOPS-Api-Key: <AOPS_API_KEY>' \
  --data-urlencode 'creatorUid=S000001' \
  --data-urlencode 'status=PUBLISHED' \
  --data-urlencode 'systemKey=crm' \
  --data-urlencode 'publishedFrom=2026-09-01T00:00:00+08:00'
```

运行查询支持 `runId/knowledgeId/workflowVersionId/ticketId/initiatedBy/status/statusGroup/currentNodeId` 和创建、开始、结束时间范围。普通用户只能查询自己的运行。

版本分页查询：

```http
GET /api/v1/workflow-versions?knowledgeId=knw_xxx&versionNumber=2&page=1&pageSize=20
```

支持 `workflowVersionId/knowledgeId/versionNumber/contentHash/publishedBy/publishedFrom/publishedTo`。

审核队列支持 `page/pageSize/knowledgeId/creatorUid/sourceTicketId/sourceTicketNo/submittedFrom/submittedTo`。

## 生命周期与失败诊断

知识详情包含 `createdAt/updatedAt/submittedAt/submittedBy/reviewedAt/reviewedBy/lastPublishedAt/lastPublishedBy/deletedAt/deletedBy` 和 `lifecycle[]`。每个不可变版本单独保留发布人和发布时间。

失败尝试在运行详情的 `attempts[]` 中返回 `errorCode/errorMessage/exitCode/diagnosticAvailable/diagnosticTruncated`。运行发起人或管理员可读取完整脱敏诊断：

```http
GET /api/v1/runs/{runId}/attempts/{attemptId}/diagnostic
```

`data.stdout` 和 `data.stderr` 已过滤凭据，并按节点结果保留期限加密保存。

## 跨环境导入导出

各环境必须配置 `KNOWLEDGE_ENVIRONMENT_NAME`。REST接口始终收发 JSON；管理页面 `/transfers` 才把导出响应保存为本地 UTF-8 JSON文件。

导出预检：

```json
POST /api/v1/transfers/export/preview
{"knowledgeIds":["knw_1","knw_2"]}
```

确认导出：

```json
POST /api/v1/transfers/export
{
  "knowledgeIds":["knw_1","knw_2"],
  "confirmed":true,
  "databaseMappings":[
    {"sourceRef":"1/dev/dev/read/svc","targetRef":"9/prod/prod/read/svc"}
  ]
}
```

返回 `schema=itsm-workflow-export`、`schemaVersion=2` 的 JSON对象。包中包含 DAG、匹配范围、授权UID和来源信息，不包含凭据、向量、运行结果或 checkpoint。

导入先调用 `/transfers/import/preview`，再调用 `/transfers/import`：

```json
{
  "package":{"schema":"itsm-workflow-export","schemaVersion":2,"items":[]},
  "confirmed":true,
  "databaseMappings":[
    {"sourceRef":"9/prod/prod/read/svc","targetRef":"12/prod/prod/readonly/svc"}
  ],
  "knowledgeOverrides":[
    {"itemIndex":0,"creatorUid":"S000001","uids":["S000123"],"action":"CREATE"}
  ]
}
```

`action` 支持 `CREATE/SKIP/COPY`。来源和内容完全相同的条目默认跳过；`COPY` 表示明确重复复制。所有新建经验进入 `PENDING_REVIEW`，整批失败时全部回滚。旧 `aops-workflow-knowledge-export/schemaVersion=1` 包仍可导入。

导出包未使用数字签名，允许管理员核实并修改路径、授权或通用描述。若当前 JSON内容与包内来源 `contentHash` 不一致，预检返回 `contentHashMismatch=true` 和告警，但不会拒绝导入；服务会对规范化后的实际导入内容生成 `effectiveContentHash`，并用它完成重复检测。

快速路径替换使用 `/database-paths/replace/preview` 和 `/database-paths/replace`，只对所选知识做完整路径匹配。已发布知识会退回待审核。迁移审计通过 `/transfer-audits` 查询。
