// 2D layout from pairwise distances (weighted stress majorization), C API.
//
// Implemented in C++ (graph_core.cpp), no MicroPython headers, so it also
// builds on a PC for tests. The MicroPython binding is modgraphlayout.c.
#pragma once

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// Relax the positions (xs[i], ys[i]) of n nodes so that every edge
// (ei[k], ej[k]) has length close to ed[k] metres, weighting each edge by
// 1/ed^2 (short edges matter more). xs/ys are the start and are overwritten
// with the result.
//
// After relaxing from the given start it tries `restarts` pseudo-random starts
// (seeded by `seed`) and keeps the best; a restart has to beat the given
// start's stress by `margin` (0.2 = 20 %), so a good previous layout stays put.
//
// mds_start != 0 replaces the given start with a classical MDS layout
// (shortest paths fill unmeasured pairs). Relaxing stops early once no node
// moves more than 1e-4 x the longest edge.
//
// Returns the weighted stress sum((|xi - xj| - d)^2 / d^2), or -1 on bad input
// (n > GRAPH_MAX_NODES, an edge index >= n). Isolated nodes don't move.
#define GRAPH_MAX_NODES 64
float graph_core_solve(int n, float *xs, float *ys,
                       int m, const uint16_t *ei, const uint16_t *ej, const float *ed,
                       int iterations, int restarts, float margin, uint32_t seed,
                       int mds_start);

#ifdef __cplusplus
}
#endif
