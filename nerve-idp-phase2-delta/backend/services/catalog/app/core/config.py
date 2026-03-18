from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class CatalogSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)

    APP_VERSION: str = "1.0.0"
    ENVIRONMENT: str = "development"
    DEBUG: bool = False

    DATABASE_URL: str = "postgresql+asyncpg://nerve_app:nerve_app_secret@localhost:6432/nerve"
    DATABASE_URL_SYNC: str = "postgresql+psycopg2://nerve_app:nerve_app_secret@localhost:6432/nerve"

    REDIS_URL: str = "redis://:nerve_redis_secret@localhost:6379/0"

    NEO4J_URI: str = "bolt://localhost:7687"
    NEO4J_USER: str = "neo4j"
    NEO4J_PASSWORD: str = "nerve_neo4j_secret"
    NEO4J_DATABASE: str = "neo4j"
    NEO4J_MAX_CONNECTION_POOL_SIZE: int = 50
    NEO4J_CONNECTION_TIMEOUT: int = 30

    # Service-to-service URLs (internal cluster DNS in k8s)
    CATALOG_SERVICE_URL: str = "http://localhost:8001"
    GATEWAY_URL: str = "http://localhost:8000"


@lru_cache
def get_settings() -> CatalogSettings:
    return CatalogSettings()


settings = get_settings()
