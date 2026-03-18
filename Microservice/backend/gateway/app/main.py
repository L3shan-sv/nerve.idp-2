"""
Nerve IDP — FastAPI Gateway
Single entry point for all API traffic.

Responsibilities:
- JWT authentication (python-jose)
- Rate limiting (slowapi)
- OpenTelemetry instrumentation (wired from startup)
- CORS
- Request routing to downstream microservices
- Audit log middleware
- OPA startup health gate

CRITICAL: OPA sidecar must be healthy before this gateway
accepts traffic. The startup probe checks OPA /health before
the pod joins the load balancer.
"""

from contextlib import asynccontextmanager
import logging
import time
import uuid

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from prometheus_client import make_asgi_app
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.api.v1.routers import (
    auth,
    catalog,
    deploy,
    scaffold,
    iac,
    pipelines,
    observability,
    cost,
    security,
    blast_radius,
    fleet,
    runbooks,
    ai_copilot,
    docs,
    chaos,
    teams,
    audit,
    policies,
    health,
)
from app.core.config import settings
from app.core.database import engine, async_session_maker
from app.middleware.audit import AuditMiddleware
from app.middleware.request_id import RequestIdMiddleware

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# OpenTelemetry setup
# Wired from startup — not retrofitted later.
# ─────────────────────────────────────────────
def setup_telemetry() -> None:
    resource = Resource.create({
        "service.name": "nerve-gateway",
        "service.version": settings.APP_VERSION,
        "deployment.environment": settings.ENVIRONMENT,
    })

    # Traces
    tracer_provider = TracerProvider(resource=resource)
    otlp_exporter = OTLPSpanExporter(
        endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT,
        insecure=True,
    )
    tracer_provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
    trace.set_tracer_provider(tracer_provider)

    # Metrics
    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT, insecure=True),
        export_interval_millis=10_000,
    )
    MeterProvider(resource=resource, metric_readers=[metric_reader])

    # Auto-instrument libraries
    HTTPXClientInstrumentor().instrument()
    SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)

    logger.info("OpenTelemetry configured → %s", settings.OTEL_EXPORTER_OTLP_ENDPOINT)


# ─────────────────────────────────────────────
# OPA health check
# The gateway MUST NOT start accepting traffic
# until OPA sidecar is ready.
# ─────────────────────────────────────────────
async def wait_for_opa(max_retries: int = 30, delay: float = 1.0) -> None:
    async with httpx.AsyncClient() as client:
        for attempt in range(max_retries):
            try:
                response = await client.get(
                    f"{settings.OPA_URL}/health",
                    timeout=2.0,
                )
                if response.status_code == 200:
                    logger.info("OPA sidecar is ready")
                    return
            except (httpx.ConnectError, httpx.TimeoutException):
                pass
            logger.warning(
                "OPA not ready (attempt %d/%d) — retrying in %.1fs",
                attempt + 1, max_retries, delay,
            )
            await asyncio.sleep(delay)
    raise RuntimeError(
        f"OPA sidecar not ready after {max_retries} attempts. "
        "Gateway startup aborted. This prevents fail-open on policy checks."
    )


# ─────────────────────────────────────────────
# Lifespan — startup and shutdown
# ─────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Nerve IDP Gateway starting — version %s", settings.APP_VERSION)

    setup_telemetry()

    # Wait for OPA before accepting traffic
    if settings.ENVIRONMENT != "test":
        await wait_for_opa()

    # Verify database connectivity via PgBouncer
    async with async_session_maker() as session:
        await session.execute("SELECT 1")
    logger.info("Database connection via PgBouncer: OK")

    yield

    # Shutdown
    logger.info("Nerve IDP Gateway shutting down")


# ─────────────────────────────────────────────
# Rate limiter
# ─────────────────────────────────────────────
limiter = Limiter(key_func=get_remote_address)


# ─────────────────────────────────────────────
# App
# ─────────────────────────────────────────────
app = FastAPI(
    title="Nerve IDP API",
    description="Internal Developer Platform — Golden path, observability, and AI ops.",
    version=settings.APP_VERSION,
    openapi_url="/api/v1/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# Rate limiting
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Request ID — every request gets a UUID for distributed tracing correlation
app.add_middleware(RequestIdMiddleware)

# Audit log — every state-changing request is written to the audit_log table
app.add_middleware(AuditMiddleware)

# Prometheus metrics endpoint
metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)

# OTel FastAPI auto-instrumentation
FastAPIInstrumentor.instrument_app(
    app,
    excluded_urls="/health,/health/ready,/metrics",
)


# ─────────────────────────────────────────────
# Global exception handler
# Returns structured ErrorResponse matching OpenAPI spec.
# ─────────────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = request.state.request_id if hasattr(request.state, "request_id") else str(uuid.uuid4())
    logger.exception("Unhandled exception [request_id=%s]", request_id)
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_server_error",
            "message": "An unexpected error occurred.",
            "request_id": request_id,
        },
    )


# ─────────────────────────────────────────────
# Routers
# All routes are prefixed /api/v1
# ─────────────────────────────────────────────
API_PREFIX = "/api/v1"

app.include_router(health.router, prefix=API_PREFIX, tags=["health"])
app.include_router(auth.router, prefix=API_PREFIX, tags=["auth"])
app.include_router(catalog.router, prefix=API_PREFIX, tags=["catalog"])
app.include_router(deploy.router, prefix=API_PREFIX, tags=["deploy"])
app.include_router(blast_radius.router, prefix=API_PREFIX, tags=["blast-radius"])
app.include_router(scaffold.router, prefix=API_PREFIX, tags=["scaffold"])
app.include_router(iac.router, prefix=API_PREFIX, tags=["iac"])
app.include_router(pipelines.router, prefix=API_PREFIX, tags=["pipelines"])
app.include_router(observability.router, prefix=API_PREFIX, tags=["observability"])
app.include_router(cost.router, prefix=API_PREFIX, tags=["cost"])
app.include_router(security.router, prefix=API_PREFIX, tags=["security"])
app.include_router(fleet.router, prefix=API_PREFIX, tags=["fleet"])
app.include_router(runbooks.router, prefix=API_PREFIX, tags=["runbooks"])
app.include_router(ai_copilot.router, prefix=API_PREFIX, tags=["ai"])
app.include_router(docs.router, prefix=API_PREFIX, tags=["docs"])
app.include_router(chaos.router, prefix=API_PREFIX, tags=["chaos"])
app.include_router(teams.router, prefix=API_PREFIX, tags=["teams"])
app.include_router(audit.router, prefix=API_PREFIX, tags=["audit"])
app.include_router(policies.router, prefix=API_PREFIX, tags=["policies"])
