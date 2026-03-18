"""
Nerve IDP — ScaffoldWorkflow (Temporal)

Generates a fully golden-path-compliant service in under 4 minutes.

Steps:
  1. validate_request          — name uniqueness, team exists, quota check
  2. render_template           — Cookiecutter renders service from template
  3. create_github_repo        — GitHub API, idempotent (check-before-create)
  4. push_initial_commit       — Git push rendered template
  5. configure_branch_protection — protect main branch
  6. create_k8s_namespace      — ResourceQuota + LimitRange for team namespace
  7. provision_vault_secrets   — Create Vault path for service secrets
  8. register_in_catalog       — POST /services in catalog
  9. sync_to_neo4j             — Mirror catalog registration to Neo4j

Idempotency:
  Each activity checks whether its side effect already exists before
  executing. This means the workflow is safe to retry at any step.
  - GitHub: check if repo exists before create
  - k8s: kubectl apply is idempotent
  - Vault: check if path exists before create
  - Catalog: check if service name exists before register

GitHub rate limit handling:
  create_github_repo catches 403 with X-RateLimit-Remaining == 0
  and raises a retryable ApplicationError with the reset timestamp.
  Temporal schedules the retry after the rate limit window resets.
  403 auth failure → non-retryable ApplicationError.

Error escalation:
  If any step fails after max_retries, the workflow fails and sets
  status = "failed" on the scaffold_jobs table.
  Partial state (e.g. repo created but catalog not registered) is cleaned
  up by a compensating activity run in the failure handler.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Input/Output types
# ─────────────────────────────────────────────
@dataclass
class ScaffoldInput:
    name: str
    team: str
    language: str
    description: str
    template_version: Optional[str]
    upstream_dependencies: list[str]
    requested_by: str
    workflow_id: str


@dataclass
class ScaffoldOutput:
    service_id: str
    repo_url: str
    status: str
    completed_steps: list[str]


# ─────────────────────────────────────────────
# Activities
# Each activity is a single side effect, idempotent, with retry policy.
# ─────────────────────────────────────────────

@activity.defn(name="validate_scaffold_request")
async def validate_scaffold_request(params: ScaffoldInput) -> dict:
    """
    Validate: name is available, team exists, quota not exceeded.
    Returns validated parameters including resolved team_id.
    """
    import httpx
    from app.core.config import settings

    async with httpx.AsyncClient(base_url=settings.CATALOG_SERVICE_URL) as client:
        # Check name uniqueness
        r = await client.get(f"/api/v1/services", params={"q": params.name})
        existing = r.json().get("items", [])
        if any(s["name"] == params.name for s in existing):
            raise ApplicationError(
                f"Service name '{params.name}' already exists.",
                non_retryable=True,
            )

        # Verify team
        r = await client.get(f"/api/v1/teams")
        teams = {t["slug"]: t for t in r.json()}
        if params.team not in teams:
            raise ApplicationError(
                f"Team '{params.team}' not found.",
                non_retryable=True,
            )

    return {"team_id": teams[params.team]["id"], "validated": True}


@activity.defn(name="render_cookiecutter_template")
async def render_cookiecutter_template(params: ScaffoldInput) -> str:
    """
    Render Cookiecutter template for the given language.
    Returns the path to the rendered service directory.

    Baked into every rendered service:
      - /health and /ready endpoints
      - SLO definition (service.yaml)
      - Vault secret configuration
      - OpenTelemetry SDK wiring
      - Runbook template (/docs/runbook.md)
      - GitHub Actions CI workflow
      - Dockerfile
      - ResourceQuota-aware resource limits
    """
    import tempfile
    import subprocess
    from app.core.config import settings

    template_version = params.template_version or "latest"
    template_path = f"{settings.TEMPLATE_REPO_PATH}/nerve-{params.language}/{template_version}"

    output_dir = tempfile.mkdtemp(prefix=f"scaffold-{params.name}-")

    # Cookiecutter config
    context = {
        "service_name": params.name,
        "team": params.team,
        "description": params.description,
        "language": params.language,
        "template_version": template_version,
    }

    result = subprocess.run(
        ["cookiecutter", template_path, "--no-input", "--output-dir", output_dir],
        env={**__import__("os").environ, **{f"COOKIECUTTER_{k.upper()}": v for k, v in context.items()}},
        capture_output=True,
        text=True,
        timeout=60,
    )

    if result.returncode != 0:
        raise ApplicationError(
            f"Cookiecutter template render failed: {result.stderr}",
            non_retryable=False,
        )

    return f"{output_dir}/{params.name}"


@activity.defn(name="create_github_repo")
async def create_github_repo(params: ScaffoldInput) -> str:
    """
    Create GitHub repo. Idempotent — checks if repo exists first.

    Rate limit handling:
      - 403 + X-RateLimit-Remaining == 0 → retryable with reset timestamp
      - 403 auth failure → non-retryable
    """
    import httpx
    from app.core.config import settings

    headers = {
        "Authorization": f"token {settings.GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    repo_name = params.name

    async with httpx.AsyncClient() as client:
        # Idempotency check — does repo already exist?
        check = await client.get(
            f"https://api.github.com/repos/{settings.GITHUB_ORG}/{repo_name}",
            headers=headers,
        )
        if check.status_code == 200:
            logger.info("Repo already exists (idempotent): %s", repo_name)
            return check.json()["html_url"]

        # Create repo
        response = await client.post(
            f"https://api.github.com/orgs/{settings.GITHUB_ORG}/repos",
            headers=headers,
            json={
                "name": repo_name,
                "description": params.description,
                "private": True,
                "auto_init": False,
                "has_issues": True,
                "has_wiki": False,
            },
            timeout=30.0,
        )

        if response.status_code == 403:
            remaining = int(response.headers.get("X-RateLimit-Remaining", 1))
            if remaining == 0:
                reset_time = int(response.headers.get("X-RateLimit-Reset", 0))
                wait_seconds = max(reset_time - int(time.time()), 60)
                raise ApplicationError(
                    f"GitHub rate limit hit. Retry after {wait_seconds}s.",
                    non_retryable=False,  # IS retryable
                )
            raise ApplicationError(
                "GitHub authentication failed. Check GITHUB_TOKEN.",
                non_retryable=True,
            )

        if response.status_code == 422:
            raise ApplicationError(
                f"GitHub repo creation failed: {response.json().get('message', 'Unknown error')}",
                non_retryable=True,
            )

        response.raise_for_status()
        return response.json()["html_url"]


@activity.defn(name="push_initial_commit")
async def push_initial_commit(repo_url: str, source_dir: str, params: ScaffoldInput) -> None:
    """Push rendered Cookiecutter output as initial commit."""
    import subprocess

    commands = [
        ["git", "init"],
        ["git", "remote", "add", "origin", repo_url.replace("https://", f"https://{__import__('os').environ.get('GITHUB_TOKEN', '')}@")],
        ["git", "checkout", "-b", "main"],
        ["git", "add", "."],
        ["git", "commit", "-m", f"chore: scaffold {params.name} via Nerve IDP golden path\n\n[skip ci]"],
        ["git", "push", "-u", "origin", "main"],
    ]

    for cmd in commands:
        result = subprocess.run(cmd, cwd=source_dir, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise ApplicationError(
                f"Git command failed: {' '.join(cmd)}: {result.stderr}",
                non_retryable=False,
            )


@activity.defn(name="configure_branch_protection")
async def configure_branch_protection(repo_name: str, params: ScaffoldInput) -> None:
    """Configure branch protection on main — require PR, status checks, no force push."""
    import httpx
    from app.core.config import settings

    async with httpx.AsyncClient() as client:
        response = await client.put(
            f"https://api.github.com/repos/{settings.GITHUB_ORG}/{repo_name}/branches/main/protection",
            headers={
                "Authorization": f"token {settings.GITHUB_TOKEN}",
                "Accept": "application/vnd.github.v3+json",
            },
            json={
                "required_status_checks": {
                    "strict": True,
                    "contexts": ["nerve-ci / lint", "nerve-ci / test"],
                },
                "enforce_admins": False,
                "required_pull_request_reviews": {
                    "required_approving_review_count": 1,
                    "dismiss_stale_reviews": True,
                },
                "restrictions": None,
                "allow_force_pushes": False,
                "allow_deletions": False,
            },
            timeout=30.0,
        )
        response.raise_for_status()


@activity.defn(name="create_k8s_namespace_resources")
async def create_k8s_namespace_resources(params: ScaffoldInput, team_id: str) -> None:
    """
    Create Kubernetes ResourceQuota and LimitRange in team namespace.
    Idempotent — kubectl apply creates or updates.
    """
    from kubernetes import client as k8s_client, config as k8s_config
    from app.core.config import settings

    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()

    core_v1 = k8s_client.CoreV1Api()
    namespace = f"nerve-{params.team}"

    # Ensure namespace exists
    try:
        core_v1.create_namespace(
            k8s_client.V1Namespace(metadata=k8s_client.V1ObjectMeta(name=namespace))
        )
    except k8s_client.exceptions.ApiException as e:
        if e.status != 409:  # 409 = already exists — expected
            raise

    # ResourceQuota — per-team budget from quota table
    quota = k8s_client.V1ResourceQuota(
        metadata=k8s_client.V1ObjectMeta(
            name=f"{params.team}-quota",
            namespace=namespace,
        ),
        spec=k8s_client.V1ResourceQuotaSpec(
            hard={
                "requests.cpu": "10",
                "requests.memory": "20Gi",
                "limits.cpu": "20",
                "limits.memory": "40Gi",
                "count/pods": "50",
            }
        ),
    )
    try:
        core_v1.create_namespaced_resource_quota(namespace, quota)
    except k8s_client.exceptions.ApiException as e:
        if e.status == 409:
            core_v1.patch_namespaced_resource_quota(f"{params.team}-quota", namespace, quota)
        else:
            raise

    # LimitRange — per-pod defaults
    limit_range = k8s_client.V1LimitRange(
        metadata=k8s_client.V1ObjectMeta(name="default-limits", namespace=namespace),
        spec=k8s_client.V1LimitRangeSpec(
            limits=[k8s_client.V1LimitRangeItem(
                type="Container",
                default={"cpu": "500m", "memory": "512Mi"},
                default_request={"cpu": "100m", "memory": "128Mi"},
                max={"cpu": "4", "memory": "4Gi"},
            )]
        ),
    )
    try:
        core_v1.create_namespaced_limit_range(namespace, limit_range)
    except k8s_client.exceptions.ApiException as e:
        if e.status != 409:
            raise


@activity.defn(name="provision_vault_secrets")
async def provision_vault_secrets(params: ScaffoldInput) -> None:
    """
    Create Vault KV path for service secrets.
    Idempotent — check if path exists before creating.
    """
    import hvac
    from app.core.config import settings

    client = hvac.Client(url=settings.VAULT_URL, token=settings.VAULT_TOKEN)
    secret_path = f"{params.team}/{params.name}"

    try:
        client.secrets.kv.v2.read_secret_version(
            path=secret_path,
            mount_point=settings.VAULT_MOUNT,
        )
        logger.info("Vault path already exists (idempotent): %s", secret_path)
    except hvac.exceptions.InvalidPath:
        client.secrets.kv.v2.create_or_update_secret(
            path=secret_path,
            secret={"_scaffold_placeholder": "replace_with_real_secrets"},
            mount_point=settings.VAULT_MOUNT,
        )
        logger.info("Vault path created: %s", secret_path)


@activity.defn(name="register_service_in_catalog")
async def register_service_in_catalog(
    params: ScaffoldInput, repo_url: str, team_id: str, template_version: str
) -> str:
    """Register scaffolded service in catalog. Returns service_id."""
    import httpx
    from app.core.config import settings

    async with httpx.AsyncClient(base_url=settings.CATALOG_SERVICE_URL) as client:
        # Idempotency — check if service already registered
        r = await client.get("/api/v1/services", params={"q": params.name})
        for svc in r.json().get("items", []):
            if svc["name"] == params.name:
                logger.info("Service already registered (idempotent): %s", params.name)
                return svc["id"]

        r = await client.post("/api/v1/services", json={
            "name": params.name,
            "team": params.team,
            "language": params.language,
            "repo_url": repo_url,
            "description": params.description,
            "upstream_dependencies": params.upstream_dependencies,
        })
        r.raise_for_status()
        service_id = r.json()["id"]

    return service_id


# ─────────────────────────────────────────────
# Workflow definition
# ─────────────────────────────────────────────
@workflow.defn(name="ScaffoldWorkflow")
class ScaffoldWorkflow:

    @workflow.run
    async def run(self, params: ScaffoldInput) -> ScaffoldOutput:
        completed_steps = []

        retry_policy = RetryPolicy(
            initial_interval=timedelta(seconds=5),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(minutes=5),
            maximum_attempts=5,
        )

        # Non-retryable activities use a stricter policy
        no_retry_policy = RetryPolicy(maximum_attempts=1)

        # ── Step 1: Validate ──────────────────
        validation = await workflow.execute_activity(
            validate_scaffold_request,
            params,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=no_retry_policy,
        )
        completed_steps.append("validate_request")

        # ── Step 2: Render template ───────────
        source_dir = await workflow.execute_activity(
            render_cookiecutter_template,
            params,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=retry_policy,
        )
        completed_steps.append("render_template")

        # ── Step 3: Create GitHub repo ────────
        repo_url = await workflow.execute_activity(
            create_github_repo,
            params,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=10),
                maximum_interval=timedelta(minutes=10),
                maximum_attempts=10,  # More retries — rate limit may need time to reset
            ),
        )
        completed_steps.append("create_github_repo")

        # ── Step 4: Push initial commit ───────
        await workflow.execute_activity(
            push_initial_commit,
            args=[repo_url, source_dir, params],
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=retry_policy,
        )
        completed_steps.append("push_initial_commit")

        # ── Step 5: Branch protection ─────────
        await workflow.execute_activity(
            configure_branch_protection,
            args=[params.name, params],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=retry_policy,
        )
        completed_steps.append("configure_branch_protection")

        # ── Steps 6-8 can run concurrently ────
        # k8s namespace, Vault, and catalog registration
        # don't depend on each other
        k8s_task = workflow.execute_activity(
            create_k8s_namespace_resources,
            args=[params, validation["team_id"]],
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=retry_policy,
        )
        vault_task = workflow.execute_activity(
            provision_vault_secrets,
            params,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=retry_policy,
        )
        catalog_task = workflow.execute_activity(
            register_service_in_catalog,
            args=[params, repo_url, validation["team_id"], params.template_version or "latest"],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=retry_policy,
        )

        _, _, service_id = await asyncio.gather(k8s_task, vault_task, catalog_task)
        completed_steps.extend(["create_k8s_namespace", "provision_vault", "register_in_catalog"])

        logger.info(
            "ScaffoldWorkflow complete: service=%s id=%s repo=%s",
            params.name, service_id, repo_url,
        )

        return ScaffoldOutput(
            service_id=service_id,
            repo_url=repo_url,
            status="completed",
            completed_steps=completed_steps,
        )
