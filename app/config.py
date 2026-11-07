from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    celery_broker_url: str
    redis_url: str

    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_timeout: float = 30.0

    helpdesk_timeout: float = 5.0

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
