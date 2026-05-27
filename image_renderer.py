"""Neo-noir styled rating image renderer for 'Deal Makers'.

Style: near-black background, teal neon accents with glow, gold/silver/bronze
podium, magenta accent line, cinematic vignette.
"""

import io
import logging
import os
from datetime import date
from typing import Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont

logger = logging.getLogger(__name__)

WIDTH, HEIGHT = 1280, 720

# Палітра — нео-нуар
BG_DARK = (8, 10, 18)
BG_MID = (14, 22, 38)
NEON_TEAL = (93, 255, 206)        # головний неон (Deal Makers)
NEON_MAGENTA = (255, 42, 109)     # акцент (вікна, лінії)
NEON_BLUE = (0, 170, 255)
GOLD = (255, 215, 60)
SILVER = (210, 220, 230)
BRONZE = (210, 130, 70)
TEXT_PRIMARY = (235, 240, 250)
TEXT_SECONDARY = (140, 155, 180)
TEXT_MUTED = (95, 105, 130)
DARK_TEXT = (10, 10, 20)

FONT_DIR = "/usr/share/fonts/truetype/dejavu"
FONT_BOLD = os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")
FONT_REGULAR = os.path.join(FONT_DIR, "DejaVuSans.ttf")


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_BOLD if bold else FONT_REGULAR, size)


def _bg() -> Image.Image:
    """Темне тло з ледь помітним радіальним градієнтом."""
    img = Image.new("RGB", (WIDTH, HEIGHT), BG_DARK)
    px = img.load()
    cx, cy = WIDTH // 2, HEIGHT // 2
    max_d = (cx ** 2 + cy ** 2) ** 0.5
    for y in range(HEIGHT):
        for x in range(0, WIDTH, 2):
            d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5 / max_d
            t = min(1.0, d * 1.15)
            r = int(BG_MID[0] * (1 - t) + BG_DARK[0] * t)
            g = int(BG_MID[1] * (1 - t) + BG_DARK[1] * t)
            b = int(BG_MID[2] * (1 - t) + BG_DARK[2] * t)
            px[x, y] = (r, g, b)
            if x + 1 < WIDTH:
                px[x + 1, y] = (r, g, b)
    return img


def _draw_scanlines(img: Image.Image):
    """Тонкі горизонтальні лінії — кінематографічний шум."""
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    for y in range(0, HEIGHT, 3):
        d.line([(0, y), (WIDTH, y)], fill=(255, 255, 255, 6))
    img.paste(overlay, (0, 0), overlay)


def _neon_text(base: Image.Image, xy, text: str, font, color, glow_radius=10, glow_alpha=180):
    """Текст із неоновим світінням через GaussianBlur."""
    glow = Image.new("RGBA", base.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.text(xy, text, font=font, fill=color + (glow_alpha,))
    glow = glow.filter(ImageFilter.GaussianBlur(radius=glow_radius))
    base.paste(glow, (0, 0), glow)
    # дублюємо чіткий текст поверх
    d = ImageDraw.Draw(base)
    d.text(xy, text, font=font, fill=color)


def _neon_rect(base: Image.Image, box, color, radius=14, glow_radius=14, glow_alpha=140, fill=None):
    """Прямокутник з неоновим контуром."""
    glow = Image.new("RGBA", base.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.rounded_rectangle(box, radius=radius, outline=color + (glow_alpha,), width=6)
    glow = glow.filter(ImageFilter.GaussianBlur(radius=glow_radius))
    base.paste(glow, (0, 0), glow)
    d = ImageDraw.Draw(base)
    if fill is not None:
        d.rounded_rectangle(box, radius=radius, fill=fill)
    d.rounded_rectangle(box, radius=radius, outline=color, width=2)


def _truncate(text: str, font, max_width: int) -> str:
    if font.getlength(text) <= max_width:
        return text
    while text and font.getlength(text + "…") > max_width:
        text = text[:-1]
    return text + "…"


def _draw_header(img: Image.Image, for_date: date):
    # верхній лейбл "DEAL MAKERS"
    brand_font = _font(20, bold=True)
    brand = "DEAL  MAKERS"
    bw = ImageDraw.Draw(img).textlength(brand, font=brand_font)
    _neon_text(
        img, ((WIDTH - bw) / 2, 20), brand, brand_font, NEON_TEAL,
        glow_radius=8, glow_alpha=200,
    )

    # тонка магента-лінія під брендом
    line_y = 56
    ImageDraw.Draw(img).line(
        [(WIDTH / 2 - 80, line_y), (WIDTH / 2 + 80, line_y)],
        fill=NEON_MAGENTA, width=2,
    )

    # головний заголовок
    title = "РЕЙТИНГ  ЧАТЕРІВ"
    title_font = _font(50, bold=True)
    tw = ImageDraw.Draw(img).textlength(title, font=title_font)
    _neon_text(
        img, ((WIDTH - tw) / 2, 75), title, title_font, TEXT_PRIMARY,
        glow_radius=12, glow_alpha=120,
    )

    # дата
    date_str = for_date.strftime("%d.%m.%Y").upper()
    df = _font(20)
    dw = ImageDraw.Draw(img).textlength(date_str, font=df)
    ImageDraw.Draw(img).text(((WIDTH - dw) / 2, 145), date_str, font=df, fill=TEXT_SECONDARY)


def _draw_podium(img: Image.Image, top3: list[tuple[str, float]]):
    if not top3:
        return

    # порядок: 2-й | 1-й | 3-й
    layout = []
    layout.append((2, top3[1], SILVER, 160) if len(top3) >= 2 else None)
    layout.append((1, top3[0], GOLD, 210))
    layout.append((3, top3[2], BRONZE, 130) if len(top3) >= 3 else None)

    box_w = 280
    gap = 28
    total_w = box_w * 3 + gap * 2
    start_x = (WIDTH - total_w) // 2
    base_y = 430

    for i, item in enumerate(layout):
        if item is None:
            continue
        rank, (name, amount), color, h = item
        x = start_x + i * (box_w + gap)
        y_top = base_y - h

        # неоновий блок
        _neon_rect(
            img, [x, y_top, x + box_w, base_y], color,
            radius=18, glow_radius=18, glow_alpha=180,
            fill=(20, 18, 35),
        )

        # цифра ранку — велика, з неоновим світінням
        rank_font = _font(72, bold=True)
        rank_str = str(rank)
        rw = ImageDraw.Draw(img).textlength(rank_str, font=rank_font)
        _neon_text(
            img, (x + (box_w - rw) / 2, y_top + 12), rank_str, rank_font, color,
            glow_radius=14, glow_alpha=200,
        )

        # ім'я
        name_font = _font(20, bold=True)
        name_short = _truncate(name, name_font, box_w - 30)
        nw = ImageDraw.Draw(img).textlength(name_short, font=name_font)
        ImageDraw.Draw(img).text(
            (x + (box_w - nw) / 2, y_top + 100),
            name_short, font=name_font, fill=TEXT_PRIMARY,
        )

        # сума з неоновим teal-світінням
        amount_str = f"{amount:,.2f}".replace(",", " ")
        amt_font = _font(30, bold=True)
        aw = ImageDraw.Draw(img).textlength(amount_str, font=amt_font)
        _neon_text(
            img, (x + (box_w - aw) / 2, y_top + 130),
            amount_str, amt_font, NEON_TEAL,
            glow_radius=8, glow_alpha=160,
        )


def _draw_rest_list(img: Image.Image, rest: list[tuple[str, float]]):
    if not rest:
        return

    list_top = 470
    col_w = 540
    gap = 50
    start_x = (WIDTH - (col_w * 2 + gap)) // 2

    item_font = _font(17)
    amt_font = _font(18, bold=True)
    line_h = 26

    avail_h = HEIGHT - list_top - 25
    max_per_col = max(1, avail_h // line_h)
    visible = rest[: max_per_col * 2]

    draw = ImageDraw.Draw(img)
    for i, (name, amount) in enumerate(visible):
        rank = i + 4
        col = i // max_per_col
        row = i % max_per_col
        x = start_x + col * (col_w + gap)
        y = list_top + row * line_h

        rank_str = f"{rank:02d}"
        rank_w = draw.textlength(rank_str, font=item_font)
        draw.text((x, y), rank_str, font=item_font, fill=NEON_MAGENTA)

        name_short = _truncate(name, item_font, col_w - rank_w - 110)
        draw.text((x + 40, y), name_short, font=item_font, fill=TEXT_PRIMARY)

        amount_str = f"{amount:,.2f}".replace(",", " ")
        aw = draw.textlength(amount_str, font=amt_font)
        draw.text((x + col_w - aw, y - 1), amount_str, font=amt_font, fill=NEON_TEAL)

    remaining = len(rest) - len(visible)
    if remaining > 0:
        mf = _font(15)
        more = f"…ще {remaining} позицій"
        mw = draw.textlength(more, font=mf)
        draw.text(((WIDTH - mw) / 2, HEIGHT - 22), more, font=mf, fill=TEXT_MUTED)


def _vignette(img: Image.Image):
    """Затемнення країв — кіношний ефект."""
    overlay = Image.new("L", img.size, 0)
    d = ImageDraw.Draw(overlay)
    cx, cy = WIDTH // 2, HEIGHT // 2
    d.ellipse([cx - WIDTH * 0.7, cy - HEIGHT * 0.85,
               cx + WIDTH * 0.7, cy + HEIGHT * 0.85], fill=255)
    overlay = overlay.filter(ImageFilter.GaussianBlur(radius=120))
    black = Image.new("RGB", img.size, (0, 0, 0))
    img.paste(black, (0, 0), Image.eval(overlay, lambda v: 255 - v))


def render_rating_image(entries: list[tuple[str, float]], for_date: date) -> Optional[bytes]:
    try:
        img = _bg().convert("RGBA")
        _draw_scanlines(img)
        _draw_header(img, for_date)

        if not entries:
            empty_font = _font(30)
            text = "За цей день даних нема."
            tw = ImageDraw.Draw(img).textlength(text, font=empty_font)
            ImageDraw.Draw(img).text(
                ((WIDTH - tw) / 2, HEIGHT / 2), text, font=empty_font, fill=TEXT_SECONDARY,
            )
        else:
            _draw_podium(img, entries[:3])
            _draw_rest_list(img, entries[3:])

        rgb = img.convert("RGB")
        _vignette(rgb)

        buf = io.BytesIO()
        rgb.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception as exc:
        logger.error("render_rating_image failed: %s", exc)
        return None
