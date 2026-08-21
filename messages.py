"""
messages.py
-----------
All user-facing text lives here as plain Python string templates. Keeping
every string in one module (instead of scattering literals across
handlers/services) makes the bot easy to localize or restyle later.

Language policy
----------------
Every USER-facing string (messages, buttons, errors, help) is in
**Persian (Farsi)**, as required for the public bot experience. Admin-only
surfaces (the NexiLink Manager Bot in ``manager_bot.py``) may stay
technical/English since they are only seen by the bot owner/admins.

Templates may contain `{placeholder}` fields that get filled in at render
time via `safe_format`.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Keys inside MESSAGES that the Manager Bot is allowed to customize at
#: runtime (persisted in SQLite, see database.py get/set_message_override).
#: Kept as an explicit allow-list so admins can only edit genuine
#: user-facing strings, never break formatting placeholders by accident.
CUSTOMIZABLE_KEYS: tuple[str, ...] = (
    "pornhub_link_received",
    "pornhub_warning",
    "pornhub_before_download",
    "pornhub_download_started",
    "pornhub_download_completed",
    "pornhub_error",
    "pornhub_admin_notify",
    "welcome",
    "help",
    "success",
    "error_generic",
    "error_download_failed",
    "cooldown_active",
    "cooldown_finished",
)

# In-memory overrides loaded from / synced with SQLite at runtime. Because
# the main bot and the Manager Bot share one Python process (see main.py),
# writing here from manager_bot.py takes effect immediately for handlers.py
# without any restart or IPC.
_OVERRIDES: dict[str, str] = {}

MESSAGES: dict[str, str] = {
    "welcome": (
        "👋 سلام {name}!\n\n"
        "به **نکسی‌لینک (NexiLink)** خوش اومدی — ربات همه‌کاره دانلود رسانه 🎬🎵\n\n"
        "کافیه لینک یوتیوب، تیک‌تاک، اینستاگرام، پینترست، توییتر/ایکس، فیسبوک، ساندکلاود، "
        "ردیت، ویمیو و ده‌ها سایت دیگه رو برام بفرستی تا با بهترین کیفیت ممکن برات دانلودش کنم.\n\n"
        "برای دیدن همهٔ امکانات از /help استفاده کن یا روی دکمهٔ زیر بزن تا لیست کامل "
        "سایت‌های پشتیبانی‌شده رو ببینی."
    ),
    "help": (
        "📖 **راهنمای استفاده از نکسی‌لینک**\n\n"
        "1️⃣ لینک ویدیو/عکس/آهنگ رو برام بفرست.\n"
        "2️⃣ پلتفرم به‌صورت خودکار شناسایی می‌شه.\n"
        "3️⃣ برای لینک‌های عمومی ویدیو، کیفیت دلخواه رو از دکمه‌ها انتخاب کن.\n"
        "4️⃣ صبر کن تا دانلود و آپلود انجام بشه.\n\n"
        "**دستورات ربات**\n"
        "/start — شروع مجدد ربات\n"
        "/help — نمایش همین راهنما\n"
        "/platforms — لیست همهٔ سایت‌های پشتیبانی‌شده\n"
        "/settings — تنظیمات و محدودیت‌های فعلی\n"
        "/status — وضعیت درخواست فعلی شما\n"
        "/cancel — لغو عملیات فعلی\n"
        "/about — دربارهٔ ربات\n\n"
        "💡 همچنین می‌تونی از حالت **اینلاین** استفاده کنی: در هر چتی تایپ کن "
        "`@نام‌کاربری‌ربات لینک`."
    ),
    "banned_user": "🚫 دسترسی شما به این ربات مسدود شده است.",
    "url_received": "🔗 لینک دریافت شد. در حال شناسایی پلتفرم...",
    "invalid_url": (
        "❌ این یک لینک معتبر به نظر نمی‌رسه.\n\n"
        "لطفاً لینکی که با http:// یا https:// شروع می‌شه رو ارسال کن."
    ),
    "unsupported_platform": "❌ متأسفانه این لینک پشتیبانی نمی‌شه یا رسانه‌ای در آن یافت نشد.",
    "extracting_info": "🔍 در حال دریافت اطلاعات رسانه، لطفاً صبر کن...",
    "video_detected": (
        "✅ رسانه پیدا شد!\n\n"
        "🌐 پلتفرم: {platform}\n"
        "🎬 عنوان: {title}\n"
        "⏱ مدت زمان: {duration}\n\n"
        "لطفاً کیفیت مورد نظر رو انتخاب کن:"
    ),
    "quality_selection": "🎚 لطفاً کیفیت دانلود رو انتخاب کن:",
    "no_formats_found": "❌ هیچ کیفیت قابل دانلودی برای این لینک پیدا نشد.",
    "download_started": "⬇️ دانلود شروع شد ({quality})...",
    "download_progress": (
        "⬇️ در حال دانلود...\n\n"
        "{bar}  {percent}%\n\n"
        "📦 حجم: {downloaded} / {total}\n"
        "🚀 سرعت: {speed}\n"
        "⏳ زمان باقی‌مانده: {eta}"
    ),
    "processing": "⚙️ در حال پردازش (ترکیب صدا و تصویر با FFmpeg)...",
    "upload_started": "⬆️ در حال آماده‌سازی برای آپلود در تلگرام...",
    "upload_progress": (
        "⬆️ در حال آپلود در تلگرام...\n\n"
        "{bar}  {percent}%\n\n"
        "📦 حجم: {uploaded} / {total}\n"
        "🚀 سرعت: {speed}"
    ),
    "success": (
        "🎉 با موفقیت ارسال شد!\n\n"
        "🎬 عنوان: {title}\n"
        "🎚 کیفیت: {quality}\n"
        "📦 حجم: {size}\n\n"
        "هر وقت خواستی یک لینک دیگه بفرست 🚀"
    ),
    "error_generic": "❌ مشکلی پیش اومد: {error}\n\nلطفاً دوباره تلاش کن.",
    "error_extraction_failed": (
        "❌ دریافت اطلاعات این لینک ناموفق بود.\n\n"
        "جزئیات: {error}\n\n"
        "ممکنه محتوا خصوصی، حذف‌شده یا محدود به یک منطقهٔ جغرافیایی خاص باشه."
    ),
    "error_download_failed": "❌ دانلود ناموفق بود.\n\nجزئیات: {error}",
    "error_upload_failed": "❌ آپلود در تلگرام ناموفق بود.\n\nجزئیات: {error}",
    "error_file_too_large": (
        "❌ حجم فایل ({size}) از حداکثر مجاز ({max_size}) بیشتره.\n\n"
        "لطفاً کیفیت پایین‌تری انتخاب کن."
    ),
    "error_rate_limit": "⏳ درخواست‌های شما خیلی سریع ارسال می‌شه.\n\nلطفاً {seconds} ثانیه دیگه دوباره تلاش کن.",
    "error_already_processing": (
        "⚠️ شما در حال حاضر یک درخواست در حال پردازش دارید.\n\n"
        "لطفاً صبر کن، یا با /cancel اون رو لغو کن."
    ),
    "cancel_success": "🛑 عملیات فعلی لغو شد.",
    "cancel_nothing": "ℹ️ هیچ عملیات فعالی برای لغو کردن نداری.",
    "status_idle": "ℹ️ درخواست فعالی نداری.\n\nیک لینک بفرست تا شروع کنیم!",
    "status_active": "📊 درخواست فعلی:\n\n🔗 لینک: {url}\n📍 مرحله: {stage}\n",
    # -----------------------------------------------------------------
    # Reply keyboard / main menu
    # -----------------------------------------------------------------
    "menu_download": "📥 دانلود",
    "menu_platforms": "📋 سایت‌های پشتیبانی‌شده",
    "menu_help": "❓ راهنما",
    "menu_settings": "⚙️ تنظیمات",
    "menu_about": "ℹ️ درباره ربات",
    "menu_download_prompt": (
        "📥 برای دانلود کافیه لینک مورد نظرت رو (یوتیوب، تیک‌تاک، اینستاگرام، پینترست، "
        "ساندکلاود، ...) همین‌جا برام بفرستی."
    ),
    "settings_text": (
        "⚙️ **تنظیمات فعلی ربات**\n\n"
        "📦 حداکثر حجم فایل: {max_size}\n"
        "⏱ محدودیت درخواست: {rate_count} درخواست هر {rate_window} ثانیه\n"
        "🔀 حداکثر دانلود همزمان: {max_concurrent}\n\n"
        "این مقادیر برای پایداری ربات روی سرور تنظیم شده‌اند."
    ),
    "about_text": (
        "ℹ️ **دربارهٔ نکسی‌لینک**\n\n"
        "نکسی‌لینک یک ربات دانلود همه‌کاره برای تلگرامه که با Telethon و yt-dlp "
        "ساخته شده و از یوتیوب، تیک‌تاک، اینستاگرام، پینترست، توییتر/ایکس، فیسبوک، "
        "ساندکلاود، ردیت، ویمیو و صدها سایت دیگه پشتیبانی می‌کنه.\n\n"
        "🔖 نسخه: {version}\n"
        "👨‍💻 توسعه‌دهنده: تیم نکسی‌لینک"
    ),
    # -----------------------------------------------------------------
    # Supported platforms
    # -----------------------------------------------------------------
    "platforms_button": "📋 سایت‌های پشتیبانی‌شده",
    "platforms_title": "📥 **سایت‌های پشتیبانی‌شده**\n\nنکسی‌لینک می‌تونه از این سایت‌ها دانلود کنه:\n",
    "platforms_category": "\n**{category}**\n",
    "platforms_item": "  {icon} {name}\n",
    "platforms_footer": (
        "\nسایت مورد علاقه‌ت رو نمی‌بینی؟ نکسی‌لینک به‌صورت خودکار برای صدها سایت دیگه هم "
        "تلاش می‌کنه دانلود کنه."
    ),
    # -----------------------------------------------------------------
    # Pinterest
    # -----------------------------------------------------------------
    "pinterest_downloading": "📌 در حال دانلود از پینترست...",
    "pinterest_error": "❌ دانلود از پینترست ناموفق بود.\n\nجزئیات: {error}",
    "pinterest_carousel_found": "🖼 این یک پست چندتایی (کاروسل) با {count} آیتم است.",
    "pinterest_carousel_ask_format": (
        "🖼 این پین یک کاروسل با {count} آیتم است.\n\n"
        "می‌خوای فایل‌ها رو چطوری دریافت کنی؟\n\n"
        "1️⃣ ارسال جداگانه هر فایل\n"
        "2️⃣ ارسال به‌صورت یک فایل ZIP"
    ),
    "pinterest_carousel_sending_separate": "📤 در حال ارسال {count} فایل به‌صورت جداگانه...",
    "pinterest_success_single": "✅ {kind} پینترست با موفقیت و در کیفیت اصلی ارسال شد.",
    "pinterest_success_carousel": "✅ کاروسل پینترست با موفقیت ارسال شد ({count} آیتم).",
    "pinterest_zip_building": "📦 در حال ساخت فایل ZIP از {count} آیتم...",
    "pinterest_carousel_expired": "⌛️ زمان انتخاب نوع ارسال تمام شد. لطفاً لینک را دوباره ارسال کنید.",
    # -----------------------------------------------------------------
    # Instagram
    # -----------------------------------------------------------------
    "instagram_downloading": "📷 در حال دانلود از اینستاگرام...",
    "instagram_carousel_found": "🖼 این یک پست چندتایی (کاروسل) با {count} آیتم است. در حال دانلود همه...",
    "instagram_success_single": "✅ {kind} اینستاگرام با موفقیت و در کیفیت اصلی ارسال شد.",
    "instagram_success_carousel": "✅ کاروسل اینستاگرام با موفقیت ارسال شد ({count} آیتم).",
    "instagram_zip_building": "📦 در حال ساخت فایل ZIP از {count} آیتم...",
    "instagram_error": "❌ دانلود از اینستاگرام ناموفق بود.\n\nجزئیات: {error}",
    # -----------------------------------------------------------------
    # SoundCloud
    # -----------------------------------------------------------------
    "soundcloud_downloading": "🎧 در حال دانلود از ساندکلاود...",
    "soundcloud_track_success": "🎉 آهنگ «{title}» با موفقیت ارسال شد.",
    "soundcloud_track_error": "❌ دانلود آهنگ ساندکلاود ناموفق بود.\n\nجزئیات: {error}",
    "soundcloud_playlist_found": "🎶 پلی‌لیست «{name}» دارای {count} آهنگ است. شروع دانلود...",
    "soundcloud_playlist_progress": "🎵 [{index}/{total}] در حال دانلود: {title}",
    "soundcloud_playlist_track_failed": "⚠️ [{index}/{total}] «{title}» ناموفق بود: {error}",
    "soundcloud_zip_building": "📦 در حال ساخت فایل ZIP از {count} آهنگ...",
    "soundcloud_playlist_finished": "✅ پلی‌لیست تمام شد: {success}/{total} آهنگ با موفقیت ارسال شد.",
    # -----------------------------------------------------------------
    # PornHub (optional, disabled via PORNHUB_ENABLED=false)
    # -----------------------------------------------------------------
    "pornhub_link_received": "🔗 لینک دریافت شد. در حال بررسی...",
    "pornhub_warning": "⚠️ توجه: این لینک ممکن است محتوای بزرگسالان داشته باشد.",
    "pornhub_before_download": "🎬 عنوان: {title}\n⏱ مدت زمان: {duration}\n\nدر حال آماده‌سازی دانلود...",
    "pornhub_download_started": "⬇️ دانلود شروع شد...",
    "pornhub_error": "❌ دانلود ناموفق بود.\n\nجزئیات: {error}",
    "pornhub_admin_notify": (
        "🔔 اعلان دانلود PornHub\n\n"
        "👤 شناسه کاربر: `{user_id}`\n"
        "🔗 نام کاربری: {username}\n"
        "🎬 عنوان ویدیو: {title}\n"
        "📊 وضعیت: {status}\n"
        "🕒 زمان: {time}"
    ),
    # -----------------------------------------------------------------
    # X / Twitter
    # -----------------------------------------------------------------
    "twitter_downloading": "🐦 در حال دانلود از ایکس/توییتر...",
    "twitter_carousel_found": "🖼 این پست چند رسانه‌ای با {count} آیتم است.",
    "twitter_success_single": "✅ {kind} ایکس/توییتر با موفقیت و در کیفیت اصلی ارسال شد.",
    "twitter_success_carousel": "✅ رسانه‌های این پست ایکس/توییتر با موفقیت ارسال شد ({count} آیتم).",
    "twitter_zip_building": "📦 در حال ساخت فایل ZIP از {count} آیتم...",
    "twitter_error": "❌ دانلود از ایکس/توییتر ناموفق بود.\n\nجزئیات: {error}",
    # -----------------------------------------------------------------
    # Shared multi-item (carousel) delivery choice — Instagram, Pinterest, X
    # -----------------------------------------------------------------
    "carousel_choice_prompt": (
        "🖼 این پست شامل {count} فایل است.\n\n"
        "چطور دریافت کنم؟"
    ),
    "carousel_btn_separate": "📤 ارسال جداگانه فایل‌ها",
    "carousel_btn_zip": "📦 ارسال به‌صورت ZIP",
    "carousel_expired": "⚠️ این درخواست منقضی شده است. لطفاً دوباره لینک را ارسال کن.",
    # -----------------------------------------------------------------
    # Cooldown system
    # -----------------------------------------------------------------
    "cooldown_active": (
        "⏳ به دلیل استفاده زیاد از ربات، لازمه کمی صبر کنی.\n\n"
        "⏱ زمان باقی‌مانده: {countdown}"
    ),
    "cooldown_finished": "✅ زمان استراحت تمام شد! حالا می‌تونی یک لینک دیگه بفرستی.",
    # -----------------------------------------------------------------
    # Inline mode
    # -----------------------------------------------------------------
    "inline_prompt_title": "🔗 یک لینک بفرست تا دانلودش کنم",
    "inline_prompt_description": "بعد از نام کاربری ربات، لینک ویدیو/آهنگ رو تایپ کن.",
    "inline_prompt_text": "یک لینک (یوتیوب، تیک‌تاک، اینستاگرام، پینترست، ...) بعد از نام ربات بفرست تا شروع کنیم.",
    "inline_extract_failed_title": "❌ دریافت اطلاعات این لینک ناموفق بود",
    "inline_no_formats_title": "❌ هیچ کیفیت قابل دانلودی پیدا نشد",
    "inline_result_description": "{platform} • {duration}",
    "inline_result_text": "🎬 **{title}**\n🌐 {platform}\n⏱ {duration}\n\nیکی از گزینه‌های زیر رو انتخاب کن:",
    "inline_btn_video": "🎬 دانلود ویدیو",
    "inline_btn_audio": "🎵 دانلود صدا",
    "inline_starting": "⏳ دانلود در پس‌زمینه شروع شد...",
    "inline_expired": "⌛ این درخواست منقضی شده، لطفاً دوباره لینک را ارسال کن.",
    "inline_attach_failed": (
        "✅ دانلود با حجم {size} تموم شد، ولی تلگرام اجازه نداد فایل به این پیام اینلاین ضمیمه بشه.\n\n"
        "لطفاً مستقیماً به ربات پیام بده تا فایل رو برات بفرستم."
    ),
}


def safe_format(template: str, **kwargs: object) -> str:
    """Safely format a message template. Never raises: on failure the raw
    template is returned so the user always gets *some* response."""
    try:
        return template.format(**kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to format message: %s | template=%r", exc, template)
        return template


def get(key: str, **kwargs: object) -> str:
    template = _OVERRIDES.get(key) or MESSAGES.get(key, key)
    return safe_format(template, **kwargs)


def get_default(key: str) -> str:
    """Return the built-in (non-overridden) template for a key."""
    return MESSAGES.get(key, key)


def get_effective(key: str) -> str:
    """Return whatever template is currently in effect (override or default)."""
    return _OVERRIDES.get(key) or MESSAGES.get(key, key)


def is_customizable(key: str) -> bool:
    return key in CUSTOMIZABLE_KEYS


def set_override(key: str, text: str) -> None:
    """Apply a runtime override (called by manager_bot.py). Does not touch
    the database itself — persistence is handled by database.py so this
    module has no SQLite dependency."""
    _OVERRIDES[key] = text


def clear_override(key: str) -> None:
    _OVERRIDES.pop(key, None)


def load_overrides(overrides: dict[str, str]) -> None:
    """Bulk-load overrides fetched from the database at startup."""
    for key, value in overrides.items():
        if key in CUSTOMIZABLE_KEYS and value:
            _OVERRIDES[key] = value
