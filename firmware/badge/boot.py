# This file is executed on every boot (including wake-boot from deepsleep)
#import esp
#esp.osdebug(None)
#import webrepl
#webrepl.start()

# Record why the chip last reset. Only the first read after a hardware reset
# is meaningful (after a soft reset it is always SOFT_RESET). Panics print to
# UART0, which is not on the USB port, so this file is the only trace of them.
#   1 = power on OR brownout (with USB power present, it was a brownout)
#   2 = panic / software reset   3 = watchdog   5 = soft reset
try:
    import machine
    import time

    _cause = machine.reset_cause()
    print("RESET_CAUSE", _cause)
    if _cause != machine.SOFT_RESET:
        with open("/data/reset_log.txt", "a") as _f:
            _f.write("%d cause=%d\n" % (time.time(), _cause))
except Exception as _ex:
    print("reset log failed", _ex)
