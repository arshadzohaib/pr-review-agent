"""Application settings loaded from environment / `.env` via pydantic-settings.

Security-relevant limits (retries, sandbox resources) are hard-capped here so that a
misconfigured `.env` can never weaken the guardrails described in ARCHITECTURE.md.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Final, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---- Hard caps (not configurable upward) -------------------------------------------
HARD_MAX_RETRIES: Final[int] = 2
HARD_MAX_SANDBOX_TIMEOUT_S: Final[int] = 600
HARD_MAX_SANDBOX_MEM_BYTES: Final[int] = 2 * 1024**3  # 2 GiB
HARD_MAX_SANDBOX_NANO_CPUS: Final[int] = 2_000_000_000  # 2 CPUs
HARD_MAX_SANDBOX_PIDS: Final[int] = 512
HARD_MAX_SANDBOX_LOG_BYTES: Final[int] = 1024 * 1024  # 1 MiB
MIN_SANDBOX_MEM_BYTES: Final[int] = 64 * 1024**2  # 64 MiB

PLACEHOLDER_MARKER: Final[str] = "replace_me"

_MEM_RE = re.compile(r"^\s*(\d+)\s*([bkmg]?)\s*$", re.IGNORECASE)
_MEM_UNITS: Final[dict[str, int]] = {"": 1, "b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}


def parse_memory(value: str) -> int:
    """Parse a Docker-style memory string (e.g. ``512m``, ``1g``) into bytes."""
    match = _MEM_RE.match(value)
    if match is None:
        raise ValueError(f"invalid memory size {value!r}; expected e.g. '512m' or '1g'")
    number, unit = match.groups()
    return int(number) * _MEM_UNITS[unit.lower()]


class Settings(BaseSettings):
    """Typed, validated runtime configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    # --- App ---
    app_env: Literal["dev", "test", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_json: bool = True

    # --- GitHub ---
    github_token: SecretStr
    github_webhook_secret: SecretStr

    # --- Gemini ---
    gemini_api_key: SecretStr
    gemini_model: str = Field(default="gemini-2.5-flash", min_length=1)
    gemini_rpm_limit: int = Field(default=8, ge=1, le=10_000)
    gemini_tpm_limit: int = Field(default=200_000, ge=1_000)
    gemini_rpd_limit: int = Field(default=200, ge=1)
    quota_max_wait_s: int = Field(default=90, ge=0, le=900)

    # --- Redis ---
    redis_url: str = "redis://localhost:6379/0"
    review_cache_ttl_s: int = Field(default=7 * 24 * 3600, ge=60)

    # --- Agent budgets / guardrails ---
    prompt_version: int = Field(default=1, ge=1)
    max_context_tokens: int = Field(default=6000, ge=500, le=100_000)
    max_callers: int = Field(default=3, ge=0, le=20)
    max_llm_calls_per_job: int = Field(default=6, ge=1, le=20)
    max_retries: int = Field(default=HARD_MAX_RETRIES, ge=0, le=HARD_MAX_RETRIES)
    max_pr_changed_lines: int = Field(default=1500, ge=1)
    repro_min_confidence: float = Field(default=0.7, ge=0.0, le=1.0)

    # --- Sandbox ---
    sandbox_image: str = Field(default="pr-agent-sandbox-python:3.11", min_length=1)
    sandbox_timeout_s: int = Field(default=120, ge=5, le=HARD_MAX_SANDBOX_TIMEOUT_S)
    sandbox_mem_limit: str = "512m"
    sandbox_nano_cpus: int = Field(
        default=1_000_000_000, ge=100_000_000, le=HARD_MAX_SANDBOX_NANO_CPUS
    )
    sandbox_pids_limit: int = Field(default=128, ge=16, le=HARD_MAX_SANDBOX_PIDS)
    sandbox_log_max_bytes: int = Field(default=65_536, ge=1024, le=HARD_MAX_SANDBOX_LOG_BYTES)

    # ---- Validators ------------------------------------------------------------------
    @field_validator("github_token", "gemini_api_key")
    @classmethod
    def _reject_placeholder_secret(cls, v: SecretStr) -> SecretStr:
        raw = v.get_secret_value().strip()
        if not raw:
            raise ValueError("secret must not be empty")
        if PLACEHOLDER_MARKER in raw.lower():
            raise ValueError("secret still contains the placeholder value from .env.example")
        return v

    @field_validator("github_webhook_secret")
    @classmethod
    def _validate_webhook_secret(cls, v: SecretStr) -> SecretStr:
        raw = v.get_secret_value()
        if PLACEHOLDER_MARKER in raw.lower():
            raise ValueError("secret still contains the placeholder value from .env.example")
        if len(raw) < 16:
            raise ValueError("webhook secret must be at least 16 characters")
        return v

    @field_validator("redis_url")
    @classmethod
    def _validate_redis_url(cls, v: str) -> str:
        if not v.startswith(("redis://", "rediss://", "unix://")):
            raise ValueError("REDIS_URL must start with redis://, rediss:// or unix://")
        return v

    @field_validator("sandbox_mem_limit")
    @classmethod
    def _validate_mem_limit(cls, v: str) -> str:
        size = parse_memory(v)
        if size < MIN_SANDBOX_MEM_BYTES:
            raise ValueError("SANDBOX_MEM_LIMIT must be at least 64m")
        if size > HARD_MAX_SANDBOX_MEM_BYTES:
            raise ValueError("SANDBOX_MEM_LIMIT exceeds the 2g hard cap")
        return v.strip().lower()

    # ---- Derived values -------------------------------------------------------------
    @property
    def sandbox_mem_bytes(self) -> int:
        return parse_memory(self.sandbox_mem_limit)

    def secret_values(self) -> list[str]:
        """Raw secret values, used by the logging redaction filter."""
        return [
            s.get_secret_value()
            for s in (self.github_token, self.github_webhook_secret, self.gemini_api_key)
            if s.get_secret_value()
        ]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide cached settings (call ``get_settings.cache_clear()`` in tests)."""
    return Settings()  # required fields are populated from env / .env
