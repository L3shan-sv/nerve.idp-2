# ADR-003: OPA Policy Enforcement — Two Layers

**Status:** Accepted  
**Date:** 2024-01-01  
**Authors:** Platform Engineering

---

## Context

Nerve IDP needs to enforce production standards on every deploy. The enforcement
must be:

- **Consistent** — not bypassable by clever API usage
- **Auditable** — every enforcement decision logged with the specific checks that failed
- **Extensible** — platform engineers must be able to add or modify policies
  without changing application code
- **Fast** — policy evaluation must not add more than 50ms to a deploy request

---

## Decision

Enforce golden path compliance at **two independent layers**:

**Layer 1 — API-level enforcement (Golden Path Enforcer service)**  
OPA runs as a sidecar alongside the golden path enforcer FastAPI service.
Every `POST /services/{id}/deploy` request is intercepted. OPA evaluates 6
Rego policies in parallel and returns a 0-100 compliance score. Requests
with score < 80 receive a `403 Forbidden` with a structured failure report.

**Layer 2 — Kubernetes admission control (OPA Gatekeeper)**  
OPA Gatekeeper runs as a Kubernetes validating admission webhook. Any pod
attempting to join the cluster without a valid `nerve.io/compliance-annotation`
is rejected at the admission layer, regardless of whether the API-level check
was bypassed.

---

## Why two layers?

A single API-level check can be bypassed by:
- Direct `kubectl apply` (bypasses the gateway entirely)
- A misconfigured CI pipeline that applies manifests directly
- An operator with cluster-admin who applies in an emergency and forgets to go
  through the portal

Layer 2 (Gatekeeper) closes these gaps. A pod that did not go through the
golden path enforcer will not have the compliance annotation, and Gatekeeper
will reject it at admission. No exceptions except for system namespaces.

---

## The 6 Rego policies

Each policy is a separate `.rego` file in `/policies/rego/`. Weights sum to 100.

| Policy | Weight | Hard block? | Check |
|--------|--------|-------------|-------|
| `health_endpoints` | 15 | No | `/health` and `/ready` return 200 |
| `slo_defined` | 20 | No | `service.yaml` has latency + uptime targets |
| `runbook` | 15 | No | TechDocs page exists, updated within 90 days, and updated after last deploy |
| `otel_instrumentation` | 15 | No | Traces exporting to OTel collector |
| `secrets_via_vault` | 20 | No | No env var secrets, no plaintext k8s Secrets |
| `security_posture` | 15 | **Yes** | No Critical CVEs (Critical → score = 0) |

**A Critical CVE hard-blocks the deploy** regardless of the overall score.
A service with all other checks passing (85/100) but one Critical CVE gets
score = 0 and is blocked.

The runbook check is deliberately strict: the runbook must be updated **after**
the last deploy. A team with a 6-month-old runbook that hasn't been touched
since their last release scores 0 on this check. This prevents the gaming
pattern of creating a placeholder runbook and never updating it.

---

## OPA sidecar startup gate

**This is a critical correctness requirement.**

The golden path enforcer pod has an OPA sidecar container. If the enforcer pod
accepts traffic before OPA has finished loading its Rego policies, the enforcer
has two bad options:

1. **Fail open** — allow the deploy without policy evaluation. Security incident.
2. **Fail closed** — block all deploys with a 500 error. Platform outage.

The correct solution: the enforcer pod's startup probe checks OPA's `/health`
endpoint before the pod is added to the load balancer. The main app's lifespan
startup hook also waits for OPA:

```python
async def wait_for_opa(max_retries: int = 30, delay: float = 1.0) -> None:
    for attempt in range(max_retries):
        response = await client.get(f"{OPA_URL}/health")
        if response.status_code == 200:
            return
        await asyncio.sleep(delay)
    raise RuntimeError("OPA not ready — refusing to start")
```

This is implemented in `backend/gateway/app/main.py`.

---

## Policy evaluation performance

OPA evaluates all 6 policies in parallel (not sequentially) via a single
`POST /v1/data/nerve/deploy` request. OPA's partial evaluation and caching
means repeated evaluations of the same policy against the same input are
served from cache.

Target: < 30ms for a full policy evaluation on a warm OPA instance.

---

## Policy change governance

Policies are version-controlled in `/policies/rego/` in Git. Changes require:
1. A PR opened via the portal's policy change proposal UI
2. Review from a `platform_engineer` role
3. CI validation: `opa test policies/rego/` must pass
4. Merge to main → ArgoCD syncs new policy to OPA sidecar

Platform engineers can view active policies and their weights in the portal
(read-only). They cannot edit Rego directly from the portal — all changes
go through Git/PR. This is intentional: policy changes have the same review
requirements as code changes.

---

## Gatekeeper constraint templates

Two constraint templates are deployed:

**RequireComplianceAnnotation** — every pod must have:
```yaml
metadata:
  annotations:
    nerve.io/compliance-score: "85"
    nerve.io/compliance-passed: "true"
    nerve.io/enforced-at: "2024-01-01T12:00:00Z"
```

**RequireNetworkPolicy** — every namespace must have at least one NetworkPolicy
restricting ingress and egress. Services without a NetworkPolicy lose security
posture score and are flagged in the maturity dashboard.

System namespaces (`kube-system`, `kube-public`, `temporal`, `vault`) are
excluded from both constraints via `excludedNamespaces` in the constraint spec.
