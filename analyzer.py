"""Photo analyzer powered by Google Gemini.

Returns a verdict whether a chatter's submitted photo is suitable for
an OnlyFans story / post preview:
- Model clearly visible?
- Sharp image / acceptable lighting?
- Not nude / explicit?

Falls back gracefully (returns None) on any API error so the main bot
flow is never blocked.
"""

import base64
import json
import logging
import os
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
GEMINI_TIMEOUT = float(os.getenv("GEMINI_TIMEOUT", "20"))

_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key={key}"
)

_PROMPT = (
    "Ти класифікуєш скріншот, який чатер надіслав у робочу Telegram-групу "
    "для трекінгу зміни на OnlyFans.\n\n"
    "ВАЖЛИВО: ти ЗАВЖДИ повертаєш одне з 5 значень у полі screenshot_type — "
    "БЕЗ ВИНЯТКІВ:\n\n"
    "1. \"login\" — після успішного входу в OnlyFans:\n"
    "   - dashboard з лівим меню (Home / Messages / Statements / Profile)\n"
    "   - profile моделі з повним меню адміна\n"
    "   - сторінка з повідомленнями (Inbox/Chats)\n"
    "   - сторінка статистики\n\n"
    "2. \"story\" — щодо сторіс:\n"
    "   - інтерфейс публікації сторіс (\"Add to your story\")\n"
    "   - превʼю активної сторіс (вертикальна, видно як stories у Instagram)\n"
    "   - вкладка Stories у профілі моделі\n\n"
    "3. \"post\" — щодо поста у стрічку:\n"
    "   - feed-пост від акаунту моделі з текстом і фото горизонтальним/"
    "квадратним (видно ім'я @username угорі, лайки/коменти)\n"
    "   - інтерфейс публікації поста (\"New post\")\n"
    "   - історія публікацій / Posts tab\n\n"
    "4. \"logout\" — після виходу:\n"
    "   - сторінка onlyfans.com з кнопкою Sign Up / Log In\n"
    "   - порожня головна без авторизації\n"
    "   - підтвердження \"You have been logged out\"\n\n"
    "5. \"other\" — щось інше (НЕ скріншот OnlyFans, мем, селфі, фото "
    "робочого столу тощо).\n\n"
    "ПРАВИЛА:\n"
    "- Якщо бачиш feed-пост від моделі з підписом і фото моделі — це \"post\"\n"
    "- Якщо бачиш вертикальне вузьке фото з фоном/контентом без ім'я акаунту — це \"story\"\n"
    "- Якщо НЕ впевнений між story / post — дивись на пропорції: вертикальне "
    "9:16 = story, інше + видно ім'я @ = post\n"
    "- Дай оцінку якості (model_visible/good_quality/is_nude_or_explicit) "
    "ТІЛЬКИ для story і post. Для login/logout/other — null.\n\n"
    "Поверни СТРОГО JSON без markdown:\n"
    "{\n"
    '  "screenshot_type": "login" | "story" | "post" | "logout" | "other",\n'
    '  "model_visible": true | false | null,\n'
    '  "good_quality":  true | false | null,\n'
    '  "is_nude_or_explicit": true | false | null,\n'
    '  "reason": "коротко українською, до 80 символів"\n'
    "}"
)

_SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_ONLY_HIGH"},
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_ONLY_HIGH"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_ONLY_HIGH"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_ONLY_HIGH"},
]


async def analyze_photo(
    image_bytes: bytes, mime_type: str = "image/jpeg"
) -> Optional[dict]:
    """Send the image to Gemini, return parsed JSON dict or None on failure."""
    if not GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY is not set, skipping analysis")
        return None

    url = _API_URL.format(model=GEMINI_MODEL, key=GEMINI_API_KEY)
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": _PROMPT},
                    {
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": base64.b64encode(image_bytes).decode("utf-8"),
                        }
                    },
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
        "safetySettings": _SAFETY_SETTINGS,
    }

    try:
        timeout = aiohttp.ClientTimeout(total=GEMINI_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                body = await resp.text()
                if resp.status != 200:
                    logger.error("Gemini HTTP %s: %s", resp.status, body[:300])
                    return None
                data = json.loads(body)
    except Exception as exc:
        logger.error("Gemini request failed: %s", exc)
        return None

    try:
        candidate = data["candidates"][0]
        # Якщо Gemini заблокував відповідь з safety reasons
        if candidate.get("finishReason") in ("SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT"):
            logger.info("Gemini blocked response (finishReason=%s) — treating as explicit",
                        candidate.get("finishReason"))
            return {
                "screenshot_type": "story",
                "model_visible": False,
                "good_quality": False,
                "is_nude_or_explicit": True,
                "reason": "контент заблоковано safety-фільтром",
            }
        text = candidate["content"]["parts"][0]["text"].strip()
        parsed = json.loads(text)
        logger.info("Gemini analysis: %s", parsed)
        return parsed
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        logger.error("Gemini parse failed: %s | payload=%s", exc, str(data)[:500])
        return None


def format_feedback(analysis: Optional[dict]) -> Optional[str]:
    """Convert analysis dict to a chat-friendly message, or None to skip reply."""
    if not analysis:
        return None

    model_visible = bool(analysis.get("model_visible"))
    good_quality = bool(analysis.get("good_quality"))
    is_explicit = bool(analysis.get("is_nude_or_explicit"))
    reason = (analysis.get("reason") or "").strip()

    is_good = model_visible and good_quality and not is_explicit

    if is_good:
        return "✅ Фото підібрано вдало"

    problems = []
    if is_explicit:
        problems.append("оголений / відвертий контент")
    if not good_quality:
        problems.append("нечітка якість")
    if not model_visible:
        problems.append("модель погано видно")

    suffix = ", ".join(problems) if problems else reason
    if not suffix:
        suffix = "не підходить"
    return f"⚠️ Фото варто замінити: {suffix}"
