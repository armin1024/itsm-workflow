from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from typing import Any

import httpx
from sqlglot import exp, parse_one

from app.cli import CliExecutionError, execute_json_command
from app.config import settings
from app.workflow import WorkflowDefinition, WorkflowEdge, WorkflowNode


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


@dataclass
class Operation:
    operation_id: str
    created_at: int
    sql: str
    normalized_sql: str
    database_ref: str


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
    error = parsed.get("e", parsed.get("error", ""))
    if error not in (None, "", [], {}):
        return "failed_result"
    return "success" if parsed.get("r") is True or parsed.get("status") in {0, "0", "success", "SUCCEEDED"} else "failed_result"


def extract_operations(rows: list[dict[str, Any]]) -> tuple[list[Operation], list[dict[str, Any]]]:
    valid: list[Operation] = []
    ignored: list[dict[str, Any]] = []
    for row in rows:
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
        try:
            expression = parse_one(command, read="mysql")
            if not isinstance(expression, _READ_ROOTS) or any(expression.find_all(_FORBIDDEN)) or re.search(r"\b(FOR\s+UPDATE|INTO\s+OUTFILE|LOAD_FILE|SLEEP)\b", command, re.I):
                raise ValueError("SQL is not a permitted read statement")
            normalized = expression.sql(dialect="mysql", pretty=False)
        except Exception as exc:
            ignored.append({"operationId": operation_id, "reason": f"invalid_read_sql:{exc}"})
            continue
        database_values = [sql_data.get(key) for key in ("serverid", "dbid", "dbname", "dbuser", "service_name")]
        if not all(value is not None and str(value).strip() for value in database_values):
            ignored.append({"operationId": operation_id, "reason": "missing_database_ref"})
            continue
        valid.append(Operation(str(operation_id or ""), int(row.get("created_at") or 0), command.strip(), normalized, "/".join(str(value).strip() for value in database_values)))
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
        raise ValueError("LLM_ANALYSIS_UNAVAILABLE：请配置 LLM_BASE_URL 和 LLM_MODEL")
    packet = {
        "ticket": {
            "title": ticket.get("event_title"),
            "description": ticket.get("incident_description"),
            "timeline": str(ticket.get("time_lines") or "")[:8000],
            "categories": [ticket.get("category_level_one"), ticket.get("category_level_two")],
        },
        "steps": nodes,
    }
    instruction = (
        "你是 AOPS 生产只读工作流提炼器。只返回 JSON 对象，包含 name、summary、matchPhrases、negativePhrases、steps。"
        "name、summary、短语、步骤标题必须为简体中文；summary 用一到三句说明适用场景、查询对象和目的。"
        "steps 每项只包含 stepId、title、parameters、dependencies；parameters 每项只包含 name、semanticField；"
        "dependencies 每项包含 sourceStepId、bindings[{parameter,jsonPointer}]。semanticField 只能取 customer_name、customer_id、"
        "phone、certificate_id、order_id、ip_address、date、date_range、status、custom。禁止复述姓名、手机号、客户号、证件号、"
        "IP、订单号等具体值，禁止修改 SQL、数据库路径或虚构依赖。只允许引用输入中已有的 stepId 和参数。"
    )
    headers = {"Content-Type": "application/json"}
    if settings.llm_api_key:
        headers["Authorization"] = "Bearer " + settings.llm_api_key
    base_body = {"model": settings.llm_model, "temperature": 0, "messages": [{"role": "system", "content": instruction}, {"role": "user", "content": json.dumps(packet, ensure_ascii=False)}]}
    path = "/" + settings.llm_path.strip("/")
    url = settings.llm_base_url.rstrip("/") if settings.llm_base_url.rstrip("/").endswith(path) else settings.llm_base_url.rstrip("/") + path
    modes = [settings.llm_response_format] if settings.llm_response_format != "auto" else ["json_object", "plain"]
    failures: list[str] = []
    result: Any = None
    async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
        for mode in modes:
            body = dict(base_body)
            if mode == "json_object":
                body["response_format"] = {"type": "json_object"}
            try:
                response = await client.post(url, headers=headers, json=body)
                if response.is_error:
                    detail = re.sub(r"\s+", " ", response.text)[:300]
                    failures.append(f"{mode}: HTTP {response.status_code} {detail}")
                    continue
                payload = response.json()
                content = payload.get("choices", [{}])[0].get("message", {}).get("content") if isinstance(payload, dict) else None
                if isinstance(content, list):
                    content = "".join(str(item.get("text") or item.get("content") or "") for item in content if isinstance(item, dict))
                if isinstance(content, dict):
                    result = content
                elif isinstance(content, str):
                    cleaned = content.strip()
                    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", cleaned, re.I | re.S)
                    result = json.loads(fenced.group(1) if fenced else cleaned)
                if isinstance(result, dict):
                    break
                failures.append(f"{mode}: 响应缺少 JSON 对象")
            except httpx.TimeoutException:
                failures.append(f"{mode}: 请求超时")
            except httpx.HTTPError as exc:
                failures.append(f"{mode}: 连接失败 {type(exc).__name__}")
            except (ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError):
                failures.append(f"{mode}: content 不是有效 JSON")
    if not isinstance(result, dict):
        raise ValueError("LLM_ANALYSIS_FAILED：" + "；".join(failures[:2]))
    return result


def _apply_assessment(nodes: list[dict[str, Any]], assessed: dict[str, Any]) -> None:
    node_map = {node["id"]: node for node in nodes}
    positions = {node["id"]: index for index, node in enumerate(nodes)}
    for assessed_step in assessed.get("steps", []):
        if not isinstance(assessed_step, dict) or assessed_step.get("stepId") not in node_map:
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
                continue
            for binding in dependency.get("bindings", []):
                if not isinstance(binding, dict):
                    continue
                parameter = next((item for item in node["inputs"] if item["name"] == binding.get("parameter")), None)
                pointer = str(binding.get("jsonPointer") or "")
                if parameter and pointer.startswith("/"):
                    parameter["source"] = {"kind": "NODE_OUTPUT", "nodeId": source_id, "jsonPointer": pointer[:1000]}


async def compile_ticket_evidence(ticket: dict[str, Any], rows: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compile evidence into a storage-agnostic DraftProposal."""
    if not isinstance(ticket, dict):
        raise ValueError("ticketInfo必须是JSON对象")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("auditTimeline必须是JSON对象数组")
    operations, ignored = extract_operations(rows)
    if not operations:
        raise ValueError("NO_VALID_OPERATIONS：没有成功且安全的 SQL 读操作")
    nodes: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for operation in operations:
        key = (operation.database_ref, operation.normalized_sql)
        if key in seen:
            ignored.append({"operationId": operation.operation_id, "reason": "duplicate_read_operation"})
            continue
        seen.add(key)
        number = len(nodes) + 1
        sql, inputs = parameterize_sql(operation.sql, number)
        nodes.append({"id": f"sql-{number}", "type": "sql_read", "title": f"只读查询步骤 {number}", "config": {"databaseRef": operation.database_ref, "sqlTemplate": sql}, "inputs": inputs, "approvalPolicy": "PLAN", "timeoutSeconds": 600})
    assessed = await _assess(ticket, nodes)
    _apply_assessment(nodes, assessed)
    name = str(assessed.get("name") or "").strip()[:200]
    summary = str(assessed.get("summary") or "").strip()[:2000]
    phrases = [str(value).strip()[:200] for value in assessed.get("matchPhrases", []) if str(value).strip()][:20]
    negatives = [str(value).strip()[:200] for value in assessed.get("negativePhrases", []) if str(value).strip()][:20]
    if not name or not summary or not phrases or not any("\u4e00" <= char <= "\u9fff" for char in name + summary):
        raise ValueError("LLM_ANALYSIS_FAILED：名称、中文摘要或匹配短语缺失")
    end_node = {"id": "done", "type": "end", "title": "完成并展示结果", "config": {}, "inputs": [], "approvalPolicy": "NONE", "timeoutSeconds": 60}
    all_nodes = nodes + [end_node]
    edges = [WorkflowEdge(id=f"edge-{index + 1}", source=node["id"], target=all_nodes[index + 1]["id"]) for index, node in enumerate(nodes)]
    definition = WorkflowDefinition(entryNodeId=nodes[0]["id"], nodes=[WorkflowNode.model_validate(node) for node in all_nodes], edges=edges)
    systems = [str(item.get("id") or item.get("name")) for item in ticket.get("system_list", []) if isinstance(item, dict) and (item.get("id") or item.get("name"))]
    proposal = {"name": name, "summary": summary, "matchPhrases": phrases, "negativePhrases": negatives, "systemKeys": systems[:20], "workflowDefinition": definition.model_dump(mode="json")}
    diagnostics = {"ticketNo": str(ticket.get("incident_id") or "").strip()[:120] or None, "auditOperationCount": len(rows), "acceptedOperationCount": len(nodes), "ignoredOperationCount": len(ignored), "ignoredOperations": ignored, "llmMode": "structured"}
    return proposal, diagnostics


async def fetch_ticket_evidence(ticket_id: int, api_key: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Fetch evidence for TEST_ONLY Studio compilation without persisting credentials."""
    try:
        info = await execute_json_command(["event-center", "info", "--id", str(ticket_id)], api_key=api_key, timeout_seconds=30)
        timeline = await execute_json_command(["event-center", "audit_timeline", "--id", str(ticket_id)], api_key=api_key, timeout_seconds=60)
    except CliExecutionError as exc:
        raise ValueError(f"AOPS_CLI_{exc.code}：{exc}") from exc
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
