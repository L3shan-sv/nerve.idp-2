"""
Catalog service — SQLAlchemy models
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Integer, Numeric,
    SmallInteger, String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Team(Base):
    __tablename__ = "teams"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    slug: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    budget_usd: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    services: Mapped[list["Service"]] = relationship(back_populates="team")
    quota: Mapped[Optional["TeamQuota"]] = relationship(back_populates="team", uselist=False)


class Service(Base):
    __tablename__ = "services"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    team_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("teams.id", ondelete="RESTRICT"), nullable=False)
    language: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[Optional[str]] = mapped_column(String)
    repo_url: Mapped[Optional[str]] = mapped_column(Text)
    description: Mapped[Optional[str]] = mapped_column(Text)
    health_status: Mapped[str] = mapped_column(String, nullable=False, default="unknown")
    compliance_score: Mapped[int] = mapped_column(SmallInteger, default=0)
    maturity_score: Mapped[int] = mapped_column(SmallInteger, default=0)
    error_budget_consumed: Mapped[float] = mapped_column(Numeric(5, 2), default=0)
    deploy_frozen: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    frozen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    frozen_reason: Mapped[Optional[str]] = mapped_column(Text)
    replica_count: Mapped[int] = mapped_column(SmallInteger, default=1)
    template_version: Mapped[Optional[str]] = mapped_column(String)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_deploy_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    team: Mapped["Team"] = relationship(back_populates="services")
    slo: Mapped[Optional["SloDefinition"]] = relationship(back_populates="service", uselist=False)
    deploy_history: Mapped[list["DeployHistory"]] = relationship(back_populates="service")
    dependencies_out: Mapped[list["ServiceDependency"]] = relationship(back_populates="source", foreign_keys="ServiceDependency.source_id")
    dependencies_in: Mapped[list["ServiceDependency"]] = relationship(back_populates="target", foreign_keys="ServiceDependency.target_id")
    maturity: Mapped[Optional["MaturityScore"]] = relationship(back_populates="service", uselist=False)
    security: Mapped[Optional["SecurityPosture"]] = relationship(back_populates="service", uselist=False)


class ServiceDependency(Base):
    __tablename__ = "service_dependencies"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("services.id", ondelete="CASCADE"), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("services.id", ondelete="CASCADE"), nullable=False)
    relationship: Mapped[str] = mapped_column(String, nullable=False, default="DEPENDS_ON")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    source: Mapped["Service"] = relationship(back_populates="dependencies_out", foreign_keys=[source_id])
    target: Mapped["Service"] = relationship(back_populates="dependencies_in", foreign_keys=[target_id])
    __table_args__ = (UniqueConstraint("source_id", "target_id", "relationship"),)


class SloDefinition(Base):
    __tablename__ = "slo_definitions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    service_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("services.id", ondelete="CASCADE"), nullable=False, unique=True)
    sli_type: Mapped[str] = mapped_column(String, nullable=False)
    target: Mapped[float] = mapped_column(Numeric(6, 4), nullable=False)
    window_days: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=30)
    latency_threshold_ms: Mapped[Optional[int]] = mapped_column(Integer)
    description: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    service: Mapped["Service"] = relationship(back_populates="slo")


class DeployHistory(Base):
    __tablename__ = "deploy_history"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    service_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("services.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[str] = mapped_column(String, nullable=False)
    environment: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    compliance_score: Mapped[Optional[int]] = mapped_column(SmallInteger)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    workflow_id: Mapped[Optional[str]] = mapped_column(String)
    deployed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    service: Mapped["Service"] = relationship(back_populates="deploy_history")


class MaturityScore(Base):
    __tablename__ = "maturity_scores"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    service_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("services.id", ondelete="CASCADE"), nullable=False, unique=True)
    overall_score: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    observability: Mapped[int] = mapped_column(SmallInteger, default=0)
    reliability: Mapped[int] = mapped_column(SmallInteger, default=0)
    security: Mapped[int] = mapped_column(SmallInteger, default=0)
    docs: Mapped[int] = mapped_column(SmallInteger, default=0)
    cost: Mapped[int] = mapped_column(SmallInteger, default=0)
    error_budget_health: Mapped[int] = mapped_column(SmallInteger, default=0)
    pillar_detail: Mapped[Optional[dict]] = mapped_column(JSONB)
    template_behind_by: Mapped[int] = mapped_column(SmallInteger, default=0)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    service: Mapped["Service"] = relationship(back_populates="maturity")


class SecurityPosture(Base):
    __tablename__ = "security_posture"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    service_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("services.id", ondelete="CASCADE"), nullable=False, unique=True)
    score: Mapped[int] = mapped_column(SmallInteger, default=0)
    critical_cves: Mapped[int] = mapped_column(SmallInteger, default=0)
    high_cves: Mapped[int] = mapped_column(SmallInteger, default=0)
    medium_cves: Mapped[int] = mapped_column(SmallInteger, default=0)
    sbom_present: Mapped[bool] = mapped_column(Boolean, default=False)
    sbom_generated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    sast_passed: Mapped[Optional[bool]] = mapped_column(Boolean)
    network_policy_present: Mapped[bool] = mapped_column(Boolean, default=False)
    last_scan_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    cve_detail: Mapped[Optional[dict]] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    service: Mapped["Service"] = relationship(back_populates="security")


class TeamQuota(Base):
    __tablename__ = "team_quotas"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    team_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, unique=True)
    cpu_cores: Mapped[float] = mapped_column(Numeric(8, 2), default=0)
    cpu_used: Mapped[float] = mapped_column(Numeric(8, 2), default=0)
    memory_gb: Mapped[float] = mapped_column(Numeric(8, 2), default=0)
    memory_used: Mapped[float] = mapped_column(Numeric(8, 2), default=0)
    storage_gb: Mapped[float] = mapped_column(Numeric(8, 2), default=0)
    storage_used: Mapped[float] = mapped_column(Numeric(8, 2), default=0)
    cost_usd: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    cost_used: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    team: Mapped["Team"] = relationship(back_populates="quota")
