"""Окремий бот для щоденного рейтингу чатерів.

Робить тільки:
  - /rating         — згенерувати картинку рейтингу (опц. за певний день місяця)
  - /broadcast_rating — вручну розіслати рейтинг у всі RATING_CHAT_IDS
  - Авто-розсилка щодня о 18:00 Київ (UTC 15:00) у всі RATING_CHAT_IDS

Env vars:
  TELEGRAM_BOT_TOKEN       — токен ОКРЕМОГО рейтинг-бота (не story-bot!)
  ADMIN_TELEGRAM_IDS       — список ID адмінів через кому
  RATING_CHAT_IDS          — список '<chat_id>[:thread_id]' через кому
  SALARY_SHEET_ID/GID      — Google Sheet Deal Makers
  CASH_KINGS_SHEET_ID/GID  — Google Sheet Cash Kings
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone, date

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.types import Message, BufferedInputFile
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import BOT_TOKEN, ADMIN_TELEGRAM_IDS
from sheets import get_balances_for_day, format_rating
from image_renderer import render_rating_image

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


def is_admin(message: Message) -> bool:
    return bool(
        message.from_user
        and message.from_user.id in ADMIN_TELEGRAM_IDS
    )


def _parse_rating_targets() -> list[tuple[int, int | None]]:
    """RATING_CHAT_IDS: '-100123,-100456:42,-100789' → [(-100123, None), ...]."""
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

    # Backward compat: legacy single chat
    legacy = int(os.getenv("RATING_CHAT_ID", "0"))
    if legacy and not any(c == legacy for c, _ in targets):
        legacy_thread_raw = os.getenv("RATING_THREAD_ID", "").strip()
        legacy_thread = (
            int(legacy_thread_raw)
            if legacy_thread_raw.lstrip("-").isdigit()
            else None
        )
        targets.append((legacy, legacy_thread))

    return targets


RATING_TARGETS = _parse_rating_targets()


# ── Rating posting ─────────────────────────────────────────────────────────

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
            logger.error("Rating photo failed for %s: %s — fallback to text",
                         target_chat, exc)

    try:
        await bot.send_message(target_chat, format_rating(entries, for_date),
                               parse_mode="HTML", **kwargs)
    except Exception as exc:
        logger.error("Rating text send failed for %s: %s", target_chat, exc)


async def send_daily_rating():
    """Pulls yesterday's balances from the Google Sheets and posts a rating
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


# ── Commands ────────────────────────────────────────────────────────────────

@dp.message(Command("rating"))
async def cmd_rating(message: Message):
    if not is_admin(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) >= 2 and parts[1].strip().isdigit():
        day = int(parts[1].strip())
        target_date = (
            kyiv_now().date().replace(day=day)
            if 1 <= day <= 31
            else (kyiv_now() - timedelta(days=1)).date()
        )
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


@dp.message(Command("start"))
async def cmd_start(message: Message):
    if is_admin(message):
        await message.reply(
            "👋 Привіт! Я бот рейтингу чатерів Next Models.\n\n"
            "Команди:\n"
            "/rating [день] — згенерувати рейтинг за вчора або за вказаний день\n"
            "/broadcast_rating — вручну розіслати рейтинг у всі групи"
        )


# ── Web server (Render Web Service вимагає відкритий порт) ─────────────────

async def run_web_server():
    async def health(_request):
        return web.Response(text="OK - rating bot")

    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Rating bot web server listening on port %s", port)


# ── Entry point ─────────────────────────────────────────────────────────────

async def main():
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as exc:
        logger.warning("delete_webhook failed: %s", exc)

    scheduler = AsyncIOScheduler(timezone="UTC")
    # Щодня о 18:00 Київ = 15:00 UTC
    scheduler.add_job(send_daily_rating, "cron", hour=15, minute=0)
    scheduler.start()

    await run_web_server()

    logger.info("Rating bot started, polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
