import micropython
import time
import asyncio as aio

try:
    time.sleep(0.5)
    print("Importing libraries...")
    from lib.app_switcher import switcher
    from lib.button import scan as button_scan, UserA, UserB
    from lib.epd import display
    from lib.led_bar import leds
    import lib.json_configs as json_configs
    print("Importing apps...")
except Exception as ex:
    # If anything goes wrong at import time, wait a second and print it
    # Sometimes these are hard to see, so the delay and extra print may help
    import sys
    import time

    time.sleep(1)
    sys.print_exception(ex)
    raise


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

    print("Running main loop...")
    while True:
        print("Main 60s heartbeat --^v--^v--")
        micropython.mem_info()
        await aio.sleep(60)


if __name__ == "__main__":


    aio.run(main())
