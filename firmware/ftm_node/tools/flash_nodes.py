#!/usr/bin/env python3
"""Watch for ESP32-C3 boards on USB and flash ftm_node onto each one.

Run inside the ESP-IDF environment (needs esptool.py on PATH):

    . ~/esp/esp-idf-v5.2.3/export.sh
    python firmware/ftm_node/tools/flash_nodes.py

Plug boards in one after another (or several at once). Each new port is
probed; ESP32-C3 boards are flashed with build/ and logged by MAC. Anything
else (the badge is an ESP32-S3) is skipped, so a badge plugged in by mistake
is not wiped. Ctrl-C to stop.
"""

import glob
import os
import re
import subprocess
import sys
import time

BUILD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "build")
BUILD = os.path.normpath(BUILD)


def ports():
    return set(glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/cu.usbserial*")
               + glob.glob("/dev/cu.wchusbserial*"))


def probe(port):
    out = subprocess.run(["esptool.py", "-p", port, "--after", "hard_reset", "chip_id"],
                         capture_output=True, text=True, timeout=60).stdout
    chip = re.search(r"Chip is (\S+)", out)
    mac = re.search(r"MAC: ([0-9a-f:]{17})", out)
    return (chip.group(1) if chip else None), (mac.group(1) if mac else None)


def flash(port):
    r = subprocess.run(["esptool.py", "--chip", "esp32c3", "-p", port, "-b", "460800",
                        "--before", "default_reset", "--after", "hard_reset",
                        "write_flash", "@flash_args"],
                       cwd=BUILD, capture_output=True, text=True, timeout=180)
    return r.returncode == 0, r.stdout + r.stderr


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def main():
    if not os.path.exists(os.path.join(BUILD, "ftm_node.bin")):
        sys.exit(f"no build at {BUILD}; run idf.py build first")
    flashed = []          # MACs flashed this session, in order
    busy = set()          # ports already handled while plugged in
    log(f"watching for boards (image: {BUILD}/ftm_node.bin)")
    while True:
        now = ports()
        busy &= now       # unplugged ports can be handled again
        for port in sorted(now - busy):
            busy.add(port)
            time.sleep(0.5)  # let the port settle after enumeration
            try:
                chip, mac = probe(port)
            except subprocess.TimeoutExpired:
                log(f"{port}: probe timed out, skipped")
                continue
            if chip is None:
                log(f"{port}: no ESP chip answered, skipped")
                continue
            if not chip.startswith("ESP32-C3"):
                log(f"{port}: {chip} {mac} is not an ESP32-C3, skipped")
                continue
            if mac in flashed:
                log(f"{port}: {mac} already flashed this session (#{flashed.index(mac) + 1})")
                continue
            log(f"{port}: {chip} {mac} flashing...")
            ok, out = flash(port)
            if ok:
                flashed.append(mac)
                log(f"{port}: {mac} OK  (#{len(flashed)} this session)")
            else:
                tail = "\n    ".join(out.strip().splitlines()[-4:])
                log(f"{port}: {mac} FAILED, unplug and replug to retry\n    {tail}")
            # The board re-enumerates after the reset. Forget the port so the
            # next board plugged in here is always probed; if it's the same
            # board coming back, the MAC check above skips it.
            busy.discard(port)
            time.sleep(2)
        time.sleep(0.5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
