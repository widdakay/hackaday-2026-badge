# Ranging protocol

Badges (ESP32-S3, `badge/lib/ranging.py`) and `ftm_node` boards (ESP32-C3, `ftm_node/main/main.cpp`) use the same protocol.

## Radio setup

Each device:

* Runs a hidden, open softAP on Wi-Fi channel 6. The SSID is the device name. The FTM responder is on.
* Sends and receives ESP-NOW broadcasts (`ff:ff:ff:ff:ff:ff`) on the AP interface. The ESP-NOW sender MAC is therefore the AP MAC, which is the address other devices range to.
* Turns STA power save off.

There is no association, no IP and no scanning. Devices find each other through ESP-NOW beacons only.

## Messages

All messages are ESP-NOW broadcasts, little-endian and packed. Receivers drop messages with the wrong magic or version, and messages shorter than their type requires.

Header, 6 bytes:

| Offset | Type | Field |
|---|---|---|
| 0 | char[4] | magic `BBFT` |
| 4 | u8 | version, `2` |
| 5 | u8 | type |

### BEACON (type 1), 26 bytes

| Offset | Type | Field |
|---|---|---|
| 6 | u32 | sequence number, starts at 1, +1 per beacon |
| 10 | char[16] | name, UTF-8, NUL-padded, not NUL-terminated if 16 bytes |

* Sent every 1000 ms by badges and nodes.
* Badge name: `name` in `/data/settings.json`, else `badge-XXXXXX`, from the last 3 bytes of the AP MAC. Node name: `node-XXXXXX`.
* A device that receives a beacon adds the sender as a peer, or refreshes its last-heard time.

### RANGE (type 2), 43 bytes

The result of one FTM session. The sender is the initiator, and `to` is the responder.

| Offset | Type | Field |
|---|---|---|
| 6 | u8[6] | `to`: responder AP MAC |
| 12 | u8 | status (`wifi_ftm_status_t`, 0 = success) |
| 13 | u8 | valid per-frame readings |
| 14 | i8 | RSSI of the responder's last ESP-NOW message, dBm |
| 15 | u32 | `dist_cm`: driver distance estimate |
| 19 | u32 | `rtt_raw_ns`: driver round trip, whole ns |
| 23 | u32 | `rtt_est_ns`: driver estimated round trip, whole ns |
| 27 | u32 | `rtt_med_ps`: median per-frame round trip |
| 31 | u32 | `rtt_mean_ps`: mean per-frame round trip |
| 35 | u32 | `rtt_min_ps` |
| 39 | u32 | `rtt_max_ps` |

* Sent only by `ftm_node` boards, once after every session, including failed ones.
* Badges do not send RANGE. They record received RANGE messages as measurements between the two nodes, which puts node-to-node pairs on the map.
* A RANGE message also counts as hearing the sender. It doesn't carry a name.

### SYNC (type 4), 14 bytes

The LED flash oscillator state. Only badges send and use SYNC. Nodes ignore it.

| Offset | Type | Field |
|---|---|---|
| 6 | u32 | cycle count |
| 10 | u16 | phase in the current cycle, units of 1/65536 cycle |
| 12 | u16 | current period, ms |

* Sent every 500 ms.
* Type 3 (NAMES, a name relay) was removed. Don't reuse it.

## Ranging loop

Each device runs one FTM initiator session at a time:

1. Drop peers not heard for 10 s. Drop measurements older than 30 s.
2. With no peers, wait for the first beacon.
3. Pick the next peer, round robin.
4. Start an FTM session to the peer's AP MAC on channel 6: 16 frames, burst period 0 (no preference). The session must finish within 1000 ms, or it is cancelled and logged as `timeout`. A session usually takes 100–300 ms.
5. Record the result, then start the next session at once. There is no gap between sessions.

A full pass ranges each peer once. With N peers, a pass takes about N × 0.1–0.3 s, plus 1 s for each timeout.

## Distance

Per session, from the per-frame round trips. Frames reported with an RTT of `UINT32_MAX` are skipped.

```
distance_cm = rtt_mean_ps × 0.0149896229      (falls back to dist_cm if the mean is 0)
corrected   = max(distance_cm − bias(initiator), 5 cm)
```

* The bias depends on the initiator's chip: badge 1.9 m, node 0 m. A peer is a node if its name starts with `node-`. Override with `"initiator_bias_m": {"badge": ..., "node": ...}` in `settings.json`.
* The last 3 results are kept per directed pair (A→B and B→A separately).
* List distance (this badge to a peer): the median of this badge's last 3 results to that peer.
* Map edge A–B: the median of the fresh results in both directions, up to 6 values.
* The map fits 2D positions to all edges (`badge/lib/graph_layout.py`, with a C solver in `user_c_modules/graphlayout`).

## LED sync (firefly)

`badge/lib/firefly.py`. Each badge runs a phase oscillator with a nominal period of 2000 ms and flashes when the phase wraps.

Every 500 ms, before sending SYNC:

1. For every badge with a SYNC received in the last 3 s *and* a measured distance, project its phase to now, using its reported period and the message's age.
2. Weight each badge by `1 / max(distance_m, 0.5)`.
3. `offset` = the weighted circular mean of (its phase − own phase), in cycles from −0.5 to 0.5.
4. `period = 2000 / (1 + 0.1 × offset)`, clamped to 1900–2100 ms.
5. If any badge's cycle count is ahead, jump to it, so the highest count wins.

SYNC messages with a period outside 1900–2100 ms are ignored. The cycle count picks the flash pattern (wave direction and colours), so synced badges show the same pattern.

## Serial output

Badges and nodes print the same lines on USB, so `ftm_node/tools/ftm_monitor.py` works with either:

| Line | When |
|---|---|
| `SELF,<mac>,<name>,<channel>` | at start, then every 10 beacons |
| `PEER,<mac>,<name>,<rssi>` | a peer is heard for the first time |
| `PEERS,<mac>,<name>,<rssi>,<ms since heard>` | every 10 beacons, one line per peer |
| `LOST,<mac>` | a peer is dropped after 10 s |
| `DIST,<from>,<to>,<status>,...` | an own session finishes |
| `RDIST,<from>,<to>,<status>,...` | a RANGE message is received |
| `PASS,<mac>,<peers>,<ms>` | a full pass finishes (badge only) |
| `FLY,<mac>,<cycle>,<period>,<offset ms>,<weight>` | the LED cycle advances (badge only) |

For `DIST` and `RDIST`, `<status>` is `ok`, `fail_<n>`, `timeout` or `start_err`. On `ok`, the line continues with `dist_cm, rtt_raw_ns, rtt_est_ns, rssi, entries, rtt_med_ps, rtt_mean_ps, rtt_min_ps, rtt_max_ps`.
