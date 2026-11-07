"""Base App to derive other apps from"""

import asyncio as aio
import gc
import sys

from lib import log


class BaseApp:
    """Base class to derive other app from.
    Exists to make it easier to make app-generic behaviors.
    """

    def __init__(self, name: str | None = None) -> None:
        self.name = name or self.__class__.__name__
        self.sleep_ms = 100
        """When your app is running, it will sleep this many ms between calls to run()"""
        self.launched = False
        """This will be True for the first time each app is selected,
        so you can do something each time it is started, like draw something to the screen
        It will be reset to False after the first cycle."""

    async def arun(self):
        """Runs a coroutine task for the app.
        Will be called in a loop and awaited by the AppSwitcher (lib.app_switcher)
        Only override if you feel comfortable with asyncio and
        want to add sleeps and control the loop behavior.
        """
        try:
            self.run()
        except Exception as ex:
            print(f"{self.name} hit an exception {ex}")
            sys.print_exception(ex)
            gc.collect()
            log.error(f"app {self.name}", ex)
        await aio.sleep_ms(self.sleep_ms)

    def run(self):
        """Override this with your app behavior. Will be run in a while True loop."""
        pass
