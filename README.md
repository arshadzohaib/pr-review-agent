# PR Review Agent

An autonomous GitHub PR review, bug-finding, and patching agent built to run on **free-tier Gemini** quotas. See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design.

**Status:** Phase 1 (foundation and orchestration) is done. Webhooks, the worker, the AST engine, the sandbox, and the agent arrive in Phases 2–7.

## Prerequisites
- Windows with **Docker Desktop** (WSL2 backend)
- **Anaconda/Miniconda** with the `pr-agent` env (Python 3.11)

## Setup

```powershell
conda activate pr-agent
pip install -r requirements-dev.txt
Copy-Item .env.example .env   # then fill in real values; placeholders are rejected
```

## Tests

```powershell
python -m pytest -q                        # unit tests (no network, no Docker)
python -m pytest -m docker -q              # sandbox integration tests (Phase 5+)
ruff check . ; mypy
```

## Local stack (Docker Desktop)

```powershell
docker compose --profile build build sandbox-image   # offline sandbox runner image
docker compose up -d redis                            # Phase 1: Redis only
docker compose up --build                             # full stack (from Phase 3 on)
```

## Receiving webhooks locally
GitHub needs a public URL. Use [smee.io](https://smee.io):

```powershell
npx smee-client --url https://smee.io/<channel> --target http://127.0.0.1:8000/webhooks/github
```

Set the GitHub webhook content type to `application/json`, and use the same secret as `GITHUB_WEBHOOK_SECRET`. Subscribe to **Pull requests** events.

## Security
- Secrets come only from `.env`, which is git-ignored, and are never logged.
- Untrusted PR code runs only in ephemeral containers with `network_disabled=True`, memory/CPU/PID caps, a read-only rootfs, and auto-removal.
- The compose `worker` mounts the Docker socket (host-level privilege). See ARCHITECTURE.md §10.
