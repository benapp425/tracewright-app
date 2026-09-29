"""Stock watch: every placed part's stock against what the planned order needs, a log of how it moves, and
in-stock alternates for the parts that run short.

The planned order is the design limits' quantity (or 5 boards); a part needs its count per board times the
boards, plus spares (5 %, at least 2) for the machine's losses. Stock is JLC's when JLC assembles the board,
LCSC's when you build it yourself. A part is "out" below what it needs, "low" below three times that (the
next order may not get it), and "gone" when LCSC lists it as discontinued.

sourcing/stock_log.json keeps each part's readings ({code: [[utc, jlc, lcsc, price]]}) and the last states,
so a watch run can say which parts have just run short."""
import datetime, json, math, os

LOG = os.path.join("sourcing", "stock_log.json")


def boards_for(tw):
    from tw import constraints
    q = constraints.get(tw.cfg).get("quantity")
    try:
        return max(1, int(q))
    except (TypeError, ValueError):
        return 5


def need(per_board, boards):
    n = per_board * boards
    return n + max(2, math.ceil(n * 0.05))


def _log(tw):
    try:
        with open(os.path.join(tw.root, LOG)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"readings": {}, "states": {}}


def _save_log(tw, d):
    f = os.path.join(tw.root, LOG)
    os.makedirs(os.path.dirname(f), exist_ok=True)
    with open(f + ".tmp", "w") as fh:
        json.dump(d, fh, indent=0)
    os.replace(f + ".tmp", f)


def status(tw, board=None, boards=None, data=None):
    """{boards, mode, rows: [{refs, lcsc, mpn, value, qty, need, stock, jlc_stock, lcsc_stock, state, asked}], out, low,
    gone, unknown, oldest}. data: the BOM (bom.bom_data) when the caller has it."""
    from . import bom as bomlib, order
    boards = boards or boards_for(tw)
    mode = order.mode(tw)
    data = data if data is not None else bomlib.bom_data(tw, board)
    rows, oldest = [], None
    for r in data.get("rows", []) if not data.get("empty") else []:
        if not r.get("assembled") or not r.get("lcsc"):
            continue
        s = r.get("source") or {}
        stock = s.get("jlc_stock") if mode == "jlc" else s.get("lcsc_stock")
        if stock is None:
            stock = s.get("lcsc_stock") if mode == "jlc" else s.get("jlc_stock")
        n = need(r["qty"], boards)
        state = "gone" if s.get("discontinued") else "unknown" if stock is None else "out" if stock < n else "low" if stock < 3 * n else "ok"
        asked = s.get("jlc_utc") or s.get("lcsc_utc")
        if asked and (oldest is None or asked < oldest):
            oldest = asked
        rows.append({"refs": r["refs"], "lcsc": r["lcsc"], "mpn": r.get("mpn") or s.get("mpn") or "", "value": r["value"],
                     "footprint": r["footprint"], "qty": r["qty"], "need": n, "stock": stock, "jlc_stock": s.get("jlc_stock"),
                     "lcsc_stock": s.get("lcsc_stock"), "state": state, "asked": asked, "lib": s.get("lib")})
    order_of = {"gone": 0, "out": 1, "low": 2, "unknown": 3, "ok": 4}
    rows.sort(key=lambda x: (order_of[x["state"]], x["refs"][0]))
    return {"boards": boards, "mode": mode, "rows": rows, "oldest": oldest,
            **{k: sum(1 for x in rows if x["state"] == k) for k in ("gone", "out", "low", "unknown")}}


def stale_codes(tw, hours=24.0):
    """The placed parts whose stock was last asked more than `hours` ago (or never)."""
    st = status(tw)
    cut = (datetime.datetime.utcnow() - datetime.timedelta(hours=hours)).isoformat()
    return [x["lcsc"] for x in st["rows"] if not x["asked"] or x["asked"] < cut]


def record(tw, st=None):
    """Log the readings and the states; returns the parts that have just run short: [{lcsc, refs, state, was, stock, need}]."""
    st = st or status(tw)
    d = _log(tw)
    now = datetime.datetime.utcnow().isoformat(timespec="seconds")
    news = []
    for x in st["rows"]:
        code = x["lcsc"]
        rd = d["readings"].setdefault(code, [])
        last = rd[-1] if rd else None
        reading = [x["jlc_stock"], x["lcsc_stock"]]
        if not last or last[1:3] != reading:
            rd.append([now, *reading])
            del rd[:-60]
        was = d["states"].get(code)
        if x["state"] in ("out", "low", "gone") and was in (None, "ok", "unknown") and x["state"] != was:
            news.append({"lcsc": code, "refs": x["refs"], "state": x["state"], "was": was, "stock": x["stock"], "need": x["need"]})
        elif x["state"] == "out" and was == "low":
            news.append({"lcsc": code, "refs": x["refs"], "state": "out", "was": was, "stock": x["stock"], "need": x["need"]})
        if x["state"] != "unknown":
            d["states"][code] = x["state"]
    _save_log(tw, d)
    return news


def history(tw, code):
    return _log(tw)["readings"].get(code, [])


def alternates(tw, row, boards=None, budget=40.0):
    """In-stock parts that can stand in for a BOM row: for a resistor or capacitor the exact equivalents (the same
    value, package, tolerance, power, voltage and dielectric); for anything else the same part number from other
    makers or grades in the same package, to check against the data sheet before swapping."""
    import time
    from . import savings
    from tw.jlc import Parts
    boards = boards or boards_for(tw)
    n = need(row["qty"], boards)
    parts = Parts(tw.root, deadline=time.time() + budget)
    src = {"describe": "", "name": ""}
    req = savings.requirements({"refs": row["refs"], "value": row["value"], "footprint": row["footprint"]}, src)
    out = []
    if req:
        kind, value, pkg, tol, w, v, diel = req
        found = parts.search(savings._query(kind, value, pkg), 30).get("items") or []
        for it in savings.equivalents(found, kind, value, pkg, n, tol, w, v, diel):
            if it.get("lcsc") != row["lcsc"]:
                out.append({**_item(it), "why": "the same value, package and rating", "check": ""})
    elif row.get("mpn"):
        import re
        core = re.sub(r"[-/](TR|T|R|REEL|CT|PBF|G4|NOPB)$", "", row["mpn"].upper())
        found = parts.search(core, 30).get("items") or []
        pkg = (row.get("footprint") or "").split(":")[-1].split("_")[0].upper()
        for it in found:
            if it.get("lcsc") == row["lcsc"] or (it.get("jlc_stock") or 0) < n:
                continue
            if core[:6] not in (it.get("mpn") or "").upper():
                continue
            if pkg and pkg[:5] not in ((it.get("package") or "") + (it.get("describe") or "")).upper():
                continue
            out.append({**_item(it), "why": "the same part number in the same package", "check": "compare its data sheet (grade, pinout, temperature) before swapping"})
    out.sort(key=lambda x: (x["lib"] != "Basic", x.get("price") or 99, -(x.get("stock") or 0)))
    return {"need": n, "boards": boards, "items": out[:8]}


def _item(it):
    lib = {"base": "Basic", "expand": "Extended"}.get(it.get("lib"), it.get("lib"))
    return {"lcsc": it.get("lcsc"), "mpn": it.get("mpn"), "brand": it.get("brand"), "stock": it.get("jlc_stock"),
            "price": it.get("price_1"), "lib": lib, "describe": (it.get("describe") or "")[:160], "package": it.get("package")}
