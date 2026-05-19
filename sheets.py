"""Google Sheets reader for the daily chatter rating.

Fetches a public sheet (CSV export) and returns chatter balances for a given day.
Expected layout (based on 'Зарплата Deal Makers'):
  Row 4 has headers: Имя | Копейки | 1 | 2 | ... | 31 | Total ...
  Data rows start at row 5.
  Day columns are labelled with the day number ('1', '2', ..., '31').

We skip:
  * Rows with empty name
  * Service rows ('Рассылки', 'Подписка', ...)
"""

import csv
import io
import logging
import os
from datetime import date
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

SHEET_ID = os.getenv("SALARY_SHEET_ID", "")
SHEET_GID = os.getenv("SALARY_SHEET_GID", "0")
SHEET_TIMEOUT = float(os.getenv("SHEETS_TIMEOUT", "15"))

# Service / non-chatter rows that must be excluded from the rating.
_SKIP_NAMES = {
    "рассылки", "розсилки",
    "подписка", "підписка",
    "имя", "імʼя", "ім'я", "имя ",
}


def _csv_url() -> Optional[str]:
    if not SHEET_ID:
        return None
    return (
        f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
        f"/export?format=csv&gid={SHEET_GID}"
    )


async def _fetch_csv() -> Optional[str]:
    url = _csv_url()
    if not url:
        logger.warning("SALARY_SHEET_ID is not configured")
        return None
    timeout = aiohttp.ClientTimeout(total=SHEET_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status != 200:
                    logger.error("Sheets fetch HTTP %s", resp.status)
                    return None
                return await resp.text()
    except Exception as exc:
        logger.error("Sheets fetch failed: %s", exc)
        return None


def _normalize_name(s: str) -> str:
    return (s or "").strip().lower()


def _parse_amount(s: str) -> float:
    """Parse a sheet cell like '264.26' or '264,26' or '1 200,50' into float."""
    if not s:
        return 0.0
    s = s.replace("\xa0", "").replace(" ", "").replace(",", ".").strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_balances_for_day(csv_text: str, day: int) -> list[tuple[str, float]]:
    """Return list of (chatter_name, amount) for the requested day-of-month.

    Sorted descending by amount. Zero / empty are filtered out.
    """
    reader = csv.reader(io.StringIO(csv_text))
    rows = list(reader)
    if not rows:
        return []

    # Знаходимо рядок із заголовками — той, де є колонка "Имя" / "Ім'я".
    header_idx = -1
    for i, row in enumerate(rows[:10]):
        normalized = [_normalize_name(c) for c in row]
        if any(c in {"имя", "ім'я", "імʼя", "ім´я"} for c in normalized):
            header_idx = i
            break
    if header_idx < 0:
        logger.warning("Header row not found in sheet")
        return []

    headers = rows[header_idx]
    # Шукаємо колонку дня — підпис буває '1', '01', ' 1 ', '1.0' тощо.
    day_col = -1
    target_variants = {str(day), str(day).zfill(2), f"{day}.0"}
    for col_idx, h in enumerate(headers):
        h_clean = (h or "").strip().lstrip("0") or "0"
        if h_clean in target_variants or h_clean == str(day):
            day_col = col_idx
            break
    if day_col < 0:
        # запасний пошук — точна збіжність як int
        for col_idx, h in enumerate(headers):
            try:
                if int(float((h or "").strip())) == day:
                    day_col = col_idx
                    break
            except (ValueError, TypeError):
                continue
    if day_col < 0:
        logger.warning("Column for day=%s not found", day)
        return []

    # Колонка з імʼям — зазвичай перша, але знайдемо по заголовку.
    name_col = -1
    for col_idx, h in enumerate(headers):
        if _normalize_name(h) in {"имя", "ім'я", "імʼя", "ім´я"}:
            name_col = col_idx
            break
    if name_col < 0:
        name_col = 0

    results: list[tuple[str, float]] = []
    for row in rows[header_idx + 1:]:
        if len(row) <= max(name_col, day_col):
            continue
        name = (row[name_col] or "").strip()
        if not name:
            continue
        if _normalize_name(name) in _SKIP_NAMES:
            continue
        amount = _parse_amount(row[day_col] if day_col < len(row) else "")
        if amount > 0:
            results.append((name, amount))

    results.sort(key=lambda x: x[1], reverse=True)
    return results


async def get_balances_for_day(day: int) -> Optional[list[tuple[str, float]]]:
    csv_text = await _fetch_csv()
    if csv_text is None:
        return None
    return parse_balances_for_day(csv_text, day)


def format_rating(entries: list[tuple[str, float]], for_date: date) -> str:
    if not entries:
        return f"🏆 Рейтинг чатерів за {for_date.strftime('%d.%m.%Y')}\n\nЗа цей день даних нема."

    lines = [f"🏆 <b>Рейтинг чатерів за {for_date.strftime('%d.%m.%Y')}</b>", ""]
    medals = ["🥇", "🥈", "🥉"]
    for i, (name, amount) in enumerate(entries, start=1):
        prefix = medals[i - 1] if i <= 3 else f"{i}."
        # формат суми: 2645.81 -> "2 645.81"
        amount_str = f"{amount:,.2f}".replace(",", " ")
        lines.append(f"{prefix} {name} — {amount_str}")
    return "\n".join(lines)
