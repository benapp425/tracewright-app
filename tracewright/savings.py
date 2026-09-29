"""Saving money at JLC: every Extended part costs a fee per order (about $3 for each distinct part) on
top of its price, while Basic parts are loaded on the machines already. For each Extended resistor and
capacitor on the board this finds a Basic part that is the same thing -- the same value, the same
package, at least the same voltage, the same kind of dielectric (C0G stays C0G), in stock for the boards
you order -- and says what the swap saves. Only exact equivalents: a divider's 97.6 k stays 97.6 k, a
0.1 % resistor is not swapped for a 1 % one, nor a 0.25 W resistor for a 0.1 W one."""
import re, time

FEE = 3.0                    # JLC's fee per distinct Extended part, per order (USD)
R_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([kKmMGR]?)\s*(?:Ω|ohm|R\b)", re.I)
C_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([pnuµm])\s*F", re.I)
V_RE = re.compile(r"(\d+(?:\.\d+)?)\s*V\b")
TOL_RE = re.compile(r"±\s*(\d+(?:\.\d+)?)\s*%|(?<![\d.])(\d+(?:\.\d+)?)\s*%")
W_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(m?)W\b")
DIELECTRICS = ("C0G", "NP0", "X7R", "X5R", "X7S", "X6S", "X8R", "Y5V", "Z5U")
PACKAGES = ("01005", "0201", "0402", "0603", "0805", "1206", "1210", "1812", "2010", "2512")


def _lib(it):
    v = (it or {}).get("lib")
    return {"base": "Basic", "expand": "Extended"}.get(v, v)


def package_of(footprint):
    m = re.search(r"(?<!\d)(01005|0201|0402|0603|0805|1206|1210|1812|2010|2512)(?!\d)", footprint or "")
    return m.group(1) if m else None


def ohms(text):
    """10k, 4k7, 4.7k, 100R, 100, 5mR, 1M -> ohms (None when it is not a resistance)."""
    t = re.sub(r"(\d)\s+(?=[kKMmRΩo])", r"\1", (text or "").strip())      # "10 k" -> "10k"
    t = t.split()[0] if t else ""                                        # "49.9k 1%" -> "49.9k"
    m = re.fullmatch(r"(\d+)([kKMmR])(\d+)", t)                       # 4k7, 2R2
    if m:
        mult = {"k": 1e3, "K": 1e3, "M": 1e6, "m": 1e-3, "R": 1}[m.group(2)]
        return float(f"{m.group(1)}.{m.group(3)}") * mult
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([kKMm]?)(?:R|Ω|ohms?)?", t)            # 10k, 100R, 5mR, 1M, 10kΩ
    if not m:
        return None
    return float(m.group(1)) * {"k": 1e3, "K": 1e3, "M": 1e6, "m": 1e-3, "": 1}[m.group(2)]


def farads(text):
    """100n, 4u7, 4.7uF, 22p C0G -> farads (None when it is not a capacitance)."""
    mult = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3}
    m = re.match(r"\s*(\d+)([pnuµm])(\d+)", text or "")                     # 4u7, 2n2
    if m:
        return float(f"{m.group(1)}.{m.group(3)}") * mult[m.group(2)]
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*([pnuµm])F?", text or "")
    if not m:
        return None
    return float(m.group(1)) * {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3}[m.group(2)]


def _desc_ohms(d):
    m = R_RE.search(d or "")
    if not m:
        return None
    return float(m.group(1)) * {"k": 1e3, "K": 1e3, "M": 1e6, "m": 1e-3, "G": 1e9, "R": 1, "r": 1, "": 1}[m.group(2)]


def _desc_farads(d):
    m = C_RE.search(d or "")
    if not m:
        return None
    return float(m.group(1)) * {"p": 1e-12, "n": 1e-9, "u": 1e-6, "U": 1e-6, "µ": 1e-6, "m": 1e-3, "P": 1e-12, "N": 1e-9}[m.group(2)]


def _volts(d):
    vs = [float(x) for x in V_RE.findall(d or "")]
    return max(vs) if vs else None


def _tolerance(d):
    """±1% -> 1.0 (percent); None when it does not say."""
    m = TOL_RE.search(d or "")
    return float(m.group(1) or m.group(2)) if m else None


def _watts(d):
    ws = [float(x) * (1e-3 if m else 1) for x, m in W_RE.findall(d or "")]
    return max(ws) if ws else None


def _dielectric(d):
    for k in DIELECTRICS:
        if k in (d or "").upper():
            return "C0G" if k == "NP0" else k
    return None


def _same(a, b, tol=1e-4):                    # the same value, not a near one: 49.9 k is not 50 k
    return a is not None and b is not None and abs(a - b) <= tol * max(abs(a), abs(b))


def _query(kind, value, pkg):
    if kind == "R":
        v = value
        s = f"{v / 1e6:g}MΩ" if v >= 1e6 else f"{v / 1e3:g}kΩ" if v >= 1e3 else f"{v:g}Ω"
    else:
        v = value
        s = f"{v / 1e-6:g}uF" if v >= 1e-6 else f"{v / 1e-9:g}nF" if v >= 1e-9 else f"{v / 1e-12:g}pF"
    return f"{s} {pkg}"


def equivalents(found, kind, value, pkg, stock_needed, need_tol=None, need_w=None, need_v=None, diel=None, basic=False, stock_key="jlc_stock"):
    """The search results that are the same resistor or capacitor: the same value and package, at least the
    tolerance, power and voltage, a dielectric at least as good (C0G stays C0G), and stock_needed in stock.
    basic: JLC Basic parts only."""
    good = []
    for it in found:
        d = it.get("describe") or ""
        if basic and _lib(it) != "Basic":
            continue
        if (it.get(stock_key) or 0) < stock_needed or pkg not in (it.get("package") or "") + d:
            continue
        if kind == "R" and not _same(_desc_ohms(d), value):
            continue
        t = _tolerance(d)
        if need_tol and (t is None or t > need_tol):
            continue
        if need_w:
            w = _watts(d)
            if w is None or w < need_w:
                continue
        if kind == "C":
            if not _same(_desc_farads(d), value):
                continue
            v = _volts(d)
            if need_v and (v is None or v < need_v):
                continue
            dd = _dielectric(d)
            if diel == "C0G" and dd != "C0G":
                continue
            if diel in ("X7R", "X7S", "X8R") and dd not in ("X7R", "X7S", "X8R", "C0G"):
                continue
            if dd in ("Y5V", "Z5U"):
                continue
        good.append(it)
    return good


def requirements(r, src):
    """What an equivalent of a BOM row must match: (kind, value, package, tolerance, watts, volts, dielectric)."""
    kind = r["refs"][0].rstrip("0123456789").upper()[:1]
    pkg = package_of(r["footprint"])
    now_desc = (src or {}).get("describe") or (src or {}).get("name") or ""
    if kind not in ("R", "C") or not pkg:
        return None
    value = ohms(r["value"].split()[0]) if kind == "R" else farads(r["value"])
    if value is None:
        return None
    return (kind, value, pkg, _tolerance(r["value"]) or _tolerance(now_desc),
            (_watts(r["value"]) or _watts(now_desc)) if kind == "R" else None,
            (_volts(r["value"]) or _volts(now_desc)) if kind == "C" else None,
            (_dielectric(r["value"]) or _dielectric(now_desc)) if kind == "C" else None)


def suggestions(tw, board=None, boards=5, budget=90.0, progress=None, stop=None):
    """[{refs, value, footprint, qty, now: {...}, instead: {...}, saving, why}] best first, plus what
    could not be matched and why."""
    from . import bom as bomlib
    from tw.jlc import Parts, LookupFailed
    data = bomlib.bom_data(tw, board)
    if data.get("empty"):
        return {"items": [], "skipped": [], "total": 0.0}
    parts = Parts(tw.root, deadline=time.time() + budget, stop=stop)
    rows = [r for r in data["rows"] if r["assembled"] and (r.get("source") or {}).get("lib") == "Extended"]
    out, skipped = [], []
    for i, r in enumerate(rows):
        if stop is not None and stop.is_set():
            break
        kind = r["refs"][0].rstrip("0123456789").upper()[:1]
        pkg = package_of(r["footprint"])
        src = r.get("source") or {}
        now_desc = src.get("describe") or src.get("name") or ""
        if progress:
            progress(i + 1, len(rows), ",".join(r["refs"][:3]))
        if kind not in ("R", "C") or not pkg:
            skipped.append({"refs": r["refs"], "why": "only resistors and capacitors in standard packages are matched automatically"})
            continue
        value = ohms(r["value"].split()[0]) if kind == "R" else farads(r["value"])
        if value is None:
            skipped.append({"refs": r["refs"], "why": f"could not read the value '{r['value']}'"})
            continue
        need_v = _volts(r["value"]) or _volts(now_desc) if kind == "C" else None
        need_tol = _tolerance(r["value"]) or _tolerance(now_desc)
        need_w = _watts(r["value"]) or _watts(now_desc) if kind == "R" else None
        diel = _dielectric(r["value"]) or _dielectric(now_desc) if kind == "C" else None
        try:
            found = parts.search(_query(kind, value, pkg), 25).get("items") or []
        except LookupFailed as e:
            skipped.append({"refs": r["refs"], "why": f"JLC did not answer ({e})"})
            continue
        need = r["qty"] * boards
        good = equivalents(found, kind, value, pkg, need * 1.2, need_tol, need_w, need_v, diel, basic=True)
        if not good:
            skipped.append({"refs": r["refs"], "why": "no Basic part with the same value and package in stock"})
            continue
        best = min(good, key=lambda it: (it.get("price_1") or 9, -(it.get("jlc_stock") or 0)))
        old_price = src.get("jlc_price") if src.get("jlc_price") is not None else src.get("lcsc_price")
        diff = ((old_price or 0) - (best.get("price_1") or 0)) * need if old_price is not None else 0.0
        out.append({"refs": r["refs"], "value": r["value"], "footprint": r["footprint"], "qty": r["qty"],
                    "now": {"lcsc": r["lcsc"], "mpn": r["mpn"] or src.get("mpn"), "price": old_price, "describe": now_desc[:140]},
                    "instead": {"lcsc": best["lcsc"], "mpn": best.get("mpn"), "brand": best.get("brand"), "price": best.get("price_1"),
                                "stock": best.get("jlc_stock"), "describe": (best.get("describe") or "")[:140]},
                    "saving": round(FEE + diff, 2)})
    out.sort(key=lambda x: -x["saving"])
    return {"items": out, "skipped": skipped, "total": round(sum(x["saving"] for x in out), 2), "checked": len(rows), "boards": boards}


def claude_message(items):
    """A message asking Claude to make the swaps (it keeps the schematic and any generator in step)."""
    lines = ["Swap these Extended parts for the JLC Basic equivalents below (same value, package, voltage and dielectric;",
             "checked in stock). Update the LCSC and MPN fields everywhere they are defined (the schematic and any design",
             "script that generates it), then run the BOM checks:", ""]
    for it in items:
        n, w = it["now"], it["instead"]
        lines.append(f"- {','.join(it['refs'])} ({it['value']}, {it['footprint']}): {n['lcsc']} -> {w['lcsc']} "
                     f"({w.get('mpn') or ''}, {w.get('describe', '')[:70]})")
    return "\n".join(lines)
