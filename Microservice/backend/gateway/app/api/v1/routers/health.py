"""
Nerve IDP — Health endpoints

GET /health       → liveness (is the process alive?)
GET /health/ready → readiness (are all dependencies healthy?)

The readiness check is used by:
  - Kubernetes readiness probe (pod removed from load balancer if unhealthy)
  - ArgoCD health check
  - Phase 1 frontend connection validation
"""

import asyncio
import time
import logging
from typing import Optional

import httpx
import redis.asyncio as aioredis
from fastapi import APIRouter
from neo4j import AsyncGraphDatabase

from app.core.config import settings
from app.core.database import check_db_health

logger = logging.getLogger(__name__)

router = APIRouter()


async def check_redis_health() -> dict:
    start = time.monotonic()
    try:
        client = aioredis.from_url(settings.REDIS_URL, socket_timeout=2)
        await client.ping()
        await client.aclose()
        return {"healthy": True, "latency_ms": int((time.monotonic() - start) * 1000)}
    except Exception as exc:
        return {"healthy": False, "latency_ms": -1, "error": str(exc)}


async def check_neo4j_health() -> dict:
    start = time.monotonic()
    try:
        driver = AsyncGraphDatabase.driver(
            settings.NEO4J_URI,
            auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
        )
        async with driver.session() as session:
            await session.run("RETURN 1")
        await driver.close()
        return {"healthy": True, "latency_ms": int((time.monotonic() - start) * 1000)}
    except Exception as exc:
        return {"healthy": False, "latency_ms": -1, "error": str(exc)}


async def check_temporal_health() -> dict:
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(f"http://{settings.TEMPORAL_HOST}:7233/health")
            healthy = response.status_code == 200
        return {"healthy": healthy, "latency_ms": int((time.monotonic() - start) * 1000)}
    except Exception as exc:
        return {"healthy": False, "latency_ms": -1, "error": str(exc)}


async def check_vault_health() -> dict:
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(f"{settings.VAULT_URL}/v1/sys/health")
            # Vault returns 200 (active) or 429 (standby — still healthy for our purposes)
            healthy = response.status_code in (200, 429)
        return {"healthy": healthy, "latency_ms": int((time.monotonic() - start) * 1000)}
    except Exception as exc:
        return {"healthy": False, "latency_ms": -1, "error": str(exc)}


@router.get("/health", include_in_schema=True)
async def liveness():
    """
    Liveness probe — is the process alive?
    Returns 200 as long as the process is running.
    Never checks dependencies — that's readiness's job.
    """
    return {
        "status": "ok",
        "version": settings.APP_VERSION,
        "uptime_seconds": 0,  # TODO: track actual uptime
    }


@router.get("/health/ready", include_in_schema=True)
async def readiness():
    """
    Readiness probe — are all dependencies healthy?
    Runs all dependency checks concurrently.
    Returns 503 if any dependency is unhealthy.
    """
    # Run all checks concurrently — don't check sequentially
    db_check, redis_check, neo4j_check, temporal_check, vault_check = await asyncio.gather(
        check_db_health(),
        check_redis_health(),
        check_neo4j_health(),
        check_temporal_health(),
        check_vault_health(),
        return_exceptions=True,
    )

    # Handle exceptions from gather (if a check itself throws)
    def normalize(result) -> dict:
        if isinstance(result, Exception):
            return {"healthy": False, "latency_ms": -1, "error": str(result)}
        return result

    checks = {
        "postgres": normalize(db_check),
        "redis": normalize(redis_check),
        "neo4j": normalize(neo4j_check),
        "temporal": normalize(temporal_check),
        "vault": normalize(vault_check),
    }

    all_healthy = all(c.get("healthy", False) for c in checks.values())

    unhealthy = [name for name, check in checks.items() if not check.get("healthy")]
    if unhealthy:
        logger.warning("Readiness check failed — unhealthy dependencies: %s", unhealthy)

    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=200 if all_healthy else 503,
        content={
            "ready": all_healthy,
            "checks": checks,
        },
    )
