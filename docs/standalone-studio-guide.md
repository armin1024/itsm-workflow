# 独立Studio调试教程

## 1. 页面功能

Studio包含三个入口：

1. **节点调试**：查看Node Catalog并单步运行一个节点。
2. **流程编排**：以卡片画布新增节点、连线、配置条件和输出绑定，再执行Registry校验。
3. **草稿提取**：根据工单ID取证，或直接粘贴`ticketInfo`与`auditTimeline` JSON，生成DraftProposal。

无需登录。页面右上角始终显示`TEST_ONLY`，避免把本地结果误认为tec01生产事实。

## 2. 三种节点调试模式

### DRY_RUN

检查节点定义和输入并展示计划，不调用AOPS或LLM。适合先核对数据库路径、SQL模板和参数。

### SIMULATION

从`Simulation Adapter JSON`读取固定输出。适合验证：

- SQL结果到LLM输入的数据形状。
- 条件节点规则。
- HITL单候选自动选择和多候选等待。
- 下游节点需要的JSON Pointer。

### TEST

调用真实Adapter：

- `sql_read`调用系统`aops-cli db read`。
- `llm_extract`调用`service.env`配置的内网LLM。
- `condition`和`hitl_select`执行本地确定性逻辑。

SQL读必须填写当前工单ID和`AOPS_API_KEY`。执行前先用DRY_RUN/SIMULATION确认配置。

## 3. 调试SQL读节点

1. 进入“节点调试”，选择“SQL只读查询”。
2. 先选`DRY_RUN`，填写节点定义和输入，点击“运行单节点”。
3. 再选`SIMULATION`，在Fixture中准备预期`status/data/rowCount`。
4. 确认无误后选`TEST`。
5. 填写工单ID和个人`AOPS_API_KEY`。
6. 点击“运行单节点”，查看输出或脱敏诊断。

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
