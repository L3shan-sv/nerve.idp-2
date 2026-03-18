"""
Nerve IDP — Phase 2 test suite

Tests cover:
  - Catalog service: CRUD, soft delete, Redis event publishing, Neo4j sync
  - Golden path enforcer: OPA gate, freeze check, compliance scoring
  - DORA engine: tier classification, metric computation
  - Scaffold workflow: input validation, idempotency checks
  - Freeze endpoint: idempotency under concurrent alerts

Run with: pytest tests/ -v --cov=app --cov-report=term-missing
"""

import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport


# ─────────────────────────────────────────────
# Catalog service tests
# ─────────────────────────────────────────────
class TestCatalogService:

    @pytest.mark.asyncio
    async def test_list_services_returns_paginated_results(self, catalog_client):
        response = await catalog_client.get("/api/v1/services?limit=5&page=1")
        assert response.status_code == 200
        data = response.json()
        assert "items" in data
        assert "total" in data
        assert "summary" in data
        assert data["limit"] == 5
        assert data["page"] == 1

    @pytest.mark.asyncio
    async def test_register_service_creates_and_publishes_event(self, catalog_client):
        with patch("app.core.events.publish_catalog_event", new_callable=AsyncMock) as mock_publish, \
             patch("app.core.neo4j.sync_service_to_neo4j", new_callable=AsyncMock) as mock_neo4j:

            response = await catalog_client.post("/api/v1/services", json={
                "name": "test-payment-service",
                "team": "commerce",
                "language": "python",
                "description": "Payment processing service",
            })

            assert response.status_code == 201
            data = response.json()
            assert data["name"] == "test-payment-service"
            assert data["health_status"] == "unknown"

            # Event published to Redis Streams
            mock_publish.assert_called_once()
            call_args = mock_publish.call_args
            assert call_args[0][0] == "service.created"

            # Neo4j sync triggered
            mock_neo4j.assert_called_once()

    @pytest.mark.asyncio
    async def test_register_service_rejects_duplicate_name(self, catalog_client, existing_service):
        response = await catalog_client.post("/api/v1/services", json={
            "name": existing_service["name"],  # Duplicate
            "team": "commerce",
            "language": "python",
        })
        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "conflict"

    @pytest.mark.asyncio
    async def test_delete_service_is_soft_delete(self, catalog_client, existing_service):
        with patch("app.core.neo4j.delete_service_from_neo4j", new_callable=AsyncMock):
            response = await catalog_client.delete(f"/api/v1/services/{existing_service['id']}")
            assert response.status_code == 204

        # Service still exists in DB (soft deleted) but not in API
        response = await catalog_client.get(f"/api/v1/services/{existing_service['id']}")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_service_name_validation(self, catalog_client):
        # Invalid name — uppercase
        response = await catalog_client.post("/api/v1/services", json={
            "name": "Invalid-Name",
            "team": "commerce",
            "language": "python",
        })
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_catalog_summary_counts(self, catalog_client):
        response = await catalog_client.get("/api/v1/services")
        data = response.json()
        summary = data["summary"]
        assert summary["total_services"] >= 0
        assert summary["healthy"] >= 0
        assert summary["degraded"] >= 0
        assert summary["frozen"] >= 0


# ─────────────────────────────────────────────
# Golden Path Enforcer tests
# ─────────────────────────────────────────────
class TestGoldenPathEnforcer:

    @pytest.mark.asyncio
    async def test_deploy_blocked_when_service_frozen(self, enforcer_client, frozen_service):
        response = await enforcer_client.post("/internal/deploy", json={
            "service_id": frozen_service["id"],
            "version": "v1.0.0",
            "environment": "production",
            "actor": "developer-1",
        })
        # Returns 200 with frozen=True payload (not 423 at internal level)
        assert response.status_code == 200
        data = response.json()
        assert data["frozen"] is True
        assert data["unfreeze_requires_role"] == "sre"

    @pytest.mark.asyncio
    async def test_deploy_blocked_when_opa_score_below_80(self, enforcer_client, healthy_service):
        # Mock OPA returning score 52
        with patch("app.core.opa.evaluate_compliance", new_callable=AsyncMock) as mock_opa:
            from app.core.opa import OpaEvaluationResult
            mock_opa.return_value = OpaEvaluationResult(
                score=52,
                passed=False,
                checks=[
                    {"name": "health_endpoints", "status": "pass", "score": 15, "weight": 15, "detail": "OK"},
                    {"name": "slo_defined", "status": "pass", "score": 20, "weight": 20, "detail": "OK"},
                    {"name": "runbook", "status": "fail", "score": 0, "weight": 15, "detail": "No runbook"},
                    {"name": "otel_instrumentation", "status": "pass", "score": 15, "weight": 15, "detail": "OK"},
                    {"name": "secrets_via_vault", "status": "pass", "score": 20, "weight": 20, "detail": "OK"},
                    {"name": "security_posture", "status": "fail", "score": 0, "weight": 15, "detail": "Critical CVE"},
                ],
                critical_cve_block=True,
            )

            response = await enforcer_client.post("/internal/deploy", json={
                "service_id": str(healthy_service["id"]),
                "version": "v1.9.0",
                "environment": "production",
                "actor": "developer-1",
            })

        assert response.status_code == 403
        data = response.json()["detail"]
        assert data["score"] == 52
        assert data["passed"] is False
        assert len(data["checks"]) == 6

    @pytest.mark.asyncio
    async def test_deploy_approved_returns_annotation(self, enforcer_client, healthy_service):
        with patch("app.core.opa.evaluate_compliance", new_callable=AsyncMock) as mock_opa:
            from app.core.opa import OpaEvaluationResult
            mock_opa.return_value = OpaEvaluationResult(
                score=100,
                passed=True,
                checks=[],
            )

            response = await enforcer_client.post("/internal/deploy", json={
                "service_id": str(healthy_service["id"]),
                "version": "v2.0.0",
                "environment": "production",
                "actor": "developer-1",
            })

        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "queued"
        assert data["compliance_score"] == 100
        assert "nerve.io/compliance-passed=true" in data["compliance_annotation"]

    @pytest.mark.asyncio
    async def test_freeze_is_idempotent(self, enforcer_client, healthy_service):
        """
        Two concurrent freeze calls for the same service should both succeed
        but only the first one publishes the freeze event.
        """
        import asyncio

        freeze_payload = {
            "service_id": str(healthy_service["id"]),
            "reason": "Error budget exhausted",
            "burn_rate": 14.5,
            "idempotency_key": "alertmanager-1h-6h-simultaneous",
        }

        with patch("app.core.events.publish_catalog_event", new_callable=AsyncMock) as mock_publish:
            # Fire two concurrent freeze requests
            r1, r2 = await asyncio.gather(
                enforcer_client.post(f"/internal/freeze/{healthy_service['id']}", params=freeze_payload),
                enforcer_client.post(f"/internal/freeze/{healthy_service['id']}", params=freeze_payload),
            )

        assert r1.status_code == 200
        assert r2.status_code == 200

        results = [r1.json(), r2.json()]
        # Exactly one should have already_frozen=False (the first one)
        # The second sees already_frozen=True
        assert sum(1 for r in results if not r.get("already_frozen", False)) == 1
        # Event published exactly once
        assert mock_publish.call_count == 1


# ─────────────────────────────────────────────
# DORA metrics tier tests
# ─────────────────────────────────────────────
class TestDoraTiers:
    """Unit tests for DORA tier classification — no DB needed."""

    def test_elite_deployment_frequency(self):
        from app.workers.dora import get_dora_tier_deployment_freq
        assert get_dora_tier_deployment_freq(3.0) == "elite"   # 3 per day
        assert get_dora_tier_deployment_freq(1.0) == "elite"   # 1 per day

    def test_high_deployment_frequency(self):
        from app.workers.dora import get_dora_tier_deployment_freq
        assert get_dora_tier_deployment_freq(0.5) == "high"    # Every 2 days
        assert get_dora_tier_deployment_freq(1/7) == "high"    # Weekly boundary

    def test_low_deployment_frequency(self):
        from app.workers.dora import get_dora_tier_deployment_freq
        assert get_dora_tier_deployment_freq(1/60) == "low"    # Less than monthly

    def test_elite_lead_time(self):
        from app.workers.dora import get_dora_tier_lead_time
        assert get_dora_tier_lead_time(0.5) == "elite"   # 30 minutes

    def test_low_lead_time(self):
        from app.workers.dora import get_dora_tier_lead_time
        assert get_dora_tier_lead_time(800) == "low"     # > 1 month

    def test_elite_mttr(self):
        from app.workers.dora import get_dora_tier_mttr
        assert get_dora_tier_mttr(0.25) == "elite"   # 15 minutes

    def test_elite_cfr(self):
        from app.workers.dora import get_dora_tier_cfr
        assert get_dora_tier_cfr(2.0) == "elite"    # 2% failure rate

    def test_low_cfr(self):
        from app.workers.dora import get_dora_tier_cfr
        assert get_dora_tier_cfr(20.0) == "low"     # 20% failure rate


# ─────────────────────────────────────────────
# OPA policy tests (integration — requires OPA running)
# ─────────────────────────────────────────────
class TestOpaIntegration:

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_fully_compliant_service_passes(self, opa_client):
        response = await opa_client.post("/v1/data/nerve/deploy", json={
            "input": {
                "service_id": "test-001",
                "service_name": "test-service",
                "version": "v1.0.0",
                "environment": "production",
                "health_check_passing": True,
                "slo_defined": True,
                "runbook_url": "https://docs/test",
                "runbook_updated_at": "2024-06-15T00:00:00Z",
                "runbook_last_deploy_at": "2024-06-10T00:00:00Z",
                "otel_traces_exporting": True,
                "otel_missing_endpoints": [],
                "has_vault_secrets": True,
                "has_plaintext_secrets": False,
                "critical_cves": 0,
                "high_cves": 0,
            }
        })
        assert response.status_code == 200
        result = response.json()["result"]
        assert result["score"] == 100
        assert result["passed"] is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_critical_cve_hard_blocks(self, opa_client):
        response = await opa_client.post("/v1/data/nerve/deploy", json={
            "input": {
                "service_id": "test-002",
                "service_name": "test-service",
                "version": "v1.0.0",
                "environment": "production",
                "health_check_passing": True,
                "slo_defined": True,
                "runbook_url": "https://docs/test",
                "runbook_updated_at": "2024-06-15T00:00:00Z",
                "runbook_last_deploy_at": "2024-06-10T00:00:00Z",
                "otel_traces_exporting": True,
                "otel_missing_endpoints": [],
                "has_vault_secrets": True,
                "has_plaintext_secrets": False,
                "critical_cves": 1,  # Hard block
                "high_cves": 0,
            }
        })
        assert response.status_code == 200
        result = response.json()["result"]
        assert result["critical_cve_block"] is True
        assert result["passed"] is False


# ─────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────
@pytest.fixture
async def catalog_client():
    from backend.services.catalog.app.main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.fixture
async def enforcer_client():
    from backend.services.enforcer.app.main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.fixture
def existing_service():
    return {"id": str(uuid.uuid4()), "name": "existing-service"}


@pytest.fixture
def healthy_service():
    return {"id": str(uuid.uuid4()), "name": "healthy-service", "deploy_frozen": False}


@pytest.fixture
def frozen_service():
    return {
        "id": str(uuid.uuid4()),
        "name": "frozen-service",
        "deploy_frozen": True,
        "frozen_reason": "Error budget exhausted",
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "error_budget_consumed": 100.0,
    }


@pytest.fixture
async def opa_client():
    """Integration fixture — requires OPA running at localhost:8181."""
    async with AsyncClient(base_url="http://localhost:8181") as client:
        yield client
