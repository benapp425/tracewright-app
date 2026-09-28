"""The bill of materials for the BOM tab: every part from the netlist, grouped the way JLC orders them,
with where each part sits on the board, what the checks say about it, and the JLC / LCSC stock,
library type and price from the dated parts cache (sourcing/cache, filled by ./tw parts and by the
Look up button). Nothing here guesses a price or a stock level: a part with no cached answer says so.
"""
import os, re, csv, io, json, glob, time, collections

from tw.checks.context import Context
from tw.checks.bom import lcsc_of
from tw.outputs import natural


def sourcing_index(root):
    """{LCSC code: what the cache knows}: LCSC's own detail (stock, price, name) and JLC's assembly
    search (JLC stock, Basic / Extended, JLC price), each with the time it was asked."""
    out = collections.defaultdict(dict)
    d = os.path.join(root, "sourcing", "cache")
    for f in glob.glob(os.path.join(d, "lcsc_*.json")):
        try:
            with open(f) as fh:
                r = json.load(fh)
        except (OSError, ValueError):
            continue
        code = r.get("lcsc") or r.get("_query")
        if not code:
            continue
        e = out[code]
        if r.get("_epoch", 0) >= e.get("_lcsc_epoch", 0):
            e.update({"lcsc_stock": r.get("lcsc_stock"), "lcsc_price": r.get("price_1"), "name": r.get("name") or r.get("description"),
                      "mpn": r.get("mpn"), "brand": r.get("brand"), "package": r.get("package"), "datasheet": r.get("datasheet"),
                      "discontinued": r.get("discontinued"), "lcsc_utc": r.get("_utc"), "_lcsc_epoch": r.get("_epoch", 0)})
    for f in glob.glob(os.path.join(d, "jlc_*.json")):
        try:
            with open(f) as fh:
                r = json.load(fh)
        except (OSError, ValueError):
            continue
        for it in r.get("items") or []:
            code = it.get("lcsc")
            if not code:
                continue
            e = out[code]
            if r.get("_epoch", 0) >= e.get("_jlc_epoch", 0):
                e.update({"jlc_stock": it.get("jlc_stock"), "lib": it.get("lib"), "jlc_price": it.get("price_1"),
                          "preferred": it.get("preferred"), "jlc_utc": r.get("_utc"), "_jlc_epoch": r.get("_epoch", 0)})
                e.setdefault("mpn", it.get("mpn"))
                e.setdefault("package", it.get("package"))
                e.setdefault("datasheet", it.get("datasheet"))
    for e in out.values():
        e.pop("_lcsc_epoch", None)
        e.pop("_jlc_epoch", None)
    return dict(out)


def findings_by_ref(tw):
    f = os.path.join(tw.build, "checks.json")
    out = collections.defaultdict(list)
    try:
        with open(f) as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return {}, None
    for c in d.get("checks", []):
        for x in c.get("findings", []):
            ref = (x.get("where") or {}).get("ref")
            if ref and x.get("severity") in ("error", "warning"):
                out[ref].append({"check": c["id"], "severity": x["severity"], "message": x["message"]})
    return dict(out), d.get("generated")


def bom_data(tw, board=None):
    """The BOM as the BOM tab shows it (see the module note)."""
    ctx = Context(tw)
    if not tw.has_sch():
        return {"empty": True}
    nl = ctx.netlist
    skip = set(tw.setting("fab.not_assembled", []) or [])
    fps = board.footprints if board is not None else {}
    src = sourcing_index(tw.root)
    finds, checked = findings_by_ref(tw)
    parts = []
    for ref, p in nl.parts.items():
        if ref.startswith("#"):
            continue
        fp = fps.get(ref)
        f = p.get("fields", {})
        parts.append({
            "ref": ref, "value": p["value"], "footprint": p["footprint"].split(":")[-1], "lib": p["footprint"],
            "mpn": f.get("MPN") or f.get("Manufacturer Part Number") or f.get("MFR Part") or "",
            "mfr": f.get("Manufacturer") or f.get("MFR") or "", "lcsc": lcsc_of(f, ctx),
            "description": p.get("description") or f.get("Description", ""), "datasheet": p.get("datasheet", ""),
            "dnp": bool(p["dnp"]), "in_bom": bool(p["in_bom"]), "not_assembled": ref in skip,
            "on_board": fp is not None, "side": fp.side if fp else None, "sheet": p.get("sheet", ""),
            "fields": {k: v for k, v in f.items() if v and k not in ("Footprint", "Datasheet", "Description")},
        })
    groups = {}                  # one line per orderable part: its LCSC code, else its MPN, else value + footprint
    for p in parts:
        part = ("lcsc", p["lcsc"]) if p["lcsc"] else ("mpn", p["mpn"], p["footprint"]) if p["mpn"] else \
            ("value", p["value"], p["footprint"])
        k = part + (p["dnp"] or not p["in_bom"], p["not_assembled"])
        groups.setdefault(k, []).append(p)
    rows = []
    for k, ps in groups.items():
        refs = sorted((p["ref"] for p in ps), key=natural)
        ps.sort(key=lambda p: natural(p["ref"]))
        first = ps[0]
        values = list(dict.fromkeys(p["value"] for p in ps))
        info = src.get(first["lcsc"]) if first["lcsc"] else None
        issues = [dict(x, ref=r) for r in refs for x in finds.get(r, [])]
        excluded = first["dnp"] or not first["in_bom"]
        rows.append({
            "refs": refs, "qty": len(refs), "value": first["value"], "values": values, "footprint": first["footprint"], "lib": first["lib"],
            "mpn": first["mpn"], "mfr": first["mfr"], "lcsc": first["lcsc"], "description": first["description"],
            "datasheet": first["datasheet"], "dnp": excluded, "not_assembled": first["not_assembled"],
            "assembled": not excluded and not first["not_assembled"],
            "sides": sorted({p["side"] for p in ps if p["side"]}), "missing": sorted(p["ref"] for p in ps if not p["on_board"]),
            "source": info, "issues": issues,
        })
    rows.sort(key=lambda r: natural(r["refs"][0]))
    placed = [r for r in rows if r["assembled"]]
    ext = [r for r in placed if (r["source"] or {}).get("lib") == "Extended"]
    cost, priced = 0.0, 0
    for r in placed:
        s = r["source"] or {}
        pr = s.get("jlc_price") if s.get("jlc_price") is not None else s.get("lcsc_price")
        if pr is not None:
            try:
                cost += float(pr) * r["qty"]
                priced += 1
            except (TypeError, ValueError):
                pass
    codes = sorted({r["lcsc"] for r in placed if r["lcsc"]})
    return {
        "rows": rows, "parts": parts, "checked": checked,
        "totals": {"lines": len(rows), "parts": sum(r["qty"] for r in rows), "assembled": sum(r["qty"] for r in placed),
                   "assembled_lines": len(placed), "codes": len(codes), "extended": len(ext),
                   "basic": sum(1 for r in placed if (r["source"] or {}).get("lib") == "Basic"),
                   "no_lcsc": sum(r["qty"] for r in placed if not r["lcsc"]), "dnp": sum(r["qty"] for r in rows if r["dnp"]),
                   "cost": round(cost, 3) if priced else None, "priced": priced,
                   "known": sum(1 for c in codes if c in src),
                   "lib_known": sum(1 for c in codes if (src.get(c) or {}).get("lib")),
                   "missing_board": sum(len(r["missing"]) for r in rows)},
        "fab": tw.setting("fab.house", "jlcpcb"),
    }


def lookup(tw, codes, progress=None, stop=None, budget=150.0):
    """Ask LCSC (stock, price, name) and JLC (assembly stock, Basic / Extended) about each code, into
    the cache. Returns {code: 'ok' | reason}."""
    from tw.jlc import Parts, LookupFailed
    parts = Parts(tw.root, deadline=time.time() + budget, stop=stop)
    out = {}
    for i, code in enumerate(codes):
        if stop is not None and stop.is_set():
            break
        why = []
        try:
            parts.detail(code)
        except (LookupFailed, Exception) as e:
            why.append(f"LCSC: {e}")
        try:
            parts.search(code, 10)
        except (LookupFailed, Exception) as e:
            why.append(f"JLC: {e}")
        out[code] = "; ".join(why) or "ok"
        if progress:
            progress(i + 1, len(codes), code, out[code])
    return out


def csv_text(data, kind="jlc"):
    """The BOM as CSV: 'jlc' (JLC's upload columns, assembled parts only) or 'full' (every line, every field)."""
    buf = io.StringIO()
    if kind == "jlc":
        w = csv.writer(buf)
        w.writerow(["Comment", "Designator", "Footprint", "LCSC Part #"])
        for r in data["rows"]:
            if r["assembled"]:
                w.writerow([r["value"] if len(r["values"]) == 1 else (r["mpn"] or r["value"]), ",".join(r["refs"]), r["footprint"], r["lcsc"]])
    else:
        w = csv.writer(buf)
        w.writerow(["Qty", "Designators", "Value", "Footprint", "MPN", "Manufacturer", "LCSC", "JLC library", "JLC stock",
                    "LCSC stock", "Unit price (USD)", "Assembled", "Description"])
        for r in data["rows"]:
            s = r["source"] or {}
            price = s.get("jlc_price") if s.get("jlc_price") is not None else s.get("lcsc_price")
            w.writerow([r["qty"], ",".join(r["refs"]), " / ".join(r["values"]), r["footprint"], r["mpn"], r["mfr"], r["lcsc"], s.get("lib") or "",
                        s.get("jlc_stock") if s.get("jlc_stock") is not None else "",
                        s.get("lcsc_stock") if s.get("lcsc_stock") is not None else "",
                        price if price is not None else "", "yes" if r["assembled"] else ("DNP" if r["dnp"] else "no"),
                        r["description"]])
    return buf.getvalue()
