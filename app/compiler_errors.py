"""Safe compiler diagnostics. Never persist exception text, inputs or locals."""
from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlglot.errors import ParseError

from app.resources import ResourceError

STAGE_NAMES = {
    "ACCEPTED": "受理提取", "FETCHING_EVIDENCE": "获取工单证据",
    "FILTERING_OPERATIONS": "过滤有效SQL", "PARAMETERIZING_SQL": "去重并参数化SQL",
    "ANALYZING_EVIDENCE": "分析模型结果", "BUILDING_WORKFLOW": "组装Workflow草稿",
    "VALIDATING_WORKFLOW": "校验Workflow定义", "REPORTING_RESULT": "生成并发送提取结果",
}
TYPE_NAMES = {"array": "数组", "object": "对象", "string": "字符串", "null": "null", "boolean": "布尔值", "number": "数字", "missing": "缺失"}


class CompilerFailure(ValueError):
    def __init__(self, code: str, reason: str, message: str, *, field="", expected="", actual="", retryable=False, upstream_status=None, filter_counts=None):
        super().__init__(message)
        self.code, self.reason, self.message = code, reason, message
        self.field, self.expected, self.actual = field, expected, actual
        self.retryable, self.upstream_status = retryable, upstream_status
        self.filter_counts = filter_counts


_FILTERS = {"unsupported_operation": "非SQL读操作", "missing_result": "缺少执行结果", "invalid_result": "结果无法解析", "failed_result": "执行结果失败", "missing_sql": "缺少SQL", "missing_database_ref": "缺少数据库路径", "invalid_read_sql": "非安全只读单语句", "other": "其他过滤"}


def no_valid_operations_error(total: int, ignored: list[dict]):
    counts: dict[str, int] = {}
    for item in ignored:
        reason = str(item.get("reason", "")).split(":", 1)[0]
        reason = reason if reason in _FILTERS else "other"
        counts[reason] = counts.get(reason, 0) + 1
    message = "审计记录为空，没有可提取的操作。" if not total else f"共{total}条审计操作均被过滤：" + "、".join(f"{_FILTERS[key]}{count}条" for key, count in counts.items()) + "。请核对证据。"
    return CompilerFailure("NO_VALID_OPERATIONS", "NO_VALID_OPERATIONS", message, field="/auditTimeline", filter_counts=counts)


def json_type(value: Any) -> str:
    if value is None: return "null"
    if isinstance(value, bool): return "boolean"
    if isinstance(value, dict): return "object"
    if isinstance(value, list): return "array"
    if isinstance(value, str): return "string"
    return "number" if isinstance(value, (int, float)) else "object"


def validate_assessment_shape(value: Any):
    """Validate field types without echoing any model-produced values."""
    def check(item, expected, field):
        actual = json_type(item)
        if actual != expected:
            raise CompilerFailure("LLM_ANALYSIS_FAILED", "LLM_RESPONSE_SCHEMA_INVALID",
                f"模型返回字段{field}应为{TYPE_NAMES[expected]}，实际为{TYPE_NAMES[actual]}；请重新分析或检查模型结构化输出配置。",
                field=field, expected=expected, actual=actual, retryable=True)

    def field(obj, name, expected, path, required=False):
        location = path + "/" + name
        if name not in obj:
            if required:
                raise CompilerFailure("LLM_ANALYSIS_FAILED", "LLM_REQUIRED_FIELD_MISSING", f"模型返回缺少必填字段{location}，请重新分析。", field=location, expected=expected, actual="missing", retryable=True)
            return [] if expected == "array" else ""
        check(obj[name], expected, location)
        return obj[name]

    check(value, "object", "/")
    for name in ("name", "summary"):
        field(value, name, "string", "", required=True)
    for name in ("matchPhrases", "negativePhrases"):
        for index, item in enumerate(field(value, name, "array", "", required=name == "matchPhrases")):
            check(item, "string", f"/{name}/{index}")
    for index, step in enumerate(field(value, "steps", "array", "")):
        path = f"/steps/{index}"
        check(step, "object", path)
        field(step, "stepId", "string", path, required=True)
        field(step, "title", "string", path)
        for offset, parameter in enumerate(field(step, "parameters", "array", path)):
            location = f"{path}/parameters/{offset}"
            check(parameter, "object", location)
            for key in ("name", "semanticField"):
                field(parameter, key, "string", location, required=True)
        for offset, dependency in enumerate(field(step, "dependencies", "array", path)):
            location = f"{path}/dependencies/{offset}"
            check(dependency, "object", location)
            field(dependency, "sourceStepId", "string", location, required=True)
            for binding_index, binding in enumerate(field(dependency, "bindings", "array", location)):
                source = f"{location}/bindings/{binding_index}"
                check(binding, "object", source)
                field(binding, "parameter", "string", source, required=True)
                field(binding, "jsonPointer", "string", source, required=True)


_FIELDS = {"workflowDefinition", "nodes", "edges", "id", "type", "title", "schemaVersion", "handlerVersion", "entryNodeId", "config", "databaseRef", "sqlTemplate", "inputs", "name", "source", "kind", "key", "nodeId", "jsonPointer", "value", "required", "approvalPolicy", "timeoutSeconds", "sourceStepId", "target", "condition", "default", "fields", "minimum", "maximum", "minLength", "maxLength", "description"}
_RULES = (
    ("SQL 占位符必须与 inputs 完全一致", "SQL_INPUT_MISMATCH", "SQL模板占位符与步骤参数定义不一致，请检查参数化结果。", "inputs"),
    ("数据库路径无效", "DATABASE_REF_INVALID", "步骤数据库路径为空或格式长度不合法，请核查审计details.sql。", "config/databaseRef"),
    ("输出绑定无效", "DEPENDENCY_BINDING_INVALID", "步骤引用的前置节点或JSON Pointer无效，请检查结果绑定。", "inputs/source"),
    ("只能绑定其前置节点输出", "DEPENDENCY_BINDING_INVALID", "步骤引用了非前置节点，请调整依赖顺序。", "inputs/source"),
    ("输入名称重复", "INPUT_NAMES_DUPLICATE", "同一步骤存在重复参数名称，请检查步骤参数。", "inputs"),
    ("不是只读 SQL", "SQL_NOT_READ_ONLY", "生成的SQL模板未通过安全只读校验，请人工核查。", "config/sqlTemplate"),
    ("只能包含一条 SQL", "SQL_MULTIPLE_STATEMENTS", "步骤SQL模板包含多条语句，只允许安全只读单语句。", "config/sqlTemplate"),
    ("SQL模板超过", "SQL_TEMPLATE_TOO_LARGE", "生成的SQL模板超过配置长度，请拆分步骤或调整上限。", "config/sqlTemplate"),
    ("节点 ID 必须唯一", "WORKFLOW_GRAPH_INVALID", "生成的节点ID重复或格式不合法，请检查草稿节点。", "nodes"),
    ("entryNodeId 不存在", "WORKFLOW_GRAPH_INVALID", "生成的入口节点不存在，请检查流程入口。", "entryNodeId"),
)
_RESOURCE_MESSAGES = {
    "COMPILER_NO_CAPACITY": "提取任务名额已满，请稍后重试。", "RUNTIME_NO_CAPACITY": "服务数据预算不足，请等待活动任务或缓存释放。",
    "AUDIT_OPERATIONS_TOO_LARGE": "审计操作数量超过配置上限，请拆分工单证据。", "EVIDENCE_TOO_LARGE": "工单证据字节数或展开对象超过预算，请缩小输入。",
    "COMPILER_NODE_LIMIT_EXCEEDED": "去重后的SQL候选超过499条，请拆分提取。", "SQL_TEMPLATE_TOO_LARGE": "SQL模板超过配置长度，请缩小证据范围。",
    "MODEL_INPUT_TOO_LARGE": "模型分析输入超过字符预算，请缩小工单证据范围或调整配置。", "STUDIO_RESULT_LIMIT": "本地调试临时结果超出缓存额度，请核查已保存状态。",
    "RESOURCE_LIMIT": "提取数据超出处理额度，请拆分输入。", "JSON_COMPLEXITY_LIMIT": "输入JSON结构过深或过复杂，请简化数据。",
}
_INPUT_MESSAGES = {
    "ticketInfo必须是JSON对象，请填写info响应中的data对象": ("TICKET_INFO_INVALID", "/ticketInfo", "ticketInfo必须是info响应中的data对象。"),
    "请填写info响应中的data对象，不要传完整响应": ("TICKET_INFO_INVALID", "/ticketInfo", "传入了完整info响应，请改传其data对象。"),
    "ticketInfo.id必须是正整数": ("TICKET_ID_INVALID", "/ticketInfo/id", "工单详情id必须是正整数。"),
    "ticketId必须是正整数": ("TICKET_ID_INVALID", "/ticketId", "当前工单ticketId必须是正整数。"),
    "ticketId必须与ticketInfo.id一致": ("TICKET_ID_MISMATCH", "/ticketId", "ticketId与工单详情中的id不一致，请核对同一工单。"),
    "请填写audit timeline响应中的data数组，不要传完整响应": ("AUDIT_TIMELINE_INVALID", "/auditTimeline", "auditTimeline必须是audit timeline响应的data数组。"),
    "auditTimeline必须是JSON对象数组": ("AUDIT_TIMELINE_INVALID", "/auditTimeline", "auditTimeline中的每一项必须是JSON对象。"),
}


def _location(exc: Exception) -> str:
    root, result = Path(__file__).resolve().parent.parent, ""
    trace = exc.__traceback__
    while trace:
        try:
            name = Path(trace.tb_frame.f_code.co_filename).resolve().relative_to(root).as_posix()
            function = trace.tb_frame.f_code.co_name
            if name.startswith("app/") and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", function):
                result = f"{name}:{function}:{trace.tb_lineno}"
        except (ValueError, OSError):
            pass
        trace = trace.tb_next
    return result


def failure_details(exc: Exception, stage="ACCEPTED") -> dict:
    stage = stage if stage in STAGE_NAMES else "ACCEPTED"
    code, reason, message, retryable, field = "COMPILER_FAILED", "COMPILER_INTERNAL_ERROR", "", False, ""
    extra = {}
    if isinstance(exc, CompilerFailure):
        code, reason, message, retryable, field = exc.code, exc.reason, exc.message, exc.retryable, exc.field
        if exc.expected: extra["expectedType"] = exc.expected
        if exc.actual: extra["actualType"] = exc.actual
        if type(exc.upstream_status) is int: extra["upstreamStatus"] = exc.upstream_status
        if isinstance(exc.filter_counts, dict):
            extra["filterCounts"] = {key: count for key, count in exc.filter_counts.items() if key in _FILTERS and type(count) is int and count >= 0}
        if isinstance(getattr(exc, "diagnostics", None), dict):
            extra["diagnostics"] = exc.diagnostics
    elif isinstance(exc, ResourceError):
        code = reason = exc.code if exc.code in _RESOURCE_MESSAGES else "RESOURCE_LIMIT"
        message, retryable = _RESOURCE_MESSAGES[reason], exc.retryable
    elif isinstance(exc, ParseError):
        reason, field, message = "SQL_PARSE_FAILED", "/workflowDefinition/nodes", "SQL解析失败，当前解析器无法识别参数化后的语法，请检查步骤SQL模板。"
    elif isinstance(exc, ValueError) and str(exc) in _INPUT_MESSAGES:
        reason, field, message = _INPUT_MESSAGES[str(exc)]
    elif str(exc).startswith("EXTRACTION_CONFLICT"):
        code = reason = "EXTRACTION_CONFLICT"
        message = "本经验已有活动请求，或相同requestId对应的证据不同；请核对请求标识及已有提取状态。"
    elif stage == "FETCHING_EVIDENCE":
        reason = "EVIDENCE_FETCH_FAILED"
        message = "通过aops-cli获取工单证据失败，请检查工单权限、CLI版本、网络及取证调用日志。"
    elif str(exc).startswith("NO_VALID_OPERATIONS"):
        code = reason = "NO_VALID_OPERATIONS"
        message = "审计记录中没有成功且安全的SQL读操作，请核对operation、result和过滤明细。"
    elif str(exc).startswith("LLM_ANALYSIS_UNAVAILABLE"):
        code, reason, retryable = "LLM_ANALYSIS_FAILED", "LLM_NOT_CONFIGURED", True
        message = "未配置LLM_BASE_URL或LLM_MODEL，无法进行模型分析，请检查服务配置。"
    elif str(exc).startswith("LLM_ANALYSIS_FAILED"):
        code, reason, retryable = "LLM_ANALYSIS_FAILED", "LLM_RESPONSE_INVALID", True
        message = "模型未返回有效的结构化经验定义，请检查模型响应格式后重新分析。"
    else:
        errors = exc.errors(include_input=False, include_context=False) if isinstance(exc, ValidationError) else [{"type": "value_error", "loc": (), "msg": str(exc)}]
        error = errors[0]
        location = "/".join(str(item) if isinstance(item, int) or item in _FIELDS else "*" for item in error.get("loc", ()))
        field = "/workflowDefinition" + ("/" + location if location else "") if isinstance(exc, ValidationError) else ""
        for fragment, reason_code, explanation, suffix in _RULES:
            if fragment in error.get("msg", ""):
                reason, message = reason_code, explanation
                match = re.search(r"节点 sql-([1-9][0-9]{0,2}) ", error.get("msg", ""))
                field = f"/workflowDefinition/nodes/{int(match[1])-1}/{suffix}" if match else "/workflowDefinition/" + suffix
                break
        if not message and isinstance(exc, ValidationError):
            reason = "WORKFLOW_SCHEMA_INVALID"
            labels = {"list_type": "应为数组", "string_type": "应为字符串", "dict_type": "应为对象", "missing": "缺少必填字段", "literal_error": "值不在支持范围内"}
            message = f"Workflow字段{field}校验失败：{labels.get(error.get('type'), '结构或类型不符合节点约束')}。请检查对应字段。"
    if not message and stage == "REPORTING_RESULT":
        reason = "RESULT_SERIALIZATION_FAILED"
        message = "生成或同步提取结果失败，结果可能包含不支持的结构，或本地事件缓存不可用；请按错误编号核查。"
    if not message:
        names = {TypeError: "类型处理异常", AttributeError: "字段结构处理异常", KeyError: "必需字段处理异常", MemoryError: "内存不足", ValueError: "未分类的校验异常"}
        message = "服务内部出现" + names.get(type(exc), "处理异常") + "，请提供错误编号给维护人员定位，不要反复提交相同证据。"
    identifier = "err_" + uuid.uuid4().hex
    if isinstance(getattr(exc, "extraction_diagnostics", None), dict):
        extra["diagnostics"] = exc.extraction_diagnostics
    details = {"stage": stage, "reasonCode": reason, "exceptionType": type(exc).__name__, "location": _location(exc), **extra}
    if field: details["field"] = field
    return {"code": code, "message": STAGE_NAMES[stage] + "失败：" + message, "retryable": retryable, "errorId": identifier, "details": details}


def log_compiler_failure(error: dict, *, knowledge_id="", request_id="", event="COMPILER_FAILED"):
    from app.call_logs import emit
    details = error["details"]
    emit(event, "ERROR", peer="COMPILER", direction="INTERNAL", operation="compile_evidence", method=None, url=None, httpStatus=None, exitCode=None, spanId=None, callId=None, knowledgeId=knowledge_id, requestId=request_id,
         businessStatus="FAILED", businessErrorCode=error["code"], errorCode=error["code"], errorMessage=error["message"],
         reasonCode=details["reasonCode"], errorField=details.get("field", ""), stage=details["stage"], errorId=error["errorId"],
         exceptionType=details["exceptionType"], errorLocation=details.get("location", ""), filterSummary=";".join(f"{key}={count}" for key, count in details.get("filterCounts", {}).items()))
