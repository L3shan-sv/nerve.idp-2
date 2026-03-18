"""
Catalog service — Redis Streams event publishing

Stream: catalog.events
Consumer groups:
  - maturity-scorer       (maturity scoring engine)
  - neo4j-sync            (Neo4j reconciliation worker)
  - cost-intelligence     (cost polling worker)
  - dora-metrics          (DORA computation worker)

CRITICAL: Consumer groups must be initialized with XGROUP CREATE MKSTREAM
so they exist BEFORE the first event is published. If a group is created
after events have been published, those events are invisible to the group.

Each consumer uses XREADGROUP with explicit XACK after processing.
Failed messages go to the PEL (Pending Entry List) and are
reclaimed after a timeout by a dedicated recovery worker.
"""

import json
import logging
import time
from typing import Any

import redis.asyncio as aioredis

from app.core.config import settings

logger = logging.getLogger(__name__)

CATALOG_STREAM = "catalog.events"

# All consumer groups that read from catalog.events
CONSUMER_GROUPS = [
    "maturity-scorer",
    "neo4j-sync",
    "cost-intelligence",
    "dora-metrics",
]


async def get_redis() -> aioredis.Redis:
    return aioredis.from_url(
        settings.REDIS_URL,
        encoding="utf-8",
        decode_responses=True,
        socket_timeout=5,
        socket_connect_timeout=5,
    )


async def init_redis_streams() -> None:
    """
    Initialize all consumer groups with XGROUP CREATE MKSTREAM.

    MKSTREAM creates the stream if it doesn't exist.
    $ means the group starts reading from new messages (not historical).

    This runs on every service startup — XGROUP CREATE is idempotent
    if the group already exists (raises BUSYGROUP which we catch).
    """
    redis = await get_redis()
    try:
        for group in CONSUMER_GROUPS:
            try:
                await redis.xgroup_create(
                    name=CATALOG_STREAM,
                    groupname=group,
                    id="$",
                    mkstream=True,
                )
                logger.info("Created consumer group: %s on %s", group, CATALOG_STREAM)
            except aioredis.ResponseError as e:
                if "BUSYGROUP" in str(e):
                    # Group already exists — expected on restart
                    logger.debug("Consumer group already exists: %s", group)
                else:
                    raise
    finally:
        await redis.aclose()


async def publish_catalog_event(event_type: str, payload: dict[str, Any]) -> str:
    """
    Publish a catalog change event to Redis Streams.

    Returns the stream entry ID.

    Event schema:
      type: str         — event type (service.created, service.updated, service.deleted)
      payload: str      — JSON-encoded payload
      timestamp: str    — Unix timestamp (ms)
      version: str      — Schema version for consumers to handle migrations

    Uses XADD with MAXLEN to cap stream size at 10,000 entries.
    Older entries are trimmed automatically.
    """
    redis = await get_redis()
    try:
        entry_id = await redis.xadd(
            name=CATALOG_STREAM,
            fields={
                "type": event_type,
                "payload": json.dumps(payload),
                "timestamp": str(int(time.time() * 1000)),
                "version": "1",
            },
            maxlen=10_000,
            approximate=True,
        )
        logger.debug("Published %s → %s [%s]", event_type, CATALOG_STREAM, entry_id)
        return entry_id
    except Exception as exc:
        # Log but don't fail the request — event publishing is best-effort
        # The Neo4j reconciliation job will catch any missed syncs
        logger.error("Failed to publish catalog event %s: %s", event_type, exc)
        return ""
    finally:
        await redis.aclose()
