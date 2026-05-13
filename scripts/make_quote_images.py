#!/usr/bin/env python3
"""
Render each quote in viral_clips/quotes.json as a styled 1080x1350 quote-card
image suitable for Instagram feed (4:5).

Each quote may be:
  - a bare string (legacy schema)
  - a dict {"text": str, "style": str, "punch": str?}

Styles:
  grunge_accent  — dark grainy bg, condensed bold caps + brush-script accent
                   (urgent / declarative / call-to-action quotes)
  vintage_press  — cream bg with stamp-watermark, slab serif + bold script accent
                   (reframes, paradoxes, "X is God's Y" wisdom)
  editorial_wide — charcoal bg, wide-tracked caps, red setup → white payoff
                   (sermonic build-to-payoff quotes)
  brand_block    — flat brand navy, heavy uppercase sans, single color
                   (identity statements, exclamatory bangers)
  scripture_card — warm vertical gradient, italic serif
                   (scripture, prayer-language, contemplative quotes)
  minimal_serif  — black bg, large serif (legacy/baseline; fallback)

Usage:
    python3 make_quote_images.py [work_dir] [--attribution "Pastor Name"]
                                            [--style <name>]
                                            [--all-styles]
    work_dir defaults to CWD

    --style <name>    Force every quote into this style (overrides per-quote tag)
    --all-styles      Render each quote in every style; output filenames are
                      quote_NN_<style>.png
    --attribution     Optional speaker name; rendered as "— Name" under the quote

Output:
    <work_dir>/quote_images/quote_NN.png
    <work_dir>/quote_images/quote_NN.txt   (the source text — for caption copy)
"""

import json
import os
import random
import re
import sys

from PIL import Image, ImageDraw, ImageFont

# ─── Canvas ──────────────────────────────────────────────────────────────────
WIDTH = 1080
HEIGHT = 1350

# ─── Bundled fonts ──────────────────────────────────────────────────────────
SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT_DIR = os.path.join(SKILL_ROOT, "fonts")

ANTON = "Anton-Regular.ttf"
BEBAS = "BebasNeue-Regular.ttf"
MARKER = "PermanentMarker-Regular.ttf"
SLAB = "AlfaSlabOne-Regular.ttf"
SCRIPT = "Yellowtail-Regular.ttf"
SANS = "Inter-Regular.ttf"
ITALIC = "Lora-Italic.ttf"

ALL_STYLES = [
    "grunge_accent",
    "vintage_press",
    "editorial_wide",
    "brand_block",
    "scripture_card",
    "minimal_serif",
]


def font(name, size):
    path = os.path.join(FONT_DIR, name)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Font missing: {path}")
    return ImageFont.truetype(path, size)


# ─── Text layout helpers ─────────────────────────────────────────────────────
def wrap_to_fit(draw, text, font_obj, max_width):
    """Greedy word-wrap so each rendered line stays within max_width."""
    words = text.split()
    if not words:
        return []
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        bbox = draw.textbbox((0, 0), trial, font=font_obj)
        if bbox[2] - bbox[0] <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def fit_text(draw, text, font_name, max_w, max_h,
             max_size=120, min_size=28, line_spacing=1.12,
             tracking=0):
    """Binary-search the largest size that fits. Returns (font_obj, lines, line_h, size)."""
    lo, hi = min_size, max_size
    best = None

    def lay(size):
        f = font(font_name, size)
        if tracking == 0:
            lines = wrap_to_fit(draw, text, f, max_w)
            widest = max((draw.textbbox((0, 0), ln, font=f)[2] for ln in lines), default=0)
        else:
            lines = wrap_to_fit_tracked(draw, text, f, max_w, tracking)
            widest = max((tracked_width(draw, ln, f, tracking) for ln in lines), default=0)
        ascent, descent = f.getmetrics()
        line_h = int((ascent + descent) * line_spacing)
        total_h = line_h * len(lines)
        return f, lines, line_h, widest, total_h

    while lo <= hi:
        mid = (lo + hi) // 2
        f, lines, line_h, widest, total_h = lay(mid)
        if total_h <= max_h and widest <= max_w:
            best = (f, lines, line_h, mid)
            lo = mid + 1
        else:
            hi = mid - 1

    if best is None:
        f, lines, line_h, _, _ = lay(min_size)
        best = (f, lines, line_h, min_size)
    return best


def tracked_width(draw, text, font_obj, tracking):
    """Width if each character is followed by `tracking` extra px."""
    if not text:
        return 0
    w = draw.textbbox((0, 0), text, font=font_obj)[2]
    return w + tracking * max(0, len(text) - 1)


def wrap_to_fit_tracked(draw, text, font_obj, max_width, tracking):
    """Word-wrap accounting for letter-spacing."""
    words = text.split()
    if not words:
        return []
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if tracked_width(draw, trial, font_obj, tracking) <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def draw_tracked_text(draw, xy, text, font_obj, fill, tracking):
    """Draw text with extra horizontal spacing between characters."""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font_obj, fill=fill)
        w = draw.textbbox((0, 0), ch, font=font_obj)[2]
        x += w + tracking


# ─── Quote splitting heuristics ──────────────────────────────────────────────
SPLITTERS = [
    ", but ", ", and ", ", because ",
    " — ", " – ", " - ",
    " if ", " when ", " until ", " unless ", " because ",
]


def split_body_payoff(text):
    """Split a quote into (setup, payoff) where payoff is the climactic clause.

    Tries conjunctions/connectors first, then last sentence boundary, then
    last word. Returns (setup, payoff). If no good split found, setup is "".
    """
    lower = text.lower()
    best_idx, best_len = -1, 0
    for sp in SPLITTERS:
        idx = lower.rfind(sp)
        if idx > best_idx and idx > len(text) * 0.25:
            best_idx, best_len = idx, len(sp)
    if best_idx > 0:
        return text[:best_idx + best_len].rstrip(), text[best_idx + best_len:].strip()

    # Sentence boundary fallback
    parts = re.split(r'(?<=[.!?])\s+', text.strip())
    if len(parts) >= 2:
        return " ".join(parts[:-1]), parts[-1]

    # Last word
    pieces = text.rsplit(" ", 1)
    if len(pieces) == 2:
        return pieces[0], pieces[1]
    return "", text


def pick_punch_word(text):
    """Pick a single punchy word for vintage_press script accent.

    Heuristic: last word stripped of trailing punctuation, or longest noun-y
    word in the last quarter of the quote.
    """
    cleaned = text.rstrip(".!?’'\"")
    words = cleaned.split()
    if not words:
        return text
    return words[-1].strip(".!?,;:\"'")


# ─── Backgrounds ─────────────────────────────────────────────────────────────
def bg_grunge(base=(11, 11, 11), sigma=14, vignette=True):
    """Dark base + film grain + soft vignette + faint scratchy splotches."""
    base_img = Image.new("RGB", (WIDTH, HEIGHT), base)
    noise = Image.effect_noise((WIDTH, HEIGHT), sigma).convert("RGB")
    img = Image.blend(base_img, noise, 0.18)

    # Faint dark scratchy splotches — a couple of low-contrast irregular blobs
    rng = random.Random(7)
    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    for _ in range(3):
        cx = rng.randint(WIDTH // 4, WIDTH * 3 // 4)
        cy = rng.randint(HEIGHT // 4, HEIGHT * 3 // 4)
        r = rng.randint(200, 380)
        alpha = rng.randint(14, 28)
        od.ellipse((cx - r, cy - r * 0.7, cx + r, cy + r * 0.7),
                   fill=(20, 20, 22, alpha))
    img = Image.alpha_composite(img.convert("RGBA"), overlay)

    if vignette:
        vg = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        vd = ImageDraw.Draw(vg)
        # Subtle corner vignette via four large dark ellipses
        for cx, cy in [(0, 0), (WIDTH, 0), (0, HEIGHT), (WIDTH, HEIGHT)]:
            vd.ellipse(
                (cx - 800, cy - 800, cx + 800, cy + 800),
                fill=(0, 0, 0, 60),
            )
        img = Image.alpha_composite(img, vg)

    return img.convert("RGB")


def bg_vintage_paper(base=(240, 235, 226)):
    """Warm paper-cream with very subtle grain + stamp-collage watermark."""
    base_img = Image.new("RGB", (WIDTH, HEIGHT), base)
    grain = Image.effect_noise((WIDTH, HEIGHT), 7).convert("RGB")
    img = Image.blend(base_img, grain, 0.06)

    # Very faint stamp-collage watermark — must read as background texture, not type
    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    words = ["FAITH", "HOPE", "GRACE", "ONE", "LOVE", "JOY", "PEACE", "MERCY",
             "TRUTH", "SAVED", "REACH", "12", "AMEN", "JESUS", "RISE", "CROSS",
             "★", "EST. 2009", "BAPTISM", "GROUPS", "SUNDAY"]
    rng = random.Random(42)
    for _ in range(28):
        text = rng.choice(words)
        size = rng.randint(24, 62)
        try:
            f = font(SLAB, size)
        except FileNotFoundError:
            f = font(SANS, size)
        x = rng.randint(-40, WIDTH - 40)
        y = rng.randint(-40, HEIGHT - 40)
        alpha = rng.randint(8, 16)
        od.text((x, y), text, font=f, fill=(120, 115, 105, alpha))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    return img


def bg_editorial_charcoal(base=(43, 45, 49)):
    """Dark muted slate with very subtle stamp-watermark in lower portion only."""
    base_img = Image.new("RGB", (WIDTH, HEIGHT), base)
    grain = Image.effect_noise((WIDTH, HEIGHT), 8).convert("RGB")
    img = Image.blend(base_img, grain, 0.08)

    # Watermark in lower 35% only — very subtle
    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    words = ["FAITH", "HOPE", "GRACE", "ONE", "LOVE", "JOY", "PEACE",
             "TRUTH", "SAVED", "REACH", "12", "AMEN", "RISE", "EST. 2009"]
    rng = random.Random(11)
    band_top = int(HEIGHT * 0.65)
    for _ in range(28):
        text = rng.choice(words)
        size = rng.randint(28, 76)
        try:
            f = font(SLAB, size)
        except FileNotFoundError:
            f = font(SANS, size)
        x = rng.randint(-40, WIDTH - 40)
        y = rng.randint(band_top, HEIGHT - 40)
        alpha = rng.randint(10, 22)
        od.text((x, y), text, font=f, fill=(90, 92, 96, alpha))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    return img


def bg_brand_solid(color=(26, 42, 63)):
    """Solid deep navy (Cross Church-ish) with imperceptible grain."""
    base_img = Image.new("RGB", (WIDTH, HEIGHT), color)
    grain = Image.effect_noise((WIDTH, HEIGHT), 5).convert("RGB")
    return Image.blend(base_img, grain, 0.05)


def bg_scripture_gradient(top=(42, 53, 72), bot=(8, 12, 22)):
    """Warm vertical gradient from muted navy to near-black."""
    img = Image.new("RGB", (WIDTH, HEIGHT), top)
    px = img.load()
    for y in range(HEIGHT):
        t = y / max(1, HEIGHT - 1)
        r = int(top[0] * (1 - t) + bot[0] * t)
        g = int(top[1] * (1 - t) + bot[1] * t)
        b = int(top[2] * (1 - t) + bot[2] * t)
        for x in range(WIDTH):
            px[x, y] = (r, g, b)
    # Mild grain on top
    grain = Image.effect_noise((WIDTH, HEIGHT), 6).convert("RGB")
    return Image.blend(img, grain, 0.05)


# ─── Style: grunge_accent ────────────────────────────────────────────────────
GRUNGE_ACCENT_BODY = (245, 243, 235)
GRUNGE_ACCENT_HIGHLIGHT = (199, 216, 40)  # lime yellow


def render_grunge_accent(text, out_path, punch=None, attribution=None):
    img = bg_grunge()
    draw = ImageDraw.Draw(img)

    setup, payoff = (None, text)
    if punch and punch.strip():
        # Use the explicit punch the prompt provided
        if punch in text:
            i = text.rfind(punch)
            setup = text[:i].rstrip(" ,—–-")
            payoff = punch
        else:
            payoff = punch
            setup = text
    else:
        s, p = split_body_payoff(text)
        if s:
            setup, payoff = s, p

    setup_text = (setup or "").upper().strip()
    payoff_text = (payoff or "").strip()  # script reads better mixed-case

    pad_x = 80
    pad_y = 130
    box_w = WIDTH - 2 * pad_x
    setup_h_budget = int((HEIGHT - 2 * pad_y) * (0.55 if setup_text else 0))
    payoff_h_budget = (HEIGHT - 2 * pad_y) - setup_h_budget - (40 if setup_text else 0)

    if setup_text:
        s_font, s_lines, s_line_h, _ = fit_text(
            draw, setup_text, ANTON,
            max_w=box_w, max_h=setup_h_budget,
            max_size=110, min_size=44, line_spacing=1.04,
        )
    else:
        s_font, s_lines, s_line_h = None, [], 0

    p_font, p_lines, p_line_h, _ = fit_text(
        draw, payoff_text, MARKER,
        max_w=box_w, max_h=payoff_h_budget,
        max_size=130, min_size=48, line_spacing=1.0,
    )

    total_setup_h = s_line_h * len(s_lines)
    total_payoff_h = p_line_h * len(p_lines)
    gap = 30 if setup_text else 0
    total_h = total_setup_h + gap + total_payoff_h
    y = (HEIGHT - total_h) // 2

    if setup_text:
        for ln in s_lines:
            bbox = draw.textbbox((0, 0), ln, font=s_font)
            w = bbox[2] - bbox[0]
            x = (WIDTH - w) // 2
            draw.text((x, y), ln, font=s_font, fill=GRUNGE_ACCENT_BODY)
            y += s_line_h
        y += gap

    for ln in p_lines:
        bbox = draw.textbbox((0, 0), ln, font=p_font)
        w = bbox[2] - bbox[0]
        x = (WIDTH - w) // 2
        # Slight shadow for readability
        draw.text((x + 3, y + 3), ln, font=p_font, fill=(0, 0, 0))
        draw.text((x, y), ln, font=p_font, fill=GRUNGE_ACCENT_HIGHLIGHT)
        y += p_line_h

    if attribution:
        af = font(SANS, 24)
        atext = f"— {attribution.upper()}"
        bbox = draw.textbbox((0, 0), atext, font=af)
        x = (WIDTH - (bbox[2] - bbox[0])) // 2
        draw.text((x, HEIGHT - 80), atext, font=af, fill=(170, 170, 168))

    img.save(out_path, "PNG", optimize=True)


# ─── Style: vintage_press ────────────────────────────────────────────────────
VINTAGE_BODY = (45, 52, 60)
VINTAGE_ACCENT = (208, 70, 56)
VINTAGE_DIM = (110, 105, 96)


def render_vintage_press(text, out_path, punch=None, attribution=None):
    img = bg_vintage_paper()
    draw = ImageDraw.Draw(img)

    accent_word = (punch or "").strip()
    if not accent_word or accent_word not in text:
        accent_word = pick_punch_word(text)

    # Split text into "body without accent_word" + accent_word
    body_text = text
    if accent_word and accent_word in text:
        i = text.rfind(accent_word)
        body_text = (text[:i] + text[i + len(accent_word):]).strip(" .,!?")
        body_text = re.sub(r"\s+", " ", body_text)
    body_text = body_text.upper()

    pad_x = 110
    pad_y = 180
    box_w = WIDTH - 2 * pad_x
    box_h_total = HEIGHT - 2 * pad_y
    body_h_budget = int(box_h_total * 0.55)
    accent_h_budget = box_h_total - body_h_budget - 50

    # Three diamond ornaments at the top
    ornament_y = pad_y - 60
    diamond_count = 3
    diamond_size = 10
    diamond_gap = 36
    total_orn_w = diamond_count * diamond_size + (diamond_count - 1) * diamond_gap
    ox = (WIDTH - total_orn_w) // 2
    for i in range(diamond_count):
        cx = ox + i * (diamond_size + diamond_gap)
        cy = ornament_y
        draw.polygon([
            (cx, cy - diamond_size),
            (cx + diamond_size, cy),
            (cx, cy + diamond_size),
            (cx - diamond_size, cy),
        ], fill=VINTAGE_ACCENT)

    b_font, b_lines, b_line_h, _ = fit_text(
        draw, body_text, SLAB,
        max_w=box_w, max_h=body_h_budget,
        max_size=78, min_size=30, line_spacing=1.18, tracking=2,
    )

    a_font, a_lines, a_line_h, _ = fit_text(
        draw, accent_word, SCRIPT,
        max_w=box_w, max_h=accent_h_budget,
        max_size=180, min_size=70, line_spacing=1.0,
    )

    total_body_h = b_line_h * len(b_lines)
    total_accent_h = a_line_h * len(a_lines)
    gap = 28
    total_h = total_body_h + gap + total_accent_h
    y = (HEIGHT - total_h) // 2 + 30

    for ln in b_lines:
        w = tracked_width(draw, ln, b_font, 2)
        x = (WIDTH - w) // 2
        draw_tracked_text(draw, (x, y), ln, b_font, VINTAGE_BODY, tracking=2)
        y += b_line_h
    y += gap

    accent_x_last = 0
    accent_y_last = 0
    accent_w_last = 0
    for ln in a_lines:
        bbox = draw.textbbox((0, 0), ln, font=a_font)
        w = bbox[2] - bbox[0]
        x = (WIDTH - w) // 2
        draw.text((x, y), ln, font=a_font, fill=VINTAGE_ACCENT)
        accent_x_last = x
        accent_y_last = y
        accent_w_last = w
        y += a_line_h

    # Underline under accent word (slightly angled feel via 2px line)
    if accent_w_last > 0:
        ul_y = accent_y_last + int(a_line_h * 0.82)
        draw.line(
            [(accent_x_last + 10, ul_y), (accent_x_last + accent_w_last - 10, ul_y + 6)],
            fill=VINTAGE_ACCENT, width=4,
        )

    if attribution:
        af = font(SANS, 22)
        atext = f"— {attribution.upper()}"
        bbox = draw.textbbox((0, 0), atext, font=af)
        x = (WIDTH - (bbox[2] - bbox[0])) // 2
        draw.text((x, HEIGHT - 90), atext, font=af, fill=VINTAGE_DIM)

    img.save(out_path, "PNG", optimize=True)


# ─── Style: editorial_wide ───────────────────────────────────────────────────
EDITORIAL_SETUP = (220, 74, 61)
EDITORIAL_PAYOFF = (245, 243, 238)
EDITORIAL_DIM = (140, 142, 145)


def render_editorial_wide(text, out_path, punch=None, attribution=None):
    img = bg_editorial_charcoal()
    draw = ImageDraw.Draw(img)

    setup, payoff = None, None
    if punch and punch.strip() and punch in text:
        i = text.rfind(punch)
        setup = text[:i].rstrip(" ,—–-")
        payoff = punch
    else:
        s, p = split_body_payoff(text)
        if s:
            setup, payoff = s, p
        else:
            payoff = text

    setup_text = (setup or "").upper().strip().rstrip(".,;:")
    payoff_text = (payoff or "").upper().strip().rstrip(".")

    pad_x = 130
    pad_y = 200
    box_w = WIDTH - 2 * pad_x
    box_h = HEIGHT - 2 * pad_y

    if setup_text:
        setup_budget = int(box_h * 0.55)
        payoff_budget = box_h - setup_budget - 40
    else:
        setup_budget = 0
        payoff_budget = box_h

    s_font = None
    s_lines, s_line_h = [], 0
    if setup_text:
        s_font, s_lines, s_line_h, _ = fit_text(
            draw, setup_text, SANS,
            max_w=box_w, max_h=setup_budget,
            max_size=58, min_size=22, line_spacing=1.55, tracking=6,
        )

    p_font, p_lines, p_line_h, _ = fit_text(
        draw, payoff_text, SANS,
        max_w=box_w, max_h=payoff_budget,
        max_size=78, min_size=30, line_spacing=1.45, tracking=6,
    )

    total_h = s_line_h * len(s_lines) + (40 if setup_text else 0) + p_line_h * len(p_lines)
    y = (HEIGHT - total_h) // 2

    for ln in s_lines:
        w = tracked_width(draw, ln, s_font, 6)
        x = (WIDTH - w) // 2
        draw_tracked_text(draw, (x, y), ln, s_font, EDITORIAL_SETUP, tracking=6)
        y += s_line_h
    if setup_text:
        y += 40

    last_line_x = 0
    last_line_w = 0
    last_line_y = 0
    for ln in p_lines:
        w = tracked_width(draw, ln, p_font, 6)
        x = (WIDTH - w) // 2
        draw_tracked_text(draw, (x, y), ln, p_font, EDITORIAL_PAYOFF, tracking=6)
        last_line_x = x
        last_line_w = w
        last_line_y = y
        y += p_line_h

    # Red squiggle/dashed underline beneath the last payoff line
    if p_lines and last_line_w > 0:
        ul_y = last_line_y + int(p_line_h * 0.78)
        ul_x1 = last_line_x + last_line_w // 5
        ul_x2 = last_line_x + last_line_w - last_line_w // 5
        # Three short overlapping dashes for a hand-drawn feel
        seg_w = (ul_x2 - ul_x1) // 3
        for i in range(3):
            sx = ul_x1 + i * seg_w + (i * 4)
            sy = ul_y + ((-1) ** i) * 3
            draw.line([(sx, sy), (sx + seg_w + 6, sy + 4)],
                      fill=EDITORIAL_SETUP, width=3)

    if attribution:
        af = font(SANS, 22)
        atext = f"— {attribution.upper()}"
        bbox = draw.textbbox((0, 0), atext, font=af)
        x = (WIDTH - (bbox[2] - bbox[0])) // 2
        draw.text((x, HEIGHT - 80), atext, font=af, fill=EDITORIAL_DIM)

    img.save(out_path, "PNG", optimize=True)


# ─── Style: brand_block ──────────────────────────────────────────────────────
BRAND_NAVY = (26, 42, 63)
BRAND_TEXT = (242, 240, 232)
BRAND_DIM = (170, 178, 192)


def render_brand_block(text, out_path, punch=None, attribution=None):
    img = bg_brand_solid(BRAND_NAVY)
    draw = ImageDraw.Draw(img)

    body_text = text.upper().strip()

    pad_x = 110
    pad_y = 180
    box_w = WIDTH - 2 * pad_x
    box_h = HEIGHT - 2 * pad_y

    b_font, b_lines, b_line_h, _ = fit_text(
        draw, body_text, BEBAS,
        max_w=box_w, max_h=box_h,
        max_size=132, min_size=48, line_spacing=1.04, tracking=2,
    )

    total_h = b_line_h * len(b_lines)
    y = (HEIGHT - total_h) // 2 - 20

    bottom_y = y
    for ln in b_lines:
        w = tracked_width(draw, ln, b_font, 2)
        x = (WIDTH - w) // 2
        draw_tracked_text(draw, (x, y), ln, b_font, BRAND_TEXT, tracking=2)
        y += b_line_h
        bottom_y = y

    # Floating bar accent — sits well below the type, never touching descenders
    bar_w = 60
    bar_x = (WIDTH - bar_w) // 2
    bar_y = bottom_y + 50
    if bar_y + 6 < HEIGHT - pad_y // 2:
        draw.rectangle((bar_x, bar_y, bar_x + bar_w, bar_y + 4), fill=BRAND_TEXT)

    if attribution:
        af = font(SANS, 22)
        atext = f"— {attribution.upper()}"
        bbox = draw.textbbox((0, 0), atext, font=af)
        x = (WIDTH - (bbox[2] - bbox[0])) // 2
        draw.text((x, HEIGHT - 80), atext, font=af, fill=BRAND_DIM)

    img.save(out_path, "PNG", optimize=True)


# ─── Style: scripture_card ───────────────────────────────────────────────────
SCRIPTURE_TEXT = (240, 235, 226)
SCRIPTURE_DIM = (170, 168, 162)


def render_scripture_card(text, out_path, punch=None, attribution=None):
    img = bg_scripture_gradient()
    draw = ImageDraw.Draw(img)

    pad_x = 100
    pad_y = 220
    box_w = WIDTH - 2 * pad_x
    box_h = HEIGHT - 2 * pad_y

    b_font, b_lines, b_line_h, _ = fit_text(
        draw, text, ITALIC,
        max_w=box_w, max_h=box_h,
        max_size=82, min_size=32, line_spacing=1.30,
    )

    total_h = b_line_h * len(b_lines)
    y = (HEIGHT - total_h) // 2

    for ln in b_lines:
        bbox = draw.textbbox((0, 0), ln, font=b_font)
        w = bbox[2] - bbox[0]
        x = (WIDTH - w) // 2
        draw.text((x, y), ln, font=b_font, fill=SCRIPTURE_TEXT)
        y += b_line_h

    # Hairline divider above the text
    div_y = (HEIGHT - total_h) // 2 - 60
    draw.line([(WIDTH // 2 - 40, div_y), (WIDTH // 2 + 40, div_y)],
              fill=SCRIPTURE_TEXT, width=3)

    if attribution:
        af = font(SANS, 24)
        atext = f"— {attribution}"
        bbox = draw.textbbox((0, 0), atext, font=af)
        x = (WIDTH - (bbox[2] - bbox[0])) // 2
        draw.text((x, HEIGHT - 100), atext, font=af, fill=SCRIPTURE_DIM)

    img.save(out_path, "PNG", optimize=True)


# ─── Style: minimal_serif (legacy/baseline) ──────────────────────────────────
MINIMAL_BG = (10, 10, 10)
MINIMAL_FG = (240, 240, 235)
MINIMAL_DIM = (140, 140, 138)
MINIMAL_MARK = (60, 60, 58)


def render_minimal_serif(text, out_path, punch=None, attribution=None):
    img = Image.new("RGB", (WIDTH, HEIGHT), MINIMAL_BG)
    draw = ImageDraw.Draw(img)

    pad_x = 110
    pad_y = 200
    box_w = WIDTH - 2 * pad_x
    box_h = HEIGHT - 2 * pad_y - (60 if attribution else 0)

    # Decorative opening quotation mark
    mark_font = font(ITALIC, 240)
    draw.text((pad_x - 24, pad_y - 160), "“", font=mark_font, fill=MINIMAL_MARK)

    b_font, b_lines, b_line_h, _ = fit_text(
        draw, text, ITALIC,
        max_w=box_w, max_h=box_h,
        max_size=92, min_size=34, line_spacing=1.22,
    )

    total_h = b_line_h * len(b_lines)
    y = (HEIGHT - total_h) // 2

    for ln in b_lines:
        bbox = draw.textbbox((0, 0), ln, font=b_font)
        w = bbox[2] - bbox[0]
        x = (WIDTH - w) // 2
        draw.text((x, y), ln, font=b_font, fill=MINIMAL_FG)
        y += b_line_h

    if attribution:
        af = font(SANS, 26)
        atext = f"— {attribution}"
        bbox = draw.textbbox((0, 0), atext, font=af)
        x = (WIDTH - (bbox[2] - bbox[0])) // 2
        draw.text((x, HEIGHT - 110), atext, font=af, fill=MINIMAL_DIM)

    img.save(out_path, "PNG", optimize=True)


# ─── Style dispatch ──────────────────────────────────────────────────────────
RENDERERS = {
    "grunge_accent":  render_grunge_accent,
    "vintage_press":  render_vintage_press,
    "editorial_wide": render_editorial_wide,
    "brand_block":    render_brand_block,
    "scripture_card": render_scripture_card,
    "minimal_serif":  render_minimal_serif,
}


def pick_default_style(text):
    """Heuristic style picker for quotes lacking an explicit style tag."""
    t = text.strip()
    lower = t.lower()
    if any(kw in lower for kw in [" lord", " god ", "scripture", "psalm", "verse",
                                  "prayer", "amen", "savior", "holy"]):
        # contemplative / sacred → scripture
        if any(w in lower for w in ["father", "lord", "holy", "spirit"]):
            return "scripture_card"
    if lower.startswith(("stop ", "start ", "don't ", "you can't", "you have to",
                         "we can't", "you need", "we need")):
        return "grunge_accent"
    if " but " in lower or " is god's " in lower or "your greatest" in lower:
        return "vintage_press"
    if t.count(".") >= 2 or " if " in lower or " when " in lower:
        return "editorial_wide"
    if len(t) <= 70 and t.endswith("."):
        return "brand_block"
    return "minimal_serif"


def normalize_quote(q):
    """Accept str or dict. Return (text, style, punch)."""
    if isinstance(q, str):
        return q.strip(), None, None
    if isinstance(q, dict):
        return (
            (q.get("text") or q.get("quote") or "").strip(),
            q.get("style"),
            q.get("punch"),
        )
    return "", None, None


# ─── Main ────────────────────────────────────────────────────────────────────
def parse_args():
    args = {"work_dir": None, "attribution": None, "style": None, "all_styles": False}
    pos = []
    i = 1
    while i < len(sys.argv):
        a = sys.argv[i]
        if a == "--attribution" and i + 1 < len(sys.argv):
            args["attribution"] = sys.argv[i + 1]
            i += 2
        elif a == "--style" and i + 1 < len(sys.argv):
            args["style"] = sys.argv[i + 1]
            i += 2
        elif a == "--all-styles":
            args["all_styles"] = True
            i += 1
        elif a.startswith("--"):
            i += 1
        else:
            pos.append(a)
            i += 1
    if pos:
        args["work_dir"] = pos[0]
    return args


def main():
    args = parse_args()
    work_dir = args["work_dir"] or os.getcwd()
    attribution = args["attribution"]
    force_style = args["style"]
    all_styles = args["all_styles"]

    if force_style and force_style not in RENDERERS:
        print(f"✗ Unknown style: {force_style}")
        print(f"  Available: {', '.join(RENDERERS.keys())}")
        sys.exit(1)

    quotes_path = os.path.join(work_dir, "viral_clips", "quotes.json")
    if not os.path.exists(quotes_path):
        print(f"✗ quotes.json not found: {quotes_path}")
        print("  Run find_moments.py first.")
        sys.exit(1)

    with open(quotes_path) as f:
        quotes_raw = json.load(f)
    if not quotes_raw:
        print("⚠ quotes.json is empty — nothing to render")
        sys.exit(0)

    out_dir = os.path.join(work_dir, "quote_images")
    os.makedirs(out_dir, exist_ok=True)

    print(f"Quotes : {len(quotes_raw)}")
    print(f"Output : {out_dir}")
    print(f"Fonts  : {FONT_DIR}")
    if attribution:
        print(f"Attrib : {attribution}")
    if force_style:
        print(f"Style  : forced → {force_style}")
    elif all_styles:
        print("Style  : --all-styles (one render per style per quote)")
    else:
        print("Style  : per-quote tag (heuristic fallback if missing)")

    style_counts = {s: 0 for s in RENDERERS}
    for i, q in enumerate(quotes_raw, start=1):
        text, tag, punch = normalize_quote(q)
        if not text:
            continue

        slug = f"quote_{i:02d}"
        txt_path = os.path.join(out_dir, slug + ".txt")
        with open(txt_path, "w") as f:
            f.write(text + "\n")

        preview = text if len(text) <= 80 else text[:77] + "..."

        if all_styles:
            print(f"  [{i:02d}] {preview}")
            for s in RENDERERS:
                png = os.path.join(out_dir, f"{slug}_{s}.png")
                RENDERERS[s](text, png, punch=punch, attribution=attribution)
                style_counts[s] += 1
                print(f"        ✓ {s}")
        else:
            style = force_style or tag or pick_default_style(text)
            if style not in RENDERERS:
                style = "minimal_serif"
            png = os.path.join(out_dir, slug + ".png")
            RENDERERS[style](text, png, punch=punch, attribution=attribution)
            style_counts[style] += 1
            print(f"  [{i:02d}] ({style:<14}) {preview}")

    print(f"\n✓ {sum(style_counts.values())} renders → {out_dir}")
    print("  by style:")
    for s in ALL_STYLES:
        if style_counts.get(s, 0):
            print(f"    {s:<14} {style_counts[s]}")


if __name__ == "__main__":
    main()
