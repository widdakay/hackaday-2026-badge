"""Nametag app

Shows /data/graphics/nametag.png: the "Hello I am..." template with the name
pre-rendered in Audiowide. Regenerate it on your computer with
`scripts/make_nametag.py "Your Name"` and push it to the badge.
"""

from apps.base_app import BaseApp
from lib.epd import display
from lib.led_bar import leds

_NAMETAG = "/data/graphics/nametag.png"
_FALLBACK = "/data/graphics/hello_i_am_blank.png"


class NametagApp(BaseApp):
    def __init__(self, name: str | None = None) -> None:
        self.name = "Nametag"
        self.sleep_ms = 100
        self.launched = False

    def run(self):
        if self.launched:
            leds.fill((0, 1, 1))
            display.clear()
            try:
                display.draw_png(_NAMETAG)
            except OSError:
                print(f"{_NAMETAG} missing, showing blank template")
                display.draw_png(_FALLBACK)
            display.update()
            leds.fill((0, 1, 0))
