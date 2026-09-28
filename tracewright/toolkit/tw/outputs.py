"""Fabrication and documentation outputs (kicad-cli), named after the project.

build/fab/     <name>-gerbers.zip (Gerbers + Excellon + drill map), <name>-BOM-JLC.csv,
               <name>-CPL-JLC.csv (turned / shifted onto JLC's footprints when sourcing/jlc-placement.json
               has corrections), <name>.step, <name>-ipc356.ipc
build/docs/    <name>-schematic.pdf, <name>-board.pdf (a page per copper layer), <name>-assembly.pdf,
               <name>-board-stats.json
build/images/  3D renders (top, bottom, iso)
build/release/ <name>-rev<rev>-<date>.zip: all of the above + checks report + manifest

The BOM groups by value + footprint + LCSC code and leaves out DNP parts, parts excluded from the
BOM and parts listed in tracewright.json fab.not_assembled (e.g. a module plugged in later).
"""
import os, csv, json, math, re, shutil, zipfile, datetime, hashlib, subprocess
from . import env, kicad
from .netlist import Netlist
from .checks.bom import lcsc_of


def natural(ref):
    m = re.match(r"([A-Za-z#]+)(\d+)", ref)
    return (m.group(1), int(m.group(2))) if m else (ref, 0)


class Outputs:
    def __init__(self, project=None):
        self.p = project or env.project()
        self.name = self.p.stem or "board"
        self.fab = self.p.out("fab")
        self.docs = self.p.out("docs")
        self.img = self.p.out("images")
        self.log = []

    def say(self, msg):
        self.log.append(msg)
        print(msg, flush=True)

    # ------------------------------------------------------------------ BOM / CPL
    def netlist(self):
        out = os.path.join(self.p.build, f"{self.name}.net")
        kicad.netlist(self.p.sch, out)
        return Netlist.load(out)

    def parts(self, nl):
        skip = set(self.p.setting("fab.not_assembled", []) or [])
        ctxlike = _CfgOnly(self.p)
        out = []
        for ref, p in nl.parts.items():
            if ref.startswith("#"):
                continue
            out.append({"ref": ref, "value": p["value"], "footprint": p["footprint"].split(":")[-1],
                        "mpn": p["fields"].get("MPN", ""), "mfr": p["fields"].get("Manufacturer", ""),
                        "lcsc": lcsc_of(p["fields"], ctxlike),
                        "placed": p["in_bom"] and not p["dnp"] and ref not in skip and p.get("on_board", True)})
        return out

    def bom(self, parts):
        groups = {}
        for p in parts:
            if not p["placed"]:
                continue
            k = (p["value"], p["footprint"], p["lcsc"], p["mpn"], p["mfr"])
            groups.setdefault(k, []).append(p["ref"])
        rows = []
        for (val, fp, lcsc, mpn, mfr), refs in sorted(groups.items(), key=lambda kv: natural(sorted(kv[1], key=natural)[0])):
            refs = sorted(refs, key=natural)
            rows.append({"Comment": val, "Designator": ",".join(refs), "Footprint": fp, "LCSC Part #": lcsc,
                         "Quantity": len(refs), "MPN": mpn, "Manufacturer": mfr})
        path = os.path.join(self.fab, f"{self.name}-BOM-JLC.csv")
        with open(path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["Comment", "Designator", "Footprint", "LCSC Part #", "Quantity", "MPN",
                                               "Manufacturer"])
            w.writeheader()
            w.writerows(rows)
        missing = [r["Designator"] for r in rows if not r["LCSC Part #"]]
        self.say(f"BOM: {len(rows)} lines, {sum(r['Quantity'] for r in rows)} parts"
                 + (f"; no LCSC code: {', '.join(missing)}" if missing else ""))
        return path, rows

    def cpl_rows(self, parts, corrections=True):
        """CPL rows (JLC columns) for the placed parts, with the JLC footprint corrections applied."""
        tmp = os.path.join(self.fab, "pos_kicad.csv")
        kicad.cli("pcb", "export", "pos", "--format", "csv", "--units", "mm", "--side", "both", "-o", tmp, self.p.pcb)
        keep = {p["ref"] for p in parts if p["placed"]}
        code = {p["ref"]: p["lcsc"] for p in parts}
        fixf = self.p.path("sourcing", "jlc-placement.json")
        fix = {}
        if corrections and os.path.exists(fixf):
            with open(fixf) as f:
                fix = {k: v for k, v in json.load(f).items() if not k.startswith("_")}
        rows, fixed = [], 0
        with open(tmp) as fh:
            for r in csv.DictReader(fh):
                if r["Ref"] not in keep:
                    continue
                x, y, rot = float(r["PosX"]), float(r["PosY"]), float(r["Rot"])
                f = fix.get(code.get(r["Ref"]))
                if f:
                    c, s = math.cos(math.radians(rot)), math.sin(math.radians(rot))
                    x, y, rot = x + c * f["dx"] - s * f["dy"], y + s * f["dx"] + c * f["dy"], rot + f["rot"]
                    fixed += 1
                rows.append({"Designator": r["Ref"], "Mid X": f"{x:.3f}mm", "Mid Y": f"{y:.3f}mm",
                             "Layer": "Top" if r["Side"].lower().startswith("top") else "Bottom",
                             "Rotation": f"{rot % 360:.1f}"})
        os.remove(tmp)
        return sorted(rows, key=lambda r: natural(r["Designator"])), fixed

    def cpl(self, parts, corrections=True):
        rows, fixed = self.cpl_rows(parts, corrections)
        path = os.path.join(self.fab, f"{self.name}-CPL-JLC.csv")
        with open(path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])
            w.writeheader()
            w.writerows(rows)
        self.say(f"CPL: {len(rows)} placements" + (f", {fixed} turned/shifted onto JLC's footprints" if fixed else ""))
        return path

    # ------------------------------------------------------------------ plots
    def gerbers(self):
        gdir = os.path.join(self.fab, "gerbers")
        shutil.rmtree(gdir, ignore_errors=True)
        os.makedirs(gdir)
        from .board import Board
        b = Board.load(self.p.pcb)
        layers = b.copper + ["F.Paste", "B.Paste", "F.Silkscreen", "B.Silkscreen", "F.Mask", "B.Mask", "Edge.Cuts"]
        kicad.cli("pcb", "export", "gerbers", "--layers", ",".join(layers), "--subtract-soldermask", "-o", gdir + os.sep,
                  self.p.pcb)
        kicad.cli("pcb", "export", "drill", "--format", "excellon", "--excellon-separate-th", "--generate-map",
                  "--map-format", "gerberx2", "--excellon-units", "mm", "-o", gdir + os.sep, self.p.pcb)
        z = os.path.join(self.fab, f"{self.name}-gerbers.zip")
        with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(os.listdir(gdir)):
                zf.write(os.path.join(gdir, f), f)
        self.say(f"Gerbers: {len(os.listdir(gdir))} files -> {os.path.relpath(z, self.p.root)}")
        return z

    def pdfs(self):
        out = []
        if self.p.has_sch():
            out.append(kicad.sch_pdf(self.p.sch, os.path.join(self.docs, f"{self.name}-schematic.pdf")))
        from .board import Board
        b = Board.load(self.p.pcb)
        pages = [f"{l},Edge.Cuts" + (",F.Silkscreen" if l == "F.Cu" else ",B.Silkscreen" if l == "B.Cu" else "")
                 for l in b.copper]
        tmpd = os.path.join(self.docs, "_pages")
        shutil.rmtree(tmpd, ignore_errors=True)
        os.makedirs(tmpd)
        files = []
        for i, layers in enumerate(pages):
            f = os.path.join(tmpd, f"p{i}.pdf")
            kicad.cli("pcb", "export", "pdf", "--layers", layers, "--mode-single", "--include-border-title",
                      "--drill-shape-opt", "1", "-o", f, self.p.pcb, *(["--mirror"] if layers.startswith("B.") else []))
            files.append(f)
        board_pdf = os.path.join(self.docs, f"{self.name}-board.pdf")
        if not _merge_pdfs(files, board_pdf):
            for i, f in enumerate(files):
                shutil.copy(f, os.path.join(self.docs, f"{self.name}-board-{b.copper[i]}.pdf"))
        shutil.rmtree(tmpd, ignore_errors=True)
        asm = os.path.join(self.docs, f"{self.name}-assembly.pdf")
        kicad.cli("pcb", "export", "pdf", "--layers", "F.Fab,F.Silkscreen,Edge.Cuts", "--mode-single", "--include-border-title",
                  "--sketch-pads-on-fab-layers", "-o", asm, self.p.pcb)
        self.say("PDFs: schematic, board layers, assembly")
        return out

    def renders(self, quality="high", views=("top", "bottom", "iso")):
        spec = {"top": ["--side", "top"], "bottom": ["--side", "bottom"],
                "iso": ["--side", "top", "--rotate", "-40,0,-30", "--perspective", "--zoom", "0.9"]}
        out = []
        for v in views:
            f = os.path.join(self.img, f"{self.name}-{v}.png")
            kicad.pcb_render(self.p.pcb, f, width=2400, height=1500, quality=quality, extra=spec[v][2:] if v == "iso" else (),
                             side=spec[v][1])
            out.append(f)
        self.say("renders: " + ", ".join(views))
        return out

    def step(self):
        f = os.path.join(self.fab, f"{self.name}.step")
        if os.path.exists(f):
            os.remove(f)
        exe = env.require("cli")
        subprocess.run([exe, "pcb", "export", "step", "--subst-models", "--force", "-o", f, self.p.pcb],
                       capture_output=True, text=True, timeout=900, cwd=os.path.dirname(self.p.pcb))
        ok = os.path.exists(f) and os.path.getsize(f) > 1000
        kicad.cli("pcb", "export", "ipcd356", "-o", os.path.join(self.fab, f"{self.name}-ipc356.ipc"), self.p.pcb)
        kicad.cli("pcb", "export", "stats", "--format", "json", "-o", os.path.join(self.docs, f"{self.name}-board-stats.json"),
                  self.p.pcb)
        self.say("STEP, IPC-D-356, board statistics" + ("" if ok else " (STEP export failed)"))
        return f if ok else None

    # ------------------------------------------------------------------ release
    def release(self):
        rev = self.p.setting("revision") or _title_rev(self.p.pcb) or "A"
        day = datetime.date.today().isoformat()
        rel = self.p.out("release")
        z = os.path.join(rel, f"{self.name}-rev{rev}-{day}.zip")
        files = []
        for d in (self.fab, self.docs, self.img):
            for root, dirs, fs in os.walk(d):
                dirs[:] = [x for x in dirs if x not in ("gerbers", "_pages")]
                for f in fs:
                    files.append(os.path.join(root, f))
        for extra in ("checks_report.md", "checks.json", "readiness.md"):
            f = os.path.join(self.p.build, extra)
            if os.path.exists(f):
                files.append(f)
        for f in (self.p.pro, self.p.sch, self.p.pcb):
            if f and os.path.exists(f):
                files.append(f)
        files += [f for f in self.p.sheets() if f not in files]
        manifest = []
        with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(set(files)):
                arc = os.path.relpath(f, self.p.root)
                zf.write(f, arc)
                manifest.append(f"{hashlib.sha256(open(f, 'rb').read()).hexdigest()[:16]}  {arc}")
            zf.writestr("MANIFEST.txt", f"{self.name} rev {rev}, {day}\n\n" + "\n".join(manifest) + "\n")
        self.say(f"release: {len(manifest)} files -> {os.path.relpath(z, self.p.root)}")
        return z

    def all(self, renders=True):
        nl = self.netlist()
        parts = self.parts(nl)
        self.bom(parts)
        self.cpl(parts)
        self.gerbers()
        self.pdfs()
        self.step()
        if renders:
            self.renders()
        return self.release()


class _CfgOnly:
    """Just enough of a check Context for lcsc_of()."""
    def __init__(self, p):
        self.p = p

    def setting(self, k, d=None):
        return self.p.setting(k, d)


def _title_rev(pcb):
    try:
        with open(pcb, encoding="utf-8") as f:
            head = f.read(4000)
        m = re.search(r'\(rev "([^"]*)"\)', head)
        return m.group(1) if m else None
    except OSError:
        return None


def _merge_pdfs(files, out):
    """Concatenate PDFs with whatever is available (pypdf, pymupdf, macOS 'join' tool); False if none."""
    try:
        from pypdf import PdfWriter
        w = PdfWriter()
        for f in files:
            w.append(f)
        with open(out, "wb") as fh:
            w.write(fh)
        return True
    except ImportError:
        pass
    try:
        import pymupdf
        doc = pymupdf.open()
        for f in files:
            doc.insert_pdf(pymupdf.open(f))
        doc.save(out)
        return True
    except ImportError:
        pass
    join = "/System/Library/Automator/Combine PDF Pages.action/Contents/MacOS/join"
    if os.path.exists(join):
        r = subprocess.run([join, "-o", out] + files, capture_output=True)
        return r.returncode == 0 and os.path.exists(out)
    return False
