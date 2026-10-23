# Runtime与Studio REST API

0.12.2增加中文OpenAPI说明与字段级校验提示；技术路径、参数键和错误码不变。日志增加event/operation检索及`GET /api/v1/studio/logs/raw`、`GET /api/v1/studio/logs/journal`两个只读原文入口，字段、权限和主机授权注意事项见[日志手册](logging.md)。

完整生产接入流程、字段表、请求响应及恢复示例见[Workflow全流程接口文档](workflow-full-api-guide-0.12.0.md)。本文中的Studio接口与生产内部接口认证、参数不同，不应混用。

提取各阶段、执行逐节点回报、保存确认、失败与重试的完整说明见[提取与执行全流程回报说明](callbacks-extraction-execution-guide.md)，包括同阶段多条“第1/6次尝试”的含义。

tec01下游开发请使用[提取与执行协议对齐交付文档](tec01-extraction-execution-alignment.md)：当前源码五阶段各一次进度、不发心跳，运行确认采用AopsResult，终止回报为TERMINATED。

前端表单和画布接入请使用[前端JSON约束与交互指南](frontend-json-contract-0.12.0.md)及[0.12.0完整Schema](json-schema-0.12.0.json)，包括节点配置、参数来源、连线及HITL规则。

## 调用日志（0.12.0）

新增只读`GET /api/v1/studio/logs`和`GET /api/v1/studio/logs/status`，均需Studio管理会话。默认100条、最多200条，单次扫描最多8MiB，不返回全量总数；游标因清理失效返回409。字段与示例见[日志运维手册](logging.md)。只读配置响应新增`logging`策略，业务存储仍为browser-indexeddb。

当前版本0.12.3。日志支持调用配对、中文操作说明和spanId/callId/traceId关联查询。运行回调采用HTTP200、整数status=200及data.runId匹配的AopsResult采纳确认，不再要求saved；提取五阶段各一次进度，终止状态TERMINATED。详见[tec01回调API](tec01-to-itsm-workflow-api.md)。旧0.11.0对齐事项为历史设计。

## 只读运行配置

`GET /api/v1/studio/config` 需要Studio登录Cookie；未登录返回401，不提供修改方法。响应使用显式安全字段白名单，不包含任何令牌或密钥。

```json
{
  "runtimeVersion": "0.12.1",
  "readOnly": true,
  "storage": "browser-indexeddb",
  "serverDatabase": false,
  "executorId": "executor-01",
  "tec01": {
    "baseUrl": "https://tec01.internal",
    "serviceTokenConfigured": true,
    "runCallbackPath": "/internal/v1/runs/{runId}/callbacks",
    "extractionCallbackPath": "/internal/v1/extractions/{knowledgeId}/callbacks",
    "runCallbackUrl": "https://tec01.internal/internal/v1/runs/{runId}/callbacks",
    "extractionCallbackUrl": "https://tec01.internal/internal/v1/extractions/{knowledgeId}/callbacks",
    "timeoutSeconds": 20,
    "callbackMaxAttempts": 6,
    "callbackWaitSeconds": 70,
    "extractionMaxAttempts": 6
  }
}
```

默认地址 `http://127.0.0.1:8089`。接口JSON使用UTF-8，`/docs`在内网读取本地组件与 `/openapi.json`，无需Swagger CDN。

下游合法性校验接入见[JSON约束说明](json-validation-contract.md)，完整离线Schema见[0.12.0 Schema集合](json-schema-0.12.0.json)。

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
  "runtimeVersion": "0.11.0",
  "jsonSchemaDialect": "https://json-schema.org/draft/2020-12/schema",
  "schemas": {
    "workflowDefinition": {},
    "dispatch": {},
    "extraction": {}
  },
  "nodes": [
    {"type":"sql_read","schemaVersion":1,"configSchema":{},"inputSchema":{},"outputSchema":{}}
  ]
}
```

示例中的空对象代表服务实际返回的完整Schema。单独读取时使用 `/schemas/workflowDefinition`、`/schemas/dispatch` 或 `/schemas/extraction`。JSON Schema适合生成表单和字段检查；只读SQL、DAG可达性及前置依赖仍以校验接口为准。

## Studio接口

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/v1/studio/catalog` | 画布可用节点目录 |
| POST | `/api/v1/studio/extractions` | JSON证据异步抽取 |
| POST | `/api/v1/studio/extractions/from-ticket` | 按工单ID取证并异步抽取 |
| POST | `/api/v1/studio/compiler/preview`、`/compiler/from-ticket` | 同步抽取预览 |
| POST | `/api/v1/studio/workflows/validate` | 校验编辑中的Workflow |
| POST | `/api/v1/studio/workflows/plan` | 校验并生成本地计划材料 |
| POST | `/api/v1/studio/executions/dispatch` | 启动/继续本地计划 |
| POST | `/api/v1/studio/executions/{runId}/commands` | 暂停/取消 |
| GET | `/api/v1/studio/executions/{runId}` | 查询本执行器实例是否仍有活动任务 |
| GET | `/api/v1/studio/events/{streamId}?after=0` | 读取新进度与节点事件 |

后台无数据库，知识、计划与结果由浏览器保存；API仅负责计算和活动执行。事件接口是短期内存缓存，页面按事件游标轮询，并把已知结果存回IndexedDB。

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

### 最小流程与本地计划执行

测试一条SQL时创建“SQL读→结束”的最小Workflow，保存知识后按创建运行、校验计划、确认执行的流程操作。表单和条件同样放入Workflow测试，统一使用以下计划与下发接口。

先向 `/api/v1/studio/workflows/plan` 提交 `workflowDefinition`、`ticketId`、`inputs`。它返回规范化定义、内容哈希、必填参数和渲染计划，不会执行SQL。

执行请求与内部dispatch相同，可加 `syncTec01:false`。页面保存完整快照、`nodeStates`、`nodeOutputs`、`selectedRoutes`和逐节点结果。继续时换新dispatchId，已成功节点不重放；表单恢复增加 `resumePayload`。暂停/取消命令只包含 `commandId` 与 `type:"PAUSE"|"CANCEL"`。

SQL调试固定为真实执行。API Key每次执行或恢复临时提供，不进入浏览器数据库、事件或导出包。取消从页面移除计划，知识仍可再次创建新计划。

0.11.0页面先无Key确认计划，再提供Key执行；确认摘要与时间保存在浏览器。新的Studio下发带可选`planMaterialHash`，服务端据当前定义、工单和参数重算校验，不一致返回409。为兼容旧Studio API，直接请求暂未强制此字段；它防止陈旧确认，不是生产用户授权。

### 可选同步tec01

`syncTec01`默认false。true要求服务配置回调地址/令牌，且tec01已创建对应知识或运行记录。统一回调的格式、幂等和重试见[tec01提供的回调API](tec01-to-itsm-workflow-api.md)。

抽取结果可独立完成；同步失败另行显示 `syncStatus/callbackError`。执行开启同步时，节点回调未确认会停止进入下一节点，页面显示已知结果并要求核实后恢复。

## 错误

内部接口统一返回 `{code,message,retryable,details?}`。主要HTTP状态为401认证失败、409幂等冲突或运行活动冲突、422字段/Workflow校验失败、429容量不足、503未配置同步或服务不可达。节点业务执行失败通过响应 `status:"FAILED"` 与脱敏 `diagnostic` 表达。

健康检查 `GET /api/v1/health` 返回版本、CLI可用性、执行器ID与活动数量，`storage:"browser"` 表示没有本地业务数据库。
