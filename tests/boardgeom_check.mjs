// The board editor's geometry (web/js/boardgeom.js), checked in Node on a real board: the walkaround router finds a
// way between the pads of each net on the board with its tracks taken off, every segment at 45° steps and clear of
// every other net's copper (checked here by brute force, not with the router's own index); the clearance check sees
// a track run into another net's; a keep-out over the only way leaves no path; the router answers quickly.
// Run: node tests/boardgeom_check.mjs board.json   (board.json: the board view's JSON; prints "ok" or the failures)
import fs from "fs";
const G = await import("../tracewright/web/js/boardgeom.js");

const fails = [];
const check = (ok, what) => { if (!ok) fails.push(what); };
const routed = JSON.parse(fs.readFileSync(process.argv[2]));
const bare = { ...routed, tracks: [], tid: [], vias: [], vid: [] };
const viewOf = (d) => ({ data: d, fps: d.footprints, pose: () => null });
const W = 0.25, CL = 0.2;

// 1. each net's first two pads on the top, routed on the bare board
const pads = {};
for (const f of bare.footprints) for (const p of f.pads) if (p.net && !p.net.startsWith("unconnected-") && (p.l.includes("F.Cu") || p.l.includes("*.Cu"))) (pads[p.net] = pads[p.net] || []).push(p);
let n = 0, ms = 0;
for (const [net, ps] of Object.entries(pads)) {
  if (ps.length < 2) continue;
  const a = [ps[0].x, ps[0].y], b = [ps[1].x, ps[1].y];
  const obs = new G.Obstacles(viewOf(bare), "F.Cu", net);
  const t0 = performance.now();
  const path = G.walk(obs, a, b, W, CL, { ends: [...ps[0].p, ...ps[1].p] });
  ms += performance.now() - t0; n++;
  check(path, `no way found for ${net}`);
  if (!path) continue;
  check(Math.hypot(path[0][0] - a[0], path[0][1] - a[1]) < 1e-9 && Math.hypot(path.at(-1)[0] - b[0], path.at(-1)[1] - b[1]) < 1e-9, `${net}: the path does not end on its pads`);
  for (let i = 0; i + 1 < path.length; i++) {
    const A = path[i], B = path[i + 1], dx = B[0] - A[0], dy = B[1] - A[1];
    const k = Math.atan2(dy, dx) / (Math.PI / 4);
    check(Math.abs(k - Math.round(k)) < 1e-6, `${net}: segment ${i} is not at a 45° step (${JSON.stringify([A, B])})`);
    for (const f of bare.footprints) for (const p of f.pads) {
      if (p.net === net || !(p.l.includes("F.Cu") || p.l.includes("*.Cu"))) continue;
      for (const pl of p.p) {
        const dd = G.segPolyDist(A, B, pl);
        check(dd >= W / 2 + CL - 1e-3, `${net}: segment ${i} comes ${dd.toFixed(3)} mm from ${f.ref} pad ${p.n} (${p.net || "no net"})`);
      }
    }
    check(obs.inside(A[0], A[1]) && obs.inside(B[0], B[1]), `${net}: segment ${i} leaves the board`);
  }
}
check(n >= 10, `only ${n} nets tried`);
check(ms / Math.max(1, n) < 80, `the router took ${Math.round(ms / n)} ms a net on average`);

// 2. on the routed board, a straight line across the board meets other nets' tracks; one along a track of the same
// net does not
const obsR = new G.Obstacles(viewOf(routed), "F.Cu", "");
const t = routed.tracks.find((t) => t[5] === "F.Cu" && Math.hypot(t[2] - t[0], t[3] - t[1]) > 2);
const mid = [(t[0] + t[2]) / 2, (t[1] + t[3]) / 2], nx = -(t[3] - t[1]), ny = t[2] - t[0], L = Math.hypot(nx, ny);
const across = [[mid[0] - nx / L * 3, mid[1] - ny / L * 3], [mid[0] + nx / L * 3, mid[1] + ny / L * 3]];
const hit = new G.Obstacles(viewOf(routed), "F.Cu", "some-other-net").hitSeg(across[0], across[1], W, CL);
check(hit && (hit.what === "track" || hit.what === "pad"), `a track across ${t[6]} was not seen to cross it (${hit && hit.what})`);
const own = new G.Obstacles(viewOf(routed), "F.Cu", t[6]).hitSeg([t[0], t[1]], [t[2], t[3]], t[4], CL);
check(!own || own.net !== t[6], "a net's own track counted as in the way");
check(obsR.items.length > 50, "too few obstacles on the routed board");

// 3. a keep-out over the goal: no way in
const [net0, ps0] = Object.entries(pads).find(([, ps]) => ps.length >= 2);
const g = [ps0[1].x, ps0[1].y];
const ko = { rule: true, name: "test keep-out", l: ["F.Cu"], ko: { tracks: true }, o: [[[g[0] - 1.5, g[1] - 1.5], [g[0] + 1.5, g[1] - 1.5], [g[0] + 1.5, g[1] + 1.5], [g[0] - 1.5, g[1] + 1.5]]] };
const kb = { ...bare, zones: [...bare.zones, ko] };
const none = G.walk(new G.Obstacles(viewOf(kb), "F.Cu", net0), [ps0[0].x, ps0[0].y], g, W, CL, { ends: [...ps0[0].p] });
check(none === null, `a path went through a keep-out: ${JSON.stringify(none)}`);

// 4. posture: two segments at 45° steps
const pz = G.posture([0, 0], [3, 1], true);
check(pz.length === 3 && pz[1][0] === 1 && pz[1][1] === 1, `posture ${JSON.stringify(pz)}`);

// 5. a pair's halves: the path offset to each side keeps its distance from the centre line, corners mitred
const cl5 = [[0, 0], [10, 0], [15, 5], [15, 15]];
for (const d of [0.3, -0.3]) {
  const off = G.offsetPath(cl5, d);
  for (const q of off) {
    let m = Infinity;
    for (let i = 0; i + 1 < cl5.length; i++) m = Math.min(m, G.segDist(q[0], q[1], cl5[i][0], cl5[i][1], cl5[i + 1][0], cl5[i + 1][1]));
    check(Math.abs(m - 0.3) < 0.03, `offset ${d}: a corner ${m.toFixed(3)} mm from the centre line`);
  }
}
// 6. a meander adds exactly what is asked, while there is room; says how much is missing when there is not
for (const extra of [0.2, 1, 3, 7.5]) {
  const m = G.serpentine([0, 0], [20, 0], extra, 1.0, 2.0);
  check(m && Math.abs(m.added - extra) < 1e-6 && !m.short, `meander for ${extra}: ${m && m.added}`);
}
const tight = G.serpentine([0, 0], [6, 0], 10, 1.0, 1.5);
check(tight && tight.short > 4 && Math.abs(tight.added + tight.short - 10) < 1e-6, `a run too short: ${JSON.stringify(tight && [tight.added, tight.short])}`);
// 7. a track's length: an arc along the arc (a quarter circle of radius 2, a half circle), a straight one end to end
const q = Math.SQRT1_2 * 2;
check(Math.abs(G.trackLength([2, 0, 0, 2, 0.2, "F.Cu", "n", q, q]) - Math.PI) < 1e-9, "quarter arc length");
check(Math.abs(G.trackLength([-2, 0, 2, 0, 0.2, "F.Cu", "n", 0, -2]) - 2 * Math.PI) < 1e-9, "half arc length");
check(Math.abs(G.trackLength([0, 0, 3, 4, 0.2, "F.Cu", "n"]) - 5) < 1e-12, "straight length");

if (fails.length) { console.log(fails.slice(0, 20).join("\n")); process.exit(1); }
console.log(`ok (${n} nets, ${Math.round(ms / n)} ms each)`);
