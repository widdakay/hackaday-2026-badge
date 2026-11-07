"""Draw the badge's name in Audiowide, straight into the display framebuffer.

The glyphs come from pre-rendered sheets in /data/fonts (made once by
scripts/make_nametag.py), and the name from data/settings.json, so a new
name needs no firmware rebuild and no host tools: edit the file and reset.

    from lib import name_render
    name_render.draw_name(display, left, top, right, bottom, size=240)

The name is centred in the box and scaled down (never up) to fit.
"""

import struct

import micropython

from lib import settings

_ENTRY = "<HhhhhHI"
_ENTRY_LEN = struct.calcsize(_ENTRY)
_FONT_PATH = "/data/fonts/audiowide_%d.bbf"
_SCREEN_W = 960
_SCREEN_H = 672
_SCALE_DEN = 256

_fonts = {}


class BitmapFont:
    """A .bbf glyph sheet: the index is kept in memory, bitmaps are read from
    the file only for the characters drawn."""

    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            magic, self.size, count = struct.unpack("<4sHH", f.read(8))
            if magic != b"BBFN":
                raise ValueError("not a .bbf font: " + path)
            raw = f.read(count * _ENTRY_LEN)
        self.glyphs = {}
        for i in range(count):
            code, x0, y0, x1, y1, adv, off = struct.unpack_from(_ENTRY, raw, i * _ENTRY_LEN)
            self.glyphs[code] = (x0, y0, x1, y1, adv, off)

    def _glyph(self, ch):
        return self.glyphs.get(ord(ch)) or self.glyphs.get(ord("?"))

    def bbox(self, text):
        """(x0, y0, x1, y1) of the inked area relative to the pen start on the
        baseline, at full size."""
        pen = 0
        bx0 = by0 = 1 << 30
        bx1 = by1 = -(1 << 30)
        for ch in text:
            g = self._glyph(ch)
            if g is None:
                continue
            x0, y0, x1, y1, adv, _ = g
            if x1 > x0 and y1 > y0:
                px = pen >> 4
                bx0, by0 = min(bx0, px + x0), min(by0, y0)
                bx1, by1 = max(bx1, px + x1), max(by1, y1)
            pen += adv
        if bx1 < bx0:
            return 0, 0, 0, 0
        return bx0, by0, bx1, by1

    def draw(self, fb, text, x, y, num, den=_SCALE_DEN):
        """Draw text with its pen starting at (x, baseline y), scaled num/den,
        OR-ing ink into the 960 x 672 1-bpp framebuffer bytearray `fb`."""
        pen = 0
        with open(self.path, "rb") as f:
            for ch in text:
                g = self._glyph(ch)
                if g is None:
                    continue
                x0, y0, x1, y1, adv, off = g
                w, h = x1 - x0, y1 - y0
                if w > 0 and h > 0:
                    stride = (w + 7) // 8
                    f.seek(off)
                    bits = f.read(stride * h)
                    gx = x + ((pen >> 4) + x0) * num // den
                    gy = y + y0 * num // den
                    _blit(bits, w, h, stride, fb, gx, gy, num, den)
                pen += adv


@micropython.viper
def _blit(src: ptr8, w: int, h: int, stride: int, dst: ptr8, dx: int, dy: int, num: int, den: int):
    ow = w * num // den
    oh = h * num // den
    oy = 0
    while oy < oh:
        y = dy + oy
        if 0 <= y < 672:
            srow = (oy * den // num) * stride
            drow = y * 960
            ox = 0
            while ox < ow:
                xx = dx + ox
                if 0 <= xx < 960:
                    sx = ox * den // num
                    if src[srow + (sx >> 3)] & (128 >> (sx & 7)):
                        i = drow + xx
                        dst[i >> 3] = dst[i >> 3] | (128 >> (i & 7))
                ox += 1
        oy += 1


def font(size):
    f = _fonts.get(size)
    if f is None:
        f = _fonts[size] = BitmapFont(_FONT_PATH % size)
    return f


def draw_name(display, left, top, right, bottom, size=240, name=None):
    """Draw the badge name (settings.json, or `name`) centred in the box."""
    name = name or settings.badge_name(None) or settings.default_name()
    fnt = font(size)
    x0, y0, x1, y1 = fnt.bbox(name)
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return
    num = min(_SCALE_DEN, (right - left) * _SCALE_DEN // w, (bottom - top) * _SCALE_DEN // h)
    sw, sh = w * num // _SCALE_DEN, h * num // _SCALE_DEN
    x = left + ((right - left) - sw) // 2 - x0 * num // _SCALE_DEN
    y = top + ((bottom - top) - sh) // 2 - y0 * num // _SCALE_DEN
    fnt.draw(display._framebuffer_black, name, x, y, num)
