# Plan: Wi-Fi CSI to improve FTM ranging

Status (2026-11-07): phases 1–4 built. Firmware builds for C3 and S3 and
runs on a C3. The logger and analysis tools are tested on that board's
output and on synthetic data. Next: collect the labelled data (phase 3,
step 2), then run `tools/csi_analysis.py`. Phase 5 waits for its go/no-go.

First hardware finding: only OFDM packets carry CSI. Badges and older nodes
send ESP-NOW at 1 Mbps 802.11b (DSSS), which has no OFDM training field, so
from them the only CSI is from FTM frames (legacy OFDM, 6 Mbps) during this
node's own ranging sessions. The HT-rate pings (phase 2) are what give CSI
from ESP-NOW, so the most data comes when every node runs this firmware.

## Why

Ranging today (`badge/lib/ranging.py`, `ftm_node/`) works like this. Every
directed pair gets a median of the last 3 FTM results. The median is corrected
by a fixed bias for the initiating chip (`INITIATOR_BIAS_M`). Then
`graph_layout` fits a 2D map whose edge weights depend only on length. Nothing
says whether a reading is trustworthy. Readings through a body, a crowd or a
wall get the same weight as clear line-of-sight (LOS) readings, and the fixed
`HISTORY = 3` trades steadiness against response time.

Channel State Information (CSI) gives the amplitude and phase of each OFDM
subcarrier for every received packet. On channel 6 at HT20, a single packet
can't resolve multipath any finer than about 50 ns (15 m). **So CSI won't make
the distance itself more accurate**, but it can tell us:

1. **Edge confidence / NLOS detection.** Weight edges in `graph_layout` by
   quality, not only by length.
2. **Body blocking.** Badges are worn on the chest. Detect it, and possibly
   correct for it.
3. **Link motion.** Use a short median while things change and a long one while
   they're still, instead of the fixed `HISTORY = 3`.
4. **Outlier rejection.** Gate each FTM result on the CSI of the same moment.

Out of scope: the 1.9 m badge-vs-C3 initiator bias. CSI phase slope can't
recover it, because the timing and frequency offsets are random on every
packet. It stays a calibration constant.

Every phase ends with a go/no-go. If the offline analysis (phase 3) shows that
CSI features don't predict FTM error, we stop there, with only the data tools
built.

## Phase 1: capture CSI on `ftm_node`

The C3 nodes are the simplest place to start: plain ESP-IDF C++, no MicroPython
heap, and quick to reflash with `tools/flash_nodes.py`.

1. `sdkconfig.defaults`: add `CONFIG_ESP_WIFI_CSI_ENABLED=y`, then delete
   `sdkconfig` (or run `idf.py reconfigure`) so it takes effect. It is currently
   `# ... is not set`.
2. `main/main.cpp`, after `esp_wifi_start()`:
   - `wifi_csi_config_t`: `lltf_en = true`, `htltf_en = true`,
     `stbc_htltf2_en = true`, `ltf_merge_en = false` (keep LLTF and HT-LTF
     separate),
     `channel_filter_en = false` (we want the raw shape), `manu_scale = false`.
     The S3 and C3 share this struct; the C6 differs, so guard it with
     `CONFIG_IDF_TARGET_*` if a C6 ever joins.
   - `esp_wifi_set_csi_config()`, `esp_wifi_set_csi_rx_cb(on_csi, nullptr)`,
     `esp_wifi_set_csi(true)`.
3. `on_csi(void *, wifi_csi_info_t *info)` runs in the Wi-Fi task, so keep it
   short:
   - drop packets whose `info->mac` isn't a known peer (an unlocked peek at
     `g_peers` is fine; a stale read just drops one packet)
   - copy into a `CsiPacket` and `xQueueSend(..., 0)` it to a new `g_csi_queue`
     (depth ~16, drop when full). The packet holds `src[6]`, `rx_ctrl` fields
     (`rssi`, `noise_floor`, `rate`, `sig_mode`, `cwb`, `timestamp`,
     `rx_state`), `first_word_invalid`, `len`, and `buf[384]` (LLTF + HT-LTF +
     STBC worst case; check the real `len` and size it to that).
4. New `csi_task` that drains the queue and prints one line per packet:

   ```
   CSI,<from>,<to=self>,<t_us>,<rssi>,<noise_floor>,<rate>,<sig_mode>,<cwb>,<first_word_invalid>,<len>,<base64 buf>
   ```

   Document it in the serial-format comment at the top of `main.cpp` and in
   `ftm_node/README.md`. The console is USB-Serial-JTAG, so baud rate doesn't
   matter. About 25 peers × 20 Hz × ~200 bytes per line is ~100 kB/s, which is
   near what that link can carry. Hence the rate limit and the peer filter
   below.
5. Kconfig-style switch: `constexpr bool kCsiEnabled` and
   `constexpr uint32_t kCsiMaxHzPerPeer = 20`. Throttle per peer in `csi_task`
   rather than in the callback.

**Done when:** a node on USB prints CSI lines for every other node, while
ranging keeps its current rate and `DIST` lines don't drop.

## Phase 2: more and better CSI packets

Beacons come once a second, which is too sparse to measure motion or average
noise.

1. **Ping message (done).** `kMsgPing = 5` (4 is the badge's SYNC). It carries a
   header plus a `uint32_t seq`. Every node broadcasts it at `kPingHz` (default
   10, 0 = off), but only while CSI is enabled. Both receivers already ignore
   unknown types (`main.cpp` rx_task and `ranging.py` `_rx_task`), so we don't
   need to bump the protocol version. The seq lets the logger count lost
   packets.
2. **HT rate.** ESP-NOW goes out at 1 Mbps legacy by default, which gives only
   the LLTF field (64 subcarriers). Sending at MCS0 adds the HT-LTF field:
   - IDF 5.2.3: `esp_wifi_config_espnow_rate(WIFI_IF_AP, WIFI_PHY_RATE_MCS0_LGI)`.
     Check that it exists there; IDF 5.4+ has `esp_now_set_peer_rate_config`.
   - Check that range doesn't suffer: MCS0 needs a few dB more SNR than 1 Mbps.
     If edges at the far end of the room are lost, use HT only for pings and
     keep beacons at 1 Mbps.
3. **AGC (not done: per-packet normalisation only).** Automatic gain changes the amplitude scale on every packet. Look at
   the gain-compensation helpers in Espressif's `esp-csi` repo
   (`esp_csi_gain_ctrl_*`). If they work on IDF 5.2.3 with the S3/C3, log the
   gain with each packet. If not, normalise amplitude per packet offline and
   rely on spectral shape, not absolute level.

**Done when:** each live peer produces ≥ 8 CSI lines/s with HT-LTF present, and
FTM success rate and pass time are within 10 % of today's (compare
`ftm_monitor.py` summaries before and after).

## Phase 3: logging and labelled data

1. `tools/ftm_monitor.py`:
   - parse `CSI` lines; keep them out of the live tables except as a per-pair
     "CSI pkt/s" column
   - `--csi PATH`: write CSI to a sidecar file. Use one `.jsonl` per run (one
     record per line, simple to append), or `.npz` at exit if size becomes a
     problem. Each record holds `time`, `true_m`, `scene`, `from`, `to`, the
     `rx_ctrl` fields and decoded `int8` pairs.
   - `--scene LABEL`: stored on every CSV and CSI row (new CSV column at the end
     so old files still parse): `los`, `body` (wearer's back to the other node),
     `person` (someone standing between), `crowd`, `wall`, `walking`
   - host time on every row, so `DIST`/`RDIST` and `CSI` join by pair and a
     ±0.5 s window
2. **Data collection protocol** (one session, ~2 h):
   - distances 1, 2, 3.5, 5, 8, 12 m, if the space allows
   - at each distance: `los`, `body`, `person`, and `wall` where possible,
     60 s each
   - two `walking` runs: one where the node moves, one where people move
     between static nodes
   - include at least one badge (S3) in each set, not only C3 nodes, because
     body blocking is the badge case
   - record node height and orientation in a short `cal_notes.md` next to the
     data
3. Keep the raw data out of git if it gets large (> ~20 MB). Commit a
   `README` that says where it lives.

## Phase 4: offline analysis (go/no-go)

New `tools/csi_analysis.py` (run with `uv run --with numpy,pandas,matplotlib`):

1. Per packet: drop the guard and DC subcarriers, convert to complex, normalise.
   Features:
   - **amplitude flatness**: std/mean of |H| across subcarriers
     (frequency-selective fading means multipath)
   - **Rician K-factor estimate**, from the moments of |H|
   - **RMS delay spread and first-to-strongest tap ratio** from the IFFT,
     keeping in mind it is coarse at 20 MHz
   - **RSSI–distance residual**: CSI power vs. the free-space value for the
     FTM distance
2. Per pair per 1 s window: mean of each feature, plus **temporal variance**
   (correlation between consecutive normalised |H| vectors, 1 = static).
3. Targets: FTM error `dist_fine_cm/100 − true_m` (after the initiator bias),
   and the `scene` label.
4. Questions to answer:
   - Does a simple threshold or a logistic fit on 2–3 features separate `los`
     from `body`/`person`/`wall`? Report AUC.
   - How much of the |error| variance do the features explain? Report Spearman
     ρ per feature and R² of a small linear fit.
   - Does temporal variance separate `walking` from static scenes reliably
     within 2 s?
   - Is there a consistent extra bias for `body`? If so, how big is it, and is
     it predictable from the features?
5. **Go if:** LOS-vs-NLOS AUC ≥ 0.8, or features explain ≥ 30 % of the error
   variance, or motion is detected at ≥ 90 % within 2 s. Otherwise write up
   the result and stop.

Also try once, while the data setup is in place: FTM on an HT40 softAP (both
FTM and CSI resolution double). It's cheap to test, and it may beat
everything above.

## Phase 5: features on the badge

Only after phase 4 says go, and only the features that proved useful.

1. `lvgl_billboard_badge/sdkconfig.board`: `CONFIG_ESP_WIFI_CSI_ENABLED=y`.
   Measure the heap before and after (`lib/heap.py`). The Wi-Fi driver takes
   some internal RAM for CSI buffers.
2. `user_c_modules/ftm`, new `csi_core.cpp`:
   - CSI callback, filtered to MACs the Python side registers
     (`ftm.csi_peers([mac, ...])`, called when `ranging.py` updates
     `self.peers`)
   - per-peer running feature accumulators in C (Welford mean/var, previous
     |H| for temporal correlation). No raw CSI crosses into MicroPython.
   - `ftm.csi_stats(mac)` returns `(n, flat, k, spread, motion)` and resets the
     window, or None
   - fixed table of 32 peers, one spinlock. The callback does O(subcarriers)
     work and no allocation.
3. `badge/lib/ranging.py`:
   - attach the latest `csi_stats` to each result in `_record`
   - no broadcast of CSI features: result relaying between nodes is being
     removed, so each badge uses the CSI of its own links only
   - edge quality `q ∈ (0, 1]` from the phase 4 model; `graph()` returns it
     with each edge
   - adaptive history: keep up to 7 results per pair, and use the last 2 when
     `motion` is high, all 7 when low
4. `badge/lib/graph_layout.py` and `user_c_modules/graphlayout`: multiply the
   existing length weight by `q`. Keep the Python and C solvers in step, and
   extend the PC tests in the same change.
5. `badge/apps/ranging.py`: optionally draw low-`q` edges dashed or lighter,
   and show `q` instead of (or next to) the 5 dB RSSI.
6. Badges must send ESP-NOW at HT20 MCS0 (and send pings) so other badges
   get CSI from more than FTM frames. That's one `esp_now_set_peer_rate_config`
   call on the broadcast peer, from the `ftm` C module.

**Done when:** with the same nodes and scenes as phase 3, the map's RMS
position error against measured positions improves (target ≥ 25 %) in mixed
LOS/NLOS scenes. It must not get worse in pure LOS, and the badge's refresh
timings in `ranging.py` must not slip.

## Risks

- **CSI callback load slows FTM.** Measure in phase 1/2; the throttle and the
  peer filter are the levers.
- **HT-rate ESP-NOW shortens range.** Fall back to legacy for beacons, as in
  phase 2.
- **AGC makes amplitude features noisy.** Use normalised, shape-only features.
- **Features don't generalise between rooms.** Collect in at least two
  different spaces before trusting the phase 4 numbers.
- **Badge RAM.** Phase 5 adds a C accumulator table only, no Python-side
  buffers.

## Files touched (summary)

| Phase | Files |
|---|---|
| 1–2 | `ftm_node/sdkconfig.defaults`, `ftm_node/main/main.cpp`, `ftm_node/README.md` |
| 3 | `ftm_node/tools/ftm_monitor.py` |
| 4 | `ftm_node/tools/csi_analysis.py` (new) |
| 5 | `lvgl_billboard_badge/sdkconfig.board`, `user_c_modules/ftm/*` (+ `csi_core.cpp`), `badge/lib/ranging.py`, `badge/lib/graph_layout.py`, `user_c_modules/graphlayout/*`, `badge/apps/ranging.py` |
