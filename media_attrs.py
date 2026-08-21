"""
media_attrs.py
---------------
Builds Telegram document attributes (filename, duration, video dimensions,
audio tags) without any heavy dependency; ffprobe (bundled with FFmpeg in
the Dockerfile) is used to quickly probe video dimensions.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
from typing import Optional

from telethon.tl.types import DocumentAttributeAudio, DocumentAttributeFilename, DocumentAttributeVideo

logger = logging.getLogger(__name__)


async def probe_video_dimensions(path: str) -> tuple[int, int]:
    """Quickly fetch video width/height with ffprobe; falls back to 1280x720."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=s=x:p=0",
            path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        text = stdout.decode().strip()
        if "x" in text:
            w_str, h_str = text.split("x")[:2]
            return int(w_str), int(h_str)
    except Exception as exc:  # noqa: BLE001
        logger.debug("ffprobe failed for %s: %s", path, exc)
    return 1280, 720


async def has_video_stream(path: str) -> Optional[bool]:
    """Use ffprobe to check whether a file genuinely contains a video
    stream. Returns ``True``/``False`` when ffprobe ran successfully, or
    ``None`` if ffprobe itself is unavailable/failed to run (in which case
    the caller should fall back to a raw file-signature check).

    This is used to guarantee the bot never sends an image/preview frame
    to a user who requested a video (see pinterest_service.py).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_type",
            "-of", "csv=p=0",
            path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            logger.debug("ffprobe exited with code %s for %s", proc.returncode, path)
            return None
        return "video" in stdout.decode(errors="ignore").strip().lower()
    except FileNotFoundError:
        logger.debug("ffprobe binary not found; skipping authoritative video validation.")
        return None
    except Exception as exc:  # noqa: BLE001
        logger.debug("ffprobe validation failed for %s: %s", path, exc)
        return None


def build_video_attributes(path: str, duration: Optional[float], width: int, height: int) -> list:
    filename = os.path.basename(path)
    return [
        DocumentAttributeFilename(filename),
        DocumentAttributeVideo(
            duration=int(duration or 0),
            w=width or 1280,
            h=height or 720,
            supports_streaming=True,
        ),
    ]


def build_audio_attributes(
    path: str,
    duration: Optional[float],
    title: Optional[str] = None,
    performer: Optional[str] = None,
) -> list:
    filename = os.path.basename(path)
    return [
        DocumentAttributeFilename(filename),
        DocumentAttributeAudio(
            duration=int(duration or 0),
            title=title,
            performer=performer,
            voice=False,
        ),
    ]


def build_document_attributes(path: str) -> list:
    return [DocumentAttributeFilename(os.path.basename(path))]


def guess_mime(path: str, default: str = "application/octet-stream") -> str:
    return mimetypes.guess_type(path)[0] or default
