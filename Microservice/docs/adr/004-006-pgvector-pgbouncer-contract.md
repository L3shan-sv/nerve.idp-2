# ADR-004: pgvector for Semantic Search

**Status:** Accepted  
**Date:** 2024-01-01

---

## Context

The AI co-pilot needs to find semantically similar past incidents and relevant
TechDocs content given an incident description. TechDocs search needs both
keyword (full-text) and semantic (embedding) search.

Options evaluated: **pgvector** (PostgreSQL extension), **Pinecone**, **Weaviate**,
**Qdrant**.

---

## Decision

Use **pgvector** as the vector store, colocated with the existing PostgreSQL instance.

---

## Rationale

**Stack consolidation.** pgvector is a PostgreSQL extension — it runs inside the
same PostgreSQL instance already in the stack. No additional service to deploy,
monitor, or back up. For a platform already running PostgreSQL, adding pgvector
is a one-line init SQL command (`CREATE EXTENSION vector`).

**Combined queries.** The AI co-pilot retrieval query needs to join incident
embeddings with service metadata (team, severity, resolved_at). In pgvector this
is a single SQL query with a cosine similarity ORDER BY. In a separate vector DB,
this requires a similarity search, then a second query to PostgreSQL to fetch
metadata, then a join in application code. pgvector eliminates this round-trip.

**Full-text + semantic hybrid search.** TechDocs search benefits from both
keyword search (exact service name, error code) and semantic search (conceptual
similarity). PostgreSQL's built-in `tsvector` handles keyword search.
pgvector handles semantic search. A hybrid ranking function can combine both
scores in a single query. A dedicated vector DB would require maintaining two
separate indexes and merging results in application code.

---

## Context window management for AI co-pilot

The retrieval strategy must prevent context bloat. With 500 services and 2 years
of incident history, an unconstrained similarity search could return hundreds of
results that exceed Claude's context window.

Constraints applied:
- **Top-k:** Maximum 3 similar incidents returned (configurable via `AI_MAX_SIMILAR_INCIDENTS`)
- **Similarity threshold:** Only incidents with cosine similarity > 0.75 are included
- **Token budget:** Total incident context is capped at 4,000 tokens before
  being passed to Claude. If the context exceeds this, incidents are truncated
  from least-similar to most-similar.
- **TechDocs retrieval:** Maximum 2 relevant runbook excerpts, capped at 1,000 tokens each

---

## Index management

The `ivfflat` index on the `embedding` column **must not** be created on an empty
table. An index built on zero rows has zero lists and is useless. The correct
sequence:

1. Insert seed incident data (or wait for real incidents to accumulate)
2. Run `VACUUM ANALYZE incidents`
3. Create the index: `CREATE INDEX ... USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)`
4. Set `lists` to `rows / 1000` (minimum 100)

This is documented in the init SQL with a comment block. The index creation is
a separate Alembic migration that runs after the seed data migration.

---

# ADR-005: PgBouncer from Day One

**Status:** Accepted  
**Date:** 2024-01-01

---

## Context

PostgreSQL has a hard limit on concurrent connections (`max_connections`). Each
connection consumes ~5-10MB of memory. FastAPI with async SQLAlchemy creates
connections on demand. Without pooling, the platform hits its connection wall
at approximately 150 concurrent users.

---

## Decision

**PgBouncer is mandatory from day one** in transaction pooling mode.

Application code connects to PgBouncer on port 6432. PgBouncer maintains a small
pool of real PostgreSQL connections and multiplexes application connections through
them. The application sees unlimited connections; PostgreSQL sees a controlled pool.

---

## Configuration

```ini
# Transaction mode — compatible with async SQLAlchemy
pool_mode = transaction

# Application-side limits
max_client_conn = 1000      # Maximum connections PgBouncer will accept
default_pool_size = 20      # Real PostgreSQL connections per database/user pair

# Reliability
reserve_pool_size = 5       # Extra connections for bursts
reserve_pool_timeout = 3    # Seconds before using reserve pool
server_idle_timeout = 600   # Disconnect idle server connections after 10 minutes
```

**Critical:** SQLAlchemy must use `NullPool` when connecting through PgBouncer.
Using SQLAlchemy's own connection pool on top of PgBouncer creates double-pooling:
SQLAlchemy holds connections open in its pool while PgBouncer also holds them
open. The result is connection exhaustion at the PostgreSQL level.

```python
# Correct — NullPool delegates pooling entirely to PgBouncer
engine = create_async_engine(DATABASE_URL, pool_class=NullPool)
```

---

## Alembic migrations

Alembic migrations run DDL statements inside transactions. PgBouncer transaction
mode is incompatible with DDL transactions (session-level locking required).

**Alembic must connect directly to PostgreSQL, not PgBouncer.**

Two separate DATABASE_URL environment variables:
- `DATABASE_URL` → PgBouncer (port 6432) — used by the application
- `DATABASE_URL_MIGRATIONS` → PostgreSQL direct (port 5432) — used by Alembic only

---

## Capacity model

| Configuration | Max concurrent users | PostgreSQL connections used |
|---|---|---|
| No pooling (direct PostgreSQL) | ~150 | 150 |
| PgBouncer transaction mode | 2,000+ | 20 (default_pool_size) |

---

# ADR-006: Contract-First API with OpenAPI 3.1

**Status:** Accepted  
**Date:** 2024-01-01

---

## Context

The frontend was built before the backend. TypeScript types in `src/types/index.ts`
define the data shapes the frontend expects. The backend must return data that
matches these shapes exactly, or the frontend will break in subtle ways when
mock data is swapped for real API calls.

---

## Decision

**Write the OpenAPI spec first. Write Pydantic models that match the spec.
Never add an endpoint without updating the spec.**

The spec lives at `docs/openapi.yaml` and is the single source of truth for:
- All 24 REST endpoints
- All 3 WebSocket endpoint contracts
- The GraphQL endpoint placeholder
- All request/response schemas

FastAPI auto-generates `/docs` (Swagger UI) and `/redoc` from Pydantic models.
The generated schema must match `docs/openapi.yaml`. A CI step validates this:

```bash
# Generate schema from running FastAPI app
curl http://localhost:8000/api/v1/openapi.json > generated.json

# Compare against committed spec (after converting YAML to JSON)
python -c "import yaml,json,sys; print(json.dumps(yaml.safe_load(open('docs/openapi.yaml'))))" > expected.json

# Diff — any discrepancy fails CI
diff <(jq -S . generated.json) <(jq -S . expected.json)
```

---

## Frontend connection strategy

Every frontend screen currently imports from `src/data/mock.ts`. When the backend
is ready, the swap is mechanical:

```typescript
// Before
import { services } from "../data/mock";

// After
const { data: services } = useQuery({
  queryKey: ["services"],
  queryFn: () => api.get("/services"),
  staleTime: 30_000,  // 30 seconds — matches CACHE_TTL_CATALOG
});
```

React Query stale times must be set to match the backend cache TTLs defined in
`app/core/config.py`. This prevents unnecessary re-fetching and prevents
hammering the backend on every navigation event.

| Data type | staleTime | Backend cache TTL |
|---|---|---|
| Service catalog | 30s | CACHE_TTL_CATALOG = 30s |
| Blast radius | 60s | CACHE_TTL_BLAST_RADIUS = 60s |
| DORA metrics | 60s | CACHE_TTL_DORA = 60s |
| Cost data | 5min | CACHE_TTL_COST = 300s |
| Maturity scores | 30s | CACHE_TTL_MATURITY = 30s |
