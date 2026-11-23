"""Integration tests executing real Docker containers on local Docker Desktop.

Marked with @pytest.mark.docker.
"""

from __future__ import annotations

import pytest

import docker
from app.sandbox.models import ExecutionOutcome
from app.sandbox.runner import get_docker_client, run_in_sandbox
from app.sandbox.workspace import sandbox_workspace


@pytest.fixture(scope="module")
def real_docker_client() -> docker.DockerClient:
    client = get_docker_client()
    if client is None:
        pytest.skip("Docker daemon is not available")
    return client


@pytest.mark.docker
def test_docker_sandbox_passing_test(real_docker_client: docker.DockerClient) -> None:
    files = {
        "calc.py": "def add(a, b): return a + b\n",
    }
    test_code = "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"

    with sandbox_workspace(files, test_code=test_code) as ws:
        res = run_in_sandbox(
            workspace_dir=ws,
            command="pytest -o cache_dir=/tmp/.pytest_cache -q test_repro.py",
            timeout_s=15.0,
            client=real_docker_client,
        )

        assert res.docker_available is True
        assert res.exit_code == 0
        assert res.test_passed is True
        assert res.outcome == ExecutionOutcome.PASSED
        assert "1 passed" in res.stdout


@pytest.mark.docker
def test_docker_sandbox_failing_test(real_docker_client: docker.DockerClient) -> None:
    files = {
        "calc.py": "def add(a, b): return a - b\n",
    }
    test_code = "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"

    with sandbox_workspace(files, test_code=test_code) as ws:
        res = run_in_sandbox(
            workspace_dir=ws,
            command="pytest -o cache_dir=/tmp/.pytest_cache -q test_repro.py",
            timeout_s=15.0,
            client=real_docker_client,
        )

        assert res.docker_available is True
        assert res.exit_code == 1
        assert res.test_passed is False
        assert res.outcome == ExecutionOutcome.TEST_FAILED
        assert "FAILED test_repro.py::test_add" in res.stdout


@pytest.mark.docker
def test_docker_sandbox_network_disabled(real_docker_client: docker.DockerClient) -> None:
    # Attempting to open socket or connection must fail inside network_disabled container
    test_code = """
import socket, pytest

def test_no_network():
    with pytest.raises(OSError):
        s = socket.create_connection(('1.1.1.1', 80), timeout=1.0)
        s.close()
"""
    with sandbox_workspace({}, test_code=test_code) as ws:
        res = run_in_sandbox(
            workspace_dir=ws,
            command="pytest -o cache_dir=/tmp/.pytest_cache -q test_repro.py",
            timeout_s=15.0,
            client=real_docker_client,
        )

        assert res.test_passed is True
        assert res.outcome == ExecutionOutcome.PASSED


@pytest.mark.docker
def test_docker_sandbox_timeout_killing(real_docker_client: docker.DockerClient) -> None:
    # A test that sleeps longer than timeout_s should be killed
    test_code = "import time\n\ndef test_slow():\n    time.sleep(10)\n"

    with sandbox_workspace({}, test_code=test_code) as ws:
        res = run_in_sandbox(
            workspace_dir=ws,
            command="pytest -o cache_dir=/tmp/.pytest_cache -q test_repro.py",
            timeout_s=2.0,
            client=real_docker_client,
        )

        assert res.timed_out is True
        assert res.outcome == ExecutionOutcome.TIMEOUT
