# Billboard Badge Firmware

## Installing

The installer adds Ranging to a stock badge and keeps everything already on it (apps, nametag, gallery, data). It flashes only the MicroPython app image, which is stock MicroPython plus the `ftm` and `graphlayout` modules, from the [latest release](https://github.com/widdakay/hackaday-2026-badge/releases/latest), then copies in the Ranging files. It backs up your code and settings first, and asks for the name to show.

```sh
pip install esptool mpremote
python scripts/install_ranging.py        # badge plugged in over USB
```

Options: `--port`, `--name NAME`, `--local` (use the files in this repo, not the release). The top of `scripts/install_ranging.py` lists the rest, and how to undo the install.

* Ranging only finds badges that run this firmware. ESP32-C3 boards running [`ftm_node`](ftm_node/README.md) also show up, as fixed reference points.
* Your name is in `/data/settings.json` on the badge: `{"name": "..."}`, up to 16 bytes.
* Developers working from this repo: `python scripts/update.py push --reset` syncs all of `badge/` to the badge.
* Crash log: `mpremote cp :data/log.txt .`, then press RESET.

# Micropython
Baseline micropython 1.27 (stock MicroPython, without the `ftm` module, so no Ranging)
Download from https://micropython.org/download/ESP32_GENERIC_S3/ `Firmware (Support for Octal-SPIRAM)` v1.27.0 .bin
`esptool.py --baud 460800 write_flash 0 ~/Downloads/ESP32_GENERIC_S3-SPIRAM_OCT-20251209-v1.27.0.bin`

# Build notes (lvgl_micropython) (Deprecated)
You need to sync the lvgl_micropython submodule and it's recursive submoules.
Then in `firmware/lvgl_micropython/lib/lv_conf.h`, find `#define LV_USE_LODEPNG 1` and change it to `0`.

lvgl_micropython build command from `firmware/lvgl_micropython`: `GEN_SCRIPT=python python make.py esp32s3 --custom-board-path=../lvgl_billboard_badge/`
Requires `toml` python package be installed.

Building outputs a command to flash: `~/.espressif/python_env/idf5.5_py3.14_env/bin/python -m esptool --chip esp32s3 -p /dev/ttyACM0 -b 460800 --before default_reset --after hard_reset write_flash --flash_mode dio --flash_size 16MB --flash_freq 80m --erase-all 0x0 ./build/lvgl_micropy_lvgl_billboard_badge.bin`

# Build notes (lv_micropython)


Install esp-idf: https://github.com/lvgl/lv_micropython/blob/master/ports/esp32/README.md#setting-up-the-toolchain-and-esp-idf
`./install.sh` and `. ./export.sh` in the shell that will be doing builds.

In `firmware/lv_micropython/ports/esp32` run `make BOARD=ESP32_GENERIC_S3 BOARD_VARIANT=BILLBOARD_BADGE`  to build and `python -m esptool --chip esp32s3 -b 460800 --before default_reset --after hard_reset write_flash --flash_mode dio --flash_size 8MB --flash_freq 80m 0x0 build-ESP32_GENERIC_S3-BILLBOARD_BADGE/bootloader/bootloader.bin 0x8000 build-ESP32_GENERIC_S3-BILLBOARD_BADGE/partition_table/partition-table.bin 0x10000 build-ESP32_GENERIC_S3-BILLBOARD_BADGE/micropython.bin` to flash.
You will need to `make clean BOARD=ESP32_GENERIC_S3 BOARD_VARIANT=BILLBOARD_BADGE` (yes including the arguments) to rebuild. The BOARD argument defines a build directory, so clean won't clean the correct directory without it.

Flashing the pre-built bin files: From `firmware/` run `python -m esptool --chip esp32s3 -b 460800 --before default_reset --after hard_reset write_flash --flash_mode dio --flash_size 8MB --flash_freq 80m 0x0 bin/bootloader.bin 0x8000 bin/partition-table.bin 0x10000 bin/micropython.bin`. To get into the mode to flash this, you need to hold down the BOOT button while either plugging in or pressing and releasing RESET.

## Building on macOS (notes from the ftm-ranging-ui branch)
- ESP-IDF v5.2.3: `git clone -b v5.2.3 --depth 1 --recursive --shallow-submodules https://github.com/espressif/esp-idf.git`, then `./install.sh esp32s3` and `python3 tools/idf_tools.py install cmake ninja` (no Homebrew needed). On Python 3.9, if the dependency check complains about `ruamel.yaml`, install `ruamel.yaml==0.17.21` into the IDF Python env.
- Clone `lvgl/lv_micropython` into `firmware/lv_micropython` and fetch the submodules the ESP32 build needs: `git submodule update --init --depth 1 --recursive user_modules/lv_binding_micropython` and `git submodule update --init --depth 1 lib/berkeley-db-1.xx lib/micropython-lib lib/tinyusb`.
- Copy `lv_files/boards/ESP32_GENERIC_S3/*` to `lv_micropython/ports/esp32/boards/ESP32_GENERIC_S3/` and `lv_files/user_modules/lv_binding_micropython/lv_conf.h` to `lv_micropython/user_modules/lv_binding_micropython/`.
- Recent Apple clang fails on `mpy-cross`: `make -C mpy-cross CFLAGS_EXTRA="-Wno-gnu-folding-constant"`.
- After adding or removing a user C module, `make clean` (with the BOARD/BOARD_VARIANT arguments) before building, or the link fails with `undefined reference to mp_module_lvgl`.
- User C modules: `user_c_modules/ftm` (Wi-Fi FTM) and `user_c_modules/graphlayout` (the Ranging map's layout solver).
- Only these Montserrat sizes are compiled in: 14, 16, 24, 28, 32, 36, 40, 44, 48.

## lv_micropython changes:
Copy the files from `firmware/lv_files/` to their matching paths, ex: `lv_files/foo/` -> `lv_micropython/foo/`.
