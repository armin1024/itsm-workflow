from __future__ import annotations

import copy
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.crypto import canonical_hash
from app.knowledge import add_lifecycle
from app.models import Knowledge, KnowledgeTransferAudit, KnowledgeUser, WorkflowVersion
from app.workflow import WorkflowDefinition, legacy_steps_to_workflow


SCHEMA = "itsm-workflow-export"
VERSION = 2
MAX_ITEMS = 500
MAX_BYTES = 10 * 1024 * 1024


def _id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex


def _clean_uid(value: Any, label: str) -> str:
    uid = str(value or "").strip()
    if not uid or len(uid) > 120:
        raise ValueError(f"{label}不能为空且不能超过120字符")
    return uid


def _uids(values: Any) -> list[str]:
    if not isinstance(values, list):
        raise ValueError("uids必须是数组")
    result = list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
    if len(result) > 100 or any(len(value) > 120 for value in result):
        raise ValueError("uids最多100项，每项不能超过120字符")
    return result


def _ids(values: Any) -> list[str]:
    if not isinstance(values, list):
        raise ValueError("knowledgeIds必须是数组")
    result = list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
    if not result or len(result) > MAX_ITEMS:
        raise ValueError(f"knowledgeIds必须包含1至{MAX_ITEMS}项")
    return result


def _mappings(values: Any) -> dict[str, str]:
    if values is None:
        return {}
    if not isinstance(values, list):
        raise ValueError("databaseMappings必须是数组")
    result: dict[str, str] = {}
    for item in values:
        if not isinstance(item, dict):
            raise ValueError("databaseMappings元素必须是对象")
        source, target = str(item.get("sourceRef") or "").strip(), str(item.get("targetRef") or "").strip()
        if not source or not target or max(len(source), len(target)) > 1000 or any(ord(char) < 32 for char in source + target):
            raise ValueError("数据库路径不能为空、包含控制字符或超过1000字符")
        if source in result and result[source] != target:
            raise ValueError(f"数据库路径存在冲突映射：{source}")
        result[source] = target
    return result


def _refs(definition: dict[str, Any]) -> list[tuple[str, str, str]]:
    result = []
    for node in definition.get("nodes") or []:
        if node.get("type") == "sql_read":
            ref = str((node.get("config") or {}).get("databaseRef") or "").strip()
            if ref:
                result.append((ref, str(node.get("id") or ""), str(node.get("title") or "")))
    return result


def _map_definition(definition: dict[str, Any], mappings: dict[str, str]) -> dict[str, Any]:
    result = copy.deepcopy(definition)
    for node in result.get("nodes") or []:
        if node.get("type") == "sql_read":
            config = node.setdefault("config", {})
            source = str(config.get("databaseRef") or "")
            config["databaseRef"] = mappings.get(source, source)
    return WorkflowDefinition.model_validate(result).model_dump(mode="json")


async def _records(session: AsyncSession, values: Any) -> list[Knowledge]:
    ids = _ids(values)
    result = await session.execute(select(Knowledge).where(Knowledge.id.in_(ids), Knowledge.status != "DELETED"))
    records = {item.id: item for item in result.scalars()}
    missing = [value for value in ids if value not in records]
    if missing:
        raise ValueError("经验不存在：" + ", ".join(missing))
    return [records[value] for value in ids]


async def export_preview(session: AsyncSession, knowledge_ids: Any) -> dict[str, Any]:
    if not settings.knowledge_environment_name.strip():
        raise ValueError("KNOWLEDGE_ENVIRONMENT_NAME未配置，禁止导出")
    records = await _records(session, knowledge_ids)
    paths: dict[str, list[dict[str, Any]]] = {}
    items, blockers = [], []
    for record in records:
        definition = record.draft_definition
        version_id = None
        if record.status == "PUBLISHED" and record.published_version_id:
            version = await session.get(WorkflowVersion, record.published_version_id)
            if version:
                definition, version_id = version.definition, version.id
        try:
            normalized = WorkflowDefinition.model_validate(definition).model_dump(mode="json")
        except ValueError as exc:
            blockers.append(f"{record.id}工作流无效：{exc}")
            continue
        for ref, node_id, title in _refs(normalized):
            paths.setdefault(ref, []).append({"knowledgeId": record.id, "nodeId": node_id, "nodeTitle": title})
        items.append({"knowledgeId": record.id, "name": record.name, "summary": record.summary, "sourceStatus": record.status, "sourceVersionId": version_id, "nodeCount": len(normalized["nodes"])})
    return {"sourceEnvironment": settings.knowledge_environment_name, "knowledgeCount": len(records), "items": items, "databasePaths": [{"sourceRef": ref, "targetRef": ref, "usageCount": len(usages), "usages": usages} for ref, usages in sorted(paths.items())], "blockers": blockers, "exportable": not blockers, "confirmationRequired": True}


async def export_package(session: AsyncSession, body: dict[str, Any], operator_uid: str) -> dict[str, Any]:
    preview = await export_preview(session, body.get("knowledgeIds"))
    if body.get("confirmed") is not True:
        return preview
    if preview["blockers"]:
        raise ValueError("存在阻止导出的结构问题")
    mappings = _mappings(body.get("databaseMappings"))
    required = {item["sourceRef"] for item in preview["databasePaths"]}
    if set(mappings) != required:
        raise ValueError("databaseMappings必须完整且仅包含预览中的去重路径")
    records = await _records(session, body.get("knowledgeIds"))
    package_id, items = _id("pkg_"), []
    for record in records:
        source_version_id, definition = None, record.draft_definition
        if record.status == "PUBLISHED" and record.published_version_id:
            version = await session.get(WorkflowVersion, record.published_version_id)
            if version:
                source_version_id, definition = version.id, version.definition
        portable = _map_definition(definition, mappings)
        item_definition = {"name": record.name, "summary": record.summary, "matchPhrases": record.match_phrases, "negativePhrases": record.negative_phrases, "systemKeys": record.system_keys, "creatorUid": record.creator_uid, "uids": [item.uid for item in record.users], "workflowDefinition": portable}
        items.append({"sourceKnowledgeId": record.id, "sourceVersionId": source_version_id, "sourceStatus": record.status, "sourceTicketId": record.source_ticket_id, "sourceTicketNo": record.source_ticket_no, "contentHash": canonical_hash(item_definition), "definition": item_definition})
    package = {"schema": SCHEMA, "schemaVersion": VERSION, "packageId": package_id, "sourceEnvironment": settings.knowledge_environment_name, "exportedAt": datetime.now(UTC).isoformat(), "operatorUid": operator_uid, "databaseMappings": [{"sourceRef": source, "targetRef": target} for source, target in mappings.items()], "items": items}
    if len(json.dumps(package, ensure_ascii=False).encode()) > MAX_BYTES:
        raise ValueError("导出包超过10 MiB，请拆分经验")
    session.add(KnowledgeTransferAudit(id=_id("kta_"), action="EXPORT", operator_uid=operator_uid, package_id=package_id, details={"knowledgeIds": [item.id for item in records], "databaseMappings": package["databaseMappings"]}))
    await session.commit()
    return package


def _package_items(package: Any) -> tuple[list[dict[str, Any]], bool]:
    if not isinstance(package, dict) or len(json.dumps(package, ensure_ascii=False).encode()) > MAX_BYTES:
        raise ValueError("package必须是未超过10 MiB的JSON对象")
    legacy = package.get("schema") == "aops-workflow-knowledge-export" and package.get("schemaVersion") == 1
    if not legacy and (package.get("schema") != SCHEMA or package.get("schemaVersion") != VERSION):
        raise ValueError("不支持的导入包Schema或版本")
    if not legacy and (not str(package.get("packageId") or "").strip() or not str(package.get("sourceEnvironment") or "").strip()):
        raise ValueError("原生导入包缺少packageId或sourceEnvironment")
    items = package.get("items")
    if not isinstance(items, list) or not items or len(items) > MAX_ITEMS:
        raise ValueError(f"导入包items必须包含1至{MAX_ITEMS}项")
    return items, legacy


def _overrides(values: Any) -> dict[int, dict[str, Any]]:
    result = {}
    for item in values or []:
        if not isinstance(item, dict) or not isinstance(item.get("itemIndex"), int):
            raise ValueError("knowledgeOverrides需要整数itemIndex")
        result[item["itemIndex"]] = item
    return result


async def import_preview(session: AsyncSession, body: dict[str, Any]) -> dict[str, Any]:
    package = body.get("package")
    items, legacy = _package_items(package)
    mappings, overrides = _mappings(body.get("databaseMappings")), _overrides(body.get("knowledgeOverrides"))
    prepared, warnings, paths = [], [], {}
    for index, item in enumerate(items):
        raw = item.get("definition") if isinstance(item, dict) else None
        if not isinstance(raw, dict):
            raise ValueError(f"items[{index}]定义无效")
        if not str(raw.get("name") or "").strip() or not str(raw.get("summary") or "").strip() or not isinstance(raw.get("matchPhrases"), list) or not raw.get("matchPhrases"):
            raise ValueError(f"items[{index}]缺少名称、摘要或匹配短语")
        if not legacy:
            if not str(item.get("sourceKnowledgeId") or "").strip() or not str(item.get("contentHash") or "").strip():
                raise ValueError(f"items[{index}]缺少来源知识ID或内容哈希")
            source_hash = str(item.get("contentHash"))
            calculated_source_hash = canonical_hash(raw)
            if calculated_source_hash != source_hash:
                warnings.append(f"items[{index}]内容与导出时的来源哈希不同；将按当前JSON内容重新计算有效哈希")
        else:
            source_hash = str(item.get("contentHash") or canonical_hash(raw))
        workflow = legacy_steps_to_workflow(raw.get("steps") or []).model_dump(mode="json") if legacy else raw.get("workflowDefinition")
        workflow = _map_definition(workflow, mappings)
        override = overrides.get(index, {})
        creator = _clean_uid(override.get("creatorUid", raw.get("creatorUid")), f"items[{index}].creatorUid")
        allowed = _uids(override.get("uids", raw.get("uids", [])))
        source_id, source_version = str(item.get("sourceKnowledgeId") or ""), str(item.get("sourceVersionId") or "")
        effective_definition = {
            "name": str(raw.get("name") or "导入经验")[:200],
            "summary": str(raw.get("summary") or "")[:2000],
            "matchPhrases": [str(value)[:200] for value in raw.get("matchPhrases", [])][:20],
            "negativePhrases": [str(value)[:200] for value in raw.get("negativePhrases", [])][:20],
            "systemKeys": [str(value)[:200] for value in raw.get("systemKeys", [])][:20],
            "creatorUid": creator,
            "uids": allowed,
            "workflowDefinition": workflow,
        }
        content_hash = canonical_hash(effective_definition)
        duplicate = await session.scalar(select(Knowledge.id).where(Knowledge.origin_environment == str(package.get("sourceEnvironment") or "legacy"), Knowledge.origin_knowledge_id == source_id, Knowledge.origin_version_id == (source_version or None), Knowledge.origin_content_hash == content_hash, Knowledge.status != "DELETED"))
        action = str(override.get("action") or ("SKIP" if duplicate else "CREATE")).upper()
        if action not in {"CREATE", "SKIP", "COPY"}:
            raise ValueError(f"items[{index}].action只允许CREATE、SKIP或COPY")
        if duplicate:
            warnings.append(f"items[{index}]与{duplicate}来源和内容完全一致，默认跳过")
        for ref, _, _ in _refs(workflow):
            paths[ref] = paths.get(ref, 0) + 1
        prepared.append({"itemIndex": index, "sourceKnowledgeId": source_id, "sourceVersionId": source_version or None, "name": effective_definition["name"], "summary": effective_definition["summary"], "creatorUid": creator, "uids": allowed, "visibility": "RESTRICTED" if allowed else "PUBLIC", "action": action, "duplicateKnowledgeId": duplicate, "sourceContentHash": source_hash, "effectiveContentHash": content_hash, "contentHashMismatch": source_hash != calculated_source_hash if not legacy else False, "contentHash": content_hash, "workflowDefinition": workflow})
    return {"packageId": str(package.get("packageId") or "legacy-" + _id("pkg_")), "sourceEnvironment": str(package.get("sourceEnvironment") or "legacy"), "schemaVersion": 1 if legacy else VERSION, "itemCount": len(prepared), "items": prepared, "databasePaths": [{"sourceRef": ref, "targetRef": ref, "usageCount": count} for ref, count in sorted(paths.items())], "warnings": warnings, "confirmationRequired": True}


async def import_package(session: AsyncSession, body: dict[str, Any], operator_uid: str) -> dict[str, Any]:
    preview = await import_preview(session, body)
    if body.get("confirmed") is not True:
        return preview
    package, created, skipped = body["package"], [], []
    source_environment = str(package.get("sourceEnvironment") or "legacy")
    for item in preview["items"]:
        if item["action"] == "SKIP":
            skipped.append(item["itemIndex"])
            continue
        raw = package["items"][item["itemIndex"]]["definition"]
        record = Knowledge(id=_id("knw_"), status="PENDING_REVIEW", name=item["name"][:200], summary=item["summary"][:2000], match_phrases=[str(value)[:200] for value in raw.get("matchPhrases", [])][:20] or [item["name"][:200]], negative_phrases=[str(value)[:200] for value in raw.get("negativePhrases", [])][:20], system_keys=[str(value)[:200] for value in raw.get("systemKeys", [])][:20], creator_uid=item["creatorUid"], public=not item["uids"], source_type="IMPORT", source_ticket_id=package["items"][item["itemIndex"]].get("sourceTicketId"), source_ticket_no=package["items"][item["itemIndex"]].get("sourceTicketNo"), draft_definition=item["workflowDefinition"], submitted_at=datetime.now(UTC), submitted_by=operator_uid, origin_environment=source_environment, origin_knowledge_id=item["sourceKnowledgeId"] or None, origin_version_id=item["sourceVersionId"], origin_content_hash=item["contentHash"], import_package_id=preview["packageId"])
        session.add(record)
        await session.flush()
        for uid in item["uids"]:
            session.add(KnowledgeUser(knowledge_id=record.id, uid=uid))
        add_lifecycle(session, record, "IMPORTED", operator_uid, f"从{source_environment}导入并提交审核", source="IMPORT")
        created.append(record.id)
    session.add(KnowledgeTransferAudit(id=_id("kta_"), action="IMPORT", operator_uid=operator_uid, package_id=preview["packageId"], details={"createdKnowledgeIds": created, "skippedItemIndexes": skipped, "sourceEnvironment": source_environment}))
    await session.commit()
    return {"packageId": preview["packageId"], "createdKnowledgeIds": created, "skippedItemIndexes": skipped, "status": "PENDING_REVIEW", "warnings": preview["warnings"]}


async def replace_preview(session: AsyncSession, body: dict[str, Any]) -> dict[str, Any]:
    records, mappings = await _records(session, body.get("knowledgeIds")), _mappings(body.get("databaseMappings"))
    affected = []
    for record in records:
        nodes = [{"nodeId": node_id, "nodeTitle": title, "sourceRef": ref, "targetRef": mappings[ref]} for ref, node_id, title in _refs(record.draft_definition) if ref in mappings and mappings[ref] != ref]
        if nodes:
            affected.append({"knowledgeId": record.id, "name": record.name, "currentStatus": record.status, "nextStatus": "PENDING_REVIEW" if record.status == "PUBLISHED" else record.status, "nodes": nodes})
    if not affected:
        raise ValueError("所选经验没有匹配到需要替换的数据库路径")
    return {"selectedCount": len(records), "affectedCount": len(affected), "items": affected, "confirmationRequired": True}


async def replace_paths(session: AsyncSession, body: dict[str, Any], operator_uid: str) -> dict[str, Any]:
    preview = await replace_preview(session, body)
    if body.get("confirmed") is not True:
        return preview
    mappings, updated = _mappings(body.get("databaseMappings")), []
    records = await _records(session, body.get("knowledgeIds"))
    affected_ids = {item["knowledgeId"] for item in preview["items"]}
    for record in records:
        if record.id not in affected_ids:
            continue
        record.draft_definition = _map_definition(record.draft_definition, mappings)
        if record.status == "PUBLISHED":
            record.status, record.submitted_at, record.submitted_by = "PENDING_REVIEW", datetime.now(UTC), operator_uid
        record.retrieval_text, record.embedding, record.embedding_model = "", None, None
        add_lifecycle(session, record, "PATH_REPLACED", operator_uid, "批量替换数据库路径")
        updated.append(record.id)
    session.add(KnowledgeTransferAudit(id=_id("kta_"), action="PATH_REPLACE", operator_uid=operator_uid, details={"knowledgeIds": updated, "databaseMappings": body.get("databaseMappings") or []}))
    await session.commit()
    return {"updatedKnowledgeIds": updated, "confirmationRequired": False}
