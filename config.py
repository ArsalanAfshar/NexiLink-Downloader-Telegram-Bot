"""
config.py
---------
Central configuration module. All sensitive values are read exclusively
from environment variables (Railway Variables in production, or a local
`.env` file for local development).

Spotify support has been removed completely (Spotify streams are DRM
protected and cannot be legitimately downloaded). Every Spotify related
environment variable, constant and helper has been deleted from this file.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

# Loads a local .env file when present. On Railway, environment variables
# are injected directly by the platform, so this is a no-op there.
load_dotenv()


def _get_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _get_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _get_int_list(name: str) -> list[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return []
    out: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if chunk.lstrip("-").isdigit():
            out.append(int(chunk))
    return out


# ---------------------------------------------------------------------------
# Main bot (Telegram MTProto credentials)
# ---------------------------------------------------------------------------
API_ID: int = _get_int("API_ID", 0)
API_HASH: str = os.getenv("API_HASH", "").strip()
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
# NOTE: SESSION_STRING (a *personal user account* session, see
# generate_session.py) is intentionally NOT used to log in the main bot
# anymore. Telegram Inline Mode and the BotFather command menu (/setinline,
# setMyCommands) only work for genuine bot accounts logged in with a
# BOT_TOKEN — logging the main client in as a user account instead (as a
# previous revision did whenever this variable was set) silently broke both
# features, which was the root cause of "inline mode keeps loading forever".
# The variable is kept only for backward compatibility / potential future
# use and is otherwise unused by main.py.
SESSION_STRING: str = os.getenv("SESSION_STRING", "").strip()

# ---------------------------------------------------------------------------
# NexiLink Manager Bot (second, admin-only bot). It runs in the same
# process/deployment as the main bot (no extra Railway service needed).
# ---------------------------------------------------------------------------
MANAGER_BOT_TOKEN: str = os.getenv("MANAGER_BOT_TOKEN", "").strip()
MANAGER_ENABLED: bool = _get_bool("MANAGER_ENABLED", True) and bool(MANAGER_BOT_TOKEN)

# ---------------------------------------------------------------------------
# Owner / admins
# ---------------------------------------------------------------------------
OWNER_ID: int = _get_int("OWNER_ID", 0)
# Extra admins (comma separated numeric Telegram IDs) who can also use the
# Manager Bot (broadcast, stats, logs, ...). The owner always has access.
MANAGER_ADMIN_IDS: list[int] = _get_int_list("MANAGER_ADMIN_IDS")
# Chat that receives logs/reports from the Manager Bot. Defaults to OWNER_ID.
MANAGER_CHAT_ID: int = _get_int("MANAGER_CHAT_ID", 0) or OWNER_ID

# ---------------------------------------------------------------------------
# Limits & paths
# ---------------------------------------------------------------------------
MAX_FILE_SIZE: int = _get_int("MAX_FILE_SIZE", 2 * 1024 * 1024 * 1024)  # 2 GB default
DOWNLOAD_PATH: str = os.getenv("DOWNLOAD_PATH", "/tmp/downloads").strip() or "/tmp/downloads"
DB_PATH: str = os.getenv("DB_PATH", "bot_data.db").strip() or "bot_data.db"

RATE_LIMIT_COUNT: int = _get_int("RATE_LIMIT_COUNT", 5)
RATE_LIMIT_WINDOW: int = _get_int("RATE_LIMIT_WINDOW", 60)  # seconds

# Railway Free Plan has very limited CPU/RAM, keep concurrency low.
MAX_CONCURRENT_DOWNLOADS: int = _get_int("MAX_CONCURRENT_DOWNLOADS", 2)

# Carousels (Instagram/Pinterest/X) larger than this are offered as a ZIP
# suggestion by default in the choice prompt (the user can still always pick
# either option — see handlers.py `_deliver_media_result`).
ZIP_THRESHOLD_ITEMS: int = _get_int("ZIP_THRESHOLD_ITEMS", 6)

# ---------------------------------------------------------------------------
# Cooldown system: after COOLDOWN_COUNT downloads, the user must wait
# COOLDOWN_SECONDS before starting another one. All three are also
# changeable at runtime from the Manager Bot (persisted in SQLite).
# ---------------------------------------------------------------------------
COOLDOWN_ENABLED: bool = _get_bool("COOLDOWN_ENABLED", False)
COOLDOWN_COUNT: int = _get_int("COOLDOWN_COUNT", 5)
COOLDOWN_SECONDS: int = _get_int("COOLDOWN_SECONDS", 60)

LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"

DEVICE_MODEL = "NexiLink Downloader Bot"
APP_VERSION = "3.0.0"

Path(DOWNLOAD_PATH).mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Parallel upload (see fast_upload.py)
# ---------------------------------------------------------------------------
UPLOAD_MAX_CONNECTIONS: int = _get_int("UPLOAD_MAX_CONNECTIONS", 6)
UPLOAD_PARALLEL_MIN_MB: int = _get_int("UPLOAD_PARALLEL_MIN_MB", 10)

# ---------------------------------------------------------------------------
# SoundCloud
# ---------------------------------------------------------------------------
MAX_PLAYLIST_TRACKS: int = _get_int("MAX_PLAYLIST_TRACKS", 100)

# ---------------------------------------------------------------------------
# PornHub (optional, disabled easily via env var)
# ---------------------------------------------------------------------------
PORNHUB_ENABLED: bool = _get_bool("PORNHUB_ENABLED", True)
PORNHUB_NOTIFY_ADMIN: bool = _get_bool("PORNHUB_NOTIFY_ADMIN", True)

# ---------------------------------------------------------------------------
# Pinterest
# ---------------------------------------------------------------------------
PINTEREST_ENABLED: bool = _get_bool("PINTEREST_ENABLED", True)

# ---------------------------------------------------------------------------
# Instagram
# ---------------------------------------------------------------------------
INSTAGRAM_ENABLED: bool = _get_bool("INSTAGRAM_ENABLED", True)
# Optional Netscape-format cookies.txt content for private/rate-limited posts.
INSTAGRAM_COOKIES: str = os.getenv("INSTAGRAM_COOKIES", "").strip()

# ---------------------------------------------------------------------------
# X / Twitter
# ---------------------------------------------------------------------------
TWITTER_ENABLED: bool = _get_bool("TWITTER_ENABLED", True)

# ---------------------------------------------------------------------------
# Inline mode
# ---------------------------------------------------------------------------
INLINE_MODE_ENABLED: bool = _get_bool("INLINE_MODE_ENABLED", True)

# ---------------------------------------------------------------------------
# Logging / reporting (see event_logger.py + manager_bot.py)
# ---------------------------------------------------------------------------
# Default report interval in minutes. 0 means "immediately" (every event is
# forwarded to the manager chat as soon as it happens). Can be changed at
# runtime from the Manager Bot; the chosen value is persisted in SQLite.
DEFAULT_REPORT_INTERVAL_MINUTES: int = _get_int("DEFAULT_REPORT_INTERVAL_MINUTES", 10)

Path(DOWNLOAD_PATH).mkdir(parents=True, exist_ok=True)


def setup_logging() -> None:
    """Configure application-wide logging."""
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)


def validate_config() -> list[str]:
    """Validate required environment variables. Returns a list of errors."""
    errors: list[str] = []
    if not API_ID:
        errors.append("API_ID is not set.")
    if not API_HASH:
        errors.append("API_HASH is not set.")
    if not BOT_TOKEN:
        errors.append(
            "BOT_TOKEN is not set. A real bot account (created via @BotFather) is "
            "required for the main bot, since Inline Mode and the /command menu "
            "only work for bot accounts."
        )
    if not OWNER_ID:
        errors.append("OWNER_ID is not set.")
    return errors
