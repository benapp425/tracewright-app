"""BOM health: each line of the BOM judged for getting it built -- can it be bought (a part number, stock for the
boards being ordered, not discontinued), what it costs to place (JLC's extended parts carry a setup fee each), and what
the checks say about it -- with a score for the whole BOM and the lines worth acting on first.

    health(tw, board=None, boards=None) -> {"score", "lines", "rows": [{refs, value, footprint, flags}], "counts"}
"""
from . import bom as bomlib, stock

PENALTY = {"gone": 25, "out": 20, "nosource": 15, "low": 8, "missing": 6, "issue": 5, "unknown": 2, "extended": 1}


def health(tw, board=None, boards=None):
    data = bomlib.bom_data(tw, board)
    if data.get("empty"):
        return {"score": None, "lines": ["No schematic yet."], "rows": [], "counts": {}}
    st = stock.status(tw, board, boards, data)
    by_code = {r["lcsc"]: r for r in st["rows"]}
    mode = st["mode"]
    rows, counts = [], {k: 0 for k in PENALTY}
    for r in data["rows"]:
        if not r["assembled"]:
            continue
        flags = []
        s = r.get("source") or {}
        if not r["lcsc"] and not r["mpn"]:
            flags.append(("nosource", "error", "no part number: it cannot be ordered as it is"))
        elif not r["lcsc"] and mode == "jlc":
            flags.append(("nosource", "warning", "no LCSC code: JLC cannot place it; it is sourced by hand"))
        k = by_code.get(r["lcsc"]) if r["lcsc"] else None
        if k:
            if k["state"] == "gone":
                flags.append(("gone", "error", "discontinued"))
            elif k["state"] == "out":
                flags.append(("out", "error", f"{k['stock'] or 0} in stock, {k['need']} needed"))
            elif k["state"] == "low":
                flags.append(("low", "warning", f"only {k['stock']} in stock for {k['need']} needed"))
            elif k["state"] == "unknown":
                flags.append(("unknown", "info", "stock not looked up yet"))
        if s.get("lib") == "Extended" and mode == "jlc":
            flags.append(("extended", "info", "an extended part: JLC charges a setup fee for it"))
        if r.get("missing"):
            flags.append(("missing", "warning", f"not on the board: {', '.join(r['missing'][:4])}"))
        for i in r.get("issues") or []:
            flags.append(("issue", i.get("severity", "warning") if i.get("severity") in ("error", "warning") else "warning",
                          f"{i.get('ref')}: {i.get('message') or i.get('text') or 'a check finding'}"))
        for f in flags:
            counts[f[0]] += 1
        rows.append({"refs": r["refs"], "qty": r["qty"], "value": r["value"], "footprint": r["footprint"], "lcsc": r["lcsc"],
                     "mpn": r["mpn"], "flags": [{"kind": a, "sev": b, "text": c} for a, b, c in flags]})
    score = max(0, 100 - sum(PENALTY[k] * min(n, 4) for k, n in counts.items()))
    rows.sort(key=lambda x: (min([{"error": 0, "warning": 1, "info": 2}[f["sev"]] for f in x["flags"]] or [3]), x["refs"][0]))
    lines = []
    if counts["gone"] or counts["out"]:
        lines.append(f"{counts['gone'] + counts['out']} line{'s' if counts['gone'] + counts['out'] != 1 else ''} cannot be bought for "
                     f"{st['boards']} boards: replace them first")
    if counts["nosource"]:
        lines.append(f"{counts['nosource']} line{'s' if counts['nosource'] != 1 else ''} without a part number to order by")
    if counts["low"]:
        lines.append(f"{counts['low']} line{'s' if counts['low'] != 1 else ''} with little stock left")
    if counts["extended"]:
        lines.append(f"{counts['extended']} extended part{'s' if counts['extended'] != 1 else ''} (a setup fee each at JLC)")
    if counts["unknown"]:
        lines.append(f"stock not looked up for {counts['unknown']} line{'s' if counts['unknown'] != 1 else ''} (BOM > Check stock)")
    if not lines:
        lines.append("Every line can be bought for the boards being ordered")
    return {"score": score, "lines": lines, "rows": rows, "counts": counts, "boards": st["boards"], "mode": mode}
