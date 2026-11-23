"""Unit tests for app.core.logging."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

import pytest

from app.core.config import Settings
from app.core.logging import (
    REDACTED,
    JsonFormatter,
    SecretRedactingFilter,
    configure_logging,
    redact,
)

GH_PAT = "ghp_" + "Q" * 36
FINE_PAT = "github_pat_" + "A1b2" * 10
GEMINI = "AIza" + "k" * 35


def _record(msg: str, *args: object, **extra: object) -> logging.LogRecord:
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, msg, args or None, None)
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


# ---- redact --------------------------------------------------------------------------
@pytest.mark.parametrize("token", [GH_PAT, FINE_PAT, GEMINI, "gho_" + "z" * 36])
def test_redact_token_patterns(token: str) -> None:
    out = redact(f"Authorization: token {token} end")
    assert token not in out
    assert REDACTED in out
    assert out.endswith(" end")


def test_redact_configured_secret_values() -> None:
    out = redact("webhook secret is my-super-secret-value!", ["my-super-secret-value"])
    assert out == f"webhook secret is {REDACTED}!"


def test_redact_overlapping_secrets_longest_first() -> None:
    out = redact("abc123xyz", ["abc", "abc123xyz"])
    assert out == REDACTED


def test_redact_ignores_empty_secrets_and_plain_text() -> None:
    assert redact("nothing sensitive here", ["", ""]) == "nothing sensitive here"


# ---- filter --------------------------------------------------------------------------
def test_filter_redacts_formatted_args() -> None:
    rec = _record("calling github with %s", GH_PAT)
    assert SecretRedactingFilter().filter(rec) is True
    assert GH_PAT not in rec.getMessage()
    assert rec.args is None


def test_filter_redacts_string_extras() -> None:
    rec = _record("ok", header=f"Bearer {GEMINI}", count=3)
    SecretRedactingFilter().filter(rec)
    assert GEMINI not in rec.header  # type: ignore[attr-defined]
    assert rec.count == 3  # type: ignore[attr-defined]


def test_filter_redacts_exception_text() -> None:
    try:
        raise RuntimeError(f"bad token {GH_PAT}")
    except RuntimeError:
        import sys

        rec = logging.LogRecord("t", logging.ERROR, __file__, 1, "boom", None, sys.exc_info())
    SecretRedactingFilter().filter(rec)
    assert rec.exc_info is None
    assert rec.exc_text is not None
    assert GH_PAT not in rec.exc_text
    assert "RuntimeError" in rec.exc_text


# ---- JSON formatter -----------------------------------------------------------------
def test_json_formatter_shape_and_extras() -> None:
    rec = _record("hello %s", "world", job_id="j-1", pr=42)
    payload = json.loads(JsonFormatter().format(rec))
    assert payload["msg"] == "hello world"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "t"
    assert payload["job_id"] == "j-1"
    assert payload["pr"] == 42
    assert "ts" in payload


# ---- configure_logging (end to end) --------------------------------------------------
@pytest.mark.usefixtures("restore_root_logger")
def test_configure_logging_redacts_end_to_end(
    make_settings: Callable[..., Settings],
    valid_env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(make_settings(log_level="DEBUG"))
    log = logging.getLogger("app.test")
    log.info("token=%s secret=%s", valid_env["GITHUB_TOKEN"], valid_env["GITHUB_WEBHOOK_SECRET"])
    log.debug("extra test", extra={"key": valid_env["GEMINI_API_KEY"]})

    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert len(lines) == 2
    blob = json.dumps(lines)
    for secret in valid_env.values():
        assert secret not in blob
    assert lines[0]["msg"] == f"token={REDACTED} secret={REDACTED}"
    assert lines[1]["key"] == REDACTED


@pytest.mark.usefixtures("restore_root_logger")
def test_configure_logging_is_idempotent_and_sets_level(
    make_settings: Callable[..., Settings],
) -> None:
    settings = make_settings(log_level="WARNING", log_json=False)
    configure_logging(settings)
    configure_logging(settings)
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert root.level == logging.WARNING
    assert not isinstance(root.handlers[0].formatter, JsonFormatter)
