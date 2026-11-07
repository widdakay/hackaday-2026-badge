"""2D node positions from pairwise distances (weighted stress majorization).

Uses the firmware's C++ solver (`graphlayout`, user_c_modules/graphlayout)
when it's there; otherwise a pure-Python version that runs on a PC for tests
and on older firmware, with fewer iterations and a time budget.

    pos = layout(nodes, edges, prev)
      nodes: list of hashable ids (MACs); the first is the reference node
      edges: {(a, b): metres}
      prev:  {id: (x, y)} from the last call, or None
    -> {id: (x, y)} in metres, only for nodes with at least one edge.

Distances that disagree (raw FTM readings break the triangle inequality all
the time) are fitted in the least-squares sense, weighting short edges more.
Each call starts from `prev` and is rotated/reflected onto it afterwards, so
the picture doesn't spin or mirror between redraws. Without `prev` the
reference node lands at the origin with the next node along +x.
"""

import math
import time

try:
    from micropython import native
except ImportError:          # CPython, for tests
    def native(f):
        return f

ITERATIONS = 40         # sweeps per start
RESTARTS = 3            # extra random starts, only when there's no previous layout
RESTART_MARGIN = 0.2    # a restart must beat the first start's stress by 20 %
GOOD_FIT = 0.01         # RMS relative edge error that needs no restarts
BUDGET_MS = 1500        # stop early past this: it blocks the badge's event loop
_GOLDEN = 2.399963229728653   # golden angle, radians

try:
    import graphlayout       # C++ solver in the firmware (user_c_modules/graphlayout)
    from array import array
except ImportError:
    graphlayout = None

C_ITERATIONS = 300      # cap; the C++ solver stops early once converged
C_RESTARTS = 12         # fresh solves only
BAD_FIT = 0.15          # RMS relative edge error that triggers a fresh solve

try:
    _ticks, _diff = time.ticks_ms, time.ticks_diff
except AttributeError:
    def _ticks():
        return int(time.time() * 1000)

    def _diff(a, b):
        return a - b


def layout(nodes, edges, prev=None, iterations=ITERATIONS, budget_ms=BUDGET_MS):
    adj = {}
    for (a, b), d in edges.items():
        if a == b or d is None:
            continue
        d = max(d, 0.05)
        adj.setdefault(a, []).append((b, d))
        adj.setdefault(b, []).append((a, d))
    ids = [n for n in nodes if n in adj]
    if not ids:
        return {}
    prev = prev or {}
    t0 = _ticks()

    # Flat per-index lists for the hot loop: MicroPython boxes every float and
    # every tuple, so dicts of (x, y) tuples there cost ~100x more than this.
    n = len(ids)
    idx = {m: k for k, m in enumerate(ids)}
    nb = [[idx[j] for j, _ in adj[m]] for m in ids]
    dd = [[d for _, d in adj[m]] for m in ids]
    ww = [[1.0 / (d * d) for _, d in adj[m]] for m in ids]
    inv_sw = [1.0 / sum(w) for w in ww]
    n_edges = sum(len(v) for v in nb)

    # Sparse graphs have local minima (a node folded to the wrong side of an
    # edge). With no previous layout, try a few starts and keep the best. With
    # one, start from it only, so the picture stays put and stays cheap.
    common = [m for m in ids if m in prev]
    if graphlayout is not None and n <= graphlayout.MAX_NODES:
        return _layout_c(ids, adj, idx, edges, prev, common)

    restarts = 0 if len(common) >= 2 else RESTARTS
    best = None
    for k in range(restarts + 1):
        start = _initial(ids, adj, prev) if k == 0 else _scrambled(ids, adj, k)
        xs = [start[m][0] for m in ids]
        ys = [start[m][1] for m in ids]
        _relax(xs, ys, nb, dd, ww, inv_sw, n, iterations)
        s = _stress_idx(xs, ys, nb, dd, ww, n)
        if best is None or s < best[0] * (1 - RESTART_MARGIN):
            best = (s, xs, ys)
        if math.sqrt(best[0] / n_edges) < GOOD_FIT or _diff(_ticks(), t0) > budget_ms:
            break
    _, xs, ys = best
    pos = {m: (xs[k], ys[k]) for k, m in enumerate(ids)}

    if len(common) >= 2:
        return _align(pos, prev, common)
    return _gauge(pos, ids)


def _layout_c(ids, adj, idx, edges, prev, common):
    """The firmware's C++ solver. Warm (a previous layout covers 2+ nodes):
    start from it with no restarts, so a redraw is one cheap relaxation that
    stops when converged. Only if that fits badly (the old shape was folded,
    or the nodes moved a lot) is a fresh solve tried: classical MDS start plus
    random restarts, all in C."""
    n = len(ids)
    ei, ej, ed = array("H"), array("H"), array("f")
    for (a, b), d in edges.items():
        if a != b and d is not None and a in idx and b in idx:
            ei.append(idx[a])
            ej.append(idx[b])
            ed.append(d)
    m = len(ed)
    warm = len(common) >= 2
    if warm:
        start = _initial(ids, adj, prev)   # prev + new nodes by a neighbour; cheap
        xs = array("f", [start[k][0] for k in ids])
        ys = array("f", [start[k][1] for k in ids])
        s = graphlayout.solve(xs, ys, ei, ej, ed, iterations=C_ITERATIONS, restarts=0)
        if math.sqrt(s / m) > BAD_FIT:
            xs2, ys2 = array("f", xs), array("f", ys)
            s2 = graphlayout.solve(xs2, ys2, ei, ej, ed, iterations=C_ITERATIONS,
                                   restarts=C_RESTARTS, margin=0.0, mds=True)
            if s2 < s * (1 - RESTART_MARGIN):
                xs, ys = xs2, ys2
    else:
        xs, ys = array("f", bytes(4 * n)), array("f", bytes(4 * n))
        graphlayout.solve(xs, ys, ei, ej, ed, iterations=C_ITERATIONS,
                          restarts=C_RESTARTS, margin=0.0, mds=True)
    pos = {k: (xs[i], ys[i]) for i, k in enumerate(ids)}
    if warm:
        return _align(pos, prev, common)
    return _gauge(pos, ids)


@native
def _relax(xs, ys, nb, dd, ww, inv_sw, n, iterations):
    sqrt = math.sqrt
    for _ in range(iterations):
        for i in range(n):
            xi = xs[i]
            yi = ys[i]
            sx = 0.0
            sy = 0.0
            nbi = nb[i]
            ddi = dd[i]
            wwi = ww[i]
            for k in range(len(nbi)):
                j = nbi[k]
                xj = xs[j]
                yj = ys[j]
                dx = xi - xj
                dy = yi - yj
                dist = sqrt(dx * dx + dy * dy)
                if dist < 1e-9:
                    # Coincident: nudge apart deterministically.
                    dx = 1e-3
                    dy = 0.0
                    dist = 1e-3
                w = wwi[k]
                f = ddi[k] / dist
                sx += w * (xj + f * dx)
                sy += w * (yj + f * dy)
            xs[i] = sx * inv_sw[i]
            ys[i] = sy * inv_sw[i]


@native
def _stress_idx(xs, ys, nb, dd, ww, n):
    sqrt = math.sqrt
    s = 0.0
    for i in range(n):
        nbi = nb[i]
        for k in range(len(nbi)):
            j = nbi[k]
            dx = xs[i] - xs[j]
            dy = ys[i] - ys[j]
            e = sqrt(dx * dx + dy * dy) - dd[i][k]
            s += e * e * ww[i][k]
    return s


def _scrambled(ids, adj, seed):
    """Deterministic pseudo-random start spread over the graph's scale."""
    scale = max(d for nbrs in adj.values() for _, d in nbrs)
    state = seed * 2654435761 % 4294967296
    pos = {}
    for i in ids:
        state = (state * 1103515245 + 12345) % 2147483648
        x = state / 2147483648 - 0.5
        state = (state * 1103515245 + 12345) % 2147483648
        y = state / 2147483648 - 0.5
        pos[i] = (x * 2 * scale, y * 2 * scale)
    return pos


def _initial(ids, adj, prev):
    """Previous positions where at least two are known (keeps the picture
    steady); otherwise classical MDS. New nodes start near a placed neighbour
    at the measured distance, spread around it by the golden angle."""
    pos = {}
    for i in ids:
        if i in prev:
            pos[i] = prev[i]
    if len(pos) < 2:
        return _classical_mds(ids, adj)
    k = len(pos)
    pending = [i for i in ids if i not in pos]
    while pending:
        progress = False
        for i in list(pending):
            anchor = None
            for j, d in adj[i]:
                if j in pos:
                    anchor = (j, d)
                    break
            if anchor is None:
                continue
            (ax, ay), d = pos[anchor[0]], anchor[1]
            a = k * _GOLDEN
            pos[i] = (ax + d * math.cos(a), ay + d * math.sin(a))
            k += 1
            pending.remove(i)
            progress = True
        if not progress:
            # A separate component: start it off to the side.
            i = pending.pop(0)
            far = max([abs(x) + abs(y) for x, y in pos.values()] + [1.0])
            pos[i] = (2 * far, 0.0)
            k += 1
    return pos


def _classical_mds(ids, adj):
    """Torgerson MDS: fill unmeasured pairs with shortest-path distances,
    double-centre the squared distances and take the top two eigenvectors by
    power iteration. A good global start, so stress majorization doesn't get
    stuck in a folded layout."""
    n = len(ids)
    idx = {i: k for k, i in enumerate(ids)}
    inf = float("inf")
    D = [[0.0 if r == c else inf for c in range(n)] for r in range(n)]
    for i in ids:
        for j, d in adj[i]:
            D[idx[i]][idx[j]] = d
    for k in range(n):                       # Floyd-Warshall
        Dk = D[k]
        for r in range(n):
            Dr = D[r]
            via = Dr[k]
            if via == inf:
                continue
            for c in range(n):
                if via + Dk[c] < Dr[c]:
                    Dr[c] = via + Dk[c]
    finite = [v for row in D for v in row if v != inf]
    big = 1.5 * max(finite + [1.0])
    S = [[(v if v != inf else big) ** 2 for v in row] for row in D]
    rm = [sum(row) / n for row in S]
    gm = sum(rm) / n
    B = [[-0.5 * (S[r][c] - rm[r] - rm[c] + gm) for c in range(n)] for r in range(n)]

    coords = []
    for axis in range(2):
        v = [((k * 7 + axis * 3) % 11) - 5.0 + 0.1 * k for k in range(n)]
        lam = 0.0
        for _ in range(60):
            w = [sum(B[r][c] * v[c] for c in range(n)) for r in range(n)]
            norm = math.sqrt(sum(x * x for x in w))
            if norm < 1e-12:
                break
            v = [x / norm for x in w]
            lam = norm
        scale = math.sqrt(max(lam, 0.0))
        coords.append([x * scale for x in v])
        for r in range(n):                   # deflate
            for c in range(n):
                B[r][c] -= lam * v[r] * v[c]
    return {i: (coords[0][idx[i]], coords[1][idx[i]]) for i in ids}


def _centroid(pos, ids):
    n = len(ids)
    return (sum(pos[i][0] for i in ids) / n, sum(pos[i][1] for i in ids) / n)


def _align(pos, prev, common):
    """Rotate (and reflect if that fits better) `pos` onto `prev` over the
    nodes both share, keeping the shared nodes' centroid where it was."""
    cx, cy = _centroid(pos, common)
    px, py = _centroid(prev, common)
    best = None
    for flip in (1, -1):
        sxx = sxy = 0.0
        for i in common:
            x, y = pos[i][0] - cx, (pos[i][1] - cy) * flip
            u, v = prev[i][0] - px, prev[i][1] - py
            sxx += x * u + y * v
            sxy += x * v - y * u
        theta = math.atan2(sxy, sxx)
        c, s = math.cos(theta), math.sin(theta)
        err = 0.0
        for i in common:
            x, y = pos[i][0] - cx, (pos[i][1] - cy) * flip
            u, v = prev[i][0] - px, prev[i][1] - py
            ex, ey = c * x - s * y - u, s * x + c * y - v
            err += ex * ex + ey * ey
        if best is None or err < best[0]:
            best = (err, flip, c, s)
    _, flip, c, s = best
    out = {}
    for i, (x, y) in pos.items():
        x, y = x - cx, (y - cy) * flip
        out[i] = (px + c * x - s * y, py + s * x + c * y)
    return out


def _gauge(pos, ids):
    """Reference node at the origin, the next node along +x."""
    ox, oy = pos[ids[0]]
    out = {i: (x - ox, y - oy) for i, (x, y) in pos.items()}
    if len(ids) > 1:
        x, y = out[ids[1]]
        theta = -math.atan2(y, x)
        c, s = math.cos(theta), math.sin(theta)
        out = {i: (c * x - s * y, s * x + c * y) for i, (x, y) in out.items()}
    return out


def stress(pos, edges):
    """Mean absolute edge error in metres (for tests and debugging)."""
    errs = []
    for (a, b), d in edges.items():
        if a in pos and b in pos:
            (x1, y1), (x2, y2) = pos[a], pos[b]
            errs.append(abs(math.sqrt((x1 - x2) ** 2 + (y1 - y2) ** 2) - d))
    return sum(errs) / len(errs) if errs else 0.0
