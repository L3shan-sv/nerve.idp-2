# Nerve IDP

**Internal Developer Platform** — eliminate the cognitive tax of infrastructure ownership.

> The right way is the only way.

---

## What this is

Nerve IDP is a FAANG-grade platform engineering control plane built for SRE and Platform Engineering teams. It removes the three biggest blockers to engineering velocity:

1. **Policy enforcement** — Golden path enforcer gates every deploy with OPA, 6 checks, 0–100 compliance score. Hard block below 80.
2. **Self-service infrastructure** — Scaffold a production-ready service in under 4 minutes. IaC changes via form, not tickets.
3. **Platform-wide observability** — Google SRE error budget model, DORA metrics, AI-powered incident co-pilot.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                      React Frontend                      │
│         TypeScript · Vite · Tailwind · React Query       │
└────────────────────────┬────────────────────────────────┘
                         │ REST + WebSocket + GraphQL
┌────────────────────────▼────────────────────────────────┐
│                   FastAPI Gateway                        │
│         JWT Auth · Rate Limiting · OTel · CORS           │
└──┬──────┬──────┬──────┬──────┬──────┬──────┬───────────┘
   │      │      │      │      │      │      │
Catalog Enforcer Scaffold IaC  DORA  Cost   AI
   │      │      │      │      │      │      │
┌──▼──────▼──────▼──────▼──────▼──────▼──────▼──────────┐
│              Infrastructure Layer                        │
│  PostgreSQL 15   Redis 7    Neo4j 5    Temporal.io       │
│  + PgBouncer     + Sentinel + indexes  + Celery          │
│  + pgvector      + Streams                               │
│                                                          │
│  HashiCorp Vault · Prometheus · Grafana · Loki · Jaeger  │
└────────────────────────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────┐
│                  Kubernetes (minikube/EKS)               │
│      ArgoCD · Helm 3 · HPA · VPA · ResourceQuota         │
│      OPA Gatekeeper · Chaos Mesh · Trivy                 │
└────────────────────────────────────────────────────────┘
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| Frontend | React 18, TypeScript, Vite, Tailwind CSS v3, Recharts |
| Backend | FastAPI (Python 3.12), Pydantic v2, SQLAlchemy 2.0 |
| Database | PostgreSQL 15 + PgBouncer + pgvector |
| Cache / Events | Redis 7 + Sentinel HA + Redis Streams |
| Graph | Neo4j 5 |
| Workflow Engine | Temporal.io (IaC, scaffold, runbooks) + Celery (lightweight) |
| Secrets | HashiCorp Vault |
| Observability | Prometheus + Grafana + Loki + Jaeger + OTel |
| Security | Trivy + Semgrep + Syft + OPA + OPA Gatekeeper |
| AI | Anthropic Claude API + pgvector |
| IaC | Terraform Cloud + Pulumi |
| GitOps | ArgoCD + Helm 3 |
| CI | GitHub Actions |

---

## Project Structure

```
nerve-idp/
├── frontend/               # React + TypeScript (complete — runs on mock data)
├── backend/
│   ├── gateway/            # FastAPI gateway — single entry point
│   └── services/
│       ├── catalog/        # Service catalog microservice
│       ├── enforcer/       # Golden path enforcer + OPA
│       ├── scaffolding/    # App scaffolding (Temporal workflow)
│       ├── iac/            # IaC self-service (Temporal workflow)
│       ├── pipeline/       # CI/CD pipeline visibility
│       ├── blast-radius/   # Neo4j dependency graph
│       ├── error-budget/   # Prometheus burn rate + deploy freeze
│       ├── cost-intelligence/ # AWS Cost Explorer + anomaly detection
│       ├── maturity/       # Service maturity scoring engine
│       ├── security/       # Trivy, Semgrep, SBOM
│       ├── ai-copilot/     # Claude API + pgvector incident search
│       ├── docs/           # TechDocs as code
│       ├── chaos/          # Chaos Mesh integration
│       └── fleet/          # Fleet operations bulk actions
├── infra/
│   ├── k8s/                # Raw Kubernetes manifests
│   └── helm/               # Helm charts per service
├── policies/
│   ├── rego/               # OPA policy files
│   └── gatekeeper/         # OPA Gatekeeper constraint templates
├── workflows/
│   ├── temporal/           # Temporal workflow definitions
│   └── celery/             # Celery task definitions
├── docs/
│   ├── openapi.yaml        # API contract (contract-first)
│   └── adr/                # Architecture Decision Records
└── .github/
    └── workflows/          # GitHub Actions CI
```

---

## Quickstart

### Prerequisites

- Docker Desktop
- minikube
- kubectl
- helm
- Python 3.12+
- Node.js 20+

### 1. Start the frontend (mock data — no backend needed)

```bash
cd frontend
npm install
npm run dev
# http://localhost:5173
```

### 2. Start the local dev stack (Docker Compose)

```bash
docker compose up -d
# PostgreSQL, Redis, Neo4j, Vault, Temporal UI all running locally
```

### 3. Start the gateway

```bash
cd backend/gateway
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
# http://localhost:8000/docs — auto-generated OpenAPI UI
```

### 4. Run on minikube

```bash
minikube start --cpus=4 --memory=8g
helm repo add nerve ./infra/helm
helm install nerve-stack ./infra/helm/stack -f infra/helm/values.dev.yaml
kubectl port-forward svc/nerve-gateway 8000:8000
```

---

## API Contract

The full OpenAPI 3.1 spec lives at `docs/openapi.yaml`.

All backend services implement against this spec.
All frontend React Query hooks are typed against these schemas.
FastAPI auto-generates `/docs` and `/redoc` from Pydantic models that match the spec.

**Do not add endpoints that are not in the spec without updating the spec first.**

---

## Build Phases

| Phase | Scope | Status |
|---|---|---|
| Frontend | All 6 modules, 21 screens, mock data | ✅ Complete |
| Phase 1 | Foundation — gateway, DB, cache, GitOps | 🔄 In progress |
| Phase 2 | Core platform — catalog, enforcer, DORA | ⏳ Pending |
| Phase 3 | Differentiators — blast radius, error budget | ⏳ Pending |
| Phase 4 | Wow layer — AI co-pilot, chaos, TechDocs | ⏳ Pending |
| Phase 5 | Production hardening — load test, ADRs | ⏳ Pending |

---

## Capacity Model

| Configuration | Concurrent Users | Services |
|---|---|---|
| Default (out of box) | 150 | 300 |
| Tuned (4 config changes) | 2,000 | 2,000 |

The 4 config changes: PgBouncer transaction mode, pod ulimit 65535, Redis TTL on Neo4j traversals, event-driven maturity scoring.

---

## Architecture Decision Records

| ADR | Decision |
|---|---|
| [ADR-001](docs/adr/001-temporal-over-celery.md) | Temporal for IaC and scaffold — not Celery |
| [ADR-002](docs/adr/002-neo4j-blast-radius.md) | Neo4j for blast radius graph |
| [ADR-003](docs/adr/003-opa-policy-enforcement.md) | OPA for policy — two layers |
| [ADR-004](docs/adr/004-pgvector-semantic-search.md) | pgvector for semantic search |
| [ADR-005](docs/adr/005-pgbouncer-from-day-one.md) | PgBouncer from day one |
| [ADR-006](docs/adr/006-contract-first-api.md) | Contract-first API with OpenAPI spec |
