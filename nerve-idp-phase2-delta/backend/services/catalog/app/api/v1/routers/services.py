"""
Catalog service — /services router

GET  /services            list with filters + pagination
POST /services            register new service
GET  /services/{id}       full service detail
PATCH /services/{id}      update metadata
DELETE /services/{id}     soft delete

Every write:
  1. Writes to PostgreSQL
  2. Publishes change event to Redis Streams (catalog.events)
  3. Triggers Neo4j sync for the affected node

Read queries use the read replica path when available.
"""

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.core.events import publish_catalog_event
from app.core.neo4j import sync_service_to_neo4j, delete_service_from_neo4j
from app.models.models import Service, Team, MaturityScore, SecurityPosture
from app.schemas.service import (
    ServiceResponse, ServiceDetailResponse, ServiceListResponse,
    ServiceRegistration, ServiceUpdate, CatalogSummary,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def service_is_active(service: Service) -> bool:
    return service.deleted_at is None


@router.get("/services", response_model=ServiceListResponse)
async def list_services(
    team: Optional[str] = Query(None),
    language: Optional[str] = Query(None),
    health: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    sort_by: str = Query("name"),
    sort_dir: str = Query("asc"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    # Base query — exclude soft-deleted
    query = (
        select(Service)
        .options(
            selectinload(Service.team),
            selectinload(Service.maturity),
            selectinload(Service.security),
        )
        .where(Service.deleted_at.is_(None))
    )

    # Filters
    if team:
        query = query.join(Team).where(Team.slug == team)
    if language:
        query = query.where(Service.language == language)
    if health:
        query = query.where(Service.health_status == health)
    if q:
        # Trigram search on name — uses gin_trgm_ops index
        query = query.where(Service.name.ilike(f"%{q}%"))

    # Sorting
    sort_col = getattr(Service, sort_by, Service.name)
    if sort_dir == "desc":
        query = query.order_by(sort_col.desc())
    else:
        query = query.order_by(sort_col.asc())

    # Count total (before pagination)
    count_query = select(func.count()).select_from(query.subquery())
    total = await db.scalar(count_query) or 0

    # Pagination
    query = query.offset((page - 1) * limit).limit(limit)
    result = await db.execute(query)
    services = result.scalars().all()

    # Catalog summary
    summary_result = await db.execute(
        select(
            func.count(Service.id).label("total"),
            func.sum((Service.health_status == "healthy").cast(int)).label("healthy"),
            func.sum((Service.health_status == "degraded").cast(int)).label("degraded"),
            func.sum((Service.deploy_frozen == True).cast(int)).label("frozen"),
            func.avg(Service.maturity_score).label("avg_maturity"),
        ).where(Service.deleted_at.is_(None))
    )
    summary_row = summary_result.first()

    # Count critical CVEs across all services
    cve_result = await db.execute(
        select(func.sum(SecurityPosture.critical_cves)).join(
            Service, Service.id == SecurityPosture.service_id
        ).where(Service.deleted_at.is_(None))
    )
    total_cves = cve_result.scalar() or 0

    summary = CatalogSummary(
        total_services=summary_row.total or 0,
        healthy=summary_row.healthy or 0,
        degraded=summary_row.degraded or 0,
        frozen=summary_row.frozen or 0,
        avg_maturity_score=float(summary_row.avg_maturity or 0),
        critical_cves=int(total_cves),
    )

    return ServiceListResponse(
        items=[ServiceResponse.model_validate(s) for s in services],
        total=total,
        page=page,
        limit=limit,
        summary=summary,
    )


@router.post("/services", response_model=ServiceResponse, status_code=status.HTTP_201_CREATED)
async def register_service(
    payload: ServiceRegistration,
    db: AsyncSession = Depends(get_db),
):
    # Check name uniqueness
    existing = await db.scalar(
        select(Service).where(Service.name == payload.name)
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "conflict", "message": f"Service '{payload.name}' already exists."},
        )

    # Resolve team
    team = await db.scalar(
        select(Team).where(Team.slug == payload.team)
    )
    if not team:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_team", "message": f"Team '{payload.team}' not found."},
        )

    service = Service(
        name=payload.name,
        team_id=team.id,
        language=payload.language,
        repo_url=str(payload.repo_url) if payload.repo_url else None,
        description=payload.description,
        health_status="unknown",
    )
    db.add(service)
    await db.flush()  # Get ID before relationships

    # Register upstream dependencies
    if payload.upstream_dependencies:
        from app.models.models import ServiceDependency
        for dep_id in payload.upstream_dependencies:
            dep = ServiceDependency(
                source_id=service.id,
                target_id=uuid.UUID(str(dep_id)),
                relationship="DEPENDS_ON",
            )
            db.add(dep)

    await db.commit()
    await db.refresh(service)

    # Publish to Redis Streams — downstream consumers will pick this up
    await publish_catalog_event("service.created", {
        "service_id": str(service.id),
        "name": service.name,
        "team_id": str(service.team_id),
        "language": service.language,
    })

    # Sync to Neo4j — creates Service node
    await sync_service_to_neo4j(service)

    logger.info("Service registered: %s (%s)", service.name, service.id)
    return ServiceResponse.model_validate(service)


@router.get("/services/{service_id}", response_model=ServiceDetailResponse)
async def get_service(
    service_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    service = await db.scalar(
        select(Service)
        .options(
            selectinload(Service.team),
            selectinload(Service.slo),
            selectinload(Service.deploy_history),
            selectinload(Service.dependencies_out).selectinload("target"),
            selectinload(Service.dependencies_in).selectinload("source"),
            selectinload(Service.maturity),
            selectinload(Service.security),
        )
        .where(Service.id == service_id, Service.deleted_at.is_(None))
    )
    if not service:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Service not found."})

    return ServiceDetailResponse.model_validate(service)


@router.patch("/services/{service_id}", response_model=ServiceResponse)
async def update_service(
    service_id: uuid.UUID,
    payload: ServiceUpdate,
    db: AsyncSession = Depends(get_db),
):
    service = await db.get(Service, service_id)
    if not service or service.deleted_at:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Service not found."})

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(service, field, value)

    await db.commit()
    await db.refresh(service)

    await publish_catalog_event("service.updated", {
        "service_id": str(service.id),
        "fields_updated": list(update_data.keys()),
    })

    # Sync health status change to Neo4j
    if "health_status" in update_data:
        await sync_service_to_neo4j(service)

    return ServiceResponse.model_validate(service)


@router.delete("/services/{service_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_service(
    service_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    service = await db.get(Service, service_id)
    if not service or service.deleted_at:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Service not found."})

    # Soft delete — never hard delete
    from datetime import datetime, timezone
    service.deleted_at = datetime.now(timezone.utc)
    await db.commit()

    await publish_catalog_event("service.deleted", {
        "service_id": str(service.id),
        "name": service.name,
    })

    # Remove from Neo4j — this is what blast radius queries read
    await delete_service_from_neo4j(str(service_id))

    logger.info("Service soft-deleted: %s (%s)", service.name, service_id)
