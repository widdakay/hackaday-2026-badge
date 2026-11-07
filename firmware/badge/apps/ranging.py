"""Ranging app: distances to other badges and ftm_node boards over Wi-Fi FTM.

The screen has three panes:

  left          every known node with a fixed number (0 = this badge), its
                distance from this badge and signal strength
  right top     the nametag (data/graphics/nametag_small.png, made by
                scripts/make_nametag.py)
  right bottom  a map of node positions fitted to every pairwise distance
                (lib.graph_layout), autoscaled, numbered like the list

The first launch starts lib.ranging's background service, which keeps
measuring after you switch apps. While the app is open the LEDs flash like
fireflies: every ~2 s a wave of light runs along the bar, a different
direction and colours each time, and the badges nearby pull each other into
step, gradually, until they ripple together (lib/firefly.py). Closer badges
pull harder (weight 1 / distance).

A redraw blocks everything, the LEDs included. Measured on the badge: map
layout ~60-120 ms, LVGL render of the three panes ~0.9 s, SPI send to the
panel 0.36 s (0.72 s with a balanced flash). That is more than one dark gap
between two flashes (~1.2 s), so a redraw runs as three steps, one per gap
(_in_gap): layout, render, send. The nametag PNG is decoded once and blitted
from a cache afterwards (decoding it took ~375 ms per redraw). The app also
collects garbage itself in a gap, so an automatic collection (~330 ms) never
lands mid-flash.

Live mode (from the main branch's Ranging): the panel stays powered
(display.keep_powered) and the screen redraws once per full pass over the
peer list, so the whole table and map are from one pass, and at once when a
node appears or drops out. Frames use the differential table
(display.update(differential=True), ~1.2 s of waveform): only pixels that
changed are driven, DC-balanced, so static parts (nametag, labels) get no
charge and don't ghost. Every CLEAN_EVERY frames, and whenever the layout
changes, a balanced flash (inverted frame, then the real one) wipes the light
ghosts the short drive leaves on changed pixels.

Distances are raw FTM readings and not calibrated yet. Expect a fixed offset
of a few metres that depends on the chip types involved.
"""

import asyncio as aio
import framebuf
import gc
import math
import sys
import time

import lvgl

from apps.base_app import BaseApp
from lib import app_bar, graph_layout, log
from lib.epd import display, display_height, display_width
from lib.led_bar import NUM_LEDS, leds
from lib.ranging import CHANNEL, HISTORY, service

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

MIN_GAP_MS = 300            # between the end of one frame and the next
CLEAN_EVERY = 8             # live frames between balanced-flash cleans

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

# LEDs: the firefly flash (lib/firefly.py) while the app is open, dark
# between flashes. Other apps keep the LEDs to themselves.
_DARK = [(0, 0, 0)] * NUM_LEDS
_LED_TICK_MS = 20           # LED redraw interval while the app is open
# Blocking work waits for the dark gap between flashes (>= 1.18 s) and starts
# only with at least this long left before the next flash: the render
# (~0.9 s measured, nametag from the cache)...
_DRAW_BUDGET_MS = 1050
# ...the send: display.update() (~50 ms with the app bar), then the driver's
# split and SPI send, 0.36 s (a balanced flash sends a second frame after a
# 1.2 s waveform, which can still land in the next flash)...
_SEND_BUDGET_MS = 900
# ...and the map layout: ~0.1 s with the firmware's C solver, up to its time
# budget in pure Python, which is cut to what is left of the gap.
_LAYOUT_MIN_DARK_MS = 500
_LAYOUT_MARGIN_MS = 250
# A redraw that has waited this long runs anyway (the gap shrinks while the
# period is being pulled toward the badges nearby).
_DRAW_MAX_WAIT_MS = 6000
_GC_EVERY_MS = 15000        # our own garbage collection, in a gap
_GC_BUDGET_MS = 500


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
        self._drawn_nodes = None    # node set on screen; a change redraws at once
        self._drawn_passes = -1     # service.passes at the last frame
        self._drawn_layout = None   # None = next frame is a plain fast refresh
        self._live_frames = 0
        self._period_ms = self.sleep_ms
        self._pending = False       # a redraw is waiting for a gap between flashes
        self._pending_ms = 0
        self._snap = None           # its snapshot, once the layout step has run
        self._send = None           # display.update() kwargs of a rendered frame
        self._send_ms = 0
        self._nametag = None        # decoded nametag pixels (framebuf), see _paste_nametag
        self._gc_ms = time.ticks_ms()
        self._scr = None
        self._edges = []         # map edges to draw after LVGL, see _draw_edges
        self._pos = {}           # mac -> (x, y) metres from the last layout

    def run(self):
        if self.launched:
            service.start()
            # Keep the booster up between frames: skips ~2.5 s of reset, init and
            # power-up per frame. The driver closes the session after 60 s idle
            # or on a slow (red) refresh from another app.
            display.keep_powered = True
            self._drawn_layout = None  # first frame is always a plain one
            self._pending = False
            self._snap = None
            self._send = None
            self._draw()
            return
        if not display.is_done_updating():
            self._drawn_ms = time.ticks_ms()  # gap counts from the end of a frame
            return
        if time.ticks_diff(time.ticks_ms(), self._drawn_ms) < MIN_GAP_MS:
            return
        # One frame per full pass over the peer list, so every number on screen
        # is from the same pass; at once when the nodes shown change.
        nodes, _ = service.graph()
        if set(nodes) == self._drawn_nodes and service.passes == self._drawn_passes:
            return
        if self._key() != self._drawn_key:
            # Drawn by _in_gap() while the LEDs are dark.
            if not self._pending:
                self._pending = True
                self._pending_ms = time.ticks_ms()
        else:
            self._drawn_passes = service.passes

    # ---- LEDs and the work done between flashes ------------------------------
    async def arun(self):
        """run() every _period_ms as before, and the LEDs every _LED_TICK_MS
        in between. The switcher only awaits this while the app is in front,
        so the LEDs are left alone in the menu and in other apps."""
        t0 = time.ticks_ms()
        self.sleep_ms = 0
        await super().arun()  # run(), with its exceptions caught and logged
        while time.ticks_diff(time.ticks_ms(), t0) < self._period_ms:
            self._render_leds()
            self._in_gap()
            await aio.sleep_ms(_LED_TICK_MS)

    def _render_leds(self):
        if not service.started:
            return
        try:
            leds.set(service.firefly.led_values(NUM_LEDS) or _DARK)
            leds.write()
        except Exception as ex:
            # Must not escape: arun() is awaited by the app switcher itself.
            sys.print_exception(ex)
            log.error("ranging: LED render", ex)

    def _in_gap(self):
        """Do the blocking work while the LEDs are dark, one step per call:
        send a rendered frame, or lay out, or render (each in a gap with
        enough time left before the next flash)."""
        if not service.started:
            return
        try:
            now = time.ticks_ms()
            dark = service.firefly.dark_ms(now)
            if self._send is not None:
                # The framebuffer holds a rendered frame: nothing else may draw
                # until it has gone to the panel.
                if dark >= _SEND_BUDGET_MS or time.ticks_diff(now, self._send_ms) > _DRAW_MAX_WAIT_MS:
                    kwargs, self._send = self._send, None
                    display.update(**kwargs)
                    self._drawn_ms = time.ticks_ms()
                return
            late = self._pending and time.ticks_diff(now, self._pending_ms) > _DRAW_MAX_WAIT_MS
            if self._pending and self._snap is None:
                if dark >= _LAYOUT_MIN_DARK_MS or late:
                    budget = graph_layout.BUDGET_MS if late else max(100, dark - _LAYOUT_MARGIN_MS)
                    self._snap = self._snapshot()
                    self._relayout(self._snap, budget)
            elif self._pending:
                if dark >= _DRAW_BUDGET_MS or late:
                    snap, self._snap, self._pending = self._snap, None, False
                    self._render(snap, send=False)
            elif dark >= _GC_BUDGET_MS and time.ticks_diff(now, self._gc_ms) > _GC_EVERY_MS:
                self._gc_ms = now
                gc.collect()
        except Exception as ex:
            self._pending, self._snap, self._send = False, None, None
            sys.print_exception(ex)
            log.error("ranging: redraw in gap", ex)

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
            # which would otherwise force a redraw on every pass.
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
        """Layout and render at once (first frame after launch)."""
        snap = self._snapshot()
        self._relayout(snap, graph_layout.BUDGET_MS)
        self._render(snap)

    def _relayout(self, snap, budget_ms):
        nodes, edges, _, _ = snap
        t0 = time.ticks_ms()
        self._pos = graph_layout.layout(nodes, edges, self._pos, budget_ms=budget_ms)
        print("ranging: layout of %d nodes, %d edges took %d ms" % (
            len(nodes), len(edges), time.ticks_diff(time.ticks_ms(), t0)))

    def _render(self, snap, send=True):
        """Draw the frame into the framebuffer. send=False leaves the panel
        update to _in_gap() (self._send), in a later gap."""
        nodes, edges, rows, key = snap
        scr = lvgl.obj()
        scr.remove_style_all()
        scr.set_size(display_width, display_height)
        scr.set_style_bg_color(_BG, 0)
        scr.set_style_bg_opa(lvgl.OPA.COVER, 0)
        self._edges = []

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
        self._draw_edges(display.black)
        # The nametag goes on after LVGL, which paints its whole screen.
        self._paste_nametag()
        # Black and white only, so the short waveforms lose nothing.
        layout = (len(nodes), len(rows) > 1)
        if self._drawn_layout is None:
            # First frame after launch: a normal fast refresh (slow once if red
            # is on the panel) so the driver knows what the panel shows.
            kwargs = {"fast": True}
            self._live_frames = 0
        elif layout != self._drawn_layout or self._live_frames >= CLEAN_EVERY:
            kwargs = {"fast": True, "differential": True, "flash": True}
            self._live_frames = 0
        else:
            kwargs = {"fast": True, "differential": True}
            self._live_frames += 1
        if send:
            display.update(**kwargs)
        else:
            self._send = kwargs
            self._send_ms = time.ticks_ms()
        self._drawn_layout = layout
        self._drawn_ms = time.ticks_ms()
        self._drawn_key = key
        self._drawn_nodes = set(nodes)
        self._drawn_passes = service.passes

    def _paste_nametag(self):
        """Put the nametag in the top right pane. Decoding the PNG takes ~375
        ms, so it is decoded once and its pixels kept in a small framebuffer
        (black/white, like the image), then blitted on later redraws."""
        w, h = display_width - _RIGHT_X, _SPLIT_Y - _TOP
        if self._nametag is not None:
            display.black.blit(self._nametag, _RIGHT_X, _TOP)
            return
        try:
            # Small image: no forced collections, they would overrun the gap.
            display.draw_png(_NAMETAG, _TOP, _RIGHT_X, collect=False)
        except OSError:
            print("ranging: %s missing, run scripts/make_nametag.py" % _NAMETAG)
            return
        buf = bytearray((w + 7) // 8 * h)
        fb = framebuf.FrameBuffer(buf, w, h, framebuf.MONO_HLSB)
        fb.blit(display.black, -_RIGHT_X, -_TOP)
        self._nametag = fb

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

        _label(scr, _PAD, display_height - 50, self._timing(), _NOTE_FONT)
        _label(scr, _PAD, display_height - 28, "Numbers match the circles in the map.", _NOTE_FONT)

    def _draw_edges(self, fb):
        """Edges as 3 px lines in the framebuffer, broken wherever they would
        cross a node circle or their own distance label."""
        r = _GRAPH_R + 2
        for ax, ay, bx, by, box, centres in self._edges:
            dx, dy = bx - ax, by - ay
            length2 = dx * dx + dy * dy
            if length2 == 0:
                continue
            holes = []
            for cx, cy in centres:          # t-ranges inside each circle
                fx, fy = ax - cx, ay - cy
                b = fx * dx + fy * dy
                c = fx * fx + fy * fy - r * r
                disc = b * b - length2 * c
                if disc > 0:
                    sq = math.sqrt(disc)
                    holes.append(((-b - sq) / length2, (-b + sq) / length2))
            if box is not None:             # Liang-Barsky against the label
                t0, t1 = 0.0, 1.0
                for p, q in ((-dx, ax - box[0]), (dx, box[2] - ax),
                             (-dy, ay - box[1]), (dy, box[3] - ay)):
                    if p == 0:
                        if q < 0:
                            t0, t1 = 1.0, 0.0
                    else:
                        t = q / p
                        if p < 0:
                            t0 = max(t0, t)
                        else:
                            t1 = min(t1, t)
                if t0 < t1:
                    holes.append((t0, t1))
            holes.sort()
            t = 0.0
            spans = []
            for h0, h1 in holes:
                if h0 > t:
                    spans.append((t, min(h0, 1.0)))
                t = max(t, h1)
            if t < 1.0:
                spans.append((t, 1.0))
            horizontal = abs(dx) >= abs(dy)
            for s0, s1 in spans:
                if s1 - s0 <= 0:
                    continue
                x0, y0 = int(ax + dx * s0), int(ay + dy * s0)
                x1, y1 = int(ax + dx * s1), int(ay + dy * s1)
                for o in (-1, 0, 1):        # 3 px thick
                    if horizontal:
                        fb.line(x0, y0 + o, x1, y1 + o, 1)
                    else:
                        fb.line(x0 + o, y0, x1 + o, y1, 1)

    def _timing(self):
        """Footer: how long the median window is (a median of 3 means little
        without knowing whether 3 readings took 8 s or a minute). Pass time in
        0.5 s steps, so its jitter does not force a redraw."""
        if service.pass_ms is None or not service.pass_peers:
            return "Raw FTM, uncalibrated. Median of last %d passes." % HISTORY
        pass_s = max(0.5, round(service.pass_ms / 500) / 2)
        return "Raw FTM, uncalibrated. Median of %d passes, ~%d s." % (
            HISTORY, round(pass_s * HISTORY))

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

        # Circles and edge labels are LVGL widgets; the edges themselves are
        # drawn into the framebuffer afterwards (_draw_edges), around them.
        # (LVGL line widgets keep a pointer to their point array, and the
        # MicroPython binding's converted array isn't kept alive: after a GC
        # the badge drew garbage lines and hung.)
        centres = [px(m) for m in placed]
        for (a, b), d in edges.items():
            if a not in pos or b not in pos:
                continue
            (ax, ay), (bx, by) = px(a), px(b)
            box = None
            if math.sqrt((ax - bx) ** 2 + (ay - by) ** 2) >= 2 * _GRAPH_R + 36:
                lbl = _label(scr, 0, 0, _fmt_m(d), _NOTE_FONT)
                lbl.set_style_bg_color(_BG, 0)
                lbl.set_style_bg_opa(lvgl.OPA.COVER, 0)
                lbl.set_style_pad_hor(3, 0)
                lbl.update_layout()
                w, h = lbl.get_width(), lbl.get_height()
                lx, ly = (ax + bx) // 2 - w // 2, (ay + by) // 2 - h // 2
                lbl.set_pos(lx, ly)
                box = (lx, ly, lx + w, ly + h)
            self._edges.append((ax, ay, bx, by, box, centres))
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
