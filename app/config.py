from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration for the split Runtime/Compiler/Studio service."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"
    aops_base_url: str = ""
    aops_ca_bundle: str | None = None
    aops_cli_path: Path = Path("/usr/local/bin/aops-cli")
    aops_cli_version_requirement: str = ""

    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_seconds: int = Field(default=30, ge=5, le=180)
    llm_path: str = "/chat/completions"
    llm_response_format: str = "auto"

    runtime_service_token: str = ""
    studio_admin_token: str = "change-me"
    studio_session_hours: int = Field(default=8, ge=1, le=72)
    studio_cookie_secure: bool = False

    tec01_base_url: str = ""
    tec01_service_token: str = ""
    tec01_timeout_seconds: int = Field(default=20, ge=1, le=180)
    executor_id: str = "executor-01"
    executor_max_active_runs: int = Field(default=32, ge=1, le=128)
    executor_max_active_cli: int = Field(default=8, ge=1, le=64)

    node_timeout_seconds: int = Field(default=600, ge=1, le=7200)
    workflow_base_path: str = ""
    static_dir: Path = Path("frontend/dist")

    @field_validator("aops_base_url", "llm_base_url", "tec01_base_url")
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

    def validate_service(self) -> None:
        if self.environment.lower() == "production" and (self.studio_admin_token in {"", "change-me"} or self.studio_admin_token.startswith("replace-")):
            raise RuntimeError("生产环境必须配置STUDIO_ADMIN_TOKEN")
        if self.environment.lower() == "production" and self.tec01_base_url and (not self.tec01_service_token or not self.runtime_service_token):
            raise RuntimeError("连接tec01时必须配置TEC01_SERVICE_TOKEN和RUNTIME_SERVICE_TOKEN")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
