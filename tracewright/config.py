"""App settings and folders.

Data folder (settings, knowledge base, logs, vendor tools):
  macOS    ~/Library/Application Support/Tracewright
  Linux    $XDG_DATA_HOME/tracewright (~/.local/share/tracewright)
  Windows  %APPDATA%/Tracewright
Projects live in the workspace folder (default ~/Documents/Tracewright Projects), one folder each;
projects opened in place elsewhere are remembered in the data folder's registry.
"""
import os, sys, json, threading

APP = "Tracewright"


def data_dir():
    if os.environ.get("TRACEWRIGHT_HOME"):
        d = os.environ["TRACEWRIGHT_HOME"]
    elif sys.platform == "darwin":
        d = os.path.expanduser("~/Library/Application Support/Tracewright")
    elif os.name == "nt":
        d = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Tracewright")
    else:
        d = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "tracewright")
    os.makedirs(d, exist_ok=True)
    return d


def cache_dir(*parts):
    """Scratch the app can always make again (board edit undo copies): outside the projects, so a project folder in
    iCloud never syncs it. macOS ~/Library/Caches/Tracewright."""
    if os.environ.get("TRACEWRIGHT_CACHE"):
        d = os.environ["TRACEWRIGHT_CACHE"]
    elif sys.platform == "darwin":
        d = os.path.expanduser("~/Library/Caches/Tracewright")
    elif os.name == "nt":
        d = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Tracewright", "Cache")
    else:
        d = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "tracewright")
    d = os.path.join(d, *parts)
    os.makedirs(d, exist_ok=True)
    return d


DEFAULTS = {
    "workspace": os.path.expanduser("~/Documents/Tracewright Projects"),
    "model": "claude-opus-5-5",
    "fast_model": "claude-sonnet-5",
    "effort": "high",
    "permission_mode": "acceptEdits",      # acceptEdits | default | bypassPermissions | plan
    "auto_allow_bash": True,               # toolkit / KiCad / read-only commands run without asking
    "allow_web": True,                     # datasheet and part lookups
    "anthropic_api_key": "",               # optional: otherwise Claude Code's own login is used
    "github_token": "",                    # optional: push projects to GitHub (fine-grained token)
    "server_password_hash": "",            # set with `tracewright password` / Settings: sign-in beyond 127.0.0.1
    "server_mode": False,                  # no local KiCad window or file dialogs (TW_SERVER_MODE=1 does the same)
    "port": 8764,
    "open_browser": True,
    "kicad_cli": "",                       # overrides for unusual installs
    "kicad_python": "",
    "freerouting_jar": "",
    "live_pace_s": 0.12,                   # delay between parts when placing live, so it is visible
    "snapshot_each_turn": True,            # git checkpoint before every agent turn
    "theme": "dark",
    "fab_house": "jlcpcb",
    "accounts_required": True,             # sign in (email + password, or Google) to use the app
    "allow_signups": None,                 # None: open on this Mac, owner-only on a server
    "google_client_id": "",                # Google sign-in: an OAuth client (Desktop app, or Web on a server)
    "google_client_secret": "",
    "update_check": True,                  # look for a newer release on GitHub once a day
    "stock_watch": True,                   # once a day, when a project opens: ask again about its parts' stock
    "approve_parts": True,                 # what goes on the list for the user's OK after a run (approvals.py)
    "approve_floorplan": True,
    "approve_handmade": True,
    "approve_rules": True,
    "approve_limits": True,
    "approve_signed": True,
    "pause_at_pct": 90,                    # pause Claude's runs at this share of the plan's usage limit (0: never)
    "lookup_model": "off",                 # the librarian subagent's model for look-ups: sonnet | haiku | off (its first use asks)
}

_lock = threading.Lock()


class Settings:
    def __init__(self):
        self.path = os.path.join(data_dir(), "settings.json")
        self.data = dict(DEFAULTS)
        try:
            with open(self.path) as f:
                self.data.update(json.load(f))
        except (OSError, ValueError):
            pass
        self.apply_env()

    def get(self, k, default=None):
        return self.data.get(k, DEFAULTS.get(k, default))

    def update(self, changes):
        with _lock:
            for k, v in changes.items():
                if k in DEFAULTS:
                    self.data[k] = v
            tmp = self.path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)     # holds keys: owner-only from the start
            with os.fdopen(fd, "w") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        self.apply_env()

    def apply_env(self):
        """KiCad / Freerouting overrides reach the toolkit through its environment variables."""
        for key, var in (("kicad_cli", "TW_KICAD_CLI"), ("kicad_python", "TW_KICAD_PYTHON"),
                         ("freerouting_jar", "TW_FREEROUTING")):
            v = self.data.get(key)
            if v:
                os.environ[var] = v
        if not os.environ.get("TW_FREEROUTING"):
            vend = os.path.join(data_dir(), "vendor")
            if os.path.isdir(vend):
                jars = sorted(f for f in os.listdir(vend) if f.startswith("freerouting") and f.endswith(".jar"))
                if jars:
                    os.environ["TW_FREEROUTING"] = os.path.join(vend, jars[-1])

    def public(self):
        d = dict(self.data)
        for key in ("anthropic_api_key", "github_token", "google_client_secret"):   # secrets never leave the server
            k = d.get(key) or ""
            d[key] = ("set, ends " + k[-4:]) if k else ""
        d["server_password"] = "set, ends ****" if d.pop("server_password_hash", "") else ""
        return d


_settings = None


def settings():
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def workspace():
    w = os.path.expanduser(os.environ.get("TW_WORKSPACE") or settings().get("workspace"))
    os.makedirs(w, exist_ok=True)
    return w


_NATIVE = None


def native_problems():
    """Compiled packages that fail to load here -- typically installed for the other CPU architecture
    (an Apple-silicon Mac starting this Python under Rosetta). Checked once per process."""
    global _NATIVE
    if _NATIVE is None:
        import importlib
        out = []
        for mod, what in (("PIL.Image", "Pillow (board images)"), ("numpy", "numpy (the router)"),
                          ("kipy", "kicad-python (the live KiCad link)")):
            try:
                importlib.import_module(mod)
            except ImportError as e:
                msg = str(e)
                if mod == "kipy" and isinstance(e, ModuleNotFoundError):
                    continue                                   # optional
                if "architecture" in msg or "mach-o" in msg.lower():
                    msg = ("it was installed for another CPU architecture than this process runs as. Start Tracewright "
                           "from Tracewright.app or the `tracewright` command, or re-run ./install.sh")
                out.append(f"{what} cannot load: {msg}")
        _NATIVE = out
    return _NATIVE
