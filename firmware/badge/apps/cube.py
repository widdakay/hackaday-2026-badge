"""Cube: a rotating 3D wireframe cube, redrawn as fast as the e-paper allows.

Each frame is a windowed differential refresh (display.update(window=...)):
the panel only scans the band of rows the cube lives in, and only pixels that
changed are driven. The next frame is drawn while the panel is still running
the waveform of the current one, so frames follow each other back to back.

User A / User B: spin slower / faster.
"""

import asyncio as aio
import math
import time

from apps.base_app import BaseApp
from lib.button import UserA, UserB
from lib.epd import display, display_width, display_height

try:
    from lib import app_bar
    _BAR_H = app_bar.HEIGHT
except ImportError:
    _BAR_H = 0

# The cube's square, centred below the app bar. Rows are multiples of 4 (the
# panel's refresh window counts 4-row groups).
SIZE = 240
_Y0 = (_BAR_H + (display_height - _BAR_H - SIZE) // 2) // 4 * 4
_Y1 = _Y0 + SIZE
_X0 = (display_width - SIZE) // 2
_CX = _X0 + SIZE // 2
_CY = _Y0 + SIZE // 2
# Cube half-edge in pixels; its diagonal (half * sqrt(3)) plus perspective
# must stay inside the square.
_HALF = 62
_DIST = 4.0      # camera distance, in half-edges
_FOCAL = _HALF * _DIST * 0.95

_VERTS = [(x, y, z) for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)]
_EDGES = [(a, b) for a in range(8) for b in range(a + 1, 8)
          if bin(a ^ b).count("1") == 1]
# Line thickness: draw each edge at these pixel offsets.
_THICK = ((0, 0), (1, 0), (0, 1), (1, 1))

_SPEEDS = (0.05, 0.1, 0.15, 0.25, 0.35, 0.5)


class CubeApp(BaseApp):
    def __init__(self, name: str | None = None) -> None:
        super().__init__("Cube")
        self.sleep_ms = 0
        self._angle = 0.0
        self._speed_idx = 2
        self._frames = 0
        self._t0 = time.ticks_ms()
        self._fps = 0.0
        self._last_ms = time.ticks_ms()

    async def arun(self):
        if self.launched:
            self._start()
            await aio.sleep_ms(0)
            return
        if UserA.pressed():
            self._speed_idx = max(0, self._speed_idx - 1)
        if UserB.pressed():
            self._speed_idx = min(len(_SPEEDS) - 1, self._speed_idx + 1)
        # The framebuffer is free once the display has taken the queued frame
        # (it copies it before the waveform starts): draw the next one now.
        if display._framebuffer_ready.is_set():
            await aio.sleep_ms(2)
            return
        self._tick_fps()
        self._draw()
        display.update(fast=True, differential=True, window=(_Y0, _Y1))
        await aio.sleep_ms(0)

    def _start(self):
        # Booster stays up between frames: no reset / init / soft start each time.
        display.keep_powered = True
        display.clear()
        fb = display.black
        fb.text("3D wireframe cube - windowed differential refresh", 16, _BAR_H + 12, 1)
        fb.text("User A: slower   User B: faster", 16, _BAR_H + 28, 1)
        fb.rect(_X0 - 2, _Y0 - 2, SIZE + 4, SIZE + 4, 1)  # frame, outside the band
        self._draw()
        # First frame covers the whole panel, so the previous app is wiped and
        # the driver knows what is on the panel for the differential frames.
        display.update(fast=True, differential=True)
        self._frames = 0
        self._t0 = self._last_ms = time.ticks_ms()

    def _tick_fps(self):
        now = time.ticks_ms()
        self._frames += 1
        dt = time.ticks_diff(now, self._t0)
        if dt >= 3000:
            self._fps = self._frames * 1000 / dt
            print("cube: %.2f fps" % self._fps)
            self._frames = 0
            self._t0 = now
        self._last_ms = now

    def _draw(self):
        fb = display.black
        fb.fill_rect(_X0, _Y0, SIZE, SIZE, 0)
        display.red.fill_rect(_X0, _Y0, SIZE, SIZE, 0)

        self._angle += _SPEEDS[self._speed_idx]
        a = self._angle
        ca, sa = math.cos(a), math.sin(a)
        cb, sb = math.cos(a * 0.7), math.sin(a * 0.7)
        cc, sc = math.cos(a * 0.4), math.sin(a * 0.4)

        pts = []
        for x, y, z in _VERTS:
            # Rotate about Y, then X, then Z.
            x, z = x * ca + z * sa, z * ca - x * sa
            y, z = y * cb - z * sb, y * sb + z * cb
            x, y = x * cc - y * sc, x * sc + y * cc
            k = _FOCAL / (_DIST + z)
            pts.append((int(_CX + x * k), int(_CY + y * k)))

        line = fb.line
        for a_i, b_i in _EDGES:
            x1, y1 = pts[a_i]
            x2, y2 = pts[b_i]
            for dx, dy in _THICK:
                line(x1 + dx, y1 + dy, x2 + dx, y2 + dy, 1)

        if self._fps:
            fb.text("%.1f fps" % self._fps, _X0 + 6, _Y0 + SIZE - 12, 1)
