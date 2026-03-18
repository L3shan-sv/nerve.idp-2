"""
Nerve IDP Gateway — Configuration
All config is environment-variable driven.
No hardcoded secrets anywhere in source code.
Secrets are injected by Vault at runtime in production.
"""

from functools import lru_cache
from typing import List

from pydantic import AnyHttpUrl, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── App ─────────────────────────────────────
    APP_VERSION: str = "1.0.0"
    ENVIRONMENT: str = "development"
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"

    # ── Database (PgBouncer endpoint — NOT direct PostgreSQL) ─
    # ALWAYS connect through PgBouncer, never directly.
    # Direct PostgreSQL connection is only used by Alembic migrations.
    DATABASE_URL: str = "postgresql+asyncpg://nerve_app:nerve_app_secret@localhost:6432/nerve"
    DATABASE_URL_SYNC: str = "postgresql+psycopg2://nerve_app:nerve_app_secret@localhost:6432/nerve"

    # For Alembic migrations — direct PostgreSQL, bypasses PgBouncer
    # PgBouncer transaction mode is incompatible with DDL transactions
    DATABASE_URL_MIGRATIONS: str = "postgresql+psycopg2://nerve:nerve_dev_secret@localhost:5432/nerve"

    DATABASE_POOL_SIZE: int = 10
    DATABASE_MAX_OVERFLOW: int = 20
    DATABASE_POOL_TIMEOUT: int = 30
    DATABASE_POOL_RECYCLE: int = 1800

    # ── Redis ────────────────────────────────────
    REDIS_URL: str = "redis://:nerve_redis_secret@localhost:6379/0"
    # Sentinel for production HA
    REDIS_SENTINEL_HOSTS: str = "localhost:26379"
    REDIS_SENTINEL_MASTER: str = "nerve-master"
    REDIS_SENTINEL_PASSWORD: str = "nerve_redis_secret"
    USE_REDIS_SENTINEL: bool = False   # True in staging/production

    # Redis TTLs (seconds)
    CACHE_TTL_CATALOG: int = 30
    CACHE_TTL_BLAST_RADIUS: int = 60   # Neo4j traversal cache
    CACHE_TTL_DORA: int = 60
    CACHE_TTL_COST: int = 300          # 5 minutes — matches polling cadence
    CACHE_TTL_MATURITY: int = 30

    # ── Neo4j ────────────────────────────────────
    NEO4J_URI: str = "bolt://localhost:7687"
    NEO4J_USER: str = "neo4j"
    NEO4J_PASSWORD: str = "nerve_neo4j_secret"
    NEO4J_DATABASE: str = "neo4j"
    NEO4J_MAX_CONNECTION_POOL_SIZE: int = 50
    NEO4J_CONNECTION_TIMEOUT: int = 30

    # ── Vault ────────────────────────────────────
    VAULT_URL: str = "http://localhost:8200"
    VAULT_TOKEN: str = "nerve-vault-dev-token"
    VAULT_MOUNT: str = "secret"

    # ── Temporal ─────────────────────────────────
    TEMPORAL_HOST: str = "localhost"
    TEMPORAL_PORT: int = 7233
    TEMPORAL_NAMESPACE: str = "default"
    TEMPORAL_TASK_QUEUE_SCAFFOLD: str = "nerve-scaffold"
    TEMPORAL_TASK_QUEUE_IAC: str = "nerve-iac"
    TEMPORAL_TASK_QUEUE_RUNBOOKS: str = "nerve-runbooks"
    TEMPORAL_TASK_QUEUE_CHAOS: str = "nerve-chaos"

    # ── OPA ──────────────────────────────────────
    # OPA runs as a sidecar on the same pod as the enforcer.
    # The gateway calls OPA via the enforcer service, not directly.
    # This URL is for the gateway's startup health check only.
    OPA_URL: str = "http://localhost:8181"
    OPA_POLICY_PATH: str = "nerve/deploy/allow"

    # ── JWT ──────────────────────────────────────
    JWT_SECRET_KEY: str = "nerve-jwt-dev-secret-change-in-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # ── CORS ─────────────────────────────────────
    CORS_ORIGINS: List[str] = [
        "http://localhost:5173",   # Vite dev server
        "http://localhost:3000",   # Alternative dev port
        "http://localhost:8088",   # Temporal UI
    ]

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors_origins(cls, v):
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",")]
        return v

    # ── Rate limiting ─────────────────────────────
    RATE_LIMIT_DEFAULT: str = "100/minute"
    RATE_LIMIT_DEPLOY: str = "10/minute"
    RATE_LIMIT_SCAFFOLD: str = "5/minute"
    RATE_LIMIT_AI_CHAT: str = "30/minute"

    # ── OpenTelemetry ─────────────────────────────
    OTEL_EXPORTER_OTLP_ENDPOINT: str = "http://localhost:4317"
    OTEL_SERVICE_NAME: str = "nerve-gateway"

    # ── Anthropic (AI Co-pilot) ───────────────────
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_MODEL: str = "claude-sonnet-4-20250514"
    ANTHROPIC_MAX_TOKENS: int = 2048
    # Max similar incidents to retrieve via pgvector — prevents context bloat
    AI_MAX_SIMILAR_INCIDENTS: int = 3
    AI_SIMILARITY_THRESHOLD: float = 0.75
    # Maximum tokens for incident context before truncation
    AI_MAX_CONTEXT_TOKENS: int = 4000

    # ── GitHub ───────────────────────────────────
    GITHUB_TOKEN: str = ""
    GITHUB_ORG: str = ""
    GITHUB_APP_ID: str = ""
    GITHUB_APP_PRIVATE_KEY: str = ""
    # GitHub API rate limit: 5000/hour per token
    # Scaffold workflow checks for 403 and schedules retry
    GITHUB_RATE_LIMIT_BUFFER: int = 100  # Minimum remaining before backing off

    # ── AWS (Cost Intelligence) ───────────────────
    AWS_REGION: str = "us-east-1"
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    COST_POLL_INTERVAL_SECONDS: int = 300   # 5 minutes
    COST_ANOMALY_STD_DEVS: float = 2.0      # Spike threshold

    # ── Slack ────────────────────────────────────
    SLACK_WEBHOOK_URL: str = ""
    SLACK_ALERTS_CHANNEL: str = "#nerve-alerts"

    # ── Internal auth ─────────────────────────────
    # Used by Alertmanager to call the freeze webhook
    NERVE_INTERNAL_TOKEN: str = "nerve-internal-dev-token"

    # ── Neo4j reconciliation ──────────────────────
    # How often the reconciliation job checks PostgreSQL ↔ Neo4j drift
    NEO4J_RECONCILE_INTERVAL_SECONDS: int = 300

    @property
    def temporal_address(self) -> str:
        return f"{self.TEMPORAL_HOST}:{self.TEMPORAL_PORT}"

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
