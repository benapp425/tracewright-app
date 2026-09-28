"""The `tracewright` command.

  tracewright                    start the app (http://127.0.0.1:8764) and open the browser
  tracewright serve [--port N] [--no-browser]
  tracewright new "Name" [--brief "..."] [--layers 2|4]
  tracewright import PATH [--in-place]
  tracewright list
  tracewright doctor             check KiCad, Claude, git, Java, Freerouting
  tracewright account list       the accounts on this machine
  tracewright account reset EMAIL   a new password for an account (it signs out its sessions)
"""
import argparse, json, os, sys, shutil, subprocess


def doctor():
    from tw import env
    from . import config, history
    ok = True
    k = env.kicad()
    rows = []
    rows.append(("KiCad", bool(k["cli"]), f"{k['version']} ({k['cli']})" if k["cli"] else
                 "not found -- install KiCad 9 or 10 (kicad.org), or set kicad_cli in Settings"))
    rows.append(("KiCad Python (pcbnew)", bool(k["python"]), k["python"] or "not found -- board editing needs it"))
    rows.append(("KiCad symbol/footprint libraries", bool(k["share"]), k["share"] or "not found"))
    try:
        import kipy  # noqa: F401
        from tw import live
        on = live.api_enabled()
        rows.append(("KiCad live link (kicad-python)", True,
                     "installed; KiCad's API is on" if on else
                     "installed; KiCad's API is off -- turn it on in Settings or in KiCad: Preferences > Plugins"))
    except ImportError:
        rows.append(("KiCad live link (kicad-python)", False, "pip install kicad-python"))
    try:
        import claude_agent_sdk
        rows.append(("Claude Agent SDK", True, claude_agent_sdk.__version__))
    except ImportError:
        rows.append(("Claude Agent SDK", False, "pip install claude-agent-sdk"))
    s = config.settings()
    auth = "API key in Settings" if s.get("anthropic_api_key") else (
        "Claude Code login (claude)" if shutil.which("claude") else "none: add an API key in Settings or install Claude Code")
    rows.append(("Claude sign-in", bool(s.get("anthropic_api_key") or shutil.which("claude")), auth))
    rows.append(("git (project history)", history.available(), shutil.which("git") or "not found"))
    rows.append(("Java (Freerouting)", bool(shutil.which("java")), shutil.which("java") or "optional"))
    fr = os.environ.get("TW_FREEROUTING")
    rows.append(("Freerouting", bool(fr), fr or "optional: put freerouting-2.x.jar in " + os.path.join(config.data_dir(), "vendor")))
    bad = config.native_problems()
    rows.append(("Compiled packages", not bad, "; ".join(bad) or "ok (Pillow, numpy, kicad-python)"))
    for name, good, detail in rows:
        print(f"  {'ok ' if good else '-- '} {name:<34} {detail}")
        if not good and name in ("KiCad", "Claude Agent SDK", "KiCad Python (pcbnew)", "Compiled packages"):
            ok = False
    print(f"\n  data folder: {config.data_dir()}\n  projects:    {config.workspace()}")
    return 0 if ok else 1


def account(a):
    from . import accounts
    if a.action == "list":
        us = accounts.users()
        for u in us:
            how = " + ".join(x for x in ("password" if u["has_password"] else "", "Google" if u["google"] else "") if x)
            print(f"  {u['email']:<36} {u['role']:<7} {how}")
        if not us:
            print("  No accounts yet: the first one is made in the app.")
        return 0
    if not a.email:
        print("Which account? tracewright account reset EMAIL")
        return 1
    if a.stdin:
        pw = sys.stdin.readline().rstrip("\n")
    else:
        import getpass
        pw = getpass.getpass(f"New password for {a.email}: ")
        if getpass.getpass("Again: ") != pw:
            print("The two did not match.")
            return 1
    try:
        accounts.reset_password(a.email, pw)
    except accounts.AccountError as e:
        print(str(e)[:1].upper() + str(e)[1:] + ".")
        return 1
    print(f"Done: sign in as {a.email} with the new password (its other sessions were signed out).")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tracewright", description="Tracewright: an AI pair designer for KiCad PCBs")
    sub = ap.add_subparsers(dest="cmd")
    sv = sub.add_parser("serve")
    sv.add_argument("--port", type=int)
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--no-browser", action="store_true")
    sv.add_argument("--server", action="store_true", help="server mode: no local KiCad window or file dialogs")
    sv.add_argument("--app", action="store_true", help="started by the Mac app (no browser; any free port)")
    sub.add_parser("password", help="set the password needed when Tracewright is reached from other machines")
    nw = sub.add_parser("new")
    nw.add_argument("name")
    nw.add_argument("--brief", default="")
    nw.add_argument("--layers", type=int, default=2)
    im = sub.add_parser("import")
    im.add_argument("path")
    im.add_argument("--in-place", action="store_true")
    im.add_argument("--name")
    sub.add_parser("list")
    sub.add_parser("doctor")
    ac = sub.add_parser("account", help="the accounts on this machine: list them, or reset a password")
    ac.add_argument("action", choices=["list", "reset"])
    ac.add_argument("email", nargs="?")
    ac.add_argument("--stdin", action="store_true", help="read the new password from standard input (the Mac app)")
    a = ap.parse_args(argv)
    if a.cmd == "account":
        return account(a)
    if a.cmd == "password":
        import getpass
        from . import auth, config
        pw = getpass.getpass("New Tracewright password: ")
        if len(pw) < 10:
            print("Use at least 10 characters.")
            return 1
        if getpass.getpass("Again: ") != pw:
            print("The two did not match.")
            return 1
        config.settings().update({"server_password_hash": auth.hash_password(pw)})
        print("Saved (as a salted hash).")
        return 0
    if a.cmd in (None, "serve"):
        if getattr(a, "server", False):
            os.environ["TW_SERVER_MODE"] = "1"
        from .server import run
        app_mode = getattr(a, "app", False)
        run(host=getattr(a, "host", "127.0.0.1"), port=getattr(a, "port", None),
            open_browser=not (getattr(a, "no_browser", False) or app_mode), app_mode=app_mode)
        return 0
    if a.cmd == "doctor":
        return doctor()
    from .projects import ProjectStore
    st = ProjectStore()
    if a.cmd == "new":
        p = st.create(a.name, a.brief, {"layers": a.layers})
        print(p.root)
    elif a.cmd == "import":
        from . import github
        p = st.import_git(a.path, a.name) if github.is_git_url(a.path) else \
            st.open_in_place(a.path, a.name) if a.in_place else st.import_copy(a.path, a.name)
        print(p.root)
    elif a.cmd == "list":
        for s in st.list():
            print(f"{s['id']:<32} {s.get('updated', ''):<20} {s.get('root')}")
    return 0
