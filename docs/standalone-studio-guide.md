# 独立Studio调试教程

## 1. 页面功能

进入页面先输入`STUDIO_ADMIN_TOKEN`。Token只用于建立HttpOnly管理会话。Studio包含五个入口：

1. **节点调试**：查看Node Catalog并单步运行一个节点。
2. **Node管理**：启停Studio节点并维护名称、说明和调试默认值。
3. **流程编排**：以卡片画布新增节点、连线、配置条件和输出绑定，再执行Registry校验。
4. **草稿提取**：生成DraftProposal后直接渲染DAG，支持选中节点或整流程调试。
5. **API文档**：使用本地OpenAPI渲染器，不访问公网CDN。

页面顶栏始终显示`TEST_ONLY`，避免把本地结果误认为tec01生产事实。

## 2. 调试固定执行真实Handler

页面中的“调试”固定使用`TEST`，不会用Fixture伪造节点成功：

- `sql_read`调用系统`aops-cli db read`。
- `llm_extract`调用`service.env`配置的内网LLM。
- `condition`和`hitl_select`执行本地确定性逻辑。

SQL读必须填写当前工单ID和`AOPS_API_KEY`，并真实执行`aops-cli db read`。底层API保留`SIMULATION/DRY_RUN`仅供自动化契约测试，不作为页面调试入口。

## 3. 调试SQL读节点

1. 进入“节点调试”，选择“SQL只读查询”。
2. 填写并核对节点定义、数据库路径、SQL模板和输入。
3. 填写当前工单ID和个人`AOPS_API_KEY`。
4. 点击“执行真实单节点调试”，服务调用系统`aops-cli db read`。
5. 查看真实输出或脱敏诊断。

凭据路径：

```text
浏览器密码输入框
  -> X-AOPS-Api-Key请求头
  -> FastAPI请求内存
  -> aops-cli子进程AOPS_API_KEY环境变量
  -> 请求结束后释放
```

凭据不会进入请求JSON、调试记录、SQLite或日志。页面在每次请求完成后清空输入框。

## 4. 调试HITL

选择`hitl_select`，在输入JSON中准备两个或更多候选：

```json
{
  "candidates": [
    {"id":"a","label":"客户A","value":"C001"},
    {"id":"b","label":"客户B","value":"C002"}
  ]
}
```

运行后状态变为`WAITING_INPUT`。在右侧选择候选、填写人工值或取消。回复和最终输出保存在TEST_ONLY SQLite中，便于刷新页面后检查。

## 5. 从工单提取草稿

“按工单ID提取”会依次执行：

```text
aops-cli event-center info --id <工单ID>
aops-cli event-center audit_timeline --id <工单ID>
```

Compiler只接受`operation=sql_exec_read`、结果明确成功且SQL安全只读的操作；失败、缺失结果、危险SQL和重复操作会进入`diagnostics.ignoredOperations`。

如果不希望Studio访问AOPS，选择“直接传JSON”，分别粘贴：

- `uatu info`响应中的`data`对象。
- `uatu audit_timeline`响应中的`data`数组。

生成结果不会自动提交tec01或发布。审核后由调用方通过tec01契约创建DRAFT。

提取成功后页面会把`workflowDefinition`直接载入可视化画布。你可以修改节点和连线，然后：

- 选择某一节点进行单步调试。
- 使用真实Handler执行整个DAG；其中SQL读节点逐个调用`aops-cli db read`。
- 查看每个节点的`SUCCEEDED/FAILED/WAITING/SKIPPED`状态与输出。
- 使用运行输入JSON补齐参数；运行前必须提供当前工单ID和`AOPS_API_KEY`。

## 6. 流程编排和依赖

画布支持SQL读、条件、LLM、HITL和结束节点。下游参数依赖前置输出时，在节点输入中设置：

```json
{
  "kind": "NODE_OUTPUT",
  "nodeId": "sql-1",
  "jsonPointer": "/data/0/customer_id"
}
```

HITL到上游LLM的第二条回边会转换为受控`REFINEMENT`，必须配置最大迭代次数和反馈输入名；普通控制边仍保持无环。

点击“校验流程”后，服务检查节点Schema、只读SQL、可达性、条件默认边、输出绑定和REFINEMENT约束，并返回规范化DAG及内容哈希。

## 7. 数据清理

Studio SQLite只保存临时workspace、节点定义、非凭据输入、输出、诊断和HITL回复。默认TTL为24小时，服务启动和查询workspace时执行清理。删除SQLite不会影响tec01生产知识或运行。
