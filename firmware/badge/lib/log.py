"""Crash/event log kept on flash, so it survives resets and power cycles.

Lines go to /data/log.txt (and are echoed to the USB console with a "LOG "
prefix). When the file passes MAX_BYTES it moves to /data/log.1.txt, so at
most about 2 x MAX_BYTES of flash is used.

Each line starts with the boot number and the uptime in seconds:

    b12 t3601.250 heartbeat app=Ranging free=1834512 ...

The clock is not set on the badge, so there is no time of day. A crash that
resets the board (panic, watchdog, brownout) shows up as a new "boot" line
with reset=HARD or WDT and no "stopped" line just before it; the last lines
before it say what was running. reset=PWRON is a power cycle. A reset from
mpremote or the REPL also reads reset=HARD, but always follows a "stopped:
KeyboardInterrupt" line. A crash that leaves the board running (an asyncio
task dying, an app raising) shows up as an "ERROR" line with the traceback.

Read it from a PC (Python 3.12 on this machine is not on PATH as `python`):

    python -m mpremote connect COM10 cp :data/log.txt .
    python -m mpremote connect COM10 cp :data/log.1.txt .

Connecting with mpremote stops main.py (it sends Ctrl-C); that is logged as
"stopped: KeyboardInterrupt", not as a crash. Reset the badge afterwards.

    from lib import log
    log.info("something happened")
    log.error("where", ex)    # with traceback; repeats are throttled
"""

import gc
import io
import os
import sys
import time

import machine

PATH = "/data/log.txt"
OLD_PATH = "/data/log.1.txt"
BOOT_PATH = "/data/log_boot"
MAX_BYTES = 64 * 1024
# The same error (same place, same message) is written at most once per
# REPEAT_MS; the next line says how many were skipped.
REPEAT_MS = 60_000

_RESET_NAMES = {
    getattr(machine, n): n.replace("_RESET", "")
    for n in ("PWRON_RESET", "HARD_RESET", "WDT_RESET", "DEEPSLEEP_RESET", "SOFT_RESET")
    if hasattr(machine, n)
}

boot_no = 0
_size = None
_seen = {}  # (where, message) -> [skipped count, ticks_ms last written]


def _uptime():
    ms = time.ticks_ms()
    return "%d.%03d" % (ms // 1000, ms % 1000)


def _write(text):
    """Append text to the log file; never raises."""
    global _size
    try:
        if _size is None:
            try:
                _size = os.stat(PATH)[6]
            except OSError:
                _size = 0
        if _size > MAX_BYTES:
            try:
                os.remove(OLD_PATH)
            except OSError:
                pass
            os.rename(PATH, OLD_PATH)
            _size = 0
        with open(PATH, "a") as f:
            f.write(text)
        _size += len(text)
    except Exception as ex:  # a full or broken filesystem must not take the badge down
        print("log: write failed", ex)


def info(msg):
    line = "b%d t%s %s\n" % (boot_no, _uptime(), msg)
    print("LOG", line, end="")
    _write(line)


def error(where, ex=None):
    """Log an error, with its traceback when ex is given.

    The first occurrence of an error is always written. Repeats of the same
    error are written at most once per REPEAT_MS, with a count of the ones
    skipped, so an error raised in a loop cannot fill the flash."""
    msg = "%s: %r" % (where, ex) if ex is not None else where
    key = msg[:120]
    now = time.ticks_ms()
    seen = _seen.get(key)
    if seen is not None:
        if time.ticks_diff(now, seen[1]) < REPEAT_MS:
            seen[0] += 1
            return
        if seen[0]:
            msg += " (repeated %d times since last logged)" % seen[0]
        seen[0] = 0
        seen[1] = now
    else:
        if len(_seen) > 50:
            _seen.clear()
        _seen[key] = [0, now]
    if ex is not None:
        buf = io.StringIO()
        sys.print_exception(ex, buf)
        tb = buf.getvalue().rstrip("\n").replace("\n", "\n    ")
        msg += "\n    " + tb
    info("ERROR " + msg)


def mem():
    gc.collect()
    return "free=%d alloc=%d" % (gc.mem_free(), gc.mem_alloc())


def boot():
    """Call once at startup: bumps the boot number and logs why the board reset."""
    global boot_no
    try:
        with open(BOOT_PATH) as f:
            boot_no = int(f.read()) + 1
    except (OSError, ValueError):
        boot_no = 1
    try:
        with open(BOOT_PATH, "w") as f:
            f.write(str(boot_no))
    except OSError:
        pass
    cause = machine.reset_cause()
    info("boot reset=%s %s fw=%s" % (_RESET_NAMES.get(cause, cause), mem(), sys.version))


def asyncio_handler(loop, context):
    """Loop exception handler: a background task died with an exception."""
    ex = context.get("exception")
    task = context.get("future")
    error("task %s died" % (task,), ex)
