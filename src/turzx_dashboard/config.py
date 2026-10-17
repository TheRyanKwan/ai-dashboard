# SPDX-License-Identifier: GPL-3.0-or-later
"""CLI configuration, also accepting the original flat dashboard.local.yaml."""

import math
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULTS = {
    "DEMO": False,
    "QUOTA_PER_RMB": 500000,
    "DAY_UTC_OFFSET": 8,
    "POLL_SECONDS": 20,
    "CHART_POLL_SECONDS": 2,
    "TIMEOUT_SECONDS": 5,
    "LOG_TIMEOUT_SECONDS": 20,
    "LOG_PAGE_LIMIT": 100,
    "CODEX_DONE_SECONDS": 30,
}


@dataclass
class Settings:
    dashboard: dict = field(default_factory=lambda: dict(DEFAULTS))
    fps: int = 30
    brightness: int = 60
    clip: str = ""
    ffmpeg: str = ""
    y_offset: int = 18
    rotate: int = 0
    reduced_motion: bool = False


def load_settings(path=None):
    settings = Settings()
    if path is None:
        path = Path("dashboard.local.yaml")
        if not path.exists():
            return settings
    path = Path(path).expanduser()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        raise ValueError("Invalid YAML configuration; check indentation and quoting") from None
    if not isinstance(data, dict):
        raise ValueError("Configuration must be a YAML mapping")
    # Existing dashboard settings use a flat mapping; the new format can wrap it.
    dashboard = data.get("dashboard", {k: v for k, v in data.items() if k.isupper()})
    display = data.get("display", {})
    if not isinstance(dashboard, dict) or not isinstance(display, dict):
        raise ValueError("dashboard and display must be YAML mappings")
    settings.dashboard.update(dashboard)
    for key in ("fps", "reduced_motion"):
        if key in data:
            setattr(settings, key, data[key])
    for key in ("brightness", "clip", "ffmpeg", "y_offset", "rotate"):
        if key in display:
            setattr(settings, key, display[key])
    return settings


def validate(settings):
    for key, low, high in (("fps", 1, 50), ("brightness", 0, 100), ("y_offset", 0, 18)):
        value = getattr(settings, key)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{key} must be an integer from {low} to {high}")
    if type(settings.rotate) is not int or settings.rotate not in (0, 180):
        raise ValueError("rotate must be 0 or 180")
    if type(settings.reduced_motion) is not bool:
        raise ValueError("reduced_motion must be true or false")
    for key in ("clip", "ffmpeg"):
        if not isinstance(getattr(settings, key), str):
            raise ValueError(f"{key} must be text")
    if "\x00" in settings.clip or len(settings.clip.encode("utf-8")) > 484:
        raise ValueError("clip must be a valid device filename of at most 484 UTF-8 bytes")
    dashboard = settings.dashboard
    if type(dashboard["DEMO"]) is not bool:
        raise ValueError("DEMO must be true or false")
    for key in ("QUOTA_PER_RMB", "POLL_SECONDS", "CHART_POLL_SECONDS", "TIMEOUT_SECONDS",
                "LOG_TIMEOUT_SECONDS"):
        value = dashboard[key]
        if (isinstance(value, bool) or not isinstance(value, (float, int))
                or not math.isfinite(value) or value <= 0):
            raise ValueError(f"{key} must be a positive finite number")
    offset = dashboard["DAY_UTC_OFFSET"]
    if (isinstance(offset, bool) or not isinstance(offset, (float, int))
            or not math.isfinite(offset) or not -24 < offset < 24):
        raise ValueError("DAY_UTC_OFFSET must be between -24 and 24")
    if type(dashboard["LOG_PAGE_LIMIT"]) is not int or dashboard["LOG_PAGE_LIMIT"] < 1:
        raise ValueError("LOG_PAGE_LIMIT must be a positive integer")
    for key in ("NEW_API_URL", "NEW_API_ACCESS_TOKEN", "NEW_API_COOKIE", "NEW_API_ENDPOINT",
                "NEW_API_AUTH_MODE", "NEW_API_TOKEN_ENV", "NEW_API_USER_ID_ENV",
                "CODEX_EVENT_FILE", "CODEX_SESSIONS_DIR", "CODEX_SESSION_ID"):
        if key in dashboard and not isinstance(dashboard[key], str):
            raise ValueError(f"{key} must be text")
    return settings
