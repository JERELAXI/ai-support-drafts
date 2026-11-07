from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class MockSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MOCK_", extra="ignore")

    # Share of requests answered with 500 (nothing is stored).
    error_rate: float = 0.2
    # Share of requests that are stored first and answered after slow_seconds,
    # which must exceed the client's HELPDESK_TIMEOUT.
    slow_rate: float = 0.1
    slow_seconds: float = 8.0


@lru_cache
def get_settings() -> MockSettings:
    return MockSettings()
