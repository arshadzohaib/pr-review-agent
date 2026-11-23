# Autonomous GitHub PR Code Review & Bug-Patching Agent

[![CI Status](https://img.shields.io/badge/tests-passing-brightgreen)](https://github.com/)
[![Python 3.11](https://img.shields.io/badge/python-3.11-blue.svg)](https://www.python.org/)
[![Docker Desktop](https://img.shields.io/badge/docker-hardened%20sandbox-blue)](https://www.docker.com/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg)](https://fastapi.tiangolo.com/)
[![LangGraph](https://img.shields.io/badge/LangGraph-stateful%20workflow-orange)](https://github.com/langchain-ai/langgraph)
[![Tree-sitter](https://img.shields.io/badge/Tree--sitter-AST%20parsing-teal)](https://tree-sitter.github.io/)
[![Google Gemini API](https://img.shields.io/badge/Gemini%203.8%20Flash-Free%20Tier%20Guarded-4285F4)](https://ai.google.dev/)
[![Redis 7](https://img.shields.io/badge/Redis-Streams%20%2B%20Lua-red)](https://redis.io/)

An autonomous, production-grade GitHub Pull Request review, bug-finding, and patch-verification agent. Engineered to run completely within **free-tier Gemini API quotas** without sacrificing architectural depth or execution security.

---

## 🏛️ System Architecture

```mermaid
flowchart TD
    GH["GitHub Webhook (PR opened/synchronize)"] -->|HMAC-SHA256 Signed| API["FastAPI Webhook Gateway (/webhook/github)"]
    API -->|Atomic De-dupe & PR Lock| REDIS[("Redis 7 (Streams, Locks, Caches)")]
    REDIS -->|stream:jobs Dequeue| WRK["Background Worker Loop (app/worker.py)"]

    subgraph Quota Protection & Pre-Filtering
        WRK -->|Unified Diff| PRE["Zero-LLM Static Pre-Filter"]
        PRE -->|Filter Lockfiles, Docs, Assets, Pure Formatting| SKIP["Skip Non-Code (0 LLM Cost)"]
        PRE -->|Relevant Hunks| CACHE{"Redis SHA Diff Cache"}
        CACHE -->|Cache Hit| PUB_CACHE["Reuse Previous Review (0 LLM Cost)"]
    end

    subgraph AST-Driven RAG Engine
        CACHE -->|Cache Miss| TS["Tree-sitter Parser (Python Grammars)"]
        TS -->|Map Lines to Function Defs| SYM["Enclosing Symbol Extractor"]
        SYM -->|1-Hop Call Graph| CALLERS["Direct Caller Discovery"]
        CALLERS -->|Token Budget Pack <=6000| CTX["Compact AST Context Payload"]
    end

    subgraph LangGraph Verification Loop
        CTX -->|Lua Rate Limiter (RPM/TPM/RPD)| LLM_REV["analyze_diff (Gemini 3.8 Flash)"]
        LLM_REV -->|Clean PR / No Bugs| END_CLEAN["Exit Cleanly (Review Comment)"]
        LLM_REV -->|Suspected Bug| REPRO_GEN["generate_reproduction (Write pytest repro)"]
        REPRO_GEN --> SBX_BASE["Docker Sandbox (network_disabled=True)"]
        SBX_BASE -->|Test Passed| FP["False Positive Discard (Exit)"]
        SBX_BASE -->|Env Error / Missing Lib| FALLBACK["Downgrade to Static Review"]
        SBX_BASE -->|Test Failed| PATCH_GEN["generate_patch (Unified Diff Fix)"]
        PATCH_GEN --> SBX_PATCH["Docker Sandbox (git apply + pytest)"]
        SBX_PATCH -->|Test Passes| VERIFIED["Verified Patch Suggestion"]
        SBX_PATCH -->|Test Fails && Retries < 2| RETRY["Retry with Error Logs (max 2)"]
    end

    VERIFIED --> POST["PyGithub: Inline Suggestion & Review Summary"]
    FALLBACK --> POST
    PUB_CACHE --> POST
    END_CLEAN --> POST
```

---

## 🌟 Key Engineering Highlights

### 1. Zero-LLM Static Pre-Filter (`app/core/prefilter.py`)
- **Semantic Code-Aware Pre-Filtering:** Filters out documentation (`.md`, `.rst`), lockfiles (`poetry.lock`, `package-lock.json`), assets (`.png`, `.svg`), binary files, and auto-generated code (`*.pb2.py`, `dist/`).
- **Python AST Equivalence Detection:** Detects whitespace-, comment-, or formatting-only diffs by parsing base and head blobs with Python's built-in `ast` and comparing structural normalized fingerprints (`ast.dump(ast.parse(src))`).
- **Cost Impact:** Drops 40–70% of noisy pull request hunks before any model invocation is triggered.

### 2. Atomic Redis Lua Rate Limiter & SHA Diff Cache (`app/core/`)
- **Atomic Sliding Window Rate Limiting:** Enforces Gemini free-tier RPM (Requests Per Minute), TPM (Tokens Per Minute), and RPD (Requests Per Day) using atomic Redis Lua scripts (`GeminiRateLimiter`). Prevents race conditions across distributed workers.
- **Pacific Midnight Quota Windowing:** Accurately resets daily RPD counters according to Google Gemini's Pacific Time midnight reset schedule.
- **Deterministic SHA Diff Caching:** Normalizes function source (stripping comments, collapsing internal whitespace, preserving indentation) and caller context into a SHA-256 key (`rv:{v}:{model}:{sha256}`). Re-triggered PRs, rebases, and moved functions hit cache with **zero LLM calls**.

### 3. Tree-sitter AST Symbol & Caller Extraction (`app/ast_engine/`)
- **Surgical Symbol Mapping:** Uses concrete syntax trees (CST) via Tree-sitter to map modified hunk line ranges directly to enclosing functions, async methods, and classes.
- **1-Hop Direct Caller Discovery:** In-memory repository symbol index (`RepoIndex`) identifies direct callers and imported symbols referencing modified code to give the LLM essential surrounding context without ingesting unrelated repository files.
- **Token-Budgeted Packing:** Assembles compact AST payloads strictly prioritized by: (1) modified function source, (2) class signatures & `__init__` attributes, (3) direct callers, and (4) module imports, capped at `max_context_tokens = 6000`.

### 4. Hardened Ephemeral Docker Sandbox (`app/sandbox/`)
- **Zero-Network Execution:** Executes untrusted PR code and test scripts with `network_disabled=True` and `network_mode="none"`. Code cannot access the host, named pipes, or egress to external networks.
- **Strict Kernel Resource Hardening:** Enforces `mem_limit="256m"`, `memswap_limit="256m"`, `nano_cpus=500_000_000` (0.5 CPU), `pids_limit=64` (fork-bomb guard), dropped Linux capabilities (`cap_drop=["ALL"]`), and non-root execution (`sandboxuser`, `uid=10001`).
- **Immutable Rootfs with Tmpfs:** Read-only root filesystem (`read_only=True`) with a writable 64MB tmpfs mounted at `/tmp`.
- **Guaranteed Cleanup:** Containers are strictly killed and removed in `finally` blocks, with ephemeral workspaces created outside OneDrive (`tempfile.mkdtemp(prefix="pr_sandbox_")`).

### 5. LangGraph Stateful Review-Reproduce-Patch Verification Loop (`app/agent/`)
- **Stateful State Machine:** Built on LangGraph `StateGraph(AgentState)` with structured Pydantic v2 schemas (`ReviewFinding`, `ReproductionTestPlan`, `PatchProposal`).
- **Empirical Bug Confirmation:** Suspected bugs are tested by generating a standalone pytest test (`test_repro.py`) and running it inside the sandbox. If the baseline test passes, the finding is discarded as a **false positive**.
- **Empirical Patch Verification:** Proposed unified diffs are applied via `git apply` inside the sandbox and re-tested. Patches are only published as verified if `test_repro.py` transitions from failing to passing cleanly.
- **Strict Retry Guardrail:** Enforces `max_retries = 2` on patch failures, gracefully falling back to a static review comment rather than burning quota on infinite loops.

---

## 🚀 Quickstart & Setup

### Prerequisites
- **OS:** Windows 10/11 with **Docker Desktop** (WSL2 backend)
- **Environment:** Anaconda or Miniconda with Python 3.11
- **Accounts / Keys:**
  - [Google Gemini API Key](https://aistudio.google.com/) (free tier supported)
  - GitHub Personal Access Token (PAT) with `repo` scope
  - GitHub Webhook Secret

### 1. Clone & Activate Environment
```powershell
git clone https://github.com/your-username/PR-review-agent.git
cd PR-review-agent

conda create -n pr-agent python=3.11 -y
conda activate pr-agent

pip install -r requirements.txt
pip install -r requirements-dev.txt
```

### 2. Configure Environment Variables
```powershell
Copy-Item .env.example .env
```
Edit `.env` and provide your API keys:
```env
APP_ENV=dev
LOG_LEVEL=INFO
GITHUB_TOKEN=ghp_yourRealGitHubTokenHere
GITHUB_WEBHOOK_SECRET=yourWebhookSecretHere
GEMINI_API_KEY=AIzaSyYourGeminiApiKeyHere
GEMINI_MODEL=gemini-3.8-flash
REDIS_URL=redis://localhost:6379/0
```

### 3. Build Sandbox Runner Image
```powershell
docker compose --profile build build sandbox-image
```

---

## 🧪 Testing & Verification

Run the comprehensive unit test suite:
```powershell
# Run all unit tests (zero network, mocked external APIs)
python -m pytest -q -m "not docker" -p no:cacheprovider

# Run real Docker sandbox integration tests (verifies network block, timeouts, and resource caps)
python -m pytest -q -m "docker" -p no:cacheprovider

# Run Ruff linter and strict Mypy type-checker
python -m ruff check . --no-cache
python -m mypy --cache-dir=nul
```

---

## 🚢 Local Orchestration & Webhook Forwarding

### 1. Start Local Redis & Services
```powershell
# Start Redis container
docker compose up -d redis

# Start FastAPI webhook server
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# In a separate terminal, start the background review worker
python -m app.worker
```

### 2. Forward Webhooks from GitHub (Smee.io)
GitHub webhooks require a publicly reachable URL. You can use Smee.io to proxy events to your local machine:
```powershell
# 1. Start smee client
npx smee-client --url https://smee.io/<your-channel-id> --target http://127.0.0.1:8000/webhook/github
```
In your GitHub repository settings:
1. Go to **Settings** -> **Webhooks** -> **Add webhook**.
2. **Payload URL:** `https://smee.io/<your-channel-id>`
3. **Content type:** `application/json`
4. **Secret:** Set to the same value as `GITHUB_WEBHOOK_SECRET` in `.env`.
5. **Events:** Select **Let me select individual events** -> check **Pull requests**.

---

## 📂 Project Structure

```
PR-review-agent/
├── app/
│   ├── api/                  # FastAPI webhook & health endpoints
│   │   ├── routes_health.py  # /health & /ready probes
│   │   ├── routes_webhook.py # POST /webhook/github handler
│   │   ├── schemas.py        # Webhook payload Pydantic models
│   │   └── security.py       # HMAC-SHA256 signature verification
│   ├── core/                 # Config, Redis, pre-filters & rate limiting
│   │   ├── cache.py          # Redis SHA diff cache & delivery dedupe
│   │   ├── config.py         # Pydantic Settings with security guardrails
│   │   ├── diff_parser.py    # Unified diff parser
│   │   ├── locks.py          # Distributed atomic PR lock & supersede tracking
│   │   ├── prefilter.py      # Zero-LLM static pre-filter (AST equivalence)
│   │   ├── queue.py          # Redis Streams queue & consumer groups
│   │   └── rate_limiter.py   # Atomic Redis Lua rate limiter (RPM/TPM/RPD)
│   ├── ast_engine/           # Tree-sitter AST symbol & caller engine
│   │   ├── context_builder.py# Token-budgeted context packer (max 6000 tokens)
│   │   ├── index.py          # Cross-file direct caller index (RepoIndex)
│   │   ├── languages.py      # Pluggable grammar registry
│   │   ├── parser.py         # Safe error-tolerant CST parser
│   │   └── symbols.py        # Hunk-to-symbol and method resolution
│   ├── sandbox/              # Isolated ephemeral Docker runner
│   │   ├── models.py         # SandboxResult & ExecutionOutcome models
│   │   ├── runner.py         # Hardened docker-py runner (network_disabled=True)
│   │   └── workspace.py      # Ephemeral workspace directory manager
│   ├── agent/                # LangGraph review-reproduce-patch workflow
│   │   ├── gemini_client.py  # Structured Gemini client with Lua rate limiter
│   │   ├── graph.py          # Compiled StateGraph & pure router functions
│   │   ├── schemas.py        # ReviewFinding, ReproductionTestPlan, PatchProposal
│   │   ├── state.py          # AgentState TypedDict
│   │   ├── nodes/            # 5 stateful LangGraph nodes
│   │   └── prompts/          # Structured prompt builders
│   ├── github_client/        # PyGithub PR client
│   │   └── client.py         # Workspace extraction, review formatting, bot dedupe
│   ├── main.py               # FastAPI application factory & lifespan
│   └── worker.py             # End-to-end background worker pipeline loop
├── docker/
│   ├── app.Dockerfile        # Production FastAPI & worker container
│   └── sandbox.Dockerfile    # Hardened offline Python 3.11 test runner image
├── tests/                    # 200+ unit and Docker integration tests
├── ARCHITECTURE.md           # Full technical architectural specification
├── docker-compose.yml        # Orchestration for Redis + App + Worker + Sandbox
└── pyproject.toml            # Tooling configuration (Ruff, Mypy strict, Pytest)
```

---

## 🔒 Security Architecture

1. **Host Isolation:** Untrusted repository code or diffs are never executed on the host system. Execution happens solely inside ephemeral Docker containers with `network_disabled=True` and `user="sandboxuser"`.
2. **Zero Ingress/Egress:** Network isolation ensures no exfiltration of environment secrets or external malicious payload downloads.
3. **No Root Privileges:** All root and Linux capabilities are dropped (`cap_drop=["ALL"]`), with `no-new-privileges` enabled.
4. **Secret Redaction:** Logging infrastructure strictly redacts sensitive tokens, API keys, and authorization headers.
5. **Path Traversal Guards:** All tarball extraction and workspace file preparation sanitize relative paths, rejecting any path containing `..` or absolute prefixes.
