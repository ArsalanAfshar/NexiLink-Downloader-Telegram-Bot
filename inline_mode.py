"""
inline_mode.py
---------------
Telegram Inline Mode support: type ``@<bot_username> <link>`` in any chat
to fetch a video/audio without ever opening a private chat with the bot.

Workflow
--------
1. ``handle_inline_query`` detects the URL, extracts metadata (title,
   platform, thumbnail, duration) and returns a single inline result with
   two buttons: 🎬 Download Video / 🎵 Download Audio.
2. ``handle_inline_callback`` runs when one of those buttons is pressed.
   It answers the callback immediately (so Telegram doesn't show a
   timeout spinner), then starts the download in the background
   (``asyncio.create_task``) so the event loop is never blocked and many
   users can use inline mode concurrently.
3. Progress is reported by periodically editing the inline message
   (``event.edit(...)``), and the final file is attached to that same
   inline message once the download finishes.

Inline results only carry a short opaque token (not the full URL) in
their `callback_data`, because Telegram limits `callback_data` to 64
bytes. The token maps to an in-memory job description that is cleaned up
after use / after a timeout.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from telethon import Button, events

import concurrency
import config
import downloader
import media_attrs
import messages
import utils

logger = logging.getLogger(__name__)

_JOB_TTL_SECONDS = 20 * 60

# token -> {"url", "info", "created_at"}
_INLINE_JOBS: dict[str, dict[str, Any]] = {}


def _purge_expired_jobs() -> None:
    now = time.time()
    expired = [t for t, j in _INLINE_JOBS.items() if now - j["created_at"] > _JOB_TTL_SECONDS]
    for t in expired:
        _INLINE_JOBS.pop(t, None)


async def handle_inline_query(event: events.InlineQuery.Event) -> None:
    _purge_expired_jobs()
    query_text = (event.text or "").strip()
    url = utils.extract_first_url(query_text)

    if not url:
        result = event.builder.article(
            title="Send a link to download",
            description="Type a video/audio link after the bot's username.",
            text="Send me a link (YouTube, TikTok, Instagram, Pinterest, ...) to get started.",
        )
        await event.answer([result], cache_time=5)
        return

    try:
        info = await downloader.extract_video_info(url)
    except Exception as exc:  # noqa: BLE001
        logger.info("Inline query extraction failed for %s: %s", url, exc)
        result = event.builder.article(
            title="❌ Could not fetch this link",
            description=str(exc)[:100],
            text=messages.get("error_extraction_failed", error=str(exc)),
        )
        await event.answer([result], cache_time=0)
        return

    if not info.qualities:
        result = event.builder.article(
            title="❌ No downloadable format found",
            text=messages.get("no_formats_found"),
        )
        await event.answer([result], cache_time=0)
        return

    token = uuid.uuid4().hex[:16]
    _INLINE_JOBS[token] = {"url": url, "info": info, "created_at": time.time()}

    buttons = [[
        Button.inline("🎬 Download Video", f"iv|{token}"),
        Button.inline("🎵 Download Audio", f"ia|{token}"),
    ]]

    try:
        result = event.builder.article(
            title=info.title,
            description=f"{info.platform} • {utils.format_duration(info.duration)}",
            thumb=info.thumbnail,
            text=f"🎬 **{info.title}**\n🌐 {info.platform}\n⏱ {utils.format_duration(info.duration)}\n\nChoose a format below:",
            buttons=buttons,
        )
    except Exception:  # noqa: BLE001
        # Some thumbnail URLs are rejected by Telegram; retry without one.
        result = event.builder.article(
            title=info.title,
            description=f"{info.platform} • {utils.format_duration(info.duration)}",
            text=f"🎬 **{info.title}**\n🌐 {info.platform}\n⏱ {utils.format_duration(info.duration)}\n\nChoose a format below:",
            buttons=buttons,
        )

    await event.answer([result], cache_time=0)


async def handle_inline_callback(event: events.CallbackQuery.Event, parts: list[str]) -> None:
    if len(parts) != 2:
        await event.answer()
        return

    prefix, token = parts
    job = _INLINE_JOBS.get(token)
    if not job:
        await event.answer("This request has expired, please search again.", alert=True)
        return

    await event.answer("Starting download in the background...")

    info: downloader.VideoInfo = job["info"]
    url: str = job["url"]

    if prefix == "ia":
        quality = next((q for q in info.qualities if q.kind == "audio"), None) or downloader.QualityOption(
            id="audio", label="Audio", kind="audio"
        )
    else:
        quality = max(
            (q for q in info.qualities if q.kind == "video"),
            key=lambda q: (q.height or 0),
            default=info.qualities[0],
        )

    asyncio.create_task(_run_inline_download(event, url, info, quality))


async def _run_inline_download(
    event: events.CallbackQuery.Event,
    url: str,
    info: downloader.VideoInfo,
    quality: downloader.QualityOption,
) -> None:
    task_id = uuid.uuid4().hex[:10]
    temp_dir = Path(config.DOWNLOAD_PATH) / f"inline_{task_id}"

    async with concurrency.download_semaphore:
        last_edit = {"t": 0.0}

        def on_progress(d: dict[str, Any]) -> None:
            status = d.get("status")
            now = time.monotonic()
            if status != "downloading" or now - last_edit["t"] < 5:
                return
            last_edit["t"] = now
            downloaded = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            percent = (downloaded / total * 100) if total else 0.0
            text = messages.get(
                "download_progress",
                bar=utils.progress_bar(percent),
                percent=f"{percent:.0f}",
                downloaded=utils.format_bytes(downloaded),
                total=utils.format_bytes(total) if total else "unknown",
                speed=utils.format_speed(d.get("speed")),
                eta=utils.format_eta(d.get("eta")),
            )
            try:
                loop = asyncio.get_running_loop()
                asyncio.run_coroutine_threadsafe(_safe_inline_edit(event, text), loop)
            except RuntimeError:
                pass

        try:
            result = await downloader.download_video(url, quality, str(temp_dir), progress_callback=on_progress)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Inline download failed")
            await _safe_inline_edit(event, messages.get("error_download_failed", error=str(exc)))
            utils.safe_delete(temp_dir)
            return

        max_size = await _max_file_size()
        if result.filesize > max_size:
            await _safe_inline_edit(
                event,
                messages.get(
                    "error_file_too_large",
                    size=utils.format_bytes(result.filesize),
                    max_size=utils.format_bytes(max_size),
                ),
            )
            utils.safe_delete(temp_dir)
            return

        await _safe_inline_edit(event, messages.get("upload_started"))

        try:
            is_audio_only = quality.kind == "audio"
            if is_audio_only:
                attrs = media_attrs.build_audio_attributes(result.filepath, result.duration, title=result.title)
            else:
                width, height = await media_attrs.probe_video_dimensions(result.filepath)
                attrs = media_attrs.build_video_attributes(result.filepath, result.duration, width, height)

            await event.edit(
                file=result.filepath,
                text=messages.get("success", title=result.title, quality=quality.label, size=utils.format_bytes(result.filesize)),
                attributes=attrs,
                force_document=is_audio_only,
                link_preview=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not attach the file to the inline message: %s", exc)
            await _safe_inline_edit(
                event,
                f"✅ Download finished ({utils.format_bytes(result.filesize)}), but Telegram would not let me "
                f"attach the file to this inline message. Please message the bot directly to receive it.",
            )
        finally:
            utils.safe_delete(temp_dir)


async def _max_file_size() -> int:
    try:
        import database

        return await database.get_max_file_size()
    except Exception:  # noqa: BLE001
        return config.MAX_FILE_SIZE


async def _safe_inline_edit(event: events.CallbackQuery.Event, text: str) -> None:
    try:
        await event.edit(text, link_preview=False)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed to edit inline message: %s", exc)
