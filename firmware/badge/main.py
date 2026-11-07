import micropython
import time
import asyncio as aio

from lib import log

log.boot()

try:
    time.sleep(0.5)
    print("Importing libraries...")
    from lib.app_switcher import switcher
    from lib.button import scan as button_scan, UserA, UserB
    from lib.epd import display
    from lib.led_bar import leds
    import lib.json_configs as json_configs
    print("Importing apps...")
    import lib.heap
    log.info("heap grown: free=%d" % lib.heap.grow())
except Exception as ex:
    # If anything goes wrong at import time, wait a second and print it
    # Sometimes these are hard to see, so the delay and extra print may help
    import sys
    import time

    time.sleep(1)
    sys.print_exception(ex)
    log.error("import failed", ex)
    raise

# Log every 5th 60 s heartbeat: the last one before a crash says roughly when
# it happened and how much memory was left.
HEARTBEAT_LOG_EVERY = 5
# A refresh takes 1-35 s. One still unfinished after this long is logged as
# stuck: the BUSY waits in lib/epd.py have no timeout, so a panel that never
# signals ready freezes the screen without raising anything.
DISPLAY_STUCK_S = 60


async def display_watch():
    import lib.epd as epd
    busy_since = None
    reported = False
    while True:
        await aio.sleep(5)
        if display.is_done_updating():
            if reported:
                log.info("display: refresh finished after %d s" % (
                    time.ticks_diff(time.ticks_ms(), busy_since) // 1000))
            busy_since = None
            reported = False
            continue
        if busy_since is None:
            busy_since = time.ticks_ms()
        elif not reported and time.ticks_diff(time.ticks_ms(), busy_since) > DISPLAY_STUCK_S * 1000:
            reported = True
            app = "menu" if switcher.selecting_mode else switcher.apps[switcher.active_idx].name
            log.info("ERROR display: refresh stuck > %d s, BUSY1=%d BUSY2=%d session=%s app=%s %s" % (
                DISPLAY_STUCK_S, epd.EPD_BUSY1_PIN.value(), epd.EPD_BUSY2_PIN.value(),
                getattr(display, "_session_mode", "?"), app, log.mem()))


async def main():
    
    try:
        configs = json_configs.load()
    except OSError: 
        configs = {}
    if "load_counter" in configs:
        configs["load_counter"] = configs["load_counter"] + 1
    else:
        configs["load_counter"] = 1
    json_configs.store(configs)
    print("Checking out the load-in counter")
    if configs['load_counter'] < 2: 
        ## Out of the box config.  Check out the counter in data/panel_config.json
        ## do splashscreen
        leds.fill((0,1,1))
        display.clear()
        display.draw_png(f"/data/graphics/supercon-x-splash.png")
        display.update()
        leds.fill((0,1,0))
    if configs['load_counter'] == 42:
        leds.fill((1,1,0))
        display.clear()
        display.draw_png(f"/data/graphics/never_epd.png")
        display.update()
        leds.fill((0,1,0))

    print("Initializing main...")
    # A background task that dies with an exception lands in the log.
    aio.get_event_loop().set_exception_handler(log.asyncio_handler)

    _button_task = aio.create_task(button_scan())
    _display_task = aio.create_task(display.run())
    _led_task = aio.create_task(leds.update())
    _apps_task = aio.create_task(switcher.arun())
    try:
        from lib import app_bar
        from lib.battery import monitor as battery_monitor
        _battery_task = aio.create_task(battery_monitor(app_bar.refresh))
    except Exception as ex:
        print("Battery monitor not started:", ex)
    _display_watch_task = aio.create_task(display_watch())

    print("Running main loop...")
    log.info("running " + log.mem())
    beat = 0
    while True:
        print("Main 60s heartbeat --^v--^v--")
        micropython.mem_info()
        if beat % HEARTBEAT_LOG_EVERY == 0:
            app = "menu" if switcher.selecting_mode else switcher.apps[switcher.active_idx].name
            log.info("heartbeat app=%s %s" % (app, log.mem()))
        beat += 1
        await aio.sleep(60)


if __name__ == "__main__":
    try:
        aio.run(main())
    except KeyboardInterrupt:
        # Ctrl-C, also what mpremote sends when it connects.
        log.info("stopped: KeyboardInterrupt")
        raise
    except BaseException as ex:
        log.error("main loop crashed", ex)
        raise
