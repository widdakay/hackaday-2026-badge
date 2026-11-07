"""Wi-Fi FTM ranging service: discovery over ESP-NOW, distances over FTM.

Speaks the same protocol as firmware/ftm_node (the ESP32-C3 nodes), so badges
and C3 nodes discover and range each other:

  * a hidden softAP on CHANNEL with the FTM responder on (C++ `ftm` module)
  * an ESP-NOW beacon every second (sent on the AP interface, so the sender
    MAC is the AP MAC the FTM responder answers on)
  * a ranging loop over every node heard in the last PEER_TIMEOUT_MS
  * an ESP-NOW broadcast of every result, and the results other nodes
    broadcast are collected too (all-to-all table)

It prints the same serial lines as ftm_node (SELF, PEER, PEERS, LOST, DIST,
RDIST), so firmware/ftm_node/tools/ftm_monitor.py works with a badge on USB.

    from lib.ranging import service
    service.start()            # idempotent; runs in the background
    service.peers              # {ap_mac: Peer}
    service.distance(mac)      # median metres over recent results, or None
    service.latest(mac)        # (round trip ns, age ms) of the newest result, or None
"""

import asyncio as aio
import struct
import sys
import time

import aioespnow
import network

import ftm

CHANNEL = 6
FRAMES = 16
BURST_PERIOD = 2           # 100 ms units
BEACON_MS = 1000
RANGE_GAP_MS = 150
SESSION_TIMEOUT_MS = 3000
PEER_TIMEOUT_MS = 10000
HISTORY = 3                # results kept per directed pair, for the median.
                           # Each pair gets one result per pass (~0.4-0.6 s x number of
                           # peers), so the median spans HISTORY passes: 3 rather than 5
                           # keeps it responsive, ~8 s with 5 peers, ~30 s with 20.

_MAGIC = b"BBFT"
_VERSION = 2
_BEACON = 1
_RANGE = 2
_BEACON_FMT = "<4sBBI16s"          # header, seq, name
_RANGE_FMT = "<4sBB6sBBbIIIIIII"   # header, to, status, entries, rssi, 7 x u32
_BEACON_LEN = struct.calcsize(_BEACON_FMT)
_RANGE_LEN = struct.calcsize(_RANGE_FMT)
_BROADCAST = b"\xff" * 6
PS_TO_CM = 0.0149896229    # one-way cm per picosecond of round trip


def mac_str(mac):
    return ":".join("%02x" % b for b in mac)


class Peer:
    def __init__(self, mac):
        self.mac = mac
        self.name = mac_str(mac)[-8:]
        self.rssi = 0
        self.last_ms = 0


class RangingService:
    def __init__(self):
        self.started = False
        self.mac = b""
        self.name = ""
        self.peers = {}       # ap_mac -> Peer
        self.results = {}     # (from_mac, to_mac) -> list of recent cm values
        self.latest_rtt = {}  # (from_mac, to_mac) -> (round trip ps, ticks_ms)
        self.updated_ms = 0   # last time any distance changed
        # Pass timing: how long one round over every live peer takes, so the
        # app can say how much time the median covers (HISTORY passes).
        self.pass_ms = None   # smoothed time to range every peer once
        self.pass_peers = 0   # peers in the last full pass
        self.passes = 0       # full passes completed; the app redraws when it grows
        self._espnow = None
        self._tasks = []

    # ---- public -------------------------------------------------------------
    def start(self):
        if self.started:
            return
        ap = network.WLAN(network.AP_IF)
        ap.active(True)
        self.mac = ap.config("mac")
        self.name = "badge-%02X%02X%02X" % (self.mac[3], self.mac[4], self.mac[5])
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

        self._tasks = [aio.create_task(t()) for t in (self._beacon_task, self._rx_task, self._range_task)]
        self.started = True
        print("SELF,%s,%s,%d" % (mac_str(self.mac), self.name, CHANNEL))

    def distance(self, to_mac, from_mac=None):
        """Median distance in metres from `from_mac` (default: this badge) to
        `to_mac`, over the last HISTORY results; None if nothing yet."""
        vals = self.results.get((from_mac or self.mac, to_mac))
        if not vals:
            return None
        s = sorted(vals)
        return s[len(s) // 2] / 100

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

    # ---- internals ----------------------------------------------------------
    def _heard(self, mac, name, rssi):
        p = self.peers.get(mac)
        if p is None:
            p = self.peers[mac] = Peer(mac)
            if name:
                p.name = name
            print("PEER,%s,%s,%d" % (mac_str(mac), p.name, rssi))
        elif name:
            p.name = name
        p.rssi = rssi
        p.last_ms = time.ticks_ms()

    def _record(self, tag, frm, to, r):
        """r: (status, entries, rssi, dist_cm, rtt_raw, rtt_est, med, mean, lo, hi)"""
        status, entries, rssi, dist_cm, rtt_raw, rtt_est, med, mean, lo, hi = r
        if status == ftm.STATUS_OK:
            print("%s,%s,%s,ok,%d,%d,%d,%d,%d,%d,%d,%d,%d" % (
                tag, mac_str(frm), mac_str(to), dist_cm, rtt_raw, rtt_est, rssi, entries, med, mean, lo, hi))
            cm = mean * PS_TO_CM if mean else dist_cm
            self.latest_rtt[(frm, to)] = (mean if mean else rtt_raw * 1000, time.ticks_ms())
            vals = self.results.setdefault((frm, to), [])
            vals.append(cm)
            if len(vals) > HISTORY:
                vals.pop(0)
            self.updated_ms = time.ticks_ms()
        else:
            print("%s,%s,%s,fail_%d,,,,%d,0,,,," % (tag, mac_str(frm), mac_str(to), status, rssi))

    def _rssi_of(self, mac):
        try:
            return self._espnow.peers_table[mac][0]
        except (KeyError, IndexError, TypeError):
            return 0

    async def _beacon_task(self):
        seq = 0
        name = self.name.encode()[:16]
        while True:
            seq += 1
            try:
                self._espnow.send(_BROADCAST, struct.pack(_BEACON_FMT, _MAGIC, _VERSION, _BEACON, seq, name), False)
            except OSError as ex:
                print("ranging: beacon send failed", ex)
            if seq % 10 == 1:
                print("SELF,%s,%s,%d" % (mac_str(self.mac), self.name, CHANNEL))
                now = time.ticks_ms()
                for p in self.peers.values():
                    print("PEERS,%s,%s,%d,%d" % (mac_str(p.mac), p.name, p.rssi, time.ticks_diff(now, p.last_ms)))
            await aio.sleep_ms(BEACON_MS)

    async def _rx_task(self):
        e = self._espnow
        while True:
            try:
                mac, msg = await e.arecv()
                if not msg or len(msg) < 6 or msg[:4] != _MAGIC or msg[4] != _VERSION:
                    continue
                rssi = self._rssi_of(mac)
                if msg[5] == _BEACON and len(msg) >= _BEACON_LEN:
                    name = struct.unpack(_BEACON_FMT, msg[:_BEACON_LEN])[4].split(b"\x00")[0].decode()
                    self._heard(bytes(mac), name, rssi)
                elif msg[5] == _RANGE and len(msg) >= _RANGE_LEN:
                    f = struct.unpack(_RANGE_FMT, msg[:_RANGE_LEN])
                    self._heard(bytes(mac), None, rssi)
                    self._record("RDIST", bytes(mac), bytes(f[3]), f[4:])
            except Exception as ex:
                print("ranging: rx error", ex)
                sys.print_exception(ex)
                await aio.sleep_ms(100)

    async def _range_one(self, peer):
        try:
            ftm.start(peer.mac, CHANNEL, frames=FRAMES, burst=BURST_PERIOD)
        except OSError as ex:
            print("DIST,%s,%s,start_err,,,,,0,,,," % (mac_str(self.mac), mac_str(peer.mac)))
            return
        t0 = time.ticks_ms()
        while True:
            res = ftm.poll()
            if res is not None:
                break
            if time.ticks_diff(time.ticks_ms(), t0) > SESSION_TIMEOUT_MS:
                ftm.cancel()
                print("DIST,%s,%s,timeout,,,,,0,,,," % (mac_str(self.mac), mac_str(peer.mac)))
                return
            await aio.sleep_ms(20)
        peer_mac, status, entries, dist_cm, rtt_raw, rtt_est, med, mean, lo, hi = res
        if peer_mac != peer.mac:
            return  # late result from a cancelled session
        r = (status, entries, peer.rssi, dist_cm, rtt_raw, rtt_est, med, mean, lo, hi)
        self._record("DIST", self.mac, peer.mac, r)
        try:
            self._espnow.send(_BROADCAST, struct.pack(
                _RANGE_FMT, _MAGIC, _VERSION, _RANGE, peer.mac, status, entries, peer.rssi,
                dist_cm, rtt_raw, rtt_est, med, mean, lo, hi), False)
        except OSError as ex:
            print("ranging: result send failed", ex)

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
            now = time.ticks_ms()
            for mac, p in list(self.peers.items()):
                if time.ticks_diff(now, p.last_ms) > PEER_TIMEOUT_MS:
                    print("LOST,%s" % mac_str(mac))
                    del self.peers[mac]
            peers = list(self.peers.values())
            if not peers:
                pass_t0 = None
                await aio.sleep_ms(500)
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
            await aio.sleep_ms(RANGE_GAP_MS)


service = RangingService()
