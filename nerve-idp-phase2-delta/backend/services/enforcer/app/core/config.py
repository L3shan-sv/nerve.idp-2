from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class EnforcerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)

    APP_VERSION: str = "1.0.0"
    ENVIRONMENT: str = "development"

    DATABASE_URL: str = "postgresql+asyncpg://nerve_app:nerve_app_secret@localhost:6432/nerve"
    REDIS_URL: str = "redis://:nerve_redis_secret@localhost:6379/0"

    OPA_URL: str = "http://localhost:8181"
    CATALOG_SERVICE_URL: str = "http://localhost:8001"
    GATEWAY_URL: str = "http://localhost:8000"

    VAULT_URL: str = "http://localhost:8200"
    VAULT_TOKEN: str = "nerve-vault-dev-token"
    VAULT_MOUNT: str = "secret"

    # GitHub token for rate limit checks in scaffold
    GITHUB_TOKEN: str = ""
    GITHUB_ORG: str = ""

    # Terraform Cloud
    TERRAFORM_CLOUD_TOKEN: str = ""

    # Template repo path (local filesystem or git path)
    TEMPLATE_REPO_PATH: str = "/templates"


@lru_cache
def get_settings() -> EnforcerSettings:
    return EnforcerSettings()


settings = get_settings()
