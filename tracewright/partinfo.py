"""One part, everything known about it: what it is (value, part number, maker, package, the data sheet,
LCSC's description and parameters, photos), where it is (sheet, board position and side, its
footprint's pads and outline), what it connects (each pin's name and net, with the net's kind), what
it costs (JLC / LCSC stock, price, Basic or Extended), and what the checks say about it."""
import os, re, json, hashlib, urllib.request


def _netlist(p):
    from tw.checks.context import Context
    try:
        return Context(p.tw).netlist if p.tw.has_sch() else None
    except Exception:
        return None


def _findings(p, ref):
    f = os.path.join(p.tw.build, "checks.json")
    try:
        d = json.load(open(f))
    except (OSError, ValueError):
        return []
    results = d.get("results") or d.get("checks") or []
    if isinstance(results, dict):
        results = list(results.values())
    word = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(ref)}(?![0-9A-Za-z_])")
    out = []
    for r in results:
        for x in (r.get("findings") or []) if isinstance(r, dict) else []:
            w = x.get("where") or {}
            if w.get("ref") == ref or ref in (w.get("refs") or []) or word.search(x.get("message") or ""):
                out.append({"check": r.get("id"), "title": r.get("title"), "severity": x.get("severity"),
                            "message": x.get("message"), "hint": x.get("hint")})
    order = {"error": 0, "warning": 1, "info": 2}
    return sorted(out, key=lambda x: order.get(x["severity"], 3))[:30]


def _placed_why(p, ref):
    """Why the part sits where it does, from the placement plan (tw/placeplan.py), or ""."""
    try:
        from tw import placeplan
        return ((placeplan.load(p.tw).get("parts") or {}).get(ref) or {}).get("why", "")
    except Exception:
        return ""


def part_info(p, board, ref, fetch=False):
    """Everything about `ref` in project p (projects.Project) with its board (tw.board.Board or None).
    fetch: look the part up at LCSC when the cache has nothing (the user opened it; one request)."""
    from tw import nettypes
    from . import bom
    nl = _netlist(p)
    part = (nl.parts.get(ref) if nl else None) or {}
    fp = board.footprints.get(ref) if board is not None else None
    if not part and fp is None:
        return None
    fields = dict(part.get("fields") or {})
    if fp is not None:
        for k, v in (fp.fields or {}).items():
            fields.setdefault(k, v)
    lcsc = next((fields.get(k) for k in ("LCSC", "LCSC Part", "LCSC Part #", "JLCPCB Part", "JLC") if fields.get(k)), "")
    info = {"ref": ref, "value": part.get("value") or (fp.value if fp is not None else ""),
            "footprint": part.get("footprint") or (fp.lib_id if fp is not None else ""),
            "mpn": fields.get("MPN") or fields.get("Mfr Part") or fields.get("Part Number") or "",
            "manufacturer": fields.get("Manufacturer") or fields.get("MFR") or "",
            "lcsc": lcsc, "datasheet": part.get("datasheet") or fields.get("Datasheet") or "",
            "description": part.get("description") or fields.get("Description") or "",
            "sheet": part.get("sheet") or (fp.sheetname if fp is not None else ""),
            "dnp": bool(part.get("dnp") or (fp is not None and fp.dnp)), "in_schematic": bool(part),
            "placed_why": _placed_why(p, ref),
            "fields": {k: v for k, v in fields.items() if v and k not in ("Reference", "Value", "Footprint", "Datasheet", "Description")
                       and not re.match(r"^(ki_|KiLib|Sim\.|Sheet)", k)}}
    try:
        from tw import netmodel
        m = netmodel.for_project(p.tw)
        types = {n: {"kind": r["kind"], "tag": netmodel.tag(r)} for n, r in m.records.items()}
    except Exception:
        types = nettypes.from_netlist(nl, p.cfg) if nl else (nettypes.from_board(board, p.cfg) if board is not None else {})
    pins = []
    if nl:
        for pin in nl.pins_of(ref):
            full = nl.pin.get((ref, pin)) or ""
            t = types.get(full) or {}
            if full.startswith("unconnected-"):
                full, t = "", {"kind": "unconnected"}
            pins.append({"pin": pin, "name": nl.pin_name(ref, pin), "type": nl.pin_type(ref, pin), "net": nettypes.short(full),
                         "full": full, "kind": t.get("kind", "signal"), "tag": t.get("tag", "")})
    elif fp is not None:
        for pd in fp.pads:
            t = types.get(pd.net) or {}
            pins.append({"pin": pd.num, "name": pd.pinfunction or "", "type": pd.pintype or "", "net": nettypes.short(pd.net),
                         "full": pd.net or "", "kind": t.get("kind", "signal") if pd.net else "unconnected", "tag": t.get("tag", "")})
    info["pins"] = pins
    if fp is not None:
        j = fp.to_json()
        info["board"] = {"x": j["x"], "y": j["y"], "rot": j["a"], "side": j["side"], "bbox": j["bbox"], "pads": j["pads"],
                         "g": j["g"], "cy": j["cy"]}
    src = bom.sourcing_index(p.root).get(lcsc) if lcsc else None
    detail = None
    if lcsc:
        from tw.jlc import Parts, LookupFailed
        parts = Parts(p.root)
        detail = parts.cache.get("lcsc", lcsc, False, max_age_h=None)
        if fetch and (not detail or "images" not in detail):
            try:
                detail = parts.detail(lcsc, refresh=True)
                src = bom.sourcing_index(p.root).get(lcsc) or src
            except LookupFailed:
                pass
    s = src or {}
    info["sourcing"] = {"jlc_stock": s.get("jlc_stock"), "lcsc_stock": s.get("lcsc_stock"), "lib": s.get("lib"),
                        "price": s.get("jlc_price") if s.get("jlc_price") is not None else s.get("lcsc_price"),
                        "asked": s.get("lcsc_utc") or s.get("jlc_utc"), "discontinued": s.get("discontinued")} if src else None
    if detail:
        info["lcsc_detail"] = {"name": detail.get("name"), "category": detail.get("category"), "package": detail.get("package"),
                               "description": detail.get("description"), "params": detail.get("params") or {},
                               "datasheet": detail.get("datasheet"), "photos": len(detail.get("images") or [])}
        if not info["datasheet"] or info["datasheet"] == "~":
            info["datasheet"] = detail.get("datasheet") or ""
        if not info["description"]:
            info["description"] = detail.get("name") or detail.get("description") or ""
    # the project's data sheet library: the saved PDF, and the pin table read from it against the symbol's pins
    from tw import datasheets
    info["datasheet_saved"] = datasheets.pdf_for(p.tw, info["mpn"], lcsc, info["value"])
    names, source, pfile = datasheets.pins_for(p.tw, lcsc=lcsc, mpn=info["mpn"], value=info["value"])
    if names:
        from tw.checks.integrity import compare_pin
        sym = {x["pin"]: x["name"] for x in pins}
        rows = []
        for n in sorted(set(names) | set(sym), key=lambda k: (len(k), k)):
            rows.append({"pin": n, "sheet": names.get(n, ""), "symbol": sym.get(n, ""),
                         "match": compare_pin(sym.get(n, ""), names[n]) if n in names and n in sym else "missing"})
        info["pin_table"] = {"source": source, "file": pfile, "rows": rows,
                             "differ": sum(1 for r in rows if r["match"] in ("mismatch", "critical"))}
    info["findings"] = _findings(p, ref)
    return info


def photo(p, lcsc, i=0):
    """A local copy of LCSC's product photo `i` for the part (fetched once, kept in sourcing/cache)."""
    from tw.jlc import Parts
    detail = Parts(p.root).cache.get("lcsc", lcsc, False, max_age_h=None) or {}
    urls = detail.get("images") or []
    if not (0 <= i < len(urls)):
        return None
    url = urls[i]
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith("https://") or "lcsc.com/" not in url:
        return None
    d = os.path.join(p.root, "sourcing", "cache", "photos")
    os.makedirs(d, exist_ok=True)
    f = os.path.join(d, f"{re.sub(r'[^A-Za-z0-9]', '', lcsc)}_{i}_{hashlib.sha1(url.encode()).hexdigest()[:8]}.jpg")
    if not os.path.exists(f):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Tracewright)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read(6_000_000)
        if not data[:3] == b"\xff\xd8\xff" and not data[:8] == b"\x89PNG\r\n\x1a\n":
            return None
        with open(f + ".tmp", "wb") as fh:
            fh.write(data)
        os.replace(f + ".tmp", f)
    return f
