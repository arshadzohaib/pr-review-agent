# Autonomous GitHub PR Code Review & Bug-Finding Agent
# Multi-purpose production container for FastAPI webhook gateway and background review worker
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install system build essentials and git (required for tree-sitter C compilation and git operations)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install Python runtime dependencies
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy application source code
COPY app ./app

# Expose default HTTP port for FastAPI webhook server
EXPOSE 8000

# Default entrypoint runs the FastAPI webhook server
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
