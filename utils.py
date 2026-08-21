"""
utils.py
--------
Shared helper functions: URL validation, byte/speed/duration formatting,
progress bars, a per-user rate limiter, safe file deletion and ZIP
archive creation.
"""

from __future__ import annotations

import re
import shutil
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse

_URL_REGEX = re.compile(
    r"^(https?://)"
    r"([a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}"
    r"(:\d{1,5})?"
    r"(/[^\s]*)?$",
    re.IGNORECASE,
)

_BLOCKED_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


def is_valid_url(text: str) -> bool:
    """Validate a URL and reject internal/SSRF-prone hosts."""
    if not text or len(text) > 2000:
        return False
    text = text.strip()
    if not _URL_REGEX.match(text):
        return False
    try:
        parsed = urlparse(text)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    hostname = (parsed.hostname or "").lower()
    if not hostname or hostname in _BLOCKED_HOSTS:
        return False
    if hostname.startswith("192.168.") or hostname.startswith("10.") or hostname.startswith("169.254."):
        return False
    if re.match(r"^172\.(1[6-9]|2\d|3[0-1])\.", hostname):
        return False
    return True


def extract_first_url(text: str) -> str | None:
    """Extract the first valid URL found in an arbitrary text string."""
    for token in text.split():
        token = token.strip()
        if is_valid_url(token):
            return token
    return None


def format_bytes(size: float | None) -> str:
    if not size or size < 0:
        return "unknown"
    units = ["B", "KB", "MB", "GB", "TB"]
    index = 0
    size = float(size)
    while size >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    return f"{size:.1f} {units[index]}"


def format_speed(bytes_per_second: float | None) -> str:
    if not bytes_per_second or bytes_per_second <= 0:
        return "unknown"
    return f"{format_bytes(bytes_per_second)}/s"


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_eta(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "unknown"
    return format_duration(seconds)


def progress_bar(percent: float, length: int = 12) -> str:
    percent = max(0.0, min(100.0, percent))
    filled = int(length * percent / 100)
    return "🟩" * filled + "⬜️" * (length - filled)


def safe_delete(path: str | Path) -> None:
    """Delete a file or directory without ever raising."""
    p = Path(path)
    try:
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def sanitize_filename(name: str, max_length: int = 150) -> str:
    """Strip characters that are not safe for filenames while keeping the title readable."""
    name = re.sub(r'[\\/*?:"<>|\n\r\t]', "_", name).strip()
    name = re.sub(r"\s+", " ", name)
    name = name.strip(" .")
    if not name:
        name = "file"
    return name[:max_length]


def build_track_filename(title: str, artist: str | None = None, ext: str = "mp3") -> str:
    """Build the final filename for audio tracks (SoundCloud)."""
    safe_title = sanitize_filename(title or "track", max_length=120)
    return f"{safe_title}.{ext.lstrip('.')}"


def unique_path(directory: str | Path, filename: str) -> Path:
    """Avoid overwriting an existing file with the same name in a directory."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    counter = 2
    while True:
        candidate = directory / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def create_zip(files: list[str | Path], zip_path: str | Path) -> Path:
    """Create a ZIP archive from a list of files, streamed (low RAM usage)."""
    zip_path = Path(zip_path)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
        for index, f in enumerate(files, start=1):
            f = Path(f)
            if f.exists():
                zf.write(f, arcname=f"{index:02d}_{f.name}")
    return zip_path


_IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
)


def _read_header(path: str | Path, length: int = 32) -> bytes:
    try:
        with open(path, "rb") as fh:
            return fh.read(length)
    except OSError:
        return b""


def sniff_kind(path: str | Path) -> str:
    """Best-effort detection of a file's real type from its magic bytes.

    Returns one of ``"image"``, ``"video"``, ``"audio"`` or ``"unknown"``.
    This never trusts file extensions or HTTP Content-Type headers (both
    are easy to get wrong) — only the actual bytes on disk matter. Used to
    guarantee the bot never sends an image when a video was requested
    (or vice versa); see pinterest_service.py.
    """
    header = _read_header(path, 32)
    if not header:
        return "unknown"

    if len(header) >= 12 and header[0:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image"

    for magic, _ext in _IMAGE_SIGNATURES:
        if header.startswith(magic):
            return "image"

    if len(header) >= 8 and header[4:8] == b"ftyp":  # MP4 / MOV / 3GP
        return "video"

    if header.startswith(b"\x1a\x45\xdf\xa3"):  # EBML: WebM/Matroska
        return "video"

    if header.startswith(b"\x47"):  # MPEG-TS sync byte
        return "video"

    if header.startswith(b"ID3") or header.startswith(b"\xff\xfb") or header.startswith(b"\xff\xf3"):
        return "audio"

    return "unknown"


def sniff_image_extension(path: str | Path) -> str | None:
    """Return the correct file extension for an image based on its magic
    bytes, or ``None`` if the file is not a recognizable image."""
    header = _read_header(path, 16)
    if len(header) >= 12 and header[0:4] == b"RIFF" and header[8:12] == b"WEBP":
        return ".webp"
    for magic, ext in _IMAGE_SIGNATURES:
        if header.startswith(magic):
            return ext
    return None


class RateLimiter:
    """Simple sliding-window, per-user request rate limiter."""

    def __init__(self) -> None:
        self._hits: dict[int, list[float]] = {}

    def check(self, user_id: int, max_count: int, window_seconds: int) -> tuple[bool, float]:
        """Returns (allowed, seconds_to_wait_if_not_allowed)."""
        now = time.monotonic()
        hits = self._hits.setdefault(user_id, [])
        hits[:] = [t for t in hits if now - t < window_seconds]
        if len(hits) >= max_count:
            retry_after = window_seconds - (now - hits[0])
            return False, max(retry_after, 0)
        hits.append(now)
        return True, 0.0


def throttle(min_interval: float = 3.0):
    """Decorator limiting how often a callback (e.g. progress edit) can run,
    to avoid hitting Telegram's flood limits."""

    def decorator(func):
        last_called: dict[str, float] = {"t": 0.0}

        def wrapper(*args, **kwargs):
            now = time.monotonic()
            force = kwargs.pop("force", False)
            if not force and now - last_called["t"] < min_interval:
                return None
            last_called["t"] = now
            return func(*args, **kwargs)

        return wrapper

    return decorator
