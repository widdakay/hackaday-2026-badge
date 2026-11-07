"""App Switcher"""

import asyncio as aio  # type: ignore
import os
import sys
import time
import btree

from apps.base_app import BaseApp
from lib.button import MenuMinus, MenuPlus, UserA, UserB, PRESSED_READ
from lib.led_bar import leds
from lib import app_bar
from lib.epd import display

# MENU_TIMEOUT_MILLISECONDS = 1000

# In the app menu, redraw the bar this long after the last button press, so a
# run of presses costs one partial refresh.
BAR_SETTLE_MS = 500

# Apps to launch on boot, first one found wins (Ranging needs firmware with the
# ftm module; without it the app doesn't load and Nametag starts instead).
# Set to () to start in the app menu.
BOOT_APP_NAMES = ("Ranging", "Nametag")


APPMOD_DENYLIST = {
    "__init__",
    "base_app",
    "template",
}


class AppSwitcher:
    def __init__(self, initial_idx: int = 0) -> None:
        self.apps = []
        self.active_idx = initial_idx
        self.selecting_mode = True
        self.selection_idx = initial_idx
        self._bar_dirty_ms = None

        # Discover and load apps
        app_mods = []
        for filename, _, _, _ in os.ilistdir("/apps"):  # pyright: ignore[reportAssignmentType]
            if not filename.endswith(".py") or filename.startswith("_"):
                continue
            app_modname = filename.split(".")[0]
            app_mods.append(app_modname)
        if app_mods:
            print(f"Found app mods: {app_mods}")
            with open("/data/user_apps.py", "w") as out:
                out.write(f"from apps import {', '.join(app_mods)}\n")

        import data.user_apps as apps  # pyright: ignore[reportMissingImports]
        apps_list = [app for app in dir(apps) if "__" not in app]
        print(f"App module list: {apps_list}")
        for appmod_name in apps_list:
            if appmod_name.startswith("_") or appmod_name in APPMOD_DENYLIST:
                continue
            appmod = getattr(apps, appmod_name)
            try:
                for modattr in appmod.__dict__.values():
                    # Subclasses of an app class count too. _abstract skips the
                    # shared base so only the concrete apps are constructed.
                    if not isinstance(modattr, type) or not issubclass(modattr, BaseApp):
                        continue
                    if modattr is BaseApp or modattr.__dict__.get("_abstract"):
                        continue
                    app = modattr()
                    self.apps.append(app)
                    print(f"Added app: {app.name}")
            except Exception as exc:
                print(f"Failed to load app module: {appmod}")
                sys.print_exception(exc)

        try:
            apps_file = open("/data/app_list", "r+b")
        except OSError:
            apps_file = open("/data/app_list", "w+b")
        # Now open a database itself
        apps_db = btree.open(apps_file)
        for idx, app in enumerate(self.apps):
            apps_db[bytes([idx])] = bytes(app.name, "UTF-8")
        apps_db.flush()
        apps_db.close()
        apps_file.close()

        app_bar.setup([app.name for app in self.apps])

        # Boot straight into the boot app instead of waiting in the menu.
        names = [app.name for app in self.apps]
        for boot_name in BOOT_APP_NAMES:
            if boot_name in names:
                idx = names.index(boot_name)
                self.active_idx = self.selection_idx = idx
                self.apps[idx].launched = True
                self.selecting_mode = False
                app_bar.set_active(idx)
                print(f"Booting into {boot_name}")
                break


    async def arun(self):
        while True:
            if self.selecting_mode:
               
                ## Display current app from all installed apps
                leds.menu_display(self.selection_idx, len(self.apps))

                if MenuPlus.pressed():
                    self.selection_idx = (self.selection_idx + 1) % len(self.apps)
                    print(
                        f"Selected app {self.selection_idx}: {self.apps[self.selection_idx].name}"
                    )
                    self._bar_dirty_ms = time.ticks_ms()
                if MenuMinus.pressed():
                    self.selection_idx = (self.selection_idx - 1) % len(self.apps)
                    print(
                        f"Selected app {self.selection_idx}: {self.apps[self.selection_idx].name}"
                    )
                    self._bar_dirty_ms = time.ticks_ms()
                # Like the Ranging app: wait for the previous refresh to finish,
                # then refresh (fast, bar strip only) if the bar changed.
                if (self._bar_dirty_ms is not None
                        and time.ticks_diff(time.ticks_ms(), self._bar_dirty_ms) > BAR_SETTLE_MS
                        and display.is_done_updating()):
                    self._bar_dirty_ms = None
                    app_bar.set_active(self.active_idx, cursor=self.selection_idx)
                    if app_bar.changed():
                        app_bar.refresh()
                if UserA.pressed() or UserB.pressed():
                    print(f"Switching to {self.apps[self.selection_idx].name}")
                    self.active_idx = self.selection_idx
                    self.apps[self.active_idx].launched = True
                    self._bar_dirty_ms = None
                    app_bar.set_active(self.active_idx)
                    leds.clear_sequence()
                    leds.fill((0, 0, 0))
                    leds.write()
                    self.selecting_mode = False
                await aio.sleep_ms(10)
            else:
                if MenuMinus.pressed() or MenuPlus.pressed():
                    self.selection_idx = self.active_idx
                    self.selecting_mode = True
                    self._bar_dirty_ms = time.ticks_ms()   # show the menu cursor
                    ## Reset pressed state so that button doesn't advance menu, just enters menu state
                    MenuMinus.discard()
                    MenuPlus.discard()
                    # Presses latch until read; apps that ignore A/B would
                    # otherwise leave one that instantly picks in the menu.
                    UserA.discard()
                    UserB.discard()
                    print("Activated app switching")
                await self.apps[self.active_idx].arun()
                await aio.sleep_ms(0)
                self.apps[self.active_idx].launched = False


switcher = AppSwitcher()
