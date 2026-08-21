"""
manager_bot.py
---------------
NexiLink Manager Bot: a second, admin-only Telegram bot that runs *inside
the same process* as the main downloader bot (no extra Railway service is
required — both share the same container, event loop and SQLite database).

Features
--------
- `/stats` — totals (users, downloads, success/fail).
- `/logs` — the most recent download events.
- `/interval` — choose how often aggregated reports are sent (immediately /
  5 / 10 / 30 / 60 minutes), persisted in SQLite.
- `/limits` — configure download limits at runtime (NEW):
    * Maximum file size allowed.
    * Maximum simultaneous downloads.
    * User cooldown time.
    * Number of downloads after which cooldown activates.
- `/messages` — customize user-facing bot messages at runtime (NEW),
  especially the PornHub-related messages (link received, download
  started/finished, error, warnings, ...). Nothing is hardcoded: every
  editable key lives in `messages.CUSTOMIZABLE_KEYS` and is applied
  immediately (same process, no redeploy needed).
- `/broadcast` — owner-only broadcast to every registered user, with a
  confirmation step, live progress and a final report. Sending happens
  through the *main* bot client, since that's the bot users have actually
  started a chat with.
- `/ping` — quick liveness/uptime check.

The bot consumes `event_logger.shared` (an `asyncio.Queue`) to receive live
events from the main bot and either forwards them immediately or
aggregates them into a periodic report, based on the configured interval.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from typing import Optional

from telethon import Button, TelegramClient, errors, events

import admin_settings
import config
import database
import messages
from event_logger import Event, EventType, shared as event_logger

logger = logging.getLogger(__name__)

_INTERVAL_OPTIONS = [
    (0, "Immediately"),
    (5, "5 minutes"),
    (10, "10 minutes"),
    (30, "30 minutes"),
    (60, "1 hour"),
]

_START_TIME = time.time()

# owner_id -> {"action": "broadcast_confirm", "text": str}
_pending_broadcast: dict[int, dict] = {}

# admin_id -> {"action": "set_max_file_size" | "set_cooldown_count" |
#              "set_cooldown_seconds" | "set_max_concurrent" |
#              "set_message", "key": Optional[str]}
# Simple one-shot "reply with a value" state machine, kept in memory only.
_pending_input: dict[int, dict] = {}

_MAX_FILE_SIZE_PRESETS_MB = [250, 500, 1000, 1500, 2000, 4000]
_MAX_CONCURRENT_PRESETS = [1, 2, 3, 4, 5]
_COOLDOWN_COUNT_PRESETS = [3, 5, 10, 15, 20]
_COOLDOWN_SECONDS_PRESETS = [30, 60, 120, 300, 600]


def _is_admin(user_id: int) -> bool:
    if config.OWNER_ID and user_id == config.OWNER_ID:
        return True
    return user_id in config.MANAGER_ADMIN_IDS


def _format_event(event: Event) -> str:
    icons = {
        EventType.NEW_USER: "🆕",
        EventType.DOWNLOAD_REQUEST: "📥",
        EventType.DOWNLOAD_SUCCESS: "✅",
        EventType.DOWNLOAD_FAILED: "❌",
        EventType.ERROR: "⚠️",
    }
    icon = icons.get(event.type, "ℹ️")
    ts = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(event.timestamp))
    lines = [f"{icon} **{event.type.value}**"]
    if event.user_id:
        who = f"`{event.user_id}`"
        if event.username:
            who += f" (@{event.username})"
        lines.append(f"👤 User: {who}")
    if event.platform:
        lines.append(f"🌐 Platform: {event.platform}")
    if event.url:
        lines.append(f"🔗 URL: {event.url}")
    if event.quality:
        lines.append(f"🎚 Quality: {event.quality}")
    if event.file_size:
        from utils import format_bytes

        lines.append(f"📦 Size: {format_bytes(event.file_size)}")
    if event.error:
        lines.append(f"🧯 Error: {event.error}")
    lines.append(f"🕒 {ts}")
    return "\n".join(lines)


async def _consume_events(manager_client: TelegramClient) -> None:
    """Background task: either forward events immediately, or aggregate
    them for a periodic report, depending on the current setting."""
    counters: Counter[str] = Counter()
    window_start = time.time()

    async def flush_report() -> None:
        nonlocal counters, window_start
        if not config.MANAGER_CHAT_ID:
            counters.clear()
            window_start = time.time()
            return
        total = counters.get(EventType.DOWNLOAD_REQUEST.value, 0)
        success = counters.get(EventType.DOWNLOAD_SUCCESS.value, 0)
        failed = counters.get(EventType.DOWNLOAD_FAILED.value, 0)
        new_users = counters.get(EventType.NEW_USER.value, 0)
        if total or success or failed or new_users:
            interval = await database.get_report_interval_minutes()
            period = next((label for m, label in _INTERVAL_OPTIONS if m == interval), f"{interval} minutes")
            text = (
                "📊 **NexiLink Report**\n\n"
                f"Time period:\n{period}\n\n"
                f"New users:\n{new_users}\n\n"
                f"Downloads:\n{total}\n\n"
                f"Successful:\n{success}\n\n"
                f"Failed:\n{failed}"
            )
            try:
                await manager_client.send_message(config.MANAGER_CHAT_ID, text)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to send the periodic report")
        counters.clear()
        window_start = time.time()

    async def periodic_flusher() -> None:
        while True:
            interval = await database.get_report_interval_minutes()
            if interval <= 0:
                await asyncio.sleep(5)
                continue
            await asyncio.sleep(min(interval * 60, 3600))
            if time.time() - window_start >= interval * 60 - 1:
                await flush_report()

    flusher_task = asyncio.create_task(periodic_flusher())
    try:
        async for event in event_logger.events():
            counters[event.type.value] += 1
            interval = await database.get_report_interval_minutes()
            if interval <= 0 and config.MANAGER_CHAT_ID:
                try:
                    await manager_client.send_message(config.MANAGER_CHAT_ID, _format_event(event))
                except errors.FloodWaitError as exc:
                    await asyncio.sleep(exc.seconds)
                except Exception:  # noqa: BLE001
                    logger.exception("Failed to forward a live event")
                counters.clear()
    finally:
        flusher_task.cancel()


# ---------------------------------------------------------------------------
# Basic commands
# ---------------------------------------------------------------------------
async def _cmd_start(event) -> None:
    if not _is_admin(event.sender_id):
        return
    await event.respond(
        "🛠 **NexiLink Manager Bot**\n\n"
        "/stats — bot statistics\n"
        "/logs — recent download events\n"
        "/interval — configure report frequency\n"
        "/limits — configure download limits & cooldown\n"
        "/messages — customize bot messages (incl. PornHub)\n"
        "/broadcast <text> — message all users\n"
        "/ping — uptime check"
    )


async def _cmd_ping(event) -> None:
    if not _is_admin(event.sender_id):
        return
    uptime = int(time.time() - _START_TIME)
    hours, rem = divmod(uptime, 3600)
    minutes, seconds = divmod(rem, 60)
    await event.respond(f"🏓 Pong!\n⏱ Uptime: {hours}h {minutes}m {seconds}s")


async def _cmd_stats(event) -> None:
    if not _is_admin(event.sender_id):
        return
    stats = await database.get_stats()
    await event.respond(
        "📊 **Bot statistics**\n\n"
        f"👥 Total users: {stats['total_users']}\n"
        f"⬇️ Total downloads: {stats['total_downloads']}\n"
        f"✅ Successful: {stats['successful_downloads']}\n"
        f"❌ Failed: {stats['failed_downloads']}"
    )


async def _cmd_logs(event) -> None:
    if not _is_admin(event.sender_id):
        return
    rows = await database.get_recent_downloads(15)
    if not rows:
        await event.respond("No download events yet.")
        return
    lines = ["🧾 **Recent downloads**\n"]
    for row in rows:
        ts = time.strftime("%m-%d %H:%M", time.localtime(row["created_at"]))
        status_icon = {"success": "✅", "failed": "❌", "requested": "📥", "cancelled": "🛑"}.get(
            row["status"], "ℹ️"
        )
        lines.append(f"{status_icon} [{ts}] user `{row['user_id']}` — {row['platform'] or 'unknown'}")
    await event.respond("\n".join(lines))


async def _cmd_interval(event) -> None:
    if not _is_admin(event.sender_id):
        return
    current = await database.get_report_interval_minutes()
    buttons = [
        [Button.inline(f"{'✅ ' if m == current else ''}{label}", f"ivl|{m}")] for m, label in _INTERVAL_OPTIONS
    ]
    await event.respond("⏱ Choose the report interval:", buttons=buttons)


# ---------------------------------------------------------------------------
# /limits — download limits & cooldown configuration (NEW)
# ---------------------------------------------------------------------------
async def _cmd_limits(event) -> None:
    if not _is_admin(event.sender_id):
        return
    await _send_limits_menu(event)


async def _send_limits_menu(event_or_msg) -> None:
    max_size = await admin_settings.get_max_file_size()
    max_concurrent = await admin_settings.get_max_concurrent_downloads()
    cooldown_cfg = await admin_settings.get_cooldown_config()
    from utils import format_bytes

    text = (
        "⚙️ **Download limits**\n\n"
        f"📦 Max file size: {format_bytes(max_size)}\n"
        f"🔀 Max simultaneous downloads: {max_concurrent}\n"
        f"🧊 Cooldown enabled: {'yes' if cooldown_cfg['enabled'] else 'no'}\n"
        f"🔢 Downloads before cooldown: {cooldown_cfg['count']}\n"
        f"⏳ Cooldown duration: {cooldown_cfg['seconds']} sec\n\n"
        "Choose what to change:"
    )
    buttons = [
        [Button.inline("📦 Max file size", "lim|size")],
        [Button.inline("🔀 Max concurrent downloads", "lim|conc")],
        [Button.inline(f"🧊 Cooldown: {'disable' if cooldown_cfg['enabled'] else 'enable'}", "lim|toggle_cd")],
        [Button.inline("🔢 Downloads before cooldown", "lim|cdcount")],
        [Button.inline("⏳ Cooldown duration", "lim|cdsec")],
    ]
    if hasattr(event_or_msg, "respond"):
        await event_or_msg.respond(text, buttons=buttons)
    else:
        await event_or_msg.edit(text, buttons=buttons)


async def _limits_callback(event: events.CallbackQuery.Event, action: str) -> None:
    from utils import format_bytes

    if action == "size":
        buttons = [
            [Button.inline(f"{mb} MB", f"limset|size|{mb}")] for mb in _MAX_FILE_SIZE_PRESETS_MB
        ]
        await event.edit("📦 Choose the maximum file size:", buttons=buttons)
    elif action == "conc":
        buttons = [[Button.inline(str(n), f"limset|conc|{n}")] for n in _MAX_CONCURRENT_PRESETS]
        await event.edit("🔀 Choose the maximum number of simultaneous downloads:", buttons=buttons)
    elif action == "toggle_cd":
        cfg = await admin_settings.get_cooldown_config()
        new_cfg = await admin_settings.set_cooldown_config(enabled=not cfg["enabled"])
        await event.answer(f"Cooldown {'enabled' if new_cfg['enabled'] else 'disabled'}.")
        await _send_limits_menu(event)
    elif action == "cdcount":
        buttons = [[Button.inline(str(n), f"limset|cdcount|{n}")] for n in _COOLDOWN_COUNT_PRESETS]
        await event.edit("🔢 After how many downloads should the cooldown activate?", buttons=buttons)
    elif action == "cdsec":
        buttons = [[Button.inline(f"{n}s", f"limset|cdsec|{n}")] for n in _COOLDOWN_SECONDS_PRESETS]
        await event.edit("⏳ Choose the cooldown duration:", buttons=buttons)
    else:
        await event.answer()


async def _limits_set_callback(event: events.CallbackQuery.Event, kind: str, value: str) -> None:
    if kind == "size":
        await admin_settings.set_max_file_size(int(value) * 1024 * 1024)
        await event.answer(f"Max file size set to {value} MB.")
    elif kind == "conc":
        await admin_settings.set_max_concurrent_downloads(int(value))
        await event.answer(f"Max concurrent downloads set to {value}.")
    elif kind == "cdcount":
        await admin_settings.set_cooldown_config(count=int(value))
        await event.answer(f"Cooldown now activates after {value} downloads.")
    elif kind == "cdsec":
        await admin_settings.set_cooldown_config(seconds=int(value))
        await event.answer(f"Cooldown duration set to {value} seconds.")
    else:
        await event.answer()
        return
    await _send_limits_menu(event)


# ---------------------------------------------------------------------------
# /messages — customizable bot messages (NEW), especially PornHub texts
# ---------------------------------------------------------------------------
_KEY_LABELS = {
    "pornhub_link_received": "PornHub: link received",
    "pornhub_warning": "PornHub: warning",
    "pornhub_before_download": "PornHub: before download",
    "pornhub_download_started": "PornHub: download started",
    "pornhub_download_completed": "PornHub: success message",
    "pornhub_error": "PornHub: error message",
    "pornhub_admin_notify": "PornHub: admin notification",
    "welcome": "Welcome message",
    "help": "Help message",
    "success": "Generic success message",
    "error_generic": "Generic error message",
    "error_download_failed": "Download failed message",
    "cooldown_active": "Cooldown active message",
    "cooldown_finished": "Cooldown finished message",
}


async def _cmd_messages(event) -> None:
    if not _is_admin(event.sender_id):
        return
    await _send_messages_menu(event)


async def _send_messages_menu(event_or_msg) -> None:
    keys = admin_settings.customizable_keys()
    buttons = []
    row = []
    for key in keys:
        label = _KEY_LABELS.get(key, key)
        row.append(Button.inline(label, f"msg|{key}"))
        if len(row) == 1:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    text = (
        "✍️ **Customize bot messages**\n\n"
        "Choose a message to view / edit / reset. Nothing here is hardcoded — "
        "edits apply immediately to every user, with no redeploy needed."
    )
    if hasattr(event_or_msg, "respond"):
        await event_or_msg.respond(text, buttons=buttons)
    else:
        await event_or_msg.edit(text, buttons=buttons)


async def _messages_show(event: events.CallbackQuery.Event, key: str) -> None:
    current = admin_settings.get_current_text(key)
    default = admin_settings.get_default_text(key)
    is_custom = current != default
    label = _KEY_LABELS.get(key, key)
    text = (
        f"✍️ **{label}**\n\n"
        f"Status: {'🟢 customized' if is_custom else '⚪ default'}\n\n"
        f"Current text:\n\n{current}"
    )
    buttons = [
        [Button.inline("✏️ Edit", f"msgedit|{key}")],
        [Button.inline("♻️ Reset to default", f"msgreset|{key}")],
        [Button.inline("⬅️ Back", "msgback")],
    ]
    await event.edit(text, buttons=buttons)


def register_manager_handlers(manager_client: TelegramClient, main_client: TelegramClient) -> None:
    async def dispatcher(event) -> None:
        if not event.is_private:
            return
        text = (event.raw_text or "").strip()

        # One-shot "waiting for a typed value" flows (broadcast text is
        # handled separately via /broadcast <text>; this covers /messages
        # free-text edits).
        pending = _pending_input.get(event.sender_id)
        if pending and not text.startswith("/"):
            try:
                if pending["action"] == "set_message":
                    ok = await admin_settings.set_message_override(pending["key"], text)
                    _pending_input.pop(event.sender_id, None)
                    if ok:
                        await event.respond(f"✅ Message '{_KEY_LABELS.get(pending['key'], pending['key'])}' updated.")
                    else:
                        await event.respond("❌ This key is not editable.")
                return
            except Exception:  # noqa: BLE001
                logger.exception("Failed to apply pending admin input")
                _pending_input.pop(event.sender_id, None)
                return

        try:
            if text.startswith("/start"):
                await _cmd_start(event)
            elif text.startswith("/ping"):
                await _cmd_ping(event)
            elif text.startswith("/stats"):
                await _cmd_stats(event)
            elif text.startswith("/logs"):
                await _cmd_logs(event)
            elif text.startswith("/interval"):
                await _cmd_interval(event)
            elif text.startswith("/limits"):
                await _cmd_limits(event)
            elif text.startswith("/messages"):
                await _cmd_messages(event)
            elif text.startswith("/broadcast"):
                await _cmd_broadcast(event, main_client)
        except Exception:  # noqa: BLE001
            logger.exception("Error while handling a Manager Bot command")

    async def callback_dispatcher(event: events.CallbackQuery.Event) -> None:
        if not _is_admin(event.sender_id):
            await event.answer()
            return
        data = event.data.decode("utf-8", errors="ignore")
        try:
            if data.startswith("ivl|"):
                minutes = int(data.split("|")[1])
                await database.set_report_interval_minutes(minutes)
                await event.answer(f"Report interval set to {minutes} minute(s) (0 = immediately).")
                await event.edit("⏱ Report interval updated.")
            elif data.startswith("lim|"):
                await _limits_callback(event, data.split("|", 1)[1])
            elif data.startswith("limset|"):
                _, kind, value = data.split("|", 2)
                await _limits_set_callback(event, kind, value)
            elif data.startswith("msgedit|"):
                key = data.split("|", 1)[1]
                _pending_input[event.sender_id] = {"action": "set_message", "key": key}
                await event.answer()
                await event.respond(
                    f"✏️ Send the new text for '{_KEY_LABELS.get(key, key)}' as your next message.\n"
                    "Keep any {placeholder} tokens shown in the current text if present."
                )
            elif data.startswith("msgreset|"):
                key = data.split("|", 1)[1]
                await admin_settings.reset_message_override(key)
                await event.answer("Reset to default.")
                await _messages_show(event, key)
            elif data.startswith("msg|"):
                key = data.split("|", 1)[1]
                await event.answer()
                await _messages_show(event, key)
            elif data == "msgback":
                await event.answer()
                await _send_messages_menu(event)
            elif data == "bcconfirm":
                pending = _pending_broadcast.pop(event.sender_id, None)
                await event.answer()
                if not pending:
                    await event.edit("This broadcast request has expired.")
                    return
                await event.edit("🚀 Broadcast started...")
                asyncio.create_task(_run_broadcast(event.sender_id, pending["text"], main_client, manager_client))
            elif data == "bccancel":
                _pending_broadcast.pop(event.sender_id, None)
                await event.answer()
                await event.edit("❌ Broadcast cancelled.")
            else:
                await event.answer()
        except Exception:  # noqa: BLE001
            logger.exception("Error while handling a Manager Bot callback")
            await event.answer("An error occurred", alert=True)

    manager_client.add_event_handler(dispatcher, events.NewMessage(incoming=True))
    manager_client.add_event_handler(callback_dispatcher, events.CallbackQuery())
    asyncio.create_task(_consume_events(manager_client))
    logger.info("NexiLink Manager Bot handlers registered (stats/logs/interval/limits/messages/broadcast).")


async def _cmd_broadcast(event, main_client: TelegramClient) -> None:
    if not _is_admin(event.sender_id):
        return
    text = event.raw_text.split(maxsplit=1)
    message_text: Optional[str] = text[1] if len(text) > 1 else None
    if not message_text and event.is_reply:
        replied = await event.get_reply_message()
        message_text = replied.raw_text if replied else None
    if not message_text:
        await event.respond("Usage: `/broadcast <message>` (or reply to a message with /broadcast).")
        return

    user_count = await database.get_user_count()
    _pending_broadcast[event.sender_id] = {"text": message_text}
    await event.respond(
        f"📣 Ready to broadcast to **{user_count}** users:\n\n{message_text}\n\nConfirm?",
        buttons=[[Button.inline("✅ Send", "bcconfirm"), Button.inline("❌ Cancel", "bccancel")]],
    )


async def _run_broadcast(admin_id: int, text: str, main_client: TelegramClient, manager_client: TelegramClient) -> None:
    user_ids = await database.get_all_user_ids()
    broadcast_id = await database.start_broadcast()
    sent = 0
    failed = 0
    progress_msg = await manager_client.send_message(
        admin_id, f"📤 Broadcasting to {len(user_ids)} users...\n\nSent: 0 | Failed: 0"
    )
    for idx, user_id in enumerate(user_ids, start=1):
        try:
            await main_client.send_message(user_id, text)
            sent += 1
        except errors.FloodWaitError as exc:
            await asyncio.sleep(exc.seconds)
            try:
                await main_client.send_message(user_id, text)
                sent += 1
            except Exception:  # noqa: BLE001
                failed += 1
        except Exception:  # noqa: BLE001
            failed += 1

        if idx % 25 == 0 or idx == len(user_ids):
            try:
                await progress_msg.edit(f"📤 Broadcasting to {len(user_ids)} users...\n\nSent: {sent} | Failed: {failed}")
            except Exception:  # noqa: BLE001
                pass
        await asyncio.sleep(0.05)  # gentle pacing to avoid hitting global flood limits

    await database.finish_broadcast(broadcast_id, sent, failed)
    await manager_client.send_message(
        admin_id,
        f"✅ **Broadcast completed**\n\nSent:\n{sent}\n\nFailed:\n{failed}",
    )
