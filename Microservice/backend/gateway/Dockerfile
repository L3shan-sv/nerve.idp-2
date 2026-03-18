# Nerve IDP Gateway — Dockerfile
# Multi-stage build: builder installs deps, final image is minimal

# ── Stage 1: builder ──────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies into a virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ── Stage 2: production image ─────────────────
FROM python:3.12-slim AS production

ARG BUILD_SHA=unknown
LABEL org.opencontainers.image.description="Nerve IDP Gateway"
LABEL org.opencontainers.image.revision="${BUILD_SHA}"

# Runtime dependencies only
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root user — never run as root in production
RUN useradd --create-home --shell /bin/bash nerve
WORKDIR /app

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy application code
COPY --chown=nerve:nerve app/ ./app/

# Build SHA for version endpoint
ENV BUILD_SHA=${BUILD_SHA}

USER nerve

# Readiness/liveness probe port
EXPOSE 8000

# Health check — Kubernetes liveness probe
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Production: uvicorn with multiple workers
# Workers = (2 * CPU cores) + 1 — standard formula
# Override via UVICORN_WORKERS env var
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "4", \
     "--loop", "uvloop", \
     "--http", "httptools", \
     "--access-log", \
     "--log-level", "info"]
