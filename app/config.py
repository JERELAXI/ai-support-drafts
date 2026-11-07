from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    celery_broker_url: str
    redis_url: str

    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_timeout: float = 30.0
    llm_base_url: str = "https://api.openai.com/v1"
    llm_rate_limit: int = 60

    helpdesk_timeout: float = 5.0

    # HTTP Basic Auth for /operator; required so the UI never starts unprotected.
    operator_user: str = Field(min_length=1)
    operator_password: str = Field(min_length=1)

    # Async pool of the API process (one uvicorn worker).
    api_db_pool_size: int = 20
    api_db_max_overflow: int = 10

    worker_concurrency: int = 20

    max_generate_attempts: int = 5
    max_send_attempts: int = 5

    @property
    def async_database_url(self) -> str:
        return self._with_driver("postgresql+asyncpg")

    @property
    def sync_database_url(self) -> str:
        return self._with_driver("postgresql+psycopg")

    def _with_driver(self, driver: str) -> str:
        _, _, rest = self.database_url.partition("://")
        return f"{driver}://{rest}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
