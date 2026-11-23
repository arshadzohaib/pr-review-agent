# Minimal isolated sandbox image for running untrusted PR tests offline.
# Preloaded with pytest, git, and running as non-root sandboxuser (uid 10001).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    "pytest>=8.2,<10" \
    "pytest-timeout>=2.3,<3"

RUN useradd --create-home --uid 10001 sandboxuser \
    && git config --system --add safe.directory '*' \
    && git config --system user.email "sandbox@pr-agent.local" \
    && git config --system user.name "sandboxuser"

USER sandboxuser
WORKDIR /work
CMD ["python", "--version"]
