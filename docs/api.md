# Runtime与Studio REST API

默认地址 `http://127.0.0.1:8089`。接口JSON使用UTF-8，`/docs`在内网读取本地组件与 `/openapi.json`，无需Swagger CDN。

## 认证与凭据

- `/internal/v1/*`：`Authorization: Bearer <RUNTIME_SERVICE_TOKEN>`。
- `/api/v1/studio/*`：管理Token登录后使用HttpOnly Cookie。
- 真实SQL执行与按工单ID取证：额外传 `X-AOPS-Api-Key: <AOPS_API_KEY>`。

```http
POST /api/v1/studio/session
Content-Type: application/json

{"token":"<STUDIO_ADMIN_TOKEN>"}
```

`GET /api/v1/studio/session`检查会话，`DELETE`同一路径退出。不要把API Key写入JSON、Workflow、节点输入或浏览器持久存储。

## 内部接口

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/internal/v1/runtime/catalog` | 节点目录，支持ETag/304 |
| GET | `/internal/v1/runtime/schemas` | 全部JSON Schema与节点配置约束 |
| GET | `/internal/v1/runtime/schemas/{schemaName}` | 单个JSON Schema |
| POST | `/internal/v1/runtime/workflows/validate` | Workflow归一化与校验 |
| POST | `/internal/v1/runtime/workflows/plan` | 预览/联调计划 |
| POST | `/internal/v1/runtime/nodes/execute` | 同步真实执行单个节点 |
| POST | `/internal/v1/extractions/{knowledgeId}` | 异步提取草稿 |
| POST | `/internal/v1/executions/dispatch` | 首次执行、恢复或重试完整Workflow |
| POST | `/internal/v1/executions/{runId}/commands` | 暂停或取消 |

正式计划由tec01从已校验的发布版本创建，检查当前用户权限、工单ID、运行输入与确认哈希；Executor下发前复核Workflow。详见[itsm-workflow提供的API](itsm-workflow-to-tec01-api.md)。

### 获取JSON约束

```bash
curl 'http://127.0.0.1:8089/internal/v1/runtime/schemas' \
  -H 'Authorization: Bearer <RUNTIME_SERVICE_TOKEN>'
```

返回：

```json
{
  "runtimeVersion": "0.10.0",
  "jsonSchemaDialect": "https://json-schema.org/draft/2020-12/schema",
  "schemas": {
    "workflowDefinition": {},
    "dispatch": {},
    "extraction": {},
    "singleNode": {}
  },
  "nodes": [
    {"type":"sql_read","schemaVersion":1,"configSchema":{},"inputSchema":{},"outputSchema":{}}
  ]
}
```

示例中的空对象代表服务实际返回的完整Schema，服务另提供`singleNodeResponse`响应约束。单独读取时使用 `/schemas/workflowDefinition`、`/schemas/dispatch`、`/schemas/extraction`、`/schemas/singleNode` 或 `/schemas/singleNodeResponse`。JSON Schema适合生成表单和字段检查；只读SQL、DAG可达性及前置依赖仍以校验接口为准。

### 真实执行单个节点

```bash
curl -X POST 'http://127.0.0.1:8089/internal/v1/runtime/nodes/execute' \
  -H 'Authorization: Bearer <RUNTIME_SERVICE_TOKEN>' \
  -H 'X-AOPS-Api-Key: <AOPS_API_KEY>' \
  -H 'Content-Type: application/json' \
  -d '{
    "requestId":"single-sql-001",
    "ticketId":100173,
    "node":{
      "id":"sql-1",
      "type":"sql_read",
      "title":"查询客户",
      "config":{
        "databaseRef":"124/aops-t/aops-t/readonly/aops-t",
        "sqlTemplate":"SELECT id FROM users WHERE name={{customer_name}}"
      },
      "inputs":[{"name":"customer_name","type":"string","source":{"kind":"RUN_INPUT","key":"customer_name"}}]
    },
    "inputs":{"customer_name":"王五"}
  }'
```

返回结构：

```json
{
  "requestId":"single-sql-001",
  "nodeId":"sql-1",
  "status":"SUCCEEDED",
  "output":{"status":0,"rowCount":1,"data":[{"id":"C001"}]},
  "diagnostic":null,
  "interaction":null,
  "handlerVersion":"1.0.0"
}
```

该接口只执行提交的节点，不执行其上游；调用方必须传入已经解析好的 `inputs`。含 `sql_read` 时工单ID与API Key必填。参数在服务端进行类型校验并安全渲染SQL，comment固定为 `#uatu-100173`。

相同 `requestId` 与相同内容在当前服务进程内复用原结果；同ID不同内容返回409。失败重试、HITL回复和新的执行必须使用新的 `requestId`。此内存幂等记录不跨服务重启。

单节点 `hitl_form` 首次返回 `WAITING_INPUT` 与 `interaction`；提交新requestId及相同node/inputs，并附：

```json
{"resumePayload":{"nodeId":"input-1","action":"SUBMIT","values":{"customer_id":"C001","limit":20}}}
```

这类调用同步返回结果，由调用方自行保存；不会创建完整运行或自动产生运行回调。

## Studio接口

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/v1/studio/catalog`、`/api/v1/studio/nodes` | Node信息 |
| PATCH | `/api/v1/studio/nodes/{type}/{schemaVersion}` | 校验Node显示/默认值，实际由浏览器保存 |
| POST | `/api/v1/studio/node-debug-runs` | 单节点真实调试 |
| POST | `/api/v1/studio/node-debug-runs/reply` | 单节点人工表单回复 |
| POST | `/api/v1/studio/extractions` | JSON证据异步抽取 |
| POST | `/api/v1/studio/extractions/from-ticket` | 按工单ID取证并异步抽取 |
| POST | `/api/v1/studio/compiler/preview`、`/compiler/from-ticket` | 同步抽取预览 |
| POST | `/api/v1/studio/workflows/validate` | 校验编辑中的Workflow |
| POST | `/api/v1/studio/workflows/plan` | 校验并生成本地计划材料 |
| POST | `/api/v1/studio/executions/dispatch` | 启动/继续本地计划 |
| POST | `/api/v1/studio/executions/{runId}/commands` | 暂停/取消 |
| GET | `/api/v1/studio/events/{streamId}?after=0` | 读取新进度与节点事件 |

后台无数据库，Node设置、知识和计划均由浏览器保存；API仅负责计算和活动执行。事件接口是短期内存缓存，页面按事件游标轮询，并把已知结果存回IndexedDB。

### JSON抽取

```json
{
  "knowledgeId":"local_knowledge_1",
  "requestId":"extract_1",
  "ticketId":100173,
  "ticketInfo":{"id":100173,"incident_id":"INC-1","event_title":"客户查询"},
  "auditTimeline":[
    {
      "id":19,
      "operation":"sql_exec_read",
      "result":"{\"r\":true,\"e\":\"\"}",
      "details":"{\"sql\":{\"serverid\":124,\"dbid\":\"aops-t\",\"dbname\":\"aops-t\",\"dbuser\":\"readonly\",\"service_name\":\"aops-t\",\"command\":\"SELECT id FROM users\"}}"
    }
  ],
  "syncTec01":false
}
```

响应202：

```json
{"knowledgeId":"local_knowledge_1","accepted":true,"duplicate":false}
```

用knowledgeId读取事件。成功终态携带草稿、校验摘要和筛选账本，页面可编辑保存为多条本地知识。

按ID取证请求为 `{"knowledgeId":"local_knowledge_2","requestId":"extract_2","ticketId":100173,"syncTec01":false}`，额外传API Key。只有此Studio入口会调用CLI获取证据；tec01集成始终直接传data对象与数组。

### 本地计划执行

先向 `/api/v1/studio/workflows/plan` 提交 `workflowDefinition`、`ticketId`、`inputs`。它返回规范化定义、内容哈希、必填参数和渲染计划，不会执行SQL。

执行请求与内部dispatch相同，可加 `syncTec01:false`。页面保存完整快照、`nodeStates`、`nodeOutputs`、`selectedRoutes`和逐节点结果。继续时换新dispatchId，已成功节点不重放；表单恢复增加 `resumePayload`。暂停/取消命令只包含 `commandId` 与 `type:"PAUSE"|"CANCEL"`。

SQL调试固定为真实执行。API Key每次执行或恢复临时提供，不进入浏览器数据库、事件或导出包。取消从页面移除计划，知识仍可再次创建新计划。

### 可选同步tec01

`syncTec01`默认false。true要求服务配置回调地址/令牌，且tec01已创建对应知识或运行记录。统一回调的格式、幂等和重试见[tec01提供的回调API](tec01-to-itsm-workflow-api.md)。

抽取结果可独立完成；同步失败另行显示 `syncStatus/callbackError`。执行开启同步时，节点回调未确认会停止进入下一节点，页面显示已知结果并要求核实后恢复。

## 错误

内部接口统一返回 `{code,message,retryable,details?}`。主要HTTP状态为401认证失败、409幂等冲突或运行活动冲突、422字段/Workflow校验失败、429容量不足、503未配置同步或服务不可达。节点业务执行失败通过响应 `status:"FAILED"` 与脱敏 `diagnostic` 表达。

健康检查 `GET /api/v1/health` 返回版本、CLI可用性、执行器ID与活动数量，`storage:"browser"` 表示没有本地业务数据库。
