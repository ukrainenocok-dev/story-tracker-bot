import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone, date

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import BOT_TOKEN, ADMIN_TELEGRAM_ID
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
        return

    shift_hour, shift_date = get_current_shift()
    if shift_hour is None:
        return

    await add_submission(
        chat_id, thread_id,
        message.from_user.id,
        message.from_user.username,
        shift_hour, shift_date,
    )
    await message.reply("+")


# ── Admin commands ───────────────────────────────────────────────────────────

@dp.message(Command("add_story_chat"))
async def cmd_add_story_chat(message: Message):
    if message.from_user.id != ADMIN_TELEGRAM_ID:
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


@dp.message(Command("list_story_chats"))
async def cmd_list_story_chats(message: Message):
    if message.from_user.id != ADMIN_TELEGRAM_ID:
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


@dp.message(Command("remove_story_chat"))
async def cmd_remove_story_chat(message: Message):
    if message.from_user.id != ADMIN_TELEGRAM_ID:
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
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
    if message.from_user.id != ADMIN_TELEGRAM_ID:
        return

    today = kyiv_now().date()
    chats = await get_active_chats()
    all_subs = await get_today_status(today)

    if not chats:
        await message.reply("Немає підключених гілок.")
        return

    lines = [f"<b>Статус сторіс за {today} (Київ)</b>"]
    for shift_hour in (6, 14, 22):
        lines.append(f"\n<b>Зміна {shift_hour}:00</b>")
        for c in chats:
            subs = [
                s for s in all_subs
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
            names = ", ".join(fmt_user(s) for s in subs)
            text = f"📊 Звіт зміни {shift_hour}:00 ({sd})\n✅ Скинули: {names}"
        else:
            text = f"📊 Звіт зміни {shift_hour}:00 ({sd})\n❌ Ніхто не скинув сторіс!"

        try:
            kwargs = {"message_thread_id": tid} if tid else {}
            await bot.send_message(cid, text, parse_mode="HTML", **kwargs)
        except Exception as exc:
            logger.error("Report chat=%s thread=%s: %s", cid, tid, exc)

        if not subs:
            try:
                await bot.send_message(
                    ADMIN_TELEGRAM_ID,
                    f"🚨 [{chat['chat_name']}] Зміна {shift_hour}:00 ({sd}) — ніхто не скинув сторіс!",
                )
            except Exception as exc:
                logger.error("Admin DM: %s", exc)


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

    scheduler = AsyncIOScheduler(timezone="UTC")

    # Reminders  Kyiv 06:30 = UTC 03:30 | 14:30 = 11:30 | 22:30 = 19:30
    scheduler.add_job(send_reminder, "cron", hour=3,  minute=30, args=[6])
    scheduler.add_job(send_reminder, "cron", hour=11, minute=30, args=[14])
    scheduler.add_job(send_reminder, "cron", hour=19, minute=30, args=[22])

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
