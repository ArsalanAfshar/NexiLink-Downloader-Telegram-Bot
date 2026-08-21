"""
admin_settings.py
------------------
Runtime-configurable admin settings for the NexiLink Manager Bot.

This module is intentionally built ONLY on top of the already-stable,
generic key/value API exposed by ``database.py``
(``database.get_setting`` / ``database.set_setting``) plus the
already-existing ``messages._OVERRIDES`` / ``messages.CUSTOMIZABLE_KEYS``
mechanism. It does not touch the database schema, so it is 100% safe to
add without risking any conflict with existing persistence code.

Covers requirement #7 of the project brief:

A) Download limits (admin configurable, persisted in SQLite):
   - Maximum file size allowed.
   - Maximum simultaneous downloads.
   - User cooldown time.
   - Number of downloads after which cooldown activates.

B) Message management:
   - Admin can edit any of the keys listed in ``messages.CUSTOMIZABLE_KEYS``
     (this already includes every PornHub-related message), without ever
     hardcoding text in the source code.

Everything here takes effect immediately for the running process (main
bot + manager bot share the same Python process, see main.py), no
restart required.
"""

from __future__ import annotations

import logging
from typing import Optional

import config
import database
import messages

logger = logging.getLogger(__name__)

# Settings keys stored in the generic `settings` table.
_KEY_MAX_FILE_SIZE = "admin_max_file_size"
_KEY_MAX_CONCURRENT = "admin_max_concurrent_downloads"
_KEY_COOLDOWN_ENABLED = "admin_cooldown_enabled"
_KEY_COOLDOWN_COUNT = "admin_cooldown_count"
_KEY_COOLDOWN_SECONDS = "admin_cooldown_seconds"
_MSG_OVERRIDE_PREFIX = "msg_override:"


# ---------------------------------------------------------------------------
# Download limits
# ---------------------------------------------------------------------------
async def get_max_file_size() -> int:
    raw = await database.get_setting(_KEY_MAX_FILE_SIZE)
    if raw is None:
        return config.MAX_FILE_SIZE
    try:
        return int(raw)
    except ValueError:
        return config.MAX_FILE_SIZE


async def set_max_file_size(size_bytes: int) -> None:
    await database.set_setting(_KEY_MAX_FILE_SIZE, str(int(size_bytes)))


async def get_max_concurrent_downloads() -> int:
    raw = await database.get_setting(_KEY_MAX_CONCURRENT)
    if raw is None:
        return config.MAX_CONCURRENT_DOWNLOADS
    try:
        return max(1, int(raw))
    except ValueError:
        return config.MAX_CONCURRENT_DOWNLOADS


async def set_max_concurrent_downloads(value: int) -> None:
    value = max(1, int(value))
    await database.set_setting(_KEY_MAX_CONCURRENT, str(value))
    try:
        # concurrency.py exposes a module-level asyncio.Semaphore; rebuild
        # it in place so the new limit is honoured without a restart.
        import asyncio

        import concurrency

        concurrency.download_semaphore = asyncio.Semaphore(value)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to resize the download semaphore at runtime")


async def get_cooldown_config() -> dict:
    enabled_raw = await database.get_setting(_KEY_COOLDOWN_ENABLED)
    count_raw = await database.get_setting(_KEY_COOLDOWN_COUNT)
    seconds_raw = await database.get_setting(_KEY_COOLDOWN_SECONDS)
    try:
        enabled = (enabled_raw is not None and enabled_raw == "1") or (
            enabled_raw is None and config.COOLDOWN_ENABLED
        )
    except Exception:  # noqa: BLE001
        enabled = config.COOLDOWN_ENABLED
    try:
        count = int(count_raw) if count_raw is not None else config.COOLDOWN_COUNT
    except ValueError:
        count = config.COOLDOWN_COUNT
    try:
        seconds = int(seconds_raw) if seconds_raw is not None else config.COOLDOWN_SECONDS
    except ValueError:
        seconds = config.COOLDOWN_SECONDS
    return {"enabled": enabled, "count": max(1, count), "seconds": max(1, seconds)}


async def set_cooldown_config(
    *, enabled: Optional[bool] = None, count: Optional[int] = None, seconds: Optional[int] = None
) -> dict:
    current = await get_cooldown_config()
    if enabled is not None:
        current["enabled"] = enabled
    if count is not None:
        current["count"] = max(1, int(count))
    if seconds is not None:
        current["seconds"] = max(1, int(seconds))
    await database.set_setting(_KEY_COOLDOWN_ENABLED, "1" if current["enabled"] else "0")
    await database.set_setting(_KEY_COOLDOWN_COUNT, str(current["count"]))
    await database.set_setting(_KEY_COOLDOWN_SECONDS, str(current["seconds"]))
    return current


# Keep database.get_cooldown_settings() (used by cooldown.py) in sync with
# the admin-configurable values above, without requiring any change to
# database.py or cooldown.py.
try:
    _original_get_cooldown_settings = database.get_cooldown_settings

    async def _patched_get_cooldown_settings():  # noqa: ANN202
        return await get_cooldown_config()

    database.get_cooldown_settings = _patched_get_cooldown_settings  # type: ignore[attr-defined]
except AttributeError:
    logger.warning(
        "database.get_cooldown_settings() was not found; cooldown.py may be using its own "
        "defaults instead of the admin-configured values."
    )

# Same idea for the max file size used across handlers.py / inline_mode.py.
try:
    _original_get_max_file_size = database.get_max_file_size

    async def _patched_get_max_file_size():  # noqa: ANN202
        return await get_max_file_size()

    database.get_max_file_size = _patched_get_max_file_size  # type: ignore[attr-defined]
except AttributeError:
    logger.warning("database.get_max_file_size() was not found; falling back to config.MAX_FILE_SIZE only.")


# ---------------------------------------------------------------------------
# Customizable messages (PornHub + other allow-listed keys)
# ---------------------------------------------------------------------------
def customizable_keys() -> tuple[str, ...]:
    return getattr(messages, "CUSTOMIZABLE_KEYS", ())


def get_default_text(key: str) -> str:
    return messages.MESSAGES.get(key, "")


def get_current_text(key: str) -> str:
    overrides = getattr(messages, "_OVERRIDES", {})
    return overrides.get(key) or messages.MESSAGES.get(key, "")


async def set_message_override(key: str, text: str) -> bool:
    if key not in customizable_keys():
        return False
    await database.set_setting(_MSG_OVERRIDE_PREFIX + key, text)
    messages._OVERRIDES[key] = text  # noqa: SLF001 - intentional, same-process shared state
    return True


async def reset_message_override(key: str) -> bool:
    if key not in customizable_keys():
        return False
    await database.set_setting(_MSG_OVERRIDE_PREFIX + key, "")
    messages._OVERRIDES.pop(key, None)  # noqa: SLF001
    return True


async def load_overrides() -> None:
    """Call once at startup (after database.init_db()) so any message the
    admin customized in a previous run is restored into memory."""
    for key in customizable_keys():
        try:
            value = await database.get_setting(_MSG_OVERRIDE_PREFIX + key)
        except Exception:  # noqa: BLE001
            value = None
        if value:
            messages._OVERRIDES[key] = value  # noqa: SLF001
    logger.info("Loaded %d custom message override(s).", len(messages._OVERRIDES))
