"""Hardened Docker sandbox execution runner using docker-py.

Enforces zero-network, read-only rootfs, memory/CPU/PID limits, and guaranteed container cleanup.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from typing import Any, Final

import docker
from app.sandbox.models import ExecutionOutcome, SandboxResult, make_sandbox_result

logger = logging.getLogger(__name__)

DEFAULT_SANDBOX_IMAGE: Final[str] = "pr-agent-sandbox-python:3.11"
DEFAULT_TIMEOUT_S: Final[float] = 30.0
DEFAULT_MEM_LIMIT: Final[str] = "256m"
DEFAULT_NANO_CPUS: Final[int] = 500_000_000  # 0.5 CPU
DEFAULT_PIDS_LIMIT: Final[int] = 64


def get_docker_client() -> docker.DockerClient | None:
    """Connect to the local Docker daemon. Returns None if unreachable."""
    try:
        client: docker.DockerClient = docker.from_env()
        client.ping()
        return client
    except Exception as exc:
        logger.warning("Docker daemon ping failed: %s", exc)
        return None


def run_in_sandbox(
    workspace_dir: str,
    command: str = "pytest -o cache_dir=/tmp/.pytest_cache -q test_repro.py",
    image: str = DEFAULT_SANDBOX_IMAGE,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    mem_limit: str = DEFAULT_MEM_LIMIT,
    nano_cpus: int = DEFAULT_NANO_CPUS,
    pids_limit: int = DEFAULT_PIDS_LIMIT,
    client: docker.DockerClient | None = None,
) -> SandboxResult:
    """Execute untrusted code inside a hardened ephemeral Docker container.

    Enforces:
    - network_disabled=True (no egress/ingress)
    - mem_limit and no swap
    - CPU quota (nano_cpus)
    - pids_limit (fork-bomb guard)
    - read_only=True root filesystem with writable /tmp tmpfs
    - non-root user execution
    - strict timeout with kill and remove in finally
    """
    if client is not None:
        try:
            client.ping()
            docker_client = client
        except Exception as exc:
            logger.warning("Passed Docker client ping failed: %s", exc)
            docker_client = None
    else:
        docker_client = get_docker_client()

    if docker_client is None:
        return SandboxResult(
            exit_code=-1,
            stdout="",
            stderr="Docker daemon is unavailable or not running",
            timed_out=False,
            duration_ms=0.0,
            test_passed=False,
            outcome=ExecutionOutcome.DOCKER_UNAVAILABLE,
            docker_available=False,
        )

    start_time = time.perf_counter()
    container: Any = None
    timed_out = False
    exit_code = -1
    stdout_str = ""
    stderr_str = ""

    # Ensure absolute path for Windows volume mount
    abs_work_dir = os.path.abspath(workspace_dir)

    try:
        container = docker_client.containers.create(
            image=image,
            command=["/bin/sh", "-c", command],
            network_disabled=True,
            network_mode="none",
            mem_limit=mem_limit,
            memswap_limit=mem_limit,
            nano_cpus=nano_cpus,
            pids_limit=pids_limit,
            read_only=True,
            tmpfs={"/tmp": "rw,size=64m"},  # noqa: S108
            volumes={abs_work_dir: {"bind": "/work", "mode": "rw"}},
            user="sandboxuser",
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            environment={"PYTHONDONTWRITEBYTECODE": "1", "HOME": "/tmp"},  # noqa: S108
        )

        container.start()

        # Wait with timeout
        try:
            wait_result = container.wait(timeout=int(timeout_s))
            exit_code = int(wait_result.get("StatusCode", -1))
        except Exception:
            # Timeout or socket read timeout occurred
            timed_out = True
            exit_code = 124
            with contextlib.suppress(Exception):
                container.kill()

        # Collect stdout and stderr logs
        try:
            raw_stdout = container.logs(stdout=True, stderr=False)
            stdout_str = raw_stdout.decode("utf-8", errors="replace") if raw_stdout else ""
        except Exception:
            stdout_str = ""

        try:
            raw_stderr = container.logs(stdout=False, stderr=True)
            stderr_str = raw_stderr.decode("utf-8", errors="replace") if raw_stderr else ""
        except Exception:
            stderr_str = ""

    except Exception as exc:
        logger.error("Failed to run sandbox container: %s", exc)
        stderr_str = f"Container execution error: {exc}"
        exit_code = -1

    finally:
        # Guaranteed cleanup: container is always removed
        if container is not None:
            with contextlib.suppress(Exception):
                container.remove(force=True)

    elapsed_ms = (time.perf_counter() - start_time) * 1000.0
    return make_sandbox_result(
        exit_code=exit_code,
        stdout=stdout_str,
        stderr=stderr_str,
        duration_ms=elapsed_ms,
        timed_out=timed_out,
        docker_available=True,
    )


async def run_in_sandbox_async(
    workspace_dir: str,
    command: str = "pytest -o cache_dir=/tmp/.pytest_cache -q test_repro.py",
    image: str = DEFAULT_SANDBOX_IMAGE,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    mem_limit: str = DEFAULT_MEM_LIMIT,
    nano_cpus: int = DEFAULT_NANO_CPUS,
    pids_limit: int = DEFAULT_PIDS_LIMIT,
    client: docker.DockerClient | None = None,
) -> SandboxResult:
    """Asynchronous wrapper running the blocking docker execution in a worker thread."""
    return await asyncio.to_thread(
        run_in_sandbox,
        workspace_dir=workspace_dir,
        command=command,
        image=image,
        timeout_s=timeout_s,
        mem_limit=mem_limit,
        nano_cpus=nano_cpus,
        pids_limit=pids_limit,
        client=client,
    )
