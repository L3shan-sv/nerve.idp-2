"""
Nerve IDP — OPA Client

Calls the OPA sidecar at /v1/data/nerve/deploy to evaluate all 6 policies
in a single request. OPA evaluates them in parallel internally.

Policy weights (must sum to 100):
  health_endpoints      15
  slo_defined           20
  runbook               15
  otel_instrumentation  15
  secrets_via_vault     20
  security_posture      15
  ─────────────────────────
  Total                100

A Critical CVE in security_posture hard-blocks regardless of total score.

The input document passed to OPA:
  {
    "service_id": "...",
    "service_name": "...",
    "version": "...",
    "environment": "...",
    "health_check_passing": bool,
    "slo_defined": bool,
    "runbook_url": str | null,
    "runbook_updated_at": str | null,    ← must be after last_deploy_at
    "runbook_last_deploy_at": str | null,
    "otel_traces_exporting": bool,
    "otel_missing_endpoints": list[str],
    "has_vault_secrets": bool,
    "has_plaintext_secrets": bool,
    "critical_cves": int,
    "high_cves": int
  }
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

POLICY_WEIGHTS = {
    "health_endpoints": 15,
    "slo_defined": 20,
    "runbook": 15,
    "otel_instrumentation": 15,
    "secrets_via_vault": 20,
    "security_posture": 15,
}


@dataclass
class OpaEvaluationResult:
    score: int
    passed: bool  # score >= 80 AND no critical CVE
    checks: list[dict[str, Any]] = field(default_factory=list)
    critical_cve_block: bool = False


async def build_opa_input(
    service_id: str,
    service_name: str,
    version: str,
    environment: str,
) -> dict:
    """
    Gather all signals needed for OPA policy evaluation.
    In Phase 2 some of these are stubbed — full implementation in Phase 3/4.
    """
    # TODO Phase 3: real health check probe
    # TODO Phase 3: real OTel trace check against Jaeger
    # TODO Phase 3: real Vault secret check against k8s secret objects
    # For now, fetch from the security_posture and maturity tables

    # Stub input — replace with real probes in Phase 3
    return {
        "service_id": service_id,
        "service_name": service_name,
        "version": version,
        "environment": environment,
        "health_check_passing": True,       # TODO: probe /health endpoint
        "slo_defined": True,                # TODO: check slo_definitions table
        "runbook_url": None,                # TODO: check docs_pages table
        "runbook_updated_at": None,         # TODO: check docs_pages.updated_at
        "runbook_last_deploy_at": None,     # TODO: check deploy_history
        "otel_traces_exporting": True,      # TODO: query Jaeger for recent traces
        "otel_missing_endpoints": [],       # TODO: check OTel collector
        "has_vault_secrets": True,          # TODO: check k8s Secret objects
        "has_plaintext_secrets": False,     # TODO: scan manifest for env vars
        "critical_cves": 0,                 # TODO: read from security_posture table
        "high_cves": 0,
    }


async def evaluate_compliance(
    service_id: str,
    service_name: str,
    version: str,
    environment: str,
) -> OpaEvaluationResult:
    """
    Call OPA sidecar and parse the evaluation result into an OpaEvaluationResult.

    OPA endpoint: POST /v1/data/nerve/deploy
    Expected response:
      {
        "result": {
          "checks": {
            "health_endpoints": {"status": "pass", "score": 15, "detail": "...", "fix_url": "..."},
            "slo_defined": {"status": "pass", "score": 20, "detail": "..."},
            ...
          },
          "score": 85,
          "passed": true,
          "critical_cve_block": false
        }
      }
    """
    opa_input = await build_opa_input(service_id, service_name, version, environment)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{settings.OPA_URL}/v1/data/nerve/deploy",
                json={"input": opa_input},
            )
            response.raise_for_status()
            data = response.json()

    except httpx.TimeoutException:
        logger.error("OPA evaluation timed out for service %s", service_id)
        # Fail closed on timeout — never fail open
        raise RuntimeError("Policy evaluation timed out. Deploy blocked for safety.")

    except httpx.HTTPStatusError as exc:
        logger.error("OPA returned error %s for service %s", exc.response.status_code, service_id)
        raise RuntimeError(f"Policy evaluation failed with status {exc.response.status_code}.")

    result = data.get("result", {})
    raw_checks = result.get("checks", {})

    checks = []
    for check_name, check_data in raw_checks.items():
        checks.append({
            "name": check_name,
            "status": check_data.get("status", "fail"),
            "score": check_data.get("score", 0),
            "weight": POLICY_WEIGHTS.get(check_name, 0),
            "detail": check_data.get("detail", ""),
            "fix_url": check_data.get("fix_url"),
        })

    return OpaEvaluationResult(
        score=result.get("score", 0),
        passed=result.get("passed", False),
        checks=checks,
        critical_cve_block=result.get("critical_cve_block", False),
    )
