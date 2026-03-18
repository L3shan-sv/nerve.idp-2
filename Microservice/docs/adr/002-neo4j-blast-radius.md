# ADR-002: Neo4j for Blast Radius Graph Traversal

**Status:** Accepted  
**Date:** 2024-01-01  
**Authors:** Platform Engineering

---

## Context

The blast radius visualizer must compute the full dependency subgraph of any
service on the platform before every deploy. The query is: "given service A,
what services (and infrastructure) would be impacted if A deploys a breaking
change?" The traversal goes up to 5 hops.

The platform targets 300 services initially, scaling to 2,000 services at the
tuned capacity ceiling.

---

## Decision

Use **Neo4j 5** as the graph database for service dependency relationships.

PostgreSQL stores canonical service data (service catalog). Neo4j stores the
dependency graph. A reconciliation job runs every 5 minutes to detect and
correct drift between the two.

---

## Why not PostgreSQL for the graph?

A 5-hop traversal on a relational database requires recursive CTEs:

```sql
WITH RECURSIVE blast_radius AS (
  SELECT source_id, target_id, 1 AS hop
  FROM service_dependencies
  WHERE source_id = $1
  UNION ALL
  SELECT sd.source_id, sd.target_id, br.hop + 1
  FROM service_dependencies sd
  JOIN blast_radius br ON sd.source_id = br.target_id
  WHERE br.hop < 5
)
SELECT DISTINCT target_id FROM blast_radius;
```

On 300 services with an average of 5 dependencies each (1,500 edges), this
query takes ~50ms with a proper index. On 2,000 services with 10,000 edges,
the recursive CTE's plan degrades — the query planner cannot always use the
index across recursive iterations. Benchmarks on similar schemas show 500ms–2s
query times at 2,000 nodes, which is unacceptable for a pre-deploy gate.

Neo4j's native graph traversal on the same dataset runs in <10ms with proper
indexes. The Cypher query is also significantly more readable:

```cypher
MATCH path = (start:Service {id: $serviceId})-[:DEPENDS_ON|USES*1..5]->(dep)
RETURN dep, length(path) AS hop_distance
```

---

## Neo4j ↔ PostgreSQL consistency model

**Neo4j is authoritative for graph traversal only.**  
PostgreSQL is authoritative for all service metadata.

When a service is registered, updated, or deleted in PostgreSQL, the catalog
service publishes a change event to Redis Streams. A Celery worker consumes
this stream and applies the change to Neo4j.

**What happens if the sync fails?**

A reconciliation Celery beat task runs every 5 minutes:
1. Fetch all service IDs and dependency edges from PostgreSQL
2. Fetch all Service nodes and DEPENDS_ON/USES edges from Neo4j
3. Diff the two sets
4. If drift detected:
   - Write the drift to `neo4j_sync_log` table in PostgreSQL
   - Apply the missing changes to Neo4j
   - Alert if drift is large (>10 services out of sync)

This ensures that blast radius results are at most 5 minutes stale in the
worst case (Redis Streams consumer crash + reconciliation not yet run).

---

## Index requirements

Two indexes are mandatory. Without them, traversal degrades to full graph scan:

```cypher
CREATE INDEX service_team_index FOR (s:Service) ON (s.team_id);
CREATE INDEX service_health_index FOR (s:Service) ON (s.health_status);
```

The init.cypher script in `infra/docker/neo4j/` creates these on first start.

**Adding a new index requires verifying traversal performance with EXPLAIN:**

```cypher
EXPLAIN MATCH (s:Service {id: $id})-[:DEPENDS_ON*1..5]->(dep)
RETURN dep
```

The plan should show `NodeIndexSeek` on the first node, not `AllNodesScan`.

---

## Redis cache on traversal results

Blast radius traversal results are cached in Redis with a 60-second TTL:

```
Key: blast_radius:{service_id}:{hops}
TTL: 60 seconds
```

A cache hit is indicated in the API response (`"cached": true`).

Cache is invalidated when a service dependency changes (catalog change event).

This means repeated blast radius queries for the same service (common during
deploy review) are served from Redis in <1ms rather than Neo4j in ~10ms.

---

## Capacity validation

| Services | Edges | Neo4j traversal (5 hops) | PostgreSQL recursive CTE |
|----------|-------|--------------------------|--------------------------|
| 300 | 1,500 | ~8ms | ~50ms |
| 1,000 | 5,000 | ~12ms | ~200ms |
| 2,000 | 10,000 | ~18ms | ~800ms |
| 2,000 + cache | 10,000 | <1ms (Redis hit) | N/A |

Benchmarks run with Neo4j 5.18 Community, heap 1GB, pagecache 512MB.

---

## Heap sizing

In the Docker Compose local stack:
```
NEO4J_server_memory_heap_initial__size: 512m
NEO4J_server_memory_heap_max__size: 1G
NEO4J_server_memory_pagecache__size: 512m
```

In production (Helm values):
```yaml
neo4j:
  env:
    NEO4J_server_memory_heap_max__size: "4G"
    NEO4J_server_memory_pagecache__size: "2G"
```

Undersized heap causes frequent GC pauses that show up as traversal latency
spikes. Size heap to fit the full graph in memory for production deployments.
