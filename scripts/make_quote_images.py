#!/usr/bin/env python3
"""
Render quote cards from viral_clips/quotes.json as 1080×1350 (Instagram 4:5)
images, inspired by Sunday Social TV / Bayside-style sermon quote dumps.

Pipeline:
  1. Read viral_clips/quotes.json (deduped raw quotes).
  2. If there are more than ~8 quotes, call Claude to curate the strongest
     4-8 and tag each with style + palette + line breaks + emphasis spans.
     Cached at viral_clips/quotes_curated.json. Skip the call when the
     cache is already present and the input quotes haven't changed.
  3. Render each curated quote as a 1080×1350 PNG.

Six layouts (no carousel numbering, no attribution per spec):
  massive_stack     — big stacked caps, each phrase on its own line, left-aligned
  mixed_hierarchy   — caps with size emphasis: small lines + HUGE emphasis lines
  stacked_payoff    — setup line(s) at medium size, payoff line at HUGE size
  classic_caps      — centered all-caps, single uniform size, generous padding
  centered_with_rule — centered small caps with horizontal rule accents
  scripture_card    — sentence-case italic serif on soft gradient (contemplative)

Two palettes per layout: light (paper cream + ink) and dark (black + cream).
Roughly 70/30 light/dark distribution across a curated set.

Usage:
    python3 make_quote_images.py [work_dir]
                                  [--style <name>]   (force one style)
                                  [--palette light|dark]
                                  [--no-curate]      (render every quote in quotes.json as-is)
                                  [--re-curate]      (ignore existing quotes_curated.json)
                                  [--limit N]        (cap at N curated cards)

Outputs:
    <work_dir>/quote_images/quote_NN.png
    <work_dir>/quote_images/quote_NN.txt
    <work_dir>/viral_clips/quotes_curated.json   (cached Claude curate output)
"""

import argparse
import hashlib
import json
import os
import random
import re
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont, ImageFilter

# ─── Canvas ──────────────────────────────────────────────────────────────────
WIDTH = 1080
HEIGHT = 1350

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT_DIR = os.path.join(SKILL_ROOT, "fonts")

# ─── Bundled fonts ──────────────────────────────────────────────────────────
ANTON = "Anton-Regular.ttf"           # condensed display caps — primary for big type
INTER_BLACK = "Inter-Black.ttf"       # heavy uniform caps
INTER_BOLD = "Inter-Bold.ttf"
INTER = "Inter-Regular.ttf"
INTER_ITALIC = "Inter-Italic.ttf"
LORA = "Lora-Regular.ttf"
LORA_ITALIC = "Lora-Italic.ttf"

ALL_STYLES = [
    "massive_stack",
    "mixed_hierarchy",
    "stacked_payoff",
    "classic_caps",
    "centered_with_rule",
    "scripture_card",
]

# Old style names from earlier iterations — remapped so legacy quotes.json
# files keep rendering.
LEGACY_STYLE_ALIASES = {
    "grunge_accent":   "stacked_payoff",
    "vintage_press":   "mixed_hierarchy",
    "editorial_wide":  "stacked_payoff",
    "editorial_split": "stacked_payoff",
    "minimal_serif":   "scripture_card",
    "soft_paper":      "mixed_hierarchy",
    "accent_payoff":   "stacked_payoff",
    "brand_block":     "classic_caps",
}


def font(name, size):
    path = os.path.join(FONT_DIR, name)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Font missing: {path}")
    return ImageFont.truetype(path, size)


# ─── Palettes ────────────────────────────────────────────────────────────────
PAPER = {
    "bg":      (240, 235, 225),
    "ink":     (22, 22, 24),
    "ink_dim": (105, 100, 92),
    "rule":    (22, 22, 24),
}
NOIR = {
    "bg":      (10, 10, 12),
    "ink":     (240, 235, 225),
    "ink_dim": (175, 170, 160),
    "rule":    (240, 235, 225),
}


def palette_for(name):
    return NOIR if name == "dark" else PAPER


def bg_image(palette, paper_grain=True):
    """Solid palette bg with a touch of grain to avoid banding."""
    base = Image.new("RGB", (WIDTH, HEIGHT), palette["bg"])
    if not paper_grain:
        return base
    sigma = 7 if palette is PAPER else 6
    noise = Image.effect_noise((WIDTH, HEIGHT), sigma).convert("RGB")
    return Image.blend(base, noise, 0.05)


def bg_gradient(top, bot):
    img = Image.new("RGB", (WIDTH, HEIGHT), top)
    px = img.load()
    for y in range(HEIGHT):
        t = y / max(1, HEIGHT - 1)
        r = int(top[0] * (1 - t) + bot[0] * t)
        g = int(top[1] * (1 - t) + bot[1] * t)
        b = int(top[2] * (1 - t) + bot[2] * t)
        for x in range(WIDTH):
            px[x, y] = (r, g, b)
    return img


# ─── Text helpers ────────────────────────────────────────────────────────────
def line_width(draw, text, font_obj):
    return draw.textbbox((0, 0), text, font=font_obj)[2]


def fit_uniform(draw, lines, font_name, max_w, max_h, max_size=240, min_size=40,
                line_spacing=1.02):
    """Find the largest font size such that every pre-broken line fits max_w
    and the stacked block fits max_h. `lines` is a list of strings."""
    if not lines:
        return None, 0, 0
    lo, hi = min_size, max_size
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        f = font(font_name, mid)
        widest = max(line_width(draw, ln, f) for ln in lines)
        a, d = f.getmetrics()
        line_h = int((a + d) * line_spacing)
        total_h = line_h * len(lines)
        if widest <= max_w and total_h <= max_h:
            best = (f, line_h, mid)
            lo = mid + 1
        else:
            hi = mid - 1
    if best is None:
        f = font(font_name, min_size)
        a, d = f.getmetrics()
        line_h = int((a + d) * line_spacing)
        best = (f, line_h, min_size)
    return best


def wrap_balanced(text, max_words_per_line):
    """Greedy line wrap for caps body — break by word count, trying to keep
    natural punctuation breaks together."""
    words = text.split()
    if not words:
        return []
    lines, cur = [], []
    for w in words:
        cur.append(w)
        ends_clause = w.endswith((",", ".", ":", ";", "!", "?"))
        if len(cur) >= max_words_per_line or ends_clause:
            lines.append(" ".join(cur))
            cur = []
    if cur:
        lines.append(" ".join(cur))
    return lines


# ─── Style: massive_stack ────────────────────────────────────────────────────
def render_massive_stack(text, out_path, lines=None, palette="light",
                         emphasis=None, punch=None):
    """Big stacked caps, each phrase on its own line, left-aligned.

    If `lines` is supplied (a list of strings from Claude), use that breaking.
    Otherwise auto-break the text into roughly 2-3 word lines.
    """
    pal = palette_for(palette)
    img = bg_image(pal)
    draw = ImageDraw.Draw(img)

    raw_lines = lines if lines else wrap_balanced(text, max_words_per_line=3)
    raw_lines = [ln.upper() for ln in raw_lines if ln and ln.strip()]
    if not raw_lines:
        raw_lines = [text.upper()]

    pad_x = 100
    pad_y = 130
    max_w = WIDTH - 2 * pad_x
    max_h = HEIGHT - 2 * pad_y

    f, line_h, _ = fit_uniform(
        draw, raw_lines, ANTON,
        max_w=max_w, max_h=max_h,
        max_size=260, min_size=58, line_spacing=1.02,
    )
    total_h = line_h * len(raw_lines)
    y = (HEIGHT - total_h) // 2

    for ln in raw_lines:
        draw.text((pad_x, y), ln, font=f, fill=pal["ink"])
        y += line_h

    img.save(out_path, "PNG", optimize=True)


# ─── Style: mixed_hierarchy ──────────────────────────────────────────────────
def render_mixed_hierarchy(text, out_path, palette="light", emphasis=None,
                           lines=None, punch=None):
    """Mix of small caps + HUGE emphasis caps, left-aligned and stacked.

    `emphasis` is a list of verbatim substrings that should render at the
    huge size. The text is segmented around them — non-emphasis spans are
    broken into 1-3 word lines at the small size; each emphasis span gets
    its own large line(s).
    """
    pal = palette_for(palette)
    img = bg_image(pal)
    draw = ImageDraw.Draw(img)

    emph = [e.strip() for e in (emphasis or []) if e and e.strip() and e in text]
    if not emph and punch and punch in text:
        emph = [punch]

    # Build an ordered list of (text, is_huge) spans
    segs = []
    cursor = 0
    if emph:
        # Order by appearance
        positions = sorted(
            ((text.find(e, cursor), e) for e in emph if text.find(e, cursor) >= 0),
            key=lambda x: x[0],
        )
        seen_idx = -1
        for idx, e in positions:
            if idx < seen_idx:
                continue
            if idx > cursor:
                segs.append((text[cursor:idx].strip(" "), False))
            segs.append((e.strip(" "), True))
            cursor = idx + len(e)
            seen_idx = cursor
    if cursor < len(text):
        segs.append((text[cursor:].strip(" "), False))
    if not segs:
        segs = [(text, False)]

    # Each segment → 1+ lines (UPPER, 1-3 words per line)
    plan = []  # list of (line_text_upper, is_huge)
    for txt, is_huge in segs:
        if not txt:
            continue
        # tighter wrap for small spans, looser for big ones (single phrase per line)
        wpl = 4 if is_huge else 3
        for ln in wrap_balanced(txt, max_words_per_line=wpl):
            plan.append((ln.upper(), is_huge))

    if not plan:
        plan = [(text.upper(), True)]

    pad_x = 100
    pad_y = 130
    max_w = WIDTH - 2 * pad_x
    max_h = HEIGHT - 2 * pad_y

    # Pick sizes that fit: huge ~2.2× small. Binary search small size such
    # that everything fits both width and height.
    small_lines = [ln for ln, h in plan if not h]
    huge_lines = [ln for ln, h in plan if h]

    def lay(small_size):
        huge_size = int(small_size * 2.2)
        f_s = font(INTER_BLACK, small_size)
        f_h = font(ANTON, huge_size)
        a_s, d_s = f_s.getmetrics()
        a_h, d_h = f_h.getmetrics()
        line_h_s = int((a_s + d_s) * 1.10)
        line_h_h = int((a_h + d_h) * 1.02)
        # height
        h = sum(line_h_h if is_h else line_h_s for _, is_h in plan)
        # widest
        widest = 0
        for ln, is_h in plan:
            f = f_h if is_h else f_s
            widest = max(widest, line_width(draw, ln, f))
        return f_s, f_h, line_h_s, line_h_h, h, widest

    lo, hi = 30, 110
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        f_s, f_h, lh_s, lh_h, h, widest = lay(mid)
        if widest <= max_w and h <= max_h:
            best = (mid, f_s, f_h, lh_s, lh_h, h)
            lo = mid + 1
        else:
            hi = mid - 1
    if best is None:
        f_s, f_h, lh_s, lh_h, h, _ = lay(30)
        best = (30, f_s, f_h, lh_s, lh_h, h)

    _, f_s, f_h, lh_s, lh_h, total_h = best
    y = (HEIGHT - total_h) // 2

    for ln, is_h in plan:
        f = f_h if is_h else f_s
        lh = lh_h if is_h else lh_s
        draw.text((pad_x, y), ln, font=f, fill=pal["ink"])
        y += lh

    img.save(out_path, "PNG", optimize=True)


# ─── Style: stacked_payoff ───────────────────────────────────────────────────
def render_stacked_payoff(text, out_path, punch=None, palette="light",
                          emphasis=None, lines=None):
    """Setup line(s) at medium size, payoff line at HUGE size below."""
    pal = palette_for(palette)
    img = bg_image(pal)
    draw = ImageDraw.Draw(img)

    setup, payoff = "", text
    if punch and punch in text:
        i = text.rfind(punch)
        setup = text[:i].rstrip(" ,;:—–-")
        payoff = punch
    elif emphasis:
        for e in emphasis:
            if e and e in text:
                i = text.rfind(e)
                setup = text[:i].rstrip(" ,;:—–-")
                payoff = e
                break

    if not setup:
        # Try to split on last sentence boundary or last clause connector
        parts = re.split(r'(?<=[.!?])\s+', text.strip())
        if len(parts) >= 2:
            setup = " ".join(parts[:-1])
            payoff = parts[-1]
        else:
            for sp in [", but ", " but ", ", and ", " — ", " if ", " when "]:
                idx = text.lower().rfind(sp)
                if idx > len(text) * 0.25:
                    setup = text[:idx + len(sp)].rstrip()
                    payoff = text[idx + len(sp):]
                    break

    setup_lines = wrap_balanced(setup, max_words_per_line=4) if setup else []
    payoff_lines = wrap_balanced(payoff, max_words_per_line=3)
    setup_lines = [ln.upper() for ln in setup_lines if ln]
    payoff_lines = [ln.upper() for ln in payoff_lines if ln]

    pad_x = 100
    pad_y = 130
    max_w = WIDTH - 2 * pad_x
    max_h = HEIGHT - 2 * pad_y

    def lay(setup_size):
        payoff_size = int(setup_size * 2.1)
        f_su = font(INTER_BLACK, setup_size)
        f_po = font(ANTON, payoff_size)
        a, d = f_su.getmetrics()
        lh_su = int((a + d) * 1.08)
        a, d = f_po.getmetrics()
        lh_po = int((a + d) * 1.02)
        gap = max(20, int(setup_size * 0.55))
        h = lh_su * len(setup_lines) + (gap if setup_lines else 0) + lh_po * len(payoff_lines)
        widest = 0
        for ln in setup_lines:
            widest = max(widest, line_width(draw, ln, f_su))
        for ln in payoff_lines:
            widest = max(widest, line_width(draw, ln, f_po))
        return f_su, f_po, lh_su, lh_po, gap, h, widest

    lo, hi = 32, 110
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        f_su, f_po, lh_su, lh_po, gap, h, widest = lay(mid)
        if widest <= max_w and h <= max_h:
            best = (mid, f_su, f_po, lh_su, lh_po, gap, h)
            lo = mid + 1
        else:
            hi = mid - 1
    if best is None:
        f_su, f_po, lh_su, lh_po, gap, h, _ = lay(32)
        best = (32, f_su, f_po, lh_su, lh_po, gap, h)

    _, f_su, f_po, lh_su, lh_po, gap, total_h = best
    y = (HEIGHT - total_h) // 2

    for ln in setup_lines:
        draw.text((pad_x, y), ln, font=f_su, fill=pal["ink"])
        y += lh_su
    if setup_lines:
        y += gap
    for ln in payoff_lines:
        draw.text((pad_x, y), ln, font=f_po, fill=pal["ink"])
        y += lh_po

    img.save(out_path, "PNG", optimize=True)


# ─── Style: classic_caps ─────────────────────────────────────────────────────
def render_classic_caps(text, out_path, palette="light", emphasis=None,
                        lines=None, punch=None):
    """Centered uniform caps in Inter Black. Balanced size, generous padding."""
    pal = palette_for(palette)
    img = bg_image(pal)
    draw = ImageDraw.Draw(img)

    raw_lines = lines if lines else wrap_balanced(text, max_words_per_line=3)
    raw_lines = [ln.upper() for ln in raw_lines if ln]
    if not raw_lines:
        raw_lines = [text.upper()]

    pad_x = 100
    pad_y = 170
    max_w = WIDTH - 2 * pad_x
    max_h = HEIGHT - 2 * pad_y

    f, line_h, _ = fit_uniform(
        draw, raw_lines, INTER_BLACK,
        max_w=max_w, max_h=max_h,
        max_size=180, min_size=44, line_spacing=1.08,
    )
    total_h = line_h * len(raw_lines)
    y = (HEIGHT - total_h) // 2

    for ln in raw_lines:
        w = line_width(draw, ln, f)
        x = (WIDTH - w) // 2
        draw.text((x, y), ln, font=f, fill=pal["ink"])
        y += line_h

    # Subtle rule below the block for visual closure
    rule_y = y + 30
    if rule_y < HEIGHT - 70:
        rule_w = 70
        rule_x = (WIDTH - rule_w) // 2
        draw.line([(rule_x, rule_y), (rule_x + rule_w, rule_y)],
                  fill=pal["rule"], width=2)

    img.save(out_path, "PNG", optimize=True)


# ─── Style: centered_with_rule ───────────────────────────────────────────────
def render_centered_with_rule(text, out_path, palette="light", emphasis=None,
                              lines=None, punch=None):
    """Centered small caps with horizontal rule accents.

    Two-tier when emphasis or punch is provided: setup (small) above the
    emphasis (slightly larger), with thin rules above and below.
    """
    pal = palette_for(palette)
    img = bg_image(pal)
    draw = ImageDraw.Draw(img)

    setup, accent = "", text
    if punch and punch in text:
        i = text.rfind(punch)
        setup = text[:i].rstrip(" ,;:—–-")
        accent = punch
    elif emphasis:
        for e in emphasis:
            if e and e in text:
                i = text.rfind(e)
                setup = text[:i].rstrip(" ,;:—–-")
                accent = e
                break

    setup_lines = wrap_balanced(setup, max_words_per_line=5) if setup else []
    accent_lines = wrap_balanced(accent, max_words_per_line=4)
    setup_lines = [ln.upper() for ln in setup_lines if ln]
    accent_lines = [ln.upper() for ln in accent_lines if ln]

    pad_x = 130
    pad_y = 180
    max_w = WIDTH - 2 * pad_x
    max_h = HEIGHT - 2 * pad_y

    def lay(setup_size):
        accent_size = int(setup_size * 1.5)
        f_su = font(INTER, setup_size) if setup_lines else None
        f_ac = font(INTER_BLACK, accent_size)
        lh_su = 0
        if f_su:
            a, d = f_su.getmetrics()
            lh_su = int((a + d) * 1.25)
        a, d = f_ac.getmetrics()
        lh_ac = int((a + d) * 1.10)
        gap = 28 if setup_lines else 0
        h = lh_su * len(setup_lines) + gap + lh_ac * len(accent_lines)
        widest = 0
        for ln in setup_lines:
            widest = max(widest, line_width(draw, ln, f_su))
        for ln in accent_lines:
            widest = max(widest, line_width(draw, ln, f_ac))
        return f_su, f_ac, lh_su, lh_ac, gap, h, widest

    lo, hi = 28, 70
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        f_su, f_ac, lh_su, lh_ac, gap, h, widest = lay(mid)
        if widest <= max_w and h <= max_h:
            best = (mid, f_su, f_ac, lh_su, lh_ac, gap, h)
            lo = mid + 1
        else:
            hi = mid - 1
    if best is None:
        f_su, f_ac, lh_su, lh_ac, gap, h, _ = lay(28)
        best = (28, f_su, f_ac, lh_su, lh_ac, gap, h)

    _, f_su, f_ac, lh_su, lh_ac, gap, total_h = best
    y_block_top = (HEIGHT - total_h) // 2

    # Rule above the block (only if there's setup; for accent-only render rule below)
    if setup_lines:
        draw.line([(WIDTH // 2 - 40, y_block_top - 30),
                   (WIDTH // 2 + 40, y_block_top - 30)],
                  fill=pal["rule"], width=2)

    y = y_block_top
    for ln in setup_lines:
        w = line_width(draw, ln, f_su)
        x = (WIDTH - w) // 2
        draw.text((x, y), ln, font=f_su, fill=pal["ink"])
        y += lh_su
    if setup_lines:
        y += gap
    for ln in accent_lines:
        w = line_width(draw, ln, f_ac)
        x = (WIDTH - w) // 2
        draw.text((x, y), ln, font=f_ac, fill=pal["ink"])
        y += lh_ac

    # Rule below the block
    rule_y = y + 26
    if rule_y < HEIGHT - 80:
        draw.line([(WIDTH // 2 - 40, rule_y),
                   (WIDTH // 2 + 40, rule_y)],
                  fill=pal["rule"], width=2)

    img.save(out_path, "PNG", optimize=True)


# ─── Style: scripture_card ───────────────────────────────────────────────────
def render_scripture_card(text, out_path, palette="dark", emphasis=None,
                          lines=None, punch=None):
    """Italic Lora, sentence case, soft gradient. The contemplative one."""
    pal = palette_for(palette)
    # Always render on a soft gradient regardless of palette choice
    if palette == "light":
        img = bg_gradient((242, 238, 228), (220, 214, 200))
        text_color = (35, 30, 26)
        rule_color = (110, 102, 90)
    else:
        img = bg_gradient((21, 25, 43), (8, 9, 15))
        text_color = (237, 233, 223)
        rule_color = (153, 150, 143)

    draw = ImageDraw.Draw(img)

    pad_x = 170
    pad_y = 280
    max_w = WIDTH - 2 * pad_x
    max_h = HEIGHT - 2 * pad_y

    # Simple wrap for natural prose
    words = text.split()
    cur = ""
    body_lines = []
    f_try = font(LORA_ITALIC, 48)
    for w in words:
        trial = (cur + " " + w).strip()
        if line_width(draw, trial, f_try) <= max_w or not cur:
            cur = trial
        else:
            body_lines.append(cur)
            cur = w
    if cur:
        body_lines.append(cur)

    f, line_h, _ = fit_uniform(
        draw, body_lines, LORA_ITALIC,
        max_w=max_w, max_h=max_h,
        max_size=58, min_size=28, line_spacing=1.50,
    )
    total_h = line_h * len(body_lines)
    y = (HEIGHT - total_h) // 2

    # Hairline above
    draw.line([(WIDTH // 2 - 40, y - 50), (WIDTH // 2 + 40, y - 50)],
              fill=rule_color, width=2)

    for ln in body_lines:
        w = line_width(draw, ln, f)
        x = (WIDTH - w) // 2
        draw.text((x, y), ln, font=f, fill=text_color)
        y += line_h

    img.save(out_path, "PNG", optimize=True)


# ─── Dispatch ────────────────────────────────────────────────────────────────
RENDERERS = {
    "massive_stack":      render_massive_stack,
    "mixed_hierarchy":    render_mixed_hierarchy,
    "stacked_payoff":     render_stacked_payoff,
    "classic_caps":       render_classic_caps,
    "centered_with_rule": render_centered_with_rule,
    "scripture_card":     render_scripture_card,
}


def resolve_style(name):
    if not name:
        return None
    name = name.strip()
    name = LEGACY_STYLE_ALIASES.get(name, name)
    return name if name in RENDERERS else None


# ─── Curate step (Claude call) ───────────────────────────────────────────────
CURATE_PROMPT = """You are curating a Sermon Quote Dump — an Instagram carousel of 4-8 typographic quote cards from a single Cross Church sermon. Pick the BEST 4-8 quotes for the carousel and tag each with how it should be visually composed.

INPUT — the deduped raw quote list (each pre-filtered for standalone readability):
{quotes_list}

YOUR JOB
1. Pick 4-8 quotes that, together, make the strongest carousel. Optimize for:
   - Sharpness: each one lands as a complete punchy thought on its own
   - Diversity: no two quotes saying the same thing — pick distinct beats
   - Visual range: prefer quotes that can be composed differently from each other
2. For each pick, return EXACTLY this shape with these fields:

   text     — the verbatim quote (you may lightly tweak punctuation/casing
              for typographic clarity, but do not paraphrase)
   style    — one of:
                  massive_stack     (big stacked caps; works for short
                                     punchy declarations 5-12 words)
                  mixed_hierarchy   (mix of small caps + HUGE emphasis caps —
                                     great when 1-3 words deserve to be massive
                                     while the rest reads at a smaller size)
                  stacked_payoff    (setup phrase → payoff phrase — best for
                                     two-part X but Y / X if Y constructions)
                  classic_caps      (centered uniform caps — good for clean
                                     identity statements / scripture-flavored)
                  centered_with_rule(centered, smaller, with horizontal rule
                                     accents — for reflective wisdom one-liners)
                  scripture_card    (sentence-case italic serif on gradient —
                                     for prayer-language / scripture / sacred-feeling)
   palette  — "light" (paper cream) or "dark" (black). Aim for roughly
              70% light / 30% dark across the carousel.
   lines    — optional list of strings. For massive_stack / classic_caps you
              MAY pass the exact line breaks (e.g. ["DEATH", "DOES NOT",
              "GET THE", "FINAL WORD."]). 1-3 words per line is ideal.
              Omit to let the renderer auto-break.
   emphasis — optional list of verbatim substrings of `text` that the
              renderer should treat as the "huge" or "accent" content.
              For mixed_hierarchy: the substrings that go HUGE.
              For stacked_payoff / centered_with_rule: a single substring
              that is the payoff/accent (rest becomes setup).
              For massive_stack / classic_caps / scripture_card: omit.

OUTPUT — ONLY valid JSON (no markdown, no commentary):
{{
  "curated": [
    {{
      "text": "...",
      "style": "...",
      "palette": "light" | "dark",
      "lines": ["...", "..."],
      "emphasis": ["..."]
    }}
  ]
}}"""


def _hash_quotes(quotes_raw):
    h = hashlib.sha256()
    for q in quotes_raw:
        if isinstance(q, str):
            h.update(q.encode("utf-8"))
        elif isinstance(q, dict):
            h.update((q.get("text") or "").encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:12]


def curate(quotes_raw, work_dir, cache_path, force=False, limit=None):
    """Ask Claude to pick the strongest 4-8 quotes with full visual tagging.

    Caches result to cache_path; reuses if input hash matches.
    Falls back to a heuristic pick (top-N by length-and-punch heuristic) if
    Claude is unavailable.
    """
    quotes_text = []
    for q in quotes_raw:
        if isinstance(q, str):
            quotes_text.append(q.strip())
        elif isinstance(q, dict):
            t = (q.get("text") or "").strip()
            if t:
                quotes_text.append(t)
    quotes_text = [q for q in quotes_text if q]

    if not quotes_text:
        return []

    input_hash = _hash_quotes(quotes_text)

    # Check cache
    if not force and os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                cached = json.load(f)
            if cached.get("input_hash") == input_hash and cached.get("curated"):
                print(f"  using cached curation ({len(cached['curated'])} cards)")
                if limit:
                    return cached["curated"][:limit]
                return cached["curated"]
        except (json.JSONDecodeError, KeyError):
            pass

    # Build the prompt input list
    numbered = "\n".join(f"{i+1}. {q}" for i, q in enumerate(quotes_text))
    prompt = CURATE_PROMPT.format(quotes_list=numbered)

    print(f"  asking Claude to curate {len(quotes_text)} → 4-8 cards...")
    r = subprocess.run(
        ["claude", "-p", prompt],
        capture_output=True, text=True, cwd=work_dir, timeout=600,
    )
    if r.returncode != 0:
        print(f"  ✗ Claude error: {r.stderr[:200]}")
        return _heuristic_curate(quotes_text, limit=limit or 6)

    m = re.search(r"\{[\s\S]*\}", r.stdout.strip())
    if not m:
        print("  ✗ no JSON in Claude response")
        return _heuristic_curate(quotes_text, limit=limit or 6)

    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        print(f"  ✗ JSON parse error: {e}")
        return _heuristic_curate(quotes_text, limit=limit or 6)

    curated = data.get("curated") or []
    # Validate + normalize
    clean = []
    for c in curated:
        text = (c.get("text") or "").strip()
        if not text:
            continue
        style = resolve_style(c.get("style"))
        if not style:
            style = "classic_caps"
        palette = c.get("palette", "light")
        if palette not in ("light", "dark"):
            palette = "light"
        lines = c.get("lines") or None
        if lines and not isinstance(lines, list):
            lines = None
        emphasis = c.get("emphasis") or []
        if isinstance(emphasis, str):
            emphasis = [emphasis]
        emphasis = [e for e in emphasis if isinstance(e, str) and e.strip()]
        clean.append({
            "text": text,
            "style": style,
            "palette": palette,
            "lines": lines,
            "emphasis": emphasis,
        })

    if not clean:
        return _heuristic_curate(quotes_text, limit=limit or 6)

    if limit:
        clean = clean[:limit]

    # Cache
    try:
        with open(cache_path, "w") as f:
            json.dump({"input_hash": input_hash, "curated": clean}, f, indent=2)
    except OSError:
        pass

    return clean


def _heuristic_curate(quotes_text, limit=6):
    """Last-resort fallback when Claude is unreachable. Picks shorter punchy
    quotes and alternates styles/palettes mechanically."""
    scored = []
    for q in quotes_text:
        length_penalty = max(0, len(q) - 90) * 0.02
        period_bonus = 0.3 if q.endswith(".") else 0
        score = period_bonus - length_penalty + (1.0 / max(1, len(q.split())))
        scored.append((score, q))
    scored.sort(reverse=True)
    picks = [q for _, q in scored[:limit]]
    styles = ["massive_stack", "mixed_hierarchy", "stacked_payoff",
              "classic_caps", "centered_with_rule", "scripture_card"]
    out = []
    rng = random.Random(7)
    for i, q in enumerate(picks):
        out.append({
            "text": q,
            "style": styles[i % len(styles)],
            "palette": "light" if rng.random() < 0.7 else "dark",
            "lines": None,
            "emphasis": [],
        })
    return out


# ─── Main ────────────────────────────────────────────────────────────────────
def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("work_dir", nargs="?", default=None)
    ap.add_argument("--style")
    ap.add_argument("--palette", choices=["light", "dark"])
    ap.add_argument("--no-curate", action="store_true",
                    help="Render every quote in quotes.json as-is (skip Claude curate step)")
    ap.add_argument("--re-curate", action="store_true",
                    help="Ignore cached quotes_curated.json")
    ap.add_argument("--limit", type=int)
    return ap.parse_args()


def main():
    args = parse_args()
    work_dir = args.work_dir or os.getcwd()
    force_style = resolve_style(args.style) if args.style else None
    if args.style and not force_style:
        print(f"✗ Unknown style: {args.style}")
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
    cache_path = os.path.join(work_dir, "viral_clips", "quotes_curated.json")

    print(f"Input  : {quotes_path}  ({len(quotes_raw)} raw)")
    print(f"Output : {out_dir}")
    print(f"Fonts  : {FONT_DIR}")

    if args.no_curate:
        # Render every quote in quotes.json with heuristic style assignment.
        # Preserves the v2-era "render all" behavior for anyone who wants it.
        curated = []
        for q in quotes_raw:
            if isinstance(q, str):
                text = q.strip()
                d = {"text": text, "style": "classic_caps", "palette": "light",
                     "lines": None, "emphasis": []}
            elif isinstance(q, dict):
                text = (q.get("text") or "").strip()
                if not text:
                    continue
                style = resolve_style(q.get("style")) or "classic_caps"
                palette = q.get("palette") if q.get("palette") in ("light", "dark") else "light"
                emphasis = q.get("emphasis") or ([q["punch"]] if q.get("punch") else [])
                if isinstance(emphasis, str):
                    emphasis = [emphasis]
                d = {"text": text, "style": style, "palette": palette,
                     "lines": q.get("lines"), "emphasis": emphasis}
            else:
                continue
            curated.append(d)
        if args.limit:
            curated = curated[:args.limit]
    else:
        curated = curate(
            quotes_raw, work_dir, cache_path,
            force=args.re_curate, limit=args.limit,
        )

    if not curated:
        print("✗ Nothing to render after curate step")
        sys.exit(1)

    # Force overrides
    if force_style:
        for c in curated:
            c["style"] = force_style
    if args.palette:
        for c in curated:
            c["palette"] = args.palette

    print(f"Cards  : {len(curated)}\n")
    by_style = {}
    by_pal = {"light": 0, "dark": 0}

    for i, c in enumerate(curated, start=1):
        slug = f"quote_{i:02d}"
        png_path = os.path.join(out_dir, slug + ".png")
        txt_path = os.path.join(out_dir, slug + ".txt")
        with open(txt_path, "w") as f:
            f.write(c["text"] + "\n")

        style = c["style"]
        renderer = RENDERERS.get(style, render_classic_caps)
        renderer(
            c["text"], png_path,
            palette=c.get("palette", "light"),
            emphasis=c.get("emphasis") or [],
            lines=c.get("lines"),
            punch=(c.get("emphasis") or [None])[0] if c.get("emphasis") else None,
        )
        by_style[style] = by_style.get(style, 0) + 1
        by_pal[c.get("palette", "light")] = by_pal.get(c.get("palette", "light"), 0) + 1
        preview = c["text"] if len(c["text"]) <= 70 else c["text"][:67] + "..."
        print(f"  [{i:02d}] ({style:<19} / {c.get('palette','light'):<5}) {preview}")

    print(f"\n✓ {len(curated)} cards → {out_dir}")
    print("  by style:")
    for s in ALL_STYLES:
        if by_style.get(s):
            print(f"    {s:<19} {by_style[s]}")
    print(f"  palette: light {by_pal['light']}  /  dark {by_pal['dark']}")


if __name__ == "__main__":
    main()
