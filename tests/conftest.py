"""Shared pytest fixtures."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings, get_settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent

VALID_SECRETS: dict[str, str] = {
    "GITHUB_TOKEN": "ghp_" + "a" * 36,
    "GITHUB_WEBHOOK_SECRET": "w" * 32,
    "GEMINI_API_KEY": "AIza" + "b" * 35,
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate every test from the developer's real environment variables."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def valid_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    for key, value in VALID_SECRETS.items():
        monkeypatch.setenv(key, value)
    return dict(VALID_SECRETS)


@pytest.fixture
def make_settings(valid_env: dict[str, str]) -> Callable[..., Settings]:
    """Build Settings from valid env secrets + overrides, never reading a real `.env`."""

    def _make(**overrides: Any) -> Settings:
        return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]

    return _make


@pytest.fixture
def restore_root_logger() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in handlers:
        root.addHandler(h)
    root.setLevel(level)
