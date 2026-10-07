"""Is there a newer Tracewright? Once a day the app asks GitHub for the latest release of the repository
it was built from (REPO_URL in tracewright/__init__.py; nothing is sent but the request itself), and
Settings, About shows it with a link to the download. Settings: update_check turns it off."""
import os, re, json, time, urllib.request
from . import __version__, REPO_URL, config


def _parse(v):
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", v or "")
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def newer(a, b):
    """Is version a newer than version b?"""
    return _parse(a) > _parse(b)


def _cache():
    return os.path.join(config.data_dir(), "update.json")


def check(force=False):
    repo = REPO_URL.rstrip("/")
    m = re.match(r"https://github\.com/([^/]+)/([^/]+)$", repo)
    if not m:
        return {"current": __version__, "configured": False}
    try:
        with open(_cache()) as f:
            c = json.load(f)
        if not force and time.time() - c.get("t", 0) < 86400:
            return {**c["r"], "current": __version__, "available": newer(c["r"].get("latest"), __version__)}
    except (OSError, ValueError, KeyError):
        pass
    req = urllib.request.Request(f"https://api.github.com/repos/{m.group(1)}/{m.group(2)}/releases/latest",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": f"Tracewright/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.load(r)
    except Exception as e:
        return {"current": __version__, "configured": True, "error": f"{type(e).__name__}"}
    notes = d.get("body") or ""
    if len(notes) > 12000:  # long notes end at a line, not mid-word
        cut = notes.rfind("\n", 0, 12000)
        notes = notes[:cut if cut > 0 else 12000]
    res = {"configured": True, "latest": (d.get("tag_name") or "").lstrip("v"), "url": d.get("html_url"),
           "published": d.get("published_at"), "notes": notes}
    with open(_cache(), "w") as f:
        json.dump({"t": time.time(), "r": res}, f)
    return {**res, "current": __version__, "available": newer(res["latest"], __version__)}
