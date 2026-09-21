# Runtime与Studio REST API

默认地址：`http://127.0.0.1:8089`。Studio接口先使用管理Token建立HttpOnly会话；若配置`RUNTIME_SERVICE_TOKEN`，`/internal/v1/*`必须携带：

```http
Authorization: Bearer <RUNTIME_SERVICE_TOKEN>
```

Studio登录：

```http
POST /api/v1/studio/session
Content-Type: application/json

{"token":"<STUDIO_ADMIN_TOKEN>"}
```

登录成功后浏览器自动携带HttpOnly Cookie。Node管理接口为：

```text
GET   /api/v1/studio/nodes
PATCH /api/v1/studio/nodes/{type}/{schemaVersion}
```

## 健康检查

```http
GET /api/v1/health
```

返回版本、角色、aops-cli版本/SHA256和tec01 Worker开关。

## Node Catalog

```http
GET /api/v1/studio/catalog
GET /internal/v1/runtime/catalog
```

内部接口支持`If-None-Match`；响应`ETag`等于Catalog Digest。

## 单节点调试

```http
POST /api/v1/studio/node-debug-runs
X-AOPS-Api-Key: <仅TEST sql_read需要>
Content-Type: application/json
```

```json
{
  "ticketId": 100173,
  "mode": "TEST",
  "node": {
    "id": "sql-1",
    "type": "sql_read",
    "title": "查询客户",
    "config": {
      "databaseRef": "124/aops-t/aops-t/readonly/aops-t",
      "sqlTemplate": "SELECT id FROM users WHERE name={{customer_name}}"
    },
    "inputs": [
      {
        "name": "customer_name",
        "type": "string",
        "source": {"kind": "RUN_INPUT", "key": "customer_name"}
      }
    ]
  },
  "inputs": {"customer_name": "王五"},
  "simulation": {}
}
```

页面调试固定提交`TEST`：

- `TEST`：使用真实Handler。`sql_read`必须同时提供工单ID和请求头API Key。

协议仍接受`SIMULATION/DRY_RUN`用于自动化契约测试，但它们不属于页面上的“调试”。

HITL回复：

```http
POST /api/v1/studio/node-debug-runs/{debugRunId}/interrupts/reply
```

```json
{"response":{"action":"SELECT","candidateId":"candidate-1"}}
```

## 草稿编译

直接传AOPS响应的`data`内容：

```http
POST /api/v1/studio/compiler/preview
```

```json
{
  "ticketInfo": {"id":100173,"incident_id":"INC-1","event_title":"客户查询"},
  "auditTimeline": [
    {
      "operation":"sql_exec_read",
      "result":"{\"r\":true,\"e\":\"\"}",
      "details":"{\"sql\":{\"serverid\":124,\"dbid\":\"aops-t\",\"dbname\":\"aops-t\",\"dbuser\":\"readonly\",\"service_name\":\"aops-t\",\"command\":\"SELECT 1\"}}"
    }
  ]
}
```

或让Studio临时调用aops-cli取证：

```http
POST /api/v1/studio/compiler/from-ticket
X-AOPS-Api-Key: <AOPS_API_KEY>

{"ticketId":100173}
```

服务执行`event-center info`和`event-center audit_timeline`，只处理两者的`data`。API Key不进入响应或SQLite。

## Workflow校验和计划

```http
POST /api/v1/studio/workflows/validate
POST /internal/v1/runtime/workflows/validate
POST /internal/v1/runtime/workflows/plan
POST /internal/v1/compiler/preview
```

生产草稿提取由tec01同步调用`/internal/v1/compiler/preview`，直接传递`ticketInfo`和`auditTimeline`。tec01在调用前保存PROCESSING状态并完成`evidenceHash`幂等去重；Runtime无生产状态，不使用独立Compiler Worker、claim或编译租约。

整流程调试：

```http
POST /api/v1/studio/workflow-debug-runs
X-AOPS-Api-Key: <TEST模式且包含sql_read时需要>
```

响应包含`nodeStatuses`和逐节点`nodeResults`。条件节点只执行命中分支，其余节点标记为`SKIPPED`。

页面`/docs`使用本地React组件读取`/openapi.json`，不依赖Swagger CDN，适用于隔离内网。

内部API契约详见[tec01集成契约](tec01-integration-contract.md)。本服务没有`/mcp`、`/auth/session`、知识检索或生产运行控制接口。
