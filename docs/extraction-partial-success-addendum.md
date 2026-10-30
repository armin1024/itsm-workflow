# 提取完整性与异常 SQL 隔离：tec01 接入补充说明

日期：2026-10-27

状态：执行器端已实现，纳入 0.13.3 源码；tec01 的保存、审核及发布控制仍需下游配套实现。已部署的旧版本不会返回新增字段。

## 1. 目的与适用范围

本补充约定解决“一条 SQL 参数化异常导致整次提取失败”以及“跳过异常后草稿缺少步骤却未提醒审核人”的问题。

执行器逐条处理审计操作，保留有效节点，并明确回报异常遗漏。tec01 保存完整性诊断，在审核页面提示并控制发布。

不改变执行器与 tec01 的职责、存储方式和执行调度；不增加节点类型，不增加回报类型，不修改 Workflow 的 `nodes`、`edges`、条件或参数绑定结构。

## 2. 三类处理结果

| 类别 | 场景 | 处理 |
|---|---|---|
| 正常过滤 | 非 SQL 读、审计结果失败或不明确、没有 SQL、数据库路径缺失、重复操作 | 记录 `ignoredOperations`；其余记录继续 |
| 单条处理异常 | SQL 解析失败、非安全只读单语句、SQL 参数化失败、参数化后的节点校验失败、单条 SQL 超长 | 记录 `failedOperations`；其余记录继续；草稿为部分成功 |
| 整体失败 | 无有效 SQL、总请求或证据超限、操作数或节点数超限、模型输入超限、LLM 失败、最终 Workflow 结构不合法、内部不可恢复异常 | 沿用失败响应，不输出可发布草稿 |

单条 SQL 超长与总资源超限不同：前者隔离该条，后者拒绝整个任务。内存不足等服务级异常仍整体失败，不作为单条 SQL 错误吞掉。

正常过滤不计入 `failedOperationCount`。`SUCCESS` 仅表示所有候选处理没有异常遗漏，不表示历史操作全部保留，也不表示已经审核或授权执行。

## 3. 提取流程

1. 校验工单与审计记录结构、请求大小及总资源限制。
2. 逐条检查操作类型、结果、SQL 和数据库路径。
3. 逐条解析、只读检查、去重、参数化并立即校验生成的 SQL 节点。
4. 将通过校验的节点交给 LLM，总结名称、摘要、参数语义及依赖；禁止 LLM 修复或改写历史 SQL。
5. 校验依赖、绑定和完整 Workflow。结构非法时整体失败，不返回一个看似有效的执行图。
6. 回报草稿、校验结果及完整性诊断。

保留现有五个阶段与阶段入口回报：`FILTERING_OPERATIONS`、`PARAMETERIZING_SQL`、`ANALYZING_EVIDENCE`、`BUILDING_WORKFLOW`、`VALIDATING_WORKFLOW`。不为每条 SQL 增加远程进度回报，不恢复心跳。失败时仅存在实际进入过的阶段回报。

## 4. 回报字段扩展

在成功的 `FINISHED` 回报顶层新增 `extractionOutcome`、`publishable`，在现有 `diagnostics` 中新增异常与阻断项。查询或调试接口返回同一提取结果时，字段含义一致。

| 字段 | 类型 | 新协议要求 | 说明 |
|---|---|---|---|
| `extractionOutcome` | string | 必返 | `SUCCESS` 或 `PARTIAL_SUCCESS` |
| `publishable` | boolean | 必返 | 提取时是否不存在完整性阻断；不是审核通过、执行授权或发布成功 |
| `diagnostics.auditOperationCount` | integer | 保留 | 输入审计操作总数 |
| `diagnostics.acceptedOperationCount` | integer | 保留 | 最终保留的 SQL 节点数，不含 end 节点 |
| `diagnostics.ignoredOperationCount` | integer | 保留 | 正常过滤数 |
| `diagnostics.ignoredOperations` | object[] | 保留 | 正常过滤的记录与原因 |
| `diagnostics.failedOperationCount` | integer | 新增必返 | 单条处理异常数 |
| `diagnostics.failedOperations` | object[] | 新增必返 | 异常记录；无异常返回 `[]` |
| `diagnostics.blockingIssues` | object[] | 新增必返 | 已识别的依赖或完整性问题；没有则返回 `[]` |

成功返回时，一条原始审计记录只归入一个分类，应满足：

```text
auditOperationCount = acceptedOperationCount + ignoredOperationCount + failedOperationCount
```

### 4.1 异常记录

`failedOperations[]` 每项包含：

| 字段 | 类型 | 说明 |
|---|---|---|
| `operationId` | string 或 null | 来源操作 ID；不存在时为 null，不虚构 ID |
| `auditIndex` | integer | 在原始 auditTimeline 数组中的索引，从 0 开始，便于定位无 ID 或重复 ID 的记录 |
| `stage` | string | `FILTERING_OPERATIONS` 或 `PARAMETERIZING_SQL` |
| `code` | string | 稳定原因码，见下表 |
| `message` | string | 中文安全说明，不含历史 SQL、字面值或原始异常正文 |

| code | 含义 |
|---|---|
| `SQL_PARSE_FAILED` | 无法按支持的方言解析 |
| `SQL_NOT_READ_ONLY` | 不符合安全只读要求 |
| `SQL_MULTIPLE_STATEMENTS` | 包含多条语句 |
| `SQL_TEMPLATE_TOO_LARGE` | 单条 SQL 或参数化模板超长 |
| `SQL_PARAMETERIZATION_FAILED` | 无法安全完成参数化 |
| `SQL_NODE_VALIDATION_FAILED` | 生成节点的 SQL、参数或配置未通过校验 |

`ignoredOperations` 保留已有结构和原因码；允许增量增加 `auditIndex`，下游不得依赖其一定存在。

### 4.2 阻断问题

`blockingIssues[]` 每项包含：

```json
{
  "code": "DEPENDENCY_REQUIRES_REVIEW",
  "message": "前置操作未纳入草稿，请核实当前节点的输入来源。",
  "nodeIds": ["sql-2"],
  "operationIds": ["10897"]
}
```

`nodeIds` 只引用最终草稿中存在的节点，无法定位时返回 `[]`。`operationIds` 只包含已知来源 ID，未知时返回 `[]`。

首批问题码为 `DEPENDENCY_REQUIRES_REVIEW`（输入依赖待核实）和 `EXTRACTION_INCOMPLETE`（已识别的其他完整性问题）。该清单不保证识别所有业务依赖；审核人仍需检查完整流程。

禁止将异常操作的输出绑定到另一个无关节点以自动补齐依赖。保留下来的节点 ID 连续生成，来源定位使用 operationId/auditIndex，不能把原审计序号当作节点 ID。

## 5. 成功回报示例

以下展示新增字段所在位置。实际回报继续携带现有完整 `proposal` 和 `validation`；示例只展开诊断相关字段，不作为完整请求体直接调用。

```json
{
  "knowledgeId": "knw_example",
  "requestId": "req_example",
  "type": "FINISHED",
  "status": "SUCCEEDED",
  "seq": 6,
  "occurredAt": "2026-10-27T08:00:00Z",
  "extractionOutcome": "PARTIAL_SUCCESS",
  "publishable": false,
  "diagnostics": {
    "auditOperationCount": 10,
    "acceptedOperationCount": 3,
    "ignoredOperationCount": 6,
    "ignoredOperations": [
      {"operationId": "10890", "reason": "unsupported_operation"},
      {"operationId": "10891", "reason": "failed_result"},
      {"operationId": "10892", "reason": "missing_result"},
      {"operationId": "10893", "reason": "invalid_result"},
      {"operationId": "10894", "reason": "missing_sql"},
      {"operationId": "10895", "reason": "duplicate_read_operation"}
    ],
    "failedOperationCount": 1,
    "failedOperations": [
      {
        "operationId": "10897",
        "auditIndex": 7,
        "stage": "PARAMETERIZING_SQL",
        "code": "SQL_PARAMETERIZATION_FAILED",
        "message": "该操作无法安全参数化，请人工核实并补充步骤。"
      }
    ],
    "blockingIssues": [],
    "llmMode": "structured"
  }
}
```

`blockingIssues=[]` 不会消除 `failedOperations` 带来的完整性阻断。完整成功时：`extractionOutcome=SUCCESS`、异常数为 0、两个新增数组为空、`publishable=true`。

## 6. 部分成功不增加新的任务状态

现有 `status=SUCCEEDED` 表示已产出结构合法的草稿；是否完整由 `extractionOutcome` 表达。不要把 `PARTIAL_SUCCESS` 写入原任务状态枚举。

| 情况 | status | extractionOutcome | publishable |
|---|---|---|---|
| 产出有效草稿，无异常遗漏和阻断 | SUCCEEDED | SUCCESS | true |
| 产出有效草稿，有异常遗漏或阻断 | SUCCEEDED | PARTIAL_SUCCESS | false |
| 无有效 SQL 或整体处理失败 | FAILED | 不返回 | 不返回 |

失败继续使用现有 `error.code/message/retryable` 和可选 `errorId/details`；无有效 SQL 返回 `NO_VALID_OPERATIONS`。若处理后没有节点，不调用 LLM，也不通过生成空流程伪装成功。

已完成分类的异常和过滤统计应随失败诊断提供；结构或资源预检失败可能尚无完整统计，下游不得要求每个失败响应都有操作清单。

## 7. tec01 保存、审核与发布责任

1. 继续按 `knowledgeId + requestId` 隔离提取尝试；迟到旧回报不能覆盖当前草稿。
2. 当前成功回报事务内同时保存草稿、校验结果、新增字段及诊断，保存成功后返回现有 AopsResult 确认。不得只保存草稿而丢弃异常清单。
3. `PARTIAL_SUCCESS` 显示“提取部分成功，存在异常遗漏”，允许编辑和保存，不直接发布。
4. 展示异常来源操作、数组索引、阶段、原因，以及保留步骤和输入依赖。不得仅显示通用 COMPILER_FAILED。
5. 发布前，用户补齐或修正步骤、确认依赖；确实不需保留的异常项可人工说明理由并确认排除。tec01 保存处理人、处理时间和处理说明，并将其绑定到本次提取请求及当前草稿修订。
6. 对当前编辑后的定义重新调用现有发布模式校验，校验通过且完整性问题已人工处理后，才允许发布。更换提取请求或相关草稿修改后，应重新检查，不沿用旧内容的确认。

`publishable` 是提取时的只读判断，不允许前端改成 true 来代替审核。Workflow 校验只证明结构与约束合法，不能证明业务步骤没有遗漏；tec01 必须同时执行完整性审核规则。

人工处理记录由 tec01 管理，不加入可执行 Workflow JSON，不为此增加执行器存储。即使完整成功，仍需沿用原有人工审核及权限要求。

## 8. 兼容与上线顺序

- 下游 DTO/JSON Schema 增量允许新增字段；历史记录可缺省，不能默认赋 `SUCCESS` 或 `publishable=true`。
- 缺少完整性字段的旧记录显示“未提供完整性诊断”，沿用人工审核，并建议重新提取；不得把字段缺失解释为已完成检查。
- Workflow Schema 版本不变。提取结果 Schema 增量扩展；客户端应按字段存在性兼容，不修改执行计划格式。
- 先发布 tec01 的接收、保存、显示及审核控制，再发布执行器的异常隔离与新字段；联调验证完整后启用。
- 回报 URL、鉴权、requestId/seq、幂等键、保存确认、重试和总等待策略全部沿用现有协议；失败重试只重发回报，不重新执行 SQL 或调用 LLM。
- 执行器已提供 `GET /internal/v1/runtime/schemas/extractionFinished`（成功终态回报约束）及 `/internal/v1/runtime/schemas/extractionDiagnostics`（诊断约束），使用现有服务鉴权。前者不适用于 FAILED 回报或同步预览响应；同步预览返回同义的完整性字段，但没有回报的 type/seq 等字段。
- 也可直接使用本文同目录的 `extraction-finished.schema.json` 和 `extraction-diagnostics.schema.json`。Workflow Schema 版本未改变。
- 整体失败时，已经完成的分类统计位于 `error.details.diagnostics`；预检提前失败时可以没有该字段。
- Studio 展示部分成功警告，保存到本地知识后保留原始诊断；本地测试仍允许生成计划和执行，不提供正式发布或代替 tec01 审核。

## 9. 联调验收

1. 有效 SQL 与参数化异常 SQL 混合输入：产出有效草稿，部分成功、不可直接发布，异常可定位。
2. 全部记录被过滤或失败：整体失败，无空草稿、不调用 LLM，能够查看已完成的分类统计。
3. 非 SQL、失败审计、重复操作只计入 ignored，不误报为处理异常。
4. 异常前置操作不会被静默替换为其他节点；未解决的依赖阻止发布。
5. 草稿修正并重新校验、完整性人工处理后可发布，原始提取诊断仍保留。
6. 新字段经 tec01 保存和详情查询后不丢失；同一 requestId 重复回报不会生成重复处理记录，旧请求不覆盖新请求。
7. 历史记录缺少字段时页面可用，且不自动认定完整。
8. 五阶段回报与原确认、重试不变；诊断和日志不包含 SQL 正文、实际参数值或凭据。
