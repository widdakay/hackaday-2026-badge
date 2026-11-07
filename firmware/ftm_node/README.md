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

## CSI (channel state information)

Experimental, see `firmware/CSI_PLAN.md`. With `kCsiEnabled` (on by default in
`main/main.cpp`) the node prints a `CSI` line for packets it receives from
known peers, with the raw per-subcarrier channel, rate-limited to
`kCsiMaxHzPerPeer` per peer and packet kind, plus a `CSISTAT` line every 10 s.
It also broadcasts a small ESP-NOW `PING` (type 5) `kPingHz` times a second,
at HT20 MCS0 (`kEspnowHtRate`), so other nodes get frequent CSI that
includes the HT-LTF field.

Only OFDM packets carry CSI. Badges and older nodes send ESP-NOW at 1 Mbps
802.11b, which has no OFDM training field, so from those peers CSI comes only
from the FTM frames of this node's own ranging sessions (`kind` = `ftm`).

Record labelled runs (one per distance and scene):

```sh
uv run --with pyserial firmware/ftm_node/tools/ftm_monitor.py --true-m 3.0 --scene body \
    --csv csi_cal.csv --csi csi_cal.jsonl --duration 60
```

Scenes: `los`, `body` (wearer's back to the other node), `person` (someone
between), `crowd`, `wall`, `walking-node`, `walking-people`. Then:

```sh
uv run --with numpy,pandas firmware/ftm_node/tools/csi_analysis.py --csv csi_cal.csv --csi csi_cal.jsonl
```

It prints whether CSI predicts NLOS, FTM error or motion well enough to be
worth putting on the badge (the plan's go/no-go).
