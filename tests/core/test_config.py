"""Unit tests for app.core.config."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import (
    HARD_MAX_RETRIES,
    Settings,
    get_settings,
    parse_memory,
)

MakeSettings = Callable[..., Settings]


# ---- parse_memory --------------------------------------------------------------------
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("512m", 512 * 1024**2),
        ("1g", 1024**3),
        ("1G", 1024**3),
        ("64k", 64 * 1024),
        ("1024", 1024),
        ("100b", 100),
        (" 256m ", 256 * 1024**2),
    ],
)
def test_parse_memory_valid(value: str, expected: int) -> None:
    assert parse_memory(value) == expected


@pytest.mark.parametrize("value", ["", "abc", "1.5g", "-1m", "10t", "m512"])
def test_parse_memory_invalid(value: str) -> None:
    with pytest.raises(ValueError, match="invalid memory size"):
        parse_memory(value)


# ---- defaults & loading --------------------------------------------------------------
def test_defaults_load_with_valid_secrets(make_settings: MakeSettings) -> None:
    s = make_settings()
    assert s.max_retries == HARD_MAX_RETRIES == 2
    assert s.gemini_model == "gemini-2.5-flash"
    assert s.sandbox_image == "pr-agent-sandbox-python:3.11"
    assert s.sandbox_mem_bytes == 512 * 1024**2
    assert s.redis_url == "redis://localhost:6379/0"
    assert s.max_llm_calls_per_job == 6


def test_missing_secrets_are_reported() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None)  # type: ignore[call-arg]
    missing = {err["loc"][0] for err in exc.value.errors() if err["type"] == "missing"}
    assert missing == {"github_token", "github_webhook_secret", "gemini_api_key"}


def test_env_vars_are_case_insensitive_and_coerced(
    monkeypatch: pytest.MonkeyPatch, make_settings: MakeSettings
) -> None:
    monkeypatch.setenv("max_retries", "1")
    monkeypatch.setenv("LOG_JSON", "false")
    s = make_settings()
    assert s.max_retries == 1
    assert s.log_json is False


def test_loads_from_dotenv_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "GITHUB_TOKEN=ghp_" + "x" * 36 + "\n"
        "GITHUB_WEBHOOK_SECRET=" + "y" * 20 + "\n"
        "GEMINI_API_KEY=AIza" + "z" * 35 + "\n"
        "SANDBOX_TIMEOUT_S=60\n",
        encoding="utf-8",
    )
    s = Settings(_env_file=env_file)  # type: ignore[call-arg]
    assert s.sandbox_timeout_s == 60
    assert s.github_token.get_secret_value() == "ghp_" + "x" * 36


def test_get_settings_is_cached(valid_env: dict[str, str]) -> None:
    first = get_settings()
    assert get_settings() is first
    get_settings.cache_clear()
    assert get_settings() is not first


def test_settings_are_immutable(make_settings: MakeSettings) -> None:
    s = make_settings()
    with pytest.raises(ValidationError):
        s.max_retries = 0  # type: ignore[misc]


# ---- secrets -------------------------------------------------------------------------
def test_secrets_never_leak_in_repr_or_dump(
    make_settings: MakeSettings, valid_env: dict[str, str]
) -> None:
    s = make_settings()
    rendered = " ".join([repr(s), str(s), s.model_dump_json(), str(s.model_dump())])
    for secret in valid_env.values():
        assert secret not in rendered
    assert "**********" in rendered


def test_secret_values_helper(make_settings: MakeSettings, valid_env: dict[str, str]) -> None:
    assert sorted(make_settings().secret_values()) == sorted(valid_env.values())


@pytest.mark.parametrize("field", ["github_token", "gemini_api_key", "github_webhook_secret"])
def test_placeholder_secrets_rejected(make_settings: MakeSettings, field: str) -> None:
    with pytest.raises(ValidationError, match="placeholder"):
        make_settings(**{field: "replace_me"})


@pytest.mark.parametrize("field", ["github_token", "gemini_api_key"])
def test_blank_secrets_rejected(make_settings: MakeSettings, field: str) -> None:
    with pytest.raises(ValidationError, match="must not be empty"):
        make_settings(**{field: "   "})


def test_short_webhook_secret_rejected(make_settings: MakeSettings) -> None:
    with pytest.raises(ValidationError, match="at least 16"):
        make_settings(github_webhook_secret="short")


# ---- guardrail hard caps ------------------------------------------------------------
@pytest.mark.parametrize("value", [0, 1, 2])
def test_max_retries_within_cap(make_settings: MakeSettings, value: int) -> None:
    assert make_settings(max_retries=value).max_retries == value


@pytest.mark.parametrize("value", [3, 10, -1])
def test_max_retries_cap_enforced(make_settings: MakeSettings, value: int) -> None:
    with pytest.raises(ValidationError):
        make_settings(max_retries=value)


def test_max_retries_cap_enforced_from_env(
    monkeypatch: pytest.MonkeyPatch, make_settings: MakeSettings
) -> None:
    monkeypatch.setenv("MAX_RETRIES", "3")
    with pytest.raises(ValidationError):
        make_settings()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sandbox_timeout_s", 601),
        ("sandbox_timeout_s", 1),
        ("sandbox_nano_cpus", 3_000_000_000),
        ("sandbox_pids_limit", 1000),
        ("sandbox_pids_limit", 4),
        ("sandbox_log_max_bytes", 2 * 1024 * 1024),
        ("max_llm_calls_per_job", 0),
        ("max_llm_calls_per_job", 50),
        ("repro_min_confidence", 1.5),
        ("gemini_rpm_limit", 0),
    ],
)
def test_numeric_hard_caps(make_settings: MakeSettings, field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


@pytest.mark.parametrize(
    ("value", "message"),
    [("4g", "2g hard cap"), ("32m", "at least 64m"), ("lots", "invalid memory size")],
)
def test_sandbox_mem_limit_validation(
    make_settings: MakeSettings, value: str, message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        make_settings(sandbox_mem_limit=value)


def test_sandbox_mem_limit_normalised(make_settings: MakeSettings) -> None:
    s = make_settings(sandbox_mem_limit=" 1G ")
    assert s.sandbox_mem_limit == "1g"
    assert s.sandbox_mem_bytes == 1024**3


@pytest.mark.parametrize(
    "url", ["redis://localhost:6379/0", "rediss://host:6380/1", "unix:///tmp/redis.sock"]
)
def test_redis_url_valid(make_settings: MakeSettings, url: str) -> None:
    assert make_settings(redis_url=url).redis_url == url


@pytest.mark.parametrize("url", ["http://localhost:6379", "localhost:6379", ""])
def test_redis_url_invalid(make_settings: MakeSettings, url: str) -> None:
    with pytest.raises(ValidationError, match="REDIS_URL"):
        make_settings(redis_url=url)


def test_invalid_log_level_rejected(make_settings: MakeSettings) -> None:
    with pytest.raises(ValidationError):
        make_settings(log_level="VERBOSE")
