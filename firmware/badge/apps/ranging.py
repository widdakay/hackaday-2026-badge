"""Ranging app: distances to other badges and ftm_node boards over Wi-Fi FTM.

The first launch starts lib.ranging's background service, which keeps
measuring after you switch apps. The screen lists nearby nodes, nearest first.
It redraws with the fast black/white refresh (a few seconds instead of about
35) whenever what it shows changes, at most every REDRAW_MS, and only after
the previous refresh has finished. While the app is open, the LED bar shows
the nearest node: more LEDs = closer.

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
_TITLE_FONT = lvgl.font_montserrat_40
_ROW_FONT = lvgl.font_montserrat_32
_NOTE_FONT = lvgl.font_montserrat_16
_ROW_H = 44
_PAD = 16
_MAX_ROWS = 11
_COLS = (0, 270, 480, 720)   # x of Node, Distance, Round trip, Signal
REDRAW_MS = 10000           # fast refresh takes ~4-7 s; leave a gap
_SEARCH_STEP_S = 10          # "looking for" elapsed time, in steps of this
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
        self._drawn_content = None
        self._scr = None
        self._search_start_ms = None

    def run(self):
        if self.launched:
            service.start()
            self._search_start_ms = time.ticks_ms()
            self._draw(self._content())
            return
        self._show_leds()
        if not display.is_done_updating():
            return
        if time.ticks_diff(time.ticks_ms(), self._drawn_ms) < REDRAW_MS:
            return
        content = self._content()
        if content != self._drawn_content:
            self._draw(content)

    def _content(self):
        """What the screen shows, as plain data: redraw only when it changes."""
        peers = service.live_peers()
        rows = []
        for p in peers[:_MAX_ROWS]:
            d = service.distance(p.mac)
            # RSSI in 5 dB steps: it jitters by a dB or two with every beacon,
            # which would otherwise redraw the screen every REDRAW_MS.
            rssi = -((-p.rssi + 2) // 5 * 5)
            latest = service.latest(p.mac)
            rtt = "%.1f ns" % latest[0] if latest is not None else ""
            rows.append((p.name, "%.1f m" % d if d is not None else "measuring...", rtt, "%d dBm" % rssi))
        searching_s = None
        if not peers:
            elapsed = time.ticks_diff(time.ticks_ms(), self._search_start_ms) // 1000
            searching_s = elapsed // _SEARCH_STEP_S * _SEARCH_STEP_S
        return (tuple(rows), max(0, len(peers) - _MAX_ROWS), searching_s)

    def _show_leds(self):
        peers = service.live_peers()
        d = service.distance(peers[0].mac) if peers else None
        vals = [(0, 0, 0)] * NUM_LEDS
        if d is not None:
            lit = max(1, NUM_LEDS - int(d / _METRES_PER_LED))
            vals = [_LED_COLOR if i < lit else (0, 0, 0) for i in range(NUM_LEDS)]
        leds.set(vals)
        leds.write()

    def _draw(self, content):
        rows, more, searching_s = content
        leds.fill((0, 1, 1))
        scr = lvgl.obj()
        scr.remove_style_all()
        scr.set_size(display_width, display_height)
        scr.set_style_bg_color(_BG, 0)
        scr.set_style_bg_opa(lvgl.OPA.COVER, 0)

        w = display_width - 2 * _PAD
        _label(scr, _PAD, _PAD, w, "Ranging  " + service.name, _TITLE_FONT)
        y = _PAD + 56
        if searching_s is not None:
            _label(scr, _PAD, y + 40, w, "Looking for other nodes on channel 6...", _ROW_FONT)
            _label(scr, _PAD, y + 40 + _ROW_H, w,
                   "Searching for %d s. Other badges need the Ranging app open," % searching_s, _NOTE_FONT)
            _label(scr, _PAD, y + 40 + _ROW_H + 24, w,
                   "C3 boards need the ftm_node firmware.", _NOTE_FONT)
        else:
            for x, title in zip(_COLS, ("Node", "Distance", "Round trip", "Signal")):
                _label(scr, _PAD + x, y, 300, title, _ROW_FONT)
            y += _ROW_H + 4
            for row in rows:
                for x, text in zip(_COLS, row):
                    _label(scr, _PAD + x, y, 300, text, _ROW_FONT)
                y += _ROW_H
            if more:
                _label(scr, _PAD, y, w, "+ %d more" % more, _ROW_FONT)

        _label(scr, _PAD, display_height - _PAD - 44, w,  # two lines at 16 px
               "Raw Wi-Fi FTM, not calibrated. Distance: median of the last 5. "
               "Round trip: newest reading (1 ns = 15 cm one way).", _NOTE_FONT)

        display.clear()
        lvgl.screen_load(scr)
        if self._scr is not None:
            self._scr.delete()  # one screen per redraw; free the previous one
        self._scr = scr
        scr.update_layout()
        lvgl.refr_now(display.lv_display)
        # Black and white only, so the short waveform loses nothing.
        display.update(fast=True)
        self._drawn_ms = time.ticks_ms()
        self._drawn_content = content
        leds.fill((0, 1, 0))
