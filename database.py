import aiosqlite
from datetime import date as date_type

DB_PATH = "story_tracker.db"


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS monitored_chats (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     INTEGER NOT NULL,
                thread_id   INTEGER,
                chat_name   TEXT    NOT NULL,
                active      INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS story_submissions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id      INTEGER NOT NULL,
                thread_id    INTEGER,
                telegram_id  INTEGER NOT NULL,
                username     TEXT,
                shift_hour   INTEGER NOT NULL,
                shift_date   TEXT    NOT NULL,
                submitted_at TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)
        await db.commit()


async def add_monitored_chat(chat_id: int, thread_id: int | None, chat_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO monitored_chats (chat_id, thread_id, chat_name) VALUES (?, ?, ?)",
            (chat_id, thread_id, chat_name),
        )
        await db.commit()


async def get_active_chats() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM monitored_chats WHERE active = 1 ORDER BY id"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def deactivate_chat(record_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE monitored_chats SET active = 0 WHERE id = ?", (record_id,)
        )
        await db.commit()


async def find_monitored(chat_id: int, thread_id: int | None) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM monitored_chats
               WHERE chat_id = ? AND active = 1
                 AND (thread_id = ? OR (thread_id IS NULL AND ? IS NULL))""",
            (chat_id, thread_id, thread_id),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def add_submission(
    chat_id: int,
    thread_id: int | None,
    telegram_id: int,
    username: str | None,
    shift_hour: int,
    shift_date: date_type,
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO story_submissions
               (chat_id, thread_id, telegram_id, username, shift_hour, shift_date)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (chat_id, thread_id, telegram_id, username, shift_hour, str(shift_date)),
        )
        await db.commit()


async def get_submissions(
    chat_id: int, thread_id: int | None, shift_hour: int, shift_date: date_type
) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM story_submissions
               WHERE chat_id = ? AND shift_hour = ? AND shift_date = ?
                 AND (thread_id = ? OR (thread_id IS NULL AND ? IS NULL))""",
            (chat_id, shift_hour, str(shift_date), thread_id, thread_id),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_today_status(shift_date: date_type) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM story_submissions
               WHERE shift_date = ?
               ORDER BY shift_hour, submitted_at""",
            (str(shift_date),),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]
