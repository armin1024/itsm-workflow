from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from app.config import settings


def _now() -> datetime:
    return datetime.now(UTC)


class StudioStore:
    def __init__(self, path: Path | None = None):
        self.path = path or settings.studio_database_path

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as database:
            await database.executescript("""
                CREATE TABLE IF NOT EXISTS studio_workspaces (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, created_by TEXT NOT NULL,
                    draft_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL, test_only INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS studio_node_debug_runs (
                    id TEXT PRIMARY KEY, workspace_id TEXT, node_json TEXT NOT NULL,
                    inputs_json TEXT NOT NULL, mode TEXT NOT NULL, handler_version TEXT NOT NULL,
                    status TEXT NOT NULL, output_json TEXT, diagnostic_json TEXT,
                    interrupt_json TEXT, response_json TEXT, created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                    test_only INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS studio_node_settings (
                    node_type TEXT NOT NULL, schema_version INTEGER NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1, name TEXT NOT NULL,
                    description TEXT NOT NULL, debug_config_json TEXT NOT NULL,
                    debug_inputs_json TEXT NOT NULL, debug_fixture_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (node_type, schema_version)
                );
                CREATE INDEX IF NOT EXISTS ix_studio_debug_expires ON studio_node_debug_runs(expires_at);
                CREATE INDEX IF NOT EXISTS ix_studio_workspace_expires ON studio_workspaces(expires_at);
            """)
            await database.commit()

    async def node_settings(self) -> dict[tuple[str, int], dict[str, Any]]:
        async with aiosqlite.connect(self.path) as database:
            database.row_factory = aiosqlite.Row
            result = await database.execute("SELECT * FROM studio_node_settings")
            rows = await result.fetchall()
        return {
            (row["node_type"], int(row["schema_version"])): {
                "enabled": bool(row["enabled"]),
                "name": row["name"],
                "description": row["description"],
                "debugConfig": json.loads(row["debug_config_json"]),
                "debugInputs": json.loads(row["debug_inputs_json"]),
                "debugFixture": json.loads(row["debug_fixture_json"]),
                "updatedAt": row["updated_at"],
            }
            for row in rows
        }

    async def update_node_setting(self, node_type: str, schema_version: int, value: dict[str, Any]) -> dict[str, Any]:
        self._check_capacity(value)
        now = _now().isoformat()
        async with aiosqlite.connect(self.path) as database:
            await database.execute(
                """INSERT INTO studio_node_settings
                (node_type,schema_version,enabled,name,description,debug_config_json,debug_inputs_json,debug_fixture_json,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(node_type,schema_version) DO UPDATE SET
                enabled=excluded.enabled,name=excluded.name,description=excluded.description,
                debug_config_json=excluded.debug_config_json,debug_inputs_json=excluded.debug_inputs_json,
                debug_fixture_json=excluded.debug_fixture_json,updated_at=excluded.updated_at""",
                (node_type, schema_version, int(bool(value["enabled"])), value["name"], value["description"], json.dumps(value["debugConfig"], ensure_ascii=False), json.dumps(value["debugInputs"], ensure_ascii=False), json.dumps(value["debugFixture"], ensure_ascii=False), now),
            )
            await database.commit()
        return {"type": node_type, "schemaVersion": schema_version, **value, "updatedAt": now}

    def _check_capacity(self, value: Any) -> None:
        size = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if size > settings.studio_artifact_max_bytes:
            raise ValueError(f"Studio临时数据超过限制 {settings.studio_artifact_max_bytes} bytes")
        if self.path.exists() and self.path.stat().st_size > settings.studio_database_max_bytes:
            raise ValueError("Studio SQLite已达到容量上限，请等待清理或删除临时workspace")

    async def cleanup(self) -> int:
        now = _now().isoformat()
        async with aiosqlite.connect(self.path) as database:
            first = await database.execute("DELETE FROM studio_node_debug_runs WHERE expires_at <= ?", (now,))
            second = await database.execute("DELETE FROM studio_workspaces WHERE expires_at <= ?", (now,))
            await database.commit()
            return int(first.rowcount or 0) + int(second.rowcount or 0)

    async def create_workspace(self, name: str, creator_uid: str, draft: dict[str, Any] | None = None) -> dict[str, Any]:
        self._check_capacity(draft or {})
        workspace_id = "test_ws_" + uuid.uuid4().hex
        now, expires = _now(), _now() + timedelta(hours=settings.studio_ttl_hours)
        async with aiosqlite.connect(self.path) as database:
            await database.execute("INSERT INTO studio_workspaces VALUES (?,?,?,?,?,?,?,1)", (workspace_id, name, creator_uid, json.dumps(draft or {}, ensure_ascii=False), now.isoformat(), now.isoformat(), expires.isoformat()))
            await database.commit()
        return {"workspaceId": workspace_id, "name": name, "createdBy": creator_uid, "draft": draft or {}, "createdAt": now.isoformat(), "updatedAt": now.isoformat(), "expiresAt": expires.isoformat(), "testOnly": True}

    async def list_workspaces(self, creator_uid: str) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as database:
            database.row_factory = aiosqlite.Row
            result = await database.execute("SELECT * FROM studio_workspaces WHERE created_by=? AND expires_at>? ORDER BY updated_at DESC", (creator_uid, _now().isoformat()))
            rows = await result.fetchall()
        return [{"workspaceId": row["id"], "name": row["name"], "createdBy": row["created_by"], "draft": json.loads(row["draft_json"]), "createdAt": row["created_at"], "updatedAt": row["updated_at"], "expiresAt": row["expires_at"], "testOnly": True} for row in rows]

    async def create_debug_run(self, *, creator_uid: str, workspace_id: str | None, node: dict[str, Any], inputs: dict[str, Any], mode: str, handler_version: str, status: str, output: dict[str, Any] | None, diagnostic: dict[str, Any] | None, interrupt: dict[str, Any] | None) -> dict[str, Any]:
        self._check_capacity({"node": node, "inputs": inputs, "output": output, "diagnostic": diagnostic, "interrupt": interrupt})
        debug_id = "test_debug_" + uuid.uuid4().hex
        now, expires = _now(), _now() + timedelta(hours=settings.studio_ttl_hours)
        values = (debug_id, workspace_id, json.dumps(node, ensure_ascii=False), json.dumps(inputs, ensure_ascii=False), mode, handler_version, status, json.dumps(output, ensure_ascii=False) if output is not None else None, json.dumps(diagnostic, ensure_ascii=False) if diagnostic else None, json.dumps(interrupt, ensure_ascii=False) if interrupt else None, None, creator_uid, now.isoformat(), now.isoformat(), expires.isoformat())
        async with aiosqlite.connect(self.path) as database:
            await database.execute("INSERT INTO studio_node_debug_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)", values)
            await database.commit()
        return {"debugRunId": debug_id, "workspaceId": workspace_id, "node": node, "inputs": inputs, "mode": mode, "handlerVersion": handler_version, "status": status, "output": output, "diagnostic": diagnostic, "interrupt": interrupt, "createdBy": creator_uid, "createdAt": now.isoformat(), "updatedAt": now.isoformat(), "expiresAt": expires.isoformat(), "testOnly": True}

    async def get_debug_run(self, debug_id: str, creator_uid: str, is_admin: bool) -> dict[str, Any]:
        async with aiosqlite.connect(self.path) as database:
            database.row_factory = aiosqlite.Row
            result = await database.execute("SELECT * FROM studio_node_debug_runs WHERE id=? AND expires_at>?", (debug_id, _now().isoformat()))
            row = await result.fetchone()
        if not row or not is_admin and row["created_by"] != creator_uid:
            raise KeyError(debug_id)
        return {"debugRunId": row["id"], "workspaceId": row["workspace_id"], "node": json.loads(row["node_json"]), "inputs": json.loads(row["inputs_json"]), "mode": row["mode"], "handlerVersion": row["handler_version"], "status": row["status"], "output": json.loads(row["output_json"]) if row["output_json"] else None, "diagnostic": json.loads(row["diagnostic_json"]) if row["diagnostic_json"] else None, "interrupt": json.loads(row["interrupt_json"]) if row["interrupt_json"] else None, "response": json.loads(row["response_json"]) if row["response_json"] else None, "createdBy": row["created_by"], "createdAt": row["created_at"], "updatedAt": row["updated_at"], "expiresAt": row["expires_at"], "testOnly": True}

    async def resolve_interrupt(self, debug_id: str, creator_uid: str, response: dict[str, Any]) -> dict[str, Any]:
        current = await self.get_debug_run(debug_id, creator_uid, False)
        if current["status"] != "WAITING_INPUT" or not current["interrupt"]:
            raise ValueError("调试运行当前没有可回复的HITL")
        action = str(response.get("action") or "")
        candidates = current["interrupt"].get("candidates") or []
        if action == "SELECT":
            selected = next((item for item in candidates if item.get("id") == response.get("candidateId")), None)
            if not selected:
                raise ValueError("candidateId不在候选集合中")
            output, status = {"selected": selected}, "SUCCEEDED"
        elif action == "MANUAL_VALUE":
            output, status = {"selected": {"id": "manual", "value": response.get("value"), "reason": response.get("reason", "")}}, "SUCCEEDED"
        elif action == "CANCEL":
            output, status = None, "CANCELLED"
        else:
            raise ValueError("Studio单节点HITL回复只允许SELECT、MANUAL_VALUE或CANCEL")
        now = _now().isoformat()
        async with aiosqlite.connect(self.path) as database:
            await database.execute("UPDATE studio_node_debug_runs SET status=?, output_json=?, response_json=?, interrupt_json=NULL, updated_at=? WHERE id=?", (status, json.dumps(output, ensure_ascii=False) if output is not None else None, json.dumps(response, ensure_ascii=False), now, debug_id))
            await database.commit()
        return await self.get_debug_run(debug_id, creator_uid, False)


studio_store = StudioStore()
