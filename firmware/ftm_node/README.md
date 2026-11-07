# ftm_node: Wi-Fi FTM ranging test nodes (ESP32-C3)

Standalone ESP-IDF firmware (C++) for measuring distances between boards with
Wi-Fi Fine Timing Measurement (802.11mc round-trip timing). Used to calibrate
and test ranging before it goes into the badge firmware.

Every board runs the same firmware:

- a hidden softAP on channel 6 with the FTM responder on
- an ESP-NOW broadcast beacon every second, for discovery (no Wi-Fi scanning)
- a loop that ranges each board heard in the last 10 s, in turn
- an ESP-NOW broadcast of every result, so the board on USB also prints what
  the other boards measured (`RDIST` lines)

The blue LED (GPIO8 on a C3 SuperMini) flashes on every successful measurement,
so a board on a USB charger across the room shows it's working.

## Build and flash

Needs ESP-IDF v5.2.3 (the same version as the badge firmware).

```sh
. ~/esp/esp-idf-v5.2.3/export.sh
cd firmware/ftm_node
idf.py set-target esp32c3      # first time only
idf.py build
idf.py -p /dev/cu.usbmodemXXXX flash
```

## Collect calibration data

Put one board on USB power at a measured distance, with a clear line of sight,
both boards at the same height and the same orientation. Plug the other into
the laptop and run, once per distance:

```sh
uv run --with pyserial firmware/ftm_node/tools/ftm_monitor.py --true-m 3.0 --csv cal.csv --duration 60
```

`--true-m` labels every row with the real distance, so runs at different
distances can go in one CSV. The tool prints live per-pair stats and, with
`--true-m`, the error of the median.

## Serial output

One record per line, comma separated. See the comment at the top of
`main/main.cpp` for the fields. Notes on the numbers:

- `dist_cm`, `rtt_raw_ns` and `rtt_est_ns` are the driver's, rounded down to
  whole nanoseconds of round trip (15 cm steps).
- Per-frame round trips are quantized to 1.5625 ns (640 MHz clock, 23 cm) and
  negative frames are clamped to 0. `rtt_mean_ps` is the useful value: the
  driver's average without the rounding. Clamping biases it upward at short
  range (around 1 m).
- One-way distance in cm = `rtt_mean_ps * 0.0149896`.
