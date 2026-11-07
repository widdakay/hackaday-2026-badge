#!/usr/bin/env python3
"""Live FTM ranging stats from an ftm_node board on USB, with CSV logging.

Usage:
    uv run --with pyserial firmware/ftm_node/tools/ftm_monitor.py --true-m 3.0 --csv cal.csv
    uv run --with pyserial firmware/ftm_node/tools/ftm_monitor.py --port /dev/cu.usbmodem1101

Every measurement is logged, both the ones the USB board made (DIST) and the
ones other boards made and broadcast over ESP-NOW (RDIST). Each CSV row gets
the --true-m label so calibration runs at different distances can be combined.
Stats are per directed pair (initiator -> responder) over the last --window
seconds. Ctrl-C prints a final summary over the whole run.
"""

import argparse
import csv
import glob
import statistics
import sys
import time
from collections import defaultdict, deque

import serial

FIELDS = ["time", "true_m", "source", "from", "to", "status", "dist_cm", "rtt_raw_ns", "rtt_est_ns", "rssi", "entries",
          "rtt_med_ps", "rtt_mean_ps", "rtt_min_ps", "rtt_max_ps", "dist_fine_cm"]
PS_TO_ONE_WAY_CM = 299792458e2 * 1e-12 / 2  # cm per picosecond of round trip


def fine_cm(rtt_mean_ps):
    """One-way distance from the mean per-frame round trip in ps.

    Per-frame round trips come in 1.5625 ns steps (640 MHz clock) and negative
    ones are clamped to 0, so the median is useless at short range. The mean
    is what the driver reports too, but without its rounding to whole ns.
    """
    return int(rtt_mean_ps) * PS_TO_ONE_WAY_CM if rtt_mean_ps else None


def pick_port(port):
    if port:
        return port
    ports = sorted(glob.glob("/dev/cu.usbmodem*"))
    if len(ports) != 1:
        sys.exit(f"Found {len(ports) or 'no'} USB serial ports {ports}; pass --port")
    return ports[0]


def summarize(samples):
    """samples: list of (status, dist_cm). Returns a stats dict or None."""
    ok = sorted(d / 100 for s, d in samples if s == "ok")
    if not samples:
        return None
    out = {"n": len(samples), "ok": len(ok), "ok_pct": 100 * len(ok) / len(samples)}
    if ok:
        out.update(
            median=statistics.median(ok),
            mean=statistics.fmean(ok),
            std=statistics.pstdev(ok) if len(ok) > 1 else 0.0,
            p10=ok[int(0.1 * (len(ok) - 1))],
            p90=ok[int(0.9 * (len(ok) - 1))],
        )
    return out


def print_table(title, groups, names, true_m, node=None):
    print(f"\n{title}")
    print(f"  {'from':<12} -> {'to':<12} {'n':>4} {'ok%':>5} {'median':>7} {'mean':>6} {'std':>5} {'p10':>5} {'p90':>5}"
          + ("   err(median)" if true_m is not None else ""))
    for (frm, to), samples in sorted(groups.items()):
        st = summarize(list(samples))
        if not st:
            continue
        line = f"  {names.get(frm, frm[-8:]):<12} -> {names.get(to, to[-8:]):<12} {st['n']:>4} {st['ok_pct']:>4.0f}%"
        if st["ok"]:
            line += f" {st['median']:>6.2f}m {st['mean']:>5.2f}m {st['std']:>4.2f}m {st['p10']:>4.2f}m {st['p90']:>4.2f}m"
            if true_m is not None and (node is None or node.lower() in
                                       (frm, to, names.get(frm, "").lower(), names.get(to, "").lower())):
                line += f"   {st['median'] - true_m:+.2f}m"
        print(line)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial port (default: the only /dev/cu.usbmodem*)")
    ap.add_argument("--true-m", type=float, help="actual distance in meters, for calibration runs")
    ap.add_argument("--node", help="only label pairs involving this node (name like node-D2F7AD, or AP MAC); "
                                   "other pairs are logged without a true distance")
    ap.add_argument("--csv", help="append every measurement to this CSV file")
    ap.add_argument("--window", type=float, default=30, help="seconds of data in the live stats (default 30)")
    ap.add_argument("--every", type=float, default=5, help="seconds between live tables (default 5)")
    ap.add_argument("--duration", type=float, help="stop after this many seconds")
    ap.add_argument("--quiet", action="store_true", help="no live tables, only the final summary")
    args = ap.parse_args()

    port = pick_port(args.port)
    ser = serial.Serial(port, 115200, timeout=0.5)
    print(f"Listening on {port}" + (f", true distance {args.true_m} m" if args.true_m is not None else ""))

    writer = None
    if args.csv:
        f = open(args.csv, "a", newline="")
        writer = csv.writer(f)
        if f.tell() == 0:
            writer.writerow(FIELDS)

    names = {}                                   # ap_mac -> short name
    recent = defaultdict(deque)                  # (from, to) -> deque[(t, status, cm)]
    everything = defaultdict(list)               # (from, to) -> [(status, cm)]
    start = last_print = time.time()
    buf = b""
    try:
        while args.duration is None or time.time() - start < args.duration:
            buf += ser.read(4096)
            *lines, buf = buf.split(b"\n")
            now = time.time()
            for raw in lines:
                line = raw.decode(errors="replace").strip()
                parts = line.split(",")
                if parts[0] in ("SELF", "PEER", "PEERS") and len(parts) >= 3:
                    if parts[0] != "SELF" and parts[1] not in names:
                        parts[0] = "PEER"  # first time we see it, announce it
                    names[parts[1]] = parts[2]
                    if parts[0] == "PEER":
                        print(f"  discovered {parts[2]} ({parts[1]}) rssi {parts[3] if len(parts) > 3 else '?'}")
                elif parts[0] == "LOST":
                    print(f"  lost {names.get(parts[1], parts[1])}")
                elif parts[0] in ("DIST", "RDIST") and len(parts) >= 13:
                    _, frm, to, status, cm, rtt_raw, rtt_est, rssi, entries, med, mean, lo, hi = parts[:13]
                    fine = fine_cm(mean) if status == "ok" else None
                    dist = fine if fine is not None else (int(cm) if status == "ok" and cm else None)
                    recent[(frm, to)].append((now, status, dist))
                    everything[(frm, to)].append((status, dist))
                    if writer:
                        labeled = args.true_m is not None and (
                            args.node is None or any(args.node.lower() in (m, names.get(m, "").lower())
                                                     for m in (frm, to)))
                        writer.writerow([f"{now:.3f}", args.true_m if labeled else "",
                                         parts[0], frm, to, status, cm, rtt_raw, rtt_est, rssi, entries,
                                         med, mean, lo, hi, f"{fine:.1f}" if fine is not None else ""])
                elif parts[0] in ("DIST", "RDIST"):
                    print(f"  unexpected line (old firmware?): {line}")
                elif line.startswith(("E (", "abort", "Guru", "assert")):
                    print(f"  board: {line}")
            for q in recent.values():
                while q and now - q[0][0] > args.window:
                    q.popleft()
            if not args.quiet and now - last_print >= args.every:
                last_print = now
                print_table(f"Last {args.window:.0f}s", {k: [(s, d) for _, s, d in q] for k, q in recent.items()},
                            names, args.true_m, args.node)
                if writer:
                    f.flush()
    except KeyboardInterrupt:
        pass
    print_table(f"Whole run ({time.time() - start:.0f}s)", everything, names, args.true_m, args.node)
    if writer:
        f.close()
        print(f"\nAppended to {args.csv}")


if __name__ == "__main__":
    main()
