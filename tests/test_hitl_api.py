import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.auth import Principal, current_principal
from app.crypto import SecretBox, sha256_bytes
from app.db import Base, get_session
from app.main import app
from app.models import EncryptedArtifact, InterruptRecord, WorkflowRun


def encrypted_artifact(artifact_id: str, run_id: str, node_id: str, payload: dict, kind: str) -> EncryptedArtifact:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    return EncryptedArtifact(id=artifact_id, run_id=run_id, node_id=node_id, artifact_type=kind, ciphertext=SecretBox().seal(payload, purpose="artifact:" + artifact_id), content_hash=sha256_bytes(encoded), size_bytes=len(encoded), expires_at=datetime.now(UTC) + timedelta(days=30))


@pytest.mark.asyncio
async def test_hitl_options_hide_values_and_reply_is_atomic(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'hitl-api.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    run_id, interrupt_id, artifact_id = "run_select", "int_select", "art_options"
    node = {"id": "choose", "type": "hitl_select", "title": "选择客户", "config": {"title": "选择客户", "selectionMode": "SINGLE", "minimumSelections": 1, "maximumSelections": 1}, "inputs": []}
    candidates = [{"candidateId": "candidate_1", "label": "王五 / C1", "display": {"name": "王五"}, "values": {"customer_id": "C1", "secret": "hidden"}}]
    async with sessions() as session:
        session.add(WorkflowRun(id=run_id, knowledge_id="knw", workflow_version_id="wfv", ticket_id=1, initiated_by="uid", status="WAITING_INPUT", plan_hash="0" * 64, workflow_snapshot={"nodes": [node], "edges": [], "entryNodeId": "choose"}, run_inputs={}, node_statuses={"choose": "WAITING"}, output_refs={}, current_node_id="choose"))
        session.add(encrypted_artifact(artifact_id, run_id, "choose", {"candidates": candidates}, "HITL_OPTIONS"))
        session.add(InterruptRecord(id=interrupt_id, run_id=run_id, node_id="choose", kind="HITL_SELECT", status="OPEN", request_payload={"title": "选择客户", "selectionMode": "SINGLE", "displayFields": [{"name": "name", "label": "姓名"}], "minimumSelections": 1, "maximumSelections": 1}, option_artifact_id=artifact_id, option_count=1))
        await session.commit()

    async def session_override() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session
    async def principal_override() -> Principal:
        return Principal("uid", "key", False, True)
    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[current_principal] = principal_override
    with TestClient(app) as client:
        options = client.get(f"/api/v1/runs/{run_id}/interrupts/{interrupt_id}/options")
        assert options.status_code == 200 and options.json()["items"] == [{"candidateId": "candidate_1", "label": "王五 / C1", "display": {"name": "王五"}}]
        assert "customer_id" not in options.text and "hidden" not in options.text
        reply = client.post(f"/api/v1/runs/{run_id}/interrupts/{interrupt_id}/resume", json={"payload": {"action": "SELECT", "candidateIds": ["candidate_1"]}}, headers={"Idempotency-Key": "hitl-select-1"})
        assert reply.status_code == 200 and reply.json()["status"] == "QUEUED"
        duplicate = client.post(f"/api/v1/runs/{run_id}/interrupts/{interrupt_id}/resume", json={"payload": {"action": "SELECT", "candidateIds": ["candidate_1"]}}, headers={"Idempotency-Key": "hitl-select-1"})
        assert duplicate.status_code == 200
        conflict = client.post(f"/api/v1/runs/{run_id}/interrupts/{interrupt_id}/resume", json={"payload": {"action": "SELECT", "candidateIds": ["candidate_1"]}}, headers={"Idempotency-Key": "hitl-select-2"})
        assert conflict.status_code == 409
    async with sessions() as session:
        run, record = await session.get(WorkflowRun, run_id), await session.get(InterruptRecord, interrupt_id)
        assert run.resume_payload == {"interactionId": interrupt_id, "action": "SELECT", "candidateIds": ["candidate_1"]}
        assert record.status == "RESUME_PENDING" and record.response_payload == {"action": "SELECT", "candidateIds": ["candidate_1"]}
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.mark.asyncio
async def test_hitl_form_values_are_only_in_encrypted_artifact(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'hitl-form.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    run_id, interrupt_id = "run_form", "int_form"
    fields = [{"name": "customer_id", "label": "客户编号", "type": "string", "required": True}]
    node = {"id": "form", "type": "hitl_form", "title": "输入", "config": {"title": "填写参数", "fields": fields}, "inputs": []}
    async with sessions() as session:
        session.add(WorkflowRun(id=run_id, knowledge_id="knw", workflow_version_id="wfv", ticket_id=1, initiated_by="uid", status="WAITING_INPUT", plan_hash="0" * 64, workflow_snapshot={"nodes": [node], "edges": [], "entryNodeId": "form"}, run_inputs={}, node_statuses={"form": "WAITING"}, output_refs={}, current_node_id="form"))
        session.add(InterruptRecord(id=interrupt_id, run_id=run_id, node_id="form", kind="HITL_FORM", status="OPEN", request_payload={"title": "填写参数", "fields": fields}, option_count=0))
        await session.commit()
    async def session_override() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session
    async def principal_override() -> Principal:
        return Principal("uid", "key", False, True)
    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[current_principal] = principal_override
    with TestClient(app) as client:
        invalid = client.post(f"/api/v1/runs/{run_id}/interrupts/{interrupt_id}/resume", json={"payload": {"values": {"customer_id": "C-SECRET"}}}, headers={"Idempotency-Key": "hitl-form-reusable"})
        assert invalid.status_code == 422 and "不匹配" in invalid.json()["detail"]
        response = client.post(f"/api/v1/runs/{run_id}/interrupts/{interrupt_id}/resume", json={"payload": {"action": "SUBMIT", "values": {"customer_id": "C-SECRET"}}}, headers={"Idempotency-Key": "hitl-form-reusable"})
        assert response.status_code == 200
    async with sessions() as session:
        run, record = await session.get(WorkflowRun, run_id), await session.get(InterruptRecord, interrupt_id)
        artifact = await session.get(EncryptedArtifact, record.response_artifact_id)
        assert "C-SECRET" not in json.dumps(run.resume_payload) and "C-SECRET" not in json.dumps(record.response_payload)
        assert b"C-SECRET" not in artifact.ciphertext
        assert SecretBox().open(artifact.ciphertext, purpose="artifact:" + artifact.id)["values"]["customer_id"] == "C-SECRET"
    app.dependency_overrides.clear()
    await engine.dispose()
