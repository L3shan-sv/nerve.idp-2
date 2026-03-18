"""
Catalog service — Neo4j sync

Keeps Neo4j in sync with PostgreSQL service catalog.

Neo4j is authoritative for graph traversal only.
PostgreSQL is authoritative for all service metadata.

Sync strategy:
  - On service create/update: MERGE node + SET properties
  - On service delete: DETACH DELETE node
  - On dependency create/delete: MERGE/DELETE relationship

Reconciliation job (runs every 5 min via Celery beat):
  1. Fetch all active service IDs + dependency edges from PostgreSQL
  2. Fetch all Service nodes + relationships from Neo4j
  3. Diff both sets
  4. Apply missing creates, remove phantom deletes
  5. Write sync result to neo4j_sync_log table

If drift is detected (> 10 services out of sync), an alert fires.
"""

import logging
from typing import Optional

from neo4j import AsyncGraphDatabase, AsyncDriver

from app.core.config import settings

logger = logging.getLogger(__name__)

_driver: Optional[AsyncDriver] = None


async def get_driver() -> AsyncDriver:
    global _driver
    if _driver is None:
        _driver = AsyncGraphDatabase.driver(
            settings.NEO4J_URI,
            auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
            max_connection_pool_size=settings.NEO4J_MAX_CONNECTION_POOL_SIZE,
            connection_timeout=settings.NEO4J_CONNECTION_TIMEOUT,
        )
    return _driver


async def init_neo4j() -> None:
    """Verify Neo4j connectivity on startup."""
    driver = await get_driver()
    async with driver.session(database=settings.NEO4J_DATABASE) as session:
        await session.run("RETURN 1")
    logger.info("Neo4j connection verified: %s", settings.NEO4J_URI)


async def sync_service_to_neo4j(service) -> None:
    """
    MERGE a Service node in Neo4j. Idempotent — safe to call on create or update.

    Uses MERGE on id so re-running never creates duplicates.
    SET overwrites all properties on every sync to keep Neo4j consistent
    with the PostgreSQL source of truth.
    """
    driver = await get_driver()
    try:
        async with driver.session(database=settings.NEO4J_DATABASE) as session:
            await session.run(
                """
                MERGE (s:Service {id: $id})
                SET s.name = $name,
                    s.team_id = $team_id,
                    s.language = $language,
                    s.health_status = $health_status,
                    s.updated_at = $updated_at
                """,
                id=str(service.id),
                name=service.name,
                team_id=str(service.team_id),
                language=service.language,
                health_status=service.health_status,
                updated_at=service.updated_at.isoformat() if service.updated_at else None,
            )
        logger.debug("Neo4j sync: MERGE Service %s (%s)", service.name, service.id)
    except Exception as exc:
        # Log but don't fail — reconciliation job will catch the drift
        logger.error("Neo4j sync failed for service %s: %s", service.id, exc)


async def delete_service_from_neo4j(service_id: str) -> None:
    """
    DETACH DELETE a Service node and all its relationships.
    Called on soft-delete.
    """
    driver = await get_driver()
    try:
        async with driver.session(database=settings.NEO4J_DATABASE) as session:
            await session.run(
                "MATCH (s:Service {id: $id}) DETACH DELETE s",
                id=service_id,
            )
        logger.debug("Neo4j: DETACH DELETE Service %s", service_id)
    except Exception as exc:
        logger.error("Neo4j delete failed for service %s: %s", service_id, exc)


async def sync_dependency_to_neo4j(
    source_id: str, target_id: str, relationship: str
) -> None:
    """Create a DEPENDS_ON or USES relationship between two Service nodes."""
    driver = await get_driver()
    try:
        async with driver.session(database=settings.NEO4J_DATABASE) as session:
            cypher = f"""
            MATCH (a:Service {{id: $source_id}})
            MATCH (b:Service {{id: $target_id}})
            MERGE (a)-[:{relationship}]->(b)
            """
            await session.run(cypher, source_id=source_id, target_id=target_id)
    except Exception as exc:
        logger.error("Neo4j dependency sync failed (%s → %s): %s", source_id, target_id, exc)


async def delete_dependency_from_neo4j(
    source_id: str, target_id: str, relationship: str
) -> None:
    driver = await get_driver()
    try:
        async with driver.session(database=settings.NEO4J_DATABASE) as session:
            cypher = f"""
            MATCH (a:Service {{id: $source_id}})-[r:{relationship}]->(b:Service {{id: $target_id}})
            DELETE r
            """
            await session.run(cypher, source_id=source_id, target_id=target_id)
    except Exception as exc:
        logger.error("Neo4j dependency delete failed: %s", exc)


async def reconcile_neo4j_with_postgres(db_session) -> dict:
    """
    Full reconciliation pass: PostgreSQL → Neo4j.

    Returns a summary dict logged to neo4j_sync_log.
    Called by Celery beat every 5 minutes.

    Algorithm:
    1. Get all active service IDs from PostgreSQL
    2. Get all Service node IDs from Neo4j
    3. Missing in Neo4j → create
    4. Extra in Neo4j (deleted in PG) → delete
    5. Repeat for dependency edges
    """
    from sqlalchemy import select, text
    from app.models.models import Service, ServiceDependency

    logger.info("Starting Neo4j reconciliation pass")
    drift_detected = False
    services_synced = 0
    edges_synced = 0
    drift_detail = {}

    driver = await get_driver()

    # Get PostgreSQL active services
    result = await db_session.execute(
        select(Service).where(Service.deleted_at.is_(None))
    )
    pg_services = {str(s.id): s for s in result.scalars().all()}

    # Get Neo4j service IDs
    async with driver.session(database=settings.NEO4J_DATABASE) as session:
        neo4j_result = await session.run("MATCH (s:Service) RETURN s.id AS id")
        neo4j_service_ids = {record["id"] async for record in neo4j_result}

    pg_ids = set(pg_services.keys())

    # Services in PG but not Neo4j → sync them
    missing_in_neo4j = pg_ids - neo4j_service_ids
    if missing_in_neo4j:
        drift_detected = True
        drift_detail["missing_in_neo4j"] = list(missing_in_neo4j)
        logger.warning("Neo4j drift: %d services missing", len(missing_in_neo4j))
        for sid in missing_in_neo4j:
            await sync_service_to_neo4j(pg_services[sid])
            services_synced += 1

    # Services in Neo4j but not PG (soft-deleted) → delete from Neo4j
    phantom_in_neo4j = neo4j_service_ids - pg_ids
    if phantom_in_neo4j:
        drift_detected = True
        drift_detail["phantom_in_neo4j"] = list(phantom_in_neo4j)
        logger.warning("Neo4j drift: %d phantom services", len(phantom_in_neo4j))
        for sid in phantom_in_neo4j:
            await delete_service_from_neo4j(sid)
            services_synced += 1

    # Reconcile edges
    dep_result = await db_session.execute(
        select(ServiceDependency)
    )
    pg_edges = {
        (str(d.source_id), str(d.target_id), d.relationship)
        for d in dep_result.scalars().all()
    }

    async with driver.session(database=settings.NEO4J_DATABASE) as session:
        edge_result = await session.run(
            "MATCH (a:Service)-[r]->(b:Service) RETURN a.id AS src, b.id AS tgt, type(r) AS rel"
        )
        neo4j_edges = {
            (record["src"], record["tgt"], record["rel"])
            async for record in edge_result
        }

    missing_edges = pg_edges - neo4j_edges
    if missing_edges:
        drift_detected = True
        drift_detail["missing_edges"] = len(missing_edges)
        for src, tgt, rel in missing_edges:
            await sync_dependency_to_neo4j(src, tgt, rel)
            edges_synced += 1

    phantom_edges = neo4j_edges - pg_edges
    if phantom_edges:
        drift_detected = True
        drift_detail["phantom_edges"] = len(phantom_edges)
        for src, tgt, rel in phantom_edges:
            await delete_dependency_from_neo4j(src, tgt, rel)
            edges_synced += 1

    result = {
        "drift_detected": drift_detected,
        "services_synced": services_synced,
        "edges_synced": edges_synced,
        "drift_detail": drift_detail,
    }

    if drift_detected:
        logger.warning("Neo4j reconciliation complete — drift found and corrected: %s", drift_detail)
    else:
        logger.debug("Neo4j reconciliation complete — no drift detected")

    return result
