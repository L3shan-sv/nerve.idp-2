"""
Nerve IDP — Golden Path Enforcer

The deploy gate. Every POST /deploy goes through here.

Flow:
  1. Check deploy_frozen flag — return 423 if frozen
  2. Check dependency health risk score
  3. Run OPA evaluation (6 checks in parallel via single OPA query)
  4. If score < 80 → return 403 with structured failure report
  5. If Critical CVE → return 403 regardless of score
  6. If score >= 80 → annotate manifest with compliance annotation, queue deploy

OPA startup gate:
  The enforcer MUST NOT accept traffic until OPA sidecar is ready.
  Startup probe checks OPA /health before pod joins load balancer.
  See main.py lifespan for the wait_for_opa() call.

Two-layer enforcement (see ADR-003):
  Layer 1: This service (API-level, 403 Forbidden)
  Layer 2: OPA Gatekeeper (Kubernetes admission webhook)
"""

import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db, async_session_maker
from app.core.opa import evaluate_compliance, OpaEvaluationResult

logger = logging.getLogger(__name__)


async def wait_for_opa(max_retries: int = 30, delay: float = 1.0) -> None:
    """
    Block startup until OPA sidecar is healthy.
    Prevents fail-open (allowing deploys without policy evaluation).
    """
    import asyncio
    async with httpx.AsyncClient() as client:
        for attempt in range(max_retries):
            try:
                r = await client.get(f"{settings.OPA_URL}/health", timeout=2.0)
                if r.status_code == 200:
                    logger.info("OPA sidecar ready")
                    return
            except (httpx.ConnectError, httpx.TimeoutException):
                pass
            logger.warning("OPA not ready (%d/%d)", attempt + 1, max_retries)
            await asyncio.sleep(delay)
    raise RuntimeError(
        "OPA sidecar not ready after startup attempts. "
        "Refusing to start — this prevents fail-open on policy checks."
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.ENVIRONMENT != "test":
        await wait_for_opa()
    yield


app = FastAPI(
    title="Nerve Golden Path Enforcer",
    version=settings.APP_VERSION,
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ─────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────
class DeployRequest(BaseModel):
    service_id: uuid.UUID
    version: str
    environment: str
    actor: str
    notes: Optional[str] = None
    skip_blast_radius: bool = False


class ComplianceCheck(BaseModel):
    name: str
    status: str  # pass | warn | fail
    score: int
    weight: int
    detail: str
    fix_url: Optional[str] = None


class ComplianceReport(BaseModel):
    service_id: uuid.UUID
    version: str
    score: int
    passed: bool
    checks: list[ComplianceCheck]
    evaluated_at: datetime


class DeployResponse(BaseModel):
    deploy_id: uuid.UUID
    status: str
    compliance_score: int
    compliance_annotation: str  # Applied to k8s manifest


class DeployFrozenResponse(BaseModel):
    frozen: bool
    reason: str
    frozen_at: Optional[datetime]
    budget_consumed: float
    unfreeze_requires_role: str = "sre"


# ─────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────
@app.post("/internal/deploy", status_code=status.HTTP_202_ACCEPTED)
async def submit_deploy(
    payload: DeployRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Internal deploy endpoint called by the gateway.
    Not exposed directly — goes through the gateway's /services/{id}/deploy.
    """
    # ── Step 1: Check deploy freeze ───────────
    from app.models.models import Service
    service = await db.scalar(
        select(Service).where(
            Service.id == payload.service_id,
            Service.deleted_at.is_(None),
        )
    )
    if not service:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Service not found."})

    if service.deploy_frozen:
        return DeployFrozenResponse(
            frozen=True,
            reason=service.frozen_reason or "Error budget exhausted",
            frozen_at=service.frozen_at,
            budget_consumed=float(service.error_budget_consumed),
        )

    # ── Step 2: OPA evaluation ─────────────────
    opa_result: OpaEvaluationResult = await evaluate_compliance(
        service_id=str(payload.service_id),
        service_name=service.name,
        version=payload.version,
        environment=payload.environment,
    )

    # Build compliance report
    checks = [
        ComplianceCheck(
            name=check["name"],
            status=check["status"],
            score=check["score"],
            weight=check["weight"],
            detail=check["detail"],
            fix_url=check.get("fix_url"),
        )
        for check in opa_result.checks
    ]

    # Write compliance result to DB
    from app.models.models import ComplianceCheck as ComplianceCheckModel, DeployHistory
    deploy_record = DeployHistory(
        service_id=payload.service_id,
        version=payload.version,
        environment=payload.environment,
        status="blocked" if not opa_result.passed else "queued",
        compliance_score=opa_result.score,
        actor=payload.actor,
        notes=payload.notes,
    )
    db.add(deploy_record)
    await db.flush()

    for check in opa_result.checks:
        db.add(ComplianceCheckModel(
            deploy_id=deploy_record.id,
            service_id=payload.service_id,
            check_name=check["name"],
            status=check["status"],
            score=check["score"],
            weight=check["weight"],
            detail=check["detail"],
            fix_url=check.get("fix_url"),
        ))

    # Update service compliance score
    await db.execute(
        update(Service)
        .where(Service.id == payload.service_id)
        .values(compliance_score=opa_result.score)
    )

    await db.commit()

    # ── Step 3: Block if not passing ──────────
    if not opa_result.passed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "score": opa_result.score,
                "passed": False,
                "message": f"Compliance score {opa_result.score}/100 is below the required threshold of 80. "
                           f"Fix the failing checks before deploying to {payload.environment}.",
                "checks": [c.model_dump() for c in checks],
            },
        )

    # ── Step 4: Generate compliance annotation ─
    annotation = (
        f"nerve.io/compliance-score={opa_result.score},"
        f"nerve.io/compliance-passed=true,"
        f"nerve.io/enforced-at={datetime.now(timezone.utc).isoformat()}"
    )

    logger.info(
        "Deploy approved: service=%s version=%s env=%s score=%d",
        service.name, payload.version, payload.environment, opa_result.score,
    )

    return DeployResponse(
        deploy_id=deploy_record.id,
        status="queued",
        compliance_score=opa_result.score,
        compliance_annotation=annotation,
    )


@app.post("/internal/compliance/evaluate")
async def evaluate_only(
    service_id: uuid.UUID,
    version: str,
    environment: str = "production",
    db: AsyncSession = Depends(get_db),
):
    """Run compliance check without deploying — used by the portal preview."""
    from app.models.models import Service
    service = await db.scalar(
        select(Service).where(Service.id == service_id, Service.deleted_at.is_(None))
    )
    if not service:
        raise HTTPException(status_code=404, detail={"error": "not_found"})

    opa_result = await evaluate_compliance(
        service_id=str(service_id),
        service_name=service.name,
        version=version,
        environment=environment,
    )

    return ComplianceReport(
        service_id=service_id,
        version=version,
        score=opa_result.score,
        passed=opa_result.passed,
        checks=[ComplianceCheck(**c) for c in opa_result.checks],
        evaluated_at=datetime.now(timezone.utc),
    )


@app.post("/internal/freeze/{service_id}")
async def freeze_service(
    service_id: uuid.UUID,
    reason: str,
    burn_rate: float,
    idempotency_key: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Called by Alertmanager when burn rate crosses critical threshold.

    IDEMPOTENCY: Uses UPDATE ... WHERE deploy_frozen = FALSE RETURNING id
    Only the first call succeeds and publishes the freeze event.
    Subsequent calls (from simultaneous multi-window alerts) see no row returned
    and are no-ops — preventing duplicate events in Redis Streams.
    """
    from sqlalchemy import text
    result = await db.execute(
        text("""
            UPDATE services
            SET deploy_frozen = TRUE,
                frozen_at = NOW(),
                frozen_reason = :reason
            WHERE id = :service_id
              AND deploy_frozen = FALSE
              AND deleted_at IS NULL
            RETURNING id
        """),
        {"service_id": str(service_id), "reason": reason},
    )
    updated_id = result.scalar()

    if updated_id:
        # Only publish if this was the first freeze (not a duplicate)
        from app.core.events import publish_catalog_event
        await publish_catalog_event("service.deploy_frozen", {
            "service_id": str(service_id),
            "reason": reason,
            "burn_rate": burn_rate,
            "idempotency_key": idempotency_key,
        })
        logger.warning("Service deploy frozen: %s (burn_rate=%.2fx)", service_id, burn_rate)
        await db.commit()
        return {"frozen": True, "already_frozen": False, "frozen_at": datetime.now(timezone.utc)}
    else:
        # Already frozen — idempotent success
        return {"frozen": True, "already_frozen": True}
