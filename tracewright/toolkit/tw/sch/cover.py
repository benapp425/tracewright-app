"""The cover page (the root sheet) arranged once the hierarchy is drawn: each sheet symbol as tall and wide as its pins
need, the sheet symbols in page order in rows, then the cover's own blocks and texts (mechanical parts, contents,
notes), nothing on anything else, clear of the title block, on the smallest paper that holds it.

A script places its sheet symbols before it knows their pins; the hierarchy adds the pins afterwards, so boxes grew
over each other and over the contents. This runs inside style.convert, before KiCad's netlist proof: an arrangement
that changed a connection is never written.

    arrange(text) -> (text, report)        the root sheet's file, rearranged
"""
import re
from .. import sexp, font

PAPERS = (("A4", 297.0, 210.0), ("A3", 420.0, 297.0), ("A2", 594.0, 420.0), ("A1", 841.0, 594.0))
MARGIN = 15.24                  # from the paper's edge to the first unit (the drawing sheet's frame is 10 mm in)
TITLE = (114.3, 40.64)          # the title block's corner, kept clear (width, height from the bottom right)
GAP_X, GAP_Y = 10.16, 7.62
PITCH = 2.54
SHIFT_HEADS = ("at", "start", "end", "xy", "center", "mid")


def _f(v):
    return float(v)


def _num(v):
    return round(float(v), 4)


def _snap(v, g=PITCH):
    return round(round(v / g) * g, 4)


def _shift(node, dx, dy):
    """Move a node and everything in it by (dx, dy): every at / start / end / xy / center / mid in it."""
    if not isinstance(node, list) or not node:
        return
    if node[0] in SHIFT_HEADS and len(node) >= 3:
        try:
            node[1], node[2] = _num(_f(node[1]) + dx), _num(_f(node[2]) + dy)
        except (TypeError, ValueError):
            pass
        return
    for sub in node[1:]:
        _shift(sub, dx, dy)


def _find(node, head):
    for sub in node[1:] if isinstance(node, list) else []:
        if isinstance(sub, list) and sub and sub[0] == head:
            return sub
    return None


def _findall(node, head):
    return [s for s in (node[1:] if isinstance(node, list) else []) if isinstance(s, list) and s and s[0] == head]


def _at(node):
    a = _find(node, "at")
    return (_f(a[1]), _f(a[2]), _f(a[3]) if len(a) > 3 else 0.0) if a else (0.0, 0.0, 0.0)


def _font(node):
    e = _find(node, "effects")
    fnt = _find(e, "font") if e else None
    s = _find(fnt, "size") if fnt else None
    try:
        return _f(s[1]) if s else 1.27
    except (TypeError, ValueError):
        return 1.27


def _justify(node):
    e = _find(node, "effects")
    j = _find(e, "justify") if e else None
    return [str(v) for v in j[1:]] if j else []


def _text_box(node):
    """A free text's page area, from its lines, size and justification."""
    x, y, r = _at(node)
    size = _font(node)
    lines = str(node[1]).split("\n")
    w = max(font.ink_width(l, size) for l in lines) if lines else 0.0
    h = len(lines) * size * 1.65
    j = _justify(node)
    if int(round(r)) % 180:
        w, h = h, w
    x0 = x if "left" in j else x - w if "right" in j else x - w / 2
    y0 = y if "top" in j else y - h if "bottom" in j else y - h / 2
    return (x0, y0, x0 + w, y0 + h)


def _label_box(node):
    kind = node[0]
    x, y, r = _at(node)
    size = _font(node)
    w = font.ink_width(str(node[1]), size) + (0.5 if kind == "label" else 3.0)
    r = int(round(r)) % 360
    if kind == "label":
        return {0: (x, y - 1.8, x + w, y), 180: (x - w, y - 1.8, x, y), 90: (x - 1.8, y - w, x, y), 270: (x - 1.8, y, x, y + w)}[r]
    h = 1.15
    return {0: (x, y - h, x + w, y + h), 180: (x - w, y - h, x, y + h), 90: (x - h, y - w, x + h, y), 270: (x - h, y, x + h, y + w)}[r]


def _union(boxes):
    boxes = [b for b in boxes if b]
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def _pts(node):
    p = _find(node, "pts")
    return [(_f(xy[1]), _f(xy[2])) for xy in _findall(p, "xy")] if p else []


def _key(pt):
    return (round(pt[0], 2), round(pt[1], 2))


def arrange(text):
    """The root sheet's file text, rearranged (see the module doc), and a short report."""
    tree = sexp.parse(text)
    top = tree[1:]
    sheets = [n for n in top if isinstance(n, list) and n and n[0] == "sheet"]
    if not sheets:
        return text, {"arranged": False, "why": "no sheet symbols"}
    links = [n for n in top if isinstance(n, list) and n and n[0] in ("wire", "bus")]
    labels = [n for n in top if isinstance(n, list) and n and n[0] in ("label", "global_label", "hierarchical_label")]
    junctions = [n for n in top if isinstance(n, list) and n and n[0] in ("junction", "no_connect")]
    taken = set(map(id, sheets))

    # each sheet pin: its stub (a wire or bus from the pin), the label at the stub's end
    pin_at = {}
    for s in sheets:
        for p in _findall(s, "pin"):
            x, y, _ = _at(p)
            pin_at[_key((x, y))] = (s, p)
    units = []
    split = 0
    for s in sheets:
        units.append({"kind": "sheet", "node": s, "items": [s]})
    by_sheet = {id(u["node"]): u for u in units}
    for ln in list(links):
        pts = _pts(ln)
        if len(pts) != 2:
            continue
        a, b = _key(pts[0]), _key(pts[1])
        if a in pin_at and b in pin_at and pin_at[a][0] is not pin_at[b][0]:
            # a link between two sheets' pins: a labelled stub at each end instead, so each sheet moves on its own
            tree.remove(ln)
            links.remove(ln)
            for k in (a, b):
                s, p = pin_at[k]
                x0 = _f(_find(s, "at")[1])
                left = abs(k[0] - x0) < 0.01
                end = (k[0] - PITCH, k[1]) if left else (k[0] + PITCH, k[1])
                stub = [ln[0], ["pts", ["xy", k[0], k[1]], ["xy", end[0], end[1]]], ["stroke", ["width", 0], ["type", "default"]],
                        ["uuid", sexp.Q(_uuid_str())]]
                lab = ["label", sexp.Q(str(p[1])), ["at", end[0], end[1], 180 if left else 0],
                       ["effects", ["font", ["size", 1.27, 1.27]], ["justify", *(("right", "bottom") if left else ("left", "bottom"))]],
                       ["uuid", sexp.Q(_uuid_str())]]
                tree.extend([stub, lab])
                links.append(stub)
                labels.append(lab)
            split += 1
    top = tree[1:]                                       # the page as it is now (links split above)
    for ln in links:
        pts = _pts(ln)
        if len(pts) != 2:
            continue
        for i, k in enumerate((_key(pts[0]), _key(pts[1]))):
            if k in pin_at:
                u = by_sheet[id(pin_at[k][0])]
                other = _key(pts[1 - i])
                u["items"].append(ln)
                u.setdefault("stubs", []).append((pin_at[k][1], ln, other))
                taken.add(id(ln))
                for lb in labels:
                    if _key(_at(lb)[:2]) == other and id(lb) not in taken:
                        u["items"].append(lb)
                        taken.add(id(lb))
                for j in junctions:
                    if _key(_at(j)[:2]) in (k, other) and id(j) not in taken:
                        u["items"].append(j)
                        taken.add(id(j))

    # blocks: a rectangle and everything whose anchor lies inside it
    for r in [n for n in top if isinstance(n, list) and n and n[0] == "rectangle"]:
        st, en = _find(r, "start"), _find(r, "end")
        x0, y0, x1, y1 = min(_f(st[1]), _f(en[1])), min(_f(st[2]), _f(en[2])), max(_f(st[1]), _f(en[1])), max(_f(st[2]), _f(en[2]))
        items = [r]
        taken.add(id(r))
        for n in top:
            if not isinstance(n, list) or not n or id(n) in taken or n[0] in ("lib_symbols", "sheet_instances", "symbol_instances",
                                                                              "title_block", "paper", "bus_alias", "rectangle"):
                continue
            if n[0] in ("wire", "bus"):
                pts = _pts(n)
                inside = pts and all(x0 - 0.01 <= x <= x1 + 0.01 and y0 - 0.01 <= y <= y1 + 0.01 for x, y in pts)
            elif _find(n, "at"):
                x, y, _ = _at(n)
                inside = x0 - 0.01 <= x <= x1 + 0.01 and y0 - 0.01 <= y <= y1 + 0.01
            else:
                inside = False
            if inside:
                items.append(n)
                taken.add(id(n))
        units.append({"kind": "block", "items": items, "box": (x0, y0, x1, y1)})
    for n in top:
        if isinstance(n, list) and n and n[0] in ("text", "text_box") and id(n) not in taken:
            units.append({"kind": "text", "items": [n]})
            taken.add(id(n))
    rest = [n for n in top if isinstance(n, list) and n and id(n) not in taken and n[0] in ("symbol", "wire", "bus", "label",
                                                                                            "global_label", "hierarchical_label",
                                                                                            "junction", "no_connect")]
    if rest:
        units.append({"kind": "rest", "items": rest})

    # sheet symbols as big as their pins need; the pins evenly spaced down each side, stubs and labels with them
    for u in units:
        if u["kind"] != "sheet":
            continue
        s = u["node"]
        at, size = _find(s, "at"), _find(s, "size")
        x0, y0, w, h = _f(at[1]), _f(at[2]), _f(size[1]), _f(size[2])
        pins = _findall(s, "pin")
        left = sorted([p for p in pins if abs(_at(p)[0] - x0) < 0.01], key=lambda p: _at(p)[1])
        right = sorted([p for p in pins if abs(_at(p)[0] - (x0 + w)) < 0.01], key=lambda p: _at(p)[1])
        name = next((str(q[2]) for q in _findall(s, "property") if str(q[1]) in ("Sheetname", "Sheet name")), "")
        names_w = max([font.ink_width(str(p[1])) for p in left] or [0]) + max([font.ink_width(str(p[1])) for p in right] or [0])
        nw = _snap(max(25.4, names_w + 10.16, font.ink_width(name) + 2.54), PITCH)
        nh = _snap(max(12.7, (max(len(left), len(right)) + 1) * PITCH + PITCH), PITCH)
        stubs = {id(p): [] for p in pins}
        for p, ln, _ in u.get("stubs", []):
            stubs[id(p)].append(ln)
        attached = {}                                   # pin -> the items that move with it
        for p in pins:
            px, py, _ = _at(p)
            group = []
            for ln in stubs[id(p)]:
                group.append(ln)
                o = [pt for pt in _pts(ln) if _key(pt) != _key((px, py))]
                for it in u["items"]:
                    if it is not ln and it[0] in ("label", "global_label", "hierarchical_label", "junction") and o and \
                            _key(_at(it)[:2]) == _key(o[0]):
                        group.append(it)
            attached[id(p)] = group
        for side, lst in ((0, left), (1, right)):
            for i, p in enumerate(lst):
                px, py, _ = _at(p)
                nx = x0 if side == 0 else x0 + nw
                ny = y0 + PITCH * (i + 1)
                dx, dy = nx - px, ny - py
                if dx or dy:
                    _shift(p, dx, dy)
                    for it in attached[id(p)]:
                        _shift(it, dx, dy)
        size[1], size[2] = nw, nh
        for q in _findall(s, "property"):                # the name just above the box, the file just below it
            qa = _find(q, "at")
            if qa and str(q[1]) in ("Sheetname", "Sheet name"):
                qa[1], qa[2] = _num(x0), _num(y0 - 0.7112)
            elif qa and str(q[1]) in ("Sheetfile", "Sheet file"):
                qa[1], qa[2] = _num(x0), _num(y0 + nh + 0.7112)

    # each unit's page area
    for u in units:
        boxes = []
        for it in u["items"]:
            k = it[0]
            if k == "sheet":
                at, size = _find(it, "at"), _find(it, "size")
                x0, y0, w, h = _f(at[1]), _f(at[2]), _f(size[1]), _f(size[2])
                fw = max([font.ink_width(str(q[2])) for q in _findall(it, "property")] or [0])
                boxes.append((x0, y0 - 2.8, x0 + max(w, fw), y0 + h + 2.8))
            elif k in ("wire", "bus"):
                pts = _pts(it)
                if pts:
                    boxes.append((min(p[0] for p in pts), min(p[1] for p in pts) - 0.2, max(p[0] for p in pts), max(p[1] for p in pts) + 0.2))
            elif k in ("label", "global_label", "hierarchical_label"):
                boxes.append(_label_box(it))
            elif k == "text":
                boxes.append(_text_box(it))
            elif k == "rectangle":
                boxes.append(u.get("box"))
            elif k == "text_box":
                x, y, _ = _at(it)
                sz = _find(it, "size")
                boxes.append((x, y, x + _f(sz[1]), y + _f(sz[2])) if sz else None)
            elif k == "symbol":
                x, y, _ = _at(it)
                boxes.append((x - 2.54, y - 2.54, x + 2.54, y + 2.54))
                for q in _findall(it, "property"):
                    qa = _find(q, "at")
                    hidden = any(isinstance(e, list) and e and e[0] == "hide" and (len(e) < 2 or str(e[1]) == "yes")
                                 for e in (_find(q, "effects") or [])[1:]) or "hide" in [str(e) for e in (_find(q, "effects") or [])]
                    if qa and not hidden and str(q[2]):
                        tw = font.ink_width(str(q[2]))
                        boxes.append((_f(qa[1]) - tw / 2, _f(qa[2]) - 1.0, _f(qa[1]) + tw / 2, _f(qa[2]) + 1.0))
        u["area"] = _union(boxes)

    # order: the sheets in page order, then the blocks, then the texts (as they were, top to bottom, left to right)
    def page_no(s):
        m = re.search(r'\(page "?(\d+)"?\)', sexp.dumps(_find(s, "instances") or []))
        return int(m.group(1)) if m else 999
    sheet_units = sorted([u for u in units if u["kind"] == "sheet"], key=lambda u: page_no(u["node"]))
    others = sorted([u for u in units if u["kind"] != "sheet"], key=lambda u: (u["kind"] == "rest", round(u["area"][1] / 25.4), u["area"][0]))

    def pack(W, H):
        """Each unit's new top-left, row by row, or None if they do not fit on this paper."""
        tb = (W - MARGIN + 5.08 - TITLE[0], H - MARGIN + 5.08 - TITLE[1], W, H)
        spots = []
        for group in (sheet_units, others):
            x, y, row_h = MARGIN, (spots and max(sp[1] + u_["area"][3] - u_["area"][1] for sp, u_ in spots) + GAP_Y + 2.54) or MARGIN, 0
            for u in group:
                w, h = u["area"][2] - u["area"][0], u["area"][3] - u["area"][1]
                for _ in range(2):
                    over_title = x < tb[2] and x + w > tb[0] and y < tb[3] and y + h > tb[1]
                    if (x + w > W - MARGIN or over_title) and x > MARGIN:
                        x, y, row_h = MARGIN, y + row_h + GAP_Y, 0
                        continue
                    break
                if x + w > W - MARGIN or y + h > H - MARGIN or (x < tb[2] and x + w > tb[0] and y < tb[3] and y + h > tb[1]):
                    return None
                spots.append(((x, y), u))
                x += w + GAP_X
                row_h = max(row_h, h)
        return spots

    for paper, W, H in PAPERS:
        spots = pack(W, H)
        if spots:
            break
    else:
        return sexp.dumps(tree) + "\n", {"arranged": False, "why": "does not fit on A1", "split": split}
    for (x, y), u in spots:
        dx, dy = _snap(x - u["area"][0]), _snap(y - u["area"][1])
        for it in u["items"]:
            _shift(it, dx, dy)
    pn = _find(tree, "paper")
    if pn:
        pn[1] = sexp.Q(paper)
    return sexp.dumps(tree) + "\n", {"arranged": True, "paper": paper, "units": len(units), "split": split}


def _uuid_str():
    import uuid
    return str(uuid.uuid4())
