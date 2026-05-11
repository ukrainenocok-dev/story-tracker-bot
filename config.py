import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")


def _parse_admin_ids() -> list[int]:
    """Parse admin IDs from env vars.

    Supports two env variables:
      ADMIN_TELEGRAM_IDS = "123,456"  (new, multiple admins)
      ADMIN_TELEGRAM_ID  = "123"      (legacy, single admin)
    Both can be set together — merged, deduplicated.
    """
    raw_list = []

    multi = os.getenv("ADMIN_TELEGRAM_IDS", "")
    if multi:
        raw_list.extend(multi.split(","))

    single = os.getenv("ADMIN_TELEGRAM_ID", "")
    if single:
        raw_list.append(single)

    ids: list[int] = []
    for part in raw_list:
        part = part.strip()
        if not part:
            continue
        try:
            uid = int(part)
            if uid and uid not in ids:
                ids.append(uid)
        except ValueError:
            pass
    return ids


ADMIN_TELEGRAM_IDS: list[int] = _parse_admin_ids()

# Backward compat: legacy single-ID consumers use the first admin from the list.
ADMIN_TELEGRAM_ID: int = ADMIN_TELEGRAM_IDS[0] if ADMIN_TELEGRAM_IDS else 0
