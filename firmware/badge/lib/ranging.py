"""Wi-Fi FTM ranging service: discovery over ESP-NOW, distances over FTM.

Speaks the same protocol as firmware/ftm_node (the ESP32-C3 nodes), so badges
and C3 nodes discover and range each other:

  * a hidden softAP on CHANNEL with the FTM responder on (C++ `ftm` module)
  * an ESP-NOW beacon every second with this badge's name (lib.settings), sent
    on the AP interface, so the sender MAC is the AP MAC the responder answers on
  * a ranging loop over every node heard in the last PEER_TIMEOUT_MS
  * no retransmission: a badge does not broadcast its own results or relay
    names. It still collects the results ftm_node boards broadcast (RDIST),
    so their pairs appear in the map alongside this badge's own.
  * a SYNC broadcast of the firefly LED oscillator (lib.firefly) every
    firefly.SYNC_MS; badges only, the C3 nodes ignore it

It prints the same serial lines as ftm_node (SELF, PEER, PEERS, LOST, DIST,
RDIST), so firmware/ftm_node/tools/ftm_monitor.py works with a badge on USB.

    from lib.ranging import service
    service.start()             # idempotent; runs in the background
    service.distance(mac)       # median metres from this badge, or None
    service.graph()             # every node and edge with fresh results
    service.firefly             # lib.firefly.Firefly, synced with the badges nearby
"""

import asyncio as aio
from asyncio import core as _aio_core
import io
import struct
import sys
import time

import aioespnow
import network

import ftm

from lib import firefly, log, settings

CHANNEL = 6
FRAMES = 16                # smallest fixed count the driver accepts
BURST_PERIOD = 0           # no preference: fastest (176 ms vs 316 ms at 2); 1 is rejected (ESP_ERR_INVALID_ARG)
BEACON_MS = 1000
SESSION_TIMEOUT_MS = 1000  # a session takes ~100-300 ms; a peer busy ranging
                           # someone else may not answer, so give up quickly
PEER_TIMEOUT_MS = 10000    # stop ranging a node not heard for this long
RESULT_TIMEOUT_MS = 30000  # ignore and drop measurements older than this

# Range bias by the chip that initiated the measurement, subtracted from every
# raw reading before medians, edges and the map. Measured on 2,382 readings
# between 3 badges (ESP32-S3) and 5 ftm_node boards (ESP32-C3): on the same
# pair, a badge-initiated reading is 1.94 m (median, 15 pairs) longer than a
# C3-initiated one, and C3-initiated readings go down to ~0 m for nodes side
# by side, while badge-initiated ones bottom out around 0.5-1.8 m. So the C3
# side is taken as unbiased. Override in data/settings.json, e.g.
#   {"initiator_bias_m": {"badge": 1.9, "node": 0.0}}
# and calibrate against a tape measure with ftm_node/tools/ftm_monitor.py.
INITIATOR_BIAS_M = {"badge": 1.9, "node": 0.0}
MIN_DISTANCE_CM = 5        # corrected readings are clamped to at least this
HISTORY = 3                # results kept per directed pair, for the median.
                           # Each pair gets one result per pass (one FTM session per
                           # peer, ~0.1-0.3 s each), so the median spans HISTORY passes.

_MAGIC = b"BBFT"
_VERSION = 2
_BEACON = 1
_RANGE = 2
_BEACON_FMT = "<4sBBI16s"          # header, seq, name
_RANGE_FMT = "<4sBB6sBBbIIIIIII"   # header, to, status, entries, rssi, 7 x u32
_BEACON_LEN = struct.calcsize(_BEACON_FMT)
_RANGE_LEN = struct.calcsize(_RANGE_FMT)
_SYNC = 4                          # 3 was NAMES (removed; keep it unused)
_SYNC_FMT = "<4sBBIHH"             # header, firefly cycles, phase (1/65536), period ms
_SYNC_LEN = struct.calcsize(_SYNC_FMT)
_BROADCAST = b"\xff" * 6
PS_TO_CM = 0.0149896229    # one-way cm per picosecond of round trip


def mac_str(mac):
    return ":".join("%02x" % b for b in mac)


def _median(vals):
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


class _FtmReady(io.IOBase):
    """Pollable by asyncio: readable once the FTM session has a result. The
    C module wakes the main task when the result lands, so this fires at once
    rather than on a timer (same mechanism as asyncio.ThreadSafeFlag)."""

    def ioctl(self, req, flags):
        if req == 3:  # MP_STREAM_POLL
            return flags if ftm.ready() else 0
        return -1

    async def wait(self):
        if not ftm.ready():
            yield _aio_core._io_queue.queue_read(self)


class Peer:
    """A node this badge hears directly (and ranges)."""

    def __init__(self, mac):
        self.mac = mac
        self.rssi = 0
        self.last_ms = 0


class RangingService:
    def __init__(self):
        self.started = False
        self.mac = b""
        self.name = ""
        self.peers = {}       # ap_mac -> Peer, nodes heard directly
        self.names = {}       # ap_mac -> name, from beacons
        self.numbers = {}     # ap_mac -> display number, 0 = this badge
        self.results = {}     # (from_mac, to_mac) -> [(cm, ticks_ms), ...]
        self.latest_rtt = {}  # (from_mac, to_mac) -> (round trip ps, ticks_ms)
        self.updated_ms = 0   # last time any distance changed
        self.bias_cm = {k: v * 100 for k, v in INITIATOR_BIAS_M.items()}
        self._next_number = 1
        # Pass timing: how long one round over every live peer takes, so the
        # app can say how much time the median covers (HISTORY passes).
        self.pass_ms = None   # smoothed time to range every peer once
        self.pass_peers = 0   # peers in the last full pass
        self.passes = 0       # full passes completed; the app redraws when it grows
        self.firefly = firefly.Firefly(self.distance)
        self._ftm_ready = _FtmReady()
        self._new_peer = aio.Event()   # set when a node is first heard
        self._espnow = None
        self._tasks = []

    # ---- public -------------------------------------------------------------
    def start(self):
        if self.started:
            return
        ap = network.WLAN(network.AP_IF)
        ap.active(True)
        self.mac = bytes(ap.config("mac"))
        self.name = settings.badge_name(None) or settings.default_name()
        bias = dict(INITIATOR_BIAS_M)
        bias.update(settings.load().get("initiator_bias_m") or {})
        self.bias_cm = {k: float(v) * 100 for k, v in bias.items()}
        self.numbers[self.mac] = 0
        self.names[self.mac] = self.name
        ap.config(essid=self.name, channel=CHANNEL, hidden=True, authmode=network.AUTH_OPEN)
        sta = network.WLAN(network.STA_IF)
        sta.active(True)
        try:
            sta.config(pm=sta.PM_NONE)
        except Exception:
            pass
        ftm.responder(True)

        e = aioespnow.AIOESPNow()
        e.active(True)
        e.add_peer(_BROADCAST, channel=CHANNEL, ifidx=network.AP_IF)
        self._espnow = e

        self._tasks = [aio.create_task(t()) for t in (self._beacon_task, self._rx_task, self._range_task, self._sync_task)]
        self.started = True
        print("SELF,%s,%s,%d" % (mac_str(self.mac), self.name, CHANNEL))
        log.info("ranging: started as %s %s" % (self.name, mac_str(self.mac)))

    def name_of(self, mac):
        return self.names.get(mac) or mac_str(mac)[-8:]

    def number_of(self, mac):
        """Stable display number: assigned the first time a node turns up and
        kept for the rest of the session, so the list and graph don't renumber."""
        n = self.numbers.get(mac)
        if n is None:
            n = self.numbers[mac] = self._next_number
            self._next_number += 1
        return n

    def distance(self, to_mac, from_mac=None):
        """Median metres from `from_mac` (default: this badge) to `to_mac` over
        the recent results of that one direction; None if nothing fresh."""
        vals = self._fresh(from_mac or self.mac, to_mac)
        return _median(vals) / 100 if vals else None

    def edge(self, a, b):
        """Metres between a and b using both directions (A ranging B and B
        ranging A are separate measurements); None if neither is fresh."""
        vals = self._fresh(a, b) + self._fresh(b, a)
        return _median(vals) / 100 if vals else None

    def latest(self, to_mac, from_mac=None):
        """(round trip in ns, age in ms) of the newest successful result from
        `from_mac` (default: this badge) to `to_mac`; None if nothing yet."""
        r = self.latest_rtt.get((from_mac or self.mac, to_mac))
        if r is None:
            return None
        return r[0] / 1000, time.ticks_diff(time.ticks_ms(), r[1])

    def live_peers(self):
        """Peers heard recently, nearest first (unknown distances last)."""
        now = time.ticks_ms()
        live = [p for p in self.peers.values() if time.ticks_diff(now, p.last_ms) < PEER_TIMEOUT_MS]
        live.sort(key=lambda p: (self.distance(p.mac) is None, self.distance(p.mac) or 0))
        return live

    def graph(self):
        """(nodes, edges) for everything with fresh information.

        nodes: [mac, ...] sorted by display number, this badge first. A node
               is included if it was heard directly in the last
               PEER_TIMEOUT_MS or appears in a fresh measurement.
        edges: {(mac_a, mac_b): metres} with mac_a < mac_b.
        """
        now = time.ticks_ms()
        macs = {self.mac}
        for p in self.peers.values():
            if time.ticks_diff(now, p.last_ms) < PEER_TIMEOUT_MS:
                macs.add(p.mac)
        edges = {}
        for (a, b) in self.results:
            if a == b:
                continue
            key = (a, b) if a < b else (b, a)
            if key in edges:
                continue
            d = self.edge(a, b)
            if d is not None:
                edges[key] = d
                macs.add(a)
                macs.add(b)
        nodes = sorted(macs, key=self.number_of)
        return nodes, edges

    # ---- internals ----------------------------------------------------------
    def kind_of(self, mac):
        """'node' for ftm_node boards (ESP32-C3, always named node-XXXXXX),
        'badge' for everything else, including this badge."""
        if mac == self.mac:
            return "badge"
        return "node" if self.names.get(mac, "").startswith("node-") else "badge"

    def _fresh(self, frm, to):
        """Recent readings from frm to to, in cm, corrected for frm's bias."""
        now = time.ticks_ms()
        bias = self.bias_cm.get(self.kind_of(frm), 0.0)
        return [max(cm - bias, MIN_DISTANCE_CM) for cm, t in self.results.get((frm, to), ())
                if time.ticks_diff(now, t) < RESULT_TIMEOUT_MS]

    def _heard(self, mac, name, rssi):
        p = self.peers.get(mac)
        if p is None:
            p = self.peers[mac] = Peer(mac)
            if name:
                self.names[mac] = name
            self.number_of(mac)
            self._new_peer.set()
            print("PEER,%s,%s,%d" % (mac_str(mac), self.name_of(mac), rssi))
        elif name:
            self.names[mac] = name
        p.rssi = rssi
        p.last_ms = time.ticks_ms()

    def _record(self, tag, frm, to, r):
        """r: (status, entries, rssi, dist_cm, rtt_raw, rtt_est, med, mean, lo, hi)"""
        status, entries, rssi, dist_cm, rtt_raw, rtt_est, med, mean, lo, hi = r
        if status == ftm.STATUS_OK:
            print("%s,%s,%s,ok,%d,%d,%d,%d,%d,%d,%d,%d,%d" % (
                tag, mac_str(frm), mac_str(to), dist_cm, rtt_raw, rtt_est, rssi, entries, med, mean, lo, hi))
            now = time.ticks_ms()
            cm = mean * PS_TO_CM if mean else dist_cm
            self.latest_rtt[(frm, to)] = (mean if mean else rtt_raw * 1000, now)
            vals = self.results.setdefault((frm, to), [])
            vals.append((cm, now))
            if len(vals) > HISTORY:
                vals.pop(0)
            self.number_of(frm)
            self.number_of(to)
            self.updated_ms = now
        else:
            print("%s,%s,%s,fail_%d,,,,%d,0,,,," % (tag, mac_str(frm), mac_str(to), status, rssi))

    def _prune(self):
        now = time.ticks_ms()
        for mac, p in list(self.peers.items()):
            if time.ticks_diff(now, p.last_ms) > PEER_TIMEOUT_MS:
                print("LOST,%s" % mac_str(mac))
                del self.peers[mac]
        for key, vals in list(self.results.items()):
            if time.ticks_diff(now, vals[-1][1]) > RESULT_TIMEOUT_MS:
                del self.results[key]
                self.latest_rtt.pop(key, None)

    def _rssi_of(self, mac):
        try:
            return self._espnow.peers_table[mac][0]
        except (KeyError, IndexError, TypeError):
            return 0

    def _send(self, msg, what):
        try:
            self._espnow.send(_BROADCAST, msg, False)
        except OSError as ex:
            print("ranging: %s send failed" % what, ex)
            log.error("ranging: %s send failed" % what, ex)

    async def _beacon_task(self):
        seq = 0
        name = self.name.encode()[:16]
        while True:
            seq += 1
            self._send(struct.pack(_BEACON_FMT, _MAGIC, _VERSION, _BEACON, seq, name), "beacon")
            if seq % 10 == 1:
                print("SELF,%s,%s,%d" % (mac_str(self.mac), self.name, CHANNEL))
                now = time.ticks_ms()
                for p in self.peers.values():
                    print("PEERS,%s,%s,%d,%d" % (mac_str(p.mac), self.name_of(p.mac), p.rssi,
                                                 time.ticks_diff(now, p.last_ms)))
            await aio.sleep_ms(BEACON_MS)

    async def _rx_task(self):
        e = self._espnow
        while True:
            try:
                mac, msg = await e.arecv()
                if not msg or len(msg) < 6 or msg[:4] != _MAGIC or msg[4] != _VERSION:
                    continue
                mac = bytes(mac)
                rssi = self._rssi_of(mac)
                kind = msg[5]
                if kind == _BEACON and len(msg) >= _BEACON_LEN:
                    name = struct.unpack(_BEACON_FMT, msg[:_BEACON_LEN])[4].split(b"\x00")[0].decode()
                    self._heard(mac, name, rssi)
                elif kind == _RANGE and len(msg) >= _RANGE_LEN:
                    f = struct.unpack(_RANGE_FMT, msg[:_RANGE_LEN])
                    self._heard(mac, None, rssi)
                    self._record("RDIST", mac, bytes(f[3]), f[4:])
                elif kind == _SYNC and len(msg) >= _SYNC_LEN:
                    f = struct.unpack(_SYNC_FMT, msg[:_SYNC_LEN])
                    self._heard(mac, None, rssi)
                    self.firefly.heard(mac, f[3], f[4] / 65536, f[5])
            except Exception as ex:
                print("ranging: rx error", ex)
                sys.print_exception(ex)
                log.error("ranging: rx error", ex)
                await aio.sleep_ms(100)

    async def _range_one(self, peer):
        try:
            ftm.start(peer.mac, CHANNEL, frames=FRAMES, burst=BURST_PERIOD)
        except OSError as ex:
            print("DIST,%s,%s,start_err,,,,,0,,,," % (mac_str(self.mac), mac_str(peer.mac)))
            log.error("ranging: ftm.start", ex)
            return
        try:
            await aio.wait_for_ms(self._ftm_ready.wait(), SESSION_TIMEOUT_MS)
        except aio.TimeoutError:
            ftm.cancel()
            print("DIST,%s,%s,timeout,,,,,0,,,," % (mac_str(self.mac), mac_str(peer.mac)))
            return
        res = ftm.poll()
        if res is None:
            return
        peer_mac, status, entries, dist_cm, rtt_raw, rtt_est, med, mean, lo, hi = res
        if peer_mac != peer.mac:
            return  # late result from a cancelled session
        r = (status, entries, peer.rssi, dist_cm, rtt_raw, rtt_est, med, mean, lo, hi)
        self._record("DIST", self.mac, peer.mac, r)

    async def _sync_task(self):
        """Broadcast the firefly phase and retune toward the badges nearby."""
        fly = self.firefly
        last_cycle = None
        while True:
            try:
                fly.adjust()
                cycles, phase, period = fly.state()
                self._send(struct.pack(
                    _SYNC_FMT, _MAGIC, _VERSION, _SYNC, cycles & 0xFFFFFFFF,
                    int(phase * 65536) & 0xFFFF, int(period)), "sync")
                if cycles != last_cycle:
                    # FLY,<self mac>,<cycle>,<period ms>,<offset ms>,<weight>:
                    # offset is how far the weighted mean of the others is
                    # ahead (+) or behind; it shrinks toward 0 as they sync.
                    print("FLY,%s,%d,%d,%d,%.2f" % (mac_str(self.mac), cycles, period,
                                                    fly.offset * firefly.PERIOD_MS, fly.weight))
                    last_cycle = cycles
            except Exception as ex:
                print("ranging: sync error", ex)
                log.error("ranging: sync error", ex)
            await aio.sleep_ms(firefly.SYNC_MS)

    def _pass_done(self, ms, n):
        """One full pass (n sessions, ms long, gaps and timeouts included).

        Prints PASS,<self mac>,<peers>,<ms> on serial. A pass grows with the
        number of peers, and with timeouts when many nodes share the channel."""
        self.pass_peers = n
        self.passes += 1
        # Light smoothing: one slow session should not jump the footer.
        self.pass_ms = ms if self.pass_ms is None else (3 * self.pass_ms + ms) / 4
        print("PASS,%s,%d,%d" % (mac_str(self.mac), n, ms))

    async def _range_task(self):
        idx = 0
        pass_t0 = None   # start of the current pass
        pass_left = 0    # sessions left in it
        pass_n = 0
        while True:
            self._prune()
            now = time.ticks_ms()
            peers = list(self.peers.values())
            if not peers:
                pass_t0 = None
                self._new_peer.clear()
                await self._new_peer.wait()
                continue
            if pass_left <= 0:
                if pass_t0 is not None:
                    self._pass_done(time.ticks_diff(now, pass_t0), pass_n)
                pass_t0 = now
                pass_left = pass_n = len(peers)
            pass_left -= 1
            idx = (idx + 1) % len(peers)
            try:
                await self._range_one(peers[idx])
            except Exception as ex:
                print("ranging: range error", ex)
                sys.print_exception(ex)
                log.error("ranging: range error", ex)
            # No gap between sessions; only yield, so a session that fails
            # without awaiting can't starve the beacon, rx and display tasks.
            await aio.sleep_ms(0)


service = RangingService()
