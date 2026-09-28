"""Bring-up at the bench: docs/bring-up.md (the plan Claude writes: sections of "- [ ]" steps with the
values to expect) as a checklist you work through with the board in front of you. Each step can be
ticked and, where the plan names a value ("V3V3 = 3.30 V ± 2 %", "current < 150 mA", "> 1 kΩ",
"≈ 4.5 V"), the reading you measured is kept and judged against it. Results are kept per board (the
serial you give each one) in .tracewright/bringup.json, never in the plan itself."""
import os, re, json, time, hashlib

UNITS = {"V": "V", "mV": "V", "A": "A", "mA": "A", "µA": "A", "uA": "A", "Ω": "Ω", "kΩ": "Ω", "MΩ": "Ω", "ohm": "Ω",
         "°C": "°C", "Hz": "Hz", "kHz": "Hz", "MHz": "Hz", "W": "W", "mW": "W", "ms": "s", "s": "s", "%": "%"}
SCALE = {"mV": 1e-3, "mA": 1e-3, "µA": 1e-6, "uA": 1e-6, "kΩ": 1e3, "MΩ": 1e6, "kHz": 1e3, "MHz": 1e6, "mW": 1e-3, "ms": 1e-3}
# what a typed reading may carry: a unit (148 mA), a bare prefix in the step's unit (4.7k), or nothing (the
# unit the plan wrote). web/js/bringup.js reads them the same way.
PREFIX = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "k": 1e3, "M": 1e6, "G": 1e9}
BASES = ("V", "A", "Ω", "Hz", "W", "s", "F", "°C", "%")
NUM = r"(\d+(?:\.\d+)?)\s*(mV|V|mA|µA|uA|A|kΩ|MΩ|Ω|°C|kHz|MHz|Hz|mW|W|ms|s)"
EXPECT = [
    ("range", re.compile(r"=\s*" + NUM + r"\s*±\s*(\d+(?:\.\d+)?)\s*(%|mV|V|mA|A)?")),
    ("max", re.compile(r"<\s*" + NUM)),
    ("min", re.compile(r">\s*" + NUM)),
    ("approx", re.compile(r"(?:≈|~|\babout\s)\s*" + NUM)),
    ("equal", re.compile(r"(?<![<>≈±])=\s*" + NUM + r"(?!\s*±)")),
]


def _si(v, unit):
    return float(v) * SCALE.get(unit, 1.0)


def expectations(text):
    """What a step expects to read: [{kind: range|max|min|approx|equal, lo, hi, unit, label}]"""
    out, seen = [], set()
    for kind, rx in EXPECT:
        for m in rx.finditer(text):
            if m.start() in seen:
                continue
            seen.add(m.start())
            unit = m.group(2)
            base = UNITS.get(unit, unit)
            v = _si(m.group(1), unit)
            if kind == "range":
                tol = float(m.group(3))
                tu = m.group(4) or "%"
                d = v * tol / 100 if tu == "%" else _si(tol, tu)
                lo, hi = v - d, v + d
            elif kind == "max":
                lo, hi = None, v
            elif kind == "min":
                lo, hi = v, None
            elif kind == "approx":
                lo, hi = v * 0.9, v * 1.1
            else:
                lo, hi = v * 0.97, v * 1.03
            out.append({"kind": kind, "lo": lo, "hi": hi, "unit": base, "shown": unit, "label": m.group(0).strip(), "at": m.start()})
    out.sort(key=lambda e: e["at"])
    return out[:3]


def parse(md):
    """[{title, items: [{key, text, expects}]}] from the plan's '## ' sections and its steps: '- [ ]' items,
    or top-level numbered ones ('1. ...'); a step's indented continuation lines belong to it. The first
    section collects steps before any heading."""
    sections, cur, item = [], None, None
    for line in md.splitlines():
        if line.startswith("## "):
            cur = {"title": line[3:].strip(), "items": []}
            sections.append(cur)
            item = None
            continue
        m = re.match(r"^\s*[-*]\s+\[([ xX])\]\s+(.*)$", line) or re.match(r"^ ?\d{1,3}[.)]()\s+(.*)$", line)
        if m:
            if cur is None:
                cur = {"title": "Steps", "items": []}
                sections.append(cur)
            item = {"text": m.group(2).strip(), "was": (m.group(1) or "").lower() == "x"}
            cur["items"].append(item)
            continue
        if item is not None and line.startswith((" ", "\t")) and line.strip():
            item["text"] += " " + line.strip()
        else:
            item = None
    for s in sections:
        for it in s["items"]:
            it["key"] = hashlib.sha1(re.sub(r"\s+", " ", it["text"]).encode()).hexdigest()[:12]
            it["expects"] = expectations(it["text"])
    return [s for s in sections if s["items"]]


def _unit(u):
    """'mV' -> ('V', 1e-3), 'Ω' -> ('Ω', 1), a bare prefix 'k' -> (None, 1e3); None when it is not a unit."""
    u = re.sub(r"(?i)ohms?$", "Ω", u)
    if u in BASES:
        return u, 1.0
    if len(u) > 1 and u[0] in PREFIX and u[1:] in BASES:
        return u[1:], PREFIX[u[0]]
    if u in PREFIX:
        return None, PREFIX[u]
    return None


def reading(text, unit, shown=None):
    """A typed reading in the step's base unit: "3.29 V", "148mA", "4.7k" (in the step's unit) or a bare
    "148" (in the unit the plan wrote, `shown`). None when it does not parse or is in another unit."""
    m = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*([a-zA-Zµ°Ω%]*)\s*$", text or "")
    if not m:
        return None
    x, u = float(m.group(1)), m.group(2)
    if not u:
        return x * ((_unit(shown) or (None, 1.0))[1] if shown else 1.0)
    got = _unit(u)
    if got is None or (got[0] is not None and got[0] != unit):
        return None
    return x * got[1]


def judge(expects, text):
    """'pass' | 'fail' | None (no expectation, or a reading that does not parse)."""
    if not expects or not (text or "").strip():
        return None
    e = expects[0]
    v = reading(text, e["unit"], e.get("shown"))
    if v is None:
        return None
    ok = (e["lo"] is None or v >= e["lo"] - 1e-12) and (e["hi"] is None or v <= e["hi"] + 1e-12)
    return "pass" if ok else "fail"


# ---------------------------------------------------------------------- results
def _state_path(root):
    return os.path.join(root, ".tracewright", "bringup.json")


def load(root):
    try:
        with open(_state_path(root)) as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    d.setdefault("boards", {"1": {}})
    d.setdefault("current", next(iter(d["boards"])))
    return d


def save(root, d):
    os.makedirs(os.path.dirname(_state_path(root)), exist_ok=True)
    tmp = _state_path(root) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, _state_path(root))


def state(root):
    path = os.path.join(root, "docs", "bring-up.md")
    if not os.path.exists(path):
        return {"exists": False}
    sections = parse(open(path, encoding="utf-8").read())
    d = load(root)
    board = d["boards"].get(d["current"], {})
    total = sum(len(s["items"]) for s in sections)
    done = sum(1 for s in sections for it in s["items"] if (board.get(it["key"]) or {}).get("done"))
    fails = sum(1 for s in sections for it in s["items"] if judge(it["expects"], (board.get(it["key"]) or {}).get("value")) == "fail")
    return {"exists": True, "path": "docs/bring-up.md", "sections": sections, "boards": sorted(d["boards"], key=lambda s: (len(s), s)),
            "current": d["current"], "results": board, "total": total, "done": done, "fails": fails}


def record(root, board, key, done=None, value=None, note=None):
    d = load(root)
    b = d["boards"].setdefault(str(board), {})
    r = b.setdefault(key, {})
    if done is not None:
        r["done"] = bool(done)
    if value is not None:
        r["value"] = str(value)[:40]
    if note is not None:
        r["note"] = str(note)[:300]
    r["t"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    d["current"] = str(board)
    save(root, d)
    return r


def add_board(root, serial):
    d = load(root)
    serial = str(serial).strip()[:20] or str(len(d["boards"]) + 1)
    d["boards"].setdefault(serial, {})
    d["current"] = serial
    save(root, d)
    return serial


def report(root):
    """The results as markdown, board by board (for the docs or to share)."""
    st = state(root)
    if not st.get("exists"):
        return ""
    d = load(root)
    lines = ["# Bring-up results", ""]
    for serial in st["boards"]:
        b = d["boards"].get(serial, {})
        n = sum(1 for s in st["sections"] for it in s["items"] if (b.get(it["key"]) or {}).get("done"))
        lines += [f"## Board {serial}: {n} of {st['total']} steps", "", "| Step | Done | Reading | Verdict | Note |", "|---|---|---|---|---|"]
        for s in st["sections"]:
            for it in s["items"]:
                r = b.get(it["key"]) or {}
                if not r:
                    continue
                v = judge(it["expects"], r.get("value"))
                lines.append(f"| {it['text'][:70]} | {'yes' if r.get('done') else ''} | {r.get('value', '')} | {v or ''} | {r.get('note', '')} |")
        lines.append("")
    return "\n".join(lines)
