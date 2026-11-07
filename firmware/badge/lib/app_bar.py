"""App bar: every app's name in a strip across the top of the screen, with the
running app highlighted, so you can always tell what the badge is showing.

lib.app_switcher calls setup() once with the app names, then set_active()
whenever an app launches. setup() installs paint() as the display's
before_update hook, so the bar is drawn on top of every frame from every app,
whether it was drawn with LVGL or PNGs, right before the refresh starts.

Apps that want to keep their content clear of the bar start at y = HEIGHT.
"""

import lvgl

from lib.epd import display, display_width

try:
    from lib.battery import battery_icon
except Exception as ex:          # no ADC (e.g. a PC test) or a broken battery module
    print("app_bar: no battery icon:", ex)
    battery_icon = None

HEIGHT = 40          # bar plus its bottom rule
BATTERY_X = display_width - 56   # byte-aligned (multiple of 8), right end of the bar
BATTERY_Y = 7
_RULE = 3
_FONT = lvgl.font_montserrat_16   # sizes in firmware: 14, 16, 24, 28, ...
_PAD_X = 12          # inside each name's box
_GAP = 6             # between boxes
_WHITE = lvgl.color_hex(0xFFFFFF)
_BLACK = lvgl.color_hex(0x000000)

_bar = None
_tabs = []           # (box, label) per app
_active = None
_cursor = None
_painted = None      # (active, cursor) last drawn into the framebuffer


def setup(names):
    """Build the bar for `names` (in menu order) and hook it into the display."""
    global _bar, _tabs
    if _bar is not None:
        _bar.delete()
    _bar = lvgl.obj(lvgl.layer_top())
    _bar.remove_style_all()
    _bar.set_pos(0, 0)
    _bar.set_size(display_width, HEIGHT)
    _bar.set_style_bg_color(_WHITE, 0)
    _bar.set_style_bg_opa(lvgl.OPA.COVER, 0)

    rule = lvgl.obj(_bar)
    rule.remove_style_all()
    rule.set_pos(0, HEIGHT - _RULE)
    rule.set_size(display_width, _RULE)
    rule.set_style_bg_color(_BLACK, 0)
    rule.set_style_bg_opa(lvgl.OPA.COVER, 0)

    _tabs = []
    x = 8
    box_h = HEIGHT - _RULE - 8
    for name in names:
        box = lvgl.obj(_bar)
        box.remove_style_all()
        box.set_pos(x, 4)
        box.set_height(box_h)
        box.set_style_radius(6, 0)
        box.set_style_bg_opa(lvgl.OPA.COVER, 0)
        lbl = lvgl.label(box)
        lbl.set_style_text_font(_FONT, 0)
        lbl.set_text(name)
        lbl.update_layout()
        w = lbl.get_width() + 2 * _PAD_X
        box.set_width(w)
        lbl.center()
        _tabs.append((box, lbl))
        x += w + _GAP
    set_active(_active)
    display.before_update = paint


def set_active(idx, cursor=None):
    """Highlight the running app `idx` (filled black) and, while picking an
    app in the menu, outline the one under the `cursor`."""
    global _active, _cursor
    _active, _cursor = idx, cursor
    for i, (box, lbl) in enumerate(_tabs):
        on = i == idx
        box.set_style_bg_color(_BLACK if on else _WHITE, 0)
        lbl.set_style_text_color(_WHITE if on else _BLACK, 0)
        box.set_style_border_width(3 if i == cursor else 0, 0)
        box.set_style_border_color(_BLACK, 0)
        # An outline on the filled tab would vanish; draw it outside instead.
        box.set_style_outline_width(3 if (i == cursor and on) else 0, 0)
        box.set_style_outline_color(_BLACK, 0)
        box.set_style_outline_pad(2, 0)


def changed():
    """Does the bar differ from what was last drawn?"""
    return (_active, _cursor) != _painted


def refresh():
    """Show the current bar on the panel with a partial refresh of just the
    bar strip (paint() runs from the display's before_update hook).

    Same strategy as the Ranging app: the fast black/white waveform, and only
    once the previous refresh has finished (callers check
    display.is_done_updating() and changed() first)."""
    display.update(fast=True, rows=HEIGHT)


def paint():
    """Draw the bar into the framebuffer, touching nothing below it."""
    global _painted
    if _bar is None:
        return
    _painted = (_active, _cursor)
    _bar.update_layout()
    # Flush only the bar: drop whatever else LVGL thinks is dirty.
    display.lv_display.inv_p = 0
    _bar.invalidate()
    lvgl.refr_now(display.lv_display)
    if battery_icon is not None:
        try:
            battery_icon.draw(display.black, display.red, BATTERY_X, BATTERY_Y)
        except Exception as ex:
            print("app_bar: battery icon failed:", ex)
