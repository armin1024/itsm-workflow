# ITSM Workflow Runtime

理解流程定义本身：[Workflow定义逐字段解释](docs/workflow-definition-field-reference-0.12.0.md)，含顶层、节点、输入来源、连线、条件及HITL每个字段和完整注释示例，不是接口调用说明。

全流程接入：[节点、连线、计划、执行与回报详细接口文档](docs/workflow-full-api-guide-0.12.0.md) · [完整请求/响应示例](docs/examples/workflow-api-examples-0.12.0.json)。

前端编排接入：[JSON约束与交互设计指南](docs/frontend-json-contract-0.12.0.md) · [完整Schema](docs/json-schema-0.12.0.json) · [画布示例](docs/examples/frontend-workflow-0.12.0.json)。

新增只读“日志”菜单，覆盖tec01、LLM和AOPS CLI关键调用；7天、最多512MiB滚动文件，终端同步输出，不保存正文或凭据。配置和清理见[日志运维手册](docs/logging.md)。

当前源码版本 `0.13.3`。本服务负责节点定义、工单知识抽取、Workflow校验和真实执行；tec01负责生产知识、计划、权限、MCP、Hermes消息渠道和运行数据。

0.13.3逐条隔离SQL解析、参数化与节点校验异常，保留有效草稿并回报完整性诊断；部分成功须由tec01配套审核后发布，详见[提取完整性接入补充说明](docs/extraction-partial-success-addendum.md)。

0.13.2提取失败返回安全的具体原因、字段、阶段和错误编号，并独立记录可检索ERROR日志；关闭tec01同步也可查，详见[提取失败诊断与日志检索](docs/compiler-failure-diagnostics.md)。

0.13.1修正SQL读错误诊断：显式SSE错误优先于缺少done，HTML/文本单独识别，未完整结束的结果仍失败并停止后续节点。见[SQL读故障排查](docs/sql-read-troubleshooting.md)。

性能与容量：[实现说明](docs/performance-optimization.md) · [四档机器参数与配置方法](docs/machine-sizing.md) · [模拟性能报告与复测方式](docs/performance-report-0.13.0.md)。新默认为4核8GiB起始档（8个活动运行、2个CLI、1个提取、1个CPU线程），既有显式配置升级不覆盖；不是容量承诺。

“运行配置”菜单只读展示当前tec01连接、回报接口与重试策略，不提供网页修改入口。回报路径由 `TEC01_RUN_CALLBACK_PATH`、`TEC01_EXTRACTION_CALLBACK_PATH` 配置，默认沿用文档；修改 `service.env` 后重启服务。Studio知识、草稿、计划和调试结果只存当前浏览器IndexedDB，不需要服务端数据库。

运行节点为 `sql_read`、`condition`、`hitl_form`、`end`。LLM只用于草稿提取。用户参考SQL结果，在人工表单中填写一个或多个参数，再传给后续节点。

## 独立调试

```bash
cp .env.example .env
uv sync
cd frontend && npm ci && npm run build && cd ..
uv run uvicorn app.main:app --host 127.0.0.1 --port 8089
```

打开 `http://127.0.0.1:8089/`，用 `STUDIO_ADMIN_TOKEN` 登录。

Studio提供知识抽取、本地知识管理、手工新建、可视化编辑、执行计划和流程测试。知识、计划与节点结果保存在浏览器IndexedDB中，后台不使用数据库。支持多条知识和计划、导入导出、暂停、继续、失败重试；取消后移除本地计划。

SQL调试真实执行系统已安装的 `aops-cli db read`。当前工单ID和 `AOPS_API_KEY` 必填，Key仅通过当前请求的 `X-AOPS-Api-Key` 头传入，不保存到浏览器存储、Workflow、事件或导出数据。

默认关闭tec01同步，可独立完成摄入和执行。勾选同步前需配置 `TEC01_BASE_URL` 与 `TEC01_SERVICE_TOKEN`，并提供tec01已创建的经验ID或运行ID。

本地操作分为“确认此执行计划”和“执行已确认计划”。计划确认不需要AOPS Key；执行时才填写凭据。确认记录保存在浏览器，计划定义、工单或参数变化后不能复用旧确认。

## 生产集成

只有一个常驻服务 `itsm-workflow-api`：

1. tec01获取Catalog/JSON Schema，在发布前请求Workflow校验。
2. tec01提交工单详情data对象和操作记录data数组，Compiler异步回调进度与草稿。
3. tec01从已发布版本创建并保存计划，向用户展示并确认。
4. tec01主动下发一次固定Workflow和必要输入；Executor保留内存计划，每节点回报获得明确保存确认后连续推进，不反复下发完整快照。
5. 暂停、HITL等待、失败或结束后释放执行资源；tec01根据已保存状态重新下发恢复。

当前运行回调采用HTTP200、整数status=200及匹配data.runId的AopsResult，不再要求saved或节点身份回显。tec01仅在事务提交并采纳本次消息后返回200；旧调度或新终态冲突返回409。恢复前可查询原执行器是否仍活动，active=false不代表SQL从未执行。

最新回报对齐：运行回报遇5xx、连接异常、超时默认最多6次请求，仍受70秒总等待限制；4xx和无效确认不重试。提取PROGRESS/FINISHED新增原requestId并纳入幂等键，tec01必须隔离不同提取尝试、忽略旧回报。已有显式重试次数配置不会被覆盖，详见[回报协议](docs/tec01-to-itsm-workflow-api.md)。

当前源码提取五阶段各回报一次，不发送逐项进度或定时心跳，正常成功为五条PROGRESS加一条FINISHED；终止回报统一TERMINATED。tec01接入请使用[提取与执行对齐交付文档](docs/tec01-extraction-execution-alignment.md)。既有离线包需重新构建部署才生效。

JSON约束通过 `GET /internal/v1/runtime/schemas` 获取，内部Node Registry继续维护节点配置与输入输出契约。测试某项操作时，手工建立最小Workflow（如SQL读→结束），经计划确认后使用相同的流程执行接口。

## 文档

- [0.11.0实现对齐与待补充约定](docs/runtime-alignment-0.11.0.md)
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
uv run --extra dev pytest -q
cd frontend && npm test && npm run build
```
