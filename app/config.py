"""Application settings loaded from environment / .env (pydantic-settings)."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
APP_VERSION = "0.1.0"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    youtube_api_key: str = ""
    app_token: str = "change-me"
    db_path: str = "data/app.db"

    # LLM backend for subtitle enrichment: none | claude | local
    llm_backend: str = "none"
    anthropic_api_key: str = ""
    llm_model: str = "claude-opus-5"
    local_llm_url: str = "http://127.0.0.1:8081"
    local_llm_model: str = "student-v1"
    llm_daily_cap: int = 80

    # Machine-translation fallback when no model output exists: auto | deepl | gtx | none
    mt_provider: str = "auto"
    deepl_api_key: str = ""

    # transcript fetching pace (seconds between YouTube requests); YouTube blocks bursts from one IP
    transcript_sleep_min: float = 20.0
    transcript_sleep_max: float = 40.0

    # YouTube Data API quota guard (units per day, hard stop)
    quota_daily_cap: int = 8000
    # country the videos are watched from (region-restriction filter); the PC/phone are on a German connection
    region: str = "DE"

    @property
    def db_file(self) -> Path:
        p = Path(self.db_path)
        return p if p.is_absolute() else ROOT / p

    @property
    def llm_enabled(self) -> bool:
        return self.llm_backend in ("claude", "local")


settings = Settings()
