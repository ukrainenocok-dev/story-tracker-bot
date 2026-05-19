import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone, date

RATING_CHAT_ID = int(os.getenv("RATING_CHAT_ID", "0"))
_rating_thread_raw = os.getenv("RATING_THREAD_ID", "").strip()
RATING_THREAD_ID = int(_rating_thread_raw) if _rating_thread_raw.lstrip("-").isdigit() else None

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
    get_today_status,
)
from analyzer import analyze_photo, format_feedback
from sheets import get_balances_for_day, format_rating

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

    shift_hour, shift_date = get_current_shift()
    if shift_hour is not None:
        await add_submission(
            chat_id, thread_id,
            message.from_user.id,
            message.from_user.username,
            shift_hour, shift_date,
        )
        logger.info("MEDIA recorded: chat=%s thread=%s shift=%s", chat_id, thread_id, shift_hour)
    else:
        logger.info("MEDIA outside shift: chat=%s thread=%s", chat_id, thread_id)

    try:
        await message.reply("+")
    except Exception as exc:
        logger.error("Reply '+' failed: %s", exc)

    # Аналіз фото через Gemini (тільки для photo, не video/document)
    if message.photo:
        try:
            photo = message.photo[-1]  # найбільший розмір
            file = await bot.get_file(photo.file_id)
            buf = await bot.download_file(file.file_path)
            image_bytes = buf.read() if hasattr(buf, "read") else bytes(buf)
            analysis = await analyze_photo(image_bytes)
            feedback = format_feedback(analysis)
            if feedback:
                try:
                    await message.reply(feedback)
                except Exception as exc:
                    logger.error("Reply feedback failed: %s", exc)
        except Exception as exc:
            logger.error("Photo analysis pipeline failed: %s", exc)


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
            ]
            if subs:
                names = ", ".join(fmt_user(s) for s in subs)
                lines.append(f"  ✅ {c['chat_name']}: {names}")
            else:
                lines.append(f"  ❌ {c['chat_name']}: нікого")

    await message.reply("\n".join(lines), parse_mode="HTML")


# ── Daily rating ─────────────────────────────────────────────────────────────

async def send_daily_rating():
    """Pulls yesterday's balances from the Google Sheet and posts a rating."""
    yesterday = (kyiv_now() - timedelta(days=1)).date()
    entries = await get_balances_for_day(yesterday.day)
    if entries is None:
        logger.error("Daily rating: sheet fetch failed")
        return
    text = format_rating(entries, yesterday)

    if not RATING_CHAT_ID:
        logger.warning("RATING_CHAT_ID not set, falling back to admins DM")
        for admin_id in ADMIN_TELEGRAM_IDS:
            try:
                await bot.send_message(admin_id, text, parse_mode="HTML")
            except Exception as exc:
                logger.error("Rating DM to %s: %s", admin_id, exc)
        return

    kwargs = {"message_thread_id": RATING_THREAD_ID} if RATING_THREAD_ID else {}
    try:
        await bot.send_message(RATING_CHAT_ID, text, parse_mode="HTML", **kwargs)
    except Exception as exc:
        logger.error("Rating post failed (chat=%s, thread=%s): %s",
                     RATING_CHAT_ID, RATING_THREAD_ID, exc)


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
    await message.reply(format_rating(entries, target_date), parse_mode="HTML")


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

    scheduler.start()

    await run_web_server()

    logger.info("Bot started, polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
