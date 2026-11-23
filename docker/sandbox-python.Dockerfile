# Offline sandbox runner for untrusted PR code.
# Runs with network disabled, read-only rootfs, tmpfs /work and /tmp, as uid 65534.
# Everything the tests need must be baked in here; nothing is installed at runtime.
# (v2 enhancement: per-repo pre-built dependency images layered on top of this one.)
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Test tooling + a small set of very common libraries.
RUN pip install \
    "pytest>=8.2,<10" \
    "pytest-timeout>=2.3,<3" \
    "hypothesis>=6.100,<7" \
    "requests>=2.32,<3" \
    "pydantic>=2.7,<3" \
    "attrs>=23,<26"

RUN git config --system --add safe.directory '*' \
    && git config --system user.email "sandbox@pr-agent.local" \
    && git config --system user.name "pr-agent-sandbox"

USER 65534:65534
WORKDIR /work
CMD ["python", "--version"]
