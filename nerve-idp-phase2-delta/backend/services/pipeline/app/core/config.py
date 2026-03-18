from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class PipelineSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)

    APP_VERSION: str = "1.0.0"
    ENVIRONMENT: str = "development"

    DATABASE_URL: str = "postgresql+asyncpg://nerve_app:nerve_app_secret@localhost:6432/nerve"
    REDIS_URL: str = "redis://:nerve_redis_secret@localhost:6379/0"

    GITHUB_TOKEN: str = ""
    GITHUB_ORG: str = ""
    GITHUB_RATE_LIMIT_BUFFER: int = 100

    CATALOG_SERVICE_URL: str = "http://localhost:8001"
    POLL_INTERVAL_SECONDS: int = 15


@lru_cache
def get_settings() -> PipelineSettings:
    return PipelineSettings()


settings = get_settings()
