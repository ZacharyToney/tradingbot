from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    alpaca_api_key: str = Field(..., min_length=1)
    alpaca_secret: str = Field(..., min_length=1)
    alpaca_base_url: str = "https://paper-api.alpaca.markets"

    # Equity bar feed. Free Basic plan = "iex" (real-time IEX-exchange only).
    # Paid Algo Trader Plus ($99/mo) = "sip" (real-time full tape) or "delayed_sip".
    equity_data_feed: Literal["iex", "delayed_sip", "sip"] = "iex"

    trading_mode: Literal["paper", "live"] = "paper"

    db_path: str = "tradingbot.db"

    max_position_pct: float = 0.05
    max_concurrent_positions: int = 3
    daily_loss_limit_pct: float = 0.02
    total_dd_limit_pct: float = 0.10

    poll_interval_seconds: int = 15
    clock_skew_max_seconds: int = 5
    equity_drift_max_pct: float = 0.01

    log_level: str = "INFO"
    log_dir: str = "logs"

    @property
    def is_paper(self) -> bool:
        return self.trading_mode == "paper"

    @property
    def db_full_path(self) -> Path:
        p = Path(self.db_path)
        return p if p.is_absolute() else REPO_ROOT / p

    @property
    def log_dir_path(self) -> Path:
        p = Path(self.log_dir)
        return p if p.is_absolute() else REPO_ROOT / p


def load_settings(env_file: str | Path | None = None) -> Settings:
    """Load Settings, optionally from a specific .env-style file.

    Defaults to the repo's `.env` (matches Settings.model_config). The override lets
    Track B's chaos sandbox load from `.env.chaos` so it talks to a different Alpaca
    paper account and writes to a different SQLite DB. Falls back to the
    `TRADINGBOT_ENV_FILE` env var if no explicit path is given — that's what the
    systemd unit uses.
    """
    import os

    chosen = env_file or os.environ.get("TRADINGBOT_ENV_FILE")
    if chosen:
        chosen_path = Path(chosen)
        if not chosen_path.is_absolute():
            chosen_path = REPO_ROOT / chosen_path
        return Settings(_env_file=str(chosen_path))  # type: ignore[call-arg]
    return Settings()  # type: ignore[call-arg]
