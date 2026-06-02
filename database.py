"""PostgreSQL database layer (asyncpg).

Required env var:
  DATABASE_URL — postgres://user:pass@host/db?sslmode=require

Neon / Supabase connection strings зазвичай вже містять sslmode=require.
Якщо у твоєму DSN немає — додай вручну.
"""

import logging
import os
from datetime import date as date_type
from typing import Optional

import asyncpg

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "")

# Категорії скріншотів
CAT_LOGIN = "login"
CAT_STORY = "story"
CAT_POST = "post"
CAT_LOGOUT = "logout"
ALL_CATEGORIES = (CAT_LOGIN, CAT_STORY, CAT_POST, CAT_LOGOUT)

# Global connection pool (створюється у init_db)
_pool: Optional[asyncpg.Pool] = None


async def _get_pool() -> asyncpg.Pool:
    """Lazy-init the pool. asyncpg.create_pool is the official way."""
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is not set")
        _pool = await asyncpg.create_pool(
            dsn=DATABASE_URL,
            min_size=1,
            max_size=5,
            command_timeout=15,
        )
        logger.info("Postgres pool created")
    return _pool


async def init_db():
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS monitored_chats (
                id          BIGSERIAL PRIMARY KEY,
                chat_id     BIGINT      NOT NULL,
                thread_id   BIGINT,
                chat_name   TEXT        NOT NULL,
                active      BOOLEAN     NOT NULL DEFAULT TRUE,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS story_submissions (
                id           BIGSERIAL PRIMARY KEY,
                chat_id      BIGINT      NOT NULL,
                thread_id    BIGINT,
                telegram_id  BIGINT      NOT NULL,
                username     TEXT,
                shift_hour   INTEGER,
                shift_date   DATE        NOT NULL,
                category     TEXT        NOT NULL DEFAULT 'story',
                submitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_submissions_lookup
            ON story_submissions(chat_id, category, shift_date, shift_hour)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_chats_active
            ON monitored_chats(active)
        """)
        logger.info("Postgres schema ready")


async def add_monitored_chat(chat_id: int, thread_id: int | None, chat_name: str):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO monitored_chats (chat_id, thread_id, chat_name) VALUES ($1, $2, $3)",
            chat_id, thread_id, chat_name,
        )


async def get_active_chats() -> list:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM monitored_chats WHERE active = TRUE ORDER BY id"
        )
        return [dict(r) for r in rows]


async def deactivate_chat(record_id: int):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE monitored_chats SET active = FALSE WHERE id = $1",
            record_id,
        )


async def find_monitored(chat_id: int, thread_id: int | None) -> dict | None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if thread_id is None:
            row = await conn.fetchrow(
                """SELECT * FROM monitored_chats
                   WHERE chat_id = $1 AND active = TRUE AND thread_id IS NULL""",
                chat_id,
            )
        else:
            row = await conn.fetchrow(
                """SELECT * FROM monitored_chats
                   WHERE chat_id = $1 AND active = TRUE AND thread_id = $2""",
                chat_id, thread_id,
            )
        return dict(row) if row else None


async def add_submission(
    chat_id: int,
    thread_id: int | None,
    telegram_id: int,
    username: str | None,
    shift_hour: int | None,
    shift_date: date_type,
    category: str = CAT_STORY,
):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO story_submissions
               (chat_id, thread_id, telegram_id, username, shift_hour, shift_date, category)
               VALUES ($1, $2, $3, $4, $5, $6, $7)""",
            chat_id, thread_id, telegram_id, username, shift_hour, shift_date, category,
        )


async def get_submissions(
    chat_id: int, thread_id: int | None, shift_hour: int, shift_date: date_type
) -> list:
    """Backward-compat: return story submissions for a shift."""
    return await get_submissions_by_category(
        chat_id, thread_id, CAT_STORY, shift_hour, shift_date
    )


async def get_submissions_by_category(
    chat_id: int,
    thread_id: int | None,
    category: str,
    shift_hour: int | None,
    shift_date: date_type,
) -> list:
    pool = await _get_pool()
    parts = [
        "SELECT * FROM story_submissions",
        "WHERE chat_id = $1 AND category = $2 AND shift_date = $3",
    ]
    params: list = [chat_id, category, shift_date]
    if thread_id is None:
        parts.append("AND thread_id IS NULL")
    else:
        params.append(thread_id)
        parts.append(f"AND thread_id = ${len(params)}")
    if shift_hour is not None:
        params.append(shift_hour)
        parts.append(f"AND shift_hour = ${len(params)}")
    sql = " ".join(parts)
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]


async def get_today_status(shift_date: date_type) -> list:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT * FROM story_submissions
               WHERE shift_date = $1
               ORDER BY shift_hour, submitted_at""",
            shift_date,
        )
        return [dict(r) for r in rows]
