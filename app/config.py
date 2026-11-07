"""Settings, read from environment variables (or a .env file)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://forecast:forecast@localhost:5432/forecast"
    jwt_secret: str = Field("change-me-in-production-please-32b", min_length=32)
    jwt_issuer: str = "forecast-backend"
    access_token_minutes: int = 15
    refresh_token_days: int = 30
    allow_self_registration: bool = True

    ml_service_url: str = "http://localhost:8100"
    ml_service_token: str | None = None
    ml_timeout_seconds: float = 30.0
    reference_model_id: str = "m5-ca1-foods"

    run_worker_in_process: bool = False
    worker_poll_seconds: float = 1.0
    history_days_for_forecast: int = 365
    chart_history_days: int = 28


@lru_cache
def get_settings() -> Settings:
    return Settings()
