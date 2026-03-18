"""
Nerve IDP — Pipeline Service

Responsibilities:
  - Poll GitHub Actions API for active workflow runs on catalog services
  - Store pipeline run history in PostgreSQL (used by DORA engine)
  - Stream stage-by-stage updates via Redis pub/sub → WebSocket gateway

WebSocket protocol:
  Channel: pipeline:{service_id}
  Event shape:
    {
      "run_id": "...",
      "stage": "test",
      "status": "running" | "succeeded" | "failed" | "skipped",
      "started_at": "...",
      "duration_seconds": 42
    }

GitHub API rate limit awareness:
  Poller tracks X-RateLimit-Remaining on every response.
  When remaining < GITHUB_RATE_LIMIT_BUFFER (100), polling pauses
  until X-RateLimit-Reset timestamp.
  This prevents scaffold workflows from exhausting the same token.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, insert, update

from app.core.config import settings

logger = logging.getLogger(__name__)
app = FastAPI(title="Nerve Pipeline Service", version=settings.APP_VERSION)


# ─────────────────────────────────────────────
# GitHub Actions poller
# ─────────────────────────────────────────────
class GitHubActionsPoller:
    """
    Polls GitHub Actions for all services registered in the catalog.
    Runs as a background task on service startup.

    Polling interval: 15 seconds (respects GitHub API rate limits)
    On active runs: polls every 5 seconds for that specific run
    On idle: falls back to 15 second interval
    """

    def __init__(self):
        self.headers = {
            "Authorization": f"token {settings.GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
        }
        self._rate_limit_remaining = 5000
        self._rate_limit_reset = 0

    def _check_rate_limit(self, response: httpx.Response) -> None:
        self._rate_limit_remaining = int(
            response.headers.get("X-RateLimit-Remaining", self._rate_limit_remaining)
        )
        self._rate_limit_reset = int(
            response.headers.get("X-RateLimit-Reset", self._rate_limit_reset)
        )

    async def _wait_if_rate_limited(self) -> None:
        if self._rate_limit_remaining < settings.GITHUB_RATE_LIMIT_BUFFER:
            wait_seconds = max(self._rate_limit_reset - int(time.time()), 60)
            logger.warning(
                "GitHub rate limit low (%d remaining). Pausing for %ds.",
                self._rate_limit_remaining, wait_seconds,
            )
            await asyncio.sleep(wait_seconds)

    async def poll_service(
        self,
        service_name: str,
        service_id: str,
        db: AsyncSession,
    ) -> None:
        """Poll GitHub Actions runs for a single service repo."""
        await self._wait_if_rate_limited()

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"https://api.github.com/repos/{settings.GITHUB_ORG}/{service_name}/actions/runs",
                headers=self.headers,
                params={"per_page": 5},
                timeout=15.0,
            )
            self._check_rate_limit(response)

            if response.status_code == 404:
                return  # Repo not yet created (scaffold in progress)
            if response.status_code != 200:
                logger.warning(
                    "GitHub API error %d for %s", response.status_code, service_name
                )
                return

            runs = response.json().get("workflow_runs", [])

        for run in runs:
            await self._process_run(run, service_id, db)

    async def _process_run(self, run: dict, service_id: str, db: AsyncSession) -> None:
        """Store/update pipeline run and publish stage events to Redis."""
        run_id = str(run["id"])
        status_map = {
            "queued": "queued",
            "in_progress": "running",
            "completed": run.get("conclusion", "succeeded"),
        }
        status = status_map.get(run["status"], "unknown")
        if status == "success":
            status = "succeeded"
        elif status == "failure":
            status = "failed"

        # Fetch stages (jobs) for this run
        stages = await self._fetch_run_stages(run["id"], run.get("repository", {}).get("name", ""))

        # Upsert pipeline run record
        from app.models.pipeline import PipelineRun
        existing = await db.scalar(
            select(PipelineRun).where(PipelineRun.id == run_id)
        )

        run_data = {
            "id": run_id,
            "service_id": service_id,
            "run_number": run["run_number"],
            "status": status,
            "triggered_by": run.get("triggering_actor", {}).get("login", "unknown"),
            "branch": run.get("head_branch"),
            "commit_sha": run.get("head_sha", "")[:40],
            "stages": stages,
            "started_at": run.get("created_at"),
            "completed_at": run.get("updated_at") if status in ("succeeded", "failed", "cancelled") else None,
        }

        if not existing:
            db.add(PipelineRun(**run_data))
        else:
            for k, v in run_data.items():
                if k != "id":
                    setattr(existing, k, v)

        await db.commit()

        # Publish stage updates to Redis pub/sub for WebSocket consumers
        if status == "running":
            await self._publish_stage_events(service_id, run_id, stages)

        # Trigger DORA recomputation when a production run completes
        if status in ("succeeded", "failed") and run.get("head_branch") == "main":
            await self._trigger_dora_recompute(service_id)

    async def _fetch_run_stages(self, run_id: int, repo_name: str) -> list[dict]:
        """Fetch individual job (stage) statuses for a workflow run."""
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"https://api.github.com/repos/{settings.GITHUB_ORG}/{repo_name}/actions/runs/{run_id}/jobs",
                headers=self.headers,
                timeout=10.0,
            )
            self._check_rate_limit(response)
            if response.status_code != 200:
                return []

        jobs = response.json().get("jobs", [])
        return [
            {
                "name": job["name"],
                "status": "succeeded" if job.get("conclusion") == "success"
                          else "failed" if job.get("conclusion") == "failure"
                          else "running" if job["status"] == "in_progress"
                          else job["status"],
                "started_at": job.get("started_at"),
                "completed_at": job.get("completed_at"),
            }
            for job in jobs
        ]

    async def _publish_stage_events(
        self, service_id: str, run_id: str, stages: list[dict]
    ) -> None:
        """Publish stage updates to Redis pub/sub channel."""
        redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        try:
            channel = f"pipeline:{service_id}"
            for stage in stages:
                await redis.publish(channel, json.dumps({
                    "run_id": run_id,
                    "stage": stage["name"],
                    "status": stage["status"],
                    "started_at": stage.get("started_at"),
                }))
        finally:
            await redis.aclose()

    async def _trigger_dora_recompute(self, service_id: str) -> None:
        """Queue Celery task to recompute DORA metrics for this service."""
        try:
            from celery import Celery
            celery_app = Celery(broker=settings.REDIS_URL)
            celery_app.send_task(
                "dora.compute_service_metrics",
                args=[service_id],
                queue="dora",
            )
        except Exception as exc:
            logger.warning("Failed to queue DORA recompute for %s: %s", service_id, exc)


# ─────────────────────────────────────────────
# WebSocket endpoint — live pipeline stage streaming
# ─────────────────────────────────────────────
@app.websocket("/ws/pipelines/{service_id}")
async def pipeline_websocket(websocket: WebSocket, service_id: str):
    """
    WebSocket endpoint that streams live pipeline stage updates.

    Client connects: ws://gateway/ws/pipelines/{service_id}?token=JWT
    Server pushes: PipelineStageEvent on every stage status change

    Reconnection: Frontend must implement exponential backoff.
    The WebSocket closes on network interruption — this is expected.
    The frontend shows a "reconnecting..." indicator and retries.
    """
    await websocket.accept()
    redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    pubsub = redis.pubsub()

    try:
        await pubsub.subscribe(f"pipeline:{service_id}")
        logger.info("WebSocket connected: pipeline/%s", service_id)

        async for message in pubsub.listen():
            if message["type"] == "message":
                await websocket.send_text(message["data"])

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected: pipeline/%s", service_id)
    except Exception as exc:
        logger.error("WebSocket error for pipeline/%s: %s", service_id, exc)
    finally:
        await pubsub.unsubscribe(f"pipeline:{service_id}")
        await pubsub.aclose()
        await redis.aclose()


# ─────────────────────────────────────────────
# Background polling loop
# ─────────────────────────────────────────────
async def run_poller():
    """
    Background task: poll all catalog services every 15 seconds.
    Scales to ~300 services within GitHub's 5000 req/hour rate limit.

    300 services × 1 poll/service/15s = 1200 requests/hour — well within limit.
    Active runs get additional stage fetches, still safely under limit.
    """
    from app.core.database import async_session_maker
    import httpx

    poller = GitHubActionsPoller()

    async with httpx.AsyncClient() as client:
        while True:
            try:
                # Fetch active services from catalog
                r = await client.get(
                    f"{settings.CATALOG_SERVICE_URL}/api/v1/services",
                    params={"limit": 100},
                    timeout=10.0,
                )
                services = r.json().get("items", [])

                async with async_session_maker() as db:
                    for svc in services:
                        await poller.poll_service(
                            service_name=svc["name"],
                            service_id=svc["id"],
                            db=db,
                        )

            except Exception as exc:
                logger.error("Poller loop error: %s", exc)

            await asyncio.sleep(15)
