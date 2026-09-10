from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Runtime configuration loaded from environment / `.env`."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_path: Path = Field(default=PROJECT_ROOT / "data" / "prediction_markets.db")

    # High-interest HTTP poll interval when WebSockets are unavailable.
    poll_interval_seconds: float = 2.0
    poll_candidate_seconds: float = 15.0
    poll_inactive_seconds: float = 300.0
    discovery_interval_seconds: float = 300.0
    orderbook_snapshot_interval_seconds: float = 2.0

    http_timeout_seconds: float = 20.0
    http_max_retries: int = 5
    enabled_exchanges: str = "kalshi,polymarket"

    kalshi_base_url: str = "https://external-api.kalshi.com/trade-api/v2"
    kalshi_api_key: str | None = None
    kalshi_private_key_path: str | None = None
    polymarket_gamma_url: str = "https://gamma-api.polymarket.com"
    polymarket_clob_url: str = "https://clob.polymarket.com"

    max_markets_per_exchange: int = 0
    max_high_watchlist: int = 80
    max_candidate_watchlist: int = 120
    min_watch_liquidity: int = 10
    orderbook_min_match_score: float = 0.55
    match_candidate_min_score: float = 0.55
    match_high_confidence_min_score: float = 0.82

    candidate_date_tolerance_days: int = 90
    candidate_lexical_min_score: float = 0.32
    candidate_strong_lexical_min_score: float = 0.45
    candidate_min_score: float = 0.20
    max_candidates_per_market: int = 40

    log_level: str = "INFO"
    log_json: bool = False

    @field_validator("database_path", mode="before")
    @classmethod
    def resolve_database_path(cls, value: str | Path) -> Path:
        path = Path(value)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path

    @property
    def exchange_names(self) -> list[str]:
        return [part.strip().lower() for part in self.enabled_exchanges.split(",") if part.strip()]

    @property
    def metrics_path(self) -> Path:
        return self.database_path.parent / "collector_stats.json"


def load_settings() -> Settings:
    return Settings()
