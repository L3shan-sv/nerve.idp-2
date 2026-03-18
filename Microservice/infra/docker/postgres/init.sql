-- Nerve IDP — PostgreSQL initialization
-- Runs once on first container start

-- Extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "vector";           -- pgvector for semantic search
CREATE EXTENSION IF NOT EXISTS "pg_trgm";          -- trigram indexes for text search
CREATE EXTENSION IF NOT EXISTS "btree_gin";

-- ─────────────────────────────────────────────
-- Row-level security enforcement
-- Every table that stores service or team data
-- will have RLS policies applied by the app layer.
-- ─────────────────────────────────────────────
ALTER DATABASE nerve SET row_security = on;

-- ─────────────────────────────────────────────
-- Application roles
-- The app connects as nerve_app (not the superuser).
-- This prevents accidental DDL from application code.
-- ─────────────────────────────────────────────
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'nerve_app') THEN
    CREATE ROLE nerve_app LOGIN PASSWORD 'nerve_app_secret';
  END IF;
END
$$;

GRANT CONNECT ON DATABASE nerve TO nerve_app;
GRANT USAGE ON SCHEMA public TO nerve_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO nerve_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO nerve_app;

-- ─────────────────────────────────────────────
-- Teams table
-- Created first — services reference teams.
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS teams (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name        TEXT NOT NULL UNIQUE,
    slug        TEXT NOT NULL UNIQUE,
    budget_usd  NUMERIC(12,2) DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_teams_slug ON teams (slug);

-- ─────────────────────────────────────────────
-- Services table — the catalog source of truth
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS services (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name                    TEXT NOT NULL UNIQUE,
    team_id                 UUID NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
    language                TEXT NOT NULL CHECK (language IN ('python','go','typescript','rust','java')),
    version                 TEXT,
    repo_url                TEXT,
    description             TEXT,
    health_status           TEXT NOT NULL DEFAULT 'unknown'
                              CHECK (health_status IN ('healthy','degraded','frozen','unknown')),
    compliance_score        SMALLINT DEFAULT 0 CHECK (compliance_score BETWEEN 0 AND 100),
    maturity_score          SMALLINT DEFAULT 0 CHECK (maturity_score BETWEEN 0 AND 100),
    error_budget_consumed   NUMERIC(5,2) DEFAULT 0 CHECK (error_budget_consumed BETWEEN 0 AND 100),
    deploy_frozen           BOOLEAN NOT NULL DEFAULT FALSE,
    frozen_at               TIMESTAMPTZ,
    frozen_reason           TEXT,
    replica_count           SMALLINT DEFAULT 1,
    template_version        TEXT,                   -- scaffold template version used
    deleted_at              TIMESTAMPTZ,            -- soft delete
    last_deploy_at          TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes for catalog list queries
CREATE INDEX IF NOT EXISTS idx_services_team_id     ON services (team_id);
CREATE INDEX IF NOT EXISTS idx_services_health       ON services (health_status) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_services_language     ON services (language) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_services_frozen       ON services (deploy_frozen) WHERE deploy_frozen = TRUE;
CREATE INDEX IF NOT EXISTS idx_services_name_trgm    ON services USING gin (name gin_trgm_ops);

-- ─────────────────────────────────────────────
-- Service dependencies — mirrors Neo4j edges.
-- Neo4j is authoritative for graph traversal.
-- This table is used for the reconciliation job
-- that detects PostgreSQL ↔ Neo4j drift.
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS service_dependencies (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_id       UUID NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    target_id       UUID NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    relationship    TEXT NOT NULL DEFAULT 'DEPENDS_ON'
                      CHECK (relationship IN ('DEPENDS_ON', 'USES')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_id, target_id, relationship)
);

CREATE INDEX IF NOT EXISTS idx_deps_source ON service_dependencies (source_id);
CREATE INDEX IF NOT EXISTS idx_deps_target ON service_dependencies (target_id);

-- ─────────────────────────────────────────────
-- SLO definitions
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS slo_definitions (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_id          UUID NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    sli_type            TEXT NOT NULL CHECK (sli_type IN ('availability','latency','throughput','error_rate')),
    target              NUMERIC(6,4) NOT NULL,       -- e.g. 99.9
    window_days         SMALLINT NOT NULL DEFAULT 30,
    latency_threshold_ms INT,
    description         TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (service_id)  -- one SLO per service (can be extended later)
);

-- ─────────────────────────────────────────────
-- Deploy history
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS deploy_history (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_id          UUID NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    version             TEXT NOT NULL,
    environment         TEXT NOT NULL CHECK (environment IN ('dev','staging','production')),
    status              TEXT NOT NULL CHECK (status IN ('queued','running','succeeded','failed','blocked','rolled_back','frozen')),
    compliance_score    SMALLINT,
    actor               TEXT NOT NULL,
    notes               TEXT,
    workflow_id         TEXT,
    deployed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_deploys_service_id   ON deploy_history (service_id, deployed_at DESC);
CREATE INDEX IF NOT EXISTS idx_deploys_environment  ON deploy_history (environment);
CREATE INDEX IF NOT EXISTS idx_deploys_status       ON deploy_history (status);

-- ─────────────────────────────────────────────
-- Compliance checks — per deploy evaluation
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS compliance_checks (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    deploy_id   UUID NOT NULL REFERENCES deploy_history(id) ON DELETE CASCADE,
    service_id  UUID NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    check_name  TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('pass','warn','fail')),
    score       SMALLINT NOT NULL,
    weight      SMALLINT NOT NULL,
    detail      TEXT,
    fix_url     TEXT,
    evaluated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_checks_deploy_id ON compliance_checks (deploy_id);
CREATE INDEX IF NOT EXISTS idx_checks_service_id ON compliance_checks (service_id, evaluated_at DESC);

-- ─────────────────────────────────────────────
-- Error budget state
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS error_budgets (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_id          UUID NOT NULL REFERENCES services(id) ON DELETE CASCADE UNIQUE,
    budget_consumed     NUMERIC(5,2) NOT NULL DEFAULT 0,
    budget_remaining    NUMERIC(5,2) NOT NULL DEFAULT 100,
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS burn_rate_alerts (
    id                          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_id                  UUID NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    burn_rate                   NUMERIC(8,4) NOT NULL,
    window                      TEXT NOT NULL CHECK (window IN ('1h','6h','1d','3d')),
    severity                    TEXT NOT NULL CHECK (severity IN ('page','ticket','warning')),
    firing                      BOOLEAN NOT NULL DEFAULT TRUE,
    time_to_exhaustion_hours    NUMERIC(8,2),
    fired_at                    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at                 TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_burn_alerts_service ON burn_rate_alerts (service_id, fired_at DESC);
CREATE INDEX IF NOT EXISTS idx_burn_alerts_firing ON burn_rate_alerts (firing) WHERE firing = TRUE;

-- ─────────────────────────────────────────────
-- Maturity scores
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS maturity_scores (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_id          UUID NOT NULL REFERENCES services(id) ON DELETE CASCADE UNIQUE,
    overall_score       SMALLINT NOT NULL DEFAULT 0,
    -- Pillar scores
    observability       SMALLINT DEFAULT 0,
    reliability         SMALLINT DEFAULT 0,
    security            SMALLINT DEFAULT 0,
    docs                SMALLINT DEFAULT 0,
    cost                SMALLINT DEFAULT 0,
    error_budget_health SMALLINT DEFAULT 0,
    -- Full pillar breakdown stored as JSONB for flexibility
    pillar_detail       JSONB,
    template_behind_by  SMALLINT DEFAULT 0,
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─────────────────────────────────────────────
-- Security posture
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS security_posture (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_id          UUID NOT NULL REFERENCES services(id) ON DELETE CASCADE UNIQUE,
    score               SMALLINT DEFAULT 0,
    critical_cves       SMALLINT DEFAULT 0,
    high_cves           SMALLINT DEFAULT 0,
    medium_cves         SMALLINT DEFAULT 0,
    sbom_present        BOOLEAN DEFAULT FALSE,
    sbom_generated_at   TIMESTAMPTZ,
    sast_passed         BOOLEAN,
    network_policy_present BOOLEAN DEFAULT FALSE,
    last_scan_at        TIMESTAMPTZ,
    cve_detail          JSONB,      -- full Trivy output
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─────────────────────────────────────────────
-- Cost data
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS service_cost (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_id          UUID NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    date                DATE NOT NULL,
    amount_usd          NUMERIC(12,4) NOT NULL,
    anomaly_detected    BOOLEAN DEFAULT FALSE,
    anomaly_spike_pct   NUMERIC(8,2),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (service_id, date)
);

CREATE INDEX IF NOT EXISTS idx_cost_service_date ON service_cost (service_id, date DESC);

-- ─────────────────────────────────────────────
-- Team resource quotas
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS team_quotas (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    team_id         UUID NOT NULL REFERENCES teams(id) ON DELETE CASCADE UNIQUE,
    cpu_cores       NUMERIC(8,2) DEFAULT 0,
    cpu_used        NUMERIC(8,2) DEFAULT 0,
    memory_gb       NUMERIC(8,2) DEFAULT 0,
    memory_used     NUMERIC(8,2) DEFAULT 0,
    storage_gb      NUMERIC(8,2) DEFAULT 0,
    storage_used    NUMERIC(8,2) DEFAULT 0,
    cost_usd        NUMERIC(12,2) DEFAULT 0,
    cost_used       NUMERIC(12,2) DEFAULT 0,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─────────────────────────────────────────────
-- Collections (fleet operations)
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS collections (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name            TEXT NOT NULL UNIQUE,
    description     TEXT,
    filter_type     TEXT CHECK (filter_type IN ('team','language','tag','manual','score_below')),
    filter_value    TEXT,
    created_by      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS collection_services (
    collection_id   UUID NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    service_id      UUID NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    PRIMARY KEY (collection_id, service_id)
);

-- ─────────────────────────────────────────────
-- Fleet operations
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS fleet_operations (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    collection_id   UUID REFERENCES collections(id),
    operation_type  TEXT NOT NULL CHECK (operation_type IN ('deploy','rollback','patch','compliance_rescan')),
    status          TEXT NOT NULL CHECK (status IN ('pending_approval','running','completed','failed','cancelled')),
    total           INT NOT NULL DEFAULT 0,
    completed       INT NOT NULL DEFAULT 0,
    failed          INT NOT NULL DEFAULT 0,
    per_service     JSONB,
    actor           TEXT NOT NULL,
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─────────────────────────────────────────────
-- Runbooks
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS runbooks (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name                TEXT NOT NULL,
    service_id          UUID NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    description         TEXT,
    version             INT NOT NULL DEFAULT 1,
    actions             JSONB NOT NULL DEFAULT '[]',
    required_role       TEXT NOT NULL CHECK (required_role IN ('developer','sre','platform_engineer')),
    requires_approval   BOOLEAN DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS runbook_executions (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    runbook_id          UUID NOT NULL REFERENCES runbooks(id),
    runbook_version     INT NOT NULL,
    runbook_snapshot    JSONB NOT NULL,   -- immutable snapshot of runbook at execution time
    service_id          UUID NOT NULL REFERENCES services(id),
    status              TEXT NOT NULL CHECK (status IN ('pending_approval','running','completed','failed')),
    actor               TEXT NOT NULL,
    approved_by         TEXT,
    workflow_id         TEXT,
    notes               TEXT,
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─────────────────────────────────────────────
-- Incidents + pgvector embeddings
-- Used by: AI co-pilot similarity search
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS incidents (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_id          UUID REFERENCES services(id),
    summary             TEXT NOT NULL,
    root_cause          TEXT,
    resolution          TEXT,
    mttr_minutes        INT,
    severity            TEXT CHECK (severity IN ('p0','p1','p2','p3')),
    resolved_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- pgvector column — 1536 dims matches OpenAI/Claude embedding size
-- NOTE: create the ivfflat index AFTER bulk inserting seed data
-- Running VACUUM ANALYZE before building the index improves quality
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS embedding vector(1536);

-- Placeholder index — rebuild after seeding with real data:
--   VACUUM ANALYZE incidents;
--   CREATE INDEX idx_incidents_embedding ON incidents USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
-- This comment is intentional — the index must NOT be created on an empty table.

-- TechDocs embeddings
CREATE TABLE IF NOT EXISTS docs_pages (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_id  UUID NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    content     TEXT NOT NULL,
    content_tsv TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', coalesce(title,'') || ' ' || coalesce(content,''))) STORED,
    url         TEXT,
    built_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE docs_pages ADD COLUMN IF NOT EXISTS embedding vector(1536);

CREATE INDEX IF NOT EXISTS idx_docs_tsv ON docs_pages USING gin (content_tsv);
CREATE INDEX IF NOT EXISTS idx_docs_service ON docs_pages (service_id);

-- ─────────────────────────────────────────────
-- Audit log — append-only, never updated or deleted
-- Every action in the system writes here.
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit_log (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    actor           TEXT NOT NULL,
    action          TEXT NOT NULL,
    resource_type   TEXT NOT NULL,
    resource_id     TEXT,
    payload         JSONB,
    outcome         TEXT NOT NULL CHECK (outcome IN ('success','failure','blocked')),
    ip_address      INET,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Partition candidate for production — large table over time
CREATE INDEX IF NOT EXISTS idx_audit_actor     ON audit_log (actor, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_audit_resource  ON audit_log (resource_type, resource_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log (timestamp DESC);

-- Revoke update/delete on audit_log — append-only enforcement at DB level
REVOKE UPDATE, DELETE ON audit_log FROM nerve_app;

-- ─────────────────────────────────────────────
-- Scaffold template registry
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scaffold_templates (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name        TEXT NOT NULL,
    version     TEXT NOT NULL,
    language    TEXT NOT NULL,
    description TEXT,
    changelog   TEXT,
    is_latest   BOOLEAN DEFAULT FALSE,
    released_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (name, version)
);

-- ─────────────────────────────────────────────
-- IaC requests
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS iac_requests (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_id      UUID NOT NULL REFERENCES services(id),
    provider        TEXT NOT NULL CHECK (provider IN ('terraform','pulumi')),
    resource_type   TEXT NOT NULL,
    parameters      JSONB NOT NULL DEFAULT '{}',
    description     TEXT,
    status          TEXT NOT NULL CHECK (status IN ('pending','approved','applying','applied','failed','rejected')),
    plan_output     TEXT,
    cost_delta_usd  NUMERIC(12,4),
    submitted_by    TEXT NOT NULL,
    approved_by     TEXT,
    reject_reason   TEXT,
    workflow_id     TEXT,
    submitted_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─────────────────────────────────────────────
-- Pipeline runs
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id              TEXT PRIMARY KEY,           -- GitHub Actions run ID
    service_id      UUID NOT NULL REFERENCES services(id),
    run_number      INT NOT NULL,
    status          TEXT NOT NULL,
    triggered_by    TEXT,
    branch          TEXT,
    commit_sha      TEXT,
    stages          JSONB DEFAULT '[]',
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    duration_seconds INT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pipeline_service ON pipeline_runs (service_id, started_at DESC);

-- ─────────────────────────────────────────────
-- Chaos experiments
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS chaos_experiments (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_id          UUID NOT NULL REFERENCES services(id),
    experiment_type     TEXT NOT NULL CHECK (experiment_type IN ('pod_kill','network_latency','cpu_stress','memory_pressure')),
    status              TEXT NOT NULL CHECK (status IN ('pending_approval','approved','running','completed','failed','aborted')),
    duration_seconds    INT NOT NULL,
    parameters          JSONB DEFAULT '{}',
    environment         TEXT CHECK (environment IN ('dev','staging','production')),
    resilience_score    SMALLINT,
    approved_by         TEXT,
    workflow_id         TEXT,
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─────────────────────────────────────────────
-- Neo4j sync tracking
-- Used by the reconciliation job to detect drift
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS neo4j_sync_log (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    sync_type       TEXT NOT NULL CHECK (sync_type IN ('full','incremental','reconcile')),
    services_synced INT DEFAULT 0,
    edges_synced    INT DEFAULT 0,
    drift_detected  BOOLEAN DEFAULT FALSE,
    drift_detail    JSONB,
    duration_ms     INT,
    synced_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─────────────────────────────────────────────
-- Seed: default scaffold templates
-- ─────────────────────────────────────────────
INSERT INTO scaffold_templates (name, version, language, description, is_latest, released_at)
VALUES
    ('nerve-python', '1.0.0', 'python', 'Python FastAPI golden path template', FALSE, NOW() - INTERVAL '6 months'),
    ('nerve-python', '2.0.0', 'python', 'Python FastAPI golden path template — OTel v2', TRUE, NOW()),
    ('nerve-go', '1.0.0', 'go', 'Go Gin golden path template', FALSE, NOW() - INTERVAL '3 months'),
    ('nerve-go', '2.0.0', 'go', 'Go Gin golden path template — structured logging', TRUE, NOW()),
    ('nerve-typescript', '1.0.0', 'typescript', 'Node.js Fastify golden path template', TRUE, NOW())
ON CONFLICT (name, version) DO NOTHING;
