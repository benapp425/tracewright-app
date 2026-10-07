"""The setup, checked: what the app needs from this machine (KiCad and its Python, its libraries, Claude, the compiled
packages, a projects folder it can write) and what makes it better (ngspice for simulations, git for history, Java for
Freerouting), each with how to fix it. The projects page shows what is missing; Settings > About lists it all;
`tracewright doctor` prints it.

    checks() -> [{"id", "title", "ok", "required", "detail", "fix"}]
"""
import os, shutil


def checks(settings=None):
    from tw import env, sim
    from . import config, history
    s = settings if settings is not None else config.settings()
    k = env.kicad()
    out = []

    def add(id_, title, ok, required, detail, fix=""):
        out.append({"id": id_, "title": title, "ok": bool(ok), "required": required, "detail": detail, "fix": "" if ok else fix})
    ver = str(k.get("version") or "")
    major = int(ver.split(".")[0]) if ver[:1].isdigit() else 0
    add("kicad", "KiCad", k.get("cli") and major >= 9, True, f"{ver} ({k['cli']})" if k.get("cli") else "not found",
        "Install KiCad 9 or 10 from kicad.org, or set where it is in Settings > KiCad and tools." if not k.get("cli") else
        f"KiCad {ver} is too old: install KiCad 9 or 10.")
    add("pcbnew", "KiCad's Python (board editing)", k.get("python"), True, k.get("python") or "not found",
        "It comes with KiCad: reinstall KiCad, or set its Python in Settings > KiCad and tools.")
    add("libraries", "KiCad's symbol and footprint libraries", k.get("share"), True, k.get("share") or "not found",
        "Install KiCad's libraries (they come with the standard KiCad installer).")
    lib = sim.library()
    add("ngspice", "ngspice (simulations)", lib, False, lib or "not found", "It comes with KiCad 8 and later; reinstall KiCad to get it.")
    cli = shutil.which("claude")
    signed = bool(s.get("anthropic_api_key")) or bool(cli) or bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY"))
    add("claude", "Claude", signed, True, "an API key in Settings" if s.get("anthropic_api_key") else (f"Claude Code ({cli})" if cli else "not connected"),
        "Sign in with Claude Code (run claude, then /login), or add an API key in Settings > Claude.")
    bad = config.native_problems()
    add("packages", "Compiled packages", not bad, True, "; ".join(bad) or "Pillow, numpy, kicad-python load",
        "Reinstall the app (the installer puts them back).")
    ws = config.workspace()
    writable = os.path.isdir(ws) and os.access(ws, os.W_OK)
    add("workspace", "Projects folder", writable, True, ws, "Choose a folder you can write to in Settings > General.")
    try:
        free = shutil.disk_usage(ws if os.path.isdir(ws) else os.path.expanduser("~")).free / 1e9
    except OSError:
        free = None
    add("disk", "Free disk space", free is None or free >= 2.0, False, f"{free:.1f} GB free" if free is not None else "unknown",
        "Less than 2 GB free: renders, 3D models and releases need room.")
    icloud = any(p in os.path.realpath(ws) for p in ("/Mobile Documents/", "/Library/CloudStorage/")) or \
        os.path.realpath(ws).startswith(tuple(os.path.expanduser(p) for p in ("~/Desktop", "~/Documents")))
    add("icloud", "Projects folder kept on this Mac", not icloud, False, "in a folder iCloud may sync" if icloud else "a local folder",
        "iCloud can offload files that are not used for a while; projects there may open empty until they download. "
        "Keep them downloaded (Finder: Keep Downloaded) or choose a folder outside Desktop and Documents.")
    add("git", "git (project history)", history.available(), False, shutil.which("git") or "not found",
        "Install the Xcode command line tools (xcode-select --install) for project history.")
    add("java", "Java (Freerouting)", shutil.which("java"), False, shutil.which("java") or "not found", "Optional: Freerouting needs Java 17 or later.")
    return out
