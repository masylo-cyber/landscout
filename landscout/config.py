from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel

CONFIG_DIR = Path(os.environ.get("LANDSCOUT_CONFIG", Path(__file__).resolve().parent.parent / "config"))


class Settings(BaseModel):
    database_url: str = os.environ.get("DATABASE_URL", "postgresql+psycopg://landscout:landscout@localhost:5432/landscout")
    anthropic_api_key: str = os.environ.get("ANTHROPIC_API_KEY", "")
    claude_model: str = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
    apify_token: str = os.environ.get("APIFY_TOKEN", "")
    telegram_bot_token: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_galano: str = os.environ.get("TELEGRAM_CHAT_ID_GALANO", "")
    telegram_chat_bauro: str = os.environ.get("TELEGRAM_CHAT_ID_BAURO", "")
    smtp_host: str = os.environ.get("SMTP_HOST", "")
    smtp_port: int = int(os.environ.get("SMTP_PORT", "587") or 587)
    smtp_user: str = os.environ.get("SMTP_USER", "")
    smtp_password: str = os.environ.get("SMTP_PASSWORD", "")
    mail_from: str = os.environ.get("MAIL_FROM", "")
    mail_to: str = os.environ.get("MAIL_TO", "")
    panel_user: str = os.environ.get("PANEL_USER", "admin")
    panel_password: str = os.environ.get("PANEL_PASSWORD", "admin")
    run_interval_min: int = int(os.environ.get("RUN_INTERVAL_MIN", "120") or 120)
    max_llm_per_run: int = int(os.environ.get("MAX_LLM_PER_RUN", "60") or 60)
    data_dir: Path = Path(os.environ.get("LANDSCOUT_DATA", "/app/data"))


@lru_cache
def settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s


class Area(BaseModel):
    name: str
    ring: str  # warszawa | pierscien | dalej
    center: tuple[float, float]
    radius_km: float
    olx: str | None = None


@lru_cache
def areas() -> dict[str, list[Area]]:
    raw = yaml.safe_load((CONFIG_DIR / "areas.yml").read_text(encoding="utf-8"))
    return {k: [Area(**a) for a in v] for k, v in raw.items()}


@lru_cache
def profiles() -> dict:
    return yaml.safe_load((CONFIG_DIR / "profiles.yml").read_text(encoding="utf-8"))


def olx_regions() -> list[str]:
    seen: list[str] = []
    for lst in areas().values():
        for a in lst:
            if a.olx and a.olx not in seen:
                seen.append(a.olx)
    return seen
