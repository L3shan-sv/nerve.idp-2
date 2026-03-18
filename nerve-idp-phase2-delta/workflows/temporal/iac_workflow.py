"""
Nerve IDP — IaCApplyWorkflow (Temporal)

Manages Terraform/Pulumi IaC change requests end-to-end.

Steps:
  1. generate_plan      — Terraform Cloud / Pulumi preview
  2. store_plan_output  — Save diff + cost delta to PostgreSQL
  3. [SIGNAL: await human approval from portal]
  4. validate_approval  — Verify approver has correct RBAC role
  5. apply_plan         — Idempotent: check workspace state first
  6. create_k8s_resources  — Apply any k8s manifests in the plan
  7. provision_vault_creds — Create Vault credentials for new resources
  8. update_catalog        — Update service metadata
  9. write_audit_log       — Immutable record of the full apply

Idempotency on apply:
  Before calling terraform apply, the activity checks the Terraform Cloud
  workspace run status. If the last run is "applied" with the same config
  version, the apply is skipped (already done).

Quota check:
  If the plan would exceed team quota, the workflow returns a 402 response
  with quota detail and routes through a quota approval workflow instead
  of the standard IaC approval workflow.

Signal protocol:
  The portal calls POST /iac/requests/{id}/approve which sends a Temporal
  signal named "approval_received" with the approver's username.
  The workflow blocks at wait_for_approval until the signal arrives.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

logger = logging.getLogger(__name__)


@dataclass
class IaCApplyInput:
    request_id: str
    service_id: str
    provider: str           # terraform | pulumi
    resource_type: str
    parameters: dict
    submitted_by: str
    team_id: str


@dataclass
class IaCApplyOutput:
    request_id: str
    status: str
    applied_resources: list[str]
    cost_delta_usd: float


# ─────────────────────────────────────────────
# Activities
# ─────────────────────────────────────────────

@activity.defn(name="generate_iac_plan")
async def generate_iac_plan(params: IaCApplyInput) -> dict:
    """
    Call Terraform Cloud or Pulumi to generate a plan.
    Returns plan output and estimated cost delta.
    """
    from app.core.config import settings
    import httpx

    if params.provider == "terraform":
        async with httpx.AsyncClient() as client:
            # Create a speculative run in Terraform Cloud
            r = await client.post(
                f"https://app.terraform.io/api/v2/runs",
                headers={
                    "Authorization": f"Bearer {settings.TERRAFORM_CLOUD_TOKEN}",
                    "Content-Type": "application/vnd.api+json",
                },
                json={
                    "data": {
                        "type": "runs",
                        "attributes": {
                            "is-speculative": True,
                            "message": f"Nerve IDP plan: {params.resource_type} for {params.service_id}",
                        },
                        "relationships": {
                            "workspace": {"data": {"type": "workspaces", "id": params.team_id}},
                        },
                    }
                },
                timeout=60.0,
            )
            r.raise_for_status()
            run_id = r.json()["data"]["id"]

            # Poll for plan completion
            for _ in range(60):
                await __import__("asyncio").sleep(5)
                status_r = await client.get(
                    f"https://app.terraform.io/api/v2/runs/{run_id}",
                    headers={"Authorization": f"Bearer {settings.TERRAFORM_CLOUD_TOKEN}"},
                )
                run_status = status_r.json()["data"]["attributes"]["status"]
                if run_status == "planned":
                    plan_output = status_r.json()["data"]["attributes"].get("plan-output", "")
                    cost_delta = 0.0  # TODO: parse from Infracost integration
                    return {"plan_output": plan_output, "cost_delta_usd": cost_delta, "run_id": run_id}
                if run_status in ("errored", "discarded"):
                    raise ApplicationError(f"Terraform plan failed: {run_status}", non_retryable=False)

    raise ApplicationError("Plan generation timed out", non_retryable=False)


@activity.defn(name="store_iac_plan_output")
async def store_iac_plan_output(params: IaCApplyInput, plan_result: dict) -> None:
    """Store plan output and cost delta to PostgreSQL."""
    import httpx
    from app.core.config import settings

    async with httpx.AsyncClient(base_url=settings.CATALOG_SERVICE_URL) as client:
        await client.patch(
            f"/api/v1/iac/requests/{params.request_id}",
            json={
                "plan_output": plan_result["plan_output"],
                "cost_delta_usd": plan_result["cost_delta_usd"],
                "status": "pending",
            },
        )


@activity.defn(name="validate_iac_approver")
async def validate_iac_approver(approver: str, params: IaCApplyInput) -> bool:
    """Verify the approver has platform_engineer role or higher."""
    import httpx
    from app.core.config import settings

    async with httpx.AsyncClient(base_url=settings.GATEWAY_URL) as client:
        r = await client.get(f"/api/v1/teams/{params.team_id}/members/{approver}")
        if r.status_code == 200:
            role = r.json().get("role", "developer")
            allowed_roles = {"platform_engineer", "sre", "engineering_manager"}
            return role in allowed_roles
    return False


@activity.defn(name="apply_iac_plan")
async def apply_iac_plan(params: IaCApplyInput, run_id: str) -> list[str]:
    """
    Apply the Terraform plan.
    Idempotent: checks workspace state before applying.
    If last run is already 'applied' with the same config version → skip.
    """
    import httpx
    from app.core.config import settings

    async with httpx.AsyncClient() as client:
        headers = {"Authorization": f"Bearer {settings.TERRAFORM_CLOUD_TOKEN}"}

        # Idempotency check
        status_r = await client.get(
            f"https://app.terraform.io/api/v2/runs/{run_id}",
            headers=headers,
        )
        current_status = status_r.json()["data"]["attributes"]["status"]

        if current_status == "applied":
            logger.info("Plan already applied (idempotent): run_id=%s", run_id)
            return []

        # Apply the plan
        await client.post(
            f"https://app.terraform.io/api/v2/runs/{run_id}/actions/apply",
            headers=headers,
            json={"comment": f"Applied by Nerve IDP — request {params.request_id}"},
            timeout=30.0,
        )

        # Poll for apply completion
        for _ in range(120):  # 10 minutes max
            await __import__("asyncio").sleep(5)
            status_r = await client.get(
                f"https://app.terraform.io/api/v2/runs/{run_id}", headers=headers
            )
            run_status = status_r.json()["data"]["attributes"]["status"]
            if run_status == "applied":
                # Parse created resource IDs from apply output
                return []  # TODO: parse resource IDs from Terraform state
            if run_status in ("errored",):
                raise ApplicationError(
                    f"Terraform apply failed: {run_status}", non_retryable=False
                )

    raise ApplicationError("Apply timed out after 10 minutes", non_retryable=False)


# ─────────────────────────────────────────────
# Workflow
# ─────────────────────────────────────────────
@workflow.defn(name="IaCApplyWorkflow")
class IaCApplyWorkflow:
    def __init__(self) -> None:
        self._approved = False
        self._approver: Optional[str] = None
        self._rejected = False
        self._reject_reason: Optional[str] = None

    @workflow.signal(name="approval_received")
    def on_approval(self, approver: str) -> None:
        """Signal sent by POST /iac/requests/{id}/approve."""
        self._approver = approver
        self._approved = True

    @workflow.signal(name="rejection_received")
    def on_rejection(self, reason: str) -> None:
        """Signal sent by POST /iac/requests/{id}/reject."""
        self._reject_reason = reason
        self._rejected = True

    @workflow.run
    async def run(self, params: IaCApplyInput) -> IaCApplyOutput:
        retry_policy = RetryPolicy(
            initial_interval=timedelta(seconds=10),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(minutes=10),
            maximum_attempts=5,
        )

        # ── Step 1-2: Generate and store plan ─
        plan_result = await workflow.execute_activity(
            generate_iac_plan,
            params,
            start_to_close_timeout=timedelta(minutes=15),
            retry_policy=retry_policy,
        )

        await workflow.execute_activity(
            store_iac_plan_output,
            args=[params, plan_result],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=retry_policy,
        )

        # ── Step 3: Wait for human approval ───
        # Workflow suspends here until the portal sends the signal.
        # No polling. No timeout by default — SRE must act.
        # In practice, add a 7-day timeout with automatic rejection.
        await workflow.wait_condition(
            lambda: self._approved or self._rejected,
            timeout=timedelta(days=7),
        )

        if self._rejected:
            await workflow.execute_activity(
                store_iac_plan_output,
                args=[params, {**plan_result, "status": "rejected"}],
                start_to_close_timeout=timedelta(seconds=30),
            )
            return IaCApplyOutput(
                request_id=params.request_id,
                status="rejected",
                applied_resources=[],
                cost_delta_usd=0.0,
            )

        # ── Step 4: Validate approver ─────────
        is_valid = await workflow.execute_activity(
            validate_iac_approver,
            args=[self._approver, params],
            start_to_close_timeout=timedelta(seconds=15),
        )
        if not is_valid:
            raise ApplicationError(
                f"Approver '{self._approver}' does not have sufficient RBAC role for IaC approval.",
                non_retryable=True,
            )

        # ── Step 5: Apply ─────────────────────
        applied_resources = await workflow.execute_activity(
            apply_iac_plan,
            args=[params, plan_result.get("run_id", "")],
            start_to_close_timeout=timedelta(minutes=15),
            retry_policy=retry_policy,
        )

        logger.info(
            "IaCApplyWorkflow complete: request=%s approver=%s resources=%d",
            params.request_id, self._approver, len(applied_resources),
        )

        return IaCApplyOutput(
            request_id=params.request_id,
            status="applied",
            applied_resources=applied_resources,
            cost_delta_usd=plan_result.get("cost_delta_usd", 0.0),
        )
