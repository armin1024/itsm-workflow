from __future__ import annotations

import base64
import os
import re
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"
    database_url: str = "sqlite+aiosqlite:///./data/itsm-workflow.db"
    langgraph_database_url: str = ""
    aops_base_url: str = ""
    aops_ca_bundle: str | None = None
    aops_cli_path: Path = Path("/usr/local/bin/aops-cli")
    aops_cli_version_requirement: str = ""
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = "BAAI/bge-m3"
    rerank_base_url: str = ""
    rerank_api_key: str = ""
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_path: str = "/rerank"
    rerank_api_format: str = "auto"
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_seconds: int = Field(default=30, ge=5, le=180)
    llm_path: str = "/chat/completions"
    llm_response_format: str = "auto"
    workflow_api_token: str = "change-me"
    workflow_review_token: str = ""
    workflow_review_actor_uid: str = "workflow-review-service"
    runtime_service_token: str = ""
    runtime_internal_enabled: bool = False
    studio_enabled: bool = False
    studio_database_path: Path = Path("data/studio.db")
    studio_ttl_hours: int = Field(default=24, ge=1, le=72)
    studio_artifact_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=50 * 1024 * 1024)
    studio_database_max_bytes: int = Field(default=5 * 1024 * 1024 * 1024, ge=10 * 1024 * 1024)
    tec01_enabled: bool = False
    tec01_base_url: str = ""
    tec01_service_token: str = ""
    tec01_timeout_seconds: int = Field(default=20, ge=1, le=180)
    tec01_claim_wait_seconds: int = Field(default=15, ge=1, le=20)
    workflow_public_url: str = "http://127.0.0.1:8089"
    knowledge_environment_name: str = ""
    mcp_internal_api_url: str = "http://127.0.0.1:8089/api/v1"
    mcp_bind_host: str = "127.0.0.1"
    mcp_port: int = Field(default=8090, ge=1, le=65535)
    mcp_path: str = "/mcp"
    mcp_allowed_hosts: str = "localhost:*,127.0.0.1:*"
    mcp_wait_max_seconds: int = Field(default=15, ge=1, le=30)
    workflow_master_key: str = Field(default="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    workflow_admin_uids: str = ""
    workflow_operator_uids: str = ""
    worker_concurrency: int = Field(default=2, ge=1, le=32)
    worker_lease_seconds: int = Field(default=30, ge=10, le=300)
    node_timeout_seconds: int = Field(default=600, ge=1, le=7200)
    credential_ttl_hours: int = Field(default=8, ge=1, le=168)
    result_retention_days: int = Field(default=30, ge=1, le=365)
    session_cookie_secure: bool = False
    workflow_base_path: str = ""
    static_dir: Path = Path("frontend/dist")

    @field_validator("aops_base_url")
    @classmethod
    def strip_url(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("workflow_base_path")
    @classmethod
    def normalize_base_path(cls, value: str) -> str:
        value = value.strip()
        if value in {"", "/"}:
            return ""
        value = "/" + value.strip("/")
        if not re.fullmatch(r"(?:/[A-Za-z0-9._~-]+)+", value):
            raise ValueError("WORKFLOW_BASE_PATH 只能包含安全的URL路径段")
        return value

    @property
    def admin_uids(self) -> set[str]:
        return {item.strip() for item in self.workflow_admin_uids.split(",") if item.strip()}

    @property
    def operator_uids(self) -> set[str]:
        return {item.strip() for item in self.workflow_operator_uids.split(",") if item.strip()} | self.admin_uids

    @property
    def allowed_mcp_hosts(self) -> list[str]:
        return [item.strip() for item in self.mcp_allowed_hosts.split(",") if item.strip()]

    def encryption_key(self) -> bytes:
        try:
            value = base64.urlsafe_b64decode(self.workflow_master_key.encode())
        except Exception as exc:
            raise ValueError("WORKFLOW_MASTER_KEY 必须是 Base64") from exc
        if len(value) != 32:
            raise ValueError("WORKFLOW_MASTER_KEY 解码后必须为 32 字节")
        return value

    def validate_production(self) -> None:
        if self.environment.lower() == "production":
            if self.database_url.startswith("sqlite"):
                raise RuntimeError("生产环境必须使用 PostgreSQL")
            if not self.langgraph_database_url.startswith("postgres"):
                raise RuntimeError("生产环境必须配置 LANGGRAPH_DATABASE_URL")
            if self.workflow_api_token in {"", "change-me"}:
                raise RuntimeError("生产环境必须配置 WORKFLOW_API_TOKEN")
            if not self.admin_uids or not self.operator_uids:
                raise RuntimeError("生产环境必须配置管理员和操作员 UID allowlist")
            if not self.embedding_base_url or not self.rerank_base_url:
                raise RuntimeError("生产环境必须配置 BGE-M3 embedding 和 rerank 服务")
            if self.runtime_internal_enabled and not self.runtime_service_token:
                raise RuntimeError("启用Runtime内部接口时必须配置RUNTIME_SERVICE_TOKEN")
            if self.studio_enabled and not self.studio_database_path.is_absolute():
                raise RuntimeError("生产环境启用Studio时STUDIO_DATABASE_PATH必须是绝对路径")
            if self.tec01_enabled and (not self.tec01_base_url or not self.tec01_service_token):
                raise RuntimeError("启用tec01时必须配置TEC01_BASE_URL和TEC01_SERVICE_TOKEN")
            self.encryption_key()


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
