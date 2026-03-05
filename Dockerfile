# ─────────────────────────────────────────────────────────────────────────────
# LocalGenBI-Agent — Multi-stage Dockerfile
#
# Stages:
#   base     : common Python image + system deps
#   builder  : install Python packages into an isolated venv
#   backend  : FastAPI API + DB gateway servers (production)
#   frontend : Chainlit UI (production)
#
# Usage:
#   docker build --target backend  -t localgenbi-backend:latest .
#   docker build --target frontend -t localgenbi-frontend:latest .
#
# Both targets are built automatically by docker-compose.yml.
# ─────────────────────────────────────────────────────────────────────────────


# ── Stage 1: base ─────────────────────────────────────────────────────────────
FROM python:3.11-slim AS base

# Prevents .pyc files and forces unbuffered stdout/stderr for clean Docker logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# System libraries required by matplotlib (font rendering) and asyncpg (C extension)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq-dev         \
        gcc               \
        g++               \
        libfreetype6-dev  \
        libpng-dev        \
        pkg-config        \
        curl              \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app


# ── Stage 2: builder (install all Python deps into an isolated venv) ──────────
FROM base AS builder

COPY requirements.txt .

RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --upgrade pip && \
    /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

ENV PATH="/opt/venv/bin:$PATH"


# ── Stage 3: backend (FastAPI + all DB gateway servers) ───────────────────────
FROM base AS backend

LABEL maintainer="LocalGenBI-Agent"
LABEL description="FastAPI backend + DB gateway servers"

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy application source
# NOTE: folder renamed mcp_servers/ → db_gateway/
COPY backend/     /app/backend/
COPY db_gateway/  /app/db_gateway/
COPY features/    /app/features/
COPY guardrails/  /app/guardrails/
COPY llm_client/  /app/llm_client/
COPY config/      /app/config/
COPY evaluation/  /app/evaluation/

WORKDIR /app

# Default export directory (ExportManager creates it on first run, but this
# ensures the directory exists with correct ownership before first write)
RUN mkdir -p /app/temp/exports

# Non-root user
RUN useradd --no-create-home --shell /bin/false appuser && \
    chown -R appuser:appuser /app
USER appuser

# FastAPI
EXPOSE 8001

# DB gateway ports
EXPOSE 3001 3002 3003 3004

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8001/health')"

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "2"]


# ── Stage 4: frontend (Chainlit) ───────────────────────────────────────────────
FROM base AS frontend

LABEL maintainer="LocalGenBI-Agent"
LABEL description="Chainlit chat frontend"

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY frontend/ /app/frontend/
COPY config/   /app/config/

WORKDIR /app

RUN mkdir -p /tmp/localgenbi_exports

RUN useradd --no-create-home --shell /bin/false appuser && \
    chown -R appuser:appuser /app /tmp/localgenbi_exports
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000')" || exit 1

CMD ["chainlit", "run", "frontend/app.py", "--host", "0.0.0.0", "--port", "8000"]