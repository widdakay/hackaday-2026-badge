#!/usr/bin/env python3
"""Live FTM ranging stats from an ftm_node board on USB, with CSV logging.

Usage:
    uv run --with pyserial firmware/ftm_node/tools/ftm_monitor.py --true-m 3.0 --csv cal.csv
    uv run --with pyserial firmware/ftm_node/tools/ftm_monitor.py --port /dev/cu.usbmodem1101
    uv run --with pyserial firmware/ftm_node/tools/ftm_monitor.py --true-m 3.0 --scene body \
        --csv csi_cal.csv --csi csi_cal.jsonl --duration 60

Every measurement is logged, both the ones the USB board made (DIST) and the
ones other boards made and broadcast over ESP-NOW (RDIST). Each CSV row gets
the --true-m label so calibration runs at different distances can be combined.
Stats are per directed pair (initiator -> responder) over the last --window
seconds. Ctrl-C prints a final summary over the whole run.

With --csi, every CSI line from the board (firmware with kCsiEnabled, see
firmware/CSI_PLAN.md) is appended to a JSON-lines file, one packet per line,
with the same host time, --true-m and --scene labels as the CSV, so the two
join on pair and time. "rx" is the board on USB (from its SELF line, printed
every 10 s; packets before the first one have rx null). tools/csi_analysis.py reads both.
"""

import argparse
import csv
import json
import glob
import statistics
import sys
import time
from collections import defaultdict, deque

import serial

FIELDS = ["time", "true_m", "source", "from", "to", "status", "dist_cm", "rtt_raw_ns", "rtt_est_ns", "rssi", "entries",
          "rtt_med_ps", "rtt_mean_ps", "rtt_min_ps", "rtt_max_ps", "dist_fine_cm", "scene"]
CSI_FIELDS = ["from", "to", "t_us", "rssi", "noise_floor", "sig_mode", "rate", "cwb", "stbc", "first_word_invalid",
              "kind", "seq", "len", "csi"]
# Labels for --scene, as in firmware/CSI_PLAN.md phase 3. Others are allowed, with a warning.
SCENES = ("los", "body", "person", "crowd", "wall", "walking-node", "walking-people")
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


def open_csv(path):
    """Append-mode CSV writer. Returns (file, writer, columns): an existing file
    keeps its own header, so new columns are left out of old files."""
    try:
        with open(path, newline="") as f:
            header = next(csv.reader(f), None)
    except FileNotFoundError:
        header = None
    f = open(path, "a", newline="")
    writer = csv.writer(f)
    if not header:
        writer.writerow(FIELDS)
        header = FIELDS
    missing = [c for c in FIELDS if c not in header]
    if missing:
        print(f"  note: {path} has no {', '.join(missing)} column(s); use a new file to record them")
    return f, writer, header


def print_csi_table(stats, names, window):
    """stats: src mac -> deque[(t, kind, seq, is_ht)] for the last `window` s."""
    rows = [(src, q) for src, q in sorted(stats.items()) if len(q) > 1]
    if not rows:
        return
    print(f"  CSI received, last {window:.0f}s")
    print(f"    {'from':<12} {'pkt/s':>6} {'ping/s':>6} {'HT':>4} {'ping loss':>9}")
    for src, q in rows:
        span = max(q[-1][0] - q[0][0], 1.0)
        seqs = sorted({seq for _, kind, seq, _ in q if kind == "ping" and seq is not None})
        ht = 100 * sum(1 for *_, h in q if h) / len(q)
        loss = f"{100 * (1 - len(seqs) / (seqs[-1] - seqs[0] + 1)):.0f}%" if len(seqs) > 1 else ""
        print(f"    {names.get(src, src[-8:]):<12} {len(q) / span:>6.1f} {len(seqs) / span:>6.1f} {ht:>3.0f}% {loss:>9}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial port (default: the only /dev/cu.usbmodem*)")
    ap.add_argument("--true-m", type=float, help="actual distance in meters, for calibration runs")
    ap.add_argument("--node", help="only label pairs involving this node (name like node-D2F7AD, or AP MAC); "
                                   "other pairs are logged without a true distance")
    ap.add_argument("--csv", help="append every measurement to this CSV file")
    ap.add_argument("--csi", help="append every CSI packet to this JSON-lines file")
    ap.add_argument("--scene", help="scene label for every CSV/CSI row: " + ", ".join(SCENES))
    ap.add_argument("--window", type=float, default=30, help="seconds of data in the live stats (default 30)")
    ap.add_argument("--every", type=float, default=5, help="seconds between live tables (default 5)")
    ap.add_argument("--duration", type=float, help="stop after this many seconds")
    ap.add_argument("--quiet", action="store_true", help="no live tables, only the final summary")
    args = ap.parse_args()
    if args.scene and args.scene not in SCENES:
        print(f"  note: --scene {args.scene!r} isn't one of {', '.join(SCENES)}")

    port = pick_port(args.port)
    ser = serial.Serial(port, 115200, timeout=0.5)
    print(f"Listening on {port}" + (f", true distance {args.true_m} m" if args.true_m is not None else ""))

    writer = None
    if args.csv:
        f, writer, columns = open_csv(args.csv)
    csi_out = open(args.csi, "a") if args.csi else None
    csi_recent = defaultdict(deque)              # src -> deque[(t, kind, seq, is_ht)]
    csi_count = 0

    names = {}                                   # ap_mac -> short name
    self_mac = None                              # the board on USB, from its SELF line
    recent = defaultdict(deque)                  # (from, to) -> deque[(t, status, cm)]
    everything = defaultdict(list)               # (from, to) -> [(status, cm)]
    start = last_print = time.time()
    buf = b""

    def labeled(macs):
        return args.true_m is not None and (
            args.node is None or any(args.node.lower() in (m, names.get(m, "").lower()) for m in macs))

    try:
        while args.duration is None or time.time() - start < args.duration:
            buf += ser.read(4096)
            *lines, buf = buf.split(b"\n")
            now = time.time()
            for raw in lines:
                line = raw.decode(errors="replace").strip()
                parts = line.split(",")
                if parts[0] in ("SELF", "PEER", "PEERS") and len(parts) >= 3:
                    if parts[0] == "SELF":
                        self_mac = parts[1]
                    if parts[0] != "SELF" and parts[1] not in names:
                        parts[0] = "PEER"  # first time we see it, announce it
                    names[parts[1]] = parts[2]
                    if parts[0] == "PEER":
                        print(f"  discovered {parts[2]} ({parts[1]}) rssi {parts[3] if len(parts) > 3 else '?'}")
                elif parts[0] == "LOST":
                    print(f"  lost {names.get(parts[1], parts[1])}")
                elif parts[0] in ("DIST", "RDIST") and len(parts) >= 13:
                    _, frm, to, status, cm, rtt_raw, rtt_est, rssi, entries, med, mean, lo, hi = parts[:13]
                    if parts[0] == "DIST":
                        self_mac = frm  # the board's own measurements, sooner than its next SELF line
                    fine = fine_cm(mean) if status == "ok" else None
                    dist = fine if fine is not None else (int(cm) if status == "ok" and cm else None)
                    recent[(frm, to)].append((now, status, dist))
                    everything[(frm, to)].append((status, dist))
                    if writer:
                        row = dict(zip(FIELDS, [f"{now:.3f}", args.true_m if labeled((frm, to)) else "",
                                                parts[0], frm, to, status, cm, rtt_raw, rtt_est, rssi, entries,
                                                med, mean, lo, hi, f"{fine:.1f}" if fine is not None else "",
                                                args.scene or ""]))
                        writer.writerow([row.get(c, "") for c in columns])
                elif parts[0] == "CSI" and len(parts) == 1 + len(CSI_FIELDS):
                    rec = dict(zip(CSI_FIELDS, parts[1:]))
                    for k in ("t_us", "rssi", "noise_floor", "sig_mode", "rate", "cwb", "stbc",
                              "first_word_invalid", "len"):
                        rec[k] = int(rec[k])
                    rec["seq"] = int(rec["seq"]) if rec["seq"] else None
                    csi_recent[rec["from"]].append((now, rec["kind"], rec["seq"], rec["sig_mode"] == 1))
                    csi_count += 1
                    if csi_out:
                        # "to" is the frame's destination (broadcast for ESP-NOW); "rx" is who received it.
                        rec = {"time": round(now, 3), "rx": self_mac, "true_m": args.true_m if labeled((rec["from"], rec["to"])) else None,
                               "scene": args.scene, **rec}
                        csi_out.write(json.dumps(rec, separators=(",", ":")) + "\n")
                elif parts[0] == "CSI":
                    print(f"  unexpected CSI line (other firmware version?): {line[:80]}")
                elif parts[0] == "CSISTAT" and len(parts) == 5:
                    if not args.quiet:
                        seen, drops, throttled, printed = parts[1:]
                        print(f"  board CSI since boot: {seen} seen, {drops} queue drops, "
                              f"{throttled} rate-limited, {printed} printed")
                elif parts[0] == "ERR":
                    print(f"  board error: {line}")
                elif parts[0] in ("DIST", "RDIST"):
                    print(f"  unexpected line (old firmware?): {line}")
                elif line.startswith(("E (", "abort", "Guru", "assert")):
                    print(f"  board: {line}")
            for q in list(recent.values()) + list(csi_recent.values()):
                while q and now - q[0][0] > args.window:
                    q.popleft()
            if not args.quiet and now - last_print >= args.every:
                last_print = now
                print_table(f"Last {args.window:.0f}s", {k: [(s, d) for _, s, d in q] for k, q in recent.items()},
                            names, args.true_m, args.node)
                print_csi_table(csi_recent, names, args.window)
                if writer:
                    f.flush()
                if csi_out:
                    csi_out.flush()
    except KeyboardInterrupt:
        pass
    print_table(f"Whole run ({time.time() - start:.0f}s)", everything, names, args.true_m, args.node)
    if writer:
        f.close()
        print(f"\nAppended to {args.csv}")
    if csi_out:
        csi_out.close()
        print(f"Appended {csi_count} CSI packets to {args.csi}")


if __name__ == "__main__":
    main()
