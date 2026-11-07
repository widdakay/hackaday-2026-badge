"""Ranging app: distances to other badges and ftm_node boards over Wi-Fi FTM.

The first launch starts lib.ranging's background service, which keeps
measuring after you switch apps. The screen lists nearby nodes, nearest first.
Live mode: the panel stays powered (display.keep_powered) and the screen
redraws once per full pass over the peer list, i.e. after every node has
been ranged once, so the whole table is from one pass (and at once when a
node appears or drops out). The fast refresh is barely noticeable, so there
is no need to hold it back further. Frames use the differential table
(display.update(differential=True), ~1.2 s of waveform): only pixels that changed are driven, DC-balanced, so the static
parts of the screen get no charge and do not ghost. Every CLEAN_EVERY frames,
and whenever the layout changes, a balanced flash (inverted frame, then the
real one) wipes the light ghosts the short drive leaves on changed pixels.
While the app is open, the LED bar shows the nearest node: more LEDs = closer.

The footer gives the median context: how long one pass takes and so how
many seconds the median of the last HISTORY passes covers, measured live,
since it grows with the number of nodes nearby.

Distances are raw FTM readings and not calibrated yet. Expect a fixed offset
of a few metres that depends on the chip types involved.
"""

import time

import lvgl

from apps.base_app import BaseApp
from lib.epd import display, display_height, display_width
from lib.led_bar import NUM_LEDS, leds
from lib.ranging import HISTORY, service

_BG = lvgl.color_hex(0xFFFFFF)
_FG = lvgl.color_hex(0x000000)
_TITLE_FONT = lvgl.font_montserrat_40
_ROW_FONT = lvgl.font_montserrat_32
_NOTE_FONT = lvgl.font_montserrat_16
_ROW_H = 44
_PAD = 16
_MAX_ROWS = 11
_COLS = (0, 270, 480, 720)   # x of Node, Distance, Round trip, Signal
MIN_GAP_MS = 300             # between the end of one frame and the next
CLEAN_EVERY = 8              # live frames between full 3-pass cleans
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
        self._drawn_layout = None
        self._live_frames = 0
        self._drawn_passes = -1

    def run(self):
        if self.launched:
            service.start()
            # Keep the booster up between frames: skips ~2.5 s of reset, init and
            # power-up per frame. The driver closes the session after 60 s idle
            # or on a slow (red) refresh from another app.
            display.keep_powered = True
            self._search_start_ms = time.ticks_ms()
            self._drawn_layout = None  # first frame is always a clean one
            self._draw(self._content())
            return
        self._show_leds()
        if not display.is_done_updating():
            self._drawn_ms = time.ticks_ms()  # gap counts from the end of a frame
            return
        if time.ticks_diff(time.ticks_ms(), self._drawn_ms) < MIN_GAP_MS:
            return
        content = self._content()
        if content == self._drawn_content:
            return
        # One frame per full pass over the peer list, so every row on screen
        # is from the same pass. Redraw at once when the nodes listed change
        # (one appears or drops out) or while searching.
        rows, more, searching_s, _ = content
        # Sorted: rows are nearest first, and a swap in order is not a new node.
        nodes = (sorted(r[0] for r in rows), more, searching_s)
        drawn = self._drawn_content
        drawn_nodes = (sorted(r[0] for r in drawn[0]), drawn[1], drawn[2]) if drawn else None
        if nodes == drawn_nodes and service.passes == self._drawn_passes:
            return
        self._draw(content)

    def _content(self):
        """What the screen shows, as plain data: redraw only when it changes."""
        peers = service.live_peers()
        rows = []
        for p in peers[:_MAX_ROWS]:
            d = service.distance(p.mac)
            # RSSI in 5 dB steps: it jitters by a dB or two with every beacon,
            # which would otherwise force a redraw on every beacon.
            rssi = -((-p.rssi + 2) // 5 * 5)
            latest = service.latest(p.mac)
            rtt = "%.1f ns" % latest[0] if latest is not None else ""
            rows.append((p.name, "%.1f m" % d if d is not None else "measuring...", rtt, "%d dBm" % rssi))
        searching_s = None
        if not peers:
            elapsed = time.ticks_diff(time.ticks_ms(), self._search_start_ms) // 1000
            searching_s = elapsed // _SEARCH_STEP_S * _SEARCH_STEP_S
        return (tuple(rows), max(0, len(peers) - _MAX_ROWS), searching_s, self._timing())

    def _timing(self):
        """Footer line on how long the median window is: a median of 3 means
        little without knowing whether 3 readings took 8 s or a minute.
        Pass time in 0.5 s steps, so its jitter does not force a redraw."""
        if service.pass_ms is None or not service.pass_peers:
            return "Distance: median of the last %d passes (timing the first pass...)." % HISTORY
        pass_s = max(0.5, round(service.pass_ms / 500) / 2)
        n = service.pass_peers
        return "Distance: median of the last %d passes. A pass over %d node%s takes %.1f s, so the median spans ~%d s." % (
            HISTORY, n, "" if n == 1 else "s", pass_s, round(pass_s * HISTORY))

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
        rows, more, searching_s, timing = content
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
               timing + "\nRaw Wi-Fi FTM, not calibrated. "
               "Round trip: newest reading (1 ns = 15 cm one way).", _NOTE_FONT)

        display.clear()
        lvgl.screen_load(scr)
        if self._scr is not None:
            self._scr.delete()  # one screen per redraw; free the previous one
        self._scr = scr
        scr.update_layout()
        lvgl.refr_now(display.lv_display)
        # Black and white only, so the short waveforms lose nothing.
        layout = (searching_s is not None, len(rows), more)
        if self._drawn_layout is None:
            # First frame after launch: a normal fast refresh (slow once if red
            # is on the panel) so the driver knows what the panel shows.
            display.update(fast=True)
            self._live_frames = 0
        elif layout != self._drawn_layout or self._live_frames >= CLEAN_EVERY:
            display.update(fast=True, differential=True, flash=True)
            self._live_frames = 0
        else:
            display.update(fast=True, differential=True)
            self._live_frames += 1
        self._drawn_layout = layout
        self._drawn_ms = time.ticks_ms()
        self._drawn_content = content
        self._drawn_passes = service.passes
        leds.fill((0, 1, 0))
