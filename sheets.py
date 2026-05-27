"""Google Sheets reader for the daily chatter rating.

Supports multiple sheets (Deal Makers + Cash Kings).
Fetches each public sheet (CSV export) and merges balances by chatter name.

Each chatter now has an ID (taken from column B in the sheet) which is used
in the displayed label instead of the surname.

Env vars:
  SALARY_SHEET_ID / SALARY_SHEET_GID         — Deal Makers (legacy names kept)
  CASH_KINGS_SHEET_ID / CASH_KINGS_SHEET_GID — Cash Kings

Layout for each sheet:
  Row 4 has headers: Имя | <ID> | 1 | 2 | ... | 31 | Total ...
  Data rows start at row 5.
  Column A = name, column B = chatter ID, columns C+ = days.

Entry type: tuple[str, str, float] = (full_name, chatter_id, amount).
"""

import asyncio
import csv
import io
import logging
import os
from datetime import date
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

DEAL_MAKERS_SHEET_ID = os.getenv("SALARY_SHEET_ID", "") or os.getenv("DEAL_MAKERS_SHEET_ID", "")
DEAL_MAKERS_SHEET_GID = os.getenv("SALARY_SHEET_GID", "0") or os.getenv("DEAL_MAKERS_SHEET_GID", "0")

CASH_KINGS_SHEET_ID = os.getenv("CASH_KINGS_SHEET_ID", "")
CASH_KINGS_SHEET_GID = os.getenv("CASH_KINGS_SHEET_GID", "0")

SHEET_TIMEOUT = float(os.getenv("SHEETS_TIMEOUT", "15"))

# Service / non-chatter rows that must be excluded from the rating.
_SKIP_NAMES = {
    "рассылки", "розсилки",
    "подписка", "підписка",
    "имя", "імʼя", "ім'я", "имя ",
}

# Column B (zero-based index 1) — chatter identifier.
ID_COL_INDEX = 1


def _csv_url(sheet_id: str, sheet_gid: str) -> Optional[str]:
    if not sheet_id:
        return None
    return (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/export?format=csv&gid={sheet_gid}"
    )


async def _fetch_csv(sheet_id: str, sheet_gid: str, label: str = "") -> Optional[str]:
    url = _csv_url(sheet_id, sheet_gid)
    if not url:
        logger.warning("Sheet ID not configured for %s", label or "sheet")
        return None
    timeout = aiohttp.ClientTimeout(total=SHEET_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status != 200:
                    logger.error("Sheets fetch HTTP %s for %s", resp.status, label)
                    return None
                return await resp.text()
    except Exception as exc:
        logger.error("Sheets fetch failed for %s: %s", label, exc)
        return None


def _normalize_name(s: str) -> str:
    return (s or "").strip().lower()


def _parse_amount(s: str) -> float:
    """Parse '264.26' / '264,26' / '1 200,50' / '' into float."""
    if not s:
        return 0.0
    s = s.replace("\xa0", "").replace(" ", "").replace(",", ".").strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _format_id(raw: str) -> str:
    """Normalize the chatter ID from column B."""
    s = (raw or "").strip()
    if not s:
        return ""
    # '5.0' → '5', '12.00' → '12'
    try:
        f = float(s.replace(",", "."))
        if f == int(f):
            return str(int(f))
        # decimal ID → keep as-is, but normalized
        return f"{f:g}"
    except ValueError:
        return s


def _first_name(full_name: str) -> str:
    parts = (full_name or "").strip().split()
    return parts[0] if parts else (full_name or "")


def format_label(full_name: str, chatter_id: str) -> str:
    """Public helper used by renderer + text formatter."""
    fn = _first_name(full_name)
    return f"{fn} ({chatter_id})" if chatter_id else fn


def parse_balances_for_day(csv_text: str, day: int) -> list[tuple[str, str, float]]:
    """Return list of (full_name, chatter_id, amount) for the requested day.

    Sorted descending by amount. Zero / empty are filtered out.
    """
    reader = csv.reader(io.StringIO(csv_text))
    rows = list(reader)
    if not rows:
        return []

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
    day_col = -1
    target_variants = {str(day), str(day).zfill(2), f"{day}.0"}
    for col_idx, h in enumerate(headers):
        h_clean = (h or "").strip().lstrip("0") or "0"
        if h_clean in target_variants or h_clean == str(day):
            day_col = col_idx
            break
    if day_col < 0:
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

    name_col = -1
    for col_idx, h in enumerate(headers):
        if _normalize_name(h) in {"имя", "ім'я", "імʼя", "ім´я"}:
            name_col = col_idx
            break
    if name_col < 0:
        name_col = 0

    results: list[tuple[str, str, float]] = []
    for row in rows[header_idx + 1:]:
        if len(row) <= max(name_col, day_col):
            continue
        name = (row[name_col] or "").strip()
        if not name:
            continue
        if _normalize_name(name) in _SKIP_NAMES:
            continue
        amount = _parse_amount(row[day_col] if day_col < len(row) else "")
        if amount <= 0:
            continue
        chatter_id = _format_id(row[ID_COL_INDEX] if ID_COL_INDEX < len(row) else "")
        results.append((name, chatter_id, amount))

    results.sort(key=lambda x: x[2], reverse=True)
    return results


async def _get_one_sheet(sheet_id: str, sheet_gid: str, day: int, label: str) -> list[tuple[str, str, float]]:
    """Fetch + parse a single sheet. Returns [] if missing/failed."""
    if not sheet_id:
        return []
    csv_text = await _fetch_csv(sheet_id, sheet_gid, label)
    if csv_text is None:
        return []
    return parse_balances_for_day(csv_text, day)


def _merge_entries(*sources: list[tuple[str, str, float]]) -> list[tuple[str, str, float]]:
    """Merge entries by normalized name, summing amounts.

    Display name = first variant seen. ID = first non-empty seen.
    """
    totals: dict[str, float] = {}
    display: dict[str, str] = {}
    ids: dict[str, str] = {}
    for src in sources:
        for name, chatter_id, amount in src:
            key = _normalize_name(name)
            if key not in display:
                display[key] = name
            if (key not in ids or not ids[key]) and chatter_id:
                ids[key] = chatter_id
            totals[key] = totals.get(key, 0.0) + amount

    merged = [(display[k], ids.get(k, ""), totals[k]) for k in totals if totals[k] > 0]
    merged.sort(key=lambda x: x[2], reverse=True)
    return merged


async def get_balances_for_day(day: int) -> Optional[list[tuple[str, str, float]]]:
    """Return combined rating for the given day from all configured sheets."""
    have_dm = bool(DEAL_MAKERS_SHEET_ID)
    have_ck = bool(CASH_KINGS_SHEET_ID)

    if not have_dm and not have_ck:
        logger.warning("No sheets configured (SALARY_SHEET_ID / CASH_KINGS_SHEET_ID)")
        return None

    tasks = []
    if have_dm:
        tasks.append(_get_one_sheet(DEAL_MAKERS_SHEET_ID, DEAL_MAKERS_SHEET_GID, day, "Deal Makers"))
    if have_ck:
        tasks.append(_get_one_sheet(CASH_KINGS_SHEET_ID, CASH_KINGS_SHEET_GID, day, "Cash Kings"))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    cleaned: list[list[tuple[str, str, float]]] = []
    for r in results:
        if isinstance(r, Exception):
            logger.error("Sheet fetch raised: %s", r)
            continue
        cleaned.append(r)

    return _merge_entries(*cleaned)


def format_rating(entries: list[tuple[str, str, float]], for_date: date) -> str:
    if not entries:
        return f"🏆 Рейтинг чатерів за {for_date.strftime('%d.%m.%Y')}\n\nЗа цей день даних нема."

    lines = [f"🏆 <b>Рейтинг чатерів за {for_date.strftime('%d.%m.%Y')}</b>", ""]
    medals = ["🥇", "🥈", "🥉"]
    for i, entry in enumerate(entries, start=1):
        name, chatter_id, _amount = entry
        prefix = medals[i - 1] if i <= 3 else f"{i}."
        lines.append(f"{prefix} {format_label(name, chatter_id)}")
    return "\n".join(lines)
