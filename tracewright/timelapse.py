"""The board's timelapse: a frame every time the board changes (Claude's placement and routing, the
router net by net, the user's edits in KiCad), kept in .tracewright/timelapse/frames.jsonl so the
board's whole evolution can be played back and exported as a video. A project with checkpoints
can also be rebuilt from its git history.

A frame holds only what changed since the one before (parts moved, tracks and vias added or
removed, pours refilled); every KEYFRAME-th frame is a full picture so the player can seek. Parts
are poses (x, y, angle, side); the player draws them with their current footprint geometry.
"""
import os, json, time, math, threading, hashlib, subprocess

KEYFRAME = 40
MAX_FRAMES = 20000


def _r(v, n=3):
    return round(float(v), n)


def _simplify(pts, tol=0.04):
    """Douglas-Peucker on a closed loop, without recursion (fills have thousands of points; the player
    needs far fewer)."""
    n = len(pts)
    if n < 8:
        return pts
    keep = [False] * n
    keep[0] = True
    x0, y0 = pts[0]
    far = max(range(n), key=lambda i: (pts[i][0] - x0) ** 2 + (pts[i][1] - y0) ** 2)
    keep[far] = True
    stack = [(0, far), (far, n)]                   # (i, j): the run between two kept points (j == n: back to 0)
    while stack:
        i, j = stack.pop()
        if j - i < 2:
            continue
        ax, ay = pts[i]
        bx, by = pts[j % n]
        dx, dy = bx - ax, by - ay
        L = math.hypot(dx, dy)
        best, bi = -1.0, -1
        for k in range(i + 1, j):
            x, y = pts[k]
            d = abs(dy * x - dx * y + bx * ay - by * ax) / L if L > 1e-9 else math.hypot(x - ax, y - ay)
            if d > best:
                best, bi = d, k
        if bi >= 0 and best > tol:
            keep[bi] = True
            stack.append((i, bi))
            stack.append((bi, j))
    out = [pts[i] for i in range(n) if keep[i]]
    return out if len(out) >= 3 else pts


def picture(board):
    """The board as the timelapse sees it: {fp, tr, vi, zn, ol, stats}."""
    fp = {}
    on = 0
    ol = board.outline[0] if board.outline else None
    from tw import geom
    for f in board.fp_list:
        if not f.ref or f.ref.startswith("#"):
            continue
        fp[f.ref] = [_r(f.x), _r(f.y), _r(f.angle, 2), f.side]
        if ol is None or geom.inside((f.x, f.y), ol):
            on += 1
    tr = {}
    length = 0.0
    routed = set()
    for t in board.tracks:
        row = [_r(t.a[0]), _r(t.a[1]), _r(t.b[0]), _r(t.b[1]), _r(t.w), t.layer, t.net or ""]
        if t.mid:
            row += [_r(t.mid[0]), _r(t.mid[1])]
        tid = t.uuid or "g" + hashlib.sha1(json.dumps(row).encode()).hexdigest()[:12]
        tr[tid] = row
        length += t.length()
        if t.net:
            routed.add(t.net)
    vi = {}
    for v in board.vias:
        row = [_r(v.x), _r(v.y), _r(v.d), _r(v.drill), v.net or ""]
        vi[v.uuid or "v" + hashlib.sha1(json.dumps(row).encode()).hexdigest()[:12]] = row
    zn = {}
    for i, z in enumerate(board.zones):
        if z.is_rule_area or not z.fills:
            continue
        zid = z.uuid or f"{z.name}:{z.net}:{i}"
        layers = {}
        for layer, polys in z.fills.items():
            layers[layer] = [[[_r(x, 1), _r(y, 1)] for x, y in _simplify(pl, 0.12)] for pl in polys if len(pl) >= 3]
        zn[zid] = {"net": z.net, "l": layers}
    outline = [[[_r(x), _r(y)] for x, y in loop] for loop in board.outline]
    nets = {p.net for p in board.pads() if p.net}
    return {"fp": fp, "tr": tr, "vi": vi, "zn": zn, "ol": outline,
            "stats": {"on": on, "parts": len(fp), "tracks": len(tr), "vias": len(vi), "len": round(length), "routed": len(routed & nets),
                      "nets": len(nets)}}


def _zone_sig(z):
    return hashlib.sha1(json.dumps(z, sort_keys=True).encode()).hexdigest()[:16]


class Timelapse:
    def __init__(self, root):
        self.root = root
        self.dir = os.path.join(root, ".tracewright", "timelapse")
        self.path = os.path.join(self.dir, "frames.jsonl")
        self.lock = threading.Lock()
        self.state = None               # the last full picture recorded
        self.n = 0
        self.tmp = set()                # the router's in-flight tracks
        self.loaded = False

    # ------------------------------------------------------------------ reading
    def _load(self):
        if self.loaded:
            return
        self.loaded = True
        st = None
        n = 0
        for fr in self.frames():
            st = apply(st, fr)
            n += 1
        self.state, self.n = st, n
        if st:
            self.tmp = {k for k in st["tr"] if k.startswith("r:")}

    def frames(self, since=0):
        out = []
        try:
            with open(self.path, encoding="utf-8") as f:
                for i, line in enumerate(f):
                    if i < since or not line.strip():
                        continue
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            pass
        return out

    def count(self):
        with self.lock:
            self._load()
            return self.n

    # ------------------------------------------------------------------ writing
    def _write(self, fr):
        os.makedirs(self.dir, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(fr, separators=(",", ":")) + "\n")
        self.state = apply(self.state, fr)
        self.n += 1

    def record(self, board, src, note="", t=None):
        """A frame for this board, if anything changed; returns the frame (or None)."""
        pic = picture(board)
        with self.lock:
            self._load()
            if self.n >= MAX_FRAMES:
                return None
            prev = self.state
            key = prev is None or self.n % KEYFRAME == 0
            fr = {"t": round(t or time.time(), 2), "src": src}
            if key:
                fr.update({"k": 1, "fp": pic["fp"], "tr+": [[k] + v for k, v in pic["tr"].items()],
                           "vi+": [[k] + v for k, v in pic["vi"].items()], "zn": pic["zn"], "ol": pic["ol"]})
                changed = prev is None or _differs(prev, pic)
            else:
                fr.update(_delta(prev, pic))
                changed = any(fr.get(k) for k in ("fp", "tr+", "tr-", "vi+", "vi-", "zn", "ol"))
            if not changed:
                return None
            fr["stats"] = pic["stats"]
            fr["note"] = note or describe(prev, pic, src)
            self.tmp = set()
            self._write(fr)
            return {k: fr[k] for k in ("t", "src", "note", "stats")} | {"n": self.n}

    def record_route(self, net, tracks, vias, done=None, total=None):
        """A frame from the router's live stream: one net's tracks, before the file is written."""
        with self.lock:
            self._load()
            if self.state is None or self.n >= MAX_FRAMES:
                return None
            add, vadd = [], []
            for t in tracks or []:
                tid = f"r:{len(self.tmp)}"
                self.tmp.add(tid)
                a, b = t.get("a"), t.get("b")
                if a and b:
                    add.append([tid, _r(a[0]), _r(a[1]), _r(b[0]), _r(b[1]), _r(t.get("w", 0.2)), t.get("layer", "F.Cu"), net])
            for v in vias or []:
                vid = f"r:{len(self.tmp)}"
                self.tmp.add(vid)
                vadd.append([vid, _r(v.get("x", 0)), _r(v.get("y", 0)), _r(v.get("d", 0.6)), _r(v.get("drill", 0.3)), net])
            if not add and not vadd:
                return None
            st = dict(self.state["stats"]) if self.state.get("stats") else {}
            fr = {"t": round(time.time(), 2), "src": "router", "tr+": add, "vi+": vadd, "tmp": 1, "stats": st,
                  "note": f"routed {net.rsplit('/', 1)[-1]}" + (f" ({done}/{total})" if done and total else "")}
            self._write(fr)
            return {k: fr[k] for k in ("t", "src", "note")} | {"n": self.n}

    def clear(self):
        with self.lock:
            try:
                os.remove(self.path)
            except OSError:
                pass
            self.state, self.n, self.tmp, self.loaded = None, 0, set(), True

    # ------------------------------------------------------------------ from the project's history
    def from_history(self, pcb_rel, progress=None):
        """Rebuild the frames from every git commit that changed the board (oldest first)."""
        from tw.board import Board
        r = subprocess.run(["git", "-C", self.root, "log", "--reverse", "--format=%H%x09%ct%x09%s", "--", pcb_rel],
                           capture_output=True, text=True, timeout=60)
        commits = [l.split("\t", 2) for l in r.stdout.splitlines() if l.count("\t") >= 2]
        self.clear()
        for i, (h, ct, subj) in enumerate(commits):
            show = subprocess.run(["git", "-C", self.root, "show", f"{h}:{pcb_rel}"], capture_output=True, text=True, timeout=60)
            if show.returncode != 0 or not show.stdout.startswith("(kicad_pcb"):
                continue
            try:
                b = Board.parse(show.stdout)
            except Exception:
                continue
            self.record(b, "history", note=subj[:90], t=float(ct))
            if progress:
                progress(i + 1, len(commits))
        return len(commits)


def apply(state, fr):
    """The picture after a frame."""
    if fr.get("k") or state is None:
        st = {"fp": dict(fr.get("fp") or {}), "tr": {r[0]: r[1:] for r in fr.get("tr+", [])},
              "vi": {r[0]: r[1:] for r in fr.get("vi+", [])}, "zn": dict(fr.get("zn") or {}), "ol": fr.get("ol") or [],
              "stats": fr.get("stats") or {}}
        return st
    st = {"fp": dict(state["fp"]), "tr": dict(state["tr"]), "vi": dict(state["vi"]), "zn": dict(state["zn"]),
          "ol": state["ol"], "stats": fr.get("stats") or state.get("stats") or {}}
    for ref, pose in (fr.get("fp") or {}).items():
        if pose is None:
            st["fp"].pop(ref, None)
        else:
            st["fp"][ref] = pose
    if not fr.get("tmp"):                                   # a file frame retires the router's in-flight tracks
        for k in [k for k in st["tr"] if k.startswith("r:")]:
            del st["tr"][k]
        for k in [k for k in st["vi"] if k.startswith("r:")]:
            del st["vi"][k]
    for k in fr.get("tr-", []):
        st["tr"].pop(k, None)
    for r in fr.get("tr+", []):
        st["tr"][r[0]] = r[1:]
    for k in fr.get("vi-", []):
        st["vi"].pop(k, None)
    for r in fr.get("vi+", []):
        st["vi"][r[0]] = r[1:]
    for k, z in (fr.get("zn") or {}).items():
        if z is None:
            st["zn"].pop(k, None)
        else:
            st["zn"][k] = z
    if fr.get("ol"):
        st["ol"] = fr["ol"]
    return st


def _delta(prev, pic):
    d = {}
    fp = {r: p for r, p in pic["fp"].items() if prev["fp"].get(r) != p}
    fp.update({r: None for r in prev["fp"] if r not in pic["fp"]})
    if fp:
        d["fp"] = fp
    live = {k: v for k, v in prev["tr"].items() if not k.startswith("r:")}
    add = [[k] + v for k, v in pic["tr"].items() if live.get(k) != v]
    rem = [k for k in live if k not in pic["tr"]]
    if add:
        d["tr+"] = add
    if rem:
        d["tr-"] = rem
    vlive = {k: v for k, v in prev["vi"].items() if not k.startswith("r:")}
    vadd = [[k] + v for k, v in pic["vi"].items() if vlive.get(k) != v]
    vrem = [k for k in vlive if k not in pic["vi"]]
    if vadd:
        d["vi+"] = vadd
    if vrem:
        d["vi-"] = vrem
    zn = {}
    for k, z in pic["zn"].items():
        if k not in prev["zn"] or _zone_sig(prev["zn"][k]) != _zone_sig(z):
            zn[k] = z
    for k in prev["zn"]:
        if k not in pic["zn"]:
            zn[k] = None
    if zn:
        d["zn"] = zn
    if pic["ol"] != prev.get("ol"):
        d["ol"] = pic["ol"]
    return d


def _differs(prev, pic):
    return bool(_delta(prev, pic))


def describe(prev, pic, src):
    """A one-line account of what a frame changed ("placed 12 parts", "+86 tracks, +9 vias")."""
    if prev is None:
        s = pic["stats"]
        return f"{s['parts']} parts, {s['tracks']} tracks, {s['vias']} vias"
    moved = [r for r, p in pic["fp"].items() if r in prev["fp"] and prev["fp"][r][:3] != p[:3]]
    new = [r for r in pic["fp"] if r not in prev["fp"]]
    gone = [r for r in prev["fp"] if r not in pic["fp"]]
    live = {k for k in prev["tr"] if not k.startswith("r:")}
    tadd = len([k for k in pic["tr"] if k not in live])
    trem = len([k for k in live if k not in pic["tr"]])
    vlive = {k for k in prev["vi"] if not k.startswith("r:")}
    vadd = len([k for k in pic["vi"] if k not in vlive])
    vrem = len([k for k in vlive if k not in pic["vi"]])
    parts = []
    if moved:
        parts.append(f"moved {', '.join(moved[:3])}" + (f" +{len(moved) - 3}" if len(moved) > 3 else ""))
    if new:
        parts.append(f"added {len(new)} part{'s' if len(new) > 1 else ''}")
    if gone:
        parts.append(f"removed {', '.join(gone[:3])}")
    if tadd or trem:
        parts.append(" ".join(x for x in (f"+{tadd}" if tadd else "", f"-{trem}" if trem else "") if x) + " tracks")
    if vadd or vrem:
        parts.append(" ".join(x for x in (f"+{vadd}" if vadd else "", f"-{vrem}" if vrem else "") if x) + " vias")
    if any(k not in prev["zn"] or _zone_sig(prev["zn"][k]) != _zone_sig(z) for k, z in pic["zn"].items()):
        parts.append("pours refilled")
    return "; ".join(parts) or "board saved"
