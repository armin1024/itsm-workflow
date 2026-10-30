# 提取失败诊断与日志检索（0.13.2）

## 1. 响应说明

保持原有code、message、retryable字段，新增errorId及details。message是安全的具体解释，不包含工单实际值、模型原文或SQL正文。

示例：模型返回parameters=null，而程序要求数组。

```json
{
  "code": "LLM_ANALYSIS_FAILED",
  "message": "分析模型结果失败：模型返回字段/steps/0/parameters应为数组，实际为null；请重新分析或检查模型结构化输出配置。",
  "retryable": true,
  "errorId": "err_example",
  "details": {
    "stage": "ANALYZING_EVIDENCE",
    "reasonCode": "LLM_RESPONSE_SCHEMA_INVALID",
    "field": "/steps/0/parameters",
    "expectedType": "array",
    "actualType": "null",
    "exceptionType": "CompilerFailure",
    "location": "app/compiler_errors.py:check:45"
  }
}
```

编号和代码行仅为示例，实际会不同。所有数组索引从0开始；字段路径表示结构位置，不包含该字段的历史实际值。

| 具体原因码 | 含义与动作 |
|---|---|
| LLM_NOT_CONFIGURED | 缺少模型地址或名称，检查配置 |
| LLM_TIMEOUT | 模型调用超时，说明含配置超时秒数 |
| LLM_HTTP_ERROR | 模型网关返回失败HTTP状态，说明含状态码，details可含upstreamStatus |
| LLM_UNAVAILABLE | 模型网关连接、TLS或网络异常 |
| LLM_RESPONSE_INVALID | 响应不是有效JSON对象或choices/message/content结构不符 |
| LLM_RESPONSE_SCHEMA_INVALID | JSON字段类型错误，例如steps、parameters或dependencies为null |
| LLM_REQUIRED_FIELD_MISSING | 模型缺少必填字段 |
| LLM_CONTENT_INVALID | 名称/摘要不满足中文要求或没有有效匹配短语 |
| SQL_INPUT_MISMATCH | SQL占位符与步骤参数不一致 |
| DEPENDENCY_BINDING_INVALID | 前置节点、顺序或结果绑定不合法 |
| DATABASE_REF_INVALID | 数据库路径无效 |
| SQL_PARSE_FAILED | 当前解析器无法解析步骤SQL模板 |
| WORKFLOW_SCHEMA_INVALID | Workflow结构或字段类型不符合约束 |
| NO_VALID_OPERATIONS | 全部操作被过滤，details.filterCounts提供安全原因计数 |
| COMPILER_INTERNAL_ERROR | 未分类程序异常；说明含异常类别，按错误编号及安全代码位置交维护人员 |
| RESULT_SERIALIZATION_FAILED | 生成或同步终态结构异常；原业务错误另保留 |

资源超限仍使用对应明确错误码，不伪装为模型失败。retryable表示是否建议重新分析，不代表自动重试，也不保证不重试就永远无法恢复。失败重新分析仍使用新requestId。

## 2. 日志如何产生

提取器在捕获失败时先写一条独立ERROR，再发送FINISHED。即使关闭tec01同步、回报HTTP请求失败或本地事件通道异常，也能按原错误编号检索该失败。

```json
{
  "event": "COMPILER_FAILED",
  "level": "ERROR",
  "peer": "COMPILER",
  "direction": "INTERNAL",
  "operation": "compile_evidence",
  "knowledgeId": "knw_example",
  "requestId": "extract_example",
  "businessStatus": "FAILED",
  "errorCode": "LLM_ANALYSIS_FAILED",
  "businessErrorCode": "LLM_ANALYSIS_FAILED",
  "reasonCode": "LLM_RESPONSE_SCHEMA_INVALID",
  "errorField": "/steps/0/parameters",
  "errorId": "err_example",
  "errorMessage": "分析模型结果失败：模型返回字段/steps/0/parameters应为数组，实际为null；请重新分析或检查模型结构化输出配置。"
}
```

文件和控制台输出相同安全字段，沿用现有日志目录、7天/512MiB清理和队列机制。日志是尽力记录，不是业务数据库；队列满、磁盘异常或已清理时仍可能不可查询，状态页会提供丢弃和存储故障计数。

回报成功的日志仍可能为INFO、HTTP200，表示通信成功；独立ERROR说明业务失败，两者不矛盾。若终态同步自身异常，另有COMPILER_REPORT_FAILED，不覆盖原业务失败。

## 3. 查询方式

提取页先显示具体message，点击“按错误编号查看日志”自动应用ERROR和errorId。日志页关键词支持中文原因片段、字段路径、原因码及安全代码位置；高级筛选支持完整值精确匹配：

```http
GET /api/v1/studio/logs?level=ERROR&peer=COMPILER&event=COMPILER_FAILED&errorId=err_example
GET /api/v1/studio/logs?reasonCode=LLM_RESPONSE_SCHEMA_INVALID&errorField=%2Fsteps%2F0%2Fparameters
GET /api/v1/studio/logs?errorCode=COMPILER_FAILED&stage=VALIDATING_WORKFLOW
```

这些接口仍需Studio管理会话。不同条件AND组合；errorCode同时匹配通信errorCode或业务businessErrorCode。关键词最多200字符，输入原因片段即可；失败字段最多240字符。原文日志入口也使用同样过滤，journalctl可按错误编号搜索控制台。

```bash
journalctl -u itsm-workflow-api --since '1 hour ago' --no-pager | rg 'err_example'
```

## 4. 安全与兼容

- 不记录exception原文、locals、原始堆栈行、模型响应、历史SQL、业务参数或完整结果；仅记录受控说明、异常类别及项目内代码位置。
- Workflow校验错误按安全规则生成说明，未知异常不随意把输入拼入日志；维护人员使用errorId、阶段、exceptionType及location定位程序。
- 不改变回报次数、五阶段进度、暂停/恢复或节点执行规则。tec01应保留errorId/details供页面与Hermes展示，至少仍可读取原有message；严格DTO需配套允许这些新增可选字段。
- 旧版本没有记录的异常不能在升级后补造；需使用新requestId重新分析才能产生新诊断。
