#!/usr/bin/env python3
"""Render the nametag images with the name in Audiowide.

The badge's LVGL firmware only ships Montserrat, so the name is drawn here on
the host and saved as palette PNGs that lib/large_png.py can show directly:

  badge/data/graphics/nametag.png        960 x 672, red/white/black, for the
                                         Nametag app ("Hello I am..." template)
  badge/data/graphics/nametag_small.png  478 x 298, black/white, for the top
                                         right of the Ranging app (it uses the
                                         black/white fast refresh, so no red)

The name defaults to "name" in badge/data/settings.json, which is also what
the ranging service broadcasts. Run from firmware/:

    .venv/bin/python scripts/make_nametag.py            # name from settings
    .venv/bin/python scripts/make_nametag.py "Someone"  # override
"""

import argparse
import json
import os

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
BADGE = os.path.join(HERE, "..", "badge")
GRAPHICS = os.path.join(BADGE, "data", "graphics")
SETTINGS = os.path.join(BADGE, "data", "settings.json")
TEMPLATE = os.path.join(GRAPHICS, "hello_i_am_blank.png")
NAME_FONT = os.path.join(HERE, "fonts", "Audiowide-Regular.ttf")
LABEL_FONT = os.path.join(HERE, "fonts", "Montserrat-wght.ttf")

# White name band in hello_i_am_blank.png.
BAND_TOP, BAND_BOTTOM = 221, 557

# Top of the screen is covered by lib/app_bar.py's bar on every app.
APP_BAR_H = 40

# Ranging app: the nametag pane is right of the 4 px divider at x 478-481,
# between the app bar and the divider at y 298-301.
SMALL_SIZE = (478, 298 - APP_BAR_H)

BLACK, WHITE, RED = 0, 1, 2
PALETTE_3 = [0, 0, 0, 255, 255, 255, 255, 0, 0]
PALETTE_2 = [0, 0, 0, 255, 255, 255]


def fit_font(path, text, max_w, max_h):
    size = max_h * 2
    while size > 10:
        font = ImageFont.truetype(path, size)
        left, top, right, bottom = font.getbbox(text)
        if right - left <= max_w and bottom - top <= max_h:
            return font
        size -= 2
    raise ValueError("name does not fit")


def label_font(size, weight):
    font = ImageFont.truetype(LABEL_FONT, size)
    font.set_variation_by_axes([weight])
    return font


def draw_centered(out, text, font, cx, top, bottom, ink):
    """Draw text centered in a horizontal band, hard-thresholded so edges stay
    crisp on e-paper (no anti-aliasing grey to land on the wrong side)."""
    left, t, right, b = font.getbbox(text)
    x = cx - (right - left) // 2 - left
    y = top + ((bottom - top) - (b - t)) // 2 - t
    mask = Image.new("L", out.size, 0)
    ImageDraw.Draw(mask).text((x, y), text, font=font, fill=255)
    out.paste(ink, mask=mask.point(lambda v: 255 if v >= 128 else 0))


def make_full(name, path):
    # The template moves down below the app bar; its bottom red band is tall
    # enough to lose that much.
    template = Image.open(TEMPLATE).convert("RGB")
    base = Image.new("RGB", template.size, (255, 255, 255))
    base.paste(template, (0, APP_BAR_H))
    width, height = base.size
    out = Image.new("P", base.size, WHITE)
    out.putpalette(PALETTE_3)
    src, dst = base.load(), out.load()
    for y in range(height):
        for x in range(width):
            r, g, b = src[x, y]
            if r >= 128 and g < 128 and b < 128:
                dst[x, y] = RED
            elif r < 128 and g < 128 and b < 128:
                dst[x, y] = BLACK
    font = fit_font(NAME_FONT, name, width - 120, BAND_BOTTOM - BAND_TOP - 80)
    draw_centered(out, name, font, width // 2, BAND_TOP + APP_BAR_H, BAND_BOTTOM + APP_BAR_H, BLACK)
    out.save(path, optimize=True, interlace=False)
    return font.size


def make_small(name, path):
    w, h = SMALL_SIZE
    out = Image.new("P", SMALL_SIZE, WHITE)
    out.putpalette(PALETTE_2)
    d = ImageDraw.Draw(out)
    header, footer = 80, 22
    d.rectangle([0, 0, w - 1, header - 1], fill=BLACK)
    d.rectangle([0, h - footer, w - 1, h - 1], fill=BLACK)
    draw_centered(out, "HELLO", label_font(36, 700), w // 2, 4, 50, WHITE)
    draw_centered(out, "I am...", label_font(20, 600), w // 2, 48, 76, WHITE)
    font = fit_font(NAME_FONT, name, w - 60, h - header - footer - 30)
    draw_centered(out, name, font, w // 2, header, h - footer, BLACK)
    out.save(path, optimize=True, interlace=False, bits=1)
    return font.size


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("name", nargs="?", help="defaults to name in data/settings.json")
    args = parser.parse_args()
    name = args.name
    if not name:
        with open(SETTINGS) as f:
            name = json.load(f)["name"]

    full = os.path.join(GRAPHICS, "nametag.png")
    small = os.path.join(GRAPHICS, "nametag_small.png")
    print(f"Wrote {os.path.relpath(full)} ({make_full(name, full)} px Audiowide)")
    print(f"Wrote {os.path.relpath(small)} ({make_small(name, small)} px Audiowide)")
    if args.name:
        print("Note: data/settings.json still has the old name; the ranging beacon uses that.")


if __name__ == "__main__":
    main()
