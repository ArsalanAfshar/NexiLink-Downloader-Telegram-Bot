"""
fast_upload.py
--------------
آپلود موازی فایل به تلگرام با استفاده از چند اتصال MTProto هم‌زمان.

چرا لازم است؟
    Telethon به‌صورت پیش‌فرض هر فایل را با یک اتصال TCP واحد و به‌صورت
    ترتیبی (chunk به chunk) آپلود می‌کند. سرعت نهایی در این حالت مستقیماً
    به تأخیر رفت‌وبرگشت (round-trip latency) هر درخواست بستگی دارد، نه به
    پهنای باند واقعی سرور. مستندات رسمی تلگرام دقیقاً همین راه‌حل را توصیه
    می‌کند:
    https://core.telegram.org/api/files#uploading-files
    « to further increase performance, multiple parallel connections ...
      can be used to upload multiple chunks in parallel »

    این ماژول دقیقاً همین کار را انجام می‌دهد: چند اتصال MTProto جداگانه
    (اما با همان کلید احراز هویت نشست فعلی) به همان دیتاسنتر باز می‌کند و
    قطعات فایل را به‌صورت round-robin و هم‌زمان روی آن‌ها ارسال می‌کند.

    تیم Telethon به‌صراحت این قابلیت را built-in نکرده (نگاه کنید به FAQ
    رسمی‌شان) چون معتقدند FloodWait عامل محدودکننده‌ی اصلی است؛ به همین
    دلیل این پیاده‌سازی عمداً محافظه‌کارانه است:
      - فقط برای فایل‌های بزرگ‌تر از UPLOAD_PARALLEL_MIN_MB فعال می‌شود.
      - تعداد اتصال‌ها متناسب با حجم فایل و با سقف UPLOAD_MAX_CONNECTIONS
        (پیش‌فرض ۶) تنظیم می‌شود؛ این مقدار برای Railway Free Plan (منابع
        محدود CPU/RAM) امن و پایدار است.
      - در صورت بروز هرگونه خطا، فراخواننده باید به آپلود معمولی
        (client.send_file با مسیر فایل) بازگردد؛ این ماژول قابلیت
        Fallback خودکار را در ``handlers.py`` فراهم می‌کند.

مصرف حافظه: فقط یک chunk (حداکثر ۵۱۲ کیلوبایت) در لحظه در RAM نگه داشته
می‌شود (خواندن استریمی از دیسک)، بنابراین صرف‌نظر از حجم فایل مصرف RAM
تقریباً ثابت و ناچیز باقی می‌ماند.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import math
import os
import time
from typing import Awaitable, Callable, List, Optional, Union

from telethon import TelegramClient, helpers, utils
from telethon.crypto import AuthKey
from telethon.network import MTProtoSender
from telethon.tl.functions.upload import SaveBigFilePartRequest, SaveFilePartRequest
from telethon.tl.types import InputFile, InputFileBig, TypeInputFile

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int], Union[None, Awaitable[None]]]

MAX_CONNECTIONS_DEFAULT = 6
MIN_PARALLEL_SIZE_DEFAULT_MB = 10
FULL_SPEED_SIZE = 100 * 1024 * 1024  # از این حجم به بعد از حداکثر تعداد اتصال استفاده می‌شود


def _settings() -> tuple[int, int]:
    """خواندن تنظیمات از config.py به‌صورت تنبل (lazy) برای جلوگیری از circular import."""
    try:
        import config

        return config.UPLOAD_MAX_CONNECTIONS, config.UPLOAD_PARALLEL_MIN_MB
    except Exception:  # noqa: BLE001
        return MAX_CONNECTIONS_DEFAULT, MIN_PARALLEL_SIZE_DEFAULT_MB


class _UploadSender:
    """نگهدارنده‌ی یک اتصال MTProto اختصاصی برای ارسال قطعات فایل."""

    def __init__(
        self,
        client: TelegramClient,
        sender: MTProtoSender,
        file_id: int,
        part_count: int,
        big: bool,
        index: int,
        stride: int,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.client = client
        self.sender = sender
        self.part_count = part_count
        if big:
            self.request: Union[SaveBigFilePartRequest, SaveFilePartRequest] = SaveBigFilePartRequest(
                file_id, index, part_count, b""
            )
        else:
            self.request = SaveFilePartRequest(file_id, index, b"")
        self.stride = stride
        self.previous: Optional[asyncio.Task] = None
        self.loop = loop

    async def next(self, data: bytes) -> None:
        if self.previous:
            await self.previous
        self.previous = self.loop.create_task(self._next(data))

    async def _next(self, data: bytes) -> None:
        self.request.bytes = data
        await self.client._call(self.sender, self.request)
        self.request.file_part += self.stride

    async def disconnect(self) -> None:
        if self.previous:
            await self.previous
        await self.sender.disconnect()


class ParallelUploader:
    """مدیریت مجموعه‌ای از اتصال‌های MTProto برای آپلود موازی یک فایل واحد."""

    def __init__(self, client: TelegramClient) -> None:
        self.client = client
        self.loop = client.loop
        self.dc_id = client.session.dc_id
        # چون همیشه به همان دیتاسنتر نشست فعلی آپلود می‌کنیم، نیازی به
        # export/import کردن مجدد Authorization نیست؛ کلید فعلی معتبر است.
        self.auth_key: AuthKey = client.session.auth_key
        self.senders: Optional[List[_UploadSender]] = None
        self._ticker = 0

    @staticmethod
    def connection_count(file_size: int) -> int:
        max_connections, min_mb = _settings()
        min_size = max(min_mb, 1) * 1024 * 1024
        if file_size <= min_size:
            return 1
        if file_size >= FULL_SPEED_SIZE:
            return max(1, max_connections)
        ratio = file_size / FULL_SPEED_SIZE
        return max(2, min(max_connections, math.ceil(ratio * max_connections)))

    async def _create_sender(self) -> MTProtoSender:
        dc = await self.client._get_dc(self.dc_id)
        sender = MTProtoSender(self.auth_key, loggers=self.client._log)
        await sender.connect(
            self.client._connection(
                dc.ip_address,
                dc.port,
                dc.id,
                loggers=self.client._log,
                proxy=self.client._proxy,
            )
        )
        return sender

    async def _create_upload_sender(self, file_id: int, part_count: int, big: bool, index: int, stride: int) -> _UploadSender:
        return _UploadSender(self.client, await self._create_sender(), file_id, part_count, big, index, stride, self.loop)

    async def init(self, file_id: int, file_size: int, part_count: int, big: bool) -> int:
        connections = self.connection_count(file_size)
        self.senders = [
            await self._create_upload_sender(file_id, part_count, big, 0, connections),
            *await asyncio.gather(
                *[self._create_upload_sender(file_id, part_count, big, i, connections) for i in range(1, connections)]
            ),
        ]
        return connections

    async def upload(self, part: bytes) -> None:
        await self.senders[self._ticker].next(part)
        self._ticker = (self._ticker + 1) % len(self.senders)

    async def finish(self) -> None:
        if self.senders:
            await asyncio.gather(*[s.disconnect() for s in self.senders], return_exceptions=True)
            self.senders = None


async def upload_file(
    client: TelegramClient,
    path: str,
    progress_callback: Optional[ProgressCallback] = None,
) -> TypeInputFile:
    """
    آپلود یک فایل با اتصال‌های موازی MTProto و بازگرداندن شیء
    ``InputFile``/``InputFileBig`` آماده برای استفاده در
    ``client.send_file(chat, file=<خروجی این تابع>, ...)``.
    """
    file_size = os.path.getsize(path)
    part_size = int(utils.get_appropriated_part_size(file_size) * 1024)
    part_count = (file_size + part_size - 1) // part_size
    is_large = file_size > 10 * 1024 * 1024
    file_id = helpers.generate_random_long()
    name = os.path.basename(path) or "file"

    uploader = ParallelUploader(client)
    connections = await uploader.init(file_id, file_size, part_count, is_large)
    logger.info(
        "Parallel upload started: %s (%.1f MB) using %d connection(s), part_size=%dKB",
        name, file_size / 1024 / 1024, connections, part_size // 1024,
    )

    md5 = hashlib.md5() if not is_large else None
    uploaded = 0
    start = time.monotonic()

    try:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(part_size)
                if not chunk:
                    break
                if md5 is not None:
                    md5.update(chunk)
                await uploader.upload(chunk)
                uploaded += len(chunk)
                if progress_callback:
                    result = progress_callback(uploaded, file_size)
                    if inspect.isawaitable(result):
                        await result
    finally:
        await uploader.finish()

    elapsed = max(time.monotonic() - start, 0.001)
    speed_mb_s = (file_size / 1024 / 1024) / elapsed
    logger.info("Parallel upload finished: %s in %.1fs (%.2f MB/s)", name, elapsed, speed_mb_s)

    if is_large:
        return InputFileBig(file_id, part_count, name)
    return InputFile(file_id, part_count, name, md5.hexdigest() if md5 else "")


def should_use_parallel(file_size: int) -> bool:
    _, min_mb = _settings()
    return file_size > max(min_mb, 1) * 1024 * 1024


async def send_file_fast(
    client: TelegramClient,
    chat_id: Union[int, str],
    path: str,
    *,
    caption: Optional[str] = None,
    thumb: Optional[str] = None,
    attributes: Optional[list] = None,
    mime_type: Optional[str] = None,
    force_document: bool = False,
    buttons=None,
    progress_callback: Optional[ProgressCallback] = None,
    reply_to: Optional[int] = None,
):
    """
    ارسال فایل به تلگرام با تلاش برای استفاده از آپلود موازی (سریع‌تر) و
    بازگشت خودکار (fallback) به روش معمولی Telethon در صورت بروز هر خطا؛
    این تابع هرگز باعث شکست کامل ارسال فایل نمی‌شود.
    """
    file_size = os.path.getsize(path)

    if not should_use_parallel(file_size):
        return await client.send_file(
            chat_id,
            path,
            caption=caption,
            thumb=thumb,
            attributes=attributes,
            mime_type=mime_type,
            force_document=force_document,
            buttons=buttons,
            supports_streaming=not force_document,
            progress_callback=progress_callback,
            reply_to=reply_to,
        )

    try:
        input_file = await upload_file(client, path, progress_callback=progress_callback)
        thumb_file = await client.upload_file(thumb) if thumb else None
        return await client.send_file(
            chat_id,
            file=input_file,
            caption=caption,
            thumb=thumb_file,
            attributes=attributes,
            mime_type=mime_type,
            force_document=force_document,
            buttons=buttons,
            supports_streaming=not force_document,
            reply_to=reply_to,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Parallel upload failed; falling back to Telethon default upload.")
        return await client.send_file(
            chat_id,
            path,
            caption=caption,
            thumb=thumb,
            attributes=attributes,
            mime_type=mime_type,
            force_document=force_document,
            buttons=buttons,
            supports_streaming=not force_document,
            progress_callback=progress_callback,
            reply_to=reply_to,
        )
