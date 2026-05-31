import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone, date

RATING_CHAT_ID = int(os.getenv("RATING_CHAT_ID", "0"))
_rating_thread_raw = os.getenv("RATING_THREAD_ID", "").strip()
RATING_THREAD_ID = int(_rating_thread_raw) if _rating_thread_raw.lstrip("-").isdigit() else None


def _parse_rating_targets() -> list[tuple[int, int | None]]:
    """Parse RATING_CHAT_IDS env var (comma-separated).

    Each item is either '-100123' (no thread) or '-100123:45' (with thread).
    Falls back to legacy RATING_CHAT_ID/RATING_THREAD_ID if the new var is empty.
    """
    raw = os.getenv("RATING_CHAT_IDS", "").strip()
    targets: list[tuple[int, int | None]] = []
    if raw:
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            chat_part, _, thread_part = part.partition(":")
            try:
                cid = int(chat_part.strip())
            except ValueError:
                continue
            tid: int | None = None
            if thread_part:
                try:
                    tid = int(thread_part.strip())
                except ValueError:
                    tid = None
            targets.append((cid, tid))

    # Backward compat: include legacy single chat if not already in list.
    if RATING_CHAT_ID and not any(c == RATING_CHAT_ID for c, _ in targets):
        targets.append((RATING_CHAT_ID, RATING_THREAD_ID))

    return targets


RATING_TARGETS = _parse_rating_targets()

# Кому надсилати DM-ескалації про пропущені скріни (login/story/post/logout)
OWNER_TELEGRAM_ID = int(os.getenv("OWNER_TELEGRAM_ID", "0"))

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import BOT_TOKEN, ADMIN_TELEGRAM_ID, ADMIN_TELEGRAM_IDS
from database import (
    init_db,
    add_monitored_chat,
    get_active_chats,
    deactivate_chat,
    find_monitored,
    add_submission,
    get_submissions,
    get_submissions_by_category,
    get_today_status,
    CAT_LOGIN, CAT_STORY, CAT_POST, CAT_LOGOUT,
)
from analyzer import analyze_photo, format_feedback
from sheets import get_balances_for_day, format_rating
from image_renderer import render_rating_image
from aiogram.types import BufferedInputFile

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

KYIV_TZ = timezone(timedelta(hours=3))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# ── Helpers ─────────────────────────────────────────────────────────────────

def kyiv_now() -> datetime:
    return datetime.now(KYIV_TZ)


def get_current_shift() -> tuple[int | None, date | None]:
    now = kyiv_now()
    h, d = now.hour, now.date()
    if 6 <= h < 8:
        return 6, d
    if 14 <= h < 16:
        return 14, d
    if 22 <= h < 24:
        return 22, d
    return None, None


def shift_date_for_report(shift_hour: int) -> date:
    """At 00:00 Kyiv the date has rolled forward; shift 22 started yesterday."""
    now = kyiv_now()
    if shift_hour == 22:
        return (now - timedelta(days=1)).date()
    return now.date()


def fmt_user(row: dict) -> str:
    if row.get("username"):
        return f"@{row['username']}"
    uid = row["telegram_id"]
    return f'<a href="tg://user?id={uid}">{uid}</a>'


def is_admin(message: Message) -> bool:
    return bool(
        message.from_user
        and message.from_user.id in ADMIN_TELEGRAM_IDS
    )


# ── Media handler ────────────────────────────────────────────────────────────

def _closest_shift_hour(now: datetime) -> int:
    """Return 6 / 14 / 22 — the nearest shift boundary for `now`.

    Windows ±4 годин навколо кожної межі:
      04:00-12:00 → 6
      12:00-20:00 → 14
      20:00-04:00 → 22 (з переходом через північ)
    """
    h = now.hour
    if 4 <= h < 12:
        return 6
    if 12 <= h < 20:
        return 14
    return 22  # 20-23 або 0-3


def _shift_date_for(now: datetime, shift_hour: int) -> date:
    """For shift 22 around midnight (0:00-3:59), return yesterday's date —
    тобто дату, коли межа 22:00 фактично настала."""
    if shift_hour == 22 and 0 <= now.hour < 4:
        return (now - timedelta(days=1)).date()
    return now.date()


_CATEGORY_LABELS = {
    CAT_LOGIN: "Log in",
    CAT_STORY: "Story",
    CAT_POST: "Post",
    CAT_LOGOUT: "Log out",
}


def _detect_from_caption(caption: str | None) -> str | None:
    """Return 'login' or 'logout' if caption explicitly says so.

    Login/logout скріни візуально однакові — розрізняти можна лише за caption.
    Logout перевіряємо першим, бо 'logout' як підстрока містить 'log'.
    """
    if not caption:
        return None
    text = caption.lower().strip()
    logout_markers = ("log out", "logout", "лог аут", "логаут", "вихід", "виходжу")
    login_markers = ("log in", "login", "лог ін", "логін", "вхід", "захожу")
    for m in logout_markers:
        if m in text:
            return CAT_LOGOUT
    for m in login_markers:
        if m in text:
            return CAT_LOGIN
    return None


@dp.message(
    (F.photo | F.video | F.document),
    F.chat.type.in_({"group", "supergroup"}),
)
async def handle_media(message: Message):
    if not message.from_user:
        return

    chat_id = message.chat.id
    thread_id = message.message_thread_id

    record = await find_monitored(chat_id, thread_id)
    if not record:
        logger.info("MEDIA ignored (not monitored): chat=%s thread=%s", chat_id, thread_id)
        return

    now = kyiv_now()

    # Аналізуємо фото через Gemini — і для класифікації, і для оцінки якості.
    analysis: dict | None = None
    if message.photo:
        try:
            photo = message.photo[-1]
            file = await bot.get_file(photo.file_id)
            buf = await bot.download_file(file.file_path)
            image_bytes = buf.read() if hasattr(buf, "read") else bytes(buf)
            analysis = await analyze_photo(image_bytes)
        except Exception as exc:
            logger.error("Photo analysis failed: %s", exc)

    # Визначаємо категорію
    # 1) Підпис до фото — найвищий пріоритет (login/logout візуально однакові)
    caption_hint = _detect_from_caption(message.caption)
    if caption_hint:
        category = caption_hint
        logger.info("Classified via caption: %s (caption=%r)",
                    category, (message.caption or "")[:80])
    # 2) AI-класифікація
    elif analysis and analysis.get("screenshot_type") in (CAT_LOGIN, CAT_STORY, CAT_POST, CAT_LOGOUT):
        category = analysis["screenshot_type"]
        logger.info("AI classified screenshot as: %s", category)
    else:
        # Якщо це не фото (video/document) або AI не зміг розпізнати — за замовч. story
        category = CAT_STORY
        if message.photo:
            logger.warning(
                "AI classification missing (analysis=%s) — defaulting to story",
                analysis,
            )

    # shift_hour і shift_date залежать від категорії
    if category == CAT_POST:
        shift_hour: int | None = None
        shift_date = now.date()
    elif category in (CAT_LOGIN, CAT_LOGOUT):
        shift_hour = _closest_shift_hour(now)
        shift_date = _shift_date_for(now, shift_hour)
    else:  # CAT_STORY
        sh, sd = get_current_shift()
        if sh is not None:
            shift_hour, shift_date = sh, sd
        else:
            # Поза вікном зміни — все одно фіксуємо до поточної дати/найближчої зміни
            shift_hour = _closest_shift_hour(now)
            shift_date = _shift_date_for(now, shift_hour)

    await add_submission(
        chat_id, thread_id,
        message.from_user.id,
        message.from_user.username,
        shift_hour, shift_date,
        category,
    )
    logger.info(
        "MEDIA recorded: chat=%s thread=%s category=%s shift=%s date=%s",
        chat_id, thread_id, category, shift_hour, shift_date,
    )

    # Підтвердження з типом
    label = _CATEGORY_LABELS.get(category, "")
    confirmation = f"+ {label}" if label else "+"
    try:
        await message.reply(confirmation)
    except Exception as exc:
        logger.error("Reply confirmation failed: %s", exc)

    # Зворотний звʼязок про якість — тільки для story/post
    if category in (CAT_STORY, CAT_POST):
        feedback = format_feedback(analysis)
        if feedback:
            try:
                await message.reply(feedback)
            except Exception as exc:
                logger.error("Reply feedback failed: %s", exc)


# ── Admin commands ───────────────────────────────────────────────────────────

@dp.message(Command("whereami"))
async def cmd_whereami(message: Message):
    if not is_admin(message):
        return
    chat_id = message.chat.id
    thread_id = message.message_thread_id
    chat_type = message.chat.type
    chat_title = message.chat.title or "(особисті)"
    await message.reply(
        f"<b>Інфо про цей чат:</b>\n"
        f"Назва: {chat_title}\n"
        f"Тип: <code>{chat_type}</code>\n"
        f"chat_id: <code>{chat_id}</code>\n"
        f"thread_id: <code>{thread_id}</code>\n\n"
        f"Команда для додавання:\n"
        f"<code>/add_chat {chat_id} {thread_id} Назва</code>",
        parse_mode="HTML",
    )


@dp.message(Command("add_story_chat"))
async def cmd_add_story_chat(message: Message):
    if not is_admin(message):
        return
    if message.chat.type not in ("group", "supergroup"):
        await message.reply("Напишіть цю команду прямо в потрібній гілці групи.")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Використання: /add_story_chat Назва")
        return

    chat_name = parts[1].strip()
    chat_id = message.chat.id
    thread_id = message.message_thread_id

    existing = await find_monitored(chat_id, thread_id)
    if existing:
        await message.reply(f"Ця гілка вже підключена як «{existing['chat_name']}».")
        return

    await add_monitored_chat(chat_id, thread_id, chat_name)
    t = f" (тред {thread_id})" if thread_id else ""
    await message.reply(f"✅ Додано «{chat_name}»{t}")


@dp.message(Command("add_chat"))
async def cmd_add_chat(message: Message):
    if not is_admin(message):
        return
    parts = message.text.split(maxsplit=3)
    if len(parts) < 4:
        await message.reply(
            "Використання: /add_chat chat_id thread_id Назва\n"
            "Приклад: /add_chat -1003725655321 71 Kitty Angel"
        )
        return
    try:
        chat_id = int(parts[1])
        thread_id = int(parts[2])
        chat_name = parts[3].strip()
    except ValueError:
        await message.reply("Помилка: chat_id і thread_id мають бути числами.")
        return
    existing = await find_monitored(chat_id, thread_id)
    if existing:
        await message.reply(f"Ця гілка вже підключена як «{existing['chat_name']}».")
        return
    await add_monitored_chat(chat_id, thread_id, chat_name)
    await message.reply(f"✅ Додано «{chat_name}» (chat_id: {chat_id}, тред {thread_id})")


@dp.message(Command("list_story_chats"))
async def cmd_list_story_chats(message: Message):
    if not is_admin(message):
        return

    chats = await get_active_chats()
    if not chats:
        await message.reply("Немає підключених гілок.")
        return

    lines = ["<b>Підключені гілки:</b>"]
    for c in chats:
        t = f", тред {c['thread_id']}" if c["thread_id"] else ""
        lines.append(
            f"<b>#{c['id']}</b> {c['chat_name']} "
            f"(chat_id: <code>{c['chat_id']}</code>{t})"
        )
    await message.reply("\n".join(lines), parse_mode="HTML")


@dp.message(Command("remove_all_chats"))
async def cmd_remove_all_chats(message: Message):
    if not is_admin(message):
        return
    chats = await get_active_chats()
    if not chats:
        await message.reply("Немає активних гілок.")
        return
    for c in chats:
        await deactivate_chat(c["id"])
    await message.reply(f"✅ Видалено всі гілки ({len(chats)} шт.)")


@dp.message(Command("remove_story_chat"))
async def cmd_remove_story_chat(message: Message):
    if not is_admin(message):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().lstrip("-").isdigit():
        await message.reply("Використання: /remove_story_chat <ID>")
        return

    record_id = int(parts[1].strip())
    chats = await get_active_chats()
    target = next((c for c in chats if c["id"] == record_id), None)
    if not target:
        await message.reply(f"ID {record_id} не знайдено серед активних гілок.")
        return

    await deactivate_chat(record_id)
    await message.reply(f"✅ Гілку «{target['chat_name']}» відключено (ID {record_id}).")


@dp.message(Command("story_status"))
async def cmd_story_status(message: Message):
    if not is_admin(message):
        return

    today = kyiv_now().date()
    yesterday = today - timedelta(days=1)
    chats = await get_active_chats()
    today_subs = await get_today_status(today)
    yesterday_subs = await get_today_status(yesterday)

    if not chats:
        await message.reply("Немає підключених гілок.")
        return

    now_hour = kyiv_now().hour
    # Shift 22 belongs to the date when 22:00 happened.
    # Before 06:00 Kyiv we still want to see last night's shift 22 (yesterday's date).
    shift22_date = yesterday if now_hour < 6 else today
    shift22_subs = yesterday_subs if now_hour < 6 else today_subs

    lines = [f"<b>Статус сторіс за {today} (Київ)</b>"]
    for shift_hour in (6, 14, 22):
        if shift_hour == 22:
            subs_pool = shift22_subs
            shift_date = shift22_date
        else:
            subs_pool = today_subs
            shift_date = today

        lines.append(f"\n<b>Зміна {shift_hour}:00</b> ({shift_date})")
        for c in chats:
            subs = [
                s for s in subs_pool
                if s["chat_id"] == c["chat_id"]
                and s["thread_id"] == c["thread_id"]
                and s["shift_hour"] == shift_hour
                and s.get("category", CAT_STORY) == CAT_STORY
            ]
            if subs:
                names = ", ".join(fmt_user(s) for s in subs)
                lines.append(f"  ✅ {c['chat_name']}: {names}")
            else:
                lines.append(f"  ❌ {c['chat_name']}: нікого")

    await message.reply("\n".join(lines), parse_mode="HTML")


# ── Daily rating ─────────────────────────────────────────────────────────────

async def _post_rating(target_chat: int, thread_id: int | None,
                       entries: list, for_date: date):
    """Render image + send. Falls back to text on render failure."""
    caption = f"🏆 Рейтинг чатерів за {for_date.strftime('%d.%m.%Y')}"
    kwargs = {"message_thread_id": thread_id} if thread_id else {}

    png = render_rating_image(entries, for_date)
    if png:
        photo = BufferedInputFile(png, filename=f"rating_{for_date}.png")
        try:
            await bot.send_photo(target_chat, photo, caption=caption, **kwargs)
            return
        except Exception as exc:
            logger.error("Rating photo failed for %s: %s — fallback to text", target_chat, exc)

    # Fallback: текст
    try:
        await bot.send_message(target_chat, format_rating(entries, for_date),
                               parse_mode="HTML", **kwargs)
    except Exception as exc:
        logger.error("Rating text send failed for %s: %s", target_chat, exc)


async def send_daily_rating():
    """Pulls yesterday's balances from the Google Sheet and posts a rating
    to every configured target chat (RATING_TARGETS)."""
    yesterday = (kyiv_now() - timedelta(days=1)).date()
    entries = await get_balances_for_day(yesterday.day)
    if entries is None:
        logger.error("Daily rating: sheet fetch failed")
        return

    if not RATING_TARGETS:
        logger.warning("No RATING targets configured, falling back to admins DM")
        for admin_id in ADMIN_TELEGRAM_IDS:
            await _post_rating(admin_id, None, entries, yesterday)
        return

    logger.info("Daily rating: posting to %s chats", len(RATING_TARGETS))
    for chat_id, thread_id in RATING_TARGETS:
        try:
            await _post_rating(chat_id, thread_id, entries, yesterday)
        except Exception as exc:
            logger.error("Failed to post rating to %s: %s", chat_id, exc)


@dp.message(Command("rating"))
async def cmd_rating(message: Message):
    if not is_admin(message):
        return
    # Опціонально приймаємо номер дня: /rating 18  (інакше — вчора).
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) >= 2 and parts[1].strip().isdigit():
        day = int(parts[1].strip())
        target_date = kyiv_now().date().replace(day=day) if 1 <= day <= 31 else (kyiv_now() - timedelta(days=1)).date()
    else:
        target_date = (kyiv_now() - timedelta(days=1)).date()

    entries = await get_balances_for_day(target_date.day)
    if entries is None:
        await message.reply("Не вдалося прочитати таблицю. Перевір SALARY_SHEET_ID і доступ.")
        return
    await _post_rating(message.chat.id, message.message_thread_id, entries, target_date)


@dp.message(Command("broadcast_rating"))
async def cmd_broadcast_rating(message: Message):
    """Admin: manually trigger the daily broadcast right now (for testing)."""
    if not is_admin(message):
        return
    if not RATING_TARGETS:
        await message.reply("RATING_CHAT_IDS не налаштовано — нікуди розсилати.")
        return
    await message.reply(f"Запускаю розсилку в {len(RATING_TARGETS)} чатів…")
    await send_daily_rating()
    await message.reply("Готово ✅")


# ── Scheduler jobs ───────────────────────────────────────────────────────────

async def send_reminder(shift_hour: int):
    shift_date = kyiv_now().date()
    for chat in await get_active_chats():
        cid, tid = chat["chat_id"], chat["thread_id"]
        if await get_submissions(cid, tid, shift_hour, shift_date):
            continue
        try:
            kwargs = {"message_thread_id": tid} if tid else {}
            await bot.send_message(
                cid,
                f"⏰ Нагадування! Зміна {shift_hour}:00 — ще ніхто не скинув сторіс.",
                **kwargs,
            )
        except Exception as exc:
            logger.error("Reminder chat=%s thread=%s: %s", cid, tid, exc)


async def send_report(shift_hour: int):
    sd = shift_date_for_report(shift_hour)
    for chat in await get_active_chats():
        cid, tid = chat["chat_id"], chat["thread_id"]
        subs = await get_submissions(cid, tid, shift_hour, sd)

        if subs:
            continue

        text = f"❌ Зміна {shift_hour}:00 ({sd}) — ніхто не скинув сторіс!"
        try:
            kwargs = {"message_thread_id": tid} if tid else {}
            await bot.send_message(cid, text, **kwargs)
        except Exception as exc:
            logger.error("Report chat=%s thread=%s: %s", cid, tid, exc)

        for admin_id in ADMIN_TELEGRAM_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    f"🚨 [{chat['chat_name']}] Зміна {shift_hour}:00 ({sd}) — ніхто не скинув сторіс!",
                )
            except Exception as exc:
                logger.error("Admin DM to %s: %s", admin_id, exc)


# ── DM-ескалація власнику ────────────────────────────────────────────────────

async def notify_owner(text: str):
    """Personal DM to the owner (OWNER_TELEGRAM_ID). Silent if not configured."""
    target = OWNER_TELEGRAM_ID or (ADMIN_TELEGRAM_IDS[0] if ADMIN_TELEGRAM_IDS else 0)
    if not target:
        logger.warning("notify_owner skipped — OWNER_TELEGRAM_ID not set")
        return
    try:
        await bot.send_message(target, text, parse_mode="HTML")
    except Exception as exc:
        logger.error("notify_owner failed: %s", exc)


_CAT_LABEL_UI = {
    CAT_LOGIN: "Log in",
    CAT_STORY: "Story",
    CAT_POST: "Post",
    CAT_LOGOUT: "Log out",
}


async def remind_login_logout_in_group(shift_hour: int):
    """T+10 min: nudge groups in which login/logout for this shift is missing."""
    now = kyiv_now()
    shift_date = _shift_date_for(now, shift_hour)
    for chat in await get_active_chats():
        cid, tid = chat["chat_id"], chat["thread_id"]
        missing = []
        for cat in (CAT_LOGIN, CAT_LOGOUT):
            subs = await get_submissions_by_category(cid, tid, cat, shift_hour, shift_date)
            if not subs:
                missing.append(_CAT_LABEL_UI[cat])
        if not missing:
            continue
        text = (
            f"⏰ Зміна {shift_hour:02d}:00 — не отримано: "
            + ", ".join(missing)
            + ". Виставте скрін якнайшвидше."
        )
        try:
            kwargs = {"message_thread_id": tid} if tid else {}
            await bot.send_message(cid, text, **kwargs)
        except Exception as exc:
            logger.error("Login/logout reminder chat=%s: %s", cid, exc)


async def escalate_login_logout(shift_hour: int):
    """T+20 min: DM the owner about chats with still-missing login/logout."""
    now = kyiv_now()
    shift_date = _shift_date_for(now, shift_hour)
    for chat in await get_active_chats():
        cid, tid = chat["chat_id"], chat["thread_id"]
        missing = []
        for cat in (CAT_LOGIN, CAT_LOGOUT):
            subs = await get_submissions_by_category(cid, tid, cat, shift_hour, shift_date)
            if not subs:
                missing.append(_CAT_LABEL_UI[cat])
        if missing:
            await notify_owner(
                f"🚨 <b>[{chat['chat_name']}]</b> зміна {shift_hour:02d}:00 — "
                f"пропущено: {', '.join(missing)}"
            )


async def escalate_story(shift_hour: int):
    """After end-of-shift report: DM owner about chats with no story."""
    sd = shift_date_for_report(shift_hour)
    for chat in await get_active_chats():
        cid, tid = chat["chat_id"], chat["thread_id"]
        subs = await get_submissions_by_category(cid, tid, CAT_STORY, shift_hour, sd)
        if not subs:
            await notify_owner(
                f"🚨 <b>[{chat['chat_name']}]</b> зміна {shift_hour:02d}:00 ({sd}) — "
                f"немає Story"
            )


async def check_post_deadline():
    """Mon/Wed/Fri 23:50 Kyiv: DM owner about chats with no post today."""
    today = kyiv_now().date()
    for chat in await get_active_chats():
        cid, tid = chat["chat_id"], chat["thread_id"]
        subs = await get_submissions_by_category(cid, tid, CAT_POST, None, today)
        if not subs:
            await notify_owner(
                f"🚨 <b>[{chat['chat_name']}]</b> {today.strftime('%a %d.%m')} — "
                f"немає Post за сьогодні"
            )


# ── Web server (needed for Render Web Service) ───────────────────────────────

async def run_web_server():
    async def health(_request):
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Web server listening on port %s", port)


# ── Entry point ──────────────────────────────────────────────────────────────

async def main():
    await init_db()

    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as exc:
        logger.warning("delete_webhook failed: %s", exc)

    scheduler = AsyncIOScheduler(timezone="UTC")

    # Reminders  Kyiv 06:30 = UTC 03:30 | 14:30 = 11:30 | 22:30 = 19:30
    scheduler.add_job(send_reminder, "cron", hour=3,  minute=30, args=[6])
    scheduler.add_job(send_reminder, "cron", hour=11, minute=30, args=[14])
    scheduler.add_job(send_reminder, "cron", hour=19, minute=30, args=[22])

    # Daily rating Kyiv 18:00 = UTC 15:00
    scheduler.add_job(send_daily_rating, "cron", hour=15, minute=0)

    # Reports    Kyiv 08:00 = UTC 05:00 | 16:00 = 13:00 | 00:00 = 21:00
    scheduler.add_job(send_report, "cron", hour=5,  minute=0, args=[6])
    scheduler.add_job(send_report, "cron", hour=13, minute=0, args=[14])
    scheduler.add_job(send_report, "cron", hour=21, minute=0, args=[22])

    # Login/Logout — нагадування в групі (T+10хв після початку зміни)
    # Kyiv 06:10 = UTC 03:10 | 14:10 = 11:10 | 22:10 = 19:10
    scheduler.add_job(remind_login_logout_in_group, "cron", hour=3,  minute=10, args=[6])
    scheduler.add_job(remind_login_logout_in_group, "cron", hour=11, minute=10, args=[14])
    scheduler.add_job(remind_login_logout_in_group, "cron", hour=19, minute=10, args=[22])

    # Login/Logout — ескалація в DM власнику (T+20хв)
    # Kyiv 06:20 = UTC 03:20 | 14:20 = 11:20 | 22:20 = 19:20
    scheduler.add_job(escalate_login_logout, "cron", hour=3,  minute=20, args=[6])
    scheduler.add_job(escalate_login_logout, "cron", hour=11, minute=20, args=[14])
    scheduler.add_job(escalate_login_logout, "cron", hour=19, minute=20, args=[22])

    # Story — ескалація власнику (одразу після звіту в групі)
    # Kyiv 08:05 = UTC 05:05 | 16:05 = 13:05 | 00:05 = 21:05
    scheduler.add_job(escalate_story, "cron", hour=5,  minute=5, args=[6])
    scheduler.add_job(escalate_story, "cron", hour=13, minute=5, args=[14])
    scheduler.add_job(escalate_story, "cron", hour=21, minute=5, args=[22])

    # Post — перевірка щопонеділка/середи/пʼятниці о 23:50 Київ (20:50 UTC)
    scheduler.add_job(check_post_deadline, "cron",
                      day_of_week="mon,wed,fri", hour=20, minute=50)

    scheduler.start()

    await run_web_server()

    logger.info("Bot started, polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
