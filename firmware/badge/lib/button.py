"""Button interface for the Billboard Badge"""

import machine
import asyncio as aio
import time

## Note: plus/minus mislabeled on the silkscreen!
MENU_MINUS_PIN = 40  # SW5  BUTTON_2  IO40
MENU_PLUS_PIN = 15  # SW6  BUTTON_3  IO15
USER_A_PIN = 39  # SW4  BUTTON_1  IO39
USER_B_PIN = 16  # SW7  BUTTON_4  IO16
BOOT_PIN = 0

UNPRESSED = 0
PRESSED_UNREAD = 1
PRESSED_READ = 2


class Button:
    def __init__(self, pin: int, name: str):
        self.pin = machine.Signal(machine.Pin(pin, machine.Pin.IN, machine.Pin.PULL_UP), invert=True)
        self.name = name
        self.value = False
        self._pressed_state = UNPRESSED
        self._press_onset_ms = None

    def scan(self):
        self.value = self.pin.value()
        if self.value and self._pressed_state == UNPRESSED:
            self._pressed_state = PRESSED_UNREAD
            self._press_onset_ms = time.ticks_ms()
        # A press stays latched until pressed() reads it, even after release:
        # apps can block the loop for seconds (PNG loads, LVGL), and clearing
        # on release lost quick taps.
        if not self.value and self._pressed_state == PRESSED_READ:
            self._pressed_state = UNPRESSED
            self._press_onset_ms = None

    def discard(self):
        """Drop a latched press nobody read."""
        if self._pressed_state == PRESSED_UNREAD:
            self._pressed_state = PRESSED_READ

    def pressed(self) -> bool:
        """Was a button pressed? Only returns True once for each press."""
        if self._pressed_state == PRESSED_UNREAD:
            self._pressed_state = PRESSED_READ
            return True
        return False

    def pressed_ms(self) -> int:
        if self._pressed_state in (PRESSED_UNREAD, PRESSED_READ) and self._press_onset_ms is not None:
            return time.ticks_diff(time.ticks_ms(), self._press_onset_ms)
        return 0

    def __str__(self):
        if self._pressed_state == UNPRESSED:
            pressed_state_name = "unpressed"
        elif self._pressed_state == PRESSED_UNREAD:
            pressed_state_name = "pressed-unread"
        elif self._pressed_state == PRESSED_READ:
            pressed_state_name = "pressed-read"
        else:
            pressed_state_name = ""
        return f"{self.name}:{self.value}:{pressed_state_name}"

MenuMinus = Button(MENU_MINUS_PIN, "Menu -")
MenuPlus = Button(MENU_PLUS_PIN, "Menu +")
UserA = Button(USER_A_PIN, "User A")
UserB = Button(USER_B_PIN, "User B")
Boot = Button(BOOT_PIN, "Boot")


async def scan():
    """Scan buttons and update persisted state"""
    while True:
        MenuMinus.scan()
        MenuPlus.scan()
        UserA.scan()
        UserB.scan()
        Boot.scan()
        await aio.sleep_ms(1)
