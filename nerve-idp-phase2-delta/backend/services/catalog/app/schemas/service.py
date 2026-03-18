"""
Catalog service — Pydantic schemas

These schemas are the API contract between the catalog service and its consumers.
They must match the OpenAPI spec in docs/openapi.yaml.
Changes here require a corresponding change in the spec.
"""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, HttpUrl, field_validator


class TeamInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    slug: str


class CatalogSummary(BaseModel):
    total_services: int
    healthy: int
    degraded: int
    frozen: int
    avg_maturity_score: float
    critical_cves: int


class ServiceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    team: str = ""          # Resolved from team relationship
    language: str
    version: Optional[str] = None
    repo_url: Optional[str] = None
    health_status: str
    compliance_score: int
    maturity_score: int
    error_budget_consumed: float
    deploy_frozen: bool
    replica_count: int
    template_version: Optional[str] = None
    last_deploy_at: Optional[datetime] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    @field_validator("team", mode="before")
    @classmethod
    def resolve_team(cls, v, info):
        # If team is a Team object (relationship loaded), use its slug
        if hasattr(v, "slug"):
            return v.slug
        return v or ""


class SecuritySummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    score: int = 0
    critical_cves: int = 0
    last_scan_at: Optional[datetime] = None


class MaturitySummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    overall_score: int = 0


class ServiceDetailResponse(ServiceResponse):
    model_config = ConfigDict(from_attributes=True)

    description: Optional[str] = None
    upstream_dependencies: list[str] = []     # Service IDs
    downstream_dependents: list[str] = []     # Service IDs
    security: Optional[SecuritySummary] = None
    maturity_breakdown: Optional[MaturitySummary] = None

    @field_validator("upstream_dependencies", mode="before")
    @classmethod
    def resolve_upstream(cls, v, info):
        if isinstance(v, list) and v and hasattr(v[0], "target_id"):
            return [str(dep.target_id) for dep in v]
        return v or []

    @field_validator("downstream_dependents", mode="before")
    @classmethod
    def resolve_downstream(cls, v, info):
        if isinstance(v, list) and v and hasattr(v[0], "source_id"):
            return [str(dep.source_id) for dep in v]
        return v or []


class ServiceListResponse(BaseModel):
    items: list[ServiceResponse]
    total: int
    page: int
    limit: int
    summary: Optional[CatalogSummary] = None


class ServiceRegistration(BaseModel):
    name: str
    team: str
    language: str
    repo_url: Optional[HttpUrl] = None
    description: Optional[str] = None
    upstream_dependencies: list[uuid.UUID] = []

    @field_validator("language")
    @classmethod
    def validate_language(cls, v):
        allowed = {"python", "go", "typescript", "rust", "java"}
        if v not in allowed:
            raise ValueError(f"language must be one of: {', '.join(allowed)}")
        return v

    @field_validator("name")
    @classmethod
    def validate_name(cls, v):
        import re
        if not re.match(r"^[a-z][a-z0-9-]{2,62}$", v):
            raise ValueError("name must be lowercase, start with a letter, 3-63 chars, hyphens allowed")
        return v


class ServiceUpdate(BaseModel):
    version: Optional[str] = None
    replica_count: Optional[int] = None
    health_status: Optional[str] = None
    description: Optional[str] = None

    @field_validator("health_status")
    @classmethod
    def validate_health(cls, v):
        if v and v not in {"healthy", "degraded", "frozen", "unknown"}:
            raise ValueError("health_status must be: healthy, degraded, frozen, or unknown")
        return v


class SloDefinitionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    service_id: uuid.UUID
    sli_type: str
    target: float
    window_days: int
    latency_threshold_ms: Optional[int] = None
    description: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None


class SloDefinitionInput(BaseModel):
    sli_type: str
    target: float
    window_days: int = 30
    latency_threshold_ms: Optional[int] = None
    description: Optional[str] = None

    @field_validator("sli_type")
    @classmethod
    def validate_sli_type(cls, v):
        allowed = {"availability", "latency", "throughput", "error_rate"}
        if v not in allowed:
            raise ValueError(f"sli_type must be one of: {', '.join(allowed)}")
        return v

    @field_validator("target")
    @classmethod
    def validate_target(cls, v):
        if not 0 < v <= 100:
            raise ValueError("target must be between 0 and 100 (e.g. 99.9 for 99.9%)")
        return v
