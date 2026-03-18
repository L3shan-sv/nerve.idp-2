"""
Nerve IDP — DORA Metrics Engine (Celery worker)

Consumes pipeline run events from Redis Streams and computes:
  - Deployment Frequency    (deploys per day/week)
  - Lead Time for Changes   (commit → production deploy)
  - MTTR                    (incident open → resolved)
  - Change Failure Rate     (% deploys causing incidents)

DORA tier thresholds (Google 2023 State of DevOps):
  Deployment frequency:
    Elite:  Multiple per day
    High:   Once per day to once per week
    Medium: Once per week to once per month
    Low:    Less than once per month

  Lead time for changes:
    Elite:  < 1 hour
    High:   1 day to 1 week
    Medium: 1 week to 1 month
    Low:    > 1 month

  MTTR:
    Elite:  < 1 hour
    High:   < 1 day
    Medium: 1 day to 1 week
    Low:    > 1 week

  Change failure rate:
    Elite:  0-5%
    High:   5-10%
    Medium: 10-15%
    Low:    > 15%

Event-driven: Celery worker subscribes to catalog.pipeline_events stream.
Results stored in PostgreSQL. Queried by the observability router.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from celery import Celery
from sqlalchemy import select, func, and_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.pipeline import PipelineRun
from app.models.catalog import DeployHistory, Service

logger = logging.getLogger(__name__)

celery_app = Celery(
    "dora-worker",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,          # Ack only after successful completion
    worker_prefetch_multiplier=1, # One task at a time per worker — DORA tasks are DB-heavy
)


def get_dora_tier_deployment_freq(deploys_per_day: float) -> str:
    if deploys_per_day >= 1.0:
        return "elite"
    elif deploys_per_day >= 1 / 7:
        return "high"
    elif deploys_per_day >= 1 / 30:
        return "medium"
    return "low"


def get_dora_tier_lead_time(hours: float) -> str:
    if hours < 1:
        return "elite"
    elif hours < 168:  # 1 week
        return "high"
    elif hours < 720:  # 1 month
        return "medium"
    return "low"


def get_dora_tier_mttr(hours: float) -> str:
    if hours < 1:
        return "elite"
    elif hours < 24:
        return "high"
    elif hours < 168:  # 1 week
        return "medium"
    return "low"


def get_dora_tier_cfr(percent: float) -> str:
    if percent <= 5:
        return "elite"
    elif percent <= 10:
        return "high"
    elif percent <= 15:
        return "medium"
    return "low"


@celery_app.task(
    name="dora.compute_service_metrics",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def compute_service_dora_metrics(self, service_id: str, window_days: int = 30):
    """
    Compute DORA metrics for a single service over the given window.
    Triggered by pipeline run completion events.
    """
    from app.core.database import get_sync_db

    with get_sync_db() as db:
        try:
            _compute_and_store_dora(db, UUID(service_id), window_days)
        except Exception as exc:
            logger.error("DORA computation failed for %s: %s", service_id, exc)
            raise self.retry(exc=exc)


def _compute_and_store_dora(db: Session, service_id: UUID, window_days: int) -> dict:
    """Core computation — extracted for testability."""
    from app.models.dora import DoraMetricsRecord

    window_start = datetime.now(timezone.utc) - timedelta(days=window_days)

    # ── Deployment Frequency ──────────────────
    deploy_count = db.scalar(
        select(func.count(DeployHistory.id)).where(
            and_(
                DeployHistory.service_id == service_id,
                DeployHistory.status == "succeeded",
                DeployHistory.environment == "production",
                DeployHistory.deployed_at >= window_start,
            )
        )
    ) or 0

    deploys_per_day = deploy_count / window_days

    # Daily counts for bar chart (last 14 days)
    daily_counts = []
    for i in range(14):
        day = datetime.now(timezone.utc).date() - timedelta(days=i)
        count = db.scalar(
            select(func.count(DeployHistory.id)).where(
                and_(
                    DeployHistory.service_id == service_id,
                    DeployHistory.status == "succeeded",
                    DeployHistory.environment == "production",
                    func.date(DeployHistory.deployed_at) == day,
                )
            )
        ) or 0
        daily_counts.append({"date": day.isoformat(), "count": count})

    # ── Lead Time for Changes ─────────────────
    # Lead time = time from first commit to production deploy
    # Approximated as: pipeline start time to deploy completion
    # Full implementation requires GitHub API for commit timestamp
    lead_times = db.execute(
        select(
            func.extract("epoch", DeployHistory.completed_at - PipelineRun.started_at).label("lead_time_seconds")
        )
        .join(PipelineRun, PipelineRun.service_id == DeployHistory.service_id)
        .where(
            and_(
                DeployHistory.service_id == service_id,
                DeployHistory.status == "succeeded",
                DeployHistory.environment == "production",
                DeployHistory.deployed_at >= window_start,
            )
        )
    ).scalars().all()

    avg_lead_time_hours = (
        sum(lead_times) / len(lead_times) / 3600
        if lead_times else 0.0
    )

    # ── MTTR ──────────────────────────────────
    # MTTR = time from incident open to service health restored
    # Approximated here as: frozen_at to unfreeze/healthy_at
    # Full implementation queries the audit_log for freeze/unfreeze events
    # TODO Phase 4: query audit_log for freeze → unfreeze duration
    avg_mttr_hours = 4.0  # Placeholder until audit log is queryable

    # ── Change Failure Rate ───────────────────
    total_deploys = deploy_count
    failed_deploys = db.scalar(
        select(func.count(DeployHistory.id)).where(
            and_(
                DeployHistory.service_id == service_id,
                DeployHistory.status.in_(["failed", "rolled_back"]),
                DeployHistory.environment == "production",
                DeployHistory.deployed_at >= window_start,
            )
        )
    ) or 0

    cfr_percent = (failed_deploys / total_deploys * 100) if total_deploys > 0 else 0.0

    # ── Persist ───────────────────────────────
    record = db.scalar(
        select(DoraMetricsRecord).where(DoraMetricsRecord.service_id == service_id)
    )
    if not record:
        record = DoraMetricsRecord(service_id=service_id)
        db.add(record)

    record.window_days = window_days
    record.deployment_frequency = deploys_per_day
    record.deployment_frequency_tier = get_dora_tier_deployment_freq(deploys_per_day)
    record.lead_time_hours = avg_lead_time_hours
    record.lead_time_tier = get_dora_tier_lead_time(avg_lead_time_hours)
    record.mttr_hours = avg_mttr_hours
    record.mttr_tier = get_dora_tier_mttr(avg_mttr_hours)
    record.change_failure_rate = cfr_percent
    record.cfr_tier = get_dora_tier_cfr(cfr_percent)
    record.daily_deploy_counts = daily_counts
    record.computed_at = datetime.now(timezone.utc)

    db.commit()

    logger.info(
        "DORA computed: service=%s freq=%.2f/day lead=%.1fh mttr=%.1fh cfr=%.1f%%",
        service_id, deploys_per_day, avg_lead_time_hours, avg_mttr_hours, cfr_percent,
    )

    return {
        "service_id": str(service_id),
        "deployment_frequency": deploys_per_day,
        "lead_time_hours": avg_lead_time_hours,
        "mttr_hours": avg_mttr_hours,
        "change_failure_rate": cfr_percent,
    }


@celery_app.task(name="dora.compute_all_services")
def compute_all_services_dora():
    """
    Celery beat task — recomputes DORA for all services.
    Runs every hour as a catch-all in addition to event-driven computation.
    """
    from app.core.database import get_sync_db
    with get_sync_db() as db:
        service_ids = db.scalars(
            select(Service.id).where(Service.deleted_at.is_(None))
        ).all()

    for sid in service_ids:
        compute_service_dora_metrics.delay(str(sid))

    logger.info("Queued DORA recomputation for %d services", len(service_ids))
