// Stress-majorization layout core, see graph_core.h.
#include "graph_core.h"

#include <cmath>
#include <cstring>

namespace {

constexpr int kMaxNodes = GRAPH_MAX_NODES;
constexpr int kMaxEdges = kMaxNodes * (kMaxNodes - 1) / 2;

// Adjacency in CSR form, built once per solve: neighbours of node i are
// nbr[off[i]] .. nbr[off[i + 1] - 1], with target length len[] and weight w[].
struct Graph {
    int n = 0;
    int off[kMaxNodes + 1];
    uint16_t nbr[2 * kMaxEdges];
    float len[2 * kMaxEdges];
    float w[2 * kMaxEdges];
    float inv_wsum[kMaxNodes];
    float scale = 1.0f;  // longest edge, for scattering restarts
};

Graph g_graph;  // static: ~50 KB is too much for a task stack

bool build(Graph &g, int n, int m, const uint16_t *ei, const uint16_t *ej, const float *ed) {
    if (n < 0 || n > kMaxNodes || m < 0 || m > kMaxEdges) return false;
    g.n = n;
    int deg[kMaxNodes] = {};
    for (int k = 0; k < m; k++) {
        if (ei[k] >= n || ej[k] >= n) return false;
        if (ei[k] == ej[k]) continue;
        deg[ei[k]]++;
        deg[ej[k]]++;
    }
    g.off[0] = 0;
    for (int i = 0; i < n; i++) g.off[i + 1] = g.off[i] + deg[i];
    int fill[kMaxNodes];
    std::memcpy(fill, g.off, sizeof(int) * n);
    g.scale = 0.0f;
    for (int k = 0; k < m; k++) {
        int a = ei[k], b = ej[k];
        if (a == b) continue;
        float d = ed[k] < 0.05f ? 0.05f : ed[k];
        float w = 1.0f / (d * d);
        g.nbr[fill[a]] = b; g.len[fill[a]] = d; g.w[fill[a]] = w; fill[a]++;
        g.nbr[fill[b]] = a; g.len[fill[b]] = d; g.w[fill[b]] = w; fill[b]++;
        if (d > g.scale) g.scale = d;
    }
    for (int i = 0; i < n; i++) {
        float s = 0.0f;
        for (int e = g.off[i]; e < g.off[i + 1]; e++) s += g.w[e];
        g.inv_wsum[i] = s > 0.0f ? 1.0f / s : 0.0f;
    }
    if (g.scale <= 0.0f) g.scale = 1.0f;
    return true;
}

// Gauss-Seidel sweeps of the localized SMACOF update. Stops early once no
// node moves more than tol (converged). Returns the sweeps run.
int relax(const Graph &g, float *x, float *y, int iterations) {
    const float tol = 1e-4f * g.scale;
    for (int it = 0; it < iterations; it++) {
        float moved = 0.0f;
        for (int i = 0; i < g.n; i++) {
            if (g.off[i] == g.off[i + 1]) continue;  // isolated
            float xi = x[i], yi = y[i], sx = 0.0f, sy = 0.0f;
            for (int e = g.off[i]; e < g.off[i + 1]; e++) {
                int j = g.nbr[e];
                float dx = xi - x[j], dy = yi - y[j];
                float dist = std::sqrt(dx * dx + dy * dy);
                if (dist < 1e-6f) {  // coincident: nudge apart deterministically
                    dx = 1e-3f; dy = 0.0f; dist = 1e-3f;
                }
                float f = g.len[e] / dist;
                sx += g.w[e] * (x[j] + f * dx);
                sy += g.w[e] * (y[j] + f * dy);
            }
            float nx = sx * g.inv_wsum[i], ny = sy * g.inv_wsum[i];
            float m = std::fabs(nx - xi) + std::fabs(ny - yi);
            if (m > moved) moved = m;
            x[i] = nx;
            y[i] = ny;
        }
        if (moved < tol) return it + 1;
    }
    return iterations;
}

// Classical (Torgerson) MDS start: unmeasured pairs get shortest-path
// distances, then the top two eigenvectors of the double-centred squared
// distance matrix by power iteration. A good global start, so stress
// majorization rarely folds.
float g_D[kMaxNodes][kMaxNodes];

void mds_init(const Graph &g, float *x, float *y) {
    const int n = g.n;
    const float inf = 1e30f;
    for (int r = 0; r < n; r++)
        for (int c = 0; c < n; c++) g_D[r][c] = r == c ? 0.0f : inf;
    for (int i = 0; i < n; i++)
        for (int e = g.off[i]; e < g.off[i + 1]; e++) g_D[i][g.nbr[e]] = g.len[e];
    for (int k = 0; k < n; k++)                       // Floyd-Warshall
        for (int r = 0; r < n; r++) {
            float via = g_D[r][k];
            if (via >= inf) continue;
            for (int c = 0; c < n; c++)
                if (via + g_D[k][c] < g_D[r][c]) g_D[r][c] = via + g_D[k][c];
        }
    float big = 1.0f;
    for (int r = 0; r < n; r++)
        for (int c = 0; c < n; c++)
            if (g_D[r][c] < inf && g_D[r][c] > big) big = g_D[r][c];
    big *= 1.5f;                                      // separate components
    float rm[kMaxNodes], gm = 0.0f;
    for (int r = 0; r < n; r++) {
        float s = 0.0f;
        for (int c = 0; c < n; c++) {
            float d = g_D[r][c] < inf ? g_D[r][c] : big;
            g_D[r][c] = d * d;                        // now squared distances
            s += g_D[r][c];
        }
        rm[r] = s / n;
        gm += rm[r];
    }
    gm /= n;
    for (int r = 0; r < n; r++)                       // B = -1/2 J D^2 J
        for (int c = 0; c < n; c++) g_D[r][c] = -0.5f * (g_D[r][c] - rm[r] - rm[c] + gm);

    float *out[2] = {x, y};
    for (int axis = 0; axis < 2; axis++) {
        float v[kMaxNodes], w[kMaxNodes], lam = 0.0f;
        for (int k = 0; k < n; k++) v[k] = ((k * 7 + axis * 3) % 11) - 5.0f + 0.1f * k;
        for (int it = 0; it < 60; it++) {
            float norm = 0.0f;
            for (int r = 0; r < n; r++) {
                float s = 0.0f;
                for (int c = 0; c < n; c++) s += g_D[r][c] * v[c];
                w[r] = s;
                norm += s * s;
            }
            norm = std::sqrt(norm);
            if (norm < 1e-12f) break;
            for (int r = 0; r < n; r++) v[r] = w[r] / norm;
            lam = norm;
        }
        float sc = std::sqrt(lam > 0.0f ? lam : 0.0f);
        for (int r = 0; r < n; r++) out[axis][r] = v[r] * sc;
        for (int r = 0; r < n; r++)                   // deflate
            for (int c = 0; c < n; c++) g_D[r][c] -= lam * v[r] * v[c];
    }
}

float stress(const Graph &g, const float *x, const float *y) {
    float s = 0.0f;
    for (int i = 0; i < g.n; i++) {
        for (int e = g.off[i]; e < g.off[i + 1]; e++) {
            int j = g.nbr[e];
            if (j < i) continue;  // each edge once
            float dx = x[i] - x[j], dy = y[i] - y[j];
            float r = std::sqrt(dx * dx + dy * dy) - g.len[e];
            s += r * r * g.w[e];
        }
    }
    return s;
}

uint32_t next(uint32_t &state) {  // xorshift32
    state ^= state << 13;
    state ^= state >> 17;
    state ^= state << 5;
    return state;
}

}  // namespace

extern "C" float graph_core_solve(int n, float *xs, float *ys,
                                  int m, const uint16_t *ei, const uint16_t *ej, const float *ed,
                                  int iterations, int restarts, float margin, uint32_t seed,
                                  int mds_start) {
    Graph &g = g_graph;
    if (!build(g, n, m, ei, ej, ed)) return -1.0f;
    if (n == 0) return 0.0f;
    if (mds_start) mds_init(g, xs, ys);

    relax(g, xs, ys, iterations);
    float best = stress(g, xs, ys);

    float tx[kMaxNodes], ty[kMaxNodes];
    uint32_t state = seed ? seed : 0x9E3779B9u;
    for (int r = 0; r < restarts && best > 1e-8f; r++) {
        for (int i = 0; i < n; i++) {
            if (g.off[i] == g.off[i + 1]) {  // isolated: leave where it was
                tx[i] = xs[i]; ty[i] = ys[i];
                continue;
            }
            tx[i] = ((next(state) & 0xFFFF) / 65535.0f - 0.5f) * 2.0f * g.scale;
            ty[i] = ((next(state) & 0xFFFF) / 65535.0f - 0.5f) * 2.0f * g.scale;
        }
        relax(g, tx, ty, iterations);
        float s = stress(g, tx, ty);
        if (s < best * (1.0f - margin)) {
            best = s;
            std::memcpy(xs, tx, sizeof(float) * n);
            std::memcpy(ys, ty, sizeof(float) * n);
        }
    }
    return best;
}
