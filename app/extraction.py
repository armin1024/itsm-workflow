from __future__ import annotations

import base64
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from sqlglot import exp, parse, parse_one

from app.cli import CliExecutionError, execute_json_command, redact_diagnostic
from app.config import settings
from app.call_logs import call_span
from app.http_clients import get_llm_client
from app.resources import ResourceError, cpu_run, object_size
from app.workflow import WorkflowDefinition
from app.compiler_errors import CompilerFailure, validate_assessment_shape, no_valid_operations_error, json_type


SEMANTIC_DESCRIPTIONS = {
    "customer_name": "从当前工单上下文确认的查询条件，对应客户姓名",
    "customer_id": "从当前工单上下文确认的查询条件，对应客户编号",
    "phone": "从当前工单上下文确认的查询条件，对应手机号",
    "certificate_id": "从当前工单上下文确认的查询条件，对应证件编号",
    "order_id": "从当前工单上下文确认的查询条件，对应订单编号",
    "ip_address": "从当前工单上下文确认的查询条件，对应 IP 地址",
    "date": "从当前工单上下文确认的查询条件，对应日期",
    "date_range": "从当前工单上下文确认的查询条件，对应日期范围",
    "status": "从当前工单上下文确认的查询条件，对应状态",
    "custom": "待审核的自定义查询条件",
}

_READ_ROOTS = (exp.Select, exp.Show, exp.Describe, exp.Union)
_FORBIDDEN = (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Alter)
ProgressCallback = Callable[[str, str, int | None, int | None], Awaitable[None]]


@dataclass
class Operation:
    operation_id: str
    created_at: int
    sql: str
    normalized_sql: str
    database_ref: str
    audit_index: int = 0


SQL_FAILURE_MESSAGES = {
    "SQL_PARSE_FAILED": "SQL语法无法按当前支持的方言解析，请人工核实。",
    "SQL_NOT_READ_ONLY": "该SQL不符合安全只读要求，未纳入草稿。",
    "SQL_MULTIPLE_STATEMENTS": "该操作包含多条SQL，请拆分为只读单语句。",
    "SQL_TEMPLATE_TOO_LARGE": "该SQL或参数化模板超过单条长度上限，请人工拆分。",
    "SQL_PARAMETERIZATION_FAILED": "该操作无法安全参数化，请人工核实并补充步骤。",
    "SQL_NODE_VALIDATION_FAILED": "参数化后的SQL节点未通过校验，请检查SQL、参数和数据库路径。",
}


def _sql_failure(operation_id: Any, audit_index: int, stage: str, code: str) -> dict[str, Any]:
    return {"operationId": str(operation_id) if operation_id is not None else None,
            "auditIndex": audit_index, "stage": stage, "code": code, "message": SQL_FAILURE_MESSAGES[code]}


def extraction_outcome(diagnostics: dict[str, Any]) -> dict[str, Any]:
    blocked = bool(diagnostics.get("failedOperationCount") or diagnostics.get("blockingIssues"))
    return {"extractionOutcome": "PARTIAL_SUCCESS" if blocked else "SUCCESS", "publishable": not blocked}


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _raw_request(details: dict[str, Any]) -> dict[str, Any]:
    raw = details.get("raw_data") if isinstance(details.get("raw_data"), dict) else {}
    value = raw.get("sqltext")
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        decoded = base64.urlsafe_b64decode(value.strip() + "=" * (-len(value.strip()) % 4))
        parsed = json.loads(decoded.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _successful(value: Any) -> str:
    if value is None or value == "":
        return "missing_result"
    parsed = _object(value)
    if not parsed:
        return "invalid_result"
    if any(parsed.get(key) not in (None, "", [], {}) for key in ("e", "error")):
        return "failed_result"
    if parsed.get("r") is False:
        return "failed_result"
    status = parsed.get("status")
    known_success = type(status) in (int, str) and status in (0, "0", "success", "SUCCESS", "SUCCEEDED")
    return "success" if parsed.get("r") is True or known_success else "failed_result"


def extract_operations(rows: list[dict[str, Any]], failed: list[dict[str, Any]] | None = None) -> tuple[list[Operation], list[dict[str, Any]]]:
    valid: list[Operation] = []
    ignored: list[dict[str, Any]] = []
    failures = failed if failed is not None else []
    for audit_index, row in enumerate(rows):
        operation_id = row.get("id")
        if row.get("operation") != "sql_exec_read":
            ignored.append({"operationId": operation_id, "reason": "unsupported_operation"})
            continue
        result_state = _successful(row.get("result"))
        if result_state != "success":
            ignored.append({"operationId": operation_id, "reason": result_state})
            continue
        details = _object(row.get("details"))
        sql_data = details.get("sql") if isinstance(details.get("sql"), dict) else {}
        raw_data = _raw_request(details)
        command = sql_data.get("command") or raw_data.get("command")
        if not isinstance(command, str) or not command.strip():
            ignored.append({"operationId": operation_id, "reason": "missing_sql"})
            continue
        if len(command) > settings.sql_template_max_chars:
            failures.append(_sql_failure(operation_id, audit_index, "FILTERING_OPERATIONS", "SQL_TEMPLATE_TOO_LARGE"))
            continue
        failure_code = "SQL_PARSE_FAILED"
        try:
            statements = [item for item in parse(command, read="mysql") if item is not None]
            if len(statements) != 1:
                failure_code = "SQL_MULTIPLE_STATEMENTS"
                raise ValueError("only one read statement is permitted")
            expression = statements[0]
            if not isinstance(expression, _READ_ROOTS) or any(expression.find_all(_FORBIDDEN)) or re.search(r"\b(FOR\s+UPDATE|INTO\s+OUTFILE|LOAD_FILE|SLEEP)\b", command, re.I):
                failure_code = "SQL_NOT_READ_ONLY"
                raise ValueError("SQL is not a permitted read statement")
            normalized = expression.sql(dialect="mysql", pretty=False)
        except MemoryError:
            raise
        except Exception:
            failures.append(_sql_failure(operation_id, audit_index, "FILTERING_OPERATIONS", failure_code))
            # Preserve the helper's historical two-value API for library callers.
            if failed is None:
                ignored.append({"operationId": operation_id, "reason": "invalid_read_sql"})
            continue
        database_values = [sql_data.get(key) for key in ("serverid", "dbid", "dbname", "dbuser", "service_name")]
        if not all(value is not None and str(value).strip() for value in database_values):
            ignored.append({"operationId": operation_id, "reason": "missing_database_ref"})
            continue
        try:
            created_at = int(row.get("created_at") or 0)
        except (TypeError, ValueError):
            created_at = 0
        valid.append(Operation(str(operation_id or ""), created_at, command.strip(), normalized, "/".join(str(value).strip() for value in database_values), audit_index))
    valid.sort(key=lambda item: (item.created_at, item.operation_id))
    return valid, ignored


def parameterize_sql(sql: str, step: int) -> tuple[str, list[dict[str, Any]]]:
    expression = parse_one(sql, read="mysql").copy()
    inputs: list[dict[str, Any]] = []
    position = 0
    for literal in list(expression.find_all(exp.Literal)):
        if isinstance(literal.parent, (exp.Limit, exp.Offset)):
            continue
        position += 1
        name = f"step_{step}_value_{position}"
        literal.replace(exp.Var(this=f"__PARAM_{name}__"))
        inputs.append({"name": name, "type": "string" if literal.is_string else "number", "description": SEMANTIC_DESCRIPTIONS["custom"], "source": {"kind": "RUN_INPUT", "key": name}})
    rendered = expression.sql(dialect="mysql", pretty=False)
    for item in inputs:
        rendered = rendered.replace(f"__PARAM_{item['name']}__", "{{" + item["name"] + "}}")
    return rendered, inputs


async def _assess(ticket: dict[str, Any], nodes: list[dict[str, Any]]) -> dict[str, Any]:
    if not settings.llm_base_url or not settings.llm_model:
        raise CompilerFailure("LLM_ANALYSIS_FAILED", "LLM_NOT_CONFIGURED", "未配置LLM_BASE_URL或LLM_MODEL，请检查服务配置。", retryable=True)
    packet = _model_packet(ticket, nodes)
    packet_text = await cpu_run(_encode_model_packet, packet)
    instruction = (
        "你是 AOPS 生产只读工作流提炼器。只返回 JSON 对象，包含 name、summary、matchPhrases、negativePhrases、steps。"
        "name、summary、短语、步骤标题必须为简体中文；summary 用一到三句说明适用场景、查询对象和目的。"
        "steps 每项只包含 stepId、title、parameters、dependencies；parameters 每项只包含 name、semanticField；"
        "dependencies 每项包含 sourceStepId、bindings[{parameter,jsonPointer}]。semanticField 只能取 customer_name、customer_id、"
        "phone、certificate_id、order_id、ip_address、date、date_range、status、custom。禁止复述姓名、手机号、客户号、证件号、"
        "IP、订单号等具体值，禁止修改 SQL、数据库路径或虚构依赖。只允许引用输入中已有的 stepId 和参数。"
        "所有数组字段（matchPhrases、negativePhrases、steps、parameters、dependencies、bindings）必须为JSON数组，"
        "没有内容时使用[]，禁止返回null；name、summary及步骤元数据的字符串字段也禁止返回null。"
    )
    headers = {"Content-Type": "application/json"}
    if settings.llm_api_key:
        headers["Authorization"] = "Bearer " + settings.llm_api_key
    base_body = {"model": settings.llm_model, "temperature": 0, "messages": [{"role": "system", "content": instruction}, {"role": "user", "content": packet_text}]}
    path = "/" + settings.llm_path.strip("/")
    url = settings.llm_base_url.rstrip("/") if settings.llm_base_url.rstrip("/").endswith(path) else settings.llm_base_url.rstrip("/") + path
    modes = [settings.llm_response_format] if settings.llm_response_format != "auto" else ["json_object", "plain"]
    failures: list[str] = []
    result: Any = None
    reason, explanation, upstream_status = "LLM_RESPONSE_INVALID", "模型没有返回有效JSON对象，请检查响应格式。", None
    async with get_llm_client() as client:
        for mode in modes:
            body = dict(base_body)
            if mode == "json_object":
                body["response_format"] = {"type": "json_object"}
            encoded = await cpu_run(lambda: json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))
            with call_span(peer="LLM", direction="OUTBOUND", operation="analyze_evidence", method="POST", url=url, model=settings.llm_model, attempt=modes.index(mode) + 1, maxAttempts=len(modes)) as outcome:
                try:
                    response = await client.post(url, headers=headers, content=encoded, timeout=settings.llm_timeout_seconds)
                    outcome["httpStatus"] = response.status_code
                    if response.is_error:
                        outcome["errorCode"] = "LLM_HTTP_ERROR"
                        failures.append(f"{mode}: 模型接口返回HTTP {response.status_code}")
                        reason, explanation, upstream_status = "LLM_HTTP_ERROR", f"模型网关返回HTTP {response.status_code}，请检查模型服务、认证及网关配置。", response.status_code
                        continue
                    result = await cpu_run(lambda: _parse_assessment(response.json()))
                    if isinstance(result, dict):
                        break
                    outcome["errorCode"] = "LLM_INVALID_RESPONSE"
                    failures.append(f"{mode}: 响应缺少 JSON 对象")
                    reason, explanation, upstream_status = "LLM_RESPONSE_INVALID", "模型响应缺少JSON对象，或choices/message/content结构不符合约定。", None
                except httpx.TimeoutException:
                    outcome["errorCode"] = "LLM_TIMEOUT"
                    failures.append(f"{mode}: 请求超时")
                    reason, explanation, upstream_status = "LLM_TIMEOUT", f"模型调用触发HTTP超时（配置为{settings.llm_timeout_seconds}秒），请检查连接及推理耗时。", None
                except httpx.HTTPError as exc:
                    outcome["errorCode"] = "LLM_UNAVAILABLE"
                    failures.append(f"{mode}: 连接失败 {type(exc).__name__}")
                    reason, explanation, upstream_status = "LLM_UNAVAILABLE", "无法连接模型网关，请检查地址、网络、TLS及服务可用性。", None
                except (ValueError, KeyError, IndexError, TypeError, AttributeError, json.JSONDecodeError):
                    outcome["errorCode"] = "LLM_INVALID_RESPONSE"
                    failures.append(f"{mode}: content 不是有效 JSON")
                    reason, explanation, upstream_status = "LLM_RESPONSE_INVALID", "模型响应不是有效JSON，或choices/message/content结构不符合约定。", None
    if not isinstance(result, dict):
        raise CompilerFailure("LLM_ANALYSIS_FAILED", reason, explanation, retryable=True, upstream_status=upstream_status)
    return result


def _model_packet(ticket: dict[str, Any], nodes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "ticket": {
            "title": ticket.get("event_title"),
            "description": ticket.get("incident_description"),
            "timeline": str(ticket.get("time_lines") or "")[:8000],
            "categories": [ticket.get("category_level_one"), ticket.get("category_level_two")],
        },
        "steps": nodes,
    }


def _encode_model_packet(packet: dict[str, Any]) -> str:
    encoded = json.dumps(packet, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(encoded) > settings.compiler_model_input_max_chars:
        raise ResourceError("MODEL_INPUT_TOO_LARGE", "模型分析输入超出字符预算，请缩小工单证据范围")
    return encoded


def _parse_assessment(payload: Any) -> Any:
    content = payload.get("choices", [{}])[0].get("message", {}).get("content") if isinstance(payload, dict) else None
    if isinstance(content, list):
        content = "".join(str(item.get("text") or item.get("content") or "") for item in content if isinstance(item, dict))
    if isinstance(content, dict):
        return content
    if isinstance(content, str):
        cleaned = content.strip()
        fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", cleaned, re.I | re.S)
        return json.loads(fenced.group(1) if fenced else cleaned)
    return None


def _apply_assessment(nodes: list[dict[str, Any]], assessed: dict[str, Any]) -> list[dict[str, Any]]:
    node_map = {node["id"]: node for node in nodes}
    positions = {node["id"]: index for index, node in enumerate(nodes)}
    issues: list[dict[str, Any]] = []
    def unresolved(node_id: str | None):
        issue = {"code": "DEPENDENCY_REQUIRES_REVIEW", "message": "模型提出的步骤或结果绑定无法安全确认，请核实当前节点的输入来源。",
                 "nodeIds": [node_id] if node_id in node_map else [], "operationIds": []}
        if issue not in issues:
            issues.append(issue)
    for assessed_step in assessed.get("steps", []):
        if not isinstance(assessed_step, dict) or assessed_step.get("stepId") not in node_map:
            unresolved(None)
            continue
        node = node_map[assessed_step["stepId"]]
        title = str(assessed_step.get("title") or "").strip()
        if title:
            node["title"] = title[:200]
        semantics = {str(item.get("name")): str(item.get("semanticField")) for item in assessed_step.get("parameters", []) if isinstance(item, dict)}
        for parameter in node["inputs"]:
            semantic = semantics.get(parameter["name"], "custom")
            if semantic not in SEMANTIC_DESCRIPTIONS:
                semantic = "custom"
            parameter["description"] = SEMANTIC_DESCRIPTIONS[semantic]
        for dependency in assessed_step.get("dependencies", []):
            if not isinstance(dependency, dict):
                continue
            source_id = str(dependency.get("sourceStepId") or "")
            if source_id not in positions or positions[source_id] >= positions[node["id"]]:
                unresolved(node["id"])
                continue
            if not dependency.get("bindings"):
                unresolved(node["id"])
            for binding in dependency.get("bindings", []):
                if not isinstance(binding, dict):
                    continue
                parameter = next((item for item in node["inputs"] if item["name"] == binding.get("parameter")), None)
                pointer = str(binding.get("jsonPointer") or "")
                if parameter and pointer.startswith("/") and len(pointer) <= 1000 and not re.search(r"~(?![01])", pointer):
                    parameter["source"] = {"kind": "NODE_OUTPUT", "nodeId": source_id, "jsonPointer": pointer}
                else:
                    unresolved(node["id"])
    return issues


def validate_ticket_evidence(ticket: Any, rows: Any, ticket_id: int | None = None) -> int:
    """Accept only the data object and data array, never an AOPS response envelope."""
    if not isinstance(ticket, dict):
        raise ValueError("ticketInfo必须是JSON对象，请填写info响应中的data对象")
    if "data" in ticket and any(key in ticket for key in ("status", "msg", "message")):
        raise ValueError("请填写info响应中的data对象，不要传完整响应")
    source_id = ticket.get("id")
    if isinstance(source_id, bool) or not isinstance(source_id, int) or source_id < 1:
        raise ValueError("ticketInfo.id必须是正整数")
    if ticket_id is not None and (isinstance(ticket_id, bool) or not isinstance(ticket_id, int) or ticket_id < 1):
        raise ValueError("ticketId必须是正整数")
    if ticket_id is not None and ticket_id != source_id:
        raise ValueError("ticketId必须与ticketInfo.id一致")
    if ticket.get("system_list") is not None and not isinstance(ticket["system_list"], list):
        raise CompilerFailure("COMPILER_FAILED", "TICKET_INFO_SCHEMA_INVALID", "工单详情字段/ticketInfo/system_list应为数组或null，请核对info.data格式。", field="/ticketInfo/system_list", expected="array", actual=json_type(ticket["system_list"]))
    if not isinstance(rows, list):
        raise ValueError("请填写audit timeline响应中的data数组，不要传完整响应")
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError("auditTimeline必须是JSON对象数组")
    return source_id


def validate_evidence_limits(ticket: Any, rows: Any, ticket_id: int | None = None) -> int:
    """Pure preflight, before AST parsing, model I/O or asynchronous admission."""
    validate_ticket_evidence(ticket, rows, ticket_id)
    if len(rows) > settings.compiler_max_audit_operations:
        raise ResourceError("AUDIT_OPERATIONS_TOO_LARGE", "操作记录数量超过允许上限，请拆分工单证据")
    maximum = settings.compiler_evidence_max_mb * 1024 * 1024
    evidence = {"ticketInfo": ticket, "auditTimeline": rows}
    encoded = json.dumps(evidence, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > maximum:
        raise ResourceError("EVIDENCE_TOO_LARGE", "工单证据超过字节上限，请缩小输入", status_code=413)
    # Byte limits and Python object accounting are deliberately distinct.
    # The caller reserves the expanded estimate before retaining its copy.
    limit = settings.runtime_data_budget_mb * 1024 * 1024
    length = object_size(evidence, limit=limit)
    if length > limit:
        raise ResourceError("EVIDENCE_TOO_LARGE", "工单证据展开后超过服务数据预算", status_code=413)
    return length


def _candidate_nodes(operations: list[Operation], ignored: list[dict[str, Any]], failed: list[dict[str, Any]] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    # This function owns its new containers; it does not mutate compiler state.
    ignored = list(ignored)
    nodes: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for operation in operations:
        key = (operation.database_ref, operation.normalized_sql)
        if key in seen:
            ignored.append({"operationId": operation.operation_id, "reason": "duplicate_read_operation"})
            continue
        if len(nodes) >= 499:
            raise ResourceError("COMPILER_NODE_LIMIT_EXCEEDED", "候选SQL最多499条，请拆分工单证据")
        number = len(nodes) + 1
        code = "SQL_PARAMETERIZATION_FAILED"
        try:
            sql, inputs = parameterize_sql(operation.sql, number)
            if len(sql) > settings.sql_template_max_chars:
                code = "SQL_TEMPLATE_TOO_LARGE"
                raise ValueError("template too large")
            node = {"id": f"sql-{number}", "type": "sql_read", "title": f"只读查询步骤 {number}", "config": {"databaseRef": operation.database_ref, "sqlTemplate": sql}, "inputs": inputs, "approvalPolicy": "PLAN", "timeoutSeconds": 600}
            code = "SQL_NODE_VALIDATION_FAILED"
            WorkflowDefinition.model_validate({"entryNodeId": node["id"], "nodes": [node], "edges": []})
        except (ResourceError, MemoryError):
            raise
        except Exception:
            if failed is None:
                raise
            failed.append(_sql_failure(operation.operation_id or None, operation.audit_index, "PARAMETERIZING_SQL", code))
            continue
        seen.add(key)
        nodes.append(node)
    return nodes, ignored


def _operation_diagnostics(total: int, accepted: int, ignored: list[dict[str, Any]], failed: list[dict[str, Any]], issues: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"auditOperationCount": total, "acceptedOperationCount": accepted,
            "ignoredOperationCount": len(ignored), "ignoredOperations": ignored,
            "failedOperationCount": len(failed), "failedOperations": failed, "blockingIssues": issues or []}


def _no_operations(total: int, ignored: list[dict[str, Any]], failed: list[dict[str, Any]]) -> CompilerFailure:
    error = no_valid_operations_error(total, ignored)
    if failed:
        error.message = f"共{total}条审计操作，正常过滤{len(ignored)}条、处理异常{len(failed)}条，没有可用SQL节点。请查看提取诊断。"
        error.args = (error.message,)
    error.diagnostics = _operation_diagnostics(total, 0, ignored, failed)
    return error


async def compile_ticket_evidence(ticket: dict[str, Any], rows: list[dict[str, Any]], *, progress: ProgressCallback | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compile evidence into a storage-agnostic DraftProposal."""
    await cpu_run(validate_evidence_limits, ticket, rows)
    if progress:
        await progress("FILTERING_OPERATIONS", "正在过滤有效 SQL", 0, len(rows))
    failed: list[dict[str, Any]] = []
    operations, ignored = await cpu_run(extract_operations, rows, failed)
    if not operations:
        raise _no_operations(len(rows), ignored, failed)
    if progress:
        await progress("PARAMETERIZING_SQL", "正在参数化 SQL", 0, len(operations))
    nodes, ignored = await cpu_run(_candidate_nodes, operations, ignored, failed)
    if not nodes:
        raise _no_operations(len(rows), ignored, failed)
    try:
        return await _finish_draft(ticket, rows, nodes, ignored, failed, progress)
    except Exception as exc:
        exc.extraction_diagnostics = _operation_diagnostics(len(rows), len(nodes), ignored, failed)
        raise


async def _finish_draft(ticket, rows, nodes, ignored, failed, progress):
    # Enforce model budget even when the adapter is mocked or replaced.
    await cpu_run(_encode_model_packet, _model_packet(ticket, nodes))
    if progress:
        await progress("ANALYZING_EVIDENCE", "正在分析工单和操作记录", None, None)
    assessed = await _assess(ticket, nodes)
    await cpu_run(validate_assessment_shape, assessed)
    if progress:
        await progress("BUILDING_WORKFLOW", "正在生成 Workflow 草稿", None, None)
    proposal, diagnostics = await cpu_run(_assemble_proposal, ticket, rows, nodes, ignored, assessed, failed)
    if progress:
        await progress("VALIDATING_WORKFLOW", "正在校验 Workflow", None, None)
    definition = await cpu_run(WorkflowDefinition.model_validate, proposal["workflowDefinition"])
    proposal["workflowDefinition"] = await cpu_run(definition.model_dump, mode="json")
    return proposal, diagnostics


def _assemble_proposal(ticket: dict[str, Any], rows: list[dict[str, Any]], nodes: list[dict[str, Any]], ignored: list[dict[str, Any]], assessed: dict[str, Any], failed: list[dict[str, Any]] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    # Work on a private copy; applying model metadata never mutates loop-owned state.
    import copy
    validate_assessment_shape(assessed)
    nodes = copy.deepcopy(nodes)
    issues = _apply_assessment(nodes, assessed)
    name = str(assessed.get("name") or "").strip()[:200]
    summary = str(assessed.get("summary") or "").strip()[:2000]
    phrases = [str(value).strip()[:200] for value in assessed.get("matchPhrases", []) if str(value).strip()][:20]
    negatives = [str(value).strip()[:200] for value in assessed.get("negativePhrases", []) if str(value).strip()][:20]
    for field, value in (("name", name), ("summary", summary)):
        if not value or not any("\u4e00" <= char <= "\u9fff" for char in value):
            raise CompilerFailure("LLM_ANALYSIS_FAILED", "LLM_CONTENT_INVALID", f"模型字段/{field}为空或不是要求的中文内容，请重新分析或调整模型提示词。", field="/" + field, retryable=True)
    if not phrases:
        raise CompilerFailure("LLM_ANALYSIS_FAILED", "LLM_CONTENT_INVALID", "模型字段/matchPhrases没有有效匹配短语，请重新分析。", field="/matchPhrases", retryable=True)
    end_node = {"id": "done", "type": "end", "title": "完成并展示结果", "config": {}, "inputs": [], "approvalPolicy": "NONE", "timeoutSeconds": 60}
    all_nodes = nodes + [end_node]
    edges = [{"id": f"edge-{index + 1}", "source": node["id"], "target": all_nodes[index + 1]["id"]} for index, node in enumerate(nodes)]
    definition = {"entryNodeId": nodes[0]["id"], "nodes": all_nodes, "edges": edges}
    systems = [str(item.get("id") or item.get("name")) for item in (ticket.get("system_list") or []) if isinstance(item, dict) and (item.get("id") or item.get("name"))]
    proposal = {"name": name, "summary": summary, "matchPhrases": phrases, "negativePhrases": negatives, "systemKeys": systems[:20], "workflowDefinition": definition}
    diagnostics = {"ticketNo": str(ticket.get("incident_id") or "").strip()[:120] or None, **_operation_diagnostics(len(rows), len(nodes), ignored, failed or [], issues), "llmMode": "structured"}
    return proposal, diagnostics


async def fetch_ticket_evidence(ticket_id: int, api_key: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Fetch evidence for TEST_ONLY Studio compilation without persisting credentials."""
    try:
        info = await execute_json_command(["event-center", "info", "--id", str(ticket_id)], api_key=api_key, timeout_seconds=30)
        timeline = await execute_json_command(["event-center", "audit_timeline", "--id", str(ticket_id)], api_key=api_key, timeout_seconds=60)
    except CliExecutionError as exc:
        message, _ = redact_diagnostic(str(exc), 1000)
        raise ValueError(f"AOPS_CLI_{exc.code}：{message}") from exc
    ticket = info.payload.get("data")
    rows = timeline.payload.get("data")
    if not isinstance(ticket, dict):
        raise ValueError("AOPS_CLI_INVALID_INFO：info响应data不是对象")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("AOPS_CLI_INVALID_TIMELINE：audit_timeline响应data不是对象数组")
    return ticket, rows


async def compile_ticket_id(ticket_id: int, api_key: str) -> tuple[dict[str, Any], dict[str, Any]]:
    ticket, rows = await fetch_ticket_evidence(ticket_id, api_key)
    proposal, diagnostics = await compile_ticket_evidence(ticket, rows)
    return proposal, {"ticketId": ticket_id, **diagnostics}
