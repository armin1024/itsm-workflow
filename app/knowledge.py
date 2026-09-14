from __future__ import annotations

import math
import re
import uuid
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crypto import canonical_hash
from app.models import Knowledge, KnowledgeUser, WorkflowVersion
from app.schemas import KnowledgeCreate, KnowledgeUpdate
from app import retrieval
from app.workflow import WorkflowDefinition, legacy_steps_to_workflow


def _id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex


def _chars(value: str) -> set[str]:
    return {char for char in value.lower() if "\u4e00" <= char <= "\u9fff" or char.isalnum() or char == "_"}


def serialize_knowledge(record: Knowledge, uids: list[str] | None = None) -> dict[str, Any]:
    return {
        "knowledgeId": record.id,
        "status": record.status,
        "name": record.name,
        "summary": record.summary,
        "matchPhrases": record.match_phrases,
        "negativePhrases": record.negative_phrases,
        "systemKeys": record.system_keys,
        "creatorUid": record.creator_uid,
        "sourceType": record.source_type,
        "sourceTicketId": record.source_ticket_id,
        "sourceTicketNo": record.source_ticket_no,
        "uids": uids if uids is not None else [item.uid for item in record.users],
        "visibility": "PUBLIC" if record.public else "RESTRICTED",
        "workflowDefinition": record.draft_definition,
        "publishedVersionId": record.published_version_id,
        "createdAt": record.created_at.isoformat(),
        "updatedAt": record.updated_at.isoformat(),
        "submittedAt": record.submitted_at.isoformat() if record.submitted_at else None,
        "submittedBy": record.submitted_by,
        "reviewedAt": record.reviewed_at.isoformat() if record.reviewed_at else None,
        "reviewedBy": record.reviewed_by,
        "reviewNote": record.review_note,
        "deletedAt": record.deleted_at.isoformat() if record.deleted_at else None,
        "deletedBy": record.deleted_by,
    }


async def create_knowledge(session: AsyncSession, body: KnowledgeCreate, creator_uid: str) -> Knowledge:
    definition = body.workflowDefinition.model_dump(mode="json")
    record = Knowledge(
        id=_id("knw_"), status="DRAFT", name=body.name.strip(), summary=body.summary.strip(),
        match_phrases=[item.strip() for item in body.matchPhrases if item.strip()],
        negative_phrases=[item.strip() for item in body.negativePhrases if item.strip()],
        system_keys=[item.strip() for item in body.systemKeys if item.strip()],
        creator_uid=creator_uid, public=not body.uids, draft_definition=definition,
        source_type="MANUAL",
    )
    session.add(record)
    await session.flush()
    for uid in dict.fromkeys(item.strip() for item in body.uids if item.strip()):
        session.add(KnowledgeUser(knowledge_id=record.id, uid=uid))
    await session.commit()
    return record


async def update_knowledge(session: AsyncSession, record: Knowledge, body: KnowledgeUpdate) -> Knowledge:
    record.name = body.name.strip()
    record.summary = body.summary.strip()
    record.match_phrases = [item.strip() for item in body.matchPhrases if item.strip()]
    record.negative_phrases = [item.strip() for item in body.negativePhrases if item.strip()]
    record.system_keys = [item.strip() for item in body.systemKeys if item.strip()]
    record.public = not body.uids
    record.draft_definition = body.workflowDefinition.model_dump(mode="json")
    record.status = "DRAFT"
    record.submitted_at = None
    record.submitted_by = None
    record.reviewed_at = None
    record.reviewed_by = None
    record.review_note = None
    record.retrieval_text = ""
    record.embedding = None
    record.embedding_model = None
    await session.execute(delete(KnowledgeUser).where(KnowledgeUser.knowledge_id == record.id))
    for uid in dict.fromkeys(item.strip() for item in body.uids if item.strip()):
        session.add(KnowledgeUser(knowledge_id=record.id, uid=uid))
    await session.commit()
    await session.refresh(record, attribute_names=["users"])
    return record


async def submit_knowledge_review(session: AsyncSession, record: Knowledge, uid: str) -> Knowledge:
    from datetime import UTC, datetime

    WorkflowDefinition.model_validate(record.draft_definition)
    if record.status != "DRAFT":
        raise ValueError("只有草稿可以提交审核")
    record.status = "PENDING_REVIEW"
    record.submitted_at = datetime.now(UTC)
    record.submitted_by = uid
    record.reviewed_at = None
    record.reviewed_by = None
    record.review_note = None
    await session.commit()
    return record


async def delete_knowledge(session: AsyncSession, record: Knowledge, uid: str) -> Knowledge:
    from datetime import UTC, datetime

    if record.status == "DELETED":
        raise ValueError("经验已经删除")
    record.status = "DELETED"
    record.deleted_at = datetime.now(UTC)
    record.deleted_by = uid
    record.retrieval_text = ""
    record.embedding = None
    record.embedding_model = None
    await session.commit()
    return record


async def publish_knowledge(session: AsyncSession, knowledge_id: str, uid: str) -> WorkflowVersion:
    record = await session.get(Knowledge, knowledge_id)
    if not record:
        raise KeyError(knowledge_id)
    if record.status != "PENDING_REVIEW":
        raise ValueError("经验必须先提交审核才能发布")
    definition = WorkflowDefinition.model_validate(record.draft_definition).model_dump(mode="json")
    count = await session.scalar(select(func.count()).select_from(WorkflowVersion).where(WorkflowVersion.knowledge_id == knowledge_id))
    version = WorkflowVersion(id=_id("wfv_"), knowledge_id=knowledge_id, version_number=int(count or 0) + 1, content_hash=canonical_hash(definition), definition=definition, published_by=uid)
    session.add(version)
    await session.flush()
    record.status = "PUBLISHED"
    from datetime import UTC, datetime
    record.reviewed_at = datetime.now(UTC)
    record.reviewed_by = uid
    record.review_note = "审核通过并发布"
    record.published_version_id = version.id
    record.retrieval_text = retrieval.retrieval_text(record)
    if retrieval.settings.embedding_base_url:
        record.embedding = (await retrieval.embed([record.retrieval_text]))[0]
        record.embedding_model = retrieval.settings.embedding_model
    await session.commit()
    return version


async def reject_knowledge_review(session: AsyncSession, record: Knowledge, uid: str, reason: str) -> Knowledge:
    from datetime import UTC, datetime

    if record.status != "PENDING_REVIEW":
        raise ValueError("只有待审核经验可以退回")
    record.status = "DRAFT"
    record.reviewed_at = datetime.now(UTC)
    record.reviewed_by = uid
    record.review_note = reason.strip()[:2000]
    await session.commit()
    return record


async def authorized_knowledge(session: AsyncSession, knowledge_id: str, uid: str, *, published_only: bool = True, bypass_visibility: bool = False, allow_owned_draft: bool = False) -> Knowledge:
    record = await session.get(Knowledge, knowledge_id)
    owns_draft = bool(record and allow_owned_draft and record.creator_uid == uid and record.status in {"DRAFT", "PENDING_REVIEW"})
    if not record or record.status == "DELETED" or published_only and record.status != "PUBLISHED" and not owns_draft:
        raise KeyError(knowledge_id)
    if not bypass_visibility and not owns_draft and not record.public:
        allowed = await session.scalar(select(func.count()).select_from(KnowledgeUser).where(KnowledgeUser.knowledge_id == knowledge_id, KnowledgeUser.uid == uid))
        if not allowed:
            raise KeyError(knowledge_id)
    return record


async def match_knowledge(session: AsyncSession, uid: str, query: str, target_systems: list[str], limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    result = await session.execute(select(Knowledge).where(Knowledge.status == "PUBLISHED").order_by(Knowledge.updated_at.desc()))
    candidates: list[dict[str, Any]] = []
    query_chars = _chars(query)
    query_vector = None
    embedding_error = None
    if retrieval.settings.embedding_base_url:
        try:
            query_vector = (await retrieval.embed([query]))[0]
        except retrieval.RetrievalError as exc:
            embedding_error = str(exc)
    for record in result.scalars():
        if any(phrase and phrase in query for phrase in record.negative_phrases):
            continue
        if not record.public:
            allowed = await session.scalar(select(func.count()).select_from(KnowledgeUser).where(KnowledgeUser.knowledge_id == record.id, KnowledgeUser.uid == uid))
            if not allowed:
                continue
        document = record.retrieval_text or retrieval.retrieval_text(record)
        chars = _chars(document)
        lexical = len(query_chars & chars) / max(1, len(query_chars | chars))
        phrase = max((1.0 if item in query else 0.0 for item in record.match_phrases), default=0.0)
        system = 0.15 if target_systems and set(target_systems) & set(record.system_keys) else 0.0
        vector = retrieval.cosine(query_vector, record.embedding) if query_vector and record.embedding else None
        fused = lexical * 0.42 + (vector or 0.0) * 0.43 + phrase * 0.15 + system
        if fused > 0:
            candidates.append({"record": record, "document": document, "lexical": lexical, "vector": vector, "fused": fused, "system": system > 0})
    candidates.sort(key=lambda item: item["fused"], reverse=True)
    rerank_error = None
    if candidates and retrieval.settings.rerank_base_url:
        try:
            scores = await retrieval.rerank(query, [item["document"] for item in candidates[:20]], min(20, len(candidates)))
            for index, score in scores:
                candidates[index]["rerank"] = score
            candidates.sort(key=lambda item: item.get("rerank", -1.0), reverse=True)
        except retrieval.RetrievalError as exc:
            rerank_error = str(exc)
    items = []
    for item in candidates[:limit]:
        record = item["record"]
        reasons = [reason for reason, hit in (("关键词重合", item["lexical"] > 0), ("语义相似", item["vector"] is not None), ("系统一致", item["system"])) if hit]
        items.append({"knowledgeId": record.id, "name": record.name, "summary": record.summary, "systemKeys": record.system_keys, "score": round(item.get("rerank", item["fused"]), 4), "lexicalScore": round(item["lexical"], 4), "vectorScore": round(item["vector"], 4) if item["vector"] is not None else None, "rerankScore": round(item["rerank"], 4) if "rerank" in item else None, "matchReasons": reasons})
    diagnostics = {"retrievalMode": "hybrid" if query_vector else "lexical", "embeddingUnavailable": query_vector is None, "embeddingError": embedding_error, "rerankUnavailable": not bool(retrieval.settings.rerank_base_url) or rerank_error is not None, "rerankError": rerank_error}
    return items, diagnostics


async def import_legacy_package(session: AsyncSession, package: dict[str, Any], creator_uid: str) -> list[str]:
    if package.get("schema") != "aops-workflow-knowledge-export" or package.get("schemaVersion") != 1:
        raise ValueError("仅支持旧知识服务 schemaVersion=1 导出包")
    items = package.get("items")
    if not isinstance(items, list) or not items or len(items) > 500:
        raise ValueError("导入包 items 必须包含 1 至 500 条经验")
    prepared = []
    for index, item in enumerate(items):
        definition = item.get("definition") if isinstance(item, dict) else None
        if not isinstance(definition, dict):
            raise ValueError(f"items[{index}] 定义无效")
        workflow = legacy_steps_to_workflow(definition.get("steps") or [])
        prepared.append((item, definition, workflow))
    ids = []
    for item, definition, workflow in prepared:
        knowledge_id = _id("knw_")
        uids = [str(value).strip() for value in definition.get("uids", []) if str(value).strip()]
        record = Knowledge(
            id=knowledge_id, status="DRAFT", name=str(definition.get("name") or "导入经验")[:200],
            summary=str(definition.get("summary") or "")[:2000], match_phrases=definition.get("matchPhrases") or [str(definition.get("name") or "导入经验")],
            negative_phrases=definition.get("negativePhrases") or [], system_keys=definition.get("systemKeys") or [],
            creator_uid=str(definition.get("creatorUid") or creator_uid)[:120], public=not uids,
            draft_definition=workflow.model_dump(mode="json"),
        )
        session.add(record)
        for allowed_uid in dict.fromkeys(uids):
            session.add(KnowledgeUser(knowledge_id=knowledge_id, uid=allowed_uid))
        ids.append(knowledge_id)
    await session.commit()
    return ids
