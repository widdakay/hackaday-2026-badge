#!/usr/bin/env python3
"""Does Wi-Fi CSI predict FTM ranging error? Offline analysis (CSI_PLAN.md phase 4).

Usage:
    uv run --with numpy,pandas firmware/ftm_node/tools/csi_analysis.py \\
        --csv csi_cal.csv --csi csi_cal.jsonl
    # more runs, a joined table to explore further, and plots:
    uv run --with numpy,pandas,matplotlib firmware/ftm_node/tools/csi_analysis.py \\
        --csv a.csv b.csv --csi a.jsonl b.jsonl --out features.csv --plots plots/

Input is what ftm_monitor.py writes with --true-m, --scene, --csv and --csi.
Only rows with both a true distance and a scene are used.

Per CSI packet (LLTF by default, the field every packet has), with the
subcarriers normalised so automatic gain and scaling drop out:
  flat        std/mean of |H| across subcarriers (frequency-selective fading)
  k_db        Rician K-factor from the moments of |H|^2 across subcarriers, dB
  spread_ns   RMS delay spread of the power delay profile (IFFT, after
              removing the linear phase from the timing offset); 20 MHz
              resolves only ~50 ns, so this is coarse
  peak_frac   share of the profile's power in its strongest tap
  snr_db      rssi - noise_floor
Per FTM reading, from the CSI of the same pair (either direction) within
+-WINDOW s:
  the means of the above, and
  motion      mean relative change of normalised |H| between consecutive
              packets, |a - b| / |a| (0 = static channel). Not a correlation:
              on a flat LOS channel that measures mostly noise
  n_csi       packets used

FTM error = dist_fine_cm / 100 - true_m - bias, where bias is the initiator's
median error in the "los" scene (the same idea as INITIATOR_BIAS_M in
badge/lib/ranging.py, learned from this data).

It reports, and checks against the plan's go thresholds:
  * LOS vs NLOS (body, person, crowd, wall): AUC per feature, and of a
    logistic fit, cross-validated by leaving one distance out (go: >= 0.8)
  * how much of |error| the features explain: Spearman rho per feature, R^2
    of a linear fit, cross-validated the same way (go: >= 0.30)
  * extra bias per scene compared with "los"
  * motion: walking-* vs static scenes in 2 s windows, at the threshold that
    gives 5 % false alarms on static data (go: detects >= 90 %)
"""

import argparse
import base64
import json
import sys

import numpy as np
import pandas as pd

PS_TO_ONE_WAY_CM = 299792458e2 * 1e-12 / 2
NLOS_SCENES = {"body", "person", "crowd", "wall"}
WALKING_SCENES = {"walking-node", "walking-people"}
FEATURES = ["flat", "k_db", "spread_ns", "peak_frac", "snr_db", "motion"]
TAP_NS = 50.0                   # 64-point IFFT over 20 MHz

# Subcarrier index of each (imag, real) pair in a 128-byte LTF block: 0..31, -32..-1.
_SC_INDEX = np.r_[0:32, -32:0]
_VALID = {"lltf": (np.abs(_SC_INDEX) >= 1) & (np.abs(_SC_INDEX) <= 26),
          "htltf": (np.abs(_SC_INDEX) >= 1) & (np.abs(_SC_INDEX) <= 28)}


# ---- Loading ----------------------------------------------------------------
def load_ftm(paths):
    df = pd.concat([pd.read_csv(p, dtype={"from": str, "to": str}) for p in paths], ignore_index=True)
    if "scene" not in df:
        sys.exit("The CSV has no scene column: record with the new ftm_monitor.py --scene")
    df = df[(df.status == "ok") & df.true_m.notna() & df.scene.notna() & df.dist_fine_cm.notna()].copy()
    df["dist_m"] = df.dist_fine_cm / 100
    df["raw_err"] = df.dist_m - df.true_m
    df["pair"] = [pair_key(a, b) for a, b in zip(df["from"], df["to"])]
    return df.reset_index(drop=True)


def pair_key(a, b):
    return "|".join(sorted((a, b)))


def load_csi(paths, ltf):
    rows = []
    for path in paths:
        recs = [json.loads(line) for line in open(path) if line.strip()]
        # Packets logged before the first SELF line have no rx: take the next known one.
        rx = None
        for r in reversed(recs):
            rx = r.get("rx") or rx
            r["rx"] = r.get("rx") or rx
        rows.extend(r for r in recs if r["rx"] and r.get("scene"))
    if not rows:
        sys.exit("No labelled CSI packets (need ftm_monitor.py --csi with --scene)")
    feats = [packet_features(r, ltf) for r in rows]
    df = pd.DataFrame([{**{k: r[k] for k in ("time", "from", "rx", "scene", "true_m", "kind", "seq")}, **f}
                       for r, f in zip(rows, feats) if f is not None])
    df["pair"] = [pair_key(a, b) for a, b in zip(df["from"], df["rx"])]
    return df


def packet_h(rec, ltf):
    """Complex channel on the valid subcarriers, sorted by frequency, or None."""
    raw = np.frombuffer(base64.b64decode(rec["csi"]), dtype=np.int8).astype(float)
    if ltf == "lltf":
        block = raw[:128]
    elif rec["sig_mode"] == 1 and len(raw) >= 256:
        block = raw[128:256]
    else:
        return None
    if len(block) < 128:
        return None
    h = block[1::2] + 1j * block[0::2]          # pairs are (imag, real)
    valid = _VALID[ltf].copy()
    if ltf == "lltf" and rec["first_word_invalid"]:
        valid[1] = False                        # first 4 bytes = subcarriers 0 and 1
    idx = _SC_INDEX[valid]
    order = np.argsort(idx)
    return idx[order], h[valid][order]


def packet_features(rec, ltf):
    got = packet_h(rec, ltf)
    if got is None:
        return None
    idx, h = got
    amp = np.abs(h)
    if amp.mean() <= 0:
        return None
    amp_n = amp / np.sqrt(np.mean(amp ** 2))
    p = amp_n ** 2
    gamma = p.var() / p.mean() ** 2
    k = np.sqrt(1 - gamma) / (1 - np.sqrt(1 - gamma)) if gamma < 1 else 0.0
    # Take out the linear phase (packet timing offset), then the delay profile.
    phase = np.unwrap(np.angle(h))
    slope, icpt = np.polyfit(idx, phase, 1, w=amp_n)
    hc = amp_n * np.exp(1j * (phase - slope * idx - icpt))
    grid = np.zeros(64, complex)
    grid[idx % 64] = hc
    pdp = np.abs(np.fft.fftshift(np.fft.ifft(grid))) ** 2
    tau = (np.arange(64) - 32) * TAP_NS
    mean_tau = np.sum(pdp * tau) / pdp.sum()
    spread = np.sqrt(max(np.sum(pdp * tau ** 2) / pdp.sum() - mean_tau ** 2, 0.0))
    return {"flat": amp.std() / amp.mean(), "k_db": 10 * np.log10(max(k, 1e-3)), "spread_ns": spread,
            "peak_frac": pdp.max() / pdp.sum(), "snr_db": rec["rssi"] - rec["noise_floor"], "amp": amp_n}


# ---- Joining ----------------------------------------------------------------
def motion_of(amps):
    """Mean relative change |a - b| / |a| of consecutive normalised |H| vectors
    (same length only). On real C3s a static LOS link reads ~0.1 from noise."""
    ds = [np.linalg.norm(a - b) / np.linalg.norm(a) for a, b in zip(amps, amps[1:])
          if len(a) == len(b) and np.linalg.norm(a) > 0]
    return float(np.mean(ds)) if ds else np.nan


def join(ftm, csi, window, min_packets):
    """Per FTM row, CSI features of the same pair within +-window s."""
    out = []
    groups = {k: g.sort_values("time") for k, g in csi.groupby("pair")}
    for i, row in ftm.iterrows():
        g = groups.get(row.pair)
        if g is None:
            continue
        t = g.time.values
        lo, hi = np.searchsorted(t, row.time - window), np.searchsorted(t, row.time + window)
        w = g.iloc[lo:hi]
        if len(w) < min_packets:
            continue
        f = {k: w[k].mean() for k in FEATURES if k != "motion"}
        # Motion per direction, so two transmitters' packets aren't compared.
        f["motion"] = np.nanmean([motion_of(list(d.amp)) for _, d in w.groupby("from")] or [np.nan])
        f["n_csi"] = len(w)
        out.append({"i": i, **f})
    if not out:
        return ftm.iloc[0:0].assign(**{k: [] for k in FEATURES + ["n_csi"]})
    j = pd.DataFrame(out).set_index("i")
    return ftm.join(j, how="inner")


def add_error(df):
    los = df[df.scene == "los"]
    bias = los.groupby("from").raw_err.median().to_dict()
    if not bias:
        print("note: no 'los' rows, so no initiator bias is removed")
    df["bias"] = df["from"].map(bias).fillna(np.median(list(bias.values())) if bias else 0.0)
    df["err"] = df.raw_err - df.bias
    df["abs_err"] = df.err.abs()
    return bias


# ---- Statistics (numpy only) ------------------------------------------------
def auc(score, label):
    """P(score of a positive > score of a negative), ties half."""
    score, label = np.asarray(score, float), np.asarray(label, bool)
    ok = np.isfinite(score)
    score, label = score[ok], label[ok]
    npos, nneg = label.sum(), (~label).sum()
    if not npos or not nneg:
        return np.nan
    ranks = pd.Series(score).rank().values
    return (ranks[label].sum() - npos * (npos + 1) / 2) / (npos * nneg)


def spearman(x, y):
    d = pd.DataFrame({"x": x, "y": y}).dropna()
    if len(d) < 3 or d.x.nunique() < 2 or d.y.nunique() < 2:
        return np.nan
    return np.corrcoef(d.x.rank(), d.y.rank())[0, 1]


def standardize(train, test):
    mu, sd = train.mean(0), train.std(0)
    sd[sd == 0] = 1
    return (train - mu) / sd, (test - mu) / sd


def logistic_fit(x, y, l2=1.0, iters=50):
    x1 = np.c_[np.ones(len(x)), x]
    w = np.zeros(x1.shape[1])
    reg = l2 * np.eye(len(w))
    reg[0, 0] = 0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-x1 @ w))
        g = x1.T @ (p - y) + reg @ w
        h = x1.T @ (x1 * (p * (1 - p))[:, None]) + reg
        step = np.linalg.solve(h, g)
        w -= step
        if np.abs(step).max() < 1e-6:
            break
    return w


def linear_fit(x, y, l2=1e-3):
    x1 = np.c_[np.ones(len(x)), x]
    reg = l2 * np.eye(x1.shape[1])
    reg[0, 0] = 0
    return np.linalg.solve(x1.T @ x1 + reg, x1.T @ y)


def cv_by_distance(df, feats, target, kind):
    """Out-of-fold predictions, leaving one true distance out at a time."""
    pred = pd.Series(np.nan, index=df.index)
    dists = sorted(df.true_m.unique())
    if len(dists) < 2:
        return pred
    for d in dists:
        test, train = df[df.true_m == d], df[df.true_m != d]
        if kind == "logistic" and train[target].nunique() < 2:
            continue
        xtr, xte = standardize(train[feats].values, test[feats].values)
        if kind == "logistic":
            w = logistic_fit(xtr, train[target].values.astype(float))
            pred[test.index] = 1 / (1 + np.exp(-np.c_[np.ones(len(xte)), xte] @ w))
        else:
            w = linear_fit(xtr, train[target].values)
            pred[test.index] = np.c_[np.ones(len(xte)), xte] @ w
    return pred


def r2(y, pred):
    ok = np.isfinite(pred)
    y, pred = np.asarray(y)[ok], np.asarray(pred)[ok]
    return 1 - np.sum((y - pred) ** 2) / np.sum((y - y.mean()) ** 2) if len(y) > 2 else np.nan


# ---- Report -----------------------------------------------------------------
def report(df, csi, args):
    verdicts = {}
    print(f"\n{len(df)} FTM readings with CSI, {len(csi)} CSI packets, "
          f"{df.pair.nunique()} pairs, distances {', '.join(f'{d:g}' for d in sorted(df.true_m.unique()))} m")
    print("\nReadings per scene and distance:")
    print(df.pivot_table(index="scene", columns="true_m", values="err", aggfunc="size", fill_value=0).to_string())

    static = df[~df.scene.isin(WALKING_SCENES)]
    cls = static[static.scene.isin(NLOS_SCENES | {"los"})].copy()
    cls["nlos"] = cls.scene.isin(NLOS_SCENES)
    feats = [f for f in FEATURES if df[f].notna().all() and df[f].std() > 0]  # usable in the fits

    print("\nError per scene (after the initiator bias), metres:")
    by = static.groupby("scene").err.agg(["count", "median", "mean", "std"])
    if "los" in by.index:
        by["vs_los"] = by["median"] - by.loc["los", "median"]
    print(by.round(3).to_string())

    print("\nPer feature: LOS-vs-NLOS AUC (0.5 = useless; <0.5 means the feature is lower for NLOS), "
          "Spearman rho with |error|")
    for f in FEATURES:
        a = auc(cls[f], cls.nlos) if cls.nlos.any() and (~cls.nlos).any() else np.nan
        print(f"  {f:<10} AUC {a:5.2f}   rho {spearman(static[f], static.abs_err):+5.2f}")

    if cls.nlos.any() and (~cls.nlos).any():
        p = cv_by_distance(cls, feats, "nlos", "logistic")
        a = auc(p, cls.nlos)
        print(f"\nLogistic LOS/NLOS on {', '.join(feats)}: AUC {a:.2f} (leave one distance out)")
        verdicts["NLOS detection AUC >= 0.8"] = a >= 0.8 if np.isfinite(a) else None
    else:
        print("\nNeed both 'los' and NLOS scenes for the LOS/NLOS test")
        verdicts["NLOS detection AUC >= 0.8"] = None

    if len(static) > 10:
        p = cv_by_distance(static, feats, "abs_err", "linear")
        cv = r2(static.abs_err, p)
        xin, _ = standardize(static[feats].values, static[feats].values)
        w = linear_fit(xin, static.abs_err.values)
        fit = r2(static.abs_err, np.c_[np.ones(len(xin)), xin] @ w)
        print(f"Linear fit of |error|: R^2 {fit:.2f} in sample, {cv:.2f} leaving one distance out")
        print("  standardized weights: " + ", ".join(f"{f} {c:+.3f}" for f, c in zip(feats, w[1:])))
        verdicts["|error| R^2 >= 0.30"] = cv >= 0.30 if np.isfinite(cv) else None

    verdicts["motion detected >= 90 % in 2 s"] = motion_report(csi)

    print("\nGo / no-go (CSI_PLAN.md phase 4; any one is enough):")
    for k, v in verdicts.items():
        print(f"  {k:<34} {'yes' if v else 'no' if v is not None else 'not enough data'}")
    go = any(v for v in verdicts.values())
    print(f"  => {'GO: move on to phase 5' if go else 'NO-GO so far'}")


def motion_report(csi):
    """2 s windows per (transmitter, receiver): walking vs static."""
    rows = []
    for (src, rx), g in csi.groupby(["from", "rx"]):
        g = g.sort_values("time")
        for _, w in g.groupby((g.time // 2).astype(int)):
            if len(w) >= 4 and w.scene.nunique() == 1:
                rows.append({"scene": w.scene.iloc[0], "motion": motion_of(list(w.amp))})
    m = pd.DataFrame(rows).dropna() if rows else pd.DataFrame(columns=["scene", "motion"])
    walking, still = m[m.scene.isin(WALKING_SCENES)], m[~m.scene.isin(WALKING_SCENES)]
    print("\nMotion score in 2 s windows (relative change of |H| between packets):")
    if m.empty:
        print("  no windows with >= 4 packets")
        return None
    print(m.groupby("scene").motion.describe(percentiles=[0.5, 0.95])[["count", "50%", "95%"]].round(4).to_string())
    if walking.empty or still.empty:
        print("  need both walking-* and static scenes")
        return None
    thr = still.motion.quantile(0.95)
    rate = (walking.motion > thr).mean()
    print(f"  threshold {thr:.4f} (5 % false alarms on static): detects {100 * rate:.0f} % of walking windows, "
          f"AUC {auc(m.motion, m.scene.isin(WALKING_SCENES)):.2f}")
    return rate >= 0.9


def plots(df, csi, outdir):
    import os

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    os.makedirs(outdir, exist_ok=True)
    scenes = sorted(df.scene.unique())
    for f in FEATURES:
        fig, ax = plt.subplots(figsize=(6, 4))
        for s in scenes:
            d = df[df.scene == s]
            ax.scatter(d[f], d.err, s=8, alpha=0.6, label=s)
        ax.set_xlabel(f)
        ax.set_ylabel("FTM error after bias (m)")
        ax.legend(fontsize=7)
        fig.tight_layout()
        fig.savefig(os.path.join(outdir, f"err_vs_{f}.png"), dpi=120)
        plt.close(fig)
    print(f"\nPlots in {outdir}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", nargs="+", required=True, help="ftm_monitor.py --csv files")
    ap.add_argument("--csi", nargs="+", required=True, help="ftm_monitor.py --csi files")
    ap.add_argument("--ltf", choices=["lltf", "htltf"], default="lltf",
                    help="which training field to use (htltf: HT packets only)")
    ap.add_argument("--window", type=float, default=1.0, help="CSI within +-this many s of a reading (default 1)")
    ap.add_argument("--min-packets", type=int, default=3, help="readings with fewer CSI packets are dropped")
    ap.add_argument("--out", help="write the joined per-reading table to this CSV")
    ap.add_argument("--plots", help="write error-vs-feature plots to this directory (needs matplotlib)")
    args = ap.parse_args()

    ftm = load_ftm(args.csv)
    csi = load_csi(args.csi, args.ltf)
    df = join(ftm, csi, args.window, args.min_packets)
    if df.empty:
        sys.exit(f"No FTM readings have >= {args.min_packets} CSI packets of the same pair within "
                 f"+-{args.window} s. Was the board on USB part of the measured pairs?")
    bias = add_error(df)
    if bias:
        print("Initiator bias (median 'los' error), m: " + ", ".join(f"{k} {v:+.2f}" for k, v in bias.items()))
    report(df, csi, args)
    if args.out:
        df.drop(columns=[c for c in ("amp",) if c in df]).to_csv(args.out, index=False)
        print(f"\nJoined table in {args.out}")
    if args.plots:
        plots(df, csi, args.plots)


if __name__ == "__main__":
    main()
