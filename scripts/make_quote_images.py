#!/usr/bin/env python3
"""
Render each quote in viral_clips/quotes.json as a 1080×1350 (Instagram 4:5)
quote-card image.

Design language: minimal, editorial. Sentence case body, modest type sizes,
generous whitespace, no shouting. Inspired by Sunday Social TV's clean
approach to church social typography.

Each quote may be:
  - a bare string (legacy schema)
  - a dict {"text": str, "style": str?, "punch": str?}

Six styles (all clean — they differ in palette and emphasis mechanic, not loudness):
  minimal_serif    — dark bg + Lora regular (default literary)
  soft_paper       — warm cream bg + Lora regular + one italic emphasis word
  editorial_split  — charcoal bg + Inter, dim setup → bright payoff
  accent_payoff    — dark bg + Inter setup + Lora italic payoff in warm gold
  brand_block      — deep navy bg + Inter regular, formal centered layout
  scripture_card   — soft vertical gradient + Lora italic, contemplative

Usage:
    python3 make_quote_images.py [work_dir] [--attribution "Pastor Name"]
                                            [--style <name>]
                                            [--all-styles]
"""

import json
import os
import re
import sys

from PIL import Image, ImageDraw, ImageFont

# ─── Canvas ──────────────────────────────────────────────────────────────────
WIDTH = 1080
HEIGHT = 1350

# ─── Bundled fonts ──────────────────────────────────────────────────────────
SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT_DIR = os.path.join(SKILL_ROOT, "fonts")

SANS = "Inter-Regular.ttf"
SERIF = "Lora-Regular.ttf"
SERIF_ITALIC = "Lora-Italic.ttf"

ALL_STYLES = [
    "minimal_serif",
    "soft_paper",
    "editorial_split",
    "accent_payoff",
    "brand_block",
    "scripture_card",
]


def font(name, size):
    path = os.path.join(FONT_DIR, name)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Font missing: {path}")
    return ImageFont.truetype(path, size)


# ─── Text layout helpers ─────────────────────────────────────────────────────
def wrap_to_fit(draw, text, font_obj, max_width):
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
             max_size=56, min_size=26, line_spacing=1.45):
    """Binary-search the largest size that fits.

    Defaults bias smaller than typical poster-style renderers — clean,
    minimal type that breathes.
    """
    lo, hi = min_size, max_size
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        f = font(font_name, mid)
        lines = wrap_to_fit(draw, text, f, max_w)
        ascent, descent = f.getmetrics()
        line_h = int((ascent + descent) * line_spacing)
        total_h = line_h * len(lines)
        widest = max((draw.textbbox((0, 0), ln, font=f)[2] for ln in lines), default=0)
        if total_h <= max_h and widest <= max_w:
            best = (f, lines, line_h, mid)
            lo = mid + 1
        else:
            hi = mid - 1
    if best is None:
        f = font(font_name, min_size)
        lines = wrap_to_fit(draw, text, f, max_w)
        ascent, descent = f.getmetrics()
        line_h = int((ascent + descent) * line_spacing)
        best = (f, lines, line_h, min_size)
    return best


def draw_centered_block(draw, lines, font_obj, line_h, top_y, fill):
    y = top_y
    for ln in lines:
        bbox = draw.textbbox((0, 0), ln, font=font_obj)
        w = bbox[2] - bbox[0]
        x = (WIDTH - w) // 2
        draw.text((x, y), ln, font=font_obj, fill=fill)
        y += line_h
    return y


# ─── Quote splitting helpers ─────────────────────────────────────────────────
SPLITTERS = [
    ", but ", ", and ", ", because ",
    " — ", " – ",
    " if ", " when ", " until ", " unless ", " because ",
]


def split_body_payoff(text):
    """Return (setup, payoff). If no good split, setup is empty and payoff is text."""
    lower = text.lower()
    best_idx, best_len = -1, 0
    for sp in SPLITTERS:
        idx = lower.rfind(sp)
        if idx > best_idx and idx > len(text) * 0.25:
            best_idx, best_len = idx, len(sp)
    if best_idx > 0:
        return text[:best_idx + best_len].rstrip(), text[best_idx + best_len:].strip()
    parts = re.split(r'(?<=[.!?])\s+', text.strip())
    if len(parts) >= 2:
        return " ".join(parts[:-1]), parts[-1]
    return "", text


def pick_emphasis_word(text):
    """Pick the last meaningful word for soft_paper italic emphasis."""
    cleaned = text.rstrip(".!?’'\"")
    words = cleaned.split()
    if not words:
        return ""
    return words[-1].strip(".!?,;:\"'")


# ─── Backgrounds ─────────────────────────────────────────────────────────────
def bg_solid(color, grain=4):
    """Solid color with imperceptible grain to prevent banding."""
    base = Image.new("RGB", (WIDTH, HEIGHT), color)
    if grain <= 0:
        return base
    noise = Image.effect_noise((WIDTH, HEIGHT), grain).convert("RGB")
    return Image.blend(base, noise, 0.05)


def bg_paper(color=(242, 239, 231)):
    """Cream paper background with a touch of grain — no watermark."""
    base = Image.new("RGB", (WIDTH, HEIGHT), color)
    noise = Image.effect_noise((WIDTH, HEIGHT), 6).convert("RGB")
    return Image.blend(base, noise, 0.06)


def bg_gradient(top, bot):
    """Smooth vertical gradient."""
    img = Image.new("RGB", (WIDTH, HEIGHT), top)
    px = img.load()
    for y in range(HEIGHT):
        t = y / max(1, HEIGHT - 1)
        r = int(top[0] * (1 - t) + bot[0] * t)
        g = int(top[1] * (1 - t) + bot[1] * t)
        b = int(top[2] * (1 - t) + bot[2] * t)
        for x in range(WIDTH):
            px[x, y] = (r, g, b)
    noise = Image.effect_noise((WIDTH, HEIGHT), 5).convert("RGB")
    return Image.blend(img, noise, 0.04)


# ─── Color palette ───────────────────────────────────────────────────────────
DARK_BG = (14, 14, 16)
CHARCOAL_BG = (27, 28, 32)
NAVY_BG = (24, 38, 56)
PAPER_BG = (242, 239, 231)

CREAM = (237, 233, 223)
CREAM_DIM = (186, 182, 172)
INK = (27, 27, 31)
INK_DIM = (115, 112, 105)
GOLD = (199, 168, 91)
MUTED_RED = (184, 73, 63)
ATTR_DARK = (122, 120, 115)
ATTR_LIGHT = (138, 134, 124)


# ─── Common attribution renderer ─────────────────────────────────────────────
def draw_attribution(draw, attribution, color, y_pos=None, italic=False):
    """Render — Name in small Inter Regular at the bottom of the canvas."""
    if not attribution:
        return
    fnt = font(SANS, 22)
    text = f"— {attribution}"
    bbox = draw.textbbox((0, 0), text, font=fnt)
    x = (WIDTH - (bbox[2] - bbox[0])) // 2
    draw.text((x, y_pos or HEIGHT - 90), text, font=fnt, fill=color)


# ─── Style: minimal_serif ────────────────────────────────────────────────────
def render_minimal_serif(text, out_path, punch=None, attribution=None):
    img = bg_solid(DARK_BG)
    draw = ImageDraw.Draw(img)

    pad_x = 170
    pad_y = 280
    box_w = WIDTH - 2 * pad_x
    box_h = HEIGHT - 2 * pad_y - (60 if attribution else 0)

    b_font, b_lines, b_line_h, _ = fit_text(
        draw, text, SERIF,
        max_w=box_w, max_h=box_h,
        max_size=54, min_size=26, line_spacing=1.45,
    )

    total_h = b_line_h * len(b_lines)
    y = (HEIGHT - total_h) // 2 - (30 if attribution else 0)
    draw_centered_block(draw, b_lines, b_font, b_line_h, y, CREAM)

    draw_attribution(draw, attribution, ATTR_DARK)

    img.save(out_path, "PNG", optimize=True)


# ─── Style: soft_paper ───────────────────────────────────────────────────────
def render_soft_paper(text, out_path, punch=None, attribution=None):
    img = bg_paper(PAPER_BG)
    draw = ImageDraw.Draw(img)

    # Pick the emphasis word (single word, italicized in-line)
    emp = (punch or "").strip()
    if not emp or emp not in text:
        emp = pick_emphasis_word(text)

    pad_x = 170
    pad_y = 280
    box_w = WIDTH - 2 * pad_x
    box_h = HEIGHT - 2 * pad_y - (60 if attribution else 0)

    # Fit the whole text first (sets the size — emphasis font will match)
    b_font, b_lines, b_line_h, fsize = fit_text(
        draw, text, SERIF,
        max_w=box_w, max_h=box_h,
        max_size=50, min_size=26, line_spacing=1.5,
    )

    italic_font = font(SERIF_ITALIC, fsize)

    total_h = b_line_h * len(b_lines)
    y = (HEIGHT - total_h) // 2 - (30 if attribution else 0)

    # For each line, render in mixed regular + italic for the emphasis word
    for ln in b_lines:
        # Token split, preserving spaces between words
        tokens = ln.split(" ")
        # Measure widths to center
        token_meta = []
        total_line_w = 0
        for tk in tokens:
            # Strip trailing punctuation when comparing to emp
            stripped = tk.strip(".!?,;:\"'")
            is_emp = bool(emp) and stripped.lower() == emp.lower()
            use_font = italic_font if is_emp else b_font
            w = draw.textbbox((0, 0), tk, font=use_font)[2]
            token_meta.append((tk, use_font, w, is_emp))
            total_line_w += w
        space_w = draw.textbbox((0, 0), " ", font=b_font)[2]
        total_line_w += space_w * max(0, len(tokens) - 1)

        x = (WIDTH - total_line_w) // 2
        for i, (tk, f, w, is_emp) in enumerate(token_meta):
            color = MUTED_RED if is_emp else INK
            draw.text((x, y), tk, font=f, fill=color)
            x += w + (space_w if i < len(token_meta) - 1 else 0)
        y += b_line_h

    draw_attribution(draw, attribution, INK_DIM)
    img.save(out_path, "PNG", optimize=True)


# ─── Style: editorial_split ──────────────────────────────────────────────────
def render_editorial_split(text, out_path, punch=None, attribution=None):
    img = bg_solid(CHARCOAL_BG)
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

    pad_x = 180
    pad_y = 280
    box_w = WIDTH - 2 * pad_x
    box_h = HEIGHT - 2 * pad_y - (60 if attribution else 0)

    if setup:
        setup_budget = int(box_h * 0.45)
        payoff_budget = box_h - setup_budget - 40

        s_font, s_lines, s_line_h, _ = fit_text(
            draw, setup, SANS,
            max_w=box_w, max_h=setup_budget,
            max_size=42, min_size=24, line_spacing=1.55,
        )
        p_font, p_lines, p_line_h, _ = fit_text(
            draw, payoff, SANS,
            max_w=box_w, max_h=payoff_budget,
            max_size=48, min_size=26, line_spacing=1.5,
        )
        total_h = s_line_h * len(s_lines) + 40 + p_line_h * len(p_lines)
    else:
        p_font, p_lines, p_line_h, _ = fit_text(
            draw, payoff, SANS,
            max_w=box_w, max_h=box_h,
            max_size=50, min_size=26, line_spacing=1.5,
        )
        s_font, s_lines, s_line_h = None, [], 0
        total_h = p_line_h * len(p_lines)

    y = (HEIGHT - total_h) // 2 - (30 if attribution else 0)

    if setup:
        y = draw_centered_block(draw, s_lines, s_font, s_line_h, y, CREAM_DIM)
        y += 40

    draw_centered_block(draw, p_lines, p_font, p_line_h, y, CREAM)

    draw_attribution(draw, attribution, ATTR_DARK)
    img.save(out_path, "PNG", optimize=True)


# ─── Style: accent_payoff ────────────────────────────────────────────────────
def render_accent_payoff(text, out_path, punch=None, attribution=None):
    img = bg_solid(DARK_BG)
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

    pad_x = 170
    pad_y = 280
    box_w = WIDTH - 2 * pad_x
    box_h = HEIGHT - 2 * pad_y - (60 if attribution else 0)

    if setup:
        setup_budget = int(box_h * 0.45)
        payoff_budget = box_h - setup_budget - 36

        s_font, s_lines, s_line_h, _ = fit_text(
            draw, setup, SANS,
            max_w=box_w, max_h=setup_budget,
            max_size=42, min_size=24, line_spacing=1.5,
        )
        p_font, p_lines, p_line_h, _ = fit_text(
            draw, payoff, SERIF_ITALIC,
            max_w=box_w, max_h=payoff_budget,
            max_size=52, min_size=28, line_spacing=1.4,
        )
        total_h = s_line_h * len(s_lines) + 36 + p_line_h * len(p_lines)
    else:
        s_font, s_lines, s_line_h = None, [], 0
        p_font, p_lines, p_line_h, _ = fit_text(
            draw, payoff, SERIF_ITALIC,
            max_w=box_w, max_h=box_h,
            max_size=54, min_size=28, line_spacing=1.4,
        )
        total_h = p_line_h * len(p_lines)

    y = (HEIGHT - total_h) // 2 - (30 if attribution else 0)

    if setup:
        y = draw_centered_block(draw, s_lines, s_font, s_line_h, y, CREAM)
        y += 36

    draw_centered_block(draw, p_lines, p_font, p_line_h, y, GOLD)

    draw_attribution(draw, attribution, ATTR_DARK)
    img.save(out_path, "PNG", optimize=True)


# ─── Style: brand_block ──────────────────────────────────────────────────────
def render_brand_block(text, out_path, punch=None, attribution=None):
    img = bg_solid(NAVY_BG)
    draw = ImageDraw.Draw(img)

    pad_x = 160
    pad_y = 300
    box_w = WIDTH - 2 * pad_x
    box_h = HEIGHT - 2 * pad_y - (100 if attribution else 0)

    b_font, b_lines, b_line_h, _ = fit_text(
        draw, text, SANS,
        max_w=box_w, max_h=box_h,
        max_size=52, min_size=26, line_spacing=1.5,
    )

    total_h = b_line_h * len(b_lines)
    y = (HEIGHT - total_h) // 2 - (40 if attribution else 0)
    end_y = draw_centered_block(draw, b_lines, b_font, b_line_h, y, CREAM)

    # Thin rule + small attribution underneath
    if attribution:
        rule_y = end_y + 60
        rule_w = 60
        rule_x = (WIDTH - rule_w) // 2
        draw.line([(rule_x, rule_y), (rule_x + rule_w, rule_y)],
                  fill=ATTR_LIGHT, width=1)
        afnt = font(SANS, 22)
        atext = attribution
        bbox = draw.textbbox((0, 0), atext, font=afnt)
        x = (WIDTH - (bbox[2] - bbox[0])) // 2
        draw.text((x, rule_y + 24), atext, font=afnt, fill=ATTR_LIGHT)

    img.save(out_path, "PNG", optimize=True)


# ─── Style: scripture_card ───────────────────────────────────────────────────
def render_scripture_card(text, out_path, punch=None, attribution=None):
    img = bg_gradient(top=(21, 25, 43), bot=(8, 9, 15))
    draw = ImageDraw.Draw(img)

    pad_x = 180
    pad_y = 320
    box_w = WIDTH - 2 * pad_x
    box_h = HEIGHT - 2 * pad_y - (80 if attribution else 0)

    b_font, b_lines, b_line_h, _ = fit_text(
        draw, text, SERIF_ITALIC,
        max_w=box_w, max_h=box_h,
        max_size=46, min_size=24, line_spacing=1.55,
    )

    total_h = b_line_h * len(b_lines)
    y = (HEIGHT - total_h) // 2 - (30 if attribution else 0)

    # Tiny hairline above the text
    div_y = y - 50
    draw.line([(WIDTH // 2 - 30, div_y), (WIDTH // 2 + 30, div_y)],
              fill=CREAM_DIM, width=1)

    draw_centered_block(draw, b_lines, b_font, b_line_h, y, CREAM)

    draw_attribution(draw, attribution, CREAM_DIM)
    img.save(out_path, "PNG", optimize=True)


# ─── Style dispatch ──────────────────────────────────────────────────────────
RENDERERS = {
    "minimal_serif":   render_minimal_serif,
    "soft_paper":      render_soft_paper,
    "editorial_split": render_editorial_split,
    "accent_payoff":   render_accent_payoff,
    "brand_block":     render_brand_block,
    "scripture_card":  render_scripture_card,
}

# Backward-compat aliases — old style names still map to a sensible new style
LEGACY_STYLE_ALIASES = {
    "grunge_accent":   "accent_payoff",
    "vintage_press":   "soft_paper",
    "editorial_wide":  "editorial_split",
}


def pick_default_style(text):
    """Heuristic style picker for quotes lacking an explicit style tag."""
    t = text.strip()
    lower = t.lower()
    if any(kw in lower for kw in ["scripture", "psalm", " verse", "amen",
                                  "father in heaven", "lord ", " holy spirit"]):
        return "scripture_card"
    if " is god's " in lower or "your greatest" in lower:
        return "soft_paper"
    if t.count(".") >= 2 or " if " in lower or " when " in lower or " but " in lower:
        return "editorial_split"
    if lower.startswith(("stop ", "start ", "don't ", "you can't", "you have to",
                         "we can't", "we need", "you need")):
        return "accent_payoff"
    if len(t) <= 80 and t.endswith(".") and t.count(".") <= 1:
        return "brand_block"
    return "minimal_serif"


def normalize_quote(q):
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

    if force_style:
        force_style = LEGACY_STYLE_ALIASES.get(force_style, force_style)
        if force_style not in RENDERERS:
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
            # Resolve legacy tag → new style name
            chosen = force_style or LEGACY_STYLE_ALIASES.get(tag, tag) or pick_default_style(text)
            if chosen not in RENDERERS:
                chosen = "minimal_serif"
            png = os.path.join(out_dir, slug + ".png")
            RENDERERS[chosen](text, png, punch=punch, attribution=attribution)
            style_counts[chosen] += 1
            print(f"  [{i:02d}] ({chosen:<16}) {preview}")

    print(f"\n✓ {sum(style_counts.values())} renders → {out_dir}")
    print("  by style:")
    for s in ALL_STYLES:
        if style_counts.get(s, 0):
            print(f"    {s:<16} {style_counts[s]}")


if __name__ == "__main__":
    main()
