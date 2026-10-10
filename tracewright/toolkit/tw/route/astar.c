/* Octilinear multi-layer grid A* for tools/route/router.py (loaded with ctypes).
 *
 * State = (layer, cell, arrival direction); direction 8 = "none" (a source, or just arrived by via).
 * Same cost model as the Python reference in router.py:
 *   step 10 orthogonal / DG diagonal, x the layer's factor, x xc[layer] for moves not along x and x yc[layer] for
 *   moves not along y (a layer routed along x or y: 1 = no preference),
 *   + cell cost (congestion / history, per layer) scaled by the step length,
 *   bends: 45 deg -> bend45, 90 deg -> bend90, sharper turns forbidden,
 *   via: via_cost (+ cell cost of the landing cell) to any other layer; needs lv[cell] (legal on every layer) and
 *   a legal cell on the layer it lands on,
 *   diagonal moves may not cut a corner (both orthogonal neighbours legal).
 * The estimate: octilinear distance to the targets' box, plus one via on a layer that holds no target (admissible:
 * at least one more via is needed from there), so open inner layers are searched only when they help.
 * Build: cc -O3 -shared -fPIC -o libastar.so astar.c
 */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>

/* diagonal step cost: 19 (vs 14.1 for the true length) keeps long runs orthogonal with 45-degree
 * corners and jogs, the way boards are drawn by hand */
#define DG 19.0f

typedef struct { float f; float g; int32_t s; } item_t;

static item_t *heap = NULL;
static long heap_n = 0, heap_cap = 0;

static void hpush(float f, float g, int32_t s) {
    if (heap_n == heap_cap) {
        heap_cap = heap_cap ? heap_cap * 2 : (1 << 16);
        heap = (item_t *)realloc(heap, sizeof(item_t) * heap_cap);
    }
    long i = heap_n++;
    while (i > 0) {
        long p = (i - 1) >> 1;
        if (heap[p].f <= f) break;
        heap[i] = heap[p];
        i = p;
    }
    heap[i].f = f; heap[i].g = g; heap[i].s = s;
}

static item_t hpop(void) {
    item_t top = heap[0];
    item_t last = heap[--heap_n];
    long i = 0;
    for (;;) {
        long l = 2 * i + 1, r = l + 1, m = i;
        float fm = last.f;
        if (l < heap_n && heap[l].f < fm) { m = l; fm = heap[l].f; }
        if (r < heap_n && heap[r].f < fm) { m = r; }
        if (m == i) break;
        heap[i] = heap[m];
        i = m;
    }
    if (heap_n > 0) heap[i] = last;
    return top;
}

static const int DX[8] = {1, 1, 0, -1, -1, -1, 0, 1};
static const int DY[8] = {0, 1, 1, 1, 0, -1, -1, -1};

/* Returns the path length (cells, written to out as l*N+idx from source to target) or -1 / -2 (expansion
 * limit). out must hold out_cap entries. Window [wj0, wj1] x [wi0, wi1] (inclusive) bounds the search. */
long astar(int nx, int ny, int nl,
           const uint8_t *lt,          /* [nl][N] legal track cells */
           const uint8_t *lv,          /* [N] legal via cells (every layer) */
           const float *cc,            /* [nl][N] extra cell cost or NULL */
           const float *vc,            /* [N] extra cost of a via at the cell, or NULL */
           const int32_t *src, long nsrc,
           const uint8_t *tmask,       /* [nl][N] target cells */
           int ti0, int ti1, int tj0, int tj1,   /* target bounding box, for the heuristic */
           int wi0, int wi1, int wj0, int wj1,
           float bend45, float bend90, float via_cost,
           const float *lfac,          /* [nl] the layer's cost factor */
           const float *xc,            /* [nl] factor for moves not along x */
           const float *yc,            /* [nl] factor for moves not along y */
           float hweight, int allow_vias, long max_expand,
           int32_t *out, long out_cap)
{
    if (nl < 1 || nl > 16) return -3;
    const long N = (long)nx * ny;
    const int ww = wi1 - wi0 + 1, wh = wj1 - wj0 + 1;
    const long WN = (long)ww * wh;
    const long S = (long)nl * WN * 9;
    float *g = (float *)malloc(sizeof(float) * S);
    uint8_t *came = (uint8_t *)malloc(S);
    if (!g || !came) { free(g); free(came); return -3; }
    heap_n = 0;
    const float step[8] = {10, DG, 10, DG, 10, DG, 10, DG};
    long off[8];
    for (int d = 0; d < 8; d++) off[d] = DX[d] + (long)DY[d] * nx;
    float lh[16];                                     /* per layer: the via still needed to reach a target */
    for (int l = 0; l < nl; l++) {
        int any = 0;
        for (int j = tj0; j <= tj1 && !any; j++)
            for (int i = ti0; i <= ti1; i++) if (tmask[l * N + (long)j * nx + i]) { any = 1; break; }
        lh[l] = (any || !allow_vias) ? 0.0f : hweight * via_cost;
    }

#define WIDX(idx) ((long)(((idx) / nx) - wj0) * ww + (((idx) % nx) - wi0))
#define H(idx) ({ int _j = (int)((idx) / nx), _i = (int)((idx) % nx); \
        int _dx = (_i < ti0) ? ti0 - _i : ((_i > ti1) ? _i - ti1 : 0); \
        int _dy = (_j < tj0) ? tj0 - _j : ((_j > tj1) ? _j - tj1 : 0); \
        int _mx = _dx > _dy ? _dx : _dy, _mn = _dx > _dy ? _dy : _dx; \
        hweight * (10.0f * _mx + (DG - 10.0f) * _mn); })

    /* Can a target be reached at all? A flood fill over (layer, cell) by the same moves, far cheaper than the
     * search: when it finds none, neither would the search, which would visit every state to learn that. */
    {
        uint8_t *seen = (uint8_t *)calloc((size_t)nl * WN, 1);
        int32_t *q = (int32_t *)malloc(sizeof(int32_t) * (size_t)nl * WN);
        if (!seen || !q) { free(seen); free(q); free(g); free(came); return -3; }
        long qh = 0, qt = 0;
        int reach = 0;
        for (long k = 0; k < nsrc; k++) {
            int l = (int)(src[k] / N); long idx = src[k] % N;
            if (l < 0 || l >= nl) continue;
            int j = (int)(idx / nx), i = (int)(idx % nx);
            if (j < wj0 || j > wj1 || i < wi0 || i > wi1 || !lt[l * N + idx]) continue;
            long v = (long)l * WN + WIDX(idx);
            if (!seen[v]) { seen[v] = 1; q[qt++] = (int32_t)v; }
        }
        while (qh < qt && !reach) {
            long v = q[qh++];
            int l = (int)(v / WN);
            long w = v % WN;
            long idx = (long)(w / ww + wj0) * nx + (w % ww + wi0);
            if (tmask[l * N + idx]) { reach = 1; break; }
            const uint8_t *ltl = lt + l * N;
            for (int nd = 0; nd < 8; nd++) {
                long ni = idx + off[nd];
                int nj_ = (int)(ni / nx), ni_ = (int)(ni % nx);
                if (nj_ < wj0 || nj_ > wj1 || ni_ < wi0 || ni_ > wi1 || !ltl[ni]) continue;
                if ((nd & 1) && !(ltl[idx + DX[nd]] && ltl[idx + (long)DY[nd] * nx])) continue;
                long nv = (long)l * WN + WIDX(ni);
                if (!seen[nv]) { seen[nv] = 1; q[qt++] = (int32_t)nv; }
            }
            if (allow_vias && lv[idx]) {
                for (int ol = 0; ol < nl; ol++) {
                    if (ol == l || !lt[ol * N + idx]) continue;
                    long nv = (long)ol * WN + w;
                    if (!seen[nv]) { seen[nv] = 1; q[qt++] = (int32_t)nv; }
                }
            }
        }
        free(seen); free(q);
        if (!reach) { free(g); free(came); return -1; }
    }
    for (long k = 0; k < S; k++) g[k] = 1e30f;

    for (long k = 0; k < nsrc; k++) {
        int l = (int)(src[k] / N); long idx = src[k] % N;
        if (l < 0 || l >= nl) continue;
        int j = (int)(idx / nx), i = (int)(idx % nx);
        if (j < wj0 || j > wj1 || i < wi0 || i > wi1) continue;
        if (!lt[l * N + idx]) continue;
        long s = ((long)l * WN + WIDX(idx)) * 9 + 8;
        if (g[s] > 0) {
            g[s] = 0; came[s] = 15;
            hpush(H(idx) + lh[l], 0, (int32_t)s);
        }
    }
    long expanded = 0, found = -1;
    while (heap_n > 0) {
        item_t it = hpop();
        long s = it.s;
        if (it.g > g[s]) continue;
        int d = (int)(s % 9);
        long lw = s / 9;
        int l = (int)(lw / WN);
        long w = lw % WN;
        long idx = (long)(w / ww + wj0) * nx + (w % ww + wi0);
        if (tmask[l * N + idx]) { found = s; break; }
        if (++expanded > max_expand) break;
        const uint8_t *ltl = lt + l * N;
        const float gs = it.g;
        for (int nd = 0; nd < 8; nd++) {
            float bend = 0;
            if (d != 8) {
                int turn = (nd - d + 8) % 8;
                if (turn == 3 || turn == 4 || turn == 5) continue;
                bend = (turn == 0) ? 0 : ((turn == 1 || turn == 7) ? bend45 : bend90);
            }
            long ni = idx + off[nd];
            int nj_ = (int)(ni / nx), ni_ = (int)(ni % nx);
            if (nj_ < wj0 || nj_ > wj1 || ni_ < wi0 || ni_ > wi1) continue;
            if (!ltl[ni]) continue;
            if (nd & 1) {
                if (!(ltl[idx + DX[nd]] && ltl[idx + (long)DY[nd] * nx])) continue;
            }
            float c = step[nd] * lfac[l];
            if (nd != 0 && nd != 4) c *= xc[l];                   /* a layer routed along x */
            if (nd != 2 && nd != 6) c *= yc[l];                   /* a layer routed along y */
            if (cc) c += cc[l * N + ni] * step[nd] * 0.1f;
            long ns = ((long)l * WN + WIDX(ni)) * 9 + nd;
            float ng = gs + c + bend;
            if (ng < g[ns]) {
                g[ns] = ng; came[ns] = (uint8_t)d;
                hpush(ng + H(ni) + lh[l], ng, (int32_t)ns);
            }
        }
        if (allow_vias && lv[idx]) {
            for (int ol = 0; ol < nl; ol++) {
                if (ol == l || !lt[ol * N + idx]) continue;
                long ns = ((long)ol * WN + w) * 9 + 8;
                float ng = gs + via_cost + (cc ? cc[ol * N + idx] : 0.0f) + (vc ? vc[idx] : 0.0f);
                if (ng < g[ns]) {
                    g[ns] = ng; came[ns] = (uint8_t)(16 + l * 9 + d);
                    hpush(ng + H(idx) + lh[ol], ng, (int32_t)ns);
                }
            }
        }
    }
    long n = -1;
    if (found >= 0) {
        /* walk back: came = previous direction (same layer, cell - off[d]), 16 + pl * 9 + pd = via from layer pl */
        long s = found; n = 0;
        while (1) {
            int d = (int)(s % 9);
            long lw = s / 9;
            int l = (int)(lw / WN);
            long w = lw % WN;
            long idx = (long)(w / ww + wj0) * nx + (w % ww + wi0);
            if (n >= out_cap) { n = -4; break; }
            out[n++] = (int32_t)(l * N + idx);
            uint8_t c = came[s];
            if (c == 15) break;
            if (c >= 16) {            /* arrived by via: predecessor on layer pl, same cell */
                int v = c - 16, pl = v / 9, pd = v % 9;
                s = ((long)pl * WN + w) * 9 + pd;
            } else {                  /* arrived moving in direction d from cell - off[d] with direction c */
                long pidx = idx - off[d];
                s = ((long)l * WN + WIDX(pidx)) * 9 + c;
            }
        }
        if (n > 0) {                  /* reverse to source -> target */
            for (long a = 0, b = n - 1; a < b; a++, b--) { int32_t t = out[a]; out[a] = out[b]; out[b] = t; }
        }
    } else if (expanded > max_expand) {
        n = -2;
    }
    free(g); free(came);
    return n;
}
