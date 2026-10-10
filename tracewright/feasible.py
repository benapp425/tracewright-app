"""Can the board be built as set? Checked while the guided start fills in -- the canvas (floorplan, parts,
connectors, requirements) against the limits the user picked -- so a pick that cannot work is said at once, with what
would: on the canvas for the user, and to Claude in its tool replies.

    issues = check(root)          # [{"level": "impossible" | "tight", "key", "what", "why", "fix"}], impossible first

The rules are a hardware engineer's rules of thumb, kept plain: a check that says "tight" means it can work with care.
"""
import json, math, os, re

from . import canvas


def _cfg(root):
    try:
        with open(os.path.join(root, "tracewright.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _text(cv):
    """Everything the canvas says, lower case: requirements, blocks, links, parts, connectors."""
    bits = []
    for it in (cv.get("requirements") or {}).get("items") or []:
        bits += [str(it.get("label", "")), str(it.get("value", ""))]
    d = cv.get("diagram") or {}
    for b in d.get("blocks") or []:
        bits += [str(b.get("label", "")), str(b.get("note", ""))]
    for l in d.get("links") or []:
        bits.append(str(l.get("label", "")))
    for p in (cv.get("parts") or {}).get("items") or []:
        bits += [str(p.get(k, "")) for k in ("role", "mpn", "package", "why")]
    for c in (cv.get("connectors") or {}).get("items") or []:
        bits += [str(c.get("name", "")), str(c.get("type", ""))]
    return " ".join(bits).lower()


def _bgas(cv):
    """[(name, balls, pitch mm or None)] for the BGA packages among the parts."""
    out = []
    for p in (cv.get("parts") or {}).get("items") or []:
        s = f"{p.get('package', '')} {p.get('mpn', '')} {p.get('role', '')}"
        m = re.search(r"\b(?:lf|tf|f|u|v|w)?bga[-_ ]?(\d{2,4})\b", s, re.I) or re.search(r"\b(\d{2,4})[- ]?(?:ball|pin)s?\s*(?:lf|tf|f)?bga\b", s, re.I)
        if not m:
            continue
        pitch = re.search(r"(?:pitch|p)\s*(0\.\d+)|\b(0\.[3-9]\d?)\s*mm", s, re.I)
        out.append((p.get("mpn") or p.get("role") or "BGA", int(m.group(1)), float(next(g for g in pitch.groups() if g)) if pitch else None))
    return out


# tall parts by kind: how high they stand above the board (mm), from typical data sheets
HEIGHTS = [(r"rj-?45|8p8c|magjack", 13.5, "an RJ45 jack"), (r"barrel|dc[- ]?jack", 11.0, "a barrel jack"),
           (r"usb[- ]?a\b", 7.0, "a USB-A receptacle"), (r"\b2\.54\b|2x\d+\s*header|1x\d+\s*header|pin header|gpio 2x", 8.5, "a 2.54 mm pin header"),
           (r"electrolytic|e-cap", 8.0, "an electrolytic capacitor"), (r"\bsma\b", 6.4, "an SMA connector"),
           (r"jst[- ]?ph\b", 6.0, "a JST-PH connector"), (r"jst[- ]?xh\b", 9.8, "a JST-XH connector"),
           (r"screw terminal|terminal block", 10.0, "a screw terminal"), (r"relay", 15.0, "a relay"),
           (r"oled|lcd|display module", 7.0, "a display module"), (r"usb[- ]?c\b", 3.3, "a USB-C receptacle")]


def ipc_width_mm(amps, rise=10.0, oz=1.0):
    """IPC-2221 width of an outer-layer track for a current at a temperature rise (mm)."""
    area_mil2 = (amps / (0.048 * rise ** 0.44)) ** (1 / 0.725)
    return area_mil2 / (1.378 * oz) * 0.0254


def check(root, cv=None, cfg=None):
    from tw import constraints
    cv = cv if cv is not None else canvas.load(root)
    cfg = cfg if cfg is not None else _cfg(root)
    lim = constraints.get(cfg)
    hdi = bool((cfg.get("route") or {}).get("hdi"))
    text = _text(cv)
    fp = cv.get("floorplan") or {}
    out = []
    add = lambda level, key, what, why, fix: out.append({"level": level, "key": key, "what": what, "why": why, "fix": fix})

    # the floorplan: what does not fit, against the size limit too
    if fp.get("board"):
        from . import fpsolve
        W, H = float(fp["board"]["w"]), float(fp["board"]["h"])
        _, rep = fpsolve.solve(fp)
        for iid in rep["unfit"]:
            it = next((o for o in fp.get("items") or [] if o["id"] == iid), {"id": iid})
            line = next((l for l in rep["lines"] if l.startswith(fpsolve._name(it))), "")
            add("impossible", f"fit:{iid}", f"{fpsolve._name(it)} does not fit",
                (line[0].upper() + line[1:] + ".") if line else "No room left for it on the board.", fpsolve.bigger(fp).capitalize() + ".")
        used = sum(float(o["w"]) * float(o["h"]) for o in fp.get("items") or [] if fpsolve.side(o) == "top")
        if W * H and used / (W * H) > 0.8 and not rep["unfit"]:
            add("tight", "fill", f"The parts fill {round(100 * used / (W * H))} % of the board",
                "Routing needs room between them: above about 70 % it gets hard.", fpsolve.bigger(fp).capitalize() + ", or move parts to the bottom side.")
        mx = lim.get("max_size_mm")
        if mx and len(mx) == 2:
            a, b = sorted(mx, reverse=True)
            big, small = max(W, H), min(W, H)
            if big > a + 0.01 or small > b + 0.01:
                add("impossible", "size", f"The floorplan's board is {W:g} x {H:g} mm",
                    f"Your size limit is {mx[0]:g} x {mx[1]:g} mm.", "Shrink the floorplan's board, or raise the size limit.")
            if rep["unfit"]:
                m = re.search(r"about (\d+) x (\d+)", fpsolve.bigger(fp))
                if m and (max(int(m.group(1)), int(m.group(2))) > a or min(int(m.group(1)), int(m.group(2))) > b):
                    add("impossible", "size-fit", "Everything will not fit within your size limit",
                        f"The parts need about {m.group(1)} x {m.group(2)} mm; the limit is {mx[0]:g} x {mx[1]:g}.",
                        "Raise the size limit, use both sides of the board, or choose smaller parts.")

    # sides: a BGA's small capacitors go underneath it
    if lim.get("assembly_sides") == "top only":
        for name, balls, pitch in _bgas(cv):
            if balls >= 64:
                add("tight", f"sides:{name}", f"{name} with parts on the top only",
                    "Its small decoupling capacitors belong right under it, on the bottom, beside each ball's via; on top "
                    "they sit outside the ball grid, millimetres from the inner balls.",
                    "Allow parts on both sides (two-sided assembly costs more), or accept the longer paths.")

    # layers: what the parts need to get their pins out, and fast pairs
    layers = lim.get("layers")
    if layers:
        for name, balls, pitch in _bgas(cv):
            need = 6 if balls >= 196 else 4 if balls >= 64 else 2
            if layers < need:
                level = "impossible" if layers <= 2 and need >= 4 else "tight"
                add(level, f"bga:{name}", f"{layers} layers for {name} ({balls}-ball BGA)",
                    f"Its inner balls need {need} layers to get out" + (" (4 is possible with a careful escape plan)" if need == 6 and layers == 4 else "") + ".",
                    f"Use {need} layers" + (" or a smaller package" if layers <= 2 else "") + ".")
            else:                                         # enough in total: the signal layers its rings need, with one to spare
                from tw import stackup as _st
                sig = (_st.TEMPLATES.get(layers) or "").count("S")
                side = math.ceil(math.sqrt(balls))
                rings = math.ceil(side / 2)
                top, per = (2, 2) if (pitch or 0.8) >= 0.75 else (1, 1)
                need_s = 1 + math.ceil(max(0, rings - top) / per)
                if sig and need_s >= sig and balls >= 144:
                    add("tight", f"bga-margin:{name}", f"{name} needs about all {sig} signal layers of {layers}",
                        f"Its {rings} rings of balls take about {need_s} signal layers to get out; {layers} layers give {sig}, "
                        "none to spare where a capacitor under it or a via is in the way.",
                        f"Use {layers + 2} layers for a spare signal layer (or plan the escape with care).")
            if pitch and pitch <= 0.5 and not hdi:
                add("tight", f"hdi:{name}", f"{name} has a {pitch:g} mm pitch",
                    "Between its balls there is no room for a normal via: it needs via-in-pad or microvias (HDI).",
                    "Turn on HDI in the routing settings (it costs more), or choose a 0.65 mm or 0.8 mm pitch package.")
        if layers <= 2 and re.search(r"usb ?2(\.0)? ?hs|high[- ]speed usb|ethernet|rmii|rgmii|ddr|mipi|hdmi|pcie|lvds", text):
            add("tight", "pairs-2l", "Fast pairs on 2 layers",
                "USB high speed, Ethernet and the like want a solid ground plane right under their tracks.",
                "Use 4 layers, or keep those tracks short over an unbroken ground pour.")

    # height: the tallest kind of part named against the limit
    mh = lim.get("max_height_mm")
    if mh:
        for pat, hgt, what in HEIGHTS:
            if hgt > mh + 0.01 and re.search(pat, text):
                add("impossible", f"height:{what}", f"{what[0].upper()}{what[1:]} is about {hgt:g} mm tall",
                    f"Your height limit is {mh:g} mm.", "Choose a lower part (right-angle or low-profile), or raise the height limit.")

    # current: the width the input current needs on the outer copper
    amps = lim.get("max_input_a")
    if amps and fp.get("board"):
        oz = float(lim.get("copper_oz") or 1)
        w = ipc_width_mm(float(amps), 10.0, oz)
        small = min(float(fp["board"]["w"]), float(fp["board"]["h"]))
        if w > 0.3 * small:
            add("tight", "current", f"{amps:g} A needs a {w:.1f} mm wide track on {oz:g} oz copper",
                "That is a large share of the board's width (IPC-2221, 10 °C rise).",
                "Use 2 oz copper, carry it on pours on several layers, or keep the high-current path short.")

    # cost and stock: the parts' prices against the budget; parts that cannot be bought
    parts = (canvas.enrich_parts(root, cv).get("parts") or {}).get("items") or []
    budget = lim.get("cost_usd")
    priced = [(p, float(p["price"]) * int(p.get("qty") or 1)) for p in parts if p.get("price") is not None]
    if budget and priced:
        total = sum(c for _, c in priced)
        if total > budget:
            add("impossible", "cost", f"The key parts alone cost ${total:.2f} a board", f"Your budget is ${budget:g}.",
                "Raise the budget, or ask Claude for cheaper parts (the Parts tab's savings finds exact equivalents).")
        elif total > 0.85 * budget:
            add("tight", "cost", f"The key parts cost ${total:.2f} of your ${budget:g} budget", "The passives and assembly come on top.",
                "Leave room: raise the budget or choose cheaper key parts.")
    qty = int(lim.get("quantity") or 0)
    for p in parts:
        stock = p.get("jlc_stock") if p.get("jlc_stock") is not None else p.get("lcsc_stock")
        if stock is None or not p.get("found"):
            continue
        need = max(1, qty) * int(p.get("qty") or 1)
        if stock == 0:
            add("impossible", f"stock:{p.get('lcsc')}", f"{p.get('mpn') or p.get('lcsc')} is out of stock", "JLC and LCSC have none.",
                "Ask Claude for an in-stock equivalent.")
        elif stock < need:
            add("tight", f"stock:{p.get('lcsc')}", f"{p.get('mpn') or p.get('lcsc')}: {stock} in stock", f"{need} are needed for {max(1, qty)} boards.",
                "Order fewer boards, or ask Claude for a second source.")
    out.sort(key=lambda x: 0 if x["level"] == "impossible" else 1)
    return out


def lines(issues, level=None):
    """The issues as lines for Claude."""
    end = lambda t: t if t.endswith((".", "!", "?")) else t + "."
    return [f"{'CANNOT WORK' if i['level'] == 'impossible' else 'TIGHT'}: {end(i['what'])} {end(i['why'])} Fix: {end(i['fix'])}"
            for i in issues if level is None or i["level"] == level]
