"""Chatbot settings, from environment variables (a .env file in the project root is loaded first)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "data" / "chatbot_cache"
STATIC_DIR = Path(__file__).resolve().parent / "static"

TIMEZONE = "Asia/Nicosia"
# Greater Nicosia (south), as lon_min, lat_min, lon_max, lat_max.
NICOSIA_BBOX = (33.28, 35.10, 33.45, 35.22)
NICOSIA_CENTER = (35.1725, 33.3615)


@dataclass(frozen=True)
class Settings:
    model: str = "claude-sonnet-5-5"
    effort: str = "medium"
    max_tokens: int = 32000
    max_steps: int = 14  # model calls per user message
    web_search: bool = True

    host: str = "127.0.0.1"
    port: int = 8000

    user_agent: str = (
        "smart-nicosia-chatbot/0.1 (+https://github.com/CYENS/smart-nicosia-api-exploration)"
    )
    valhalla_url: str = "https://valhalla1.openstreetmap.de"
    nominatim_url: str = "https://nominatim.openstreetmap.org"
    photon_url: str = "https://photon.komoot.io"
    overpass_url: str = "https://overpass-api.de/api/interpreter"
    open_meteo_url: str = "https://api.open-meteo.com/v1/forecast"
    air_quality_url: str = "https://air-quality-api.open-meteo.com/v1/air-quality"

    snapshot_ttl_s: float = 300.0  # how long a device type's latest readings are reused
    fetch_concurrency: int = 8  # parallel Smart Nicosia requests when refreshing a device type


def load_settings() -> Settings:
    env = os.environ
    return Settings(
        model=env.get("CHATBOT_MODEL", Settings.model),
        effort=env.get("CHATBOT_EFFORT", Settings.effort),
        host=env.get("CHATBOT_HOST", Settings.host),
        port=int(env.get("CHATBOT_PORT") or env.get("PORT") or Settings.port),
        web_search=env.get("CHATBOT_WEB_SEARCH", "1") not in ("0", "false", "no"),
    )
