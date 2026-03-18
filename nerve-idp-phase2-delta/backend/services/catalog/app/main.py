"""
Nerve IDP — Catalog Service

Source of truth for every service on the platform.

Responsibilities:
- CRUD for service catalog entries
- SLO definition management
- Service changelog (GitHub release notes)
- Publishes change events to Redis Streams for downstream consumers
  (maturity scorer, Neo4j sync, cost intelligence)
- Triggers Neo4j node sync on every create/update/delete

Critical correctness requirements:
- Every write publishes to Redis Streams with XADD
- Consumer groups must be created with MKSTREAM to avoid missing early events
- Soft delete (deleted_at) — never hard delete services
- Row-level security: developers can only see services from their own team
  unless they have sre/platform_engineer role
"""

from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.routers import services, slo, changelog, dependencies
from app.core.config import settings
from app.core.database import engine
from app.core.events import init_redis_streams
from app.core.neo4j import init_neo4j

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Catalog service starting")

    # Initialize Redis Streams consumer groups
    # CRITICAL: Use MKSTREAM so groups exist before first event
    await init_redis_streams()

    # Verify Neo4j connectivity
    await init_neo4j()

    yield
    logger.info("Catalog service shutting down")


app = FastAPI(
    title="Nerve Catalog Service",
    version=settings.APP_VERSION,
    lifespan=lifespan,
    docs_url="/docs",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Gateway handles CORS externally
    allow_methods=["*"],
    allow_headers=["*"],
)

API_PREFIX = "/api/v1"
app.include_router(services.router, prefix=API_PREFIX)
app.include_router(slo.router, prefix=API_PREFIX)
app.include_router(changelog.router, prefix=API_PREFIX)
app.include_router(dependencies.router, prefix=API_PREFIX)
