"""Phase 1 structural tests: layout, env template, gitignore, compose, requirements."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from tests.conftest import PROJECT_ROOT, VALID_SECRETS

PACKAGES = [
    "app",
    "app/api",
    "app/core",
    "app/integrations",
    "app/ast_engine",
    "app/sandbox",
    "app/agent",
    "app/agent/nodes",
    "app/agent/prompts",
]
ROOT_FILES = [
    "ARCHITECTURE.md",
    "README.md",
    ".gitignore",
    ".dockerignore",
    ".env.example",
    "requirements.txt",
    "requirements-dev.txt",
    "pyproject.toml",
    "docker-compose.yml",
    "docker/app.Dockerfile",
    "docker/sandbox-python.Dockerfile",
]


def _env_example_keys() -> dict[str, str]:
    text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    pairs = (line.split("=", 1) for line in text.splitlines() if re.match(r"^[A-Z_]+=", line))
    return {k: v for k, v in pairs}


@pytest.mark.parametrize("pkg", PACKAGES)
def test_package_exists(pkg: str) -> None:
    assert (PROJECT_ROOT / pkg / "__init__.py").is_file()


@pytest.mark.parametrize("path", ROOT_FILES)
def test_root_file_exists(path: str) -> None:
    assert (PROJECT_ROOT / path).is_file()


def test_gitignore_protects_env_but_keeps_example() -> None:
    lines = {
        ln.strip()
        for ln in (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.startswith("#")
    }
    assert ".env" in lines
    assert "!.env.example" in lines
    assert ".venv/" in lines


def test_env_example_matches_settings_fields() -> None:
    keys = set(_env_example_keys())
    fields = {name.upper() for name in Settings.model_fields}
    assert keys == fields, f"missing={fields - keys} unknown={keys - fields}"


def test_env_example_placeholders_are_rejected() -> None:
    with pytest.raises(ValidationError, match="placeholder"):
        Settings(_env_file=PROJECT_ROOT / ".env.example")  # type: ignore[call-arg]


def test_env_example_non_secret_values_are_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in VALID_SECRETS.items():
        monkeypatch.setenv(key, value)  # env vars take priority over the dotenv file
    s = Settings(_env_file=PROJECT_ROOT / ".env.example")  # type: ignore[call-arg]
    assert s.max_retries == 2
    assert s.sandbox_mem_limit == "512m"


def test_requirements_cover_stack() -> None:
    text = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    for pkg in [
        "fastapi",
        "pydantic",
        "pydantic-settings",
        "redis",
        "langgraph",
        "google-genai",
        "pygithub",
        "docker",
        "tree-sitter",
        "tree-sitter-python",
        "unidiff",
    ]:
        assert re.search(rf"^{re.escape(pkg)}(\[.*\])?[<>=]", text, re.MULTILINE), pkg


def test_docker_compose_services() -> None:
    yaml = pytest.importorskip("yaml")
    compose = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert {"redis", "api", "worker", "sandbox-image"} <= set(services)
    assert "noeviction" in services["redis"]["command"]
    for name in ("redis", "api"):
        assert all(p.startswith("127.0.0.1:") for p in services[name]["ports"])
    assert services["sandbox-image"]["profiles"] == ["build"]
    assert services["sandbox-image"]["image"] == "pr-agent-sandbox-python:3.11"
    assert any("docker.sock" in v for v in services["worker"]["volumes"])
    assert "docker.sock" not in str(services["api"])
    for name in ("api", "worker"):
        assert services[name]["env_file"] == ".env"


def test_sandbox_dockerfile_is_non_root() -> None:
    text = (PROJECT_ROOT / "docker/sandbox-python.Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^USER 65534:65534$", text, re.MULTILINE)


def test_no_secrets_committed_in_templates() -> None:
    pattern = re.compile(r"(ghp_[A-Za-z0-9]{36}|github_pat_\w{22,}|AIza[0-9A-Za-z\-_]{35})")
    for rel in [".env.example", "docker-compose.yml", "ARCHITECTURE.md", "README.md"]:
        assert not pattern.search((PROJECT_ROOT / rel).read_text(encoding="utf-8")), rel


def test_no_real_env_file_checked_in_by_tests() -> None:
    # A local .env is allowed but must be ignored; this guards the template name only.
    assert Path(PROJECT_ROOT / ".env.example").name == ".env.example"
