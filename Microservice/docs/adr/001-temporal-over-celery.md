# ADR-001: Temporal.io for IaC and Scaffolding Workflows

**Status:** Accepted  
**Date:** 2024-01-01  
**Authors:** Platform Engineering

---

## Context

The IaC self-service and app scaffolding features require multi-step workflows
that interact with external systems: GitHub API, Terraform Cloud, Pulumi,
Kubernetes, and HashiCorp Vault.

These workflows have the following characteristics:

- **Long-running** — a scaffold workflow takes 2–4 minutes. An IaC apply can
  take 10–30 minutes depending on the resource being provisioned.
- **Multi-step with human approval gates** — IaC apply requires an explicit
  human approval between plan and apply. Runbook execution may require SRE
  approval before proceeding.
- **Must be resumable** — if the worker process crashes mid-scaffold (e.g. after
  creating the GitHub repo but before pushing the initial commit), the workflow
  must resume from the point of failure, not restart from the beginning.
- **External system interactions are not idempotent by default** — `terraform apply`
  run twice on a partially-applied plan can create duplicate resources or fail
  with a state lock error. GitHub repo creation called twice returns 422 Unprocessable.

Two options were evaluated: **Celery** (already in the stack for lightweight tasks)
and **Temporal.io**.

---

## Decision

Use **Temporal.io** for all IaC, scaffolding, runbook, and chaos approval workflows.

Keep **Celery** for lightweight background tasks: maturity scoring, cost polling,
doc indexing, DORA metric computation. These tasks are short-lived, stateless,
and tolerate failure without consequence.

---

## Consequences of choosing Temporal

### What Temporal gives us

**Durable execution.** Temporal persists workflow state after every activity
completion. If the worker process crashes between "create GitHub repo" and
"push initial commit", Temporal resumes at "push initial commit" on the next
worker start. The partial state is not lost.

**Human approval gates.** Temporal signals allow a workflow to pause and wait
indefinitely for an external signal — an HTTP call from the approval UI. No
polling, no timeout, no database flag. The workflow is simply suspended until
the signal arrives.

```python
# ScaffoldWorkflow pauses here until the IaC approval signal fires
await workflow.wait_condition(lambda: self._approved)
```

**Idempotency built in.** Every Temporal activity has an activity ID. If an
activity is retried (due to worker crash or transient failure), Temporal
deduplicates the retry using the activity ID. Combined with idempotency checks
at the activity level (check if GitHub repo already exists before creating),
this prevents duplicate resource creation.

**Visibility.** The Temporal UI (port 8088 in the local stack) shows every
workflow execution, its current state, event history, and any failures. During
a scaffold that's taking longer than expected, an SRE can look at the Temporal
UI and see exactly which step is running.

### What we give up

**Operational complexity.** Temporal requires its own PostgreSQL schema (handled
by the `temporalio/auto-setup` Docker image in our stack) and its own worker
processes per task queue. In production this is a managed Temporal Cloud
deployment. Locally it runs in Docker Compose.

**Python SDK learning curve.** Temporal workflows are defined as Python classes
with specific decorators. The activity/workflow distinction takes time to learn
correctly. Incorrectly using non-deterministic code inside a workflow definition
(rather than an activity) will cause replay failures.

---

## Workflow definitions

Three Temporal workflows are defined for Phase 1/2:

**ScaffoldWorkflow** — task queue: `nerve-scaffold`
```
1. validate_request (activity)
2. render_cookiecutter_template (activity)
3. create_github_repo (activity — idempotency: check if repo exists first)
4. push_initial_commit (activity)
5. configure_branch_protection (activity)
6. create_k8s_namespace_resources (activity)
7. provision_vault_secrets (activity)
8. register_in_catalog (activity)
9. sync_to_neo4j (activity — mirrors catalog registration)
```

**IaCApplyWorkflow** — task queue: `nerve-iac`
```
1. generate_plan (activity — calls Terraform Cloud / Pulumi)
2. store_plan_output (activity)
3. [SIGNAL: await human approval]
4. validate_approval (activity — checks RBAC role of approver)
5. apply_plan (activity — idempotent: check workspace state before applying)
6. create_k8s_resources (activity)
7. provision_vault_credentials (activity)
8. update_catalog (activity)
9. write_audit_log (activity)
```

**RemediationWorkflow** — task queue: `nerve-runbooks`
```
1. validate_rbac (activity)
2. [SIGNAL: await approval, if runbook.requires_approval]
3. for each action in runbook.actions:
   execute_action(action) (activity — k8s exec, vault rotate, aws api call)
4. write_audit_log (activity — full snapshot of runbook at execution time)
```

---

## GitHub API rate limit handling

The ScaffoldWorkflow's `create_github_repo` activity checks for HTTP 403
responses from the GitHub API and distinguishes between:

- **403 auth failure** → permanent failure, no retry
- **403 rate limit** → schedule retry after `X-RateLimit-Reset` header timestamp

This is implemented as a Temporal activity retry policy with a custom
`ApplicationError` that signals to Temporal whether to retry or fail permanently.

```python
@activity.defn
async def create_github_repo(params: ScaffoldParams) -> str:
    response = await github_client.create_repo(params.name)
    if response.status_code == 403:
        remaining = int(response.headers.get("X-RateLimit-Remaining", 0))
        if remaining == 0:
            reset_time = int(response.headers.get("X-RateLimit-Reset", 0))
            wait_seconds = max(reset_time - time.time(), 60)
            # Signal Temporal to retry after rate limit window resets
            raise ApplicationError(
                f"GitHub rate limit hit. Retry after {wait_seconds}s",
                non_retryable=False,  # IS retryable
            )
        raise ApplicationError("GitHub auth failure", non_retryable=True)
    return response.json()["html_url"]
```

---

## Alternatives considered

**Celery chords and chains** — Celery supports chaining tasks and callback groups
(chords). However, Celery has no native concept of durable workflow state between
task executions. If the broker restarts mid-chain, the chain state is lost. Celery
also has no native human approval gate primitive — you'd need to implement this
with database polling, which is fragile. Celery was rejected for IaC and scaffold
for these reasons.

**AWS Step Functions** — Cloud-provider specific. Rejected because Nerve IDP must
run on-premise or in any cloud.

**Prefect / Dagster** — Designed for data pipelines, not service orchestration.
Rejected because the workflow patterns (human approval gates, k8s exec actions)
are better expressed in Temporal's event-driven model.
