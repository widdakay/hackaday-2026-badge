#!/usr/bin/env python3
"""Build the assets the badge uses to draw your name on its nametags.

The badge's LVGL firmware only ships Montserrat, so this renders Audiowide
glyph sheets on the host once. The badge then draws whatever name is in
data/settings.json at boot (lib/name_render.py), so changing the name needs
no firmware rebuild and no rerun of this script: edit the file and reset.

Writes, from firmware/:

  badge/data/fonts/audiowide_240.bbf      glyphs for the full-screen Nametag
  badge/data/fonts/audiowide_120.bbf      glyphs for the Ranging app's pane
  badge/data/graphics/nametag_small_blank.png
                                          478 x 258 black/white "HELLO / I am..."
                                          template for the Ranging pane

    .venv/bin/python scripts/make_nametag.py

.bbf ("badge bitmap font"), little-endian:
  header  b"BBFN", u16 font size (px), u16 glyph count
  entries count x <HhhhhHI>: code point, bbox x0, y0, x1, y1 relative to the
          pen position on the baseline (y down), advance in 1/16 px, offset of
          the bitmap from the start of the file
  bitmaps 1 bit per pixel, rows of ceil(w / 8) bytes, MSB = leftmost pixel
"""

import os
import struct

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
BADGE = os.path.join(HERE, "..", "badge")
FONTS_OUT = os.path.join(BADGE, "data", "fonts")
GRAPHICS = os.path.join(BADGE, "data", "graphics")
NAME_FONT = os.path.join(HERE, "fonts", "Audiowide-Regular.ttf")
LABEL_FONT = os.path.join(HERE, "fonts", "Montserrat-wght.ttf")

SIZES = (240, 120)
CHARS = [chr(c) for c in range(32, 127)]

# Top of the screen is covered by lib/app_bar.py's bar on every app.
APP_BAR_H = 40
# Ranging app: the nametag pane is right of the 4 px divider at x 478-481,
# between the app bar and the divider at y 298-301.
SMALL_SIZE = (478, 298 - APP_BAR_H)
SMALL_HEADER, SMALL_FOOTER = 80, 22   # keep in sync with lib/name_render.py

_ENTRY = "<HhhhhHI"


def build_font(size, path):
    font = ImageFont.truetype(NAME_FONT, size)
    glyphs = []
    for ch in CHARS:
        x0, y0, x1, y1 = font.getbbox(ch, anchor="ls")
        adv = round(font.getlength(ch) * 16)
        w, h = max(0, x1 - x0), max(0, y1 - y0)
        data = b""
        if w and h:
            mask = Image.new("L", (w, h), 0)
            ImageDraw.Draw(mask).text((-x0, -y0), ch, font=font, fill=255, anchor="ls")
            bits = mask.point(lambda v: 255 if v >= 128 else 0).convert("1")
            data = bits.tobytes()   # PIL "1": rows padded to whole bytes, MSB first
        glyphs.append((ord(ch), x0, y0, x1, y1, adv, data))
    header = struct.pack("<4sHH", b"BBFN", size, len(glyphs))
    offset = len(header) + len(glyphs) * struct.calcsize(_ENTRY)
    entries, blobs = [], []
    for code, x0, y0, x1, y1, adv, data in glyphs:
        entries.append(struct.pack(_ENTRY, code, x0, y0, x1, y1, adv, offset))
        blobs.append(data)
        offset += len(data)
    with open(path, "wb") as f:
        f.write(header + b"".join(entries) + b"".join(blobs))
    return os.path.getsize(path)


def label_font(size, weight):
    font = ImageFont.truetype(LABEL_FONT, size)
    font.set_variation_by_axes([weight])
    return font


def draw_centered(out, text, font, cx, top, bottom, ink):
    left, t, right, b = font.getbbox(text)
    x = cx - (right - left) // 2 - left
    y = top + ((bottom - top) - (b - t)) // 2 - t
    mask = Image.new("L", out.size, 0)
    ImageDraw.Draw(mask).text((x, y), text, font=font, fill=255)
    out.paste(ink, mask=mask.point(lambda v: 255 if v >= 128 else 0))


def make_small_blank(path):
    w, h = SMALL_SIZE
    out = Image.new("P", SMALL_SIZE, 1)
    out.putpalette([0, 0, 0, 255, 255, 255])
    d = ImageDraw.Draw(out)
    d.rectangle([0, 0, w - 1, SMALL_HEADER - 1], fill=0)
    d.rectangle([0, h - SMALL_FOOTER, w - 1, h - 1], fill=0)
    draw_centered(out, "HELLO", label_font(36, 700), w // 2, 4, 50, 1)
    draw_centered(out, "I am...", label_font(20, 600), w // 2, 48, 76, 1)
    out.save(path, optimize=True, interlace=False, bits=1)


def main():
    os.makedirs(FONTS_OUT, exist_ok=True)
    for size in SIZES:
        path = os.path.join(FONTS_OUT, "audiowide_%d.bbf" % size)
        print("Wrote %s (%d KB)" % (os.path.relpath(path), build_font(size, path) // 1024))
    blank = os.path.join(GRAPHICS, "nametag_small_blank.png")
    make_small_blank(blank)
    print("Wrote %s" % os.path.relpath(blank))


if __name__ == "__main__":
    main()
