"""
main.py
-------
Application entry point. Running this file:
 1. Validates environment variables.
 2. Prepares the SQLite database.
 3. Restores admin-customized messages / limits (see admin_settings.py).
 4. Connects the main downloader bot (Telethon).
 5. Optionally connects the NexiLink Manager Bot in the *same* process
    (no extra Railway service needed).
 6. Registers all handlers and keeps the process alive.

Run with ``python main.py``. On Railway this is executed automatically
via the Dockerfile's CMD after every deploy.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from pathlib import Path

import config
import database
from telegram_client import create_client

logger = logging.getLogger(__name__)


def _cleanup_download_dir() -> None:
    """Remove leftover files from previous runs on startup."""
    path = Path(config.DOWNLOAD_PATH)
    if path.exists():
        for child in path.iterdir():
            try:
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
    path.mkdir(parents=True, exist_ok=True)


async def run() -> None:
    config.setup_logging()

    errors = config.validate_config()
    if errors:
        for err in errors:
            logger.error("Configuration error: %s", err)
        logger.error("Please set the required environment variables (see .env.example).")
        sys.exit(1)

    if not config.MANAGER_BOT_TOKEN:
        logger.warning("MANAGER_BOT_TOKEN is not set; the NexiLink Manager Bot will not start.")

    logger.info("Preparing the database...")
    await database.init_db()

    # Restore any admin-customized messages / limits saved in a previous run
    # (e.g. custom PornHub messages, max file size, cooldown settings). This
    # must run right after the database is ready and before any handler is
    # registered, so the very first message already uses the saved values.
    import admin_settings

    await admin_settings.load_overrides()

    _cleanup_download_dir()

    # Imported late to avoid any import-order issues.
    from handlers import register_handlers

    logger.info("Connecting the main bot to Telegram...")
    main_client = await create_client(device_model=config.DEVICE_MODEL)
    register_handlers(main_client)

    me = await main_client.get_me()
    logger.info("Main bot ready: @%s (id=%s)", getattr(me, "username", "?"), me.id)
    logger.info("Owner: %s", config.OWNER_ID)

    if config.INLINE_MODE_ENABLED and not getattr(me, "bot", True):
        logger.warning(
            "INLINE_MODE_ENABLED is true but the main account does not look like a bot account. "
            "Inline Mode only works for genuine Bot accounts logged in with BOT_TOKEN."
        )

    tasks = [asyncio.create_task(main_client.run_until_disconnected())]

    manager_client = None
    if config.MANAGER_ENABLED:
        from manager_bot import register_manager_handlers

        logger.info("Connecting the NexiLink Manager Bot...")
        manager_client = await create_client(
            bot_token=config.MANAGER_BOT_TOKEN,
            session_string="",
            device_model="NexiLink Manager Bot",
        )
        register_manager_handlers(manager_client, main_client)
        manager_me = await manager_client.get_me()
        logger.info("Manager bot ready: @%s (id=%s)", getattr(manager_me, "username", "?"), manager_me.id)
        tasks.append(asyncio.create_task(manager_client.run_until_disconnected()))
    else:
        logger.info("Manager Bot disabled (set MANAGER_BOT_TOKEN to enable it).")

    logger.info("NexiLink is up and running. Waiting for messages...")

    try:
        await asyncio.gather(*tasks)
    finally:
        await database.close_db()
        logger.info("Bot stopped.")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Stopped by user.")


if __name__ == "__main__":
    main()
