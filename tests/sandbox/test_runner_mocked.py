"""Unit tests for Docker runner with mocked docker client."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from app.sandbox.models import ExecutionOutcome
from app.sandbox.runner import run_in_sandbox


def test_runner_docker_unavailable_fallback(tmp_path: Path) -> None:
    mock_client = MagicMock()
    mock_client.ping.side_effect = RuntimeError("daemon down")

    res = run_in_sandbox(str(tmp_path), client=mock_client)
    assert res.docker_available is False
    assert res.outcome == ExecutionOutcome.DOCKER_UNAVAILABLE


def test_runner_enforces_hardening_kwargs(tmp_path: Path) -> None:
    mock_client = MagicMock()
    mock_client.ping.return_value = True

    mock_container = MagicMock()
    mock_container.wait.return_value = {"StatusCode": 0}
    mock_container.logs.side_effect = (
        lambda stdout=False, stderr=False: b"1 passed\n" if stdout else b""
    )
    mock_client.containers.create.return_value = mock_container

    res = run_in_sandbox(
        workspace_dir=str(tmp_path),
        command="pytest -q test_repro.py",
        client=mock_client,
    )

    assert res.test_passed is True
    assert res.outcome == ExecutionOutcome.PASSED
    assert "1 passed" in res.stdout

    # Verify hardening parameters passed to containers.create
    mock_client.containers.create.assert_called_once()
    kwargs = mock_client.containers.create.call_args.kwargs

    assert kwargs["network_disabled"] is True
    assert kwargs["network_mode"] == "none"
    assert kwargs["mem_limit"] == "256m"
    assert kwargs["memswap_limit"] == "256m"
    assert kwargs["nano_cpus"] == 500_000_000
    assert kwargs["pids_limit"] == 64
    assert kwargs["read_only"] is True
    assert kwargs["user"] == "sandboxuser"
    assert kwargs["cap_drop"] == ["ALL"]
    assert kwargs["security_opt"] == ["no-new-privileges"]

    # Verify container removed
    mock_container.remove.assert_called_once_with(force=True)


def test_runner_handles_timeout_and_kills_container(tmp_path: Path) -> None:
    mock_client = MagicMock()
    mock_client.ping.return_value = True

    mock_container = MagicMock()
    mock_container.wait.side_effect = TimeoutError("Timed out")
    mock_container.logs.side_effect = (
        lambda stdout=False, stderr=False: b"" if stdout else b"timeout"
    )
    mock_client.containers.create.return_value = mock_container

    res = run_in_sandbox(
        workspace_dir=str(tmp_path),
        timeout_s=5.0,
        client=mock_client,
    )

    assert res.timed_out is True
    assert res.outcome == ExecutionOutcome.TIMEOUT
    mock_container.kill.assert_called_once()
    mock_container.remove.assert_called_once_with(force=True)
