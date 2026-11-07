"""Ranging app: distances to other badges and ftm_node boards over Wi-Fi FTM.

The first launch starts lib.ranging's background service, which keeps
measuring after you switch apps. The screen lists nearby nodes, nearest first,
and redraws at most every REDRAW_MS when something changed (e-paper is slow).
While the app is open, the LED bar shows the nearest node: more LEDs = closer.

Distances are raw FTM readings and not calibrated yet. Expect a fixed offset
of a few metres that depends on the chip types involved.
"""

import time

import lvgl

from apps.base_app import BaseApp
from lib.epd import display, display_height, display_width
from lib.led_bar import NUM_LEDS, leds
from lib.ranging import service

_BG = lvgl.color_hex(0xFFFFFF)
_FG = lvgl.color_hex(0x000000)
_RED = lvgl.color_hex(0xFF0000)
_TITLE_FONT = lvgl.font_montserrat_40
_ROW_FONT = lvgl.font_montserrat_32
_NOTE_FONT = lvgl.font_montserrat_16
_ROW_H = 44
_PAD = 16
_MAX_ROWS = 11
REDRAW_MS = 30000
_LED_COLOR = (0, 2, 0)
_METRES_PER_LED = 1.0


def _label(parent, x, y, w, text, font, color=_FG):
    lbl = lvgl.label(parent)
    lbl.remove_style_all()
    lbl.set_pos(x, y)
    lbl.set_width(w)
    lbl.set_style_text_font(font, 0)
    lbl.set_style_text_color(color, 0)
    lbl.set_text(text)
    return lbl


class RangingApp(BaseApp):
    def __init__(self, name: str | None = None) -> None:
        super().__init__("Ranging")
        self.sleep_ms = 500
        self._drawn_ms = time.ticks_ms()
        self._drawn_version = None
        self._scr = None

    def run(self):
        if self.launched:
            service.start()
            self._draw()
            return
        self._show_leds()
        now = time.ticks_ms()
        if (service.updated_ms != self._drawn_version
                and time.ticks_diff(now, self._drawn_ms) >= REDRAW_MS):
            self._draw()

    def _show_leds(self):
        peers = service.live_peers()
        d = service.distance(peers[0].mac) if peers else None
        vals = [(0, 0, 0)] * NUM_LEDS
        if d is not None:
            lit = max(1, NUM_LEDS - int(d / _METRES_PER_LED))
            vals = [_LED_COLOR if i < lit else (0, 0, 0) for i in range(NUM_LEDS)]
        leds.set(vals)
        leds.write()

    def _draw(self):
        leds.fill((0, 1, 1))
        scr = lvgl.obj()
        scr.remove_style_all()
        scr.set_size(display_width, display_height)
        scr.set_style_bg_color(_BG, 0)
        scr.set_style_bg_opa(lvgl.OPA.COVER, 0)

        w = display_width - 2 * _PAD
        _label(scr, _PAD, _PAD, w, "Ranging  " + service.name, _TITLE_FONT, _RED)
        y = _PAD + 56
        _label(scr, _PAD, y, 400, "Node", _ROW_FONT)
        _label(scr, _PAD + 420, y, 220, "Distance", _ROW_FONT)
        _label(scr, _PAD + 680, y, 240, "Signal", _ROW_FONT)
        y += _ROW_H + 4

        peers = service.live_peers()
        if not peers:
            _label(scr, _PAD, y, w, "Looking for other nodes on channel 6...", _ROW_FONT)
        for p in peers[:_MAX_ROWS]:
            d = service.distance(p.mac)
            _label(scr, _PAD, y, 400, p.name, _ROW_FONT)
            _label(scr, _PAD + 420, y, 220, "%.1f m" % d if d is not None else "...", _ROW_FONT)
            _label(scr, _PAD + 680, y, 240, "%d dBm" % p.rssi, _ROW_FONT)
            y += _ROW_H
        if len(peers) > _MAX_ROWS:
            _label(scr, _PAD, y, w, "+ %d more" % (len(peers) - _MAX_ROWS), _ROW_FONT)

        _label(scr, _PAD, display_height - _PAD - 22, w,
               "Raw Wi-Fi FTM readings, not calibrated. Median of the last 9.", _NOTE_FONT)

        display.clear()
        lvgl.screen_load(scr)
        if self._scr is not None:
            self._scr.delete()  # one screen per redraw; free the previous one
        self._scr = scr
        scr.update_layout()
        lvgl.refr_now(display.lv_display)
        display.update()
        self._drawn_ms = time.ticks_ms()
        self._drawn_version = service.updated_ms
        leds.fill((0, 1, 0))
