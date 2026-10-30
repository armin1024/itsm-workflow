from __future__ import annotations

import re
from string import Formatter
from urllib.parse import urlsplit, urlunsplit
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
    tec01_run_callback_path: str = "/internal/v1/runs/{runId}/callbacks"
    tec01_extraction_callback_path: str = "/internal/v1/extractions/{knowledgeId}/callbacks"
    tec01_timeout_seconds: int = Field(default=20, ge=1, le=180)
    tec01_callback_max_attempts: int = Field(default=6, ge=1, le=10)
    tec01_callback_wait_seconds: int = Field(default=70, ge=1, le=300)
    executor_id: str = "executor-01"
    executor_max_active_runs: int = Field(default=8, ge=1, le=128)
    executor_max_active_cli: int = Field(default=2, ge=1, le=64)
    compiler_max_active_jobs: int = Field(default=1, ge=1, le=32)
    cpu_max_workers: int = Field(default=1, ge=1, le=8)
    request_max_body_mb: int = Field(default=64, ge=1, le=256)
    compiler_evidence_max_mb: int = Field(default=8, ge=1, le=64)
    compiler_max_audit_operations: int = Field(default=5000, ge=1, le=50000)
    compiler_model_input_max_chars: int = Field(default=24000, ge=1000, le=1000000)
    sql_template_max_chars: int = Field(default=65536, ge=100, le=1048576)
    executor_run_memory_mb: int = Field(default=256, ge=1, le=4096)
    runtime_data_budget_mb: int = Field(default=2048, ge=1, le=65536)
    compiler_cache_max_mb: int = Field(default=64, ge=1, le=4096)
    tec01_http_max_connections: int = Field(default=16, ge=1, le=256)
    tec01_http_max_keepalive: int = Field(default=8, ge=0, le=256)

    node_timeout_seconds: int = Field(default=600, ge=1, le=7200)
    workflow_base_path: str = ""
    static_dir: Path = Path("frontend/dist")
    log_level: str = "INFO"
    log_dir: Path = Path("logs")
    log_retention_days: int = Field(default=7, ge=1, le=365)
    log_max_total_mb: int = Field(default=512, ge=1, le=10240)
    log_max_file_mb: int = Field(default=16, ge=1, le=512)
    log_cleanup_interval_seconds: int = Field(default=60, ge=1, le=3600)

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        value = value.upper()
        if value not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError("LOG_LEVEL仅支持DEBUG/INFO/WARNING/ERROR")
        return value

    @field_validator("tec01_run_callback_path", "tec01_extraction_callback_path")
    @classmethod
    def validate_callback_path(cls, value: str, info) -> str:
        required = "runId" if info.field_name == "tec01_run_callback_path" else "knowledgeId"
        if not value.startswith("/") or "//" in value or any(c in value for c in "?#\\") or any(ord(c) <= 32 or ord(c) == 127 for c in value):
            raise ValueError("回报路径必须为以 / 开头的相对路径，不能包含空白、查询参数或完整URL")
        if any(part in {".", ".."} for part in value.split("/")) or "%" in value:
            raise ValueError("回报路径不能包含路径跳转或预编码字符")
        try:
            fields = [(field, spec, conversion) for _, field, spec, conversion in Formatter().parse(value) if field is not None]
        except ValueError as exc:
            raise ValueError("回报路径占位符格式错误") from exc
        if fields != [(required, "", None)] or "{{" in value or "}}" in value:
            raise ValueError(f"回报路径必须且仅能包含一个 {{{required}}} 占位符")
        return value

    def studio_configuration(self) -> dict:
        # Explicit allowlist: never serialize Settings or include credentials.
        parsed = urlsplit(self.tec01_base_url)
        host = parsed.netloc.rsplit("@", 1)[-1]
        base_url = urlunsplit((parsed.scheme, host, parsed.path, "", ""))
        return {
            "readOnly": True, "storage": "browser-indexeddb", "serverDatabase": False,
            "executorId": self.executor_id,
            "performance": {
                "activeRuns": self.executor_max_active_runs, "activeCli": self.executor_max_active_cli,
                "compilerJobs": self.compiler_max_active_jobs, "cpuWorkers": self.cpu_max_workers,
                "requestMaxBytes": self.request_max_body_mb * 1024 * 1024,
                "compilerEvidenceMaxBytes": self.compiler_evidence_max_mb * 1024 * 1024,
                "runMemoryBytes": self.executor_run_memory_mb * 1024 * 1024,
                "dataBudgetBytes": self.runtime_data_budget_mb * 1024 * 1024,
                "compilerCacheBytes": self.compiler_cache_max_mb * 1024 * 1024,
                "modelInputMaxChars": self.compiler_model_input_max_chars,
                "sqlTemplateMaxChars": self.sql_template_max_chars,
                "httpConnections": self.tec01_http_max_connections,
                "httpKeepalive": self.tec01_http_max_keepalive,
                "memoryAccounting": "estimated-json-objects-not-rss", "cpuScratchBytesPerWorker": 512 * 1024 * 1024,
            },
            "logging": {"directory": str(self.log_dir.absolute()), "level": self.log_level, "retentionDays": self.log_retention_days, "maxTotalBytes": self.log_max_total_mb * 1024 * 1024, "maxFileBytes": min(self.log_max_file_mb, self.log_max_total_mb) * 1024 * 1024, "cleanupIntervalSeconds": self.log_cleanup_interval_seconds},
            "tec01": {
                "baseUrl": base_url, "serviceTokenConfigured": bool(self.tec01_service_token),
                "runCallbackPath": self.tec01_run_callback_path,
                "extractionCallbackPath": self.tec01_extraction_callback_path,
                "runCallbackUrl": base_url + self.tec01_run_callback_path if base_url else "",
                "extractionCallbackUrl": base_url + self.tec01_extraction_callback_path if base_url else "",
                "timeoutSeconds": min(self.tec01_timeout_seconds, 30),
                "callbackMaxAttempts": self.tec01_callback_max_attempts,
                "callbackWaitSeconds": self.tec01_callback_wait_seconds,
                "extractionMaxAttempts": 6,
            },
        }

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
