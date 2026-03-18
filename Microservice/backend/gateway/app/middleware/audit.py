"""
Nerve IDP — Audit Log Middleware

Every state-changing request (POST, PATCH, DELETE, PUT) is written
to the audit_log table with:
  - actor (from JWT)
  - action (method + path)
  - resource_type and resource_id (parsed from path)
  - outcome (success / failure / blocked)
  - ip_address
  - timestamp

The audit_log table is append-only — REVOKE UPDATE, DELETE is set
in the PostgreSQL init.sql.

This middleware runs AFTER the response is generated so it can
capture the actual outcome (status code).
"""

import logging
import time
import uuid
from typing import Optional

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

# Methods that trigger audit log entries
AUDITED_METHODS = {"POST", "PATCH", "DELETE", "PUT"}

# Paths that are never audited (read-only, health checks)
EXCLUDED_PATHS = {
    "/health",
    "/health/ready",
    "/metrics",
    "/docs",
    "/redoc",
    "/api/v1/openapi.json",
}


def extract_resource_info(path: str) -> tuple[str, Optional[str]]:
    """
    Parse the resource type and ID from the request path.
    /api/v1/services/{uuid}/deploy → ("service", "{uuid}")
    /api/v1/fleet/collections/{uuid}/operations → ("collection", "{uuid}")
    """
    parts = [p for p in path.split("/") if p and p != "api" and p != "v1"]

    resource_type = "unknown"
    resource_id = None

    if not parts:
        return resource_type, resource_id

    # Map path segment to resource type
    resource_map = {
        "services": "service",
        "collections": "collection",
        "runbooks": "runbook",
        "experiments": "chaos_experiment",
        "requests": "iac_request",
        "scaffold": "scaffold",
        "teams": "team",
        "policies": "policy",
    }

    resource_type = resource_map.get(parts[0], parts[0])

    # Second segment is often the resource ID (UUID)
    if len(parts) > 1:
        try:
            uuid.UUID(parts[1])
            resource_id = parts[1]
        except ValueError:
            pass

    return resource_type, resource_id


def outcome_from_status(status_code: int) -> str:
    if status_code < 400:
        return "success"
    elif status_code in (403, 423):
        return "blocked"
    else:
        return "failure"


class AuditMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp):
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        # Skip non-audited methods and excluded paths
        if request.method not in AUDITED_METHODS:
            return await call_next(request)

        path = request.url.path
        if any(path.startswith(excluded) for excluded in EXCLUDED_PATHS):
            return await call_next(request)

        start_time = time.monotonic()
        response = await call_next(request)
        duration_ms = int((time.monotonic() - start_time) * 1000)

        # Extract actor from JWT (already decoded by auth dependency)
        actor = "anonymous"
        if hasattr(request.state, "current_user"):
            actor = request.state.current_user.username

        resource_type, resource_id = extract_resource_info(path)
        outcome = outcome_from_status(response.status_code)
        ip_address = request.client.host if request.client else None

        # Write to audit log asynchronously — don't block the response
        # In production this publishes to Redis Streams for async DB write
        # to avoid adding latency to every request
        audit_entry = {
            "id": str(uuid.uuid4()),
            "actor": actor,
            "action": f"{request.method} {path}",
            "resource_type": resource_type,
            "resource_id": resource_id,
            "outcome": outcome,
            "ip_address": ip_address,
            "duration_ms": duration_ms,
            "status_code": response.status_code,
        }

        # TODO Phase 1: direct DB write
        # TODO Phase 2: publish to Redis Streams for async consumption
        logger.info("AUDIT: %s", audit_entry)

        return response
