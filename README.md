# ITSM Workflow Runtime

当前版本 `0.10.0`。本服务负责节点定义、工单知识抽取、Workflow校验和真实执行；tec01负责生产知识、计划、权限、MCP、Hermes消息渠道和运行数据。

运行节点为 `sql_read`、`condition`、`hitl_form`、`end`。LLM只用于草稿提取。用户参考SQL结果，在人工表单中填写一个或多个参数，再传给后续节点。

## 独立调试

```bash
cp .env.example .env
uv sync
cd frontend && npm ci && npm run build && cd ..
uv run uvicorn app.main:app --host 127.0.0.1 --port 8089
```

打开 `http://127.0.0.1:8089/`，用 `STUDIO_ADMIN_TOKEN` 登录。

Studio提供知识抽取、本地知识管理、手工新建、可视化编辑、执行计划、单节点和整流程调试。知识、计划、节点结果与Node显示设置保存在浏览器IndexedDB中，后台不使用数据库。支持多条知识和计划、导入导出、暂停、继续、失败重试；取消后移除本地计划。

SQL调试真实执行系统已安装的 `aops-cli db read`。当前工单ID和 `AOPS_API_KEY` 必填，Key仅通过当前请求的 `X-AOPS-Api-Key` 头传入，不保存到浏览器存储、Workflow、事件或导出数据。

默认关闭tec01同步，可独立完成摄入和执行。勾选同步前需配置 `TEC01_BASE_URL` 与 `TEC01_SERVICE_TOKEN`，并提供tec01已创建的经验ID或运行ID。

## 生产集成

只有一个常驻服务 `itsm-workflow-api`：

1. tec01获取Catalog/JSON Schema，在发布前请求Workflow校验。
2. tec01提交工单详情data对象和操作记录data数组，Compiler异步回调进度与草稿。
3. tec01从已发布版本创建并保存计划，向用户展示并确认。
4. tec01主动下发完整Workflow和当前节点状态，Executor逐节点回报结果。
5. 暂停、HITL等待、失败或结束后释放执行资源；tec01根据已保存状态重新下发恢复。

JSON约束通过 `GET /internal/v1/runtime/schemas` 获取。三方也可用 `POST /internal/v1/runtime/nodes/execute` 只执行一个节点，自行提供解析好的输入。

## 文档

- [独立调试教程](docs/standalone-studio-guide.md)
- [REST API与示例](docs/api.md)
- [部署手册](docs/deployment.md)
- [当前架构](docs/architecture.md)
- [tec01总体分工](docs/tec01-control-plane-split.md)
- [tec01集成约定](docs/tec01-integration-contract.md)
- [本服务提供给tec01的API](docs/itsm-workflow-to-tec01-api.md)
- [tec01提供的回调API](docs/tec01-to-itsm-workflow-api.md)
- [Node Registry与节点扩展](docs/node-registry-design.md)

`/docs`是使用本地资源的API页面，不依赖Swagger CDN；`/openapi.json`提供接口定义。

## 验证

```bash
uv run pytest -q
cd frontend && npm run build
```
