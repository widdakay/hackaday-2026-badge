"""Ranging app: distances to other badges and ftm_node boards over Wi-Fi FTM.

The screen has three panes:

  left          every known node with a fixed number (0 = this badge), its
                distance from this badge and signal strength
  right top     the nametag (data/graphics/nametag_small.png, made by
                scripts/make_nametag.py)
  right bottom  a map of node positions fitted to every pairwise distance
                (lib.graph_layout), autoscaled, numbered like the list

The first launch starts lib.ranging's background service, which keeps
measuring after you switch apps. The screen redraws with the fast black/white
refresh (about 4 s instead of about 35) whenever what it shows changes, at most
every REDRAW_MS, and only after the previous refresh has finished. While the
app is open the LED bar runs a red Cylon sweep.

Distances are raw FTM readings and not calibrated yet. Expect a fixed offset
of a few metres that depends on the chip types involved.
"""

import math
import time

import lvgl

from apps.base_app import BaseApp
from lib import app_bar, graph_layout
from lib.epd import display, display_height, display_width
from lib.led_bar import NUM_LEDS, leds
from lib.ranging import CHANNEL, service

_BG = lvgl.color_hex(0xFFFFFF)
_FG = lvgl.color_hex(0x000000)
# Only these Montserrat sizes are in the firmware (lv_files/.../lv_conf.h):
# 14, 16, 24, 28, 32, 36, 40, 44, 48.
_TITLE_FONT = lvgl.font_montserrat_36
_NAME_FONT = lvgl.font_montserrat_28
_DIST_FONT = lvgl.font_montserrat_24
_SMALL_FONT = lvgl.font_montserrat_16
_NOTE_FONT = lvgl.font_montserrat_14
_LIST_NUM_FONT = lvgl.font_montserrat_16
_GRAPH_NUM_FONT = lvgl.font_montserrat_24

REDRAW_MS = 10000           # fast refresh takes ~4-7 s; leave a gap

_TITLE = "I've found"        # on-screen title; the app itself is still "Ranging"

# Panes, below the app bar. The dividers are 4 px black bars.
_TOP = app_bar.HEIGHT
_SPLIT_X = 478              # vertical divider x 478..481
_SPLIT_Y = 298              # horizontal divider y 298..301 (right side only)
_RIGHT_X = _SPLIT_X + 4
_NAMETAG = "/data/graphics/nametag_small.png"

# Left pane: node list.
_PAD = 20
_COL_NUM, _COL_NAME, _COL_DIST, _COL_SIG = 20, 64, 318, 412
_HEADER_Y = _TOP + 90
_ROWS_Y = _HEADER_Y + 36
_ROW_H = 56
_MAX_ROWS = 7
_NAME_CHARS = 13
_LIST_R = 17

# Right bottom: graph.
_GRAPH_R = 21
_GRAPH_BOX = (_RIGHT_X, _SPLIT_Y + 4, display_width, display_height)
_GRAPH_PAD = _GRAPH_R + 8
_SCALE_BAR_H = 26           # reserved below the plot for the scale bar
_NICE_METRES = (0.1, 0.2, 0.25, 0.5, 1, 2, 5, 10, 20, 50, 100)

# LEDs: red Cylon sweep.
_CYLON_MS = 70
_CYLON_TRAIL = (16, 4, 1)   # head, then fading tail (the LEDs are bright)


def _cylon():
    """LED sequence: a red dot sweeping back and forth with a fading tail."""
    path = list(range(NUM_LEDS)) + list(range(NUM_LEDS - 2, 0, -1))
    step = 0
    while True:
        vals = [(0, 0, 0)] * NUM_LEDS
        for age, level in enumerate(_CYLON_TRAIL):
            idx = path[(step - age) % len(path)]
            if vals[idx][0] < level:
                vals[idx] = (level, 0, 0)
        yield vals, _CYLON_MS
        step = (step + 1) % len(path)


def _label(parent, x, y, text, font, color=_FG, w=None):
    lbl = lvgl.label(parent)
    lbl.remove_style_all()
    lbl.set_pos(x, y)
    if w is not None:
        lbl.set_width(w)
    lbl.set_style_text_font(font, 0)
    lbl.set_style_text_color(color, 0)
    lbl.set_text(text)
    return lbl


def _rect(parent, x, y, w, h, color=_FG):
    r = lvgl.obj(parent)
    r.remove_style_all()
    r.set_pos(x, y)
    r.set_size(w, h)
    r.set_style_bg_color(color, 0)
    r.set_style_bg_opa(lvgl.OPA.COVER, 0)
    return r


def _circle(parent, cx, cy, r, number, font, filled):
    c = lvgl.obj(parent)
    c.remove_style_all()
    c.set_size(2 * r, 2 * r)
    c.set_pos(cx - r, cy - r)
    c.set_style_radius(lvgl.RADIUS_CIRCLE, 0)
    c.set_style_bg_color(_FG if filled else _BG, 0)
    c.set_style_bg_opa(lvgl.OPA.COVER, 0)
    c.set_style_border_width(3, 0)
    c.set_style_border_color(_FG, 0)
    lbl = lvgl.label(c)
    lbl.set_style_text_font(font, 0)
    lbl.set_style_text_color(_BG if filled else _FG, 0)
    lbl.set_text(str(number))
    lbl.center()
    return c


def _short(name):
    return name if len(name) <= _NAME_CHARS else name[:_NAME_CHARS - 2] + ".."


def _fmt_m(d):
    return "%.1f" % d if d < 100 else "%d" % d


class RangingApp(BaseApp):
    def __init__(self, name: str | None = None) -> None:
        super().__init__("Ranging")
        self.sleep_ms = 500
        self._drawn_ms = time.ticks_ms()
        self._drawn_key = None
        self._scr = None
        self._keep = []          # line point arrays LVGL still points at
        self._pos = {}           # mac -> (x, y) metres from the last layout

    def run(self):
        if self.launched:
            service.start()
            leds.unpause()       # the app menu pauses the sequencer
            leds.set_sequence(_cylon)
            self._draw()
            return
        if not display.is_done_updating():
            return
        if time.ticks_diff(time.ticks_ms(), self._drawn_ms) < REDRAW_MS:
            return
        if self._key() != self._drawn_key:
            self._draw()

    # ---- what's on screen ---------------------------------------------------
    def _rows(self, nodes, edges):
        """List rows: (number, name, distance text, signal text).

        This badge first, then nearest first by the distance shown (so equal
        readings keep number order), then nodes without a distance of their
        own ("via N", still measuring). Numbers stay fixed, so a row's circle
        always matches its circle in the map.
        """
        now_peers = {p.mac: p for p in service.live_peers()}
        keyed = []
        for mac in nodes:
            n = service.number_of(mac)
            name = service.name_of(mac)
            if mac == service.mac:
                keyed.append(((0, 0, n), (n, _short(name) + " (me)", "-", "")))
                continue
            d = service.distance(mac)
            if d is None:
                d = service.distance(service.mac, mac)  # it ranged me
            if d is not None:
                dist = _fmt_m(d) + " m"
            else:
                via = [service.number_of(b if a == mac else a)
                       for (a, b) in edges if mac in (a, b)]
                dist = ("via %d" % min(via)) if via else "..."
            p = now_peers.get(mac)
            # RSSI in 5 dB steps: it jitters by a dB or two with every beacon,
            # which would otherwise redraw the screen every REDRAW_MS.
            sig = str(-((-p.rssi + 2) // 5 * 5)) if p is not None and p.rssi else ""
            order = (1, round(d, 1), n) if d is not None else (2, 0, n)
            keyed.append((order, (n, _short(name), dist, sig)))
        keyed.sort(key=lambda item: item[0])
        return [row for _, row in keyed]

    def _snapshot(self):
        """(nodes, edges, rows, key). key is plain data for everything drawn,
        with edges rounded to 10 cm: redraw only when it changes."""
        nodes, edges = service.graph()
        rows = self._rows(nodes, edges)
        rounded = tuple(sorted((service.number_of(a), service.number_of(b), round(d, 1))
                               for (a, b), d in edges.items()))
        return nodes, edges, rows, (tuple(rows), rounded)

    def _key(self):
        return self._snapshot()[3]

    # ---- drawing ------------------------------------------------------------
    def _draw(self):
        nodes, edges, rows, key = self._snapshot()
        t0 = time.ticks_ms()
        self._pos = graph_layout.layout(nodes, edges, self._pos)
        print("ranging: layout of %d nodes, %d edges took %d ms" % (
            len(nodes), len(edges), time.ticks_diff(time.ticks_ms(), t0)))

        scr = lvgl.obj()
        scr.remove_style_all()
        scr.set_size(display_width, display_height)
        scr.set_style_bg_color(_BG, 0)
        scr.set_style_bg_opa(lvgl.OPA.COVER, 0)
        self._keep = []

        _rect(scr, _SPLIT_X, _TOP, 4, display_height - _TOP)
        _rect(scr, _RIGHT_X, _SPLIT_Y, display_width - _RIGHT_X, 4)
        self._draw_list(scr, rows, len(nodes))
        self._draw_graph(scr, nodes, edges)

        display.clear()
        lvgl.screen_load(scr)
        if self._scr is not None:
            self._scr.delete()  # one screen per redraw; free the previous one
        self._scr = scr
        scr.update_layout()
        lvgl.refr_now(display.lv_display)
        # The nametag goes on after LVGL, which paints its whole screen.
        try:
            display.draw_png(_NAMETAG, _TOP, _RIGHT_X)
        except OSError:
            print("ranging: %s missing, run scripts/make_nametag.py" % _NAMETAG)
        # Black and white only, so the short waveform loses nothing.
        display.update(fast=True)
        self._drawn_ms = time.ticks_ms()
        self._drawn_key = key

    def _draw_list(self, scr, rows, n_nodes):
        _label(scr, _PAD, _TOP + 12, _TITLE, _TITLE_FONT)
        _label(scr, _PAD, _TOP + 56, "ch %d  -  %d node%s" % (CHANNEL, n_nodes, "" if n_nodes == 1 else "s"),
               _SMALL_FONT)
        for x, title in ((_COL_NUM, "#"), (_COL_NAME, "Node"), (_COL_DIST, "Dist"), (_COL_SIG, "dBm")):
            _label(scr, x, _HEADER_Y, title, _SMALL_FONT)
        _rect(scr, _PAD, _HEADER_Y + 26, _SPLIT_X - 2 * _PAD, 2)

        y = _ROWS_Y
        for n, name, dist, sig in rows[:_MAX_ROWS]:
            mid = y + _ROW_H // 2
            _circle(scr, _COL_NUM + _LIST_R, mid, _LIST_R, n, _LIST_NUM_FONT, n == 0)
            _label(scr, _COL_NAME, mid - 16, name, _NAME_FONT)
            _label(scr, _COL_DIST, mid - 14, dist, _DIST_FONT)
            _label(scr, _COL_SIG, mid - 10, sig, _SMALL_FONT)
            y += _ROW_H
        if len(rows) > _MAX_ROWS:
            _label(scr, _COL_NAME, y + 8, "+ %d more" % (len(rows) - _MAX_ROWS), _NAME_FONT)
        if len(rows) <= 1:
            _label(scr, _PAD, y + 12, "Looking for other nodes on channel %d..." % CHANNEL, _SMALL_FONT)
            _label(scr, _PAD, y + 40, "Other badges need the Ranging app open,", _NOTE_FONT)
            _label(scr, _PAD, y + 62, "C3 boards need the ftm_node firmware.", _NOTE_FONT)

        _label(scr, _PAD, display_height - 50, "Raw Wi-Fi FTM, not calibrated. Median of last 5.", _NOTE_FONT)
        _label(scr, _PAD, display_height - 28, "Numbers match the circles in the map.", _NOTE_FONT)

    def _draw_graph(self, scr, nodes, edges):
        x0, y0, x1, y1 = _GRAPH_BOX
        pos = self._pos
        placed = [m for m in nodes if m in pos]
        loose = [m for m in nodes if m not in pos]   # no measurements yet

        # Loose nodes sit in a row along the top of the pane.
        lx = x0 + _GRAPH_PAD
        for mac in loose:
            n = service.number_of(mac)
            _circle(scr, lx, y0 + _GRAPH_PAD, _GRAPH_R, n, _GRAPH_NUM_FONT, n == 0)
            lx += 2 * _GRAPH_R + 10
        if not placed:
            _label(scr, x0 + 40, (y0 + y1) // 2 - 10, "Waiting for measurements...", _SMALL_FONT)
            return

        # Fit the layout into the plot area, same scale on both axes, y up.
        top = y0 + _GRAPH_PAD + (2 * _GRAPH_R + 6 if loose else 0)
        left, right = x0 + _GRAPH_PAD, x1 - _GRAPH_PAD
        bottom = y1 - _GRAPH_PAD - _SCALE_BAR_H
        xs = [pos[m][0] for m in placed]
        ys = [pos[m][1] for m in placed]
        span_x, span_y = max(xs) - min(xs), max(ys) - min(ys)
        sx = (right - left) / span_x if span_x > 1e-6 else None
        sy = (bottom - top) / span_y if span_y > 1e-6 else None
        scale = min([s for s in (sx, sy) if s is not None] or [1.0])
        cx_m, cy_m = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
        cx_px, cy_px = (left + right) / 2, (top + bottom) / 2

        def px(mac):
            x, y = pos[mac]
            return int(cx_px + (x - cx_m) * scale), int(cy_px - (y - cy_m) * scale)

        # Draw order is creation order: lines, then edge labels, then circles.
        for (a, b) in edges:
            if a in pos and b in pos:
                (ax, ay), (bx, by) = px(a), px(b)
                pts = [lvgl.point_precise_t({"x": ax, "y": ay}),
                       lvgl.point_precise_t({"x": bx, "y": by})]
                self._keep.append(pts)
                line = lvgl.line(scr)
                line.set_points(pts, 2)
                line.set_pos(0, 0)
                line.set_style_line_width(3, 0)
                line.set_style_line_color(_FG, 0)
        for (a, b), d in edges.items():
            if a in pos and b in pos:
                (ax, ay), (bx, by) = px(a), px(b)
                if math.sqrt((ax - bx) ** 2 + (ay - by) ** 2) < 2 * _GRAPH_R + 36:
                    continue  # no room between the circles for a label
                lbl = _label(scr, 0, 0, _fmt_m(d), _NOTE_FONT)
                lbl.set_style_bg_color(_BG, 0)
                lbl.set_style_bg_opa(lvgl.OPA.COVER, 0)
                lbl.set_style_pad_hor(3, 0)
                lbl.update_layout()
                lbl.set_pos((ax + bx) // 2 - lbl.get_width() // 2, (ay + by) // 2 - lbl.get_height() // 2)
        for mac in placed:
            n = service.number_of(mac)
            x, y = px(mac)
            _circle(scr, x, y, _GRAPH_R, n, _GRAPH_NUM_FONT, n == 0)

        # Scale bar: the largest round length that fits in 130 px.
        metres = None
        for m in _NICE_METRES:
            if m * scale <= 130:
                metres = m
        if metres is not None and metres * scale >= 12:
            bar = int(metres * scale)
            by = y1 - 18
            _rect(scr, left, by, bar, 4)
            _rect(scr, left, by - 8, 3, 12)
            _rect(scr, left + bar - 3, by - 8, 3, 12)
            text = ("%g m" % metres) if metres >= 1 else ("%d cm" % int(metres * 100))
            _label(scr, left + bar + 8, by - 10, text, _NOTE_FONT)
