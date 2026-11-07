"""Battery reading, plus the small icon at the right end of the app bar.

Ported from Hack-a-Day/2026-Billboard_Badge PR #15 ("Add battery indicator app
and corner icon"). Here lib.app_bar draws the icon as part of the bar, so it's
on every app's screen; the PR's separate display overlay isn't needed.

BAT_SENS (IO14) sits on a 10k/10k divider from the 3.3V regulator's input, which
comes after the USB diode and the battery PFET. With USB plugged in the PFET is
off, so we read USB minus a diode drop and the battery can't be measured at all.
"""

import asyncio as aio

from lib.board import reg_in_v_sns

DIVIDER_RATIO = 2.0
"""Regulator input volts per volt at the ADC. Tweak to calibrate against a meter."""
USB_MIN_V = 4.35
"""A LiPo tops out at 4.2V and USB arrives at about 4.7V, so above this it's USB."""
NUM_SAMPLES = 64
ICON_BARS = 4
CHECK_PERIOD_MS = 3 * 60 * 1000
"""How often the monitor looks for a change that needs the bar redrawn."""

# Resting LiPo voltage to state of charge, highest first.
_DISCHARGE_CURVE = (
    (4.20, 100),
    (4.06, 90),
    (3.98, 80),
    (3.92, 70),
    (3.87, 60),
    (3.82, 50),
    (3.79, 40),
    (3.77, 30),
    (3.74, 20),
    (3.68, 10),
    (3.45, 5),
    (3.30, 0),
)


def voltage_to_pct(volts: float) -> int:
    """Estimate state of charge by interpolating the discharge curve."""
    prev_v, prev_pct = _DISCHARGE_CURVE[0]
    if volts >= prev_v:
        return prev_pct
    for v, pct in _DISCHARGE_CURVE[1:]:
        if volts >= v:
            return pct + round((volts - v) * (prev_pct - pct) / (prev_v - v))
        prev_v, prev_pct = v, pct
    return 0


def read_reg_in_volts() -> float:
    """Regulator input voltage, averaged to smooth out ADC noise."""
    total_uv = 0
    for _ in range(NUM_SAMPLES):
        total_uv += reg_in_v_sns.read_uv()
    return total_uv / NUM_SAMPLES * DIVIDER_RATIO / 1_000_000


def is_usb(volts: float) -> bool:
    return volts > USB_MIN_V


def pct_to_bars(pct: int) -> int:
    """Whole bars, rounded up so only a truly empty battery shows none."""
    return (pct * ICON_BARS + 99) // 100


def state():
    """'usb' or the number of bars (0..ICON_BARS) right now."""
    volts = read_reg_in_volts()
    return "usb" if is_usb(volts) else pct_to_bars(voltage_to_pct(volts))


class BatteryIcon:
    """Draws into the display's 1-bpp framebuffers (framebuf.FrameBuffer)."""

    W = 48
    H = 24

    def __init__(self) -> None:
        self.drawn = None
        """State on the last icon drawn: 'usb', a bar count, or None."""

    def draw(self, black, red, x, y, now=None) -> None:
        now = state() if now is None else now
        body_x, body_y = x + 3, y + 3
        black.fill_rect(x, y, self.W, self.H, 0)
        red.fill_rect(x, y, self.W, self.H, 0)
        black.rect(body_x, body_y, 38, 18, 1)
        black.rect(body_x + 1, body_y + 1, 36, 16, 1)
        black.fill_rect(body_x + 38, body_y + 5, 3, 8, 1)
        if now == "usb":
            black.text("USB", body_x + 7, body_y + 5, 1)
        else:
            ink = red if now <= 1 else black   # red when nearly empty
            for i in range(now):
                ink.fill_rect(body_x + 3 + i * 8, body_y + 3, 6, 12, 1)
        self.drawn = now


battery_icon = BatteryIcon()


async def monitor(redraw_bar) -> None:
    """Redraw the bar when the battery state on screen is out of date.

    Only a falling bar count (seen on two checks in a row, so not a brief dip
    under load) or USB being plugged/unplugged earns a redraw. `redraw_bar` is
    lib.app_bar.refresh, which updates just the bar strip; it waits until the
    display is idle, like every other bar refresh.
    """
    dropped = False
    while True:
        await aio.sleep_ms(CHECK_PERIOD_MS)
        try:
            from lib.epd import display
            if not display.is_done_updating():
                continue   # also: reading during a refresh catches the battery sagging
            drawn = battery_icon.drawn
            now = state()
            if drawn is None:
                continue
            if (now == "usb") != (drawn == "usb"):
                redraw_bar()
                dropped = False
            elif now != "usb" and now < drawn:
                if dropped:
                    redraw_bar()
                    dropped = False
                else:
                    dropped = True
            else:
                dropped = False
        except Exception as ex:
            print(f"Battery monitor hit an exception {ex}")
