"""
handlers.py
-----------
All conversational logic for the main bot: commands, link handling,
platform routing, quality selection, download/upload, and progress
reporting.

Platform routing:
    ``platforms_registry.detect_platform`` classifies the link and one of
    the specialised workflows below runs (fully independent of each
    other):
        - ``_handle_pinterest_url``  -> pinterest_service.py
        - ``_handle_instagram_url``  -> instagram_service.py
        - ``_handle_soundcloud_url`` -> soundcloud_service.py
        - ``_handle_pornhub_url``    -> pornhub_service.py
        - otherwise, the generic yt-dlp path (``downloader.py``) is used,
          which transparently supports hundreds of additional sites.

Every user facing string comes from ``messages.py``. All notable events
(new users, requests, successes, failures) are pushed to the shared
``event_logger`` so the Manager Bot can report on them.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import aiohttp
from telethon import Button, TelegramClient, errors, events
from telethon.tl.custom import Message

import concurrency
import config
import cooldown
import database
import downloader
import inline_mode
import instagram_service
import media_attrs
import messages
import pinterest_service
import platforms_registry
import pornhub_service
import soundcloud_service
import twitter_service
import utils
from event_logger import Event, EventType, shared as event_logger
from fast_upload import send_file_fast

logger = logging.getLogger(__name__)

rate_limiter = utils.RateLimiter()
download_semaphore = concurrency.download_semaphore

# task_id -> {"user_id", "info", "url", "message"}
_pending_selection: dict[str, dict[str, Any]] = {}

# task_id -> {"items", "title", ...} — carousels waiting for the user to
# choose "send separately" vs "send as ZIP" (Instagram / Pinterest / X).
_pending_carousel: dict[str, dict[str, Any]] = {}


@dataclass
class ActiveTask:
    task_id: str
    url: str
    stage: str
    status_message: Message
    temp_dir: Path
    cancel_event: threading.Event = field(default_factory=threading.Event)
    created_at: float = field(default_factory=time.time)


# user_id -> ActiveTask
_active_tasks: dict[int, ActiveTask] = {}


def is_owner(user_id: int) -> bool:
    return bool(config.OWNER_ID) and user_id == config.OWNER_ID


def _is_busy(user_id: int) -> bool:
    if user_id in _active_tasks:
        return True
    return any(v["user_id"] == user_id for v in _pending_selection.values())


# ---------------------------------------------------------------------------
# Message send/edit helpers
# ---------------------------------------------------------------------------
async def send(event_or_chat, key: str, buttons=None, **kwargs: Any) -> Optional[Message]:
    text = messages.get(key, **kwargs)
    client: TelegramClient = event_or_chat.client
    chat_id = event_or_chat.chat_id if hasattr(event_or_chat, "chat_id") else event_or_chat
    try:
        return await client.send_message(chat_id, text, buttons=buttons, link_preview=False)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to send message")
        return None


async def safe_edit(msg: Optional[Message], key: str, buttons=None, **kwargs: Any) -> None:
    if msg is None:
        return
    await safe_edit_text(msg, messages.get(key, **kwargs), buttons=buttons)


async def safe_edit_text(msg: Optional[Message], text: str, buttons=None) -> None:
    if msg is None:
        return
    try:
        await msg.edit(text, buttons=buttons, link_preview=False)
    except errors.MessageNotModifiedError:
        pass
    except errors.FloodWaitError as exc:
        await asyncio.sleep(exc.seconds)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to edit message")


def _cleanup_task(user_id: int) -> None:
    task = _active_tasks.pop(user_id, None)
    if task is not None:
        utils.safe_delete(task.temp_dir)


def _log(event_type: EventType, user_id: int, username: Optional[str] = None, **kwargs: Any) -> None:
    try:
        event_logger.log(Event(type=event_type, user_id=user_id, username=username, **kwargs))
    except Exception:  # noqa: BLE001
        logger.exception("Failed to publish event %s", event_type)


async def _note_download_success(user_id: int, event_type: EventType, **kwargs: Any) -> None:
    """Log a DOWNLOAD_SUCCESS event and feed the cooldown counter. Kept in
    one place so every platform handler (generic/Pinterest/Instagram/
    X/SoundCloud) applies the exact same cooldown rule."""
    _log(event_type, user_id, **kwargs)
    if event_type == EventType.DOWNLOAD_SUCCESS:
        await cooldown.register_success(user_id)


async def _run_cooldown_countdown(msg: Optional[Message], remaining: int) -> None:
    """Periodically edits the cooldown message so the user sees a live
    countdown timer instead of a single static message."""
    if msg is None:
        return
    step = 5
    try:
        while remaining > step:
            await asyncio.sleep(step)
            remaining -= step
            await safe_edit(msg, "cooldown_active", countdown=utils.format_duration(remaining))
        if remaining > 0:
            await asyncio.sleep(remaining)
        await safe_edit(msg, "cooldown_finished")
    except Exception:  # noqa: BLE001
        logger.debug("Cooldown countdown task ended early", exc_info=True)


async def _send_cooldown_notice(event: Message, remaining: int) -> None:
    msg = await send(event, "cooldown_active", countdown=utils.format_duration(remaining))
    asyncio.create_task(_run_cooldown_countdown(msg, remaining))


# ---------------------------------------------------------------------------
# Basic commands
# ---------------------------------------------------------------------------
def main_menu_keyboard() -> list[list[Button]]:
    """Permanent reply keyboard shown to every user (requirement: always
    keep the main bot commands reachable as buttons, not just as /commands)."""
    return [
        [Button.text(messages.get("menu_download"), resize=True), Button.text(messages.get("menu_platforms"), resize=True)],
        [Button.text(messages.get("menu_help"), resize=True), Button.text(messages.get("menu_settings"), resize=True)],
        [Button.text(messages.get("menu_about"), resize=True)],
    ]


async def cmd_start(event: Message) -> None:
    sender = await event.get_sender()
    name = getattr(sender, "first_name", None) or "دوست عزیز"
    await send(event, "welcome", name=name, buttons=main_menu_keyboard())


async def cmd_help(event: Message) -> None:
    await send(event, "help", buttons=main_menu_keyboard())


async def cmd_platforms(event: Message) -> None:
    text = platforms_registry.build_platforms_text()
    await event.client.send_message(event.chat_id, text, link_preview=False, buttons=main_menu_keyboard())


async def cmd_settings(event: Message) -> None:
    max_size = await database.get_max_file_size()
    rate_count, rate_window = await database.get_rate_limit()
    await send(
        event,
        "settings_text",
        max_size=utils.format_bytes(max_size),
        rate_count=rate_count,
        rate_window=rate_window,
        max_concurrent=config.MAX_CONCURRENT_DOWNLOADS,
    )


async def cmd_about(event: Message) -> None:
    await send(event, "about_text", version=config.APP_VERSION)


async def cmd_menu_download_prompt(event: Message) -> None:
    await send(event, "menu_download_prompt")


async def cmd_cancel(event: Message, user_id: int) -> None:
    task = _active_tasks.get(user_id)
    pending_keys = [tid for tid, v in _pending_selection.items() if v["user_id"] == user_id]
    for tid in pending_keys:
        entry = _pending_selection.pop(tid, None)
        if entry:
            await safe_edit(entry.get("message"), "cancel_success")

    carousel_keys = [tid for tid, v in _pending_carousel.items() if v["user_id"] == user_id]
    for tid in carousel_keys:
        entry = _pending_carousel.pop(tid, None)
        if entry:
            timeout_task = entry.get("timeout_task")
            if timeout_task is not None:
                timeout_task.cancel()
            await safe_edit(entry.get("status_msg"), "cancel_success")
            _cleanup_task(user_id)

    if task is None and not pending_keys and not carousel_keys:
        await send(event, "cancel_nothing")
        return

    if task is not None and not carousel_keys:
        task.cancel_event.set()

    await send(event, "cancel_success")


async def cmd_status(event: Message, user_id: int) -> None:
    task = _active_tasks.get(user_id)
    if task is None:
        await send(event, "status_idle")
        return
    await send(event, "status_active", url=task.url, stage=task.stage)


# ---------------------------------------------------------------------------
# Link intake & routing
# ---------------------------------------------------------------------------
async def handle_url_message(event: Message, user_id: int, username: Optional[str], text: str) -> None:
    if await database.is_user_banned(user_id):
        await send(event, "banned_user")
        return

    url = utils.extract_first_url(text)
    if not url:
        await send(event, "invalid_url")
        return

    if _is_busy(user_id):
        await send(event, "error_already_processing")
        return

    max_count, window = await database.get_rate_limit()
    allowed, retry_after = rate_limiter.check(user_id, max_count, window)
    if not allowed:
        await send(event, "error_rate_limit", seconds=int(retry_after) + 1)
        return

    handler_key = platforms_registry.detect_platform(url)
    platform_name = platforms_registry.find_platform_name(url) or handler_key

    _log(EventType.DOWNLOAD_REQUEST, user_id, username, platform=platform_name, url=url)

    if handler_key == "pornhub":
        await _handle_pornhub_url(event, user_id, username, url)
    elif handler_key == "pinterest":
        await _handle_pinterest_url(event, user_id, username, url)
    elif handler_key == "instagram":
        await _handle_instagram_url(event, user_id, username, url)
    elif handler_key == "soundcloud":
        await _handle_soundcloud_url(event, user_id, username, url)
    else:
        await _handle_generic_url(event, user_id, username, url, platform_name)


# ---------------------------------------------------------------------------
# Generic path (yt-dlp): YouTube, TikTok, Twitter/X, Facebook, Vimeo, Reddit...
# ---------------------------------------------------------------------------
async def _handle_generic_url(event: Message, user_id: int, username: Optional[str], url: str, platform_hint: str) -> None:
    await send(event, "url_received")
    status_msg = await send(event, "extracting_info")

    try:
        info = await downloader.extract_video_info(url)
    except downloader.ExtractionError as exc:
        await safe_edit(status_msg, "error_extraction_failed", error=str(exc))
        _log(EventType.DOWNLOAD_FAILED, user_id, username, platform=platform_hint, url=url, error=str(exc))
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error while extracting info")
        await safe_edit(status_msg, "error_generic", error=str(exc))
        _log(EventType.DOWNLOAD_FAILED, user_id, username, platform=platform_hint, url=url, error=str(exc))
        return

    if not info.qualities:
        await safe_edit(status_msg, "no_formats_found")
        _log(EventType.DOWNLOAD_FAILED, user_id, username, platform=info.platform, url=url, error="no formats")
        return

    await safe_edit(
        status_msg,
        "video_detected",
        platform=info.platform,
        title=info.title,
        duration=utils.format_duration(info.duration),
    )

    task_id = uuid.uuid4().hex[:10]
    buttons = []
    for quality in info.qualities:
        label = quality.label
        if quality.filesize:
            label = f"{label} ({utils.format_bytes(quality.filesize)})"
        buttons.append([Button.inline(label, f"q|{task_id}|{quality.id}".encode())])
    buttons.append([Button.inline("❌ لغو", f"cancelq|{task_id}".encode())])

    quality_msg = await send(event, "quality_selection", buttons=buttons)
    _pending_selection[task_id] = {
        "user_id": user_id,
        "username": username,
        "info": info,
        "url": url,
        "message": quality_msg,
    }


async def handle_quality_selected(event: events.CallbackQuery.Event, parts: list[str]) -> None:
    if len(parts) != 3:
        await event.answer()
        return
    _, task_id, quality_id = parts
    entry = _pending_selection.get(task_id)
    if not entry or entry["user_id"] != event.sender_id:
        await event.answer("این درخواست منقضی شده است.", alert=True)
        return

    await event.answer("در حال آماده‌سازی...")
    info = entry["info"]
    url = entry["url"]
    username = entry.get("username")
    quality = next((q for q in info.qualities if q.id == quality_id), None)
    _pending_selection.pop(task_id, None)

    if quality is None:
        await event.answer("گزینهٔ نامعتبر", alert=True)
        return

    msg = await event.get_message()
    await safe_edit(msg, "download_started", quality=quality.label)

    asyncio.create_task(
        _run_download_and_upload(event.chat_id, event.sender_id, username, info, url, quality, msg, task_id)
    )


async def handle_cancel_quality(event: events.CallbackQuery.Event, parts: list[str]) -> None:
    if len(parts) != 2:
        await event.answer()
        return
    task_id = parts[1]
    entry = _pending_selection.pop(task_id, None)
    await event.answer()
    if entry and entry["user_id"] == event.sender_id:
        msg = await event.get_message()
        await safe_edit(msg, "cancel_success")


async def _download_thumbnail(url: Optional[str], out_dir: Path) -> Optional[str]:
    if not url:
        return None
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / "thumb.jpg"
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
        dest.write_bytes(data)
        return str(dest)
    except Exception:  # noqa: BLE001
        return None


async def _run_download_and_upload(
    chat_id: int,
    user_id: int,
    username: Optional[str],
    info: downloader.VideoInfo,
    url: str,
    quality: downloader.QualityOption,
    msg: Message,
    task_id: str,
    on_success=None,
    on_failure=None,
) -> None:
    async with download_semaphore:
        temp_dir = Path(config.DOWNLOAD_PATH) / task_id
        task = ActiveTask(task_id=task_id, url=url, stage="downloading", status_message=msg, temp_dir=temp_dir)
        _active_tasks[user_id] = task

        loop = asyncio.get_running_loop()
        last_edit = {"t": 0.0}

        def on_progress(d: dict[str, Any]) -> None:
            status = d.get("status")
            now = time.monotonic()
            if status == "downloading":
                if now - last_edit["t"] < 4:
                    return
                last_edit["t"] = now
                downloaded = d.get("downloaded_bytes") or 0
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                percent = (downloaded / total * 100) if total else 0.0
                kwargs = {
                    "bar": utils.progress_bar(percent),
                    "percent": f"{percent:.0f}",
                    "downloaded": utils.format_bytes(downloaded),
                    "total": utils.format_bytes(total) if total else "unknown",
                    "speed": utils.format_speed(d.get("speed")),
                    "eta": utils.format_eta(d.get("eta")),
                }
                asyncio.run_coroutine_threadsafe(safe_edit(msg, "download_progress", **kwargs), loop)
            elif status == "finished":
                asyncio.run_coroutine_threadsafe(safe_edit(msg, "processing"), loop)

        try:
            result = await downloader.download_video(
                url, quality, str(temp_dir), progress_callback=on_progress, cancel_event=task.cancel_event
            )
        except downloader.DownloadCancelledError:
            await safe_edit(msg, "cancel_success")
            _log(EventType.DOWNLOAD_FAILED, user_id, username, platform=info.platform, url=url, quality=quality.label, error="cancelled")
            _cleanup_task(user_id)
            return
        except downloader.DownloadFailedError as exc:
            await safe_edit(msg, "error_download_failed", error=str(exc))
            _log(EventType.DOWNLOAD_FAILED, user_id, username, platform=info.platform, url=url, quality=quality.label, error=str(exc))
            if on_failure:
                await on_failure(str(exc))
            _cleanup_task(user_id)
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error during download")
            await safe_edit(msg, "error_generic", error=str(exc))
            _log(EventType.DOWNLOAD_FAILED, user_id, username, platform=info.platform, url=url, quality=quality.label, error=str(exc))
            if on_failure:
                await on_failure(str(exc))
            _cleanup_task(user_id)
            return

        max_size = await database.get_max_file_size()
        if result.filesize > max_size:
            await safe_edit(
                msg,
                "error_file_too_large",
                size=utils.format_bytes(result.filesize),
                max_size=utils.format_bytes(max_size),
            )
            _log(
                EventType.DOWNLOAD_FAILED, user_id, username, platform=info.platform, url=url,
                quality=quality.label, file_size=result.filesize, error="file too large",
            )
            _cleanup_task(user_id)
            return

        task.stage = "uploading"
        await safe_edit(msg, "upload_started")

        thumb_path = await _download_thumbnail(result.thumbnail, temp_dir)

        try:
            await _upload_video_result(chat_id, msg, result, quality, thumb_path)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to upload the file")
            await safe_edit(msg, "error_upload_failed", error=str(exc))
            _log(
                EventType.DOWNLOAD_FAILED, user_id, username, platform=info.platform, url=url,
                quality=quality.label, file_size=result.filesize, error=str(exc),
            )
            _cleanup_task(user_id)
            return
        finally:
            utils.safe_delete(thumb_path) if thumb_path else None

        await safe_edit(
            msg,
            "success",
            title=result.title,
            quality=quality.label,
            size=utils.format_bytes(result.filesize),
        )
        await _note_download_success(
            user_id, EventType.DOWNLOAD_SUCCESS, username=username, platform=info.platform, url=url,
            quality=quality.label, file_size=result.filesize,
        )
        if on_success:
            await on_success(result)
        _cleanup_task(user_id)


async def _upload_video_result(
    chat_id: int,
    msg: Message,
    result: downloader.DownloadResult,
    quality: downloader.QualityOption,
    thumb_path: Optional[str] = None,
) -> None:
    """Upload the downloaded file using parallel MTProto upload for speed."""
    upload_start = time.monotonic()
    last_upload_edit = {"t": 0.0}

    async def upload_progress(current: int, total: int) -> None:
        now = time.monotonic()
        if now - last_upload_edit["t"] < 4 and current != total:
            return
        last_upload_edit["t"] = now
        percent = (current / total * 100) if total else 0.0
        elapsed = max(time.monotonic() - upload_start, 0.001)
        speed = current / elapsed
        await safe_edit(
            msg,
            "upload_progress",
            bar=utils.progress_bar(percent),
            percent=f"{percent:.0f}",
            uploaded=utils.format_bytes(current),
            total=utils.format_bytes(total),
            speed=utils.format_speed(speed),
        )

    caption = f"🎬 {result.title}"
    client: TelegramClient = msg.client

    is_audio_only = quality.kind == "audio"
    if is_audio_only:
        attrs = media_attrs.build_audio_attributes(result.filepath, result.duration, title=result.title)
        mime_type = "audio/mpeg"
    else:
        width, height = await media_attrs.probe_video_dimensions(result.filepath)
        attrs = media_attrs.build_video_attributes(result.filepath, result.duration, width, height)
        mime_type = media_attrs.guess_mime(result.filepath, default="video/mp4")

    try:
        await send_file_fast(
            client,
            chat_id,
            result.filepath,
            caption=caption,
            thumb=thumb_path,
            attributes=attrs,
            mime_type=mime_type,
            force_document=is_audio_only,
            progress_callback=lambda cur, tot: asyncio.create_task(upload_progress(cur, tot)),
        )
    except Exception:
        logger.warning("Parallel upload failed; falling back to standard upload.")
        await client.send_file(
            chat_id,
            result.filepath,
            caption=caption,
            thumb=thumb_path,
            supports_streaming=not is_audio_only,
            progress_callback=lambda cur, tot: asyncio.create_task(upload_progress(cur, tot)),
        )


# ---------------------------------------------------------------------------
# Pinterest
# ---------------------------------------------------------------------------
# How long (seconds) we keep a downloaded carousel on disk while waiting for
# the user to press "send separately" / "send as ZIP". After this the task
# is expired and the temp files are cleaned up so the bot doesn't leak disk
# space on Railway's small free-plan volume.
_PENDING_CAROUSEL_TIMEOUT_SECONDS = 5 * 60


def _pin_icon(kind: str) -> str:
    return "🎬" if kind == "video" else "📌"


async def _handle_pinterest_url(event: Message, user_id: int, username: Optional[str], url: str) -> None:
    if _is_busy(user_id):
        await send(event, "error_already_processing")
        return

    status_msg = await send(event, "pinterest_downloading")
    task_id = uuid.uuid4().hex[:10]
    temp_dir = Path(config.DOWNLOAD_PATH) / task_id
    task = ActiveTask(task_id=task_id, url=url, stage="downloading (Pinterest)", status_message=status_msg, temp_dir=temp_dir)
    _active_tasks[user_id] = task

    async with download_semaphore:
        try:
            result = await pinterest_service.download_pin(url, str(temp_dir))
        except pinterest_service.PinterestError as exc:
            logger.warning("Pinterest download failed for %s: %s", url, exc)
            await safe_edit(status_msg, "pinterest_error", error=str(exc))
            _log(EventType.DOWNLOAD_FAILED, user_id, username, platform="Pinterest", url=url, error=str(exc))
            _cleanup_task(user_id)
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected Pinterest error")
            await safe_edit(status_msg, "pinterest_error", error=str(exc))
            _log(EventType.DOWNLOAD_FAILED, user_id, username, platform="Pinterest", url=url, error=str(exc))
            _cleanup_task(user_id)
            return

    # Single image/video pin: nothing to ask, just deliver it.
    if not result.is_carousel:
        await _send_pinterest_single(event, user_id, username, url, result, status_msg)
        return

    # -----------------------------------------------------------------
    # Carousel (multiple images/videos): this is the exact bug that was
    # reported — the bot used to silently decide "separate vs ZIP" for the
    # user based on `config.ZIP_THRESHOLD_ITEMS`, without ever asking. We
    # now always pause here, show the two options as inline buttons, and
    # wait for `handle_pinterest_carousel_choice` (routed from the callback
    # dispatcher below) before sending anything.
    # -----------------------------------------------------------------
    buttons = [
        [Button.inline("📤 ارسال جداگانه", f"pcar|sep|{task_id}".encode())],
        [Button.inline("📦 ارسال به‌صورت ZIP", f"pcar|zip|{task_id}".encode())],
        [Button.inline("❌ لغو", f"pcar|cancel|{task_id}".encode())],
    ]
    await safe_edit_text(status_msg, messages.get("pinterest_carousel_ask_format", count=len(result.items)), buttons=buttons)

    timeout_task = asyncio.create_task(_expire_pending_carousel(task_id))
    _pending_carousel[task_id] = {
        "user_id": user_id,
        "username": username,
        "url": url,
        "title": result.title,
        "items": result.items,
        "temp_dir": temp_dir,
        "status_msg": status_msg,
        "platform": "Pinterest",
        "timeout_task": timeout_task,
    }


async def _send_pinterest_single(
    event: Message,
    user_id: int,
    username: Optional[str],
    url: str,
    result: pinterest_service.PinterestResult,
    status_msg: Message,
) -> None:
    item = result.items[0]
    client: TelegramClient = event.client
    try:
        # Always sent as a document/file to preserve the original quality
        # (never a compressed Telegram photo/video message), and never
        # mislabelled: `item.kind` reflects the *validated* real content of
        # the file (see pinterest_service._validate_video / _validate_and_fix_image),
        # not just a guess based on the URL.
        await client.send_file(
            event.chat_id,
            item.filepath,
            caption=f"{_pin_icon(item.kind)} {result.title}",
            force_document=(item.kind == "image"),
            supports_streaming=(item.kind == "video"),
            attributes=media_attrs.build_document_attributes(item.filepath),
        )
        await safe_edit(status_msg, "pinterest_success_single", kind=("ویدیو" if item.kind == "video" else "عکس"))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to send Pinterest media")
        await safe_edit(status_msg, "pinterest_error", error=str(exc))
        _log(EventType.DOWNLOAD_FAILED, user_id, username, platform="Pinterest", url=url, error=str(exc))
        _cleanup_task(user_id)
        return

    _log(
        EventType.DOWNLOAD_SUCCESS, user_id, username, platform="Pinterest", url=url,
        quality="1 item", file_size=Path(item.filepath).stat().st_size,
    )
    _cleanup_task(user_id)


async def _expire_pending_carousel(task_id: str) -> None:
    """Auto-cancel a carousel choice prompt nobody answered, so temp files
    on Railway's small free-plan disk don't pile up forever."""
    try:
        await asyncio.sleep(_PENDING_CAROUSEL_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        return
    entry = _pending_carousel.pop(task_id, None)
    if not entry:
        return
    await safe_edit(entry["status_msg"], "pinterest_carousel_expired")
    _cleanup_task(entry["user_id"])


async def handle_pinterest_carousel_choice(event: events.CallbackQuery.Event, parts: list[str]) -> None:
    """Runs when the user taps "ارسال جداگانه" / "ارسال به‌صورت ZIP" / "لغو"
    on the prompt shown by `_handle_pinterest_url` for a carousel pin. This
    is the conversation-state logic that was previously missing entirely —
    `_pending_carousel` used to be declared but never populated or read."""
    if len(parts) != 3:
        await event.answer()
        return
    _, action, task_id = parts
    entry = _pending_carousel.get(task_id)
    if not entry or entry["user_id"] != event.sender_id:
        await event.answer("این درخواست منقضی شده است.", alert=True)
        return

    entry = _pending_carousel.pop(task_id)
    timeout_task = entry.get("timeout_task")
    if timeout_task is not None:
        timeout_task.cancel()

    await event.answer("در حال ارسال...")

    user_id = entry["user_id"]
    username = entry["username"]
    url = entry["url"]
    title = entry["title"]
    items = entry["items"]
    temp_dir: Path = entry["temp_dir"]
    status_msg: Message = entry["status_msg"]
    client: TelegramClient = event.client

    if action == "cancel":
        await safe_edit(status_msg, "cancel_success")
        _log(EventType.DOWNLOAD_FAILED, user_id, username, platform="Pinterest", url=url, error="cancelled by user (carousel choice)")
        _cleanup_task(user_id)
        return

    total_size = sum(Path(i.filepath).stat().st_size for i in items)

    try:
        if action == "zip":
            await safe_edit(status_msg, "pinterest_zip_building", count=len(items))
            zip_path = utils.create_zip(
                [i.filepath for i in items], temp_dir / f"{utils.sanitize_filename(title, 60)}.zip"
            )
            await client.send_file(
                event.chat_id,
                str(zip_path),
                caption=f"📦 {title} ({len(items)} آیتم)",
                force_document=True,
            )
        else:  # "sep" -> send every item as its own Telegram document, in order
            await safe_edit(status_msg, "pinterest_carousel_sending_separate", count=len(items))
            for item in sorted(items, key=lambda i: i.index):
                await client.send_file(
                    event.chat_id,
                    item.filepath,
                    caption=f"{_pin_icon(item.kind)} {title} ({item.index}/{len(items)})",
                    force_document=(item.kind == "image"),
                    supports_streaming=(item.kind == "video"),
                    attributes=media_attrs.build_document_attributes(item.filepath),
                )
                await asyncio.sleep(0.5)  # be gentle with Telegram's flood limits

        await safe_edit(status_msg, "pinterest_success_carousel", count=len(items))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to send Pinterest carousel media")
        await safe_edit(status_msg, "pinterest_error", error=str(exc))
        _log(EventType.DOWNLOAD_FAILED, user_id, username, platform="Pinterest", url=url, error=str(exc))
        _cleanup_task(user_id)
        return

    _log(
        EventType.DOWNLOAD_SUCCESS, user_id, username, platform="Pinterest", url=url,
        quality=f"{len(items)} item(s) via {action}", file_size=total_size,
    )
    _cleanup_task(user_id)


# ---------------------------------------------------------------------------
# Instagram
# ---------------------------------------------------------------------------
async def _handle_instagram_url(event: Message, user_id: int, username: Optional[str], url: str) -> None:
    if _is_busy(user_id):
        await send(event, "error_already_processing")
        return

    status_msg = await send(event, "instagram_downloading")
    task_id = uuid.uuid4().hex[:10]
    temp_dir = Path(config.DOWNLOAD_PATH) / task_id
    task = ActiveTask(task_id=task_id, url=url, stage="downloading (Instagram)", status_message=status_msg, temp_dir=temp_dir)
    _active_tasks[user_id] = task

    async with download_semaphore:
        try:
            result = await instagram_service.download_post(url, str(temp_dir))
        except instagram_service.InstagramError as exc:
            await safe_edit(status_msg, "instagram_error", error=str(exc))
            _log(EventType.DOWNLOAD_FAILED, user_id, username, platform="Instagram", url=url, error=str(exc))
            _cleanup_task(user_id)
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected Instagram error")
            await safe_edit(status_msg, "instagram_error", error=str(exc))
            _log(EventType.DOWNLOAD_FAILED, user_id, username, platform="Instagram", url=url, error=str(exc))
            _cleanup_task(user_id)
            return

        total_size = sum(Path(i.filepath).stat().st_size for i in result.items)
        client: TelegramClient = event.client

        try:
            if not result.is_carousel:
                item = result.items[0]
                await client.send_file(
                    event.chat_id,
                    item.filepath,
                    caption=f"📷 {result.title}",
                    force_document=(item.kind == "image"),
                    supports_streaming=(item.kind == "video"),
                    attributes=media_attrs.build_document_attributes(item.filepath),
                )
                await safe_edit(status_msg, "instagram_success_single", kind=("ویدیو" if item.kind == "video" else "عکس"))
            else:
                await safe_edit(status_msg, "instagram_carousel_found", count=len(result.items))
                if len(result.items) > config.ZIP_THRESHOLD_ITEMS:
                    await safe_edit(status_msg, "instagram_zip_building", count=len(result.items))
                    zip_path = utils.create_zip(
                        [i.filepath for i in result.items], temp_dir / f"{utils.sanitize_filename(result.title, 60)}.zip"
                    )
                    await client.send_file(
                        event.chat_id,
                        str(zip_path),
                        caption=f"📦 {result.title} ({len(result.items)} items)",
                        force_document=True,
                    )
                else:
                    for item in result.items:
                        await client.send_file(
                            event.chat_id,
                            item.filepath,
                            caption=f"📷 {result.title} ({item.index}/{len(result.items)})",
                            force_document=(item.kind == "image"),
                            supports_streaming=(item.kind == "video"),
                            attributes=media_attrs.build_document_attributes(item.filepath),
                        )
                        await asyncio.sleep(0.5)  # be gentle with Telegram's flood limits
                await safe_edit(status_msg, "instagram_success_carousel", count=len(result.items))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to send Instagram media")
            await safe_edit(status_msg, "instagram_error", error=str(exc))
            _log(EventType.DOWNLOAD_FAILED, user_id, username, platform="Instagram", url=url, error=str(exc))
            _cleanup_task(user_id)
            return

        _log(
            EventType.DOWNLOAD_SUCCESS, user_id, username, platform="Instagram", url=url,
            quality=f"{len(result.items)} item(s)", file_size=total_size,
        )
        _cleanup_task(user_id)


# ---------------------------------------------------------------------------
# SoundCloud (tracks & playlists)
# ---------------------------------------------------------------------------
async def _handle_soundcloud_url(event: Message, user_id: int, username: Optional[str], url: str) -> None:
    if _is_busy(user_id):
        await send(event, "error_already_processing")
        return

    is_playlist = "/sets/" in url.lower()
    task_id = uuid.uuid4().hex[:10]
    temp_dir = Path(config.DOWNLOAD_PATH) / task_id

    if not is_playlist:
        status_msg = await send(event, "soundcloud_downloading")
        task = ActiveTask(task_id=task_id, url=url, stage="downloading (SoundCloud)", status_message=status_msg, temp_dir=temp_dir)
        _active_tasks[user_id] = task
        async with download_semaphore:
            try:
                result = await soundcloud_service.download_track(url, str(temp_dir))
            except soundcloud_service.SoundCloudError as exc:
                await safe_edit(status_msg, "soundcloud_track_error", error=str(exc))
                _log(EventType.DOWNLOAD_FAILED, user_id, username, platform="SoundCloud", url=url, error=str(exc))
                _cleanup_task(user_id)
                return
            except Exception as exc:  # noqa: BLE001
                logger.exception("Unexpected SoundCloud error")
                await safe_edit(status_msg, "soundcloud_track_error", error=str(exc))
                _log(EventType.DOWNLOAD_FAILED, user_id, username, platform="SoundCloud", url=url, error=str(exc))
                _cleanup_task(user_id)
                return

            try:
                attrs = media_attrs.build_audio_attributes(result.filepath, result.duration, title=result.title, performer=result.artist)
                await event.client.send_file(
                    event.chat_id, result.filepath, caption=f"🎵 {result.title}", attributes=attrs, mime_type="audio/mpeg",
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed to send SoundCloud track")
                await safe_edit(status_msg, "soundcloud_track_error", error=str(exc))
                _log(EventType.DOWNLOAD_FAILED, user_id, username, platform="SoundCloud", url=url, error=str(exc))
                _cleanup_task(user_id)
                return

            await safe_edit(status_msg, "soundcloud_track_success", title=result.title)
            await _note_download_success(user_id, EventType.DOWNLOAD_SUCCESS, username=username, platform="SoundCloud", url=url, file_size=result.filesize)
            _cleanup_task(user_id)
        return

    # Playlist ("sets")
    status_msg = await send(event, "extracting_info")
    task = ActiveTask(task_id=task_id, url=url, stage="downloading (SoundCloud playlist)", status_message=status_msg, temp_dir=temp_dir)
    _active_tasks[user_id] = task

    async with download_semaphore:
        try:
            playlist = await soundcloud_service.extract_playlist(url, config.MAX_PLAYLIST_TRACKS)
        except soundcloud_service.SoundCloudError as exc:
            await safe_edit(status_msg, "soundcloud_track_error", error=str(exc))
            _log(EventType.DOWNLOAD_FAILED, user_id, username, platform="SoundCloud", url=url, error=str(exc))
            _cleanup_task(user_id)
            return

        await safe_edit(status_msg, "soundcloud_playlist_found", name=playlist.title, count=len(playlist.tracks))

        downloaded_paths: list[str] = []
        success = 0
        for idx, track in enumerate(playlist.tracks, start=1):
            await safe_edit(status_msg, "soundcloud_playlist_progress", index=idx, total=len(playlist.tracks), title=track.title)
            try:
                result = await soundcloud_service.download_track(track.url, str(temp_dir / str(idx)))
                downloaded_paths.append(result.filepath)
                success += 1
            except Exception as exc:  # noqa: BLE001
                await safe_edit(status_msg, "soundcloud_playlist_track_failed", index=idx, total=len(playlist.tracks), title=track.title, error=str(exc))
                await asyncio.sleep(1)

        if not downloaded_paths:
            _log(EventType.DOWNLOAD_FAILED, user_id, username, platform="SoundCloud", url=url, error="no tracks downloaded")
            _cleanup_task(user_id)
            return

        total_size = sum(Path(p).stat().st_size for p in downloaded_paths)
        try:
            if len(downloaded_paths) > config.ZIP_THRESHOLD_ITEMS:
                await safe_edit(status_msg, "soundcloud_zip_building", count=len(downloaded_paths))
                zip_path = utils.create_zip(downloaded_paths, temp_dir / f"{utils.sanitize_filename(playlist.title, 60)}.zip")
                await event.client.send_file(event.chat_id, str(zip_path), caption=f"📦 {playlist.title}", force_document=True)
            else:
                for path in downloaded_paths:
                    await event.client.send_file(event.chat_id, path, caption=f"🎵 {Path(path).stem}")
                    await asyncio.sleep(0.5)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to send SoundCloud playlist")
            _log(EventType.DOWNLOAD_FAILED, user_id, username, platform="SoundCloud", url=url, error=str(exc))
            _cleanup_task(user_id)
            return

        await safe_edit(status_msg, "soundcloud_playlist_finished", success=success, total=len(playlist.tracks))
        await _note_download_success(user_id, EventType.DOWNLOAD_SUCCESS, username=username, platform="SoundCloud", url=url, quality=f"{success} tracks", file_size=total_size)
        _cleanup_task(user_id)


# ---------------------------------------------------------------------------
# PornHub (optional, isolated workflow)
# ---------------------------------------------------------------------------
async def _handle_pornhub_url(event: Message, user_id: int, username: Optional[str], url: str) -> None:
    await send(event, "pornhub_link_received")
    await send(event, "pornhub_warning")
    status_msg = await send(event, "extracting_info")
    client: TelegramClient = event.client

    try:
        info = await downloader.extract_video_info(url)
    except Exception as exc:  # noqa: BLE001
        await safe_edit(status_msg, "pornhub_error", error=str(exc))
        _log(EventType.DOWNLOAD_FAILED, user_id, username, platform="PornHub", url=url, error=str(exc))
        await pornhub_service.notify_admin(
            client, pornhub_service.PornhubNotifyPayload(user_id=user_id, username=username, title="unknown", status="failed", detail=str(exc))
        )
        return

    if not info.qualities:
        await safe_edit(status_msg, "pornhub_error", error="No quality was found.")
        return

    best_quality = max(
        (q for q in info.qualities if q.kind == "video"),
        key=lambda q: (q.height or 0),
        default=info.qualities[0],
    )

    await safe_edit(status_msg, "pornhub_before_download", title=info.title, duration=utils.format_duration(info.duration))

    if _is_busy(user_id):
        await send(event, "error_already_processing")
        return

    task_id = uuid.uuid4().hex[:10]
    await safe_edit(status_msg, "pornhub_download_started")

    async def on_success(result: downloader.DownloadResult) -> None:
        await pornhub_service.notify_admin(
            client,
            pornhub_service.PornhubNotifyPayload(user_id=user_id, username=username, title=result.title, status="success"),
        )

    async def on_failure(error: str) -> None:
        await pornhub_service.notify_admin(
            client,
            pornhub_service.PornhubNotifyPayload(user_id=user_id, username=username, title=info.title, status="failed", detail=error),
        )

    asyncio.create_task(
        _run_download_and_upload(
            event.chat_id, user_id, username, info, url, best_quality, status_msg, task_id,
            on_success=on_success, on_failure=on_failure,
        )
    )


# ---------------------------------------------------------------------------
# Event registration
# ---------------------------------------------------------------------------
def register_handlers(client: TelegramClient) -> None:
    async def dispatcher(event: Message) -> None:
        if not event.is_private:
            return
        sender = await event.get_sender()
        if sender is None or getattr(sender, "bot", False):
            return

        username = getattr(sender, "username", None)
        is_new = await database.upsert_user(sender.id, username, getattr(sender, "first_name", None))
        if is_new:
            _log(EventType.NEW_USER, sender.id, username)

        text = (event.raw_text or "").strip()

        try:
            if text.startswith("/start"):
                await cmd_start(event)
            elif text.startswith("/help"):
                await cmd_help(event)
            elif text.startswith("/platforms"):
                await cmd_platforms(event)
            elif text.startswith("/cancel"):
                await cmd_cancel(event, sender.id)
            elif text.startswith("/status"):
                await cmd_status(event, sender.id)
            elif text.startswith("/settings"):
                await cmd_settings(event)
            elif text.startswith("/about"):
                await cmd_about(event)
            elif text in (messages.get("platforms_button"), messages.get("menu_platforms")):
                await cmd_platforms(event)
            elif text == messages.get("menu_help"):
                await cmd_help(event)
            elif text == messages.get("menu_settings"):
                await cmd_settings(event)
            elif text == messages.get("menu_about"):
                await cmd_about(event)
            elif text == messages.get("menu_download"):
                await cmd_menu_download_prompt(event)
            elif text.startswith("/"):
                await cmd_help(event)
            else:
                await handle_url_message(event, sender.id, username, text)
        except Exception:  # noqa: BLE001
            logger.exception("Unhandled error while processing a message")
            _log(EventType.ERROR, sender.id, username, error="Unhandled error while processing a message")
            try:
                await send(event, "error_generic", error="internal bot error")
            except Exception:  # noqa: BLE001
                pass

    async def callback_dispatcher(event: events.CallbackQuery.Event) -> None:
        try:
            data = event.data.decode("utf-8", errors="ignore")
            parts = data.split("|")
            prefix = parts[0]
            if prefix == "q":
                await handle_quality_selected(event, parts)
            elif prefix == "cancelq":
                await handle_cancel_quality(event, parts)
            elif prefix == "pcar":
                await handle_pinterest_carousel_choice(event, parts)
            elif prefix in ("iv", "ia"):
                await inline_mode.handle_inline_callback(event, parts)
            else:
                await event.answer()
        except Exception:  # noqa: BLE001
            logger.exception("Unhandled error while processing a button press")
            try:
                await event.answer("خطایی رخ داد", alert=True)
            except Exception:  # noqa: BLE001
                pass

    client.add_event_handler(dispatcher, events.NewMessage(incoming=True))
    client.add_event_handler(callback_dispatcher, events.CallbackQuery())

    if config.INLINE_MODE_ENABLED:
        client.add_event_handler(inline_mode.handle_inline_query, events.InlineQuery())

    logger.info("All bot handlers registered successfully.")
