// Net names on the board view's copper (board.js drawNetNames), checked in Node with a canvas that
// records what is written: which names appear at which zoom, which way they read, and that no two meet.
// Run: node tests/netnames_check.mjs  (prints "ok" and exits 0, or the failures and exits 1)
// the few browser globals the view's modules touch as they load
globalThis.Element = class {}; globalThis.DocumentFragment = class {};
if (!globalThis.navigator) globalThis.navigator = { platform: "MacIntel", userAgent: "Macintosh" };
const { BoardView } = await import("../tracewright/web/js/board.js");

const fails = [];
const check = (ok, what) => { if (!ok) fails.push(what); };

// a 2D context that records each text drawn: its string, centre, angle and size (the font's px)
function recorder() {
  let m = [1, 0, 0, 1, 0, 0], font = "10px sans-serif";
  const texts = [];
  const px = () => parseFloat(/([\d.]+)px/.exec(font)[1]);
  return {
    texts,
    set font(f) { font = f; }, get font() { return font; },
    textAlign: "", textBaseline: "", fillStyle: "", strokeStyle: "", lineWidth: 1,
    measureText: (t) => ({ width: t.length * 0.6 * px() }),
    setTransform: (...a) => { m = a; },
    save() {}, restore() {},
    strokeText() {},
    fillText(t) { texts.push({ t, x: m[4], y: m[5], a: Math.atan2(m[1], m[0]), size: px(), fill: this.fillStyle }); },
  };
}

// a board: tracks [x1, y1, x2, y2, w, layer, net], vias [x, y, d, drill, net], footprints with pads
function view({ scale = 60, tracks = [], vias = [], pads = [], copperMode = false, spot = null, side = "F" } = {}) {
  const v = Object.create(BoardView.prototype);
  Object.assign(v, {
    scale, ox: 0, oy: 0, w: 1400, h: 900, dpr: 1, side, copper: ["F.Cu", "B.Cu"], anim: {}, override: {},
    vis: { "F.Cu": true, "B.Cu": true, inner: true, vias: true, netnames: true }, spot, solo: null, netColor: {},
    data: { tracks, vias }, fps: [{ ref: "U1", x: 0, y: 0, a: 0, pads }],
  });
  const c = recorder();
  v.drawNetNames(c, copperMode);
  return c.texts;
}
const deg = (a) => Math.round(a * 180 / Math.PI);
const near = (a, b, tol = 1) => Math.abs(a - b) <= tol;

// 1. a 0.25 mm track at 60 px/mm (15 px wide): named along it, repeated on a long run, reading left to right
{
  const t = view({ tracks: [[2, 5, 22, 5, 0.25, "F.Cu", "/MCU/SDA"]] });
  check(t.length >= 3 && t.every((x) => x.t === "SDA"), `long track: ${t.length} names ${JSON.stringify(t.map((x) => x.t))}`);
  check(t.every((x) => deg(x.a) === 0 && near(x.y, 300)), "long track: names centred on it, horizontal");
  check(t.every((x) => x.size <= 15 * 0.66 + 0.26), "long track: text taller than the track holds");   // sizes round to half a px
}
// 2. the same track zoomed out (5 px wide): too thin to hold a name
check(view({ scale: 20, tracks: [[2, 5, 22, 5, 0.25, "F.Cu", "/MCU/SDA"]] }).length === 0, "thin track: named anyway");
// 3. drawn right to left: still reads left to right
{
  const t = view({ tracks: [[22, 5, 2, 5, 0.25, "F.Cu", "SCL"]] });
  check(t.length && t.every((x) => deg(x.a) === 0), "right-to-left track: name upside down");
}
// 4. vertical, either way: reads bottom to top
for (const tr of [[5, 1, 5, 14, 0.25, "F.Cu", "MISO"], [5, 14, 5, 1, 0.25, "F.Cu", "MISO"]]) {
  const t = view({ tracks: [tr] });
  check(t.length && t.every((x) => deg(x.a) === -90), `vertical track ${tr[1]}->${tr[3]}: angle ${t.map((x) => deg(x.a))}`);
}
// 5. a short segment (shorter than its name): no name
check(view({ tracks: [[5, 5, 5.3, 5, 0.25, "F.Cu", "/LONG_NET_NAME"]] }).length === 0, "short segment: named");
// 6. crossing tracks, top and bottom: the top one keeps its name at the crossing
{
  const t = view({ tracks: [[2, 7.5, 22, 7.5, 0.3, "F.Cu", "TOPNET"], [12, 0.5, 12, 14.5, 0.3, "B.Cu", "BOTNET"]] });
  const top = t.filter((x) => x.t === "TOPNET"), bot = t.filter((x) => x.t === "BOTNET");
  check(top.length >= 3, "crossing: top track lost its names");
  check(!bot.some((x) => near(x.x, 720, 30) && near(x.y, 450, 30)), "crossing: bottom name drawn over the top one");
}
// 7. arcs (tracks with a midpoint) are named along both halves
{
  const t = view({ tracks: [[2, 12, 22, 12, 0.3, "F.Cu", "ARC", 12, 2]] });
  check(t.length >= 2 && new Set(t.map((x) => deg(x.a))).size >= 2, "arc: not named along its halves");
}
// 8. a pad big enough for two lines: the number above, the net under it
{
  const t = view({ scale: 40, pads: [{ n: "7", net: "/Power/+3V3", x: 10, y: 10, a: 0, w: 1.6, h: 1.0, l: ["F.Cu", "F.Mask"], s: "rect" }] });
  const num = t.find((x) => x.t === "7"), net = t.find((x) => x.t === "+3V3");
  check(num && net, `pad: ${JSON.stringify(t.map((x) => x.t))}`);
  if (num && net) check(num.y < net.y && near(num.x, 400) && near(net.x, 400), "pad: number not above the net");
}
// 9. a tall pad: its text runs along the long side, bottom to top
{
  const t = view({ scale: 40, pads: [{ n: "3", net: "SDA", x: 10, y: 10, a: 0, w: 0.9, h: 2.4, l: ["F.Cu"], s: "rect" }] });
  check(t.length && t.every((x) => deg(x.a) === -90), `tall pad: angle ${t.map((x) => deg(x.a))}`);
}
// 10. a small pad: the net alone; smaller still: nothing
{
  const one = view({ scale: 40, pads: [{ n: "12", net: "SDA", x: 10, y: 10, a: 0, w: 1.2, h: 0.45, l: ["F.Cu"], s: "rect" }] });
  check(one.length === 1 && one[0].t === "SDA", `small pad: ${JSON.stringify(one.map((x) => x.t))}`);
  check(view({ scale: 10, pads: [{ n: "12", net: "SDA", x: 10, y: 10, a: 0, w: 1.2, h: 0.45, l: ["F.Cu"], s: "rect" }] }).length === 0, "tiny pad: named");
}
// 11. a pad on no net shows its number; KiCad's single-pin nets read NC
{
  const t = view({ scale: 40, pads: [
    { n: "5", net: "", x: 5, y: 5, a: 0, w: 1.5, h: 1.5, l: ["F.Cu"], s: "rect" },
    { n: "6", net: "unconnected-(U1-Pad6)", x: 10, y: 5, a: 0, w: 1.5, h: 1.5, l: ["F.Cu"], s: "rect" }] });
  check(t.some((x) => x.t === "5") && t.some((x) => x.t === "NC") && !t.some((x) => x.t.startsWith("unconnected")), `no-net pads: ${JSON.stringify(t.map((x) => x.t))}`);
}
// 12. a bottom-side pad: named dimmer from the top (both layers shown), at full strength from below
{
  const pads = [{ n: "1", net: "GND", x: 10, y: 10, a: 0, w: 1.5, h: 1.5, l: ["B.Cu"], s: "rect" }];
  const top = view({ scale: 40, pads }), below = view({ scale: 40, pads, side: "B" });
  check(top.length === 2 && top.every((x) => /,0\.6\)$/.test(x.fill)), `bottom pad from the top: ${JSON.stringify(top)}`);
  check(below.length === 2 && below.every((x) => /,0\.92\)$/.test(x.fill)), `bottom pad from below: ${JSON.stringify(below)}`);
}
// 12b. a bottom pad under a top track: not named where the top copper covers it
{
  const t = view({ scale: 40, pads: [{ n: "1", net: "GND", x: 10, y: 10, a: 0, w: 1.5, h: 1.5, l: ["B.Cu"], s: "rect" }],
    tracks: [[2, 10, 18, 10, 0.5, "F.Cu", "SIG"]] });
  check(!t.some((x) => x.t === "GND" || x.t === "1"), `bottom pad under a top track: named ${JSON.stringify(t.map((x) => x.t))}`);
  check(t.some((x) => x.t === "SIG"), "top track over a bottom pad: lost its name");
}
// 13. vias large enough carry the net
{
  check(view({ vias: [[10, 10, 0.6, 0.3, "/PWR/VBUS"]] }).some((x) => x.t === "VBUS"), "via: not named");
  check(view({ scale: 20, vias: [[10, 10, 0.6, 0.3, "/PWR/VBUS"]] }).length === 0, "small via: named");
}
// 14. the spotlight (copper panel): only its net is named
{
  const t = view({ copperMode: true, spot: "SDA", tracks: [[2, 5, 22, 5, 0.25, "F.Cu", "SDA"], [2, 9, 22, 9, 0.25, "F.Cu", "SCL"]] });
  check(t.length && t.every((x) => x.t === "SDA"), "spotlight: other nets named");
}
// 15. a busy patch (a bus of close tracks, pads, vias): no two names meet
{
  const tracks = [], vias = [], pads = [];
  for (let i = 0; i < 12; i++) tracks.push([1, 2 + i * 0.5, 20, 2 + i * 0.5, 0.3, "F.Cu", `D${i}`]);
  for (let i = 0; i < 8; i++) tracks.push([3 + i * 2, 1, 3 + i * 2, 9, 0.3, "B.Cu", `A${i}`]);
  for (let i = 0; i < 6; i++) vias.push([4 + i * 2.5, 4.2, 0.6, 0.3, `D${i}`]);
  for (let i = 0; i < 6; i++) pads.push({ n: String(i + 1), net: `D${i}`, x: 4 + i * 2.5, y: 8.3, a: 0, w: 1.2, h: 1.6, l: ["F.Cu"], s: "rect" });
  const t = view({ scale: 60, tracks, vias, pads });
  const boxes = t.map((x) => ({ ...x, hw: x.t.length * 0.6 * x.size / 2, hh: x.size * 0.5 }));
  let meet = 0;
  for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) {
    const a = boxes[i], b = boxes[j];
    // the two lines of one pad sit together; skip pairs drawn as one pad's number and net
    if (Math.hypot(a.x - b.x, a.y - b.y) < 1e-6) continue;
    if (overlap(a, b)) meet++;
  }
  check(t.length > 20, `busy patch: only ${t.length} names`);
  check(meet === 0, `busy patch: ${meet} pairs of names meet`);
}

function overlap(a, b) {
  const ax = [Math.cos(a.a), Math.sin(a.a)], bx = [Math.cos(b.a), Math.sin(b.a)];
  const axes = [ax, [-ax[1], ax[0]], bx, [-bx[1], bx[0]]];
  const dx = b.x - a.x, dy = b.y - a.y;
  for (const [x, y] of axes) {
    const r = (o, u) => o.hw * Math.abs(u[0] * x + u[1] * y) + o.hh * Math.abs(-u[1] * x + u[0] * y);
    if (Math.abs(dx * x + dy * y) > r(a, ax) + r(b, bx) - 0.5) return false;
  }
  return true;
}

if (fails.length) { console.log(fails.join("\n")); process.exit(1); }
console.log("ok");
