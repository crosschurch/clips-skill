#!/usr/bin/env python3
"""
Render each quote in viral_clips/quotes.json as a 1080x1080 quote-card image.

Usage:
    python3 make_quote_images.py [work_dir] [--attribution "Pastor Name"]
    work_dir defaults to CWD

Output:
    <work_dir>/quote_images/quote_NN.png
    <work_dir>/quote_images/quote_NN.txt   (the source text — for caption copy)
"""

import json
import os
import sys
import textwrap

from PIL import Image, ImageDraw, ImageFont

WIDTH = 1080
HEIGHT = 1080
BG = (10, 10, 10)
FG = (240, 240, 235)
DIM = (140, 140, 138)
MARK_COLOR = (60, 60, 58)
SIDE_PAD = 110          # left/right inset for the text block
TOP_BOTTOM_PAD = 120    # min vertical inset
LINE_SPACING = 1.18     # leading multiplier on font size

QUOTE_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Georgia Bold.ttf",
    "/System/Library/Fonts/Supplemental/Georgia.ttf",
    "/System/Library/Fonts/SFGeorgian.ttf",
    "/System/Library/Fonts/Supplemental/Charter.ttc",
]
ATTRIBUTION_FONT_CANDIDATES = [
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]


def first_existing(paths):
    for p in paths:
        if os.path.exists(p):
            return p
    return None


def load_font(path, size):
    return ImageFont.truetype(path, size)


def wrap_to_fit(draw, text, font, max_width):
    """Greedy word-wrap so each rendered line stays within max_width."""
    words = text.split()
    lines = []
    cur = ""
    for w in words:
        trial = (cur + " " + w).strip()
        bbox = draw.textbbox((0, 0), trial, font=font)
        if bbox[2] - bbox[0] <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def fit_quote(draw, text, font_path, max_width, max_height):
    """Pick the largest font size where wrapped text fits the box.

    Capped at 84pt so short punchy quotes don't blow up to where every
    2-3 words land on their own line. Long quotes scale down to fit.
    """
    lo, hi = 36, 84
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        font = load_font(font_path, mid)
        lines = wrap_to_fit(draw, text, font, max_width)
        ascent, descent = font.getmetrics()
        line_h = int((ascent + descent) * LINE_SPACING)
        total_h = line_h * len(lines)
        widest = max(
            (draw.textbbox((0, 0), ln, font=font)[2] for ln in lines), default=0
        )
        if total_h <= max_height and widest <= max_width:
            best = (font, lines, line_h)
            lo = mid + 1
        else:
            hi = mid - 1
    if best is None:
        font = load_font(font_path, 36)
        lines = wrap_to_fit(draw, text, font, max_width)
        ascent, descent = font.getmetrics()
        line_h = int((ascent + descent) * LINE_SPACING)
        best = (font, lines, line_h)
    return best


def render_quote(quote, out_path, attribution=None):
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(img)

    quote_font_path = first_existing(QUOTE_FONT_CANDIDATES)
    attr_font_path = first_existing(ATTRIBUTION_FONT_CANDIDATES)
    if not quote_font_path:
        raise RuntimeError("No suitable serif font found for quote text")

    # Decorative opening quotation mark — large, dim, top-left of the text block
    mark_size = 220
    mark_font = load_font(quote_font_path, mark_size)
    mark_x = SIDE_PAD - 18
    mark_y = TOP_BOTTOM_PAD - 110
    draw.text((mark_x, mark_y), "“", font=mark_font, fill=MARK_COLOR)

    # Reserve room at the bottom for the attribution + breathing space
    attr_reserved = 110 if attribution else 60
    text_box_w = WIDTH - 2 * SIDE_PAD
    text_box_h = HEIGHT - TOP_BOTTOM_PAD - attr_reserved - 40

    font, lines, line_h = fit_quote(
        draw, quote, quote_font_path, text_box_w, text_box_h
    )

    total_h = line_h * len(lines)
    y = (HEIGHT - attr_reserved - total_h) // 2 + 10

    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        w = bbox[2] - bbox[0]
        x = (WIDTH - w) // 2
        draw.text((x, y), line, font=font, fill=FG)
        y += line_h

    if attribution and attr_font_path:
        attr_font = load_font(attr_font_path, 28)
        text = f"— {attribution}"
        bbox = draw.textbbox((0, 0), text, font=attr_font)
        w = bbox[2] - bbox[0]
        x = (WIDTH - w) // 2
        draw.text((x, HEIGHT - 90), text, font=attr_font, fill=DIM)

    img.save(out_path, "PNG", optimize=True)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    work_dir = args[0] if args else os.getcwd()

    attribution = None
    if "--attribution" in sys.argv:
        i = sys.argv.index("--attribution")
        if i + 1 < len(sys.argv):
            attribution = sys.argv[i + 1]

    quotes_path = os.path.join(work_dir, "viral_clips", "quotes.json")
    if not os.path.exists(quotes_path):
        print(f"✗ quotes.json not found: {quotes_path}")
        print("  Run find_moments.py first.")
        sys.exit(1)

    with open(quotes_path) as f:
        quotes = json.load(f)

    if not quotes:
        print("⚠ quotes.json is empty — nothing to render")
        sys.exit(0)

    out_dir = os.path.join(work_dir, "quote_images")
    os.makedirs(out_dir, exist_ok=True)

    print(f"Quotes : {len(quotes)}")
    print(f"Output : {out_dir}")
    if attribution:
        print(f"Attrib : {attribution}")

    for i, quote in enumerate(quotes, start=1):
        slug = f"quote_{i:02d}"
        png_path = os.path.join(out_dir, slug + ".png")
        txt_path = os.path.join(out_dir, slug + ".txt")
        render_quote(quote, png_path, attribution=attribution)
        with open(txt_path, "w") as f:
            f.write(quote + "\n")
        preview = quote if len(quote) <= 80 else quote[:77] + "..."
        print(f"  [{i:02d}] {preview}")

    print(f"\n✓ {len(quotes)} quote images  →  {out_dir}")


if __name__ == "__main__":
    main()
