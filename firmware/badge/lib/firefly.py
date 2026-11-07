"""Firefly LED sync: badges flash together, like a swarm of fireflies.

Each badge runs a phase oscillator with a nominal period of PERIOD_MS and
flashes its LEDs when the phase wraps. Twice a second it broadcasts its
phase (lib.ranging sends and receives the SYNC message over ESP-NOW). It
estimates where every nearby badge is in its cycle now, and takes the
circular mean of those phases weighted by 1 / distance (FTM median from
lib.ranging), so close badges pull harder than far ones. Its own period is
then shortened when the mean is ahead and lengthened when it is behind:

    period = PERIOD_MS / (1 + GAIN * offset)    offset in cycles, -0.5..0.5

With a small GAIN and a tight period range this is gradual: every flash
lands just a little closer to the others, and a group drifts into step over
about half a minute rather than snapping together.

Badges also agree on a cycle count (the highest one heard wins), and the
flash comes from it: a wave along the LED bar whose direction (out from the
centre, left to right, in from the ends, right to left) and colours change
every cycle. So a synced group ripples the same way in the same colours.

Only badges with a distance measured take part: C3 ftm_node boards do not
send SYNC, and a badge heard but not ranged yet has no weight.

This module does no radio or LED I/O itself; lib.ranging moves the messages
and apps/ranging.py drives the LEDs with led_values().
"""

import math

try:
    from time import ticks_diff, ticks_ms
except ImportError:  # CPython, for simulating on a PC
    import time as _time

    def ticks_ms():
        return int(_time.monotonic() * 1000)

    def ticks_diff(a, b):
        return a - b

PERIOD_MS = 2000      # nominal flash period
# Gradual on purpose: each cycle a badge moves only about GAIN of the way
# toward the others (10%), and its period stays within +-5% of nominal, so it
# never gains or loses more than ~100 ms on them per flash. Badges drift into
# step over about half a minute instead of snapping together.
GAIN = 0.1            # period change per cycle of phase offset
MIN_PERIOD_MS = 1900
MAX_PERIOD_MS = 2100
SYNC_MS = 500         # how often the phase is broadcast
STALE_MS = 3000       # ignore a badge's phase after this long without a SYNC
MIN_DISTANCE_M = 0.5  # distances are uncalibrated; never weigh by 1/0
RISE_MS = 60          # flash envelope of each LED: quick rise...
FALL_MS = 300         # ...slower fade, which leaves a tail behind the wave
WAVE_MS = 360         # time for the wave to cross the bar
# A flash lasts WAVE_MS + RISE_MS + FALL_MS (720 ms), well inside MIN_PERIOD_MS.
# Full-brightness flash colours, one per cycle. The LEDs are very bright;
# the rest of the badge uses 0-3.
PALETTE = (
    (16, 0, 0),    # red
    (16, 5, 0),    # orange
    (12, 10, 0),   # yellow
    (0, 16, 0),    # green
    (0, 12, 10),   # cyan
    (0, 0, 16),    # blue
    (8, 0, 14),    # purple
    (14, 0, 6),    # pink
)


def _advance(cycles, phase, ms, period):
    """(cycles, phase) after ms more at period."""
    p = phase + ms / period
    n = int(p)
    return cycles + n, p - n


def _wrap(x):
    """Phase difference into [-0.5, 0.5)."""
    return (x + 0.5) % 1.0 - 0.5


class Firefly:
    def __init__(self, distance_of, now=None):
        """distance_of(mac) -> metres or None (lib.ranging service.distance)."""
        self._distance_of = distance_of
        self.period = PERIOD_MS
        self._cycles = 0
        self._phase = 0.0
        self._anchor = ticks_ms() if now is None else now
        self._peers = {}  # mac -> (cycles, phase, period, ticks_ms received)
        self.offset = 0.0  # weighted mean phase offset of the others, cycles
        self.weight = 0.0  # total weight behind it (0 = alone)

    # ---- oscillator ---------------------------------------------------------
    def _now(self, now):
        """Advance to now and re-anchor there; returns (cycles, phase)."""
        self._cycles, self._phase = _advance(
            self._cycles, self._phase, ticks_diff(now, self._anchor), self.period)
        self._anchor = now
        return self._cycles, self._phase

    def state(self, now=None):
        """(cycles, phase 0..1, period ms) now, for the SYNC message."""
        now = ticks_ms() if now is None else now
        c, p = self._now(now)
        return c, p, self.period

    # ---- peers --------------------------------------------------------------
    def heard(self, mac, cycles, phase, period, now=None):
        """A SYNC from mac: its (cycles, phase, period) at reception time."""
        now = ticks_ms() if now is None else now
        if not MIN_PERIOD_MS <= period <= MAX_PERIOD_MS:
            return
        self._peers[mac] = (cycles, phase, period, now)

    def adjust(self, now=None):
        """Retune the period toward the distance-weighted mean phase of the
        badges nearby. Call every SYNC_MS or so."""
        now = ticks_ms() if now is None else now
        c_me, p_me = self._now(now)
        sx = sy = total = 0.0
        ahead = 0
        for mac, (c, p, period, t) in list(self._peers.items()):
            age = ticks_diff(now, t)
            if age > STALE_MS:
                del self._peers[mac]
                continue
            d = self._distance_of(mac)
            if d is None:
                continue
            w = 1.0 / max(d, MIN_DISTANCE_M)
            c, p = _advance(c, p, age, period)  # where it is in its cycle now
            diff = _wrap(p - p_me)
            a = 2 * math.pi * diff
            sx += w * math.cos(a)
            sy += w * math.sin(a)
            total += w
            # Cycle count it is on, as seen from here: a peer just past its
            # flash while we are just before ours is one cycle ahead, not 0.
            k = (c - c_me) + round(p - p_me - diff)
            if k > ahead:
                ahead = k
        self.weight = total
        if total == 0 or (sx == 0 and sy == 0):
            self.offset = 0.0
            self.period = PERIOD_MS
            return
        if ahead:
            self._cycles += ahead
        self.offset = math.atan2(sy, sx) / (2 * math.pi)
        period = PERIOD_MS / (1 + GAIN * self.offset)
        self.period = min(MAX_PERIOD_MS, max(MIN_PERIOD_MS, period))

    # ---- LEDs ---------------------------------------------------------------
    def dark_ms(self, now=None):
        """ms until the next flash starts, or 0 while one is showing. The app
        does its blocking work (screen redraws, garbage collection) when this
        is large enough, so it never freezes a flash halfway."""
        now = ticks_ms() if now is None else now
        _, p = self._now(now)
        t = p * self.period
        if t < WAVE_MS + RISE_MS + FALL_MS:
            return 0
        return int(self.period - t)

    def led_values(self, n, now=None):
        """n LED colours for this instant, or None between flashes.

        The flash is a wave along the bar: each LED runs the same rise/fade
        envelope, started after a delay set by its position, so the light
        travels. The direction changes every cycle (WAVES), and the colour
        shades along the bar from this cycle's PALETTE colour to the next.
        Both follow the shared cycle count, so badges in sync ripple the same
        way in the same colours. LEDs the wave has not reached, or has
        passed, are (0, 0, 0)."""
        now = ticks_ms() if now is None else now
        c, p = self._now(now)
        t = p * self.period
        if t >= WAVE_MS + RISE_MS + FALL_MS:
            return None
        wave = WAVES[c % len(WAVES)]
        c0 = PALETTE[c % len(PALETTE)]
        c1 = PALETTE[(c + 1) % len(PALETTE)]
        out = []
        for i in range(n):
            x = i / (n - 1) if n > 1 else 0.5  # 0 at the left end, 1 at the right
            b = _envelope(t - wave(x) * WAVE_MS)
            if b <= 0:
                out.append((0, 0, 0))
                continue
            out.append(tuple(int((a + (z - a) * x) * b + 0.5) for a, z in zip(c0, c1)))
        return out


def _envelope(t):
    """Brightness 0..1 of one LED, t ms after its flash starts."""
    if t < 0:
        return 0.0
    if t < RISE_MS:
        b = t / RISE_MS
    elif t < RISE_MS + FALL_MS:
        b = 1 - (t - RISE_MS) / FALL_MS
    else:
        return 0.0
    return b * b  # perceived brightness is far from linear; this looks smoother


# Delay of the LED at x (0 = left end, 1 = right end), as a fraction of
# WAVE_MS: one entry per cycle, in turn.
WAVES = (
    lambda x: abs(x - 0.5) * 2,        # ripple out from the centre
    lambda x: x,                       # sweep left to right
    lambda x: 1 - abs(x - 0.5) * 2,    # close in from both ends
    lambda x: 1 - x,                   # sweep right to left
)
