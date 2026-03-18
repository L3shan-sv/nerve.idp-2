"""
Nerve IDP — Database setup

Connection architecture:
  App → PgBouncer (transaction mode, port 6432) → PostgreSQL primary
  Reads → PgBouncer → PostgreSQL read replica (when available)

CRITICAL: Always connect through PgBouncer.
Direct PostgreSQL connections bypass pooling and hit the
max_connections wall at ~150 concurrent users.

Alembic migrations connect DIRECTLY to PostgreSQL (not PgBouncer)
because DDL transactions are incompatible with PgBouncer transaction mode.
"""

import logging
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from app.core.config import settings

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Primary engine — through PgBouncer
# Used for all writes and reads without replicas
# ─────────────────────────────────────────────
engine = create_async_engine(
    settings.DATABASE_URL,
    # NullPool is correct here — PgBouncer manages the actual pool.
    # Using SQLAlchemy's pool ON TOP of PgBouncer creates a double-pooling
    # problem where connections are held open on both sides.
    pool_class=NullPool,
    echo=settings.DEBUG,
    # Connection health checks
    pool_pre_ping=True,
)

async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


# ─────────────────────────────────────────────
# Base class for all SQLAlchemy models
# ─────────────────────────────────────────────
class Base(DeclarativeBase):
    pass


# ─────────────────────────────────────────────
# Dependency injection — FastAPI route dependency
# ─────────────────────────────────────────────
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency that provides an AsyncSession.
    Session is committed on success, rolled back on exception.

    Usage in routes:
        @router.get("/services")
        async def list_services(db: AsyncSession = Depends(get_db)):
            ...
    """
    async with async_session_maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def check_db_health() -> dict:
    """Used by /health/ready endpoint to verify DB connectivity."""
    import time
    start = time.monotonic()
    try:
        async with async_session_maker() as session:
            await session.execute("SELECT 1")
        latency_ms = int((time.monotonic() - start) * 1000)
        return {"healthy": True, "latency_ms": latency_ms}
    except Exception as exc:
        logger.error("Database health check failed: %s", exc)
        return {"healthy": False, "latency_ms": -1, "error": str(exc)}
