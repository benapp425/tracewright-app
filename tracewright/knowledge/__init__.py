"""The knowledge base: the design playbook and the lessons learned on real boards.

Seed lessons ship with the app (knowledge/lessons/*.md). The live set is in the data folder
(<data>/knowledge/lessons), so lessons the agent or the user records on one project reach every
project after it. Each project also carries a snapshot in .claude/knowledge/.
"""
import os, re, json, time, datetime, shutil
from .. import config

PKG = os.path.dirname(os.path.abspath(__file__))
SEED = os.path.join(PKG, "lessons")


def _sha(path):
    import hashlib
    with open(path, "rb") as f:
        return hashlib.sha1(f.read()).hexdigest()


def lessons_dir():
    """The live lessons folder. Seed lessons are copied in once; a newer app's version of a seed lesson
    replaces the copy only while nobody has edited it (the marker keeps the hash of what was copied)."""
    d = os.path.join(config.data_dir(), "knowledge", "lessons")
    if not os.path.isdir(d):
        os.makedirs(d)
    marker = os.path.join(config.data_dir(), "knowledge", ".seeded")
    seeded = {}
    if os.path.exists(marker):
        try:
            with open(marker) as f:
                m = json.load(f)
        except (OSError, ValueError):
            m = {}
        if isinstance(m, list):                      # older marker (names only): a file not touched since
            mt = os.path.getmtime(marker) + 1        # the marker was written is taken as the unedited seed
            m = {n: (_sha(os.path.join(d, n)) if os.path.exists(os.path.join(d, n)) and
                     os.path.getmtime(os.path.join(d, n)) <= mt else None) for n in m}
        seeded = m
    changed = False
    for f in sorted(os.listdir(SEED)):
        if not f.endswith(".md"):
            continue
        src, dst = os.path.join(SEED, f), os.path.join(d, f)
        new = _sha(src)
        if f not in seeded:
            if not os.path.exists(dst):
                shutil.copy(src, dst)
            seeded[f] = _sha(dst)
            changed = True
        elif os.path.exists(dst) and seeded[f] == _sha(dst) and new != seeded[f]:
            shutil.copy(src, dst)                    # unedited copy of an older seed: update it
            seeded[f] = new
            changed = True
    if changed:
        with open(marker, "w") as fh:
            json.dump(seeded, fh, indent=1, sort_keys=True)
    return d


def _parse(raw, fname):
    meta, body = {}, raw
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", raw, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                v = v.strip()
                if v.startswith("[") and v.endswith("]"):
                    v = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
                meta[k.strip()] = v
        body = m.group(2)
    return {"id": os.path.splitext(fname)[0], "file": fname, "title": meta.get("title", fname),
            "tags": meta.get("tags", []) if isinstance(meta.get("tags"), list) else [meta.get("tags", "")],
            "source": meta.get("source", ""), "scope": meta.get("scope", "global"), "body": body.strip(), "raw": raw}


def all_lessons():
    d = lessons_dir()
    out = []
    for f in sorted(os.listdir(d)):
        if f.endswith(".md"):
            with open(os.path.join(d, f), encoding="utf-8") as fh:
                out.append(_parse(fh.read(), f))
    return out


def get(lid):
    for l in all_lessons():
        if l["id"] == lid:
            return l
    raise KeyError(lid)


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60] or "lesson"


def add(title, body, tags=(), source="", lid=None):
    """Write (or replace) a lesson; returns it."""
    d = lessons_dir()
    lid = lid or slug(title)
    tags = [t.strip() for t in tags if t and t.strip()]
    raw = (f"---\ntitle: {title.strip()}\ntags: [{', '.join(tags)}]\nsource: {source or 'recorded ' + datetime.date.today().isoformat()}"
           f"\n---\n{body.strip()}\n")
    with open(os.path.join(d, lid + ".md"), "w", encoding="utf-8") as f:
        f.write(raw)
    return _parse(raw, lid + ".md")


def delete(lid):
    p = os.path.join(lessons_dir(), lid + ".md")
    if os.path.exists(p):
        os.remove(p)
        return True
    return False


def search(query, limit=8):
    """Keyword search over titles, tags and bodies (title and tag hits weigh most)."""
    words = [w for w in re.findall(r"[a-z0-9+#.-]{2,}", (query or "").lower())]
    scored = []
    for l in all_lessons():
        t, g, b = l["title"].lower(), " ".join(l["tags"]).lower(), l["body"].lower()
        s = sum(5 * t.count(w) + 4 * g.count(w) + b.count(w) for w in words)
        if s or not words:
            scored.append((s, l))
    scored.sort(key=lambda x: -x[0])
    return [l for _, l in scored[:limit]]


def playbook_text():
    with open(os.path.join(PKG, "playbook.md"), encoding="utf-8") as f:
        return f.read()


def index_text():
    lines = ["# Lessons learned", "", "Recorded on real boards; each file in this folder is one lesson. "
             "Read the ones that touch what you are doing.", ""]
    for l in all_lessons():
        lines.append(f"- [{l['title']}]({l['file']}) -- tags: {', '.join(l['tags'])}")
    return "\n".join(lines) + "\n"
