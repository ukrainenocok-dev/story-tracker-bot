import aiosqlite
from datetime import date as date_type

DB_PATH = "story_tracker.db"

# Категорії скріншотів
CAT_LOGIN = "login"
CAT_STORY = "story"
CAT_POST = "post"
CAT_LOGOUT = "logout"
ALL_CATEGORIES = (CAT_LOGIN, CAT_STORY, CAT_POST, CAT_LOGOUT)


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
        # Колонка shift_hour тепер nullable (для категорії 'post')
        await db.execute("""
            CREATE TABLE IF NOT EXISTS story_submissions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id      INTEGER NOT NULL,
                thread_id    INTEGER,
                telegram_id  INTEGER NOT NULL,
                username     TEXT,
                shift_hour   INTEGER,
                shift_date   TEXT    NOT NULL,
                category     TEXT    NOT NULL DEFAULT 'story',
                submitted_at TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)
        # Міграція для існуючих БД — додати колонку category якщо відсутня
        async with db.execute("PRAGMA table_info(story_submissions)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
        if "category" not in cols:
            await db.execute(
                "ALTER TABLE story_submissions ADD COLUMN category TEXT NOT NULL DEFAULT 'story'"
            )
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
    shift_hour: int | None,
    shift_date: date_type,
    category: str = CAT_STORY,
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO story_submissions
               (chat_id, thread_id, telegram_id, username, shift_hour, shift_date, category)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (chat_id, thread_id, telegram_id, username, shift_hour, str(shift_date), category),
        )
        await db.commit()


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
    """Return submissions of a specific category for a chat/shift/date.

    For category='post' pass shift_hour=None — it ignores shift_hour and only
    filters by date.
    """
    sql = """SELECT * FROM story_submissions
             WHERE chat_id = ? AND category = ? AND shift_date = ?
               AND (thread_id = ? OR (thread_id IS NULL AND ? IS NULL))"""
    params: tuple = (chat_id, category, str(shift_date), thread_id, thread_id)
    if shift_hour is not None:
        sql += " AND shift_hour = ?"
        params = (*params, shift_hour)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cur:
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
