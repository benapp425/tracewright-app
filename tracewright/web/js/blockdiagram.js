// The guided start's block diagram. Blocks sit in tiers top to bottom (connectors, power, processor,
// peripherals, or the order that draws cleanest: layoutDiagram scores each), a tier wrapping onto more rows
// when it is wider than the column. Wires are orthogonal:
// down from a block, along their own track in the channel between two rows (the tracks ordered to cross
// as little as possible), and down into the block they reach. A bus is drawn once, with a drop to each
// block on it; a wire that skips a row runs down the side; a supply that would cross the drawing is a
// tag on each block it feeds. Pure (no DOM): the page passes a text measurer, the tests check geometry.

const TIER = { connector: 0, power: 1, mcu: 2, memory: 2, rf: 2, sensor: 3, io: 3, display: 3, motor: 3, audio: 3, other: 3 };
const KINDS = ["power", "bus", "signal"];
const CG = 22, PAD = 14, TRACK = 9, PILL = 16, LEVEL = PILL + 3, LANE = 10, EDGE = 14, MIN_W = 140, MAX_W = 204;

// Text widths without a DOM, close enough for layout tests; the page measures with its own fonts.
// Fonts: "t" block title, "n" block note, "l" wire label and tag.
export const estimate = (s, font) => String(s).length * (font === "t" ? 7.1 : font === "n" ? 6.1 : 6.4);

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const r1 = (v) => Math.round(v * 10) / 10;

function fit(s, max, font, measure) {
  s = String(s);
  if (measure(s, font) <= max) return s;
  let lo = 0, hi = s.length;
  while (lo < hi) {
    const m = (lo + hi + 1) >> 1;
    if (measure(s.slice(0, m).trimEnd() + "…", font) <= max) lo = m; else hi = m - 1;
  }
  return s.slice(0, lo).trimEnd() + "…";
}

// Words onto at most n lines of width max; what does not fit ends the last line with an ellipsis.
function wrap(s, max, font, measure, n) {
  const lines = [];
  let cur = "";
  for (const w of String(s || "").split(/\s+/).filter(Boolean)) {
    const t = cur ? `${cur} ${w}` : w;
    if (!cur || lines.length === n - 1 || measure(t, font) <= max) cur = t;
    else { lines.push(cur); cur = w; }
  }
  if (cur) lines.push(cur);
  return lines.map((l) => fit(l, max, font, measure));
}

function blockText(b, measure, W) {
  const TEXT_W = W - 38;
  const given = String(b.label || b.id).split(/\n+/).map((s) => s.trim()).filter(Boolean);
  const title = given.length > 1 ? [fit(given[0], TEXT_W, "t", measure), fit(given.slice(1).join(" "), TEXT_W, "t", measure)]
    : wrap(given[0] || b.id, TEXT_W, "t", measure, 2);
  const note = b.note ? wrap(String(b.note).replace(/\s+/g, " "), TEXT_W, "n", measure, title.length > 1 ? 2 : 3) : [];
  return { title, note, h: Math.max(42, 16 + title.length * 15 + note.length * 13.5) };
}

// Tracks in a channel, top to bottom: a wire whose track is higher crosses the verticals that come down
// through its span to a lower track, and its own drops cross the lower tracks they pass; two verticals
// on one x that would run side by side cost the most.
function orderTracks(tracks) {
  for (const t of tracks) { const xs = [...t.entries, ...t.exits]; t.lo = Math.min(...xs); t.hi = Math.max(...xs); }
  const inside = (x, t) => x > t.lo + 0.5 && x < t.hi - 0.5;
  const cost = (a, b) => b.entries.filter((x) => inside(x, a)).length + a.exits.filter((x) => inside(x, b)).length
    + 10 * b.entries.filter((x) => a.exits.some((y) => Math.abs(x - y) < 1)).length;
  const out = [], rest = [...tracks];
  while (rest.length) {
    let best = 0, bestCost = Infinity;
    rest.forEach((t, i) => {
      const c = rest.reduce((s, o) => (o === t ? s : s + cost(t, o)), 0);
      if (c < bestCost) { bestCost = c; best = i; }
    });
    out.push(rest.splice(best, 1)[0]);
  }
  return out;
}

// A label in a band of a channel (the top band just under a row, the bottom band just over the next):
// centred on its wire, nudged along it or stacked a level further in to keep clear of the other labels
// and of every other wire that passes through the band. False when there is no such spot.
function tryPill(band, p) {
  for (let lv = 0; lv < 3; lv++) {
    for (const dx of [0, -p.w / 2 + 8, p.w / 2 - 8, -p.w / 4, p.w / 4]) {
      const x0 = p.x + dx - p.w / 2, x1 = x0 + p.w;
      if (band.verticals.some((v) => v.own !== p.own && v.x > x0 - 3 && v.x < x1 + 3)) continue;
      if ((band.levels[lv] || []).some((q) => x0 < q.x1 + 5 && x1 > q.x0 - 5)) continue;
      p.cx = p.x + dx; p.level = lv;
      (band.levels[lv] = band.levels[lv] || []).push({ x0, x1 });
      band.pills.push(p);
      return true;
    }
  }
  return false;
}

// The diagram laid out several ways -- the tiers in each order, as many blocks to a row as fit or one fewer -- and the
// cleanest kept: fewest wires crossing or running on top of each other, fewest side lanes and hidden labels, then the
// shortest wires and the most compact drawing. The usual order (connectors, power, processor, the rest) wins a tie.
export function layoutDiagram(d, opts = {}) {
  const present = [0, 1, 2, 3].filter((t) => (d.blocks || []).some((b) => (TIER[b.kind] ?? 3) === t));
  if ((d.blocks || []).length <= 3 || opts.order) return layoutOnce(d, opts, opts.order || [0, 1, 2, 3]);
  const perms = (xs) => (xs.length <= 1 ? [xs] : xs.flatMap((x, i) => perms([...xs.slice(0, i), ...xs.slice(i + 1)]).map((p) => [x, ...p])));
  // power stays above the processor and the peripherals: supplies flow down the page
  const readable = (o) => !o.includes(1) || [2, 3].every((t) => !o.includes(t) || o.indexOf(1) < o.indexOf(t));
  let best = null;
  for (const order of perms(present).filter(readable)) {
    for (const less of [0, 1]) {
      const L = layoutOnce(d, opts, order, less);
      if (!L.blocks.length) return L;
      L.score = scoreLayout(L) + (order.join() === present.join() ? 0 : 0.5) + less * 0.25;
      L.args = [order, less, new Map()];
      if (!best || L.score < best.score) best = L;
    }
  }
  // then single blocks moved into another tier's row (a connector beside the chip it serves), kept when cleaner
  const [order, less] = best.args;
  let over = new Map();
  for (let pass = 0; pass < 3; pass++) {
    let improved = false;
    for (const b of d.blocks || []) {
      if (b.kind === "power") continue;                  // the supply chain keeps its row
      const id = String(b.id), own = over.has(id) ? over.get(id) : TIER[b.kind] ?? 3;
      for (const t of order) {
        if (t === own || (t === 1 && b.kind !== "connector")) continue;     // only an input joins the power row
        const trial = new Map(over).set(id, t);
        const L = layoutOnce(d, opts, order, less, trial);
        L.score = scoreLayout(L) + (order.join() === present.join() ? 0 : 0.5) + less * 0.25 + trial.size * 0.4;
        if (L.score < best.score - 0.3) { best = L; over = trial; improved = true; }
      }
    }
    if (!improved) break;
  }
  return best;
}

// How clean a layout is (lower is better): see layoutDiagram.
export function scoreLayout(L) {
  const H = L.wires.filter((s) => Math.abs(s.y1 - s.y2) < 0.01), V = L.wires.filter((s) => Math.abs(s.x1 - s.x2) < 0.01);
  let cross = 0, stacked = 0, length = 0;
  for (const h of H) for (const v of V) {
    if (h.own === v.own) continue;
    const [hx0, hx1] = [Math.min(h.x1, h.x2), Math.max(h.x1, h.x2)], [vy0, vy1] = [Math.min(v.y1, v.y2), Math.max(v.y1, v.y2)];
    if (v.x1 > hx0 + 0.5 && v.x1 < hx1 - 0.5 && h.y1 > vy0 + 0.5 && h.y1 < vy1 - 0.5) cross++;
  }
  const along = (a, b, p, q) => Math.min(Math.max(a.p, a.q), Math.max(b.p, b.q)) - Math.max(Math.min(a.p, a.q), Math.min(b.p, b.q));
  for (const [list, key, p, q] of [[H, "y1", "x1", "x2"], [V, "x1", "y1", "y2"]]) {
    for (let i = 0; i < list.length; i++) for (let j = i + 1; j < list.length; j++) {
      const a = list[i], b = list[j];
      if (a.own !== b.own && Math.abs(a[key] - b[key]) < 2 && along({ p: a[p], q: a[q] }, { p: b[p], q: b[q] }) > 2) stacked++;
    }
  }
  for (const s of L.wires) length += Math.abs(s.x2 - s.x1) + Math.abs(s.y2 - s.y1);
  const lanes = new Set(L.wires.filter((s) => s.own && s.own.lane != null).map((s) => s.own)).size;
  const tags = L.blocks.reduce((n, b) => n + b.tags.length, 0);          // a supply shown as a tag, not a wire
  return cross * 3 + stacked * 40 + lanes * 2 + tags * 2.5 + (L.hidden || []).length * 6 + length / 250 + L.box.h / 300 + L.box.w / 600;
}

function layoutOnce(d, opts, order, less = 0, over = null) {
  const measure = opts.measure || estimate;
  const avail = Math.max(320, opts.width || 560);
  const pillW = (t) => measure(t, "l") + 12;
  const blocks = (d.blocks || []).map((b, i) => ({ id: String(b.id), label: b.label, kind: b.kind || "other", note: b.note || "", i,
    tier: over && over.has(String(b.id)) ? over.get(String(b.id)) : TIER[b.kind] ?? 3, tags: [] }));
  const out = { blocks, wires: [], dots: [], pills: [], box: { x: 0, y: 0, w: 0, h: 0 } };
  if (!blocks.length) return out;
  const byId = new Map(blocks.map((b) => [b.id, b]));

  // One wire per pair of blocks and kind, the labels joined ("5V, GND").
  const merged = new Map();
  for (const l of d.links || []) {
    const a = byId.get(String(l.from)), b = byId.get(String(l.to));
    if (!a || !b || a === b) continue;
    const kind = KINDS.includes(l.kind) ? l.kind : "signal";
    const key = [a.id, b.id].sort().join("\u0001") + "\u0001" + kind;
    let m = merged.get(key);
    if (!m) merged.set(key, (m = { a, b, kind, labels: [] }));
    const lab = String(l.label || "").trim();
    if (lab && !m.labels.includes(lab)) m.labels.push(lab);
  }
  const links = [...merged.values()].map((m) => ({ a: m.a, b: m.b, kind: m.kind, label: m.labels.join(", ") }));

  // Tiers, each ordered by where its blocks connect (one sweep down, one up), then wrapped into rows.
  const tiers = order.map((t) => blocks.filter((b) => b.tier === t)).filter((t) => t.length);
  const tierOf = new Map();
  tiers.forEach((t, ti) => t.forEach((b) => tierOf.set(b, ti)));
  const nbrs = new Map(blocks.map((b) => [b, []]));
  for (const l of links) { nbrs.get(l.a).push(l.b); nbrs.get(l.b).push(l.a); }
  const place = (b) => { const t = tiers[tierOf.get(b)]; return t.length > 1 ? t.indexOf(b) / (t.length - 1) : 0.5; };
  const reorder = (ti, above) => {
    const t = tiers[ti];
    const key = new Map(t.map((b) => {
      const ns = nbrs.get(b).filter((n) => (above ? tierOf.get(n) < ti : tierOf.get(n) > ti));
      return [b, ns.length ? ns.reduce((s, n) => s + place(n), 0) / ns.length : place(b)];
    }));
    t.sort((x, y) => key.get(x) - key.get(y) || x.i - y.i);
  };
  for (let ti = 1; ti < tiers.length; ti++) reorder(ti, true);
  for (let ti = tiers.length - 2; ti >= 0; ti--) reorder(ti, false);
  // As many blocks to a row as stay readable (up to four), as wide as the column allows.
  const room = (n) => (avail - 2 * PAD - 40 - (n - 1) * CG) / n;
  const widest = Math.max(...tiers.map((t) => t.length));
  let cap = 2;
  for (let n = Math.min(4, Math.max(2, widest)); n >= 2; n--) if (room(n) >= MIN_W || n === 2) { cap = n; break; }
  cap = Math.max(2, cap - less);
  const W = clamp(room(cap), MIN_W, MAX_W);
  out.bw = W;
  const rows = [];
  for (const t of tiers) {
    const per = Math.ceil(t.length / Math.ceil(t.length / cap));
    for (let k = 0; k < t.length; k += per) rows.push(t.slice(k, k + per));
  }
  rows.forEach((r, ri) => r.forEach((b, ci) => { b.row = ri; b.col = ci; }));
  const R = rows.length;
  for (const b of blocks) Object.assign(b, blockText(b, measure, W), { w: W });
  const rowH = rows.map((r) => Math.max(...r.map((b) => b.h)));
  rows.forEach((r, ri) => r.forEach((b) => (b.h = rowH[ri])));

  // Wires within a row: between neighbours a straight line (the gap widened for its label), otherwise
  // down and along the channel under the row.
  const hlinks = [], ulinks = [], gapAfter = new Map();
  for (const l of links) {
    if (l.a.row !== l.b.row) continue;
    const [p, q] = l.a.col < l.b.col ? [l.a, l.b] : [l.b, l.a];
    if (q.col === p.col + 1) {
      hlinks.push({ p, q, kind: l.kind, label: l.label });
      if (l.label) gapAfter.set(p, Math.max(gapAfter.get(p) || CG, pillW(l.label) + 16));
    } else ulinks.push({ p, q, kind: l.kind, label: l.label, r: p.row });
  }
  const gap = (b) => gapAfter.get(b) || CG;
  const rowW = rows.map((r) => r.reduce((s, b, i) => s + W + (i < r.length - 1 ? gap(b) : 0), 0));
  const content = Math.max(...rowW);
  rows.forEach((r, ri) => { let x = (content - rowW[ri]) / 2; for (const b of r) { b.x = x; x += W + gap(b); } });
  const cx = (b) => b.x + W / 2;

  // Wires between rows, one per source, label and destination row: a bus is one wire with several drops.
  const nets = [], groups = new Map();
  for (const l of links) {
    if (l.a.row === l.b.row) continue;
    const [a, b] = l.a.row < l.b.row ? [l.a, l.b] : [l.b, l.a];
    if (l.kind === "power" && b.row - a.row >= 2) {
      const t = l.label || "power";
      if (!b.tags.includes(t)) b.tags.push(t);
      continue;
    }
    const key = [a.id, l.label, l.kind, b.row].join("\u0001");
    let n = groups.get(key);
    if (!n) { groups.set(key, (n = { src: a, dsts: [], label: l.label, kind: l.kind, r: a.row, s: b.row, dx: new Map() })); nets.push(n); }
    n.dsts.push(b);
  }
  for (const b of blocks) {
    b.tagText = b.tags.length ? fit(b.tags.join(", "), W - 44, "l", measure) : "";
    b.tagW = b.tagText ? pillW(b.tagText) : 0;
    b.tagX = b.x + W - EDGE - b.tagW / 2;
  }
  const arrRange = (b) => [b.x + EDGE, b.x + W - EDGE - (b.tagW ? b.tagW + 8 : 0)];

  // Where each wire leaves its block (the bottom edge) ...
  const dep = new Map(blocks.map((b) => [b, []]));
  for (const n of nets) dep.get(n.src).push({ n, want: n.dsts.reduce((s, b) => s + cx(b), 0) / n.dsts.length });
  for (const u of ulinks) { dep.get(u.p).push({ u, end: "p", want: cx(u.q) }); dep.get(u.q).push({ u, end: "q", want: cx(u.p) }); }
  for (const b of blocks) {
    const list = dep.get(b);
    if (!list.length) continue;
    let xs;
    if (list.length === 1) {
      let x = cx(b);
      const n = list[0].n;
      if (n && n.dsts.length === 1) {            // straight down when the two blocks overlap
        const t = n.dsts[0], [tlo, thi] = arrRange(t);
        const lo = Math.max(b.x + EDGE, tlo), hi = Math.min(b.x + W - EDGE, thi);
        if (lo <= hi && (x < lo || x > hi)) x = (lo + hi) / 2;
      }
      xs = [x];
    } else {
      list.sort((p, q) => p.want - q.want);
      xs = list.map((_, i) => b.x + EDGE + ((W - 2 * EDGE) * (i + 1)) / (list.length + 1));
    }
    list.forEach((e, i) => { if (e.n) e.n.tx = xs[i]; else e.u[e.end + "x"] = xs[i]; });
  }
  // ... the side lane of a wire that skips a row ...
  let nL = 0, nR = 0;
  for (const n of nets) {
    if (n.s - n.r < 2) continue;
    const mid = rows.slice(n.r + 1, n.s);
    const lo = Math.min(...mid.map((r) => r[0].x)), hi = Math.max(...mid.map((r) => r[r.length - 1].x + W));
    const avg = (n.tx + n.dsts.reduce((s, b) => s + cx(b), 0) / n.dsts.length) / 2;
    n.lane = avg - lo < hi - avg ? lo - 16 - LANE * nL++ : hi + 16 + LANE * nR++;
  }
  // ... and where it enters each block it reaches (the top edge, clear of the block's supply tag).
  const arr = new Map(blocks.map((b) => [b, []]));
  for (const n of nets) for (const b of n.dsts) arr.get(b).push({ n, want: n.lane ?? n.tx });
  for (const b of blocks) {
    const list = arr.get(b);
    if (!list.length) continue;
    const [lo, hi] = arrRange(b);
    if (list.length === 1) {
      const e = list[0];
      e.n.dx.set(b, e.want >= lo && e.want <= hi ? e.want : clamp(cx(b), lo, hi));
    } else {
      list.sort((p, q) => p.want - q.want);
      list.forEach((e, i) => e.n.dx.set(b, lo + ((hi - lo) * (i + 1)) / (list.length + 1)));
    }
  }

  // Channels: channel c lies above row c (0 is the top margin, R the space under the last row). Each has
  // a band for labels under the row above it and one over the row below it, and tracks between them.
  const band = () => ({ pills: [], verticals: [], levels: [] });
  const ch = Array.from({ length: R + 1 }, () => ({ tracks: [], top: band(), bot: band() }));
  const asks = [];      // labels to place: where they may go, in order of preference
  const labelled = new Map(blocks.map((b) => [b, { dep: 0, arr: 0 }]));
  for (const n of nets) if (n.label) { labelled.get(n.src).dep++; for (const b of n.dsts) labelled.get(b).arr++; }
  for (const n of nets) {
    const dxs = n.dsts.map((b) => n.dx.get(b));
    const multi = n.dsts.length > 1;
    let near, far;
    if (n.lane == null) {
      const k = ch[n.s];
      n.straight = !multi && Math.abs(dxs[0] - n.tx) < 0.5;
      if (!n.straight) k.tracks.push((n.t1 = { own: n, entries: [n.tx], exits: dxs, h: TRACK }));
      k.top.verticals.push({ x: n.tx, own: n });
      dxs.forEach((x) => k.bot.verticals.push({ x, own: n }));
      near = [k.top, n.tx]; far = [k.bot, dxs[0]];
    } else {
      const k1 = ch[n.r + 1], k2 = ch[n.s];
      k1.tracks.push((n.t1 = { own: n, entries: [n.tx], exits: [n.lane], h: TRACK }));
      k1.top.verticals.push({ x: n.tx, own: n }); k1.bot.verticals.push({ x: n.lane, own: n });
      for (let c = n.r + 2; c < n.s; c++) { ch[c].top.verticals.push({ x: n.lane, own: n }); ch[c].bot.verticals.push({ x: n.lane, own: n }); }
      k2.tracks.push((n.t2 = { own: n, entries: [n.lane], exits: dxs, h: TRACK }));
      k2.top.verticals.push({ x: n.lane, own: n });
      dxs.forEach((x) => k2.bot.verticals.push({ x, own: n }));
      near = [k1.top, n.tx]; far = [k2.bot, dxs[0]];
    }
    // A bus is labelled where it leaves its source; a point-to-point wire at whichever end is less crowded.
    if (n.label) {
      const crowdedEnd = labelled.get(n.dsts[0]).arr > labelled.get(n.src).dep;
      asks.push({ own: n, text: n.label, kind: n.kind, rank: multi ? 2 : 1, at: multi ? [near] : crowdedEnd ? [near, far] : [far, near] });
    }
  }
  for (const u of ulinks) {
    const k = ch[u.r + 1];
    k.tracks.push((u.t = { own: u, entries: [u.px, u.qx], exits: [], h: u.label ? PILL + 6 : TRACK }));
    k.top.verticals.push({ x: u.px, own: u }, { x: u.qx, own: u });
  }
  for (const b of blocks) {
    if (!b.tagText) continue;
    const k = ch[b.row], own = { tag: b };
    k.bot.verticals.push({ x: b.tagX, own });
    asks.push({ own, text: b.tagText, kind: "power", rank: 0, at: [[k.bot, b.tagX]] });
  }
  asks.sort((a, b) => a.rank - b.rank || a.at[0][1] - b.at[0][1]);
  out.hidden = [];
  for (const a of asks) {
    const ok = a.at.some(([bd, x]) => tryPill(bd, { own: a.own, text: a.text, kind: a.kind, x, w: pillW(a.text) }));
    if (!ok) { out.hidden.push(a.text); a.own.hiddenLabel = a.text; }     // no clean spot: the wire's tooltip carries it
  }
  for (let c = 0; c <= R; c++) {
    const k = ch[c];
    k.tracks = orderTracks(k.tracks);
    k.tracksH = k.tracks.reduce((s, t) => s + t.h, 0);
    k.topB = k.top.levels.length ? 5 + k.top.levels.length * LEVEL : 0;
    k.botB = k.bot.levels.length ? 5 + k.bot.levels.length * LEVEL : 0;
    const need = k.topB + k.tracksH + k.botB;
    k.h = c === 0 ? 0 : c === R ? (k.tracks.length ? need + 14 : 0) : Math.max(36, need + 18);
  }
  let y = 0;
  for (let c = 0; c <= R; c++) {
    const k = ch[c];
    k.y0 = y; y += k.h; k.y1 = y;
    let cur = (k.y0 + k.topB + k.y1 - k.botB) / 2 - k.tracksH / 2;
    for (const t of k.tracks) { t.y = cur + t.h / 2; cur += t.h; }
    for (const p of k.top.pills) p.y = k.y0 + 4 + p.level * LEVEL;
    for (const p of k.bot.pills) p.y = k.y1 - 4 - PILL - p.level * LEVEL;
    if (c < R) { for (const b of rows[c]) b.y = y; y += rowH[c]; }
  }

  // The drawing.
  const wires = out.wires, dots = out.dots, pills = out.pills;
  const seg = (own, kind, x1, y1, x2, y2) => { if (Math.abs(x1 - x2) > 0.01 || Math.abs(y1 - y2) > 0.01) wires.push({ own, kind, x1, y1, x2, y2 }); };
  const rail = (own, kind, ry, above, below) => {
    const xs = [...above, ...below], lo = Math.min(...xs), hi = Math.max(...xs);
    seg(own, kind, lo, ry, hi, ry);
    for (const x of new Set(xs.map((v) => Math.round(v * 10) / 10))) {
      const n = above.filter((v) => Math.abs(v - x) < 0.1).length + below.filter((v) => Math.abs(v - x) < 0.1).length
        + (x > lo + 0.1 ? 1 : 0) + (x < hi - 0.1 ? 1 : 0);
      if (n >= 3) dots.push({ own, kind, x, y: ry });
    }
  };
  for (const n of nets) {
    const bot = n.src.y + n.src.h, dxs = n.dsts.map((b) => n.dx.get(b));
    if (n.straight) { seg(n, n.kind, n.tx, bot, n.tx, n.dsts[0].y); continue; }
    seg(n, n.kind, n.tx, bot, n.tx, n.t1.y);
    let from = n.t1.y, above = [n.tx];
    if (n.lane != null) {
      seg(n, n.kind, Math.min(n.tx, n.lane), n.t1.y, Math.max(n.tx, n.lane), n.t1.y);
      seg(n, n.kind, n.lane, n.t1.y, n.lane, n.t2.y);
      from = n.t2.y; above = [n.lane];
    }
    rail(n, n.kind, from, above, dxs);
    n.dsts.forEach((b, i) => seg(n, n.kind, dxs[i], from, dxs[i], b.y));
  }
  for (const u of ulinks) {
    const by = u.p.y + u.p.h;
    seg(u, u.kind, u.px, by, u.px, u.t.y);
    seg(u, u.kind, Math.min(u.px, u.qx), u.t.y, Math.max(u.px, u.qx), u.t.y);
    seg(u, u.kind, u.qx, by, u.qx, u.t.y);
  }
  const perPair = new Map();
  for (const h of hlinks) { const k = h.p.id + "\u0001" + h.q.id; perPair.set(k, [...(perPair.get(k) || []), h]); }
  for (const list of perPair.values()) {
    list.forEach((h, i) => {
      const ym = h.p.y + h.p.h / 2 + (i - (list.length - 1) / 2) * 20, x0 = h.p.x + W, x1 = h.q.x;
      seg(h, h.kind, x0, ym, x1, ym);
      if (h.label) pills.push({ own: h, kind: h.kind, text: h.label, x: (x0 + x1) / 2 - pillW(h.label) / 2, y: ym - PILL / 2, w: pillW(h.label), h: PILL });
    });
  }
  // A label on a within-row wire sits on its track, slid along it clear of the wires that cross it.
  for (const u of ulinks) {
    if (!u.label) continue;
    const w = pillW(u.label), lo = Math.min(u.px, u.qx), hi = Math.max(u.px, u.qx), ty = u.t.y;
    const crosses = (x0) => wires.some((s) => s.own !== u && s.x1 === s.x2 && s.x1 > x0 - 3 && s.x1 < x0 + w + 3
      && Math.min(s.y1, s.y2) < ty + PILL / 2 && Math.max(s.y1, s.y2) > ty - PILL / 2);
    let best = null;
    for (let k = 0; k <= 16 && best == null; k++) {
      const x0 = lo + 6 + ((hi - lo - 12 - w) * k) / 16;
      if (!crosses(x0)) best = x0;
    }
    if (best == null) { out.hidden.push(u.label); u.hiddenLabel = u.label; continue; }
    pills.push({ own: u, kind: u.kind, text: u.label, x: best, y: ty - PILL / 2, w, h: PILL });
  }
  for (const k of ch) {
    for (const p of [...k.top.pills, ...k.bot.pills]) {
      pills.push({ own: p.own, kind: p.kind, text: p.text, x: p.cx - p.w / 2, y: p.y, w: p.w, h: PILL, tag: !!p.own.tag });
      if (p.own.tag) seg(p.own, "power", p.cx, p.y + PILL, p.cx, p.own.tag.y);      // the supply's stub into its block
    }
  }

  // Bounds, with room for everything drawn.
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  const grow = (a, b, c, e) => { x0 = Math.min(x0, a); y0 = Math.min(y0, b); x1 = Math.max(x1, c); y1 = Math.max(y1, e); };
  for (const b of blocks) grow(b.x, b.y, b.x + W, b.y + b.h);
  for (const s of wires) grow(Math.min(s.x1, s.x2), Math.min(s.y1, s.y2), Math.max(s.x1, s.x2), Math.max(s.y1, s.y2));
  for (const p of pills) grow(p.x, p.y, p.x + p.w, p.y + p.h);
  out.box = { x: x0 - PAD, y: y0 - PAD, w: x1 - x0 + 2 * PAD, h: y1 - y0 + 2 * PAD };
  return out;
}

export function diagramSVG(d, opts = {}) {
  const L = layoutDiagram(d, opts);
  if (!L.blocks.length) return "";
  const byOwn = new Map();
  for (const s of L.wires) {
    if (!byOwn.has(s.own)) byOwn.set(s.own, { kind: s.kind, d: "", tip: s.own.hiddenLabel || "" });
    byOwn.get(s.own).d += `M${r1(s.x1)} ${r1(s.y1)}${s.x1 === s.x2 ? `V${r1(s.y2)}` : `H${r1(s.x2)}`}`;
  }
  const wires = [...byOwn.values()].map((w) => `<path class="dl ${w.kind}" d="${w.d}">${w.tip ? `<title>${esc(w.tip)}</title>` : ""}</path>`).join("");
  const dots = L.dots.map((p) => `<circle class="dl-j ${p.kind}" cx="${r1(p.x)}" cy="${r1(p.y)}" r="2.8"/>`).join("");
  const pills = L.pills.map((p) => `<g class="dl-l ${p.kind}${p.tag ? " tag" : ""}"><rect x="${r1(p.x)}" y="${r1(p.y)}" width="${r1(p.w)}" height="${p.h}" rx="5"/>` +
    `<text x="${r1(p.x + p.w / 2)}" y="${r1(p.y + 11.5)}">${esc(p.text)}</text></g>`).join("");
  const blocks = L.blocks.map((b) => {
    const textH = b.title.length * 15 + b.note.length * 13.5;
    let ty = b.y + (b.h - textH) / 2, t = "";
    for (const s of b.title) { t += `<text class="dt" x="${r1(b.x + 28)}" y="${r1(ty + 11.5)}">${esc(s)}</text>`; ty += 15; }
    for (const s of b.note) { t += `<text class="dn" x="${r1(b.x + 28)}" y="${r1(ty + 10)}">${esc(s)}</text>`; ty += 13.5; }
    const full = [String(b.label || b.id).replace(/\n+/g, " "), b.note].filter(Boolean).join(" · ");
    return `<g class="db ${esc(b.kind)}"><title>${esc(full)}</title><rect x="${r1(b.x)}" y="${r1(b.y)}" width="${r1(b.w)}" height="${r1(b.h)}" rx="9"/>` +
      `<circle cx="${r1(b.x + 15)}" cy="${r1(b.y + b.h / 2)}" r="4.5"/>${t}</g>`;
  }).join("");
  const { x, y, w, h } = L.box;
  return `<svg viewBox="${r1(x)} ${r1(y)} ${r1(w)} ${r1(h)}" width="${r1(w)}" style="max-width:100%;height:auto">${wires}${dots}${blocks}${pills}</svg>`;
}
