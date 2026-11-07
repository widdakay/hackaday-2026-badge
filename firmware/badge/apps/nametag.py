"""Nametag app

The "Hello I am..." template with the badge's name in Audiowide. The name
comes from data/settings.json and is drawn on the badge (lib/name_render.py),
so to change it, edit that file and reset: no firmware rebuild.
"""

from apps.base_app import BaseApp
from lib import name_render
from lib.app_bar import HEIGHT as _BAR_H
from lib.epd import display
from lib.led_bar import leds

_TEMPLATE = "/data/graphics/hello_i_am_blank.png"
# White name band in the template, moved down below the app bar.
_BAND_TOP = 221 + _BAR_H
_BAND_BOTTOM = 557 + _BAR_H
_MARGIN_X, _MARGIN_Y = 60, 40


class NametagApp(BaseApp):
    def __init__(self, name: str | None = None) -> None:
        self.name = "Nametag"
        self.sleep_ms = 100
        self.launched = False

    def run(self):
        if self.launched:
            leds.fill((0, 1, 1))
            display.clear()
            # The template's bottom red band is tall enough to lose the bar's 40 px.
            display.draw_png(_TEMPLATE, _BAR_H, 0)
            name_render.draw_name(display, _MARGIN_X, _BAND_TOP + _MARGIN_Y,
                                  960 - _MARGIN_X, _BAND_BOTTOM - _MARGIN_Y, size=240)
            display.update()
            leds.fill((0, 1, 0))
