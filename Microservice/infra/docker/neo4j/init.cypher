// Nerve IDP — Neo4j initialization
// Run once after first start via:
//   docker exec nerve-neo4j cypher-shell -u neo4j -p nerve_neo4j_secret < /var/lib/neo4j/import/init.cypher

// ─────────────────────────────────────────────
// Constraints (also create unique indexes)
// ─────────────────────────────────────────────

// Service nodes must have unique IDs matching PostgreSQL UUIDs
CREATE CONSTRAINT service_id_unique IF NOT EXISTS
FOR (s:Service) REQUIRE s.id IS UNIQUE;

// Service names must be unique
CREATE CONSTRAINT service_name_unique IF NOT EXISTS
FOR (s:Service) REQUIRE s.name IS UNIQUE;

// Infrastructure nodes
CREATE CONSTRAINT infra_id_unique IF NOT EXISTS
FOR (i:Infra) REQUIRE i.id IS UNIQUE;

// ─────────────────────────────────────────────
// Indexes for traversal performance
// Without these, 5-hop traversal on 300+ nodes
// takes seconds instead of milliseconds.
// ─────────────────────────────────────────────

// team_id index — used in fleet collection queries
CREATE INDEX service_team_index IF NOT EXISTS
FOR (s:Service) ON (s.team_id);

// health_status index — blast radius risk assessment
CREATE INDEX service_health_index IF NOT EXISTS
FOR (s:Service) ON (s.health_status);

// language index — collection filtering
CREATE INDEX service_language_index IF NOT EXISTS
FOR (s:Service) ON (s.language);

// ─────────────────────────────────────────────
// Verify setup
// ─────────────────────────────────────────────
SHOW CONSTRAINTS;
SHOW INDEXES;
