"""The project's data sheet library, docs/datasheets: each key part's data sheet as a PDF, and the pin tables
read from them (<name>.pins.json). A saved pin table is the part's pinout as its maker gives it, so the pinout
check (sch.pinout) trusts it over the parts library's data; the part card shows both.

  <name>.pdf        the data sheet (a part's MPN, or its LCSC code when it has no MPN)
  <name>.pins.json  {"mpn", "lcsc", "source": "table 3, page 4", "pins": {"1": "GND", "2": "VOUT", ...}, "saved"}
"""
import datetime, json, os, re, urllib.request

DIR = os.path.join("docs", "datasheets")
MAX_PDF = 40 * 1024 * 1024


def key_for(mpn="", lcsc="", value=""):
    """The file name a part's data sheet and pin table go under."""
    k = (mpn or lcsc or value or "").strip()
    return re.sub(r"[^A-Za-z0-9._+-]+", "_", k).strip("._")[:80]


def folder(project):
    return os.path.join(project.root, DIR)


def _pins_files(project):
    d = folder(project)
    try:
        return [os.path.join(d, f) for f in sorted(os.listdir(d)) if f.endswith(".pins.json")]
    except OSError:
        return []


def library(project):
    """Every data sheet and pin table in the project: [{name, pdf, pins, source, mpn, lcsc, size}]."""
    d = folder(project)
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return []
    out = {}
    for f in names:
        full = os.path.join(d, f)
        if f.lower().endswith(".pdf"):
            k = f[:-4]
            out.setdefault(k, {"name": k})["pdf"] = os.path.relpath(full, project.root)
            out[k]["size"] = os.path.getsize(full)
        elif f.endswith(".pins.json"):
            k = f[:-len(".pins.json")]
            try:
                with open(full) as fh:
                    t = json.load(fh)
            except (OSError, ValueError):
                continue
            out.setdefault(k, {"name": k}).update({"pins": len(t.get("pins") or {}), "source": t.get("source", ""),
                                                   "mpn": t.get("mpn", ""), "lcsc": t.get("lcsc", ""),
                                                   "pins_file": os.path.relpath(full, project.root)})
    return list(out.values())


def pdf_for(project, mpn="", lcsc="", value=""):
    """The saved data sheet for a part (relative path), or None."""
    for k in dict.fromkeys(k for k in (key_for(mpn=mpn), key_for(lcsc=lcsc), key_for(value=value)) if k):   # MPN, LCSC code, value
        f = os.path.join(folder(project), k + ".pdf")
        if os.path.exists(f):
            return os.path.relpath(f, project.root)
    return None


def pins_for(project, lcsc="", mpn="", value=""):
    """A saved pin table that is this part's: ({pin: name}, source, file) or (None, None, None). Matched by LCSC
    code first, then MPN, then the value."""
    wanted = [("lcsc", (lcsc or "").upper()), ("mpn", (mpn or "").upper()), ("mpn", (value or "").upper())]
    tables = []
    for f in _pins_files(project):
        try:
            with open(f) as fh:
                t = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(t.get("pins"), dict) and t["pins"]:
            tables.append((f, t))
    for field, v in wanted:
        if not v:
            continue
        for f, t in tables:
            if str(t.get(field) or "").upper() == v:
                return {str(k): str(n) for k, n in t["pins"].items()}, t.get("source", ""), os.path.relpath(f, project.root)
    return None, None, None


def save_pins(project, pins, source, mpn="", lcsc=""):
    """Record a part's pin table from its data sheet. pins: {number: name} (or [{n, name}])."""
    if isinstance(pins, list):
        pins = {str(p.get("n") or p.get("pin")): str(p.get("name") or "") for p in pins if isinstance(p, dict)}
    pins = {str(k).strip(): str(v).strip() for k, v in (pins or {}).items() if str(k).strip() and str(v).strip()}
    if len(pins) < 2:
        raise ValueError("a pin table needs at least two pins, each {number: name}")
    if not (source or "").strip():
        raise ValueError("say where the table comes from (the data sheet's table and page)")
    k = key_for(mpn=mpn, lcsc=lcsc)
    if not k:
        raise ValueError("an MPN or an LCSC code names the pin table")
    os.makedirs(folder(project), exist_ok=True)
    f = os.path.join(folder(project), k + ".pins.json")
    with open(f, "w") as fh:
        json.dump({"mpn": mpn, "lcsc": (lcsc or "").upper(), "source": source.strip(), "pins": pins,
                   "saved": datetime.datetime.now().isoformat(timespec="seconds")}, fh, indent=1)
    return os.path.relpath(f, project.root)


def fetch(project, url, mpn="", lcsc="", _depth=0):
    """Download a data sheet into the library (a PDF, at most 40 MB); returns its relative path."""
    if url.startswith("//"):
        url = "https:" + url
    if not re.match(r"^https?://", url or ""):
        raise ValueError("no link to the data sheet")
    k = key_for(mpn=mpn, lcsc=lcsc)
    if not k:
        raise ValueError("an MPN or an LCSC code names the data sheet")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Tracewright)", "Accept": "application/pdf,*/*"})
    with urllib.request.urlopen(req, timeout=40) as r:
        data = r.read(MAX_PDF + 1)
    if len(data) > MAX_PDF:
        raise ValueError("the data sheet is larger than 40 MB")
    if not data.startswith(b"%PDF"):
        # LCSC's links sometimes open a viewer page: find the PDF it points at
        m = re.search(rb'(https?://[^"\'\s>]+\.pdf)', data)
        if not m or _depth >= 2:
            raise ValueError("the link did not give a PDF")
        return fetch(project, m.group(1).decode(), mpn=mpn, lcsc=lcsc, _depth=_depth + 1)
    os.makedirs(folder(project), exist_ok=True)
    f = os.path.join(folder(project), k + ".pdf")
    with open(f + ".tmp", "wb") as fh:
        fh.write(data)
    os.replace(f + ".tmp", f)
    return os.path.relpath(f, project.root)
