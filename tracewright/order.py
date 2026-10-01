"""Ordering the board. Two ways to build it -- JLC turnkey (JLCPCB makes and assembles it from its
own parts library) or building it yourself (bare boards and a stencil from the fab, parts from
DigiKey / Mouser / LCSC, soldered by you) -- and for each: what the order needs and whether the files
are ready, a rough price, and the routes to the fab:

  - PCBWay: one click. The same upload PCBWay's own KiCad plugin makes (Gerbers, drill, IPC-D-356
    netlist, BOM and positions in one zip); PCBWay answers with its quote page for the board.
  - JLCPCB: its quote page opens with the package ready to drop in (JLC has no public upload; its
    Online API is by application only), and the order sheet lists what to pick on the page.
  - Parts (self-assembly): BOM files in DigiKey's, Mouser's and LCSC's upload formats, with the
    quantity for the boards you build plus spares, and their BOM tools' pages.

The sourcing mode is tracewright.json fab.sourcing ("jlc" | "self"); fab.assembly follows it, which
is what the checks read (turnkey: the JLC checks; self: the hand-assembly check)."""
import os, io, csv, json, math, time, shutil, zipfile, subprocess
from tw import kicad
from tw.board import Board

MODES = {
    "jlc": {"title": "JLC turnkey", "icon": "package",
            "text": "JLCPCB builds and assembles the boards. Parts need LCSC codes."},
    "self": {"title": "Self-assembly", "icon": "wrench",
             "text": "Bare boards and a stencil. Buy parts from DigiKey, Mouser or LCSC."},
}
LINKS = {
    "jlc": "https://cart.jlcpcb.com/quote",
    "digikey": "https://www.digikey.com/en/mylists/",
    "mouser": "https://www.mouser.com/bom/",
    "lcsc": "https://www.lcsc.com/bom",
}
PCBWAY_UPLOAD = "https://www.pcbway.com/Common/KiCadUpFile/"


def mode(tw):
    m = tw.setting("fab.sourcing")
    if m in MODES:
        return m
    return "jlc" if tw.setting("fab.assembly", True) else "self"


def set_mode(project, m):
    """fab.sourcing and fab.assembly together (the checks read fab.assembly)."""
    if m not in MODES:
        raise ValueError(f"sourcing is one of {', '.join(MODES)}")
    fab = project.cfg.setdefault("fab", {})
    fab["sourcing"] = m
    fab["assembly"] = m == "jlc"
    project.save()


# ------------------------------------------------------------------ the board as the fab sees it
def specs(board):
    w, h = board.size()
    oz = None
    cu = board.copper_mm("F.Cu")
    if cu:
        oz = round(cu / 0.035 * 2) / 2 or 1
    smd_sides = sorted({fp.side for fp in board.fp_list if any(p.kind == "smd" for p in fp.pads) and "exclude_from_bom" not in fp.attrs
                        and not fp.dnp})
    fine = fine_pitch(board)
    min_track = min((t.w for t in board.tracks), default=None)
    min_drill = min((v.drill for v in board.vias), default=None)
    from tw import stackup
    return {"w": round(w, 1), "h": round(h, 1), "layers": len(board.copper), "thickness": board.thickness or 1.6,
            "stackup": stackup.identify(board),
            "copper_oz": oz or 1, "smd_sides": smd_sides, "finish": "ENIG" if fine else "HASL (lead free)",
            "fine": [f"{r} ({p:g} mm)" for r, p in fine[:6]],
            "min_track": round(min_track, 3) if min_track else None, "min_drill": round(min_drill, 3) if min_drill else None,
            "parts": len([fp for fp in board.fp_list if fp.pads and "exclude_from_bom" not in fp.attrs and not fp.dnp])}


def fine_pitch(board, limit=0.45):
    """Parts whose pads HASL would not leave flat enough: a pitch under 0.5 mm (measured between the
    pads, whatever the footprint is called; pads stacked on one spot are one pad), and ball-grid or
    land-grid packages. 0.5 mm parts (TSSOP, QFN, USB-C) are routine on HASL. [(ref, pitch; 0 = grid)]"""
    out = []
    for fp in board.fp_list:
        if "exclude_from_bom" in fp.attrs or fp.dnp:
            continue
        smd = [p for p in fp.pads if p.kind == "smd"]
        name = fp.lib_id.upper()
        if len(smd) >= 4:
            pts = [(p.x, p.y) for p in smd]
            gaps = [math.dist(a, b) for i, a in enumerate(pts) for b in pts[i + 1:]]
            gaps = [g for g in gaps if g > 0.1]
            pitch = min(gaps) if gaps else 9
            if pitch <= limit:
                out.append((fp.ref, round(pitch, 2)))
                continue
        if "BGA" in name or "LGA" in name:
            out.append((fp.ref, 0.0))
    return sorted(out, key=lambda x: (x[1] or 9, x[0]))


# ------------------------------------------------------------------ a rough price
def estimate(sp, totals, m, qty):
    """A rough price from JLCPCB's published rates (their page has the exact one). USD, no shipping."""
    area = sp["w"] * sp["h"] / 100.0                  # cm²
    small = sp["w"] <= 100 and sp["h"] <= 100
    n = max(5, qty)
    if sp["layers"] <= 2:
        pcb = 2.0 if small and n <= 10 else 5.0 + 0.004 * area * n
    elif sp["layers"] <= 4:
        pcb = 7.0 if small and n <= 10 else 15.0 + 0.012 * area * n
    elif sp["layers"] <= 6:
        pcb = 55.0 + 0.03 * area * n
    elif sp["layers"] <= 8:
        pcb = 120.0 + 0.045 * area * n
    else:
        pcb = 180.0 + 0.06 * area * n
    if sp["copper_oz"] >= 2:
        pcb *= 1.6
    if sp["finish"].startswith("ENIG"):
        pcb += 15.0
    out = {"qty": n, "pcb": round(pcb, 2), "lines": []}
    out["lines"].append([f"{n} boards, {sp['layers']} layers, {sp['w']:g} × {sp['h']:g} mm", round(pcb, 2)])
    parts = totals.get("cost")
    if m == "jlc":
        standard = len(sp["smd_sides"]) > 1
        setup = 25.0 if standard else 8.0
        stencil = 7.86 if standard else 1.5
        ext = 3.0 * (totals.get("extended") or 0)
        joints = 0.0017 * (totals.get("assembled") or 0) * 2.5 * n
        per = (parts or 0) * n
        out["lines"] += [[f"Assembly setup ({'standard, both sides' if standard else 'economic'})", setup], ["Stencil", stencil],
                         [f"Extended parts fee ({totals.get('extended') or 0} × $3)", round(ext, 2)],
                         [f"Parts for {n} boards" + ("" if parts is not None else " (not priced yet)"), round(per, 2)],
                         ["Soldering", round(joints, 2)]]
        total = pcb + setup + stencil + ext + per + joints
    else:
        stencil = 7.0
        per = (parts or 0) * n * 1.4                  # DigiKey / Mouser run above LCSC's prices
        out["lines"] += [["Stencil (frameless)", stencil],
                         [f"Parts for {n} boards" + (" (estimated from LCSC prices)" if parts is not None else " (not priced yet)"), round(per, 2)]]
        total = pcb + stencil + per
    out["total"] = round(total, 2)
    out["note"] = "Estimated from JLCPCB's published rates and LCSC prices. Shipping and tax not included."
    return out


# ------------------------------------------------------------------ is the order ready?
def readiness(tw, data, checks, m, fresh):
    """[{id, ok (True | False | None: unknown), title, detail, fix}] for the order in mode m."""
    t = data.get("totals", {}) if data and not data.get("empty") else {}
    rows = [r for r in (data.get("rows") or []) if r.get("assembled")] if data else []
    out = []
    v = (checks or {}).get("verdict")
    c = (checks or {}).get("counts") or {}
    out.append({"id": "checks", "ok": None if not v else v == "checks pass",
                "title": "Checks pass" if v == "checks pass" else f"{c.get('warning', 0)} check warnings to review" if v == "review warnings"
                else "Check errors" if v else "Checks not run",
                "detail": f"{c.get('error', 0)} errors, {c.get('warning', 0)} warnings" if v else "Run checks before ordering",
                "fix": "checks", "blocker": v == "not ready"})
    out.append({"id": "files", "ok": bool(fresh), "title": "Fab files are up to date" if fresh else "Fab files are older than the board",
                "detail": "Gerbers, drill, BOM and positions" if fresh else "Regenerated when you order", "fix": None})
    if m == "jlc":
        no = t.get("no_lcsc") or 0
        out.append({"id": "lcsc", "ok": no == 0, "title": "Every part has an LCSC code" if not no else f"{no} parts have no LCSC code",
                    "detail": "Required for JLC assembly" if no else f"{t.get('codes', 0)} distinct parts",
                    "fix": "bom", "blocker": bool(no)})
        short = [r for r in rows if (r.get("source") or {}).get("jlc_stock") is not None and (r["source"]["jlc_stock"] or 0) < r["qty"] * 5]
        unknown = [r for r in rows if r.get("lcsc") and (r.get("source") or {}).get("jlc_stock") is None]
        out.append({"id": "stock", "ok": None if unknown and not short else not short,
                    "title": f"{len(short)} parts short at JLC" if short else f"Stock unknown for {len(unknown)} parts" if unknown else "JLC has the parts in stock",
                    "detail": ", ".join(r["lcsc"] for r in short[:6]) if short else "Look up stock in the BOM tab" if unknown else f"{t.get('basic', 0)} Basic, {t.get('extended', 0)} Extended",
                    "fix": "bom", "blocker": bool(short)})
    else:
        nompn = [r for r in rows if not (r.get("mpn") or (r.get("source") or {}).get("mpn"))]
        out.append({"id": "mpn", "ok": not nompn, "title": "Every part has a manufacturer part number" if not nompn else f"{len(nompn)} lines have no MPN",
                    "detail": ", ".join(",".join(r["refs"][:3]) for r in nompn[:5]) if nompn else "Used to find parts at DigiKey and Mouser",
                    "fix": "bom", "blocker": False})
    return out


# ------------------------------------------------------------------ the files
def _stale(tw, f):
    if not os.path.exists(f):
        return True
    t = os.path.getmtime(f)
    return any(p and os.path.exists(p) and os.path.getmtime(p) > t for p in [tw.pcb, tw.sch] + list(tw.sheets()))


def fab_fresh(tw):
    name = tw.stem or "board"
    return not any(_stale(tw, os.path.join(tw.build, "fab", n)) for n in
                   (f"{name}-gerbers.zip", f"{name}-BOM-JLC.csv", f"{name}-CPL-JLC.csv"))


def fab_files(tw, say=print):
    """Gerbers + drill, the BOM and CPL (JLC's columns, fitted to JLC's footprints) and the IPC-D-356
    netlist, made again when the board or schematic is newer."""
    from tw.outputs import Outputs
    o = Outputs(tw)
    o.say = say
    name = o.name
    if not fab_fresh(tw):
        nl = o.netlist()
        parts = o.parts(nl)
        o.bom(parts)
        o.cpl(parts)
        o.gerbers()
    ipc = os.path.join(o.fab, f"{name}-ipc356.ipc")
    if _stale(tw, ipc):
        kicad.cli("pcb", "export", "ipcd356", "-o", ipc, tw.pcb)
    return {"gerbers": os.path.join(o.fab, f"{name}-gerbers.zip"), "gerber_dir": os.path.join(o.fab, "gerbers"),
            "bom": os.path.join(o.fab, f"{name}-BOM-JLC.csv"), "cpl": os.path.join(o.fab, f"{name}-CPL-JLC.csv"), "ipc": ipc}


def order_dir(tw, target):
    d = os.path.join(tw.build, "order", target)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    return d


def jlc_package(tw, sp, est, m, say=print):
    """build/order/jlc: what JLC's quote page takes (the Gerber zip; the BOM and CPL for assembly) and
    the order sheet: every setting to pick on the page."""
    f = fab_files(tw, say)
    d = order_dir(tw, "jlc")
    name = tw.stem or "board"
    shutil.copy(f["gerbers"], d)
    if m == "jlc":
        shutil.copy(f["bom"], d)
        shutil.copy(f["cpl"], d)
    sheet = order_sheet(tw, sp, est, m)
    with open(os.path.join(d, "ORDER-SHEET.md"), "w") as fh:
        fh.write(sheet)
    say(f"JLC package: {len(os.listdir(d))} files in build/order/jlc")
    return {"dir": d, "files": sorted(os.listdir(d)), "sheet": sheet, "url": LINKS["jlc"], "name": name}


def order_sheet(tw, sp, est, m):
    lines = [f"# Ordering {tw.stem} from JLCPCB", "",
             "1. Upload `" + f"{tw.stem}-gerbers.zip" + "` on the quote page (it reads the size and layers from it).",
             "2. Check these against what the page shows:", "",
             f"   - Layers: {sp['layers']}" + (f", layer stack-up {sp['stackup']} (the build the board was designed on)" if sp.get("stackup") and sp["layers"] > 2 else ""),
             f"   - Size: {sp['w']:g} x {sp['h']:g} mm",
             f"   - Thickness: {sp['thickness']:g} mm", f"   - Outer copper: {sp['copper_oz']:g} oz",
             f"   - Surface finish: {sp['finish']}" + (f" -- flat pads for the fine-pitch parts: {', '.join(x.replace(' (0 mm)', '') for x in sp['fine'])}"
                                                    if sp["finish"].startswith("ENIG") else ""),
             f"   - Min track {sp['min_track']:g} mm" if sp.get("min_track") else "   - Min track: the page's default",
             f"   - Min via drill {sp['min_drill']:g} mm" if sp.get("min_drill") else "   - Min via drill: the page's default",
             f"   - Quantity: {est['qty']}", ""]
    if m == "jlc":
        lines += ["3. Turn on **PCB Assembly**:", "",
                  f"   - PCBA type: {'Standard (parts on both sides)' if len(sp['smd_sides']) > 1 else 'Economic (JLC moves the order to Standard if a part needs it)'}",
                  f"   - Assembly side: {'both' if len(sp['smd_sides']) > 1 else 'top' if sp['smd_sides'] == ['F'] else 'bottom'}",
                  f"   - Upload `{tw.stem}-BOM-JLC.csv` as the BOM and `{tw.stem}-CPL-JLC.csv` as the CPL.",
                  "   - On the placement preview, compare a few polarised parts (diodes, ICs) with the board.", ""]
    else:
        lines += ["3. Add a **stencil** (frameless is fine for hand work), for the side(s) with SMD parts.", "",
                  "4. Order the parts with the BOM files from the Order tab (DigiKey, Mouser or LCSC).", ""]
    lines += [f"Rough total: ${est['total']:.2f} ({est['note']})", ""]
    return "\n".join(lines)


def pcbway_zip(tw, say=print):
    """The zip PCBWay's KiCad plugin uploads: Gerbers + drill, PCBWay_netlist.ipc, PCBWay_bom.csv and
    PCBWay_positions.csv (its columns)."""
    f = fab_files(tw, say)
    d = order_dir(tw, "pcbway")
    stage = os.path.join(d, "files")
    shutil.copytree(f["gerber_dir"], stage)
    shutil.copy(f["ipc"], os.path.join(stage, "PCBWay_netlist.ipc"))
    from . import bom as bomlib
    b = Board.load(tw.pcb)
    data = bomlib.bom_data(tw, b)
    with open(os.path.join(stage, "PCBWay_bom.csv"), "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["Designator", "Quantity", "Value", "Footprint", "Package", "MPN", "Manufacturer", "LCSC"])
        for r in data.get("rows") or []:
            if r["assembled"]:
                w.writerow([", ".join(r["refs"]), r["qty"], r["value"], r["footprint"], (r.get("source") or {}).get("package") or "",
                            r["mpn"] or (r.get("source") or {}).get("mpn") or "", r["mfr"] or (r.get("source") or {}).get("brand") or "", r["lcsc"]])
    ox, oy = b.aux_origin if getattr(b, "aux_origin", None) else (0.0, 0.0)
    placed = {ref for r in (data.get("rows") or []) if r["assembled"] for ref in r["refs"]}
    vals = {p["ref"]: p for p in data.get("parts") or []}
    with open(os.path.join(stage, "PCBWay_positions.csv"), "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["pos_x", "pos_y", "rotation", "side", "designator", "mpn", "pack", "footprint", "value", "mount_type"])
        for fp in sorted(b.fp_list, key=lambda x: x.ref):
            if fp.ref not in placed:
                continue
            smd = any(p.kind == "smd" for p in fp.pads)
            p = vals.get(fp.ref, {})
            w.writerow([round(fp.x - ox, 4), round(-(fp.y - oy), 4), round(fp.angle, 2), "top" if fp.side == "F" else "bottom", fp.ref,
                        p.get("mpn", ""), "", fp.lib_id.split(":")[-1], fp.value, "smt" if smd else "tht"])
    z = os.path.join(d, f"{tw.stem or 'board'}-pcbway.zip")
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        for n in sorted(os.listdir(stage)):
            zf.write(os.path.join(stage, n), n)
    shutil.rmtree(stage)
    w, h = b.size()
    say(f"PCBWay package: {os.path.basename(z)}")
    return {"zip": z, "w": round(w, 2), "h": round(h, 2), "layers": len(b.copper)}


async def pcbway_upload(pk, say=print):
    """Upload a pcbway_zip() package the way PCBWay's KiCad plugin does (multipart upload[file] plus
    the board's size and layer count); returns PCBWay's quote page for it."""
    import aiohttp
    say("uploading to PCBWay...")
    form = aiohttp.FormData()
    form.add_field("boardWidth", str(pk["w"]))
    form.add_field("boardHeight", str(pk["h"]))
    form.add_field("boardLayer", str(pk["layers"]))
    with open(pk["zip"], "rb") as fh:
        form.add_field("upload[file]", fh.read(), filename=os.path.basename(pk["zip"]), content_type="application/zip")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as s:
        async with s.post(PCBWAY_UPLOAD, data=form) as r:
            text = await r.text()
            status = r.status
    try:
        d = json.loads(text)
    except ValueError:
        raise RuntimeError(f"PCBWay answered {status} without a quote link") from None
    url = d.get("redirect") if isinstance(d, dict) else None
    if not url:
        raise RuntimeError(f"PCBWay did not return a quote link ({text[:160]})")
    if url.startswith("/"):
        url = "https://www.pcbway.com" + url
    return {"url": url, "zip": pk["zip"]}


# ------------------------------------------------------------------ parts for self-assembly
def spares(r):
    """Extra parts to buy: small passives get lost and tombstone."""
    pref = "".join(ch for ch in r["refs"][0] if ch.isalpha()).upper()
    if pref in ("R", "C", "L", "FB") or any(s in r["footprint"] for s in ("0402", "0603", "0805")):
        return max(2, math.ceil(r["qty"] * 0.1))
    return 0


def parts_files(tw, boards, say=print):
    """build/order/parts: the BOM in DigiKey's, Mouser's and LCSC's upload formats, for `boards` boards
    plus spares. Lines without an MPN keep their LCSC code (LCSC's list) and are named in the notes."""
    from . import bom as bomlib
    b = Board.load(tw.pcb) if tw.has_pcb() else None
    data = bomlib.bom_data(tw, b)
    d = order_dir(tw, "parts")
    name = tw.stem or "board"
    rows = [r for r in data.get("rows") or [] if r["assembled"]]
    lines = []
    for r in rows:
        s = r.get("source") or {}
        mpn = r["mpn"] or s.get("mpn") or ""
        lines.append({"qty": r["qty"] * boards + spares(r), "mpn": mpn, "mfr": r["mfr"] or s.get("brand") or "",
                      "refs": ",".join(r["refs"]), "desc": f"{' / '.join(r['values'])} {r['footprint']}".strip(), "lcsc": r["lcsc"]})
    files = {}
    def write(fn, header, fmt):
        path = os.path.join(d, fn)
        with open(path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            for l in lines:
                row = fmt(l)
                if row:
                    w.writerow(row)
        return path
    files["digikey"] = write(f"{name}-DigiKey.csv", ["Quantity", "Manufacturer Part Number", "Manufacturer", "Customer Reference", "Description"],
                             lambda l: [l["qty"], l["mpn"], l["mfr"], l["refs"], l["desc"]] if l["mpn"] else None)
    files["mouser"] = write(f"{name}-Mouser.csv", ["Mfr. #", "Manufacturer", "Quantity", "Customer #", "Description"],
                            lambda l: [l["mpn"], l["mfr"], l["qty"], l["refs"][:30], l["desc"]] if l["mpn"] else None)
    files["lcsc"] = write(f"{name}-LCSC.csv", ["Quantity", "LCSC Part Number", "Manufacture Part Number", "Manufacturer", "Customer NO.", "Description"],
                          lambda l: [l["qty"], l["lcsc"], l["mpn"], l["mfr"], l["refs"][:30], l["desc"]] if l["lcsc"] or l["mpn"] else None)
    missing = [l for l in lines if not l["mpn"]]
    say(f"parts lists for {boards} boards: {len(lines)} lines" + (f", {len(missing)} without an MPN" if missing else ""))
    return {"dir": d, "files": {k: os.path.basename(v) for k, v in files.items()}, "lines": len(lines),
            "missing_mpn": [l["refs"] for l in missing], "links": {k: LINKS[k] for k in ("digikey", "mouser", "lcsc")}}
