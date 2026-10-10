"""The local web app (aiohttp): REST API + WebSocket events + the static UI."""
import os, re, sys, json, time, asyncio, mimetypes, subprocess, traceback, logging, shutil, glob
from aiohttp import web, WSMsgType

from . import __version__, REPO_URL, config, history, knowledge, scaffold, auth, accounts, overview, usage
from .projects import ProjectStore, STAGES, RUN_MODES
from .bus import Hubs
from .runtime import ProjectRuntime
from tw import env as twenv, live as twlive

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
TEXT_EXT = {".md", ".txt", ".py", ".json", ".kicad_pro", ".kicad_sch", ".kicad_pcb", ".kicad_dru", ".kicad_sym", ".kicad_mod",
            ".csv", ".net", ".rpt", ".log", ".sh", ".cmd", ".toml", ".yaml", ".yml", ".gitignore", ".ipc", ".drl", ".gbr"}
HIDDEN = {".git", "__pycache__", "node_modules", ".DS_Store"}


class App:
    def __init__(self):
        self.settings = config.settings()
        self.store = ProjectStore()
        self.hubs = Hubs()
        self.runtimes = {}
        self.agents = {}
        self.jobs = {}
        # on a server: no local KiCad window, no Finder, no file dialogs -- uploads, downloads and GitHub instead
        self.server_mode = os.environ.get("TW_SERVER_MODE") == "1" or bool(self.settings.get("server_mode"))
        self.require_auth = False              # set by run(): a password is needed beyond 127.0.0.1
        self.local_key = None                  # set by run() on this machine: only Tracewright's own window may use it
        self.port = None
        self.tickets = {}                      # one-time sign-ins for a browser window: ticket -> expiry
        self.logfile = os.path.join(config.data_dir(), "tracewright.log")

    # ------------------------------------------------------------------ the window key (this machine)
    def local_hosts(self):
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}"}

    def local_origins(self):
        return {f"http://{h}" for h in self.local_hosts()}

    def window_ok(self, request):
        """The request comes from a Tracewright window: its cookie, or the launch key itself (the
        Mac app's first request, the `tracewright` command)."""
        k = self.local_key
        return bool(k) and (auth.same(request.cookies.get(auth.LOCAL_COOKIE), auth.window_cookie(k))
                            or auth.same(request.headers.get(auth.KEY_HEADER), k))

    def shared(self):
        """Can other people reach this Tracewright (server mode, or listening beyond 127.0.0.1)? Then new
        accounts are closed unless the owner opens them, and making the first one takes the server's password."""
        return bool(self.server_mode or (self.require_auth and not self.local_key))

    def accounts_on(self):
        """Is an account needed to use the app? (Settings: accounts_required; TW_ACCOUNTS=0 turns it off.)"""
        if os.environ.get("TW_ACCOUNTS") == "0":
            return False
        return bool(self.settings.get("accounts_required", True))

    def user(self, request):
        """The signed-in account (public fields), or None."""
        return accounts.session_user(request.cookies.get(accounts.COOKIE))

    def ticket(self, seconds=120):
        import secrets
        now = time.time()
        self.tickets = {t: e for t, e in self.tickets.items() if e > now}
        t = secrets.token_urlsafe(24)
        self.tickets[t] = now + seconds
        return t

    def log(self, msg):
        try:
            with open(self.logfile, "a") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
        except OSError:
            pass

    # ------------------------------------------------------------------ project runtimes
    def rt(self, pid):
        p = self.store.get(pid)
        r = self.runtimes.get(p.id)
        if r is None or r.p.root != p.root:
            if scaffold.toolkit_outdated(p.root):          # keep ./tw in step with the app (checkpoint first)
                try:
                    scaffold.refresh(p.root, p.cfg)
                    p.save()
                    self.log(f"{p.id}: toolkit updated to match the app")
                except Exception as e:
                    self.log(f"{p.id}: toolkit update failed: {e}")
            try:
                gone = scaffold.clean_conflicts(p.root)      # iCloud's "checks 3" copies of the app's own files
                if gone:
                    self.log(f"{p.id}: removed {len(gone)} iCloud conflict copies: {', '.join(gone[:6])}")
            except Exception as e:
                self.log(f"{p.id}: conflict cleanup failed: {e}")
            r = ProjectRuntime(self, p)
            self.runtimes[p.id] = r
            r.start()
            self.watch_stock(r)
        return r

    def watch_stock(self, rt, delay=25.0):
        """Once a day, when a project opens: its placed parts' stock is asked again (the ones last asked more than a
        day ago), the readings logged, and a word when a part has just run short for the planned order."""
        if not self.settings.get("stock_watch", True) or not self.settings.get("allow_web", True) or os.environ.get("TW_OFFLINE"):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def go():
            await asyncio.sleep(delay)
            from . import stock, bom as bomlib
            try:
                codes = await asyncio.to_thread(stock.stale_codes, rt.p.tw, 24.0)
                if not codes or getattr(rt, "bom_stop", None) or self.agent_busy(rt.p.id):
                    return
                await asyncio.to_thread(bomlib.lookup, rt.p.tw, codes, None, None, 90.0)
                news = await asyncio.to_thread(stock.record, rt.p.tw)
            except Exception as e:
                self.log(f"{rt.p.id}: stock watch: {type(e).__name__}: {e}")
                return
            rt.hub.emit("bom.changed")
            if news:
                self.stock_news(rt, news)
        loop.create_task(go())

    def stock_news(self, rt, news):
        """Parts that have just run short: the windows are told, and so is Claude at its next turn."""
        rt.hub.emit("stock.alert", parts=news)
        for x in news[:6]:
            what = {"out": "is out of stock", "low": "is running low", "gone": "is discontinued"}[x["state"]]
            rt.user_changes.append(f"stock watch: {', '.join(x['refs'][:4])} ({x['lcsc']}) {what}"
                                   + (f": {x['stock']} left, {x['need']} needed for the planned order" if x["stock"] is not None else ""))

    def agent(self, pid):
        from .agent import AgentManager
        rt = self.rt(pid)
        a = self.agents.get(rt.p.id)
        if a is None:
            a = AgentManager(self, rt)
            self.agents[rt.p.id] = a
        return a

    def agent_busy(self, pid):
        a = self.agents.get(pid)
        return bool(a and a.busy)

    def hubs_emit_all(self, type_, **data):
        for h in self.hubs.all():
            h.emit(type_, **data)

    # ------------------------------------------------------------------ desktop integration
    def open_in_kicad(self, project, what="project"):
        tw = project.tw
        target = {"project": tw.pro, "board": tw.pcb, "schematic": tw.sch}.get(what) or tw.pro
        if not target or not os.path.exists(target):
            raise FileNotFoundError(f"no {what} file yet")
        k = twenv.kicad()
        if sys.platform == "darwin":
            app = {"board": "PCB Editor", "schematic": "Schematic Editor"}.get(what)
            base = os.path.dirname(k["app"]) if k.get("app") else "/Applications/KiCad"
            if app and os.path.exists(os.path.join(base, f"{app}.app")):
                subprocess.Popen(["open", "-a", os.path.join(base, f"{app}.app"), target])
            else:
                subprocess.Popen(["open", "-a", k.get("app") or "KiCad", target])
        elif os.name == "nt":
            os.startfile(target)
        else:
            exe = {"board": "pcbnew", "schematic": "eeschema"}.get(what, "kicad")
            subprocess.Popen([shutil.which(exe) or "xdg-open", target])

    def reveal(self, path):
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        elif os.name == "nt":
            subprocess.Popen(["explorer", "/select,", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])

    def pick(self, kind):
        """A native folder / file chooser (macOS); None when there is none (the UI then asks for a path)."""
        if sys.platform != "darwin":
            return None
        script = ('POSIX path of (choose folder with prompt "Choose a KiCad project folder")' if kind == "folder" else
                  'POSIX path of (choose file with prompt "Choose a KiCad project, board or zip")')
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
        return r.stdout.strip() or None


def jresp(obj, status=200):
    return web.json_response(obj, status=status, dumps=lambda o: json.dumps(o, default=str))


def err(msg, status=400):
    return jresp({"error": msg}, status)


def _disposition(name, attachment=False):
    """Content-Disposition naming the file (RFC 6266: an ASCII fallback plus the UTF-8 name)."""
    from urllib.parse import quote
    ascii_name = "".join(ch if 32 <= ord(ch) < 127 and ch not in '"\\' else "_" for ch in name)
    return f'{"attachment" if attachment else "inline"}; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(name)}'


def safe_join(root, rel):
    p = os.path.abspath(os.path.join(root, rel or ""))
    if not (p == root or p.startswith(root + os.sep)):
        raise web.HTTPForbidden(text="outside the project")
    return p


def make_app():
    app = App()
    routes = web.RouteTableDef()

    OPEN = ("/login", "/api/login", "/api/health", "/favicon.ico")
    LINKABLE = ("/", "/auth", "/api/auth/google/callback")   # pages another site may link to or send the browser back to
    # the browser comes back from Google here: it has no window key (it is not the app's window), and
    # all it can do is finish a sign-in a window started (state + PKCE)
    NO_WINDOW = ("/api/health", "/api/auth/google/callback")

    def refuse(text, status=403):
        return web.Response(text=text, status=status, content_type="text/plain")

    @web.middleware
    async def local_guard(request, handler):
        """On this machine Tracewright answers only its own window. The Host must be this server's own
        address (a web page that rebinds its domain to 127.0.0.1 is refused); requests other web pages
        make (a cross-site Origin or Sec-Fetch-Site) are refused; and the API needs the launch key: the
        window's cookie or the X-Tracewright-Key header. The WebSocket handshake goes through the same."""
        if not app.local_key:
            return await handler(request)
        if request.headers.get("Host", "").lower() not in app.local_hosts():
            return refuse("Refused: this is not Tracewright's address.")
        origin = request.headers.get("Origin")
        site = request.headers.get("Sec-Fetch-Site")
        linked = request.method == "GET" and request.path in LINKABLE and request.headers.get("Sec-Fetch-Mode", "navigate") == "navigate"
        if (origin and origin.lower() not in app.local_origins()) or (site and site not in ("same-origin", "none") and not linked):
            return refuse("Refused: requests from other web pages cannot use Tracewright.")
        if request.path.startswith("/api/") and request.path not in NO_WINDOW and not app.window_ok(request):
            return jresp({"error": "open Tracewright from its app (or run `tracewright`)", "locked": True}, 401)
        return await handler(request)

    @web.middleware
    async def gate(request, handler):
        """Accounts: every API request needs a signed-in account (the sign-in endpoints, the page itself
        and its static files excepted: the page shows the sign-in screen). Without accounts, beyond
        127.0.0.1 the server's password session is needed (the older single-password sign-in)."""
        path = request.path
        if path in OPEN or path.startswith("/static/") or path == "/api/auth/state":
            return await handler(request)
        if app.accounts_on():
            if path == "/" or path.startswith("/api/auth/"):              # the page shows the sign-in screen
                return await handler(request)
            if app.user(request) or (app.require_auth and auth.valid(request.cookies.get(auth.COOKIE, ""))):
                return await handler(request)
            if path.startswith("/api/"):
                return jresp({"error": "sign in first", "signin": True}, 401)
            raise web.HTTPFound("/")
        if not app.require_auth or auth.valid(request.cookies.get(auth.COOKIE, "")) or app.window_ok(request):
            return await handler(request)
        if path.startswith("/api/"):
            return err("sign in first", 401)
        raise web.HTTPFound("/login")

    @web.middleware
    async def errors(request, handler):
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except KeyError as e:
            return err(f"not found: {e}", 404)
        except Exception as e:
            app.log(traceback.format_exc())
            return err(f"{type(e).__name__}: {e}", 500)

    # ------------------------------------------------------------------ sign-in (servers)
    @routes.get("/login")
    async def login_page(request):
        return web.Response(text=auth.LOGIN_PAGE, content_type="text/html", headers={"Cache-Control": "no-cache"})

    @routes.post("/api/login")
    async def login(request):
        ip = request.headers.get("X-Forwarded-For", request.remote or "").split(",")[0].strip()
        if auth.throttled(ip):
            return err("too many attempts. Try again in 10 minutes.", 429)
        body = await request.json()
        if not auth.password_set() or not auth.check_password(str(body.get("password", ""))):
            auth.failed(ip)
            await asyncio.sleep(1.0)
            return err("wrong password", 401)
        r = jresp({"ok": True})
        secure = request.secure or request.headers.get("X-Forwarded-Proto", "") == "https"
        r.set_cookie(auth.COOKIE, auth.new_session(), max_age=auth.DAYS * 86400, httponly=True, samesite="Strict",
                     secure=secure, path="/")
        return r

    @routes.post("/api/logout")
    async def logout(request):
        r = jresp({"ok": True})
        r.del_cookie(auth.COOKIE, path="/")
        return r

    # ------------------------------------------------------------------ accounts
    def _client_ip(request):
        return request.headers.get("X-Forwarded-For", request.remote or "").split(",")[0].strip()

    def _signed_in(request, user, remember=True):
        token = accounts.new_session(user["id"], remember, request.headers.get("User-Agent", ""))
        r = jresp({"ok": True, "user": user})
        secure = request.secure or request.headers.get("X-Forwarded-Proto", "") == "https"
        r.set_cookie(accounts.COOKIE, token, max_age=accounts.DAYS * 86400 if remember else None, httponly=True,
                     samesite="Lax", secure=secure, path="/")
        return r

    def _first_account_ok(request, body=None):
        """The first account owns this Tracewright. On your Mac that is you (the app's own window); on a
        server it is whoever knows the server's password (its session, or the password typed in the form):
        the first visitor to a new server is not always the one who set it up."""
        if accounts.count() or not app.shared() or app.window_ok(request):
            return True
        if auth.valid(request.cookies.get(auth.COOKIE, "")):
            return True
        pw, ip = (body or {}).get("server_password") or "", _client_ip(request)
        if not pw or auth.throttled(ip):
            return False
        if auth.check_password(pw):
            return True
        auth.failed(ip)
        return False

    @routes.get("/api/auth/state")
    async def auth_state(request):
        """Who is signed in, and what the sign-in screen can offer (claim: making the first account here
        takes the server's password)."""
        return jresp({"user": app.user(request), "required": app.accounts_on(), "accounts": accounts.count(),
                      "signups": accounts.signups_open(app.settings, app.shared()),
                      "claim": app.accounts_on() and not _first_account_ok(request),
                      "google": bool(accounts.google_config(app.settings)), "server_mode": app.server_mode,
                      "version": __version__, "check_count": overview.check_count()})

    @routes.post("/api/auth/register")
    async def auth_register(request):
        body = await request.json()
        if not app.accounts_on():
            return err("accounts are off here", 403)
        if not accounts.signups_open(app.settings, app.shared()):
            return err("new accounts are closed here", 403)
        if not _first_account_ok(request, body):
            await asyncio.sleep(0.6)
            return jresp({"error": "the first account needs the server password",
                          "claim": True}, 403)
        try:
            u = await asyncio.to_thread(accounts.register, body.get("email"), body.get("name"), body.get("password") or "")
        except accounts.AccountError as e:
            return err(str(e))
        return _signed_in(request, u, bool(body.get("remember", True)))

    @routes.post("/api/auth/login")
    async def auth_login(request):
        body = await request.json()
        if not app.accounts_on():
            return err("accounts are off here", 403)
        try:
            u = await asyncio.to_thread(accounts.authenticate, body.get("email"), body.get("password") or "", _client_ip(request))
        except accounts.AccountError as e:
            await asyncio.sleep(0.6)
            return err(str(e), 401)
        return _signed_in(request, u, bool(body.get("remember", True)))

    @routes.post("/api/auth/logout")
    async def auth_logout(request):
        accounts.end_session(request.cookies.get(accounts.COOKIE))
        r = jresp({"ok": True})
        r.del_cookie(accounts.COOKIE, path="/")
        return r

    @routes.patch("/api/auth/me")
    async def auth_me(request):
        u = app.user(request)
        if not u:
            return err("sign in first", 401)
        body = await request.json()
        try:
            return jresp(accounts.update(u["id"], name=body.get("name"), onboarded=body.get("onboarded"), prefs=body.get("prefs")))
        except accounts.AccountError as e:
            return err(str(e))

    @routes.post("/api/auth/password")
    async def auth_password(request):
        u = app.user(request)
        if not u:
            return err("sign in first", 401)
        body = await request.json()
        try:
            await asyncio.to_thread(accounts.change_password, u["id"], body.get("old") or "", body.get("new") or "")
        except accounts.AccountError as e:
            return err(str(e))
        return jresp({"ok": True})

    @routes.get("/api/auth/sessions")
    async def auth_sessions(request):
        u = app.user(request)
        if not u:
            return err("sign in first", 401)
        return jresp(accounts.sessions_of(u["id"], request.cookies.get(accounts.COOKIE)))

    @routes.post("/api/auth/sessions/end-others")
    async def auth_end_others(request):
        u = app.user(request)
        if not u:
            return err("sign in first", 401)
        accounts.end_other_sessions(u["id"], request.cookies.get(accounts.COOKIE))
        return jresp({"ok": True})

    @routes.get("/api/auth/users")
    async def auth_users(request):
        u = app.user(request)
        if not u or u["role"] != "owner":
            return err("only the owner can view accounts", 403)
        return jresp(accounts.users())

    @routes.delete("/api/auth/users/{uid}")
    async def auth_remove(request):
        u = app.user(request)
        try:
            accounts.remove(request.match_info["uid"], u["id"] if u else "")
        except accounts.AccountError as e:
            return err(str(e), 403)
        return jresp({"ok": True})

    @routes.patch("/api/auth/config")
    async def auth_config(request):
        """The owner's sign-in settings: open sign-ups, Google's client."""
        u = app.user(request)
        if not u or u["role"] != "owner":
            return err("only the owner can change sign-in", 403)
        body = await request.json()
        changes = {k: body[k] for k in ("allow_signups", "google_client_id", "google_client_secret", "accounts_required") if k in body}
        if "google_client_secret" in changes and not changes["google_client_secret"]:
            del changes["google_client_secret"]                        # an empty field keeps the stored secret
        app.settings.update(changes)
        return jresp({"ok": True, "google": bool(accounts.google_config(app.settings)),
                      "signups": accounts.signups_open(app.settings, app.shared())})

    def _google_redirect(request):
        if app.local_key:                           # this Mac: Google allows any loopback port for a desktop client
            return f"http://127.0.0.1:{app.port}/api/auth/google/callback"
        proto = request.headers.get("X-Forwarded-Proto") or request.scheme
        host = request.headers.get("X-Forwarded-Host") or request.host
        return f"{proto}://{host}/api/auth/google/callback"

    @routes.post("/api/auth/google/start")
    async def google_start(request):
        """body.link: link Google to the signed-in account (Settings) rather than sign in."""
        try:
            body = await request.json()
        except ValueError:
            body = {}
        if not app.accounts_on():
            return err("accounts are off here", 403)
        me = app.user(request)
        if body.get("link") and not me:
            return err("sign in first", 401)
        if not _first_account_ok(request):
            return err("create the owner account first", 403)
        try:
            return jresp(accounts.google_start(app.settings, _google_redirect(request),
                                               link_uid=me["id"] if body.get("link") and me else None, shared=app.shared()))
        except accounts.AccountError as e:
            return err(str(e))

    @routes.get("/api/auth/google/callback")
    async def google_callback(request):
        """Where Google sends the browser back: finish the sign-in, then tell the person to return."""
        q = request.query
        ok, msg = False, ""
        if q.get("error"):
            msg = "Google sign-in was cancelled" if q["error"] == "access_denied" else f"Google said: {q['error']}"
            accounts.google_fail(q.get("state"), msg)
        else:
            try:
                u = await accounts.google_finish(app.settings, q.get("state"), q.get("code"))
                ok, msg = True, f"Signed in as {u['email']}."
            except accounts.AccountError as e:
                msg = str(e)
                accounts.google_fail(q.get("state"), msg)
            except Exception as e:
                app.log(traceback.format_exc())
                msg = f"Google sign-in failed: {type(e).__name__}"
                accounts.google_fail(q.get("state"), msg)
        return web.Response(text=auth.google_done_page(ok, msg), content_type="text/html", headers={"Cache-Control": "no-store"})

    @routes.post("/api/auth/google/poll")
    async def google_poll(request):
        body = await request.json()
        status, val = accounts.google_poll(body.get("poll", ""), body.get("secret", ""))
        if status == "ok":
            return _signed_in(request, accounts.get(val), bool(body.get("remember", True)))
        if status == "pending":
            return jresp({"status": "pending"})
        return jresp({"status": "error", "error": val}, 400)

    @routes.get("/api/health")
    async def health(request):
        return jresp({"ok": True, "version": __version__})

    # ------------------------------------------------------------------ the window key (this machine)
    @routes.get("/auth")
    async def window_auth(request):
        """A browser window's one-time ticket (opened by `tracewright`) becomes its cookie."""
        exp = app.tickets.pop(request.query.get("t", ""), 0)
        if not app.local_key or exp < time.time():
            return web.Response(text=auth.LOCKED_PAGE, content_type="text/html", status=403, headers={"Cache-Control": "no-store"})
        nxt = request.query.get("next", "/")
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"
        r = web.Response(status=302, headers={"Location": nxt, "Cache-Control": "no-store"})
        r.set_cookie(auth.LOCAL_COOKIE, auth.window_cookie(app.local_key), httponly=True, samesite="Strict", path="/")
        return r

    @routes.post("/api/ticket")
    async def new_ticket(request):
        """A one-time sign-in for another window (the `tracewright` command asks, with the key)."""
        t = app.ticket()
        return jresp({"ticket": t, "url": f"http://127.0.0.1:{app.port}/auth?t={t}"})

    # ------------------------------------------------------------------ app-level
    @routes.get("/api/info")
    async def info(request):
        k = await asyncio.to_thread(twenv.kicad)
        return jresp({"version": __version__, "kicad": k, "data_dir": config.data_dir(), "workspace": config.workspace(),
                      "settings": app.settings.public(), "git": history.available(),
                      "claude_cli": shutil.which("claude"), "stages": [{"id": s[0], "title": s[1], "description": s[2]}
                                                                      for s in STAGES],
                      "freerouting": os.environ.get("TW_FREEROUTING"), "java": shutil.which("java"),
                      "live_api": twlive.HAVE_KIPY and not app.server_mode, "platform": sys.platform,
                      "problems": await asyncio.to_thread(config.native_problems),
                      "doctor": await asyncio.to_thread(_doctor, app.settings),
                      "server_mode": app.server_mode, "auth": app.require_auth, "claude_auth": _claude_auth(app.settings),
                      "check_count": overview.check_count(), "plan_usage": usage.last(),
                      "repo": REPO_URL})

    def _doctor(settings):
        from . import doctor
        try:
            return doctor.checks(settings)
        except Exception:
            return []

    @routes.get("/api/doctor")
    async def doctor_checks(request):
        """The setup, checked (doctor.py): what is missing and how to fix it."""
        from . import doctor
        return jresp({"checks": await asyncio.to_thread(doctor.checks, app.settings)})

    @routes.get("/api/usage")
    async def plan_usage(request):
        return jresp({"plan": usage.last()})

    @routes.get("/api/update")
    async def update_check(request):
        from . import update
        if not app.settings.get("update_check", True):
            return jresp({"current": __version__, "configured": False, "off": True})
        return jresp(await asyncio.to_thread(update.check, request.query.get("force") == "1"))

    @routes.get("/api/changelog")
    async def changelog(request):
        """CHANGELOG.md as releases: [{version, date, body (markdown)}], newest first."""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "CHANGELOG.md")
        try:
            text = open(path, encoding="utf-8").read()
        except OSError:
            return jresp({"releases": []})
        rel, cur = [], None
        for line in text.splitlines():
            m = re.match(r"^## \[?([0-9][^\]\s]*)\]?(?:\s*-\s*(.+))?$", line)
            if m:
                cur = {"version": m.group(1), "date": (m.group(2) or "").strip(), "lines": []}
                rel.append(cur)
            elif cur is not None and not line.startswith("[") :
                cur["lines"].append(line)
        return jresp({"releases": [{"version": r["version"], "date": r["date"], "body": "\n".join(r["lines"]).strip()} for r in rel]})

    @routes.get("/api/settings")
    async def get_settings(request):
        return jresp(app.settings.public())

    @routes.patch("/api/settings")
    async def patch_settings(request):
        body = await request.json()
        for key in ("anthropic_api_key", "github_token", "server_password"):
            if str(body.get(key, "")).startswith("set, ends"):
                body.pop(key)
        body.pop("server_password_hash", None)            # only ever computed here, from a typed password
        pw = body.pop("server_password", None)
        if pw:
            body["server_password_hash"] = auth.hash_password(str(pw))
        app.settings.update(body)
        for a in app.agents.values():
            if not a.busy:
                await a.disconnect()
        return jresp(app.settings.public())

    @routes.get("/api/live")
    async def live_status(request):
        return jresp(await asyncio.to_thread(twlive.status))

    @routes.post("/api/kicad/enable-api")
    async def enable_api(request):
        """Turn on KiCad's IPC API server (kicad_common.json api.enable_server) -- the setting KiCad's
        Preferences > Plugins > "Enable KiCad API" writes. Takes effect when KiCad next starts. A KiCad
        that is running now may write its old settings back when it quits, so the setting is checked
        again (and re-applied) once it has."""
        try:
            path, already = await asyncio.to_thread(twlive.enable_api)
        except (OSError, ValueError) as e:
            return err(f"KiCad's settings file could not be changed ({e}); turn the API on in KiCad: Preferences > Plugins")
        if already:
            return jresp({"ok": True, "already": True, "path": path})
        running = await asyncio.to_thread(twlive.kicad_process_running)
        watcher = app.jobs.get(("kicad-api", ""))
        if running and (watcher is None or watcher.done()):
            async def reapply():
                for _ in range(7200):                                   # up to 4 h
                    await asyncio.sleep(2)
                    if not await asyncio.to_thread(twlive.kicad_process_running):
                        await asyncio.sleep(2)                          # let it finish writing its settings
                        if await asyncio.to_thread(twlive.api_enabled) is False:
                            await asyncio.to_thread(twlive.enable_api)
                            app.log("KiCad wrote its old settings back on quitting; the API setting was re-applied")
                        return
            app.jobs[("kicad-api", "")] = asyncio.create_task(reapply())
        return jresp({"ok": True, "path": path, "restart_needed": running,
                      "note": "Quit and reopen KiCad to start its API server." if running else "Open KiCad: its API server starts with it."})

    @routes.get("/api/activity")
    async def activity(request):
        """Projects where Claude, the checks or the outputs are working now (the Mac app asks before quitting)."""
        busy = {pid for pid, a in app.agents.items() if a.busy}
        busy |= {k[1] for k, j in app.jobs.items() if k[0] in ("checks", "outputs") and not j.done()}
        out = []
        for pid in sorted(busy):
            try:
                out.append({"id": pid, "name": app.store.get(pid).name})
            except Exception:
                out.append({"id": pid, "name": pid})
        return jresp({"busy": out})

    @routes.post("/api/quit")
    async def quit_app(request):
        asyncio.get_running_loop().call_later(0.3, lambda: os._exit(0))
        return jresp({"ok": True})

    @routes.post("/api/pick")
    async def pick(request):
        if app.server_mode:
            return jresp({"path": None})
        body = await request.json()
        path = await asyncio.to_thread(app.pick, body.get("kind", "folder"))
        return jresp({"path": path})

    # ------------------------------------------------------------------ projects
    @routes.get("/api/projects")
    async def list_projects(request):
        return jresp(await asyncio.to_thread(app.store.list))

    @routes.post("/api/projects")
    async def create_project(request):
        body = await request.json()
        name = (body.get("name") or "").strip() or "Untitled board"
        brief = body.get("brief", "")
        guided = body.get("workflow") == "guided" and body.get("start") and brief.strip()
        lim = None
        if body.get("constraints"):
            from tw import constraints
            try:
                lim = {k: v for k, v in constraints.validate(body["constraints"]).items() if v is not None}
            except ValueError as e:
                return err(str(e))
        p = await asyncio.to_thread(app.store.create, name, brief, {"layers": body.get("layers", 2),
                                                                    "fab_house": body.get("fab_house", "jlcpcb"),
                                                                    "assembly": body.get("assembly", True),
                                                                    "run_mode": body.get("run_mode"),
                                                                    "workflow": "guided" if guided else "classic"})
        if lim:
            p.cfg["constraints"] = lim
            if "layers" in lim:
                p.cfg.setdefault("fab", {})["layers"] = lim["layers"]
            p.save()
        if body.get("start") and brief.strip():
            a = app.agent(p.id)
            await a.new_session()
            how = guided_kickoff_text() if guided else kickoff_text(p.run_mode())
            await a.send(brief.strip(), title="Design brief", hidden=how)
        return jresp(p.summary())

    @routes.get("/api/projects/{pid}/canvas")
    async def get_canvas(request):
        from . import canvas
        p = app.store.get(request.match_info["pid"])
        cv = await asyncio.to_thread(lambda: canvas.enrich(p.root, canvas.load(p.root)))
        from tw import constraints
        return jresp({**cv, "phase": p.start_phase(), "run_mode": p.run_mode(), "constraints": constraints.get(p.cfg)})

    @routes.patch("/api/projects/{pid}/canvas/floorplan")
    async def move_on_floorplan(request):
        """The user dragged something on the floorplan (a block, hole or connector, or the board's corner): saved,
        drawn again for every window, and told to Claude at its next turn."""
        from . import canvas
        rt = app.rt(request.match_info["pid"])
        try:
            cv, line = await asyncio.to_thread(canvas.move, rt.p.root, await request.json())
        except ValueError as e:
            return err(str(e))
        rt.user_changes.append(line)
        rt.hub.emit("canvas.update", canvas=canvas.enrich(rt.p.root, cv))
        return jresp({"floorplan": cv.get("floorplan")})

    @routes.post("/api/projects/{pid}/canvas/floorplan/solve")
    async def floorplan_solve(request):
        """The floorplan's Solve button: everything inside the board and clear of everything else, what the user
        placed or locked kept where it is; what cannot fit is named. {floorplan, report}."""
        from . import canvas
        rt = app.rt(request.match_info["pid"])
        body = await request.json() if request.can_read_body else {}
        if isinstance(body.get("restore"), dict):                 # Undo: the floorplan as it was before the Solve
            cv = await asyncio.to_thread(canvas.restore_floorplan, rt.p.root, body["restore"])
            rt.user_changes.append("floorplan: the user undid Solve")
            rt.hub.emit("canvas.update", canvas=canvas.enrich(rt.p.root, cv))
            return jresp({"floorplan": cv.get("floorplan"), "report": {"lines": [], "moved": [], "turned": [], "unfit": []}})
        try:
            cv, rep = await asyncio.to_thread(canvas.solve_saved, rt.p.root)
        except ValueError as e:
            return err(str(e))
        if rep["lines"]:
            rt.user_changes.append("floorplan: the user pressed Solve: " + "; ".join(rep["lines"][:6]))
        rt.hub.emit("canvas.update", canvas=canvas.enrich(rt.p.root, cv))
        return jresp({"floorplan": cv.get("floorplan"), "report": rep})

    @routes.post("/api/projects/{pid}/canvas/floorplan/suggest")
    async def floorplan_suggest(request):
        """Let Claude suggest a layout: {notes: [text]}. The suggestion comes back as ghosts over the floorplan."""
        from . import canvas
        pid = request.match_info["pid"]
        rt, a = app.rt(pid), app.agent(pid)
        if a.busy:
            return err("Claude is working: ask when it has finished", 409)
        body = await request.json()
        try:
            cv = await asyncio.to_thread(canvas.ask_layout, rt.p.root, body.get("notes") or [])
        except ValueError as e:
            return err(str(e))
        rt.hub.emit("canvas.update", canvas=canvas.enrich(rt.p.root, cv))
        notes = cv["proposal"]["notes"]
        locked = [o.get("ref") or o.get("label") or o["id"] for o in (cv["floorplan"].get("items") or []) + (cv["floorplan"].get("holes") or [])
                  if o.get("locked")]
        hidden = ("Suggest a floorplan layout: where you would put the blocks, connectors and holes, and why. Send it with the "
                  "canvas tool, section proposal (moves for what you would move, each with a few words of why; a reply to each "
                  "note: followed, or declined with why; a one-line summary). It is drawn as ghosts; the user takes all, some or "
                  "none -- do not change the floorplan section yourself." +
                  (f" Locked, leave them: {', '.join(locked)}." if locked else "") +
                  ("\nThe user's notes:\n" + "\n".join(f"{i + 1}. {n}" for i, n in enumerate(notes)) if notes else ""))
        await a.send("Suggest a layout" + (": " + "; ".join(notes) if notes else ""), hidden=hidden)
        return jresp({"proposal": cv["proposal"]})

    @routes.post("/api/projects/{pid}/canvas/floorplan/proposal")
    async def floorplan_proposal(request):
        """The user's answer to a suggested layout: {action: accept (ids: some of it, or all) | dismiss}."""
        from . import canvas
        rt = app.rt(request.match_info["pid"])
        body = await request.json()
        try:
            if body.get("action") == "accept":
                cv, line = await asyncio.to_thread(canvas.accept, rt.p.root, body.get("ids"))
                rt.user_changes.append(line)
            elif body.get("action") == "dismiss":
                cv = await asyncio.to_thread(canvas.dismiss, rt.p.root)
                rt.user_changes.append("floorplan: the user set your suggested layout aside")
            else:
                return err("action: accept or dismiss")
        except ValueError as e:
            return err(str(e))
        rt.hub.emit("canvas.update", canvas=canvas.enrich(rt.p.root, cv))
        return jresp({"floorplan": cv.get("floorplan"), "proposal": cv.get("proposal")})

    @routes.post("/api/projects/{pid}/start")
    async def start_design(request):
        """The guided start's Start button: the intake is over, the design begins."""
        pid = request.match_info["pid"]
        rt = app.rt(pid)
        p = rt.p                                  # the runtime's own project: Claude's next turn reads it
        p.reload()
        if not p.start_phase():
            return err("this project has already started", 409)
        await asyncio.to_thread(p.set_start_phase, "done")
        await asyncio.to_thread(p.set_stage, "brief", "done", "requirements agreed at the guided start")
        rt.hub.emit("project.start", phase="done")
        rt.hub.emit("stages", stages=p.stages())
        a = app.agent(pid)
        from . import canvas as cvs
        text = start_text(p.run_mode(), floorplan=bool((cvs.load(p.root).get("floorplan") or {}).get("board")))
        if a.busy:
            await a.steer(text)
        else:
            await a.send("Start the design.", hidden=text)
        return jresp(p.summary())

    @routes.post("/api/projects/import")
    async def import_project(request):
        body = await request.json()
        path = (body.get("path") or "").strip()
        if not path:
            return err("choose a KiCad project folder, a .kicad_pro, a .zip, or another tool's board file")
        mode = body.get("mode", "copy")
        from . import github
        if github.is_git_url(path):
            try:
                p = await asyncio.to_thread(app.store.import_git, path, body.get("name") or None)
            except github.GitHubError as e:
                return err(str(e))
        elif app.server_mode and not os.path.exists(os.path.expanduser(path)):
            return err("on a server, upload the project as a .zip or give its GitHub URL")
        elif mode == "in_place":
            p = await asyncio.to_thread(app.store.open_in_place, path, body.get("name") or None)
        else:
            p = await asyncio.to_thread(app.store.import_copy, path, body.get("name") or None)
        await _after_import(p, body.get("review"))
        return jresp(p.summary())

    async def _after_import(p, review):
        if review:
            a = app.agent(p.id)
            await a.new_session()
            await a.send("This KiCad project was just imported. Review it (skill import-review): summarise the design, run every "
                         "check, and tell me what is verified, what looks wrong, and what needs the built board. Don't change "
                         "anything yet.", title="Design review")

    @routes.post("/api/projects/import/upload")
    async def import_upload(request):
        """A project uploaded as a .zip (or a single board file from another tool), streamed to disk."""
        tmp = os.path.join(config.data_dir(), "tmp")
        os.makedirs(tmp, exist_ok=True)
        fields, saved = {}, None
        reader = await request.multipart()
        async for part in reader:
            if part.filename:
                name = os.path.basename(part.filename)
                saved = os.path.join(tmp, f"upload-{int(time.time() * 1000)}-{name}")
                await _stream(part, saved, 4 * 1024 ** 3)
            else:
                fields[part.name] = (await part.text()).strip()
        if not saved:
            return err("no file in the upload")
        try:
            p = await asyncio.to_thread(app.store.import_copy, saved, fields.get("name") or None)
        finally:
            os.remove(saved)
        await _after_import(p, fields.get("review") in ("1", "true", "yes"))
        return jresp(p.summary())

    @routes.post("/api/projects/{pid}/upload")
    async def project_upload(request):
        """Files into the project (data sheets, reference designs): multipart files + field `dir`."""
        rt = app.rt(request.match_info["pid"])
        target, out = "uploads", []
        reader = await request.multipart()
        async for part in reader:
            if not part.filename:
                if part.name == "dir":
                    target = (await part.text()).strip().strip("/") or "uploads"
                continue
            dest = safe_join(rt.p.root, os.path.join(target, os.path.basename(part.filename)))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            await _stream(part, dest, 512 * 1024 ** 2)
            out.append(os.path.relpath(dest, rt.p.root))
        return jresp({"saved": out})

    @routes.get("/api/projects/{pid}/download")
    async def project_download(request):
        """The project as a .zip, to open in your own KiCad (history and app state left out unless git=1)."""
        rt = app.rt(request.match_info["pid"])
        path = await asyncio.to_thread(_zip_project, rt.p, request.query.get("git") == "1")
        name = f"{rt.p.id}.zip"
        return web.FileResponse(path, headers={"Content-Disposition": f'attachment; filename="{name}"',
                                               "Cache-Control": "no-cache"})

    @routes.post("/api/projects/demo")
    async def demo_project(request):
        """A copy of the toolkit's example board (USB-C, AMS1117, ATtiny85, Qwiic) to explore."""
        from tw.checks.selftest import FIXTURE
        p = await asyncio.to_thread(app.store.import_copy, FIXTURE, "Demo: USB-C ATtiny85 board")
        p.cfg["kind"] = "demo"
        p.cfg["description"] = "The example board: USB-C sink, AMS1117 3.3 V, ATtiny85, Qwiic I2C, power LED. Placed, routed and checked."
        p.cfg["stages"] = {s: {"status": "done", "note": "", "updated": ""} for s in
                           ("brief", "architecture", "parts", "schematic", "board_setup", "placement", "routing")}
        p.cfg["stages"]["verification"] = {"status": "active", "note": "USB pair skew to review", "updated": ""}
        p.save()
        return jresp(p.summary())

    @routes.get("/api/projects/{pid}")
    async def get_project(request):
        rt = app.rt(request.match_info["pid"])
        p = rt.p.reload()
        s = p.summary()
        s["brief"] = p.brief()
        s["live"] = rt.live
        s["annotations"] = _annotations(p)
        s["toolkit_current"] = not scaffold.toolkit_outdated(p.root)
        return jresp(s)

    @routes.patch("/api/projects/{pid}")
    async def patch_project(request):
        rt = app.rt(request.match_info["pid"])
        body = await request.json()
        p = rt.p
        for k in ("name", "description", "archived"):
            if k in body:
                p.cfg[k] = body[k]
        if body.get("run_mode") in RUN_MODES:
            p.cfg["run_mode"] = body["run_mode"]
        if "fab" in body:
            p.cfg.setdefault("fab", {}).update(body["fab"])
        if "checks" in body:
            p.cfg.setdefault("checks", {}).update(body["checks"])
        if "schematic" in body:                             # conventions (the style itself is set by redrawing)
            from tw.sch import conventions
            conv = conventions.validate(body["schematic"])
            if "style" in conv and rt.p.tw.has_sch():
                conv.pop("style")
            p.cfg.setdefault("schematic", {}).update(conv)
        if "constraints" in body:                           # the requirements' Advanced limits (None: Claude decides)
            from tw import constraints
            try:
                before = constraints.get(p.cfg)
                now = constraints.merge(p.cfg, body["constraints"] or {})
            except ValueError as e:
                return err(str(e))
            if now != before:
                rt.user_changes.append("design limits: " + (constraints.describe(p.cfg) or "all left to you"))
            if now.get("layers"):                          # the fab settings follow the limit
                p.cfg.setdefault("fab", {})["layers"] = now["layers"]
        p.save()
        if "constraints" in body:                          # the user's new limit, checked against the plan at once
            from . import canvas, feasible
            cv = await asyncio.to_thread(lambda: canvas.enrich(p.root, canvas.load(p.root)))
            rt.hub.emit("canvas.update", canvas=cv)
            new = [i for i in await asyncio.to_thread(canvas.untold, p.root, cv.get("feasibility") or []) if i["level"] == "impossible"]
            if new:
                rt.user_changes.append("with the limits the user just set, this cannot work (tell them, with the fix): " +
                                       " | ".join(feasible.lines(new)))
        return jresp(p.summary())

    @routes.get("/api/constraints")
    async def constraint_options(request):
        from tw import constraints
        return jresp({"options": [{"key": k, "label": o[0], "kind": o[1], "unit": o[2], "hint": o[3], "choices": o[4]}
                                  for k, o in constraints.OPTIONS.items()]})

    @routes.get("/api/projects/{pid}/constraints")
    async def get_constraints(request):
        """The Advanced limits: every option (label, kind, unit, hint, choices) and the values set."""
        rt = app.rt(request.match_info["pid"])
        from tw import constraints
        rt.p.reload()
        opts = [{"key": k, "label": o[0], "kind": o[1], "unit": o[2], "hint": o[3], "choices": o[4]}
                for k, o in constraints.OPTIONS.items()]
        return jresp({"options": opts, "values": constraints.get(rt.p.cfg)})

    @routes.delete("/api/projects/{pid}")
    async def delete_project(request):
        pid = request.match_info["pid"]
        rt = app.runtimes.pop(pid, None)
        if rt:
            rt.stop()
        a = app.agents.pop(pid, None)
        if a:
            await a.disconnect()
        where = await asyncio.to_thread(app.store.remove, pid, request.query.get("delete_files") == "1")
        return jresp({"ok": True, "trash": where})

    @routes.post("/api/projects/{pid}/stage")
    async def set_stage(request):
        rt = app.rt(request.match_info["pid"])
        body = await request.json()
        st = rt.p.set_stage(body["stage"], body["status"], body.get("note", ""))
        rt.hub.emit("stages", stages=st)
        return jresp(st)

    # ------------------------------------------------------------------ GitHub
    @routes.get("/api/github/me")
    async def github_me(request):
        from . import github
        try:
            return jresp({"login": await asyncio.to_thread(github.whoami)})
        except github.GitHubError as e:
            return err(str(e))

    @routes.get("/api/projects/{pid}/github")
    async def github_status(request):
        from . import github
        rt = app.rt(request.match_info["pid"])
        return jresp(await asyncio.to_thread(github.status, rt.p, request.query.get("fetch") == "1"))

    @routes.post("/api/projects/{pid}/github/{action}")
    async def github_action(request):
        from . import github
        rt = app.rt(request.match_info["pid"])
        action = request.match_info["action"]
        body = await request.json() if request.can_read_body else {}
        rt.mark_self(30)
        try:
            if action == "connect":
                out = await asyncio.to_thread(github.connect, rt.p, (body.get("repo") or "").strip() or None,
                                              body.get("private", True))
            elif action == "push":
                out = await asyncio.to_thread(github.push, rt.p)
            elif action == "pull":
                out = await asyncio.to_thread(github.pull, rt.p)
                if out.get("pulled"):
                    rt.hub.emit("board.changed", version=-1, source="github")
                    rt.hub.emit("schematic.changed", source="github")
            elif action == "settings":
                rt.p.cfg.setdefault("github", {})["auto_push"] = bool(body.get("auto_push", True))
                rt.p.save()
                out = await asyncio.to_thread(github.status, rt.p, False)
            else:
                return err("unknown action", 404)
        except github.GitHubError as e:
            return err(str(e))
        rt.hub.emit("github.status", **{k: v for k, v in out.items() if k != "type"})
        return jresp(out)

    @routes.post("/api/projects/{pid}/toolkit/update")
    async def update_toolkit(request):
        rt = app.rt(request.match_info["pid"])
        v = await asyncio.to_thread(scaffold.refresh, rt.p.root, rt.p.cfg)
        rt.p.save()
        return jresp({"toolkit": v})

    # ------------------------------------------------------------------ geometry for the viewers
    @routes.get("/api/projects/{pid}/board")
    async def board(request):
        rt = app.rt(request.match_info["pid"])
        js = await asyncio.to_thread(rt.board_json)
        if js is None:
            return jresp({"empty": True})
        return jresp(js)

    @routes.post("/api/projects/{pid}/board/edit")
    async def board_edit(request):
        """An edit made in the board view: {ops, label}, the same ops as Claude's board tools (tw/pcb/kpy_ops.py), one
        undo step. Live in KiCad when it has the board open, else on the file. Waits while Claude works on the design:
        two writers on one board would lose one's work."""
        from . import boardedit
        from tw.pcb import client
        pid = request.match_info["pid"]
        rt = app.rt(pid)
        if not rt.p.tw.has_pcb():
            return err("there is no board yet", 404)
        if app.agent_busy(pid):
            return err("Claude is working on the design: edit when it is done, or stop it", 409)
        body = await request.json()
        ops = body.get("ops") or []
        if not isinstance(ops, list) or not ops or not all(isinstance(o, dict) for o in ops):
            return err("no edit")
        bad = sorted({str(o.get("op")) for o in ops} - boardedit.EDIT_OPS)
        if bad:
            return err("not an edit the board view makes: " + ", ".join(bad))
        label = str(body.get("label") or "Edit")[:80]
        merge = bool(body.get("merge"))                   # a follow-up (the pours filled again): part of the last edit
        async with rt.edit_lock:
            def run():
                with rt.edits.lock:
                    joined = merge and rt.edits.can_amend()
                    snap = None if joined else rt.edits.before()
                    rt.mark_app_edit(10 if any(o.get("op") == "fill" for o in ops) or body.get("fill") else 4)
                    try:
                        res = client.apply(rt.p.tw, ops, save=True, live="auto", fill_after=bool(body.get("fill")))
                    except Exception as e:
                        res = {"ok": False, "error": str(e)}
                    if not res.get("ok"):
                        if snap:
                            rt.edits.failed(snap)
                        return res, None
                    if joined:
                        rt.edits.amend()
                    else:
                        rt.edits.done(snap, label)
                    boardedit.record(rt.p, ops, res.get("changes"))
                    return res, rt.edits.state()
            res, hist = await asyncio.to_thread(run)
        if not res.get("ok"):
            errs = [r.get("error") for r in res.get("results") or [] if isinstance(r, dict) and r.get("error")]
            msg = (errs[0] if errs else res.get("error")) or "the edit did not apply"
            return err(re.sub(r"^\w+(Error|Exception): ", "", str(msg))[:300], 422)
        if not merge:
            rt.user_changes.append("board (the user, in the app): " + boardedit.describe(ops, res.get("changes")))
        return jresp({"ok": True, "via": res.get("via"), "changes": res.get("changes") or [], "history": hist})

    async def _board_ops(pid, ops, label, said):
        """Board ops from an app action (fan-out, a region) as one undo step, as an edit in the board view is."""
        from . import boardedit
        from tw.pcb import client
        rt = app.rt(pid)
        async with rt.edit_lock:
            def run():
                with rt.edits.lock:
                    snap = rt.edits.before()
                    rt.mark_app_edit(10)
                    try:
                        res = client.apply(rt.p.tw, ops, save=True, live="auto")
                    except Exception as e:
                        res = {"ok": False, "error": str(e)}
                    if not res.get("ok"):
                        rt.edits.failed(snap)
                        return res, None
                    rt.edits.done(snap, label)
                    boardedit.record(rt.p, ops, res.get("changes"))
                    return res, rt.edits.state()
            res, hist = await asyncio.to_thread(run)
        if not res.get("ok"):
            errs = [r.get("error") for r in res.get("results") or [] if isinstance(r, dict) and r.get("error")]
            return None, (errs[0] if errs else res.get("error")) or "the change did not apply"
        rt.user_changes.append(said)
        rt.hub.emit("board.changed", version=-1, source="app")
        return {"ok": True, "history": hist}, None

    @routes.get("/api/projects/{pid}/board/space")
    async def board_space(request):
        """Where tracks fit: each routing layer's free share, the connections still to route against the room, the
        crowded regions in words (tw/space.py)."""
        from tw import space
        rt = app.rt(request.match_info["pid"])
        if not rt.p.tw.has_pcb():
            return err("there is no board yet", 404)
        cls = request.query.get("class") or "Default"
        try:
            return jresp(await asyncio.to_thread(space.congestion, rt.p.reload().tw, cls))
        except ValueError as e:
            return err(str(e), 422)

    @routes.get("/api/projects/{pid}/board/why")
    async def board_why(request):
        """What keeps a track off one point: ?x=&y=&layer=&net= (tw/space.py)."""
        from tw import space
        rt = app.rt(request.match_info["pid"])
        if not rt.p.tw.has_pcb():
            return err("there is no board yet", 404)
        try:
            x, y = float(request.query["x"]), float(request.query["y"])
        except (KeyError, ValueError):
            return err("x and y in mm")
        b = await asyncio.to_thread(rt.board)
        return jresp(await asyncio.to_thread(space.why, rt.p.tw, x, y, request.query.get("layer") or "F.Cu",
                                             request.query.get("net") or None, b))

    @routes.get("/api/projects/{pid}/escape")
    async def escape_get(request):
        """The dense parts' escape plans and the HDI settings (Board > Routing > Fan-out)."""
        from tw import escape, hdi
        rt = app.rt(request.match_info["pid"])
        p = rt.p.reload()
        out = {"hdi": hdi.get(p.cfg), "hdi_text": hdi.describe(p.cfg), "parts": []}
        if p.tw.has_pcb():
            b = await asyncio.to_thread(rt.board)
            out["parts"] = await asyncio.to_thread(escape.plan, p.tw, b)
            from tw import routability
            try:
                r = await asyncio.to_thread(routability.estimate, p.tw, b)
                out["routability"] = {k: r[k] for k in ("verdict", "lines", "fixes")}
            except Exception as e:
                out["routability"] = {"verdict": "unknown", "lines": [str(e)[:200]], "fixes": []}
            try:
                with open(os.path.join(p.tw.build, "breakout.json")) as f:
                    bo = json.load(f)
                out["breakout"] = [{k: x.get(k) for k in ("ref", "kind", "escaped", "signals", "under", "vias", "by_layer")}
                                   | {"left": len(x.get("left") or [])} for x in bo.get("parts", [])]
            except (OSError, ValueError):
                out["breakout"] = None
            have = {(round(v.x, 2), round(v.y, 2)) for v in b.vias}
            for pl in out["parts"]:
                fp = b.footprints.get(pl["ref"])
                if fp is not None and pl["kind"] == "array":
                    bb = fp.bbox()
                    pl["vias_under"] = sum(1 for v in b.vias if bb[0] <= v.x <= bb[2] and bb[1] <= v.y <= bb[3])
        return jresp(out)

    @routes.post("/api/projects/{pid}/escape")
    async def escape_fanout(request):
        """{ref, method?}: fan the part out (its vias, and dog-bone stubs), one undo step."""
        from tw import escape
        pid = request.match_info["pid"]
        rt = app.rt(pid)
        if not rt.p.tw.has_pcb():
            return err("there is no board yet", 404)
        if app.agent_busy(pid):
            return err("Claude is working on the design: fan out when it is done, or stop it", 409)
        body = await request.json()
        try:
            ops, summ = await asyncio.to_thread(escape.fanout, rt.p.reload().tw, str(body.get("ref") or ""), body.get("method") or None)
        except ValueError as e:
            return err(str(e), 422)
        if not ops:
            return jresp({"ok": True, "summary": summ})
        res, e = await _board_ops(pid, ops, f"Fan out {summ['ref']}",
                                  f"board (the user, in the app): fanned out {summ['ref']} ({summ['method']}, {summ['vias']} vias)")
        if e:
            return err(str(e)[:300], 422)
        return jresp({**res, "summary": summ})

    @routes.get("/api/projects/{pid}/hdi")
    async def hdi_get(request):
        from tw import hdi
        p = app.rt(request.match_info["pid"]).p.reload()
        return jresp({"hdi": hdi.get(p.cfg), "text": hdi.describe(p.cfg), "notes": hdi.fab_notes(p.cfg)})

    @routes.post("/api/projects/{pid}/hdi")
    async def hdi_post(request):
        """The HDI settings (the user's choice: it costs more). On, its DRC rules for microvias and blind vias go in."""
        from tw import hdi
        from tw.pcb import rules as dru
        rt = app.rt(request.match_info["pid"])
        body = await request.json()
        p = rt.p.reload()
        try:
            new = hdi.validate({**hdi.get(p.cfg), **{k: v for k, v in body.items() if k in hdi.DEFAULT}})
        except ValueError as e:
            return err(str(e))
        was = hdi.get(p.cfg)
        p.cfg["hdi"] = new
        p.save()
        if new["on"] and p.tw.has_pcb():
            await asyncio.to_thread(dru.ensure_rules, p.tw, hdi.dru_rules(p.cfg), True)
        if was["on"] != new["on"]:
            rt.user_changes.append("HDI: the user turned it " + ("on (" + hdi.describe(p.cfg)[8:] + ")" if new["on"] else "off: through vias only, none in pads"))
        else:
            rt.user_changes.append("HDI settings: " + hdi.describe(p.cfg))
        rt.hub.emit("routing.changed")
        return jresp({"ok": True, "hdi": new, "text": hdi.describe(p.cfg)})

    @routes.get("/api/projects/{pid}/regions")
    async def regions_get(request):
        from tw import regions
        rt = app.rt(request.match_info["pid"])
        p = rt.p.reload()
        b = await asyncio.to_thread(rt.board) if p.tw.has_pcb() else None
        return jresp({"regions": regions.listing(p.tw, b), "kinds": regions.KINDS})

    @routes.post("/api/projects/{pid}/regions")
    async def regions_post(request):
        """{name, kind, polygon, layers?, track?, clearance?} adds (or redraws) a region; {remove: name} takes it off."""
        from tw import regions
        pid = request.match_info["pid"]
        rt = app.rt(pid)
        if not rt.p.tw.has_pcb():
            return err("there is no board yet", 404)
        if app.agent_busy(pid):
            return err("Claude is working on the design: change regions when it is done, or stop it", 409)
        body = await request.json()
        p = rt.p.reload()
        b = await asyncio.to_thread(rt.board)
        try:
            if body.get("remove"):
                ops = regions.remove(p.tw, str(body["remove"]))
                label, said = f"Remove region {body['remove']}", f"regions: the user removed {body['remove']}"
            else:
                ops = regions.add(p.tw, body.get("name"), body.get("polygon"), body.get("kind"), body.get("layers"),
                                  body.get("track"), body.get("clearance"), by="user", copper=b.copper)
                r = next(x for x in p.tw.cfg.get("regions") or [] if x["name"] == regions._name_ok(body.get("name")))
                label, said = f"Region {r['name']}", "regions: the user set " + regions.describe(r)
        except KeyError:
            return err("no such region", 404)
        except ValueError as e:
            return err(str(e))
        p.reload()
        res, e = await _board_ops(pid, ops + [{"op": "fill"}], label, said)
        if e:
            return err(str(e)[:300], 422)
        rt.hub.emit("routing.changed")
        return jresp(res)

    async def _board_step(request, back):
        pid = request.match_info["pid"]
        rt = app.rt(pid)
        if app.agent_busy(pid):
            return err("Claude is working on the design: undo when it is done, or stop it", 409)
        async with rt.edit_lock:
            def run():
                with rt.edits.lock:
                    rt.mark_app_edit(4)
                    label = rt.edits.step(back)
                    if label is not None:
                        try:
                            link = twlive.link_for(rt.p.tw.pcb)
                            if link is not None:
                                link.revert()                 # KiCad shows the board file as it is again
                        except Exception:
                            traceback.print_exc()
                    return label, rt.edits.state()
            label, hist = await asyncio.to_thread(run)
        if label is None:
            return err("nothing to " + ("undo" if back else "redo"), 409)
        rt.user_changes.append(f"board (the user, in the app): {'undid' if back else 'redid'} \"{label}\"")
        return jresp({"ok": True, "label": label, "history": hist})

    @routes.post("/api/projects/{pid}/board/undo")
    async def board_undo(request):
        return await _board_step(request, True)

    @routes.post("/api/projects/{pid}/board/redo")
    async def board_redo(request):
        return await _board_step(request, False)

    @routes.get("/api/projects/{pid}/board/history")
    async def board_history(request):
        rt = app.rt(request.match_info["pid"])
        if not rt.p.tw.has_pcb():
            return jresp({"undo": 0, "redo": 0, "undo_label": "", "redo_label": ""})
        return jresp(await asyncio.to_thread(rt.edits.state))

    @routes.post("/api/projects/{pid}/board/suggest")
    async def board_suggest(request):
        """Let Claude suggest placement: {notes: [text], refs?: the parts to place (default: all)}. It comes back as
        ghosts on the board (board.proposal)."""
        from . import boardedit
        pid = request.match_info["pid"]
        rt, a = app.rt(pid), app.agent(pid)
        if a.busy:
            return err("Claude is working: ask when it has finished", 409)
        if not rt.p.tw.has_pcb():
            return err("there is no board yet", 404)
        body = await request.json()
        d = await asyncio.to_thread(boardedit.ask, rt.p, body.get("notes") or [], body.get("refs") or [])
        rt.hub.emit("board.proposal", proposal=d)
        b = await asyncio.to_thread(rt.board)
        locked = sorted(f.ref for f in b.fp_list if f.locked) if b else []
        which = f"these parts: {', '.join(d['refs'])}" if d["refs"] else "the board's parts"
        hidden = (f"Suggest placement for {which}: the place tool with propose true (nothing moves; each move with a few words of "
                  "why; a reply to each note: followed, or declined with why; a one-line summary). The user takes all, some or "
                  "none." + (f" Locked, leave them: {', '.join(locked)}." if locked else "") +
                  ("\nThe user's notes:\n" + "\n".join(f"{i + 1}. {n}" for i, n in enumerate(d["notes"])) if d["notes"] else ""))
        await a.send("Suggest placement" + (" for " + ", ".join(d["refs"][:8]) + ("…" if len(d["refs"]) > 8 else "") if d["refs"] else "") +
                     (": " + "; ".join(d["notes"]) if d["notes"] else ""), hidden=hidden)
        return jresp({"proposal": d})

    @routes.get("/api/projects/{pid}/board/proposal")
    async def board_proposal_get(request):
        from . import boardedit
        rt = app.rt(request.match_info["pid"])
        return jresp({"proposal": await asyncio.to_thread(boardedit.proposal, rt.p)})

    @routes.post("/api/projects/{pid}/board/proposal")
    async def board_proposal_act(request):
        """{action: take (refs: some, or all) | dismiss}: taking is the user's own edit, one undo step."""
        from . import boardedit
        from tw.pcb import client
        pid = request.match_info["pid"]
        rt = app.rt(pid)
        body = await request.json()
        if body.get("action") == "dismiss":
            await asyncio.to_thread(boardedit.drop, rt.p)
            rt.user_changes.append("board: the user set your suggested placement aside")
            rt.hub.emit("board.proposal", proposal=None)
            return jresp({"proposal": None})
        if body.get("action") != "take":
            return err("action: take or dismiss")
        if app.agent_busy(pid):
            return err("Claude is working on the design: take it when it is done", 409)
        try:
            ops, left = await asyncio.to_thread(boardedit.take, rt.p, body.get("refs"))
        except ValueError as e:
            return err(str(e))
        async with rt.edit_lock:
            def run():
                with rt.edits.lock:
                    snap = rt.edits.before()
                    rt.mark_app_edit(4)
                    res = client.apply(rt.p.tw, ops, save=True, live="auto")
                    if not res.get("ok"):
                        rt.edits.failed(snap)
                        return res, None
                    rt.edits.done(snap, f"Take the suggested placement ({len(ops)} part{'s' if len(ops) != 1 else ''})")
                    boardedit.record(rt.p, ops, res.get("changes"))
                    return res, rt.edits.state()
            res, hist = await asyncio.to_thread(run)
        if not res.get("ok"):
            return err("the moves did not apply", 422)
        d = await asyncio.to_thread(boardedit.keep_rest, rt.p, left)
        rt.user_changes.append(f"board: the user took your suggested placement for {', '.join(o['ref'] for o in ops)}" +
                               (f" (not for {len(left)} more)" if left else ""))
        rt.hub.emit("board.proposal", proposal=d)
        return jresp({"proposal": d, "history": hist})

    @routes.get("/api/projects/{pid}/schematic")
    async def schematic(request):
        rt = app.rt(request.match_info["pid"])
        js = await asyncio.to_thread(rt.schematic_json)
        if js is None:
            return jresp({"empty": True})
        return jresp(js)

    @routes.post("/api/projects/{pid}/breakout")
    async def breakout_post(request):
        """{make_room?: true}: the dense parts broken out (tw.breakout) -- the parts under each BGA moved off its via
        spots first, then its vias and escapes, and vias beside fine-pitch parts whose nets change layer -- one undo step."""
        from tw import breakout
        from tw.pcb import client
        pid = request.match_info["pid"]
        rt = app.rt(pid)
        if not rt.p.tw.has_pcb():
            return err("there is no board yet", 404)
        if app.agent_busy(pid):
            return err("Claude is working on the design: break out when it is done, or stop it", 409)
        body = await request.json()

        def work():
            with rt.edits.lock:
                snap = rt.edits.before()
                rt.mark_app_edit(60)
                ap = lambda ops: client.apply(rt.p.tw, ops, save=True, live="auto")
                try:
                    mr = breakout.make_room(rt.p.reload().tw, apply=True, log=lambda m: None, apply_fn=ap) \
                        if body.get("make_room", True) else None
                    rep = breakout.run(rt.p.reload().tw, apply=True, log=lambda m: None, apply_fn=ap)
                except Exception as e:
                    rt.edits.failed(snap)
                    return None, None, str(e)
                if rep.get("error"):
                    rt.edits.failed(snap)
                    return None, None, rep["error"]
                rt.edits.done(snap, "Break out the dense parts")
                return mr, rep, None
        async with rt.edit_lock:
            mr, rep, e = await asyncio.to_thread(work)
        if e:
            return err(str(e)[:300], 422)
        parts = [{k: x.get(k) for k in ("ref", "kind", "escaped", "signals", "under", "vias", "by_layer")} | {"left": len(x.get("left") or [])}
                 for x in rep["parts"]]
        rt.user_changes.append("board (the user, in the app): broke out the dense parts: " + "; ".join(
            f"{x['ref']} {x['escaped']} out" for x in parts if x.get("escaped") is not None))
        rt.hub.emit("board.changed", version=-1, source="app")
        return jresp({"ok": True, "parts": parts, "moved": len((mr or {}).get("moves") or []), "history": rt.edits.state()})

    @routes.get("/api/projects/{pid}/pairs")
    async def pairs_get(request):
        """Each differential pair with its halves' lengths, the skew and the budget the checks hold it to."""
        from tw.route import tune
        rt = app.rt(request.match_info["pid"])
        if not rt.p.tw.has_pcb():
            return jresp({"pairs": []})
        b = await asyncio.to_thread(rt.board)
        return jresp({"pairs": await asyncio.to_thread(tune.pairs, rt.p.reload().tw, b)})

    @routes.post("/api/projects/{pid}/tune")
    async def tune_post(request):
        """Meanders on the shorter halves (and length groups' short members) to within their budgets: one undo step."""
        from tw.route import tune
        from tw.pcb import client
        pid = request.match_info["pid"]
        rt = app.rt(pid)
        if not rt.p.tw.has_pcb():
            return err("there is no board yet", 404)
        if app.agent_busy(pid):
            return err("Claude is working on the design: tune when it is done, or stop it", 409)

        def work():
            with rt.edits.lock:
                snap = rt.edits.before()
                rt.mark_app_edit(30)
                try:
                    r = tune.tune(rt.p.reload().tw, apply=True, log=lambda m: None,
                                  apply_fn=lambda ops: client.apply(rt.p.tw, ops, save=True, live="auto"))
                except Exception as e:
                    rt.edits.failed(snap)
                    return None, str(e)
                if r["tuned"]:
                    rt.edits.done(snap, f"Tune {len(r['tuned'])} net{'s' if len(r['tuned']) != 1 else ''}")
                else:
                    rt.edits.failed(snap)
                return r, None
        async with rt.edit_lock:
            r, e = await asyncio.to_thread(work)
        if e:
            return err(e[:300], 422)
        if r["tuned"]:
            rt.user_changes.append("board (the user, in the app): tuned " + ", ".join(x["net"].rsplit("/", 1)[-1] for x in r["tuned"]))
            rt.hub.emit("board.changed", version=-1, source="app")
        return jresp({"ok": True, "tuned": r["tuned"], "left": r["left"], "history": rt.edits.state()})

    @routes.get("/api/projects/{pid}/pins")
    async def pins_get(request):
        """The pin plan's proposal (tw.pinplan): free GPIO moved to the connector pins that lie the way they leave the chip."""
        from tw import pinplan
        rt = app.rt(request.match_info["pid"])
        if not rt.p.tw.has_pcb():
            return err("there is no board yet", 404)
        plan = await asyncio.to_thread(pinplan.propose, rt.p.reload().tw, None, lambda m: None)
        rt.pin_plan = plan
        return jresp({k: plan[k] for k in ("moves", "groups", "crossings_before", "crossings_after", "lines")})

    @routes.post("/api/projects/{pid}/pins")
    async def pins_post(request):
        """Apply the proposed pin plan: design/pin-plan.json, the schematic generated again, the board updated."""
        from tw import pinplan
        pid = request.match_info["pid"]
        rt = app.rt(pid)
        if app.agent_busy(pid):
            return err("Claude is working on the design: apply the pin plan when it is done, or stop it", 409)
        plan = getattr(rt, "pin_plan", None) or await asyncio.to_thread(pinplan.propose, rt.p.reload().tw, None, lambda m: None)
        if not plan["moves"]:
            return jresp({"ok": True, "moves": 0})
        rt.mark_app_edit(600)
        r = await asyncio.to_thread(pinplan.apply, rt.p.reload().tw, plan)
        rt.mark_app_edit(4)
        if r.get("error"):
            return err(r["error"][:400], 422)
        rt.pin_plan = None
        rt.user_changes.append(f"pin plan (the user, in the app): applied {len(plan['moves'])} pin moves "
                               "(design/pin-plan.json, docs/pin-plan.md); the schematic was generated again")
        rt.hub.emit("schematic.changed", source="tracewright", files=[])
        rt.hub.emit("board.changed", version=-1, source="app")
        return jresp({"ok": True, "moves": len(plan["moves"]), "schematic": r.get("schematic")})

    @routes.get("/api/projects/{pid}/routing")
    async def routing_get(request):
        """The routing plan (tw/routeplan.py): every net's mode (auto, guided, hand), why, its rules and how its last
        routing went; the router presets."""
        from tw import routeplan
        rt = app.rt(request.match_info["pid"])
        if not rt.p.tw.has_sch():
            return jresp({"nets": [], "preset": routeplan.preset(rt.p.tw), "presets": []})

        def work():
            from tw.ratsnest import ratsnest
            plan = routeplan.classify(rt.p.reload().tw)
            try:
                rep = json.load(open(os.path.join(rt.p.tw.build, "route-report.json")))
            except (OSError, ValueError):
                rep = {}
            open_nets = set()
            if rt.p.tw.has_pcb():
                open_nets = {l[-1] for l in ratsnest(rt.board())}
            rows = []
            for net, m in plan.items():
                r = (rep.get("nets") or {}).get(net) or {}
                status = "unrouted" if net in open_nets else ("routed" if rt.p.tw.has_pcb() else "no board")
                rows.append({"net": net.rsplit("/", 1)[-1], "full": net, **m, "status": status,
                             **{k: r[k] for k in ("length_mm", "vias", "note", "detail") if k in r},
                             **({"routed_on": r["layers"]} if r.get("layers") else {})})
            order = {"hand": 0, "guided": 1, "auto": 2}
            rows.sort(key=lambda x: (order[x["mode"]], x["net"]))
            return {"nets": rows, "preset": routeplan.preset(rt.p.tw), "report_at": rep.get("at"),
                    "presets": [{"id": k, "label": v["label"], "why": v["why"]} for k, v in routeplan.PRESETS.items()]}
        return jresp(await asyncio.to_thread(work))

    @routes.post("/api/projects/{pid}/routing")
    async def routing_post(request):
        """{preset} | {net, mode: auto | guided | hand | null (back to the rule)} | {net, layers: [...] | null}."""
        from tw import routeplan
        rt = app.rt(request.match_info["pid"])
        body = await request.json()
        p = rt.p.reload()
        try:
            if body.get("preset"):
                await asyncio.to_thread(routeplan.set_preset, p, body["preset"])
                rt.user_changes.append(f"routing: the user chose the {routeplan.PRESETS[body['preset']]['label']} preset")
            elif body.get("net") and "layers" in body:
                b_ = await asyncio.to_thread(rt.board) if p.tw.has_pcb() else None
                await asyncio.to_thread(routeplan.set_layers, p, str(body["net"]), body.get("layers"), b_.copper if b_ else None)
                rt.user_changes.append(f"routing: the user put {body['net']} on " + (", ".join(body["layers"]) if body.get("layers") else "any layer"))
            elif body.get("net"):
                await asyncio.to_thread(routeplan.set_mode, p, str(body["net"]), body.get("mode"))
                rt.user_changes.append(f"routing: the user set {body['net']} to " +
                                       ({"hand": "hand routing (leave it to them)", "guided": "routed with its rules", "auto": "the router"}.get(body.get("mode"), "its rule")))
            else:
                return err("preset, or net and mode")
        except ValueError as e:
            return err(str(e))
        rt.hub.emit("routing.changed")
        return jresp({"ok": True})

    @routes.get("/api/projects/{pid}/placement")
    async def placement_get(request):
        """The placement plan (tw/placeplan.py): each part's reason, the stages, the constraints (kept or not) and the
        placement's score as it stands."""
        from tw import placeplan
        rt = app.rt(request.match_info["pid"])
        if not rt.p.tw.has_pcb():
            return jresp({"plan": None})

        def work():
            from tw.checks.context import Context
            plan = placeplan.load(rt.p.tw)
            b = rt.board()
            nl, grounds, hot = None, set(), []
            try:
                ctx = Context(rt.p.tw, offline=True)
                if rt.p.tw.has_sch():
                    nl = ctx.netlist
                    grounds = ctx.ground_nets()
                res = json.load(open(os.path.join(rt.p.tw.build, "checks.json")))
                th = next((c for c in res.get("checks", []) if c.get("id") == "power.thermal"), {})
                hot = [m["ref"] for m in th.get("measured") or [] if (m.get("watts") or 0) >= 0.2]
            except Exception:
                pass
            return {"plan": plan, "constraints": placeplan.evaluate(b, plan), "score": placeplan.score(b, nl, plan, grounds, hot),
                    "stages": [{"id": k, "text": placeplan.STAGE_TEXT[k], "done": plan["stages"].get(k)} for k in placeplan.STAGES]}
        return jresp(await asyncio.to_thread(work))

    @routes.post("/api/projects/{pid}/placement")
    async def placement_post(request):
        """The user's constraints: {action: add, constraint: {kind, ...}} | {action: remove, id}."""
        from tw import placeplan
        rt = app.rt(request.match_info["pid"])
        body = await request.json()
        try:
            if body.get("action") == "remove":
                await asyncio.to_thread(placeplan.remove_constraint, rt.p.tw, str(body.get("id") or ""))
                rt.user_changes.append(f"placement: the user dropped constraint {body.get('id')}")
                out = {"ok": True}
            else:
                c = await asyncio.to_thread(placeplan.add_constraint, rt.p.tw, body.get("constraint") or {}, "user")
                rt.user_changes.append(f"placement: the user set a constraint, keep it: {placeplan.describe(c)}")
                out = c
        except (ValueError, KeyError) as e:
            return err(str(e).strip("'"))
        rt.hub.emit("placement.changed")
        return jresp(out)

    @routes.get("/api/projects/{pid}/schematic/notes")
    async def sch_notes_get(request):
        """The design notes (tw/sch/notes.py) with where each sits on the sheets: {notes: [{id, anchor, short, why, by,
        sheet_path, x, y}]} -- a part's at its top-right corner, a pin's at its end, a net's at its first label."""
        from tw.sch import notes as dn
        rt = app.rt(request.match_info["pid"])
        if not rt.p.tw.has_sch():
            return jresp({"notes": []})
        js = await asyncio.to_thread(rt.schematic_json)
        out = []
        for n in await asyncio.to_thread(dn.load, rt.p.tw):
            a, placed = n["anchor"], None
            for sh in js["sheets"]:
                if a.get("ref"):
                    sym = next((y for y in sh["symbols"] if y["ref"] == a["ref"]), None)
                    if sym:
                        pin = next((q for q in sym["pins"] if str(q[0]) == a.get("pin")), None) if a.get("pin") else None
                        placed = (sh["name_path"], pin[3], pin[4]) if pin else (sh["name_path"], sym["bbox"][2], sym["bbox"][1])
                        break
                elif a.get("net"):
                    lab = next((l for l in sh["labels"] if l["text"] == a["net"]), None)
                    if lab:
                        placed = (sh["name_path"], lab["x"], lab["y"])
                        break
            out.append({**n, **({"sheet_path": placed[0], "x": placed[1], "y": placed[2]} if placed else {})})
        return jresp({"notes": out})

    @routes.post("/api/projects/{pid}/schematic/notes")
    async def sch_notes_post(request):
        """The user's own design note: {action: add, anchor: {ref, pin?} | {net}, why} | {action: remove, id}."""
        from tw.sch import notes as dn
        rt = app.rt(request.match_info["pid"])
        body = await request.json()
        try:
            if body.get("action") == "remove":
                await asyncio.to_thread(dn.remove, rt.p.tw, str(body.get("id") or ""))
                rt.user_changes.append(f"design notes: the user removed note {body.get('id')}")
                return jresp({"ok": True})
            text = str(body.get("why") or "").strip()
            if not text:
                return err("the note is empty")
            n = await asyncio.to_thread(dn.add, rt.p.tw, body.get("anchor") or {}, text, "", "", "user")
        except (ValueError, KeyError) as e:
            return err(str(e).strip("'"))
        a = n["anchor"]
        rt.user_changes.append(f"design notes: the user noted on {a.get('ref') or 'net ' + a.get('net', '')}"
                               f"{' pin ' + a['pin'] if a.get('pin') else ''}: {text[:200]}")
        rt.hub.emit("schematic.notes")
        return jresp(n)

    @routes.post("/api/projects/{pid}/schematic/edit")
    async def schematic_edit(request):
        """An edit made in the schematic view: {ops, label} (schedit.py: a part's fields, its DNP / BOM / board flags, a
        net's name), one undo step. Waits while Claude works on the design, and while KiCad has the schematic open (it
        would write its own copy over this one when saved)."""
        from . import schedit
        pid = request.match_info["pid"]
        rt = app.rt(pid)
        if not rt.p.tw.has_sch():
            return err("there is no schematic yet", 404)
        if app.agent_busy(pid):
            return err("Claude is working on the design: edit when it is done, or stop it", 409)
        if (rt.live or {}).get("sch_open"):
            return err("The schematic is open in KiCad: edit it there, or close it in KiCad to edit it here", 409)
        body = await request.json()
        ops = body.get("ops") or []
        if not isinstance(ops, list) or not ops or not all(isinstance(o, dict) and o.get("op") in schedit.OPS for o in ops):
            return err("no edit")
        async with rt.edit_lock:
            def run():
                with rt.sch_edits.lock:
                    snap = rt.sch_edits.before()
                    rt.mark_self(4)
                    try:
                        lines = schedit.apply(rt.p, ops)
                    except Exception as e:
                        rt.sch_edits.abandon(snap)               # whatever an op wrote before the one that failed
                        return None, str(e), None
                    if not lines:                                # it was so already: no undo step for nothing
                        rt.sch_edits.abandon(snap)
                        return [], None, rt.sch_edits.state()
                    rt.sch_edits.done(snap, str(body.get("label") or lines[0])[:80])
                    schedit.record(rt.p, ops)
                    return lines, None, rt.sch_edits.state()
            lines, problem, hist = await asyncio.to_thread(run)
        if problem:
            return err(re.sub(r"^\w+(Error|Exception): ", "", problem)[:300], 422)
        if not lines:
            return jresp({"ok": True, "changes": [], "history": hist, "board_out_of_date": False})
        rt.user_changes.append("schematic (the user, in the app): " + schedit.describe(lines))
        rt.hub.emit("schematic.changed", source="app")
        return jresp({"ok": True, "changes": lines, "history": hist,
                      "board_out_of_date": rt.p.tw.has_pcb() and any(o.get("op") == "rename" or "Footprint" in (o.get("fields") or {})
                                                                      or "on_board" in o for o in ops)})

    @routes.post("/api/projects/{pid}/board/sync")
    async def board_sync(request):
        """Update the board from the schematic (after the user changed a footprint or a net's name there): one undo
        step on the board."""
        from tw.pcb import client
        pid = request.match_info["pid"]
        rt = app.rt(pid)
        if not rt.p.tw.has_pcb():
            return err("there is no board yet", 404)
        if app.agent_busy(pid):
            return err("Claude is working on the design: update the board when it is done", 409)
        async with rt.edit_lock:
            def run():
                with rt.edits.lock:
                    snap = rt.edits.before()
                    rt.mark_app_edit(20)
                    try:
                        res = client.sync(rt.p.tw)
                    except Exception as e:
                        res = {"ok": False, "error": str(e)}
                    if not res.get("ok"):
                        rt.edits.failed(snap)
                        return res, None
                    rt.edits.done(snap, "Update from the schematic")
                    return res, rt.edits.state()
            res, hist = await asyncio.to_thread(run)
        if not res.get("ok"):
            return err(re.sub(r"^\w+(Error|Exception): ", "", str(res.get("error") or "the update did not apply"))[:300], 422)
        rt.user_changes.append("board (the user, in the app): updated the board from the schematic")
        rt.hub.emit("board.changed", version=-1, source="app")
        return jresp({"ok": True, "result": res, "history": hist})

    async def _sch_step(request, back):
        pid = request.match_info["pid"]
        rt = app.rt(pid)
        if app.agent_busy(pid):
            return err("Claude is working on the design: undo when it is done, or stop it", 409)
        if (rt.live or {}).get("sch_open"):
            return err("The schematic is open in KiCad: undo there, or close it in KiCad", 409)
        async with rt.edit_lock:
            def run():
                with rt.sch_edits.lock:
                    rt.mark_self(4)
                    return rt.sch_edits.step(back), rt.sch_edits.state()
            label, hist = await asyncio.to_thread(run)
        if label is None:
            return err("nothing to " + ("undo" if back else "redo"), 409)
        rt.user_changes.append(f"schematic (the user, in the app): {'undid' if back else 'redid'} \"{label}\"")
        rt.hub.emit("schematic.changed", source="app")
        return jresp({"ok": True, "label": label, "history": hist})

    @routes.post("/api/projects/{pid}/schematic/undo")
    async def schematic_undo(request):
        return await _sch_step(request, True)

    @routes.post("/api/projects/{pid}/schematic/redo")
    async def schematic_redo(request):
        return await _sch_step(request, False)

    @routes.get("/api/projects/{pid}/schematic/history")
    async def schematic_history(request):
        rt = app.rt(request.match_info["pid"])
        return jresp(rt.sch_edits.state() if rt.p.tw.has_sch() else {"undo": 0, "redo": 0, "undo_label": None, "redo_label": None})

    @routes.post("/api/projects/{pid}/firmware")
    async def firmware_make(request):
        """The firmware starter from the schematic (tw/firmware.py): pins.h, PINS.md, a bring-up sketch."""
        from tw import firmware
        from tw.checks.context import Context
        rt = app.rt(request.match_info["pid"])
        tw = rt.p.tw
        if not tw.has_sch():
            return err("there is no schematic yet")
        out = await asyncio.to_thread(lambda: firmware.write(tw, Context(tw, offline=True).netlist, rt.p.name))
        if not out:
            return err("no microcontroller in the schematic")
        return jresp({"written": out})

    @routes.get("/api/projects/{pid}/datasheets")
    async def datasheets_list(request):
        """The project's data sheet library (docs/datasheets): the PDFs and the pin tables read from them."""
        from tw import datasheets
        rt = app.rt(request.match_info["pid"])
        return jresp({"items": await asyncio.to_thread(datasheets.library, rt.p.tw)})

    @routes.post("/api/projects/{pid}/datasheets")
    async def datasheets_save(request):
        """Save a part's data sheet into the project: {ref} (its link from the part's fields or LCSC)."""
        from . import partinfo
        from tw import datasheets
        rt = app.rt(request.match_info["pid"])
        body = await request.json()
        ref = str(body.get("ref") or "")
        info = await asyncio.to_thread(lambda: partinfo.part_info(rt.p, rt.board() if rt.p.tw.has_pcb() else None, ref, True)) if ref else None
        if body.get("ref") and not info:
            return err(f"no part {body.get('ref')}", 404)
        url = str(body.get("url") or (info or {}).get("datasheet") or "")
        try:
            f = await asyncio.to_thread(datasheets.fetch, rt.p.tw, url, (info or {}).get("mpn") or str(body.get("mpn") or ""),
                                        (info or {}).get("lcsc") or str(body.get("lcsc") or ""))
        except (ValueError, OSError) as e:
            return err(f"could not save the data sheet: {e}")
        rt.hub.emit("datasheets")
        return jresp({"saved": f})

    @routes.get("/api/projects/{pid}/simulate")
    async def simulate_get(request):
        """What the Simulate tab can run: the supply rails (voltage, declared current), the signal nets (fast ones
        first, with their kind), the parts known to get warm, and the last result of each kind (build/sim)."""
        rt = app.rt(request.match_info["pid"])
        p = rt.p.reload()

        def work():
            out = {"rails": [], "signals": [], "heat": [], "board": p.tw.has_pcb(), "schematic": p.tw.has_sch(), "last": {}}
            if not (p.tw.has_pcb() and p.tw.has_sch()):
                return out
            from tw.checks.context import Context
            from tw.checks.power import rail_voltages
            from tw.checks.power_layout import _rail_current
            from tw.checks.signal import fast_nets
            from tw import fields
            ctx = Context(p.tw, offline=True)
            b = ctx.board
            volts = rail_voltages(ctx)
            grounds = set(ctx.ground_nets())
            for r in sorted(ctx.power_nets()):
                full = next((n for n in b.nets if n.rsplit("/", 1)[-1] == r), None)
                if full is None:
                    continue
                out["rails"].append({"net": r, "volts": volts.get(r), "amps": _rail_current(ctx, full)})
            fast = fast_nets(ctx, b)
            routed = {t.net for t in b.tracks}
            sig = []
            for n in sorted(routed):
                s_ = n.rsplit("/", 1)[-1]
                if not n or s_ in grounds or s_ in ctx.power_nets() or n.startswith("unconnected-"):
                    continue
                sig.append({"net": s_, "kind": fast.get(n, "")})
            out["signals"] = sorted(sig, key=lambda x: (not x["kind"], x["net"]))
            out["heat"] = fields.heat_sources(ctx)
            d = os.path.join(p.tw.build, "sim")
            for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
                if f.endswith(".json"):
                    try:
                        out["last"][f[:-5]] = json.load(open(os.path.join(d, f)))
                    except (OSError, ValueError):
                        pass
            return out
        return jresp(await asyncio.to_thread(work))

    @routes.post("/api/projects/{pid}/simulate/{kind}")
    async def simulate_run(request):
        """Run one: drop {net, amps?, loads?, source?} | heat {sources?, ambient?, air?} | return {net} |
        signal {net, rise_ns?, rs?, series?, receiver?} | pdn {net, ripple?, step?} | circuit {name, refs, sources,
        loads, extra, analysis, probes}. The summary (lines and numbers) is kept in build/sim/<kind>.json for Claude."""
        kind = request.match_info["kind"]
        rt = app.rt(request.match_info["pid"])
        body = await request.json()
        p = rt.p.reload()
        if kind not in ("drop", "heat", "return", "signal", "pdn", "circuit"):
            return err("no such simulation", 404)
        if not p.tw.has_pcb() and kind != "circuit":
            return err("there is no board yet", 404)

        def work():
            from tw.checks.context import Context
            from tw import fields, sigint, blocksim
            if kind == "circuit":
                info = blocksim.run(p.tw, str(body.get("name") or "block"), [str(r) for r in body.get("refs") or []],
                                    body.get("sources") or [], body.get("loads") or [], body.get("extra") or [],
                                    body.get("analysis") or None, body.get("probes") or [], body.get("title") or "")
                return info, {"lines": info.get("lines") or [], "notes": info.get("notes"), "name": info.get("name")}
            ctx = Context(p.tw, offline=True)
            if kind == "drop":
                r = fields.ir_drop(ctx, str(body.get("net") or ""), body.get("amps"), body.get("loads") or None, body.get("source") or None)
            elif kind == "heat":
                auto = {s["ref"]: s for s in fields.heat_sources(ctx)}
                for s_ in body.get("sources") or []:
                    if s_.get("ref"):
                        auto[str(s_["ref"])] = {"ref": str(s_["ref"]), "watts": float(s_.get("watts") or 0), "why": s_.get("why") or "set by you"}
                r = fields.heat(ctx, [s_ for s_ in auto.values() if s_["watts"] > 0], body.get("ambient"), body.get("air") or "still")
            elif kind == "return":
                r = sigint.return_paths(ctx, str(body.get("net") or ""))
            elif kind == "signal":
                net = str(body.get("net") or "")
                r = sigint.reflections(ctx, net, body.get("rise_ns"), float(body.get("rs") or 25), body.get("series") or None,
                                       body.get("receiver") or None)
                r["crosstalk"] = sigint.crosstalk(ctx, net, r["rise_ns"])
                r["lines"] = r["lines"] + r["crosstalk"]["lines"][:3]
            else:
                r = sigint.pdn(ctx, str(body.get("net") or ""), float(body.get("ripple") or 0.05), body.get("step"))
            keep = {k: v for k, v in r.items() if k in ("net", "lines", "loads", "hot", "parts", "sources", "max_c", "result",
                                                        "fix", "peaks", "target", "caps", "gaps", "vias", "max_density", "assumed")}
            return r, keep
        try:
            res, keep = await asyncio.to_thread(work)
        except ValueError as e:
            return err(str(e), 422)
        d = os.path.join(p.tw.build, "sim")
        os.makedirs(d, exist_ok=True)
        keep["at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        keep["kind"] = kind
        with open(os.path.join(d, f"{kind}.json"), "w") as f:
            json.dump(keep, f, indent=1, default=str)
        if kind == "circuit":
            rt.hub.emit("sims.changed")
        rt.user_changes.append(f"simulate ({kind}, by the user): " + "; ".join((keep.get("lines") or [])[:2]))
        return jresp(res)

    @routes.get("/api/projects/{pid}/make")
    async def make_get(request):
        """Parts > Make: the BOM's health, the test points, the drawings, the panel, the enclosure, the block library."""
        from . import bomhealth, blocks
        rt = app.rt(request.match_info["pid"])
        p = rt.p.reload()

        def work():
            out = {"board": p.tw.has_pcb(), "schematic": p.tw.has_sch(), "blocks": blocks.items(), "files": {},
                   "enclosure": p.cfg.get("enclosure") or None}
            if p.tw.has_sch():
                try:
                    out["bom"] = bomhealth.health(p.tw, rt.board() if p.tw.has_pcb() else None)
                except Exception as e:
                    out["bom"] = {"score": None, "lines": [f"The BOM could not be read: {e}"], "rows": []}
            if p.tw.has_pcb() and p.tw.has_sch():
                from tw.checks.context import Context
                from tw import testpoints
                out["testpoints"] = testpoints.plan(Context(p.tw, offline=True), (p.cfg.get("make") or {}).get("tp_side", "B"))
            for sub in ("docs", "panel", "enclosure"):
                d = os.path.join(p.tw.build, sub)
                for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
                    if f.endswith((".pdf", ".zip", ".scad", ".svg", ".step")) and ("fab-drawing" in f or "assembly-" in f or sub != "docs"):
                        out["files"][f] = {"path": f"build/{sub}/{f}", "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(os.path.getmtime(os.path.join(d, f))))}
            return out
        return jresp(await asyncio.to_thread(work))

    @routes.post("/api/projects/{pid}/make/{kind}")
    async def make_run(request):
        """drawings {which: fab | assembly} | panel {nx, ny, rail, rails, fiducials, tooling} | enclosure {w, l, h,
        standoff, gap, lid} (fit, the OpenSCAD file, and the board's STEP) | testpoints {side}."""
        kind = request.match_info["kind"]
        rt = app.rt(request.match_info["pid"])
        body = await request.json()
        p = rt.p.reload()
        if not p.tw.has_pcb():
            return err("there is no board yet", 404)

        def work():
            from tw.checks.context import Context
            if kind == "drawings":
                from tw import drawings
                if body.get("which") == "assembly":
                    files = drawings.assembly(p.tw)
                    return {"files": [os.path.relpath(f, p.root) for f in files], "lines": [f"{len(files)} assembly drawing{'s' if len(files) != 1 else ''}"]}
                r = drawings.fab_drawing(p.tw)
                return {"files": [os.path.relpath(r["pdf"], p.root)], "lines": ["The fab drawing: outline, stack-up, drills and notes"], "notes": r["notes"]}
            if kind == "panel":
                from tw import panel
                r = panel.make(p.tw, body.get("nx", 2), body.get("ny", 2), float(body.get("rail", 5)), body.get("rails", "tb"),
                               body.get("fiducials", True), body.get("tooling", True))
                return {"files": [os.path.relpath(f, p.root) for f in (r["pcb"], r["zip"], r["svg"]) if f], "lines": r["lines"],
                        "svg": os.path.relpath(r["svg"], p.root) if r["svg"] else None, "size": r["size"]}
            if kind == "enclosure":
                from tw import enclosure
                box = {k: float(body[k]) for k in ("w", "l", "h") if body.get(k)}
                if len(box) < 3:
                    raise ValueError("the box's inside: width, length and height in mm")
                opts = {k: float(body[k]) for k in ("standoff", "gap", "lid") if body.get(k) is not None}
                txt, r = enclosure.scad(Context(p.tw, offline=True), box, **opts)
                d = os.path.join(p.tw.build, "enclosure")
                os.makedirs(d, exist_ok=True)
                from tw.drawings import fname
                scad = os.path.join(d, f"{fname(p.tw)}-enclosure.scad")
                open(scad, "w").write(txt)
                files = [os.path.relpath(scad, p.root)]
                if body.get("step"):
                    from tw.outputs import Outputs
                    o = Outputs(p.tw)
                    o.step()
                    st = [f for f in os.listdir(o.fab) if f.endswith(".step")]
                    files += [os.path.relpath(os.path.join(o.fab, f), p.root) for f in st]
                p.cfg["enclosure"] = {**box, **opts}
                p.save()
                return {**r, "files": files}
            if kind == "testpoints":
                from tw import testpoints
                p.cfg.setdefault("make", {})["tp_side"] = "F" if body.get("side") == "F" else "B"
                p.save()
                return testpoints.plan(Context(p.tw, offline=True), p.cfg["make"]["tp_side"])
            raise LookupError(kind)
        try:
            res = await asyncio.to_thread(work)
        except LookupError:
            return err("no such thing to make", 404)
        except ValueError as e:
            return err(str(e), 422)
        rt.user_changes.append(f"make ({kind}, by the user): " + "; ".join((res.get("lines") or [])[:2]))
        return jresp(res)

    @routes.get("/api/blocks")
    async def blocks_list(request):
        from . import blocks
        return jresp({"items": blocks.items()})

    @routes.get("/api/blocks/{bid}")
    async def blocks_get(request):
        from . import blocks
        try:
            b = blocks.get(request.match_info["bid"])
        except KeyError:
            return err("no such block", 404)
        return jresp({**b, "text": blocks.text(b)})

    @routes.delete("/api/blocks/{bid}")
    async def blocks_delete(request):
        from . import blocks
        try:
            blocks.remove(request.match_info["bid"])
        except KeyError:
            return err("no such block", 404)
        return jresp({"ok": True})

    @routes.post("/api/projects/{pid}/blocks")
    async def blocks_save(request):
        """{name, description, refs}: save those parts of this design as a block."""
        from . import blocks
        rt = app.rt(request.match_info["pid"])
        body = await request.json()
        try:
            b = await asyncio.to_thread(blocks.save, rt.p.reload(), body.get("refs") or [], body.get("name"), body.get("description") or "")
        except ValueError as e:
            return err(str(e))
        return jresp({"ok": True, "id": b["id"], "parts": len(b["parts"]), "ports": [x["name"] for x in b["ports"]]})

    @routes.get("/api/projects/{pid}/sims")
    async def sims_list(request):
        """The simulations kept in docs/sim (Claude's simulate tool): each one's probes, results, pass criterion and
        verdict, and its files."""
        from tw import sim
        from . import signoff
        rt = app.rt(request.match_info["pid"])

        def runs():
            items = sim.runs(os.path.join(rt.p.root, "docs", "sim"))
            if any(x.get("requirement") for x in items):
                names = {r["id"]: r["text"] for r in signoff.requirements(rt.p.reload())}
                for x in items:
                    x["requirement_text"] = names.get(x.get("requirement") or "")
            return items
        return jresp({"items": await asyncio.to_thread(runs)})

    @routes.post("/api/projects/{pid}/sims/{name}")
    async def sims_act(request):
        """{action: run | delete}: run a kept simulation again (its netlist as docs/sim/<name>.cir has it now), or
        remove it. The evidence that cites it follows: its result, or its removal."""
        from tw import sim
        from . import signoff
        rt = app.rt(request.match_info["pid"])
        name = request.match_info["name"]
        d = os.path.join(rt.p.root, "docs", "sim")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name) or not os.path.exists(os.path.join(d, name + ".cir")):
            return err("no such simulation", 404)
        body = await request.json()
        ref = f"docs/sim/{name}.cir"
        if body.get("action") == "delete":
            for ext in ("cir", "csv", "svg", "json"):
                if os.path.exists(os.path.join(d, f"{name}.{ext}")):
                    os.remove(os.path.join(d, f"{name}.{ext}"))
            n = await asyncio.to_thread(signoff.follow_sim, rt.p.reload(), ref, remove=True)
            rt.hub.emit("sims")
            if n:
                rt.hub.emit("signoff")
            return jresp({"removed": name, "evidence": n})
        if body.get("action") != "run":
            return err("action: run or delete")
        try:
            old = json.load(open(os.path.join(d, name + ".json")))
        except (OSError, ValueError):
            old = {}
        text = open(os.path.join(d, name + ".cir"), encoding="utf-8").read()
        info = await asyncio.to_thread(sim.simulate, d, name, text, old.get("probes") or [], old.get("title") or name,
                                       old.get("criteria"), old.get("requirement") or "")
        n = await asyncio.to_thread(signoff.follow_sim, rt.p.reload(), ref, status="fail" if info.get("error") else info.get("status") or "ok",
                                    verdict=info.get("check") or ("FAIL: the simulation did not run" if info.get("error") else None))
        rt.hub.emit("sims")
        if n:
            rt.hub.emit("signoff")
        return jresp(info)

    @routes.get("/api/projects/{pid}/mentions")
    async def mentions(request):
        """What the message box can @-mention: the parts (from the schematic, with where each is on the board),
        the nets (with their kind), the sheets and the project's files."""
        rt = app.rt(request.match_info["pid"])

        def build():
            out = {"parts": [], "nets": [], "sheets": [], "files": []}
            pos = {}
            if rt.p.tw.has_pcb():
                try:
                    b = rt.board()
                    pos = {f.ref: {"x": round(f.x, 2), "y": round(f.y, 2), "side": f.side, "fp": f.lib_id.split(":")[-1]} for f in b.fp_list}
                    board_nets = list(b.nets)
                except Exception:
                    board_nets = []
            else:
                board_nets = []
            seen = set()
            js = rt.schematic_json() if rt.p.tw.has_sch() else None
            for sh in (js or {}).get("sheets", []):
                out["sheets"].append({"name": sh.get("name") or "root", "path": sh.get("name_path"), "file": sh.get("file")})
                for y in sh.get("symbols", []):
                    ref = y.get("ref") or ""
                    if y.get("power") or not ref or ref.startswith("#") or ref in seen:
                        continue
                    seen.add(ref)
                    out["parts"].append({"ref": ref, "val": y.get("val") or "", "sheet": sh.get("name_path"), "sheet_name": sh.get("name") or "root", **pos.get(ref, {})})
            for ref, pp in pos.items():
                if ref not in seen:
                    out["parts"].append({"ref": ref, "val": "", **pp})
            try:
                from tw import netmodel
                m = netmodel.for_project(rt.p.tw)
                out["nets"] = [{"name": n, "kind": r.get("kind")} for n, r in m.records.items() if n]
            except Exception:
                out["nets"] = [{"name": n} for n in board_nets if n]
            root = rt.p.root
            for d, dirs, files in os.walk(root):
                dirs[:] = [x for x in dirs if not x.startswith(".") and x not in ("build", "tools", "node_modules", "__pycache__", "backups")]
                for f in files:
                    if f.startswith(".") or f.endswith((".pyc", "-bak", ".lck")):
                        continue
                    out["files"].append(os.path.relpath(os.path.join(d, f), root))
                    if len(out["files"]) >= 400:
                        break
            out["parts"].sort(key=lambda x: (re.sub(r"\d+", "", x["ref"]), int(re.sub(r"\D", "", x["ref"]) or 0)))
            return out
        return jresp(await asyncio.to_thread(build))

    @routes.get("/api/projects/{pid}/schematic/svg")
    async def schematic_svg(request):
        rt = app.rt(request.match_info["pid"])
        f = await asyncio.to_thread(rt.svg_for, request.query.get("sheet", "/"))
        if not f:
            return err("no sheets plotted", 404)
        return web.FileResponse(f, headers={"Content-Type": "image/svg+xml", "Cache-Control": "no-cache"})

    @routes.get("/api/projects/{pid}/schematic/style")
    async def schematic_style(request):
        """How the sheets are joined now, and the project's choice."""
        rt = app.rt(request.match_info["pid"])
        from tw.sch import style as schstyle
        d = await asyncio.to_thread(schstyle.detect, rt.p.tw.sch) if rt.p.tw.has_sch() else None
        return jresp({"style": rt.p.schematic_style(), "detected": d})

    @routes.get("/api/projects/{pid}/schematic/conventions")
    async def schematic_conventions(request):
        """The project's schematic conventions, the choices for each, and which ones the user set."""
        rt = app.rt(request.match_info["pid"])
        from tw.sch import conventions
        opts = [{"key": k, "label": o["label"], "default": o["default"],
                 "choices": [{"value": v, "label": t, "about": a} for v, t, a in o["choices"]]} for k, o in conventions.OPTIONS.items()]
        return jresp({"options": opts, "values": conventions.get(rt.p.cfg), "chosen": sorted(conventions.chosen(rt.p.cfg)),
                      "has_sch": rt.p.tw.has_sch()})

    @routes.post("/api/projects/{pid}/schematic/style")
    async def schematic_style_set(request):
        """Redraw the sheets hierarchical or flat. The redrawn sheets are checked against KiCad's netlist
        before anything is written (a checkpoint is taken first); the choice is kept for new sheets."""
        pid = request.match_info["pid"]
        rt = app.rt(pid)
        body = await request.json()
        target = body.get("style")
        if target not in ("hierarchical", "flat"):
            return err("style must be hierarchical or flat")
        if not rt.p.tw.has_sch():
            await asyncio.to_thread(rt.p.set_schematic_style, target)
            rt.hub.emit("project.changed", summary=rt.p.summary())
            return jresp({"ok": True, "changed": [], "message": f"New sheets will be drawn {target}."})
        if app.agent_busy(pid):
            return err("Claude is working on this design. Change the style when it finishes, or ask Claude to.", 409)
        from tw.sch import style as schstyle
        from tw import kicad as twkicad

        def work():
            if history.has_repo(rt.p.root):
                history.snapshot(rt.p.root, f"Before redrawing the schematic {target}")
            rt.mark_self(30)
            r = schstyle.convert(rt.p.tw.sch, target)
            if r["ok"]:
                rt.p.set_schematic_style(target)
                if r["changed"]:
                    twkicad.netlist(rt.p.tw.sch, os.path.join(rt.p.tw.build, f"{rt.p.tw.stem}.net"))
            return r
        try:
            r = await asyncio.to_thread(work)
        except Exception as e:
            app.log(traceback.format_exc())
            return err(f"the redraw failed: {e}", 500)
        if r.get("changed"):
            rt.hub.emit("schematic.changed", source="tracewright", files=r["changed"])
        rt.hub.emit("project.changed", summary=rt.p.summary())
        return jresp(r)

    @routes.get("/api/projects/{pid}/nets")
    async def nets_list(request):
        """Every net's record from the net model (tw.netmodel): its kind (ground, a supply with its voltage
        and current, one half of a pair, a clock, an RF line...), what was declared and what is inferred,
        and a short tag for lists. A pair's halves carry skew_mm and skew_why: how far apart in length the
        halves may be, the budget the hs.pairs check holds them to."""
        rt = app.rt(request.match_info["pid"])
        from tw import netmodel
        from tw.checks.context import Context
        from tw.checks import signal

        def work():
            p = rt.p
            p.reload()
            ctx = Context(p.tw, offline=True)
            m = netmodel.for_context(ctx)
            out = {}
            for n, r in m.records.items():
                src = r.get("source") or {}
                out[n] = {**{k: v for k, v in r.items() if k != "source"}, "tag": netmodel.tag(r),
                          "declared": sorted(k for k, v in src.items() if v != "inferred")}
                if r.get("kind") == "pair" and r.get("pair"):
                    pn = signal.find_pairs([n, r["pair"]])          # which half is the positive one, as the check sees it
                    tol = signal.pair_tolerance(ctx, *pn[0]) if pn else None
                    if tol:
                        out[n]["skew_mm"], out[n]["skew_why"] = tol["mm"], tol["why"]
            return {"nets": out, "summary": m.summary(), "unused": [k for k, _ in m.unused_keys()]}
        return jresp(await asyncio.to_thread(work))

    @routes.patch("/api/projects/{pid}/nets/{net}")
    async def net_declare(request):
        """Declare what a net is (kind, voltage, current, pair, impedance, class, note); a field sent as
        null or "" is removed, back to what names and pins say."""
        rt = app.rt(request.match_info["pid"])
        from tw import netmodel
        net = request.match_info["net"]
        body = await request.json()
        fields = {k: body[k] for k in netmodel.FIELDS if k in body}
        try:
            rec = await asyncio.to_thread(netmodel.declare, rt.p.tw, net, **fields)
        except ValueError as e:
            return err(str(e))
        rt.p.reload()
        rt.user_changes.append(f"net {net} declared as {json.dumps(rec)}" if rec else f"net {net}: declarations cleared")
        rt.hub.emit("nets.changed", net=net)
        return jresp({"net": net, "declared": rec})

    @routes.get("/api/projects/{pid}/parts/{ref}")
    async def part_info(request):
        """One part, everything known about it (the inspector). fetch=1 looks it up at LCSC when the cache
        has no description, parameters or photos yet."""
        rt = app.rt(request.match_info["pid"])
        from . import partinfo
        ref = request.match_info["ref"]
        fetch = request.query.get("fetch") == "1"
        info = await asyncio.to_thread(lambda: partinfo.part_info(rt.p, rt.board() if rt.p.tw.has_pcb() else None, ref, fetch))
        if info is None:
            return err(f"no part {ref}", 404)
        return jresp(info)

    @routes.get("/api/projects/{pid}/parts/{ref}/photo")
    async def part_photo(request):
        """LCSC's product photo of the part, kept in the project's sourcing cache after the first look."""
        rt = app.rt(request.match_info["pid"])
        from . import partinfo
        code = re.sub(r"[^A-Za-z0-9]", "", request.query.get("lcsc", ""))
        try:
            i = int(request.query.get("i", "0"))
        except ValueError:
            i = 0
        if not code:
            return err("no LCSC code", 400)
        try:
            f = await asyncio.to_thread(partinfo.photo, rt.p, code, i)
        except Exception as e:
            return err(f"the photo could not be fetched: {e}", 502)
        if not f:
            return err("no photo", 404)
        return web.FileResponse(f, headers={"Cache-Control": "max-age=86400"})

    @routes.get("/api/projects/{pid}/model.glb")
    async def model_glb(request):
        """The board as a 3D model (binary glTF) for the interactive 3D view; parts=0 for the bare board."""
        rt = app.rt(request.match_info["pid"])
        try:
            f = await asyncio.to_thread(rt.glb, request.query.get("parts", "1") != "0")
        except Exception as e:
            return err(f"the 3D export failed: {e}", 500)
        if not f:
            return err("no board yet", 404)
        return web.FileResponse(f, headers={"Content-Type": "model/gltf-binary", "Cache-Control": "no-cache"})

    # ------------------------------------------------------------------ attachments (the chat's message box)
    @routes.post("/api/projects/{pid}/attach")
    async def attach_upload(request):
        """Files attached to a message (dropped or pasted in the browser): each into its place in the
        project by type (attach.py), recorded until the message goes out."""
        from . import attach
        rt = app.rt(request.match_info["pid"])
        out = []
        reader = await request.multipart()
        async for part in reader:
            if not part.filename:
                continue
            kind, dest = attach.destination(rt.p, part.filename)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            await _stream(part, dest, 512 * 1024 ** 2)
            rec = await asyncio.to_thread(attach.finish, rt.p, kind, dest)
            rt.attached[rec["path"]] = rec
            out.append(rec)
        return jresp({"attached": out})

    @routes.post("/api/projects/{pid}/attach-paths")
    async def attach_paths(request):
        """Files and folders from this Mac attached to a message (dropped on the Mac app, or picked)."""
        from . import attach
        if app.server_mode:
            return err("on a server, upload the files instead")
        rt = app.rt(request.match_info["pid"])
        body = await request.json()
        out = []
        for src in body.get("paths") or []:
            if not os.path.exists(os.path.expanduser(str(src))):
                continue
            rec = await asyncio.to_thread(attach.add_path, rt.p, src)
            rt.attached[rec["path"]] = rec
            out.append(rec)
        return jresp({"attached": out})

    @routes.post("/api/projects/{pid}/attach/remove")
    async def attach_remove(request):
        """An attachment taken back before sending: its files go (only files it added) and its library entry."""
        from . import attach
        rt = app.rt(request.match_info["pid"])
        rec = rt.attached.pop((await request.json()).get("path", ""), None)
        if rec:
            await asyncio.to_thread(attach.remove, rt.p, rec)
        return jresp({"removed": bool(rec)})

    @routes.post("/api/projects/{pid}/add-files")
    async def add_files(request):
        """Files from this Mac into the project (dropped on the window): copied into `dir` (uploads/)."""
        if app.server_mode:
            return err("on a server, upload the files instead")
        rt = app.rt(request.match_info["pid"])
        body = await request.json()
        target = (body.get("dir") or "uploads").strip("/") or "uploads"
        out = []
        for src in body.get("paths") or []:
            src = os.path.expanduser(str(src))
            if not os.path.exists(src):
                continue
            dest = safe_join(rt.p.root, os.path.join(target, os.path.basename(src.rstrip("/"))))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if os.path.isdir(src):
                await asyncio.to_thread(shutil.copytree, src, dest, dirs_exist_ok=True)
            else:
                await asyncio.to_thread(shutil.copy2, src, dest)
            out.append(os.path.relpath(dest, rt.p.root))
        return jresp({"saved": out})

    @routes.post("/api/projects/{pid}/selection")
    async def selection(request):
        rt = app.rt(request.match_info["pid"])
        body = await request.json()
        rt.selection["app"] = body.get("items", [])
        return jresp({"ok": True})

    @routes.get("/api/projects/{pid}/annotations")
    async def get_ann(request):
        rt = app.rt(request.match_info["pid"])
        return jresp(_annotations(rt.p))

    @routes.post("/api/projects/{pid}/annotations")
    async def set_ann(request):
        rt = app.rt(request.match_info["pid"])
        body = await request.json()
        path = os.path.join(rt.p.state_dir(), "annotations.json")
        items = body.get("items", [])
        json.dump(items, open(path, "w"), indent=1)
        rt.hub.emit("annotations", items=items)
        return jresp(items)

    # ------------------------------------------------------------------ review flags
    def _review(rt):
        from .review import Review
        return Review(rt.p)

    def _review_changed(rt):
        rt.hub.emit("review.changed", flags=_review(rt).flags())

    @routes.get("/api/projects/{pid}/review")
    async def review_list(request):
        rt = app.rt(request.match_info["pid"])
        return jresp({"flags": _review(rt).flags()})

    @routes.post("/api/projects/{pid}/review")
    async def review_add(request):
        rt = app.rt(request.match_info["pid"])
        body = await request.json()
        try:
            f = await asyncio.to_thread(_review(rt).add, body.get("view", "board"), body.get("text", ""), body.get("where"),
                                        body.get("snapshot"), body.get("source", "user"), body.get("ask", "request"), body.get("marks"))
        except ValueError as e:
            return err(str(e))
        _review_changed(rt)
        return jresp(f)

    @routes.patch("/api/projects/{pid}/review/{fid}")
    async def review_update(request):
        rt = app.rt(request.match_info["pid"])
        body = await request.json()
        f = await asyncio.to_thread(_review(rt).update, request.match_info["fid"],
                                    **{k: v for k, v in body.items() if k in ("text", "status", "where", "resolution", "snapshot", "ask", "marks")})
        _review_changed(rt)
        return jresp(f)

    @routes.post("/api/projects/{pid}/review/{fid}/reply")
    async def review_reply(request):
        """The user answers in a flag's thread ({text, anyway?}): it goes to Claude with the next send."""
        rt = app.rt(request.match_info["pid"])
        body = await request.json()
        try:
            f = await asyncio.to_thread(_review(rt).reply, request.match_info["fid"], body.get("text", ""), "you", None,
                                        bool(body.get("anyway")))
        except KeyError:
            return err("no such flag", 404)
        except ValueError as e:
            return err(str(e))
        _review_changed(rt)
        return jresp(f)

    @routes.delete("/api/projects/{pid}/review/{fid}")
    async def review_delete(request):
        rt = app.rt(request.match_info["pid"])
        r = _review(rt)
        if request.match_info["fid"] == "resolved":
            n = await asyncio.to_thread(r.clear_resolved)
        else:
            await asyncio.to_thread(r.delete, request.match_info["fid"])
            n = 1
        _review_changed(rt)
        return jresp({"removed": n})

    @routes.get("/api/projects/{pid}/review/{fid}/snapshot")
    async def review_snapshot(request):
        rt = app.rt(request.match_info["pid"])
        f = _review(rt).snapshot_path(request.match_info["fid"])
        if not os.path.isfile(f):
            return err("no snapshot", 404)
        return web.FileResponse(f, headers={"Cache-Control": "no-cache", "Content-Type": "image/png"})

    @routes.post("/api/projects/{pid}/review/send")
    async def review_send(request):
        """Send flags to Claude in one message, with their snapshots: the ids given, or every open one."""
        rt = app.rt(request.match_info["pid"])
        body = await request.json()
        r = _review(rt)
        ids = body.get("ids") or []
        flags = [f for f in r.flags() if (f["id"] in ids if ids else f["status"] == "open")]
        if not flags:
            return err("no open flags to send")
        a = app.agent(rt.p.id)
        if a.busy:
            return err("Claude is still working: stop it first, or send the flags when it has finished")
        images = [r.snapshot_path(f["id"]) for f in flags if f.get("snapshot") and os.path.isfile(r.snapshot_path(f["id"]))]
        text = r.message(flags, body.get("note", ""))
        att = [{"kind": "flag", "id": f["id"], "n": f["n"], "label": f"#{f['n']} {f['text'][:60]}", "view": f["view"]} for f in flags]
        sid = await a.send(text, body.get("sid"), att, title=None, images=images[:10])
        r.mark_sent([f["id"] for f in flags])
        _review_changed(rt)
        return jresp({"sid": sid, "sent": len(flags)})

    # ------------------------------------------------------------------ checks
    @routes.get("/api/projects/{pid}/checks")
    async def get_checks(request):
        rt = app.rt(request.match_info["pid"])
        f = os.path.join(rt.p.tw.build, "checks.json")
        if not os.path.exists(f):
            from tw.checks import load_all, REGISTRY
            load_all()
            return jresp({"never_run": True, "checks": [{"id": c.id, "title": c.title, "group": c.group, "doc": c.doc,
                                                         "status": "not run", "findings": []} for c in REGISTRY]})
        d = json.load(open(f))
        from tw.checks import load_all, REGISTRY
        load_all()
        docs = {c.id: c.doc for c in REGISTRY}
        for c in d.get("checks", []):
            c["doc"] = docs.get(c["id"], "")
        have = {c["id"] for c in d.get("checks", [])}
        disabled = set((rt.p.tw.cfg.get("checks") or {}).get("disabled", []))
        for c in REGISTRY:                            # checks added since the last run (an app update)
            if c.id not in have and c.default and c.id not in disabled and not c.id.startswith("project."):
                d.setdefault("checks", []).append({"id": c.id, "title": c.title, "group": c.group, "doc": c.doc,
                                                   "status": "not run", "findings": [], "new": True})
        d["running"] = rt.checks_lock.locked()
        return jresp(d)

    @routes.post("/api/projects/{pid}/checks/run")
    async def run_checks(request):
        rt = app.rt(request.match_info["pid"])
        body = await request.json() if request.can_read_body else {}
        if rt.checks_lock.locked():
            return err("checks are already running")
        import threading
        from tw.checks.runner import run_all
        stop = threading.Event()

        def prog(c, r):
            rt.mark_self(30)
            rt.hub.emit("checks.progress", id=c.id, title=c.title, status=r["status"], n=len(r["findings"]))

        async def job():
            async with rt.checks_lock:
                rt.checks_stop = stop
                rt.hub.emit("checks.start", only=body.get("only"))
                try:
                    res = await asyncio.to_thread(run_all, rt.p.tw, only=body.get("only") or None,
                                                  refresh=bool(body.get("refresh")), progress=prog, stop=stop)
                    rt.hub.emit("checks.done", counts=res["counts"], stopped=bool(res.get("stopped")))
                except Exception as e:
                    traceback.print_exc()
                    rt.hub.emit("checks.done", error=f"{type(e).__name__}: {e}")
                finally:
                    rt.mark_self(4)
                    if rt.checks_stop is stop:
                        rt.checks_stop = None
        app.jobs[("checks", rt.p.id)] = asyncio.ensure_future(job())
        return jresp({"started": True})

    @routes.post("/api/projects/{pid}/checks/stop")
    async def stop_checks(request):
        rt = app.rt(request.match_info["pid"])
        if rt.checks_stop is None:
            return jresp({"stopped": False})
        rt.checks_stop.set()
        return jresp({"stopped": True})

    # ------------------------------------------------------------------ design rules
    def _netlist_for(rt):
        try:
            from tw.checks.context import Context
            return Context(rt.p.tw).netlist if rt.p.tw.has_sch() else None
        except Exception:
            return None

    @routes.get("/api/projects/{pid}/rules")
    async def rules_get(request):
        rt = app.rt(request.match_info["pid"])
        from . import rules as ruleslib
        if not rt.p.tw.pro:
            return jresp({"empty": True})
        d = await asyncio.to_thread(lambda: ruleslib.state(rt.p.tw, rt.board(), _netlist_for(rt)))
        d["kicad_open"] = bool(rt.live.get("running") and rt.live.get("board_open"))
        return jresp(d)

    @routes.post("/api/projects/{pid}/rules/check")
    async def rules_check(request):
        from tw import dru as twdru
        body = await request.json()
        rules, problems = twdru.check(body.get("dru", ""))
        return jresp({"rules": rules, "problems": problems})

    @routes.post("/api/projects/{pid}/rules/probe")
    async def rules_probe(request):
        rt = app.rt(request.match_info["pid"])
        from tw import dru as twdru
        body = await request.json()
        if not rt.p.tw.has_pcb():
            return err("no board to try the rules on")
        ok, why = await asyncio.to_thread(twdru.kicad_accepts, rt.p.tw.pcb, body.get("dru", ""))
        return jresp({"ok": ok, "why": why})

    @routes.put("/api/projects/{pid}/rules")
    async def rules_put(request):
        rt = app.rt(request.match_info["pid"])
        from . import rules as ruleslib
        body = await request.json()

        def work():
            if history.has_repo(rt.p.root):
                history.snapshot(rt.p.root, "Before editing the design rules")
            rt.mark_self(10)
            return ruleslib.save(rt.p.tw, body)
        ok, msgs = await asyncio.to_thread(work)
        if not ok:
            return jresp({"ok": False, "errors": msgs, "error": msgs[0] if msgs else "not saved"}, 400)
        rt.hub.emit("rules.changed")
        d = await asyncio.to_thread(lambda: ruleslib.state(rt.p.tw, rt.board(), _netlist_for(rt)))
        d["kicad_open"] = bool(rt.live.get("running") and rt.live.get("board_open"))
        return jresp({"ok": True, "warnings": msgs, "state": d})

    # ------------------------------------------------------------------ the board's timelapse
    @routes.get("/api/projects/{pid}/timelapse")
    async def timelapse_get(request):
        rt = app.rt(request.match_info["pid"])
        since = int(request.query.get("since", "0") or 0)

        def work():
            frames = rt.timelapse.frames(since)
            hist = 0
            if rt.p.tw.has_pcb() and history.has_repo(rt.p.root):
                r = subprocess.run(["git", "-C", rt.p.root, "log", "--format=%H", "--", os.path.relpath(rt.p.tw.pcb, rt.p.root)],
                                   capture_output=True, text=True, timeout=30)
                hist = len(r.stdout.split())
            return {"frames": frames, "count": since + len(frames), "history": hist, "building": bool(getattr(rt, "tl_building", False))}
        return jresp(await asyncio.to_thread(work))

    @routes.post("/api/projects/{pid}/timelapse/history")
    async def timelapse_history(request):
        rt = app.rt(request.match_info["pid"])
        if getattr(rt, "tl_building", False):
            return err("already rebuilding the timelapse")
        if not rt.p.tw.has_pcb():
            return err("no board yet")
        rt.tl_building = True

        async def job():
            try:
                pcb = os.path.relpath(rt.p.tw.pcb, rt.p.root)
                n = await asyncio.to_thread(rt.timelapse.from_history, pcb,
                                            lambda i, k: rt.hub.emit("timelapse.progress", done=i, total=k))
                b = await asyncio.to_thread(rt.board)
                if b is not None:
                    await asyncio.to_thread(rt.record_frame, b, "you")        # the board as it is now, if it moved on
                rt.hub.emit("timelapse.changed", commits=n)
            except Exception as e:
                traceback.print_exc()
                rt.hub.emit("timelapse.changed", error=str(e))
            finally:
                rt.tl_building = False
        app.jobs[("timelapse", rt.p.id)] = asyncio.ensure_future(job())
        return jresp({"started": True})

    @routes.post("/api/projects/{pid}/timelapse/video")
    async def timelapse_video(request):
        """The player's recording (the body, mp4 or webm), saved as build/timelapse/<project>-<time>.<ext>."""
        rt = app.rt(request.match_info["pid"])
        ext = "mp4" if request.query.get("ext") == "mp4" else "webm"
        d = os.path.join(rt.p.root, "build", "timelapse")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{rt.p.id}-timelapse-{time.strftime('%Y%m%d-%H%M%S')}.{ext}")
        size = 0
        with open(path, "wb") as f:
            async for chunk in request.content.iter_chunked(1 << 16):
                size += len(chunk)
                if size > 400 * 1024 * 1024:
                    f.close()
                    os.remove(path)
                    return err("the recording is too large")
                f.write(chunk)
        return jresp({"path": os.path.relpath(path, rt.p.root), "abs": path, "bytes": size})

    @routes.delete("/api/projects/{pid}/timelapse")
    async def timelapse_clear(request):
        rt = app.rt(request.match_info["pid"])
        await asyncio.to_thread(rt.timelapse.clear)
        b = await asyncio.to_thread(rt.board)
        if b is not None:
            await asyncio.to_thread(rt.record_frame, b, "start")
        rt.hub.emit("timelapse.changed")
        return jresp({"ok": True})

    # ------------------------------------------------------------------ bill of materials
    @routes.get("/api/projects/{pid}/bom")
    async def bom_get(request):
        rt = app.rt(request.match_info["pid"])
        from . import bom as bomlib
        from . import stock

        def build():
            d = bomlib.bom_data(rt.p.tw, rt.board())
            if not d.get("empty"):
                st = stock.status(rt.p.tw, data=d)
                by = {x["lcsc"]: x for x in st["rows"]}
                for r in d.get("rows", []):
                    x = by.get(r.get("lcsc"))
                    if x and r.get("assembled"):
                        r["stock_state"], r["need"] = x["state"], x["need"]
                d["stock"] = {k: st[k] for k in ("boards", "mode", "out", "low", "gone", "unknown", "oldest")}
            return d
        d = await asyncio.to_thread(build)
        d["looking_up"] = bool(getattr(rt, "bom_stop", None))
        return jresp(d)

    @routes.post("/api/projects/{pid}/stock/alternates")
    async def stock_alternates(request):
        """In-stock parts that can stand in for a placed part that runs short: {lcsc}."""
        from . import stock
        from tw.jlc import LookupFailed
        rt = app.rt(request.match_info["pid"])
        code = str((await request.json()).get("lcsc") or "")
        st = await asyncio.to_thread(stock.status, rt.p.tw)
        row = next((x for x in st["rows"] if x["lcsc"] == code), None)
        if not row:
            return err(f"no placed part with {code}", 404)
        try:
            res = await asyncio.to_thread(stock.alternates, rt.p.tw, row, st["boards"])
        except LookupFailed as e:
            return err(f"JLC did not answer: {e}", 502)
        return jresp({**res, "part": row})

    @routes.get("/api/projects/{pid}/bom.csv")
    async def bom_csv(request):
        rt = app.rt(request.match_info["pid"])
        from . import bom as bomlib
        kind = "full" if request.query.get("kind") == "full" else "jlc"
        d = await asyncio.to_thread(lambda: bomlib.bom_data(rt.p.tw, rt.board()))
        if d.get("empty"):
            return err("no schematic yet", 404)
        name = f"{rt.p.tw.stem or 'board'}-BOM{'-JLC' if kind == 'jlc' else ''}.csv"
        return web.Response(text=bomlib.csv_text(d, kind), content_type="text/csv",
                            headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-cache"})

    @routes.post("/api/projects/{pid}/bom/savings")
    async def bom_savings(request):
        """Basic-part equivalents for the Extended resistors and capacitors (JLC's fee per Extended part)."""
        from . import savings
        rt = app.rt(request.match_info["pid"])
        body = await request.json() if request.can_read_body else {}
        boards = max(1, min(1000, int(body.get("boards") or 5)))
        prog = lambda i, n, what: rt.hub.emit("savings.progress", done=i, total=n, what=what)
        try:
            r = await asyncio.to_thread(savings.suggestions, rt.p.tw, rt.board() if rt.p.tw.has_pcb() else None, boards, 90.0, prog)
        except Exception as e:
            app.log(traceback.format_exc())
            return err(f"{type(e).__name__}: {e}")
        r["message"] = savings.claude_message(r["items"]) if r["items"] else ""
        return jresp(r)

    @routes.post("/api/projects/{pid}/bom/lookup")
    async def bom_lookup(request):
        rt = app.rt(request.match_info["pid"])
        if getattr(rt, "bom_stop", None):
            return err("already looking up the parts")
        import threading
        from . import bom as bomlib
        body = await request.json() if request.can_read_body else {}
        stop = threading.Event()
        rt.bom_stop = stop

        def prog(done, total, code, res):
            rt.hub.emit("bom.lookup", done=done, total=total, code=code, result=res)

        async def job():
            try:
                d = await asyncio.to_thread(lambda: bomlib.bom_data(rt.p.tw, rt.board()))
                codes = body.get("codes") or sorted({r["lcsc"] for r in d.get("rows", []) if r["lcsc"] and r["assembled"]})
                rt.hub.emit("bom.lookup", done=0, total=len(codes))
                res = await asyncio.to_thread(bomlib.lookup, rt.p.tw, codes, prog, stop)
                bad = {k: v for k, v in res.items() if v != "ok"}
                rt.hub.emit("bom.lookup", done=len(codes), total=len(codes), finished=True, failed=len(bad),
                            stopped=stop.is_set(), note=next(iter(bad.values()), "") if bad else "")
                from . import stock
                news = await asyncio.to_thread(stock.record, rt.p.tw)
                if news:
                    app.stock_news(rt, news)
            except Exception as e:
                traceback.print_exc()
                rt.hub.emit("bom.lookup", finished=True, error=f"{type(e).__name__}: {e}")
            finally:
                rt.bom_stop = None
                rt.hub.emit("bom.changed")
        app.jobs[("bom", rt.p.id)] = asyncio.ensure_future(job())
        return jresp({"started": True})

    @routes.post("/api/projects/{pid}/bom/stop")
    async def bom_stop(request):
        rt = app.rt(request.match_info["pid"])
        if getattr(rt, "bom_stop", None):
            rt.bom_stop.set()
        return jresp({"ok": True})

    @routes.post("/api/projects/{pid}/selftest")
    async def selftest(request):
        rt = app.rt(request.match_info["pid"])
        from tw import kicad as twk
        rc, out = await asyncio.to_thread(_selftest)
        rt.hub.emit("notice", level="ok" if rc == 0 else "error", text=out.strip().splitlines()[-1] if out.strip() else "")
        return jresp({"ok": rc == 0, "output": out})

    # ------------------------------------------------------------------ files, history, outputs
    @routes.get("/api/projects/{pid}/files")
    async def files(request):
        rt = app.rt(request.match_info["pid"])
        root = rt.p.root
        base = safe_join(root, request.query.get("path", ""))
        out = []
        if not os.path.isdir(base):                       # a folder not made yet (firmware/, docs/): nothing in it
            return jresp({"path": os.path.relpath(base, root), "entries": [], "missing": True})
        for name in sorted(os.listdir(base)):
            if name in HIDDEN or name.endswith(".pyc"):
                continue
            full = os.path.join(base, name)
            st = os.stat(full)
            out.append({"name": name, "path": os.path.relpath(full, root), "dir": os.path.isdir(full), "size": st.st_size,
                        "mtime": st.st_mtime})
        out.sort(key=lambda e: (not e["dir"], e["name"].lower()))
        return jresp({"path": os.path.relpath(base, root) if base != root else "", "entries": out})

    @routes.get("/api/projects/{pid}/file")
    async def file(request):
        rt = app.rt(request.match_info["pid"])
        path = safe_join(rt.p.root, request.query.get("path", ""))
        if not os.path.isfile(path):
            return err("no such file", 404)
        ext = os.path.splitext(path)[1].lower()
        if request.query.get("raw") or ext not in TEXT_EXT and not path.endswith(("-lib-table", "CLAUDE.md", "tw")):
            ct = mimetypes.guess_type(path)[0] or "application/octet-stream"
            # the file's own name for a download (without it the Mac app saves "file.zip", "file.csv")
            return web.FileResponse(path, headers={"Content-Type": ct, "Cache-Control": "no-cache",
                                                   "Content-Disposition": _disposition(os.path.basename(path),
                                                                                       request.query.get("download") == "1")})
        size = os.path.getsize(path)
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(600_000)
        return jresp({"path": os.path.relpath(path, rt.p.root), "size": size, "text": text, "truncated": size > 600_000})

    @routes.post("/api/projects/{pid}/reveal")
    async def reveal(request):
        rt = app.rt(request.match_info["pid"])
        body = await request.json()
        app.reveal(safe_join(rt.p.root, body.get("path", "")))
        return jresp({"ok": True})

    @routes.get("/api/projects/{pid}/history")
    async def get_history(request):
        rt = app.rt(request.match_info["pid"])
        return jresp(await asyncio.to_thread(history.log, rt.p.root, 150))

    @routes.get("/api/projects/{pid}/history/{rev}")
    async def get_rev(request):
        rt = app.rt(request.match_info["pid"])
        return jresp(await asyncio.to_thread(history.changed_files, rt.p.root, request.match_info["rev"]))

    board_pictures = {}                          # (project, git rev) -> the board's picture then (revs never change)

    @routes.get("/api/projects/{pid}/board-at/{rev}")
    async def board_at(request):
        """The board as it was at a checkpoint ('now': as it is), in the timelapse's picture form, for
        Compare versions."""
        from .timelapse import picture
        from tw.board import Board
        rt = app.rt(request.match_info["pid"])
        rev = request.match_info["rev"]
        tw = rt.p.tw
        if not tw.has_pcb():
            return err("no board yet", 404)

        def work():
            if rev == "now":
                b = rt.board()
                return {"rev": "now", "picture": picture(b), "shapes_board": True}
            key = (rt.p.id, rev)
            if key in board_pictures:
                return board_pictures[key]
            if not re.fullmatch(r"[0-9a-fA-F]{4,40}", rev):
                raise ValueError("not a checkpoint")
            text = history.show_file(rt.p.root, rev, os.path.relpath(tw.pcb, rt.p.root))
            if not text or not text.lstrip().startswith("(kicad_pcb"):
                return None
            out = {"rev": rev, "picture": picture(Board.parse(text))}
            board_pictures[key] = out
            if len(board_pictures) > 40:
                board_pictures.pop(next(iter(board_pictures)))
            return out
        try:
            d = await asyncio.to_thread(work)
        except ValueError as e:
            return err(str(e))
        if d is None:
            return err("the board did not exist at that checkpoint", 404)
        return jresp(d)

    @routes.post("/api/projects/{pid}/history/snapshot")
    async def snapshot(request):
        rt = app.rt(request.match_info["pid"])
        body = await request.json()
        h = await asyncio.to_thread(history.snapshot, rt.p.root, body.get("message") or "Checkpoint")
        rt.hub.emit("history.snapshot", hash=h, message=body.get("message"))
        return jresp({"hash": h})

    @routes.post("/api/projects/{pid}/history/restore")
    async def restore(request):
        rt = app.rt(request.match_info["pid"])
        body = await request.json()
        rt.mark_self(10)
        h = await asyncio.to_thread(history.restore, rt.p.root, body["rev"])
        rt.hub.emit("history.snapshot", hash=h, message=f"Restored {body['rev']}")
        rt.hub.emit("board.changed", version=-1, source="tracewright")
        rt.hub.emit("schematic.changed", source="tracewright")
        return jresp({"hash": h})

    @routes.get("/api/projects/{pid}/outputs")
    async def outputs(request):
        rt = app.rt(request.match_info["pid"])
        b = rt.p.tw.build
        out = {}
        for sec in ("release", "fab", "docs", "images"):
            d = os.path.join(b, sec)
            if os.path.isdir(d):
                out[sec] = [{"name": f, "path": os.path.relpath(os.path.join(d, f), rt.p.root), "size": os.path.getsize(os.path.join(d, f)),
                             "mtime": os.path.getmtime(os.path.join(d, f))}
                            for f in sorted(os.listdir(d)) if os.path.isfile(os.path.join(d, f))]
        return jresp(out)

    @routes.post("/api/projects/{pid}/outputs/run")
    async def run_outputs(request):
        rt = app.rt(request.match_info["pid"])
        body = await request.json() if request.can_read_body else {}
        key = ("outputs", rt.p.id)
        if key in app.jobs and not app.jobs[key].done():
            return err("outputs are already being generated")
        from tw.outputs import Outputs

        async def job():
            rt.hub.emit("outputs.start")
            rt.mark_self(600)
            try:
                o = Outputs(rt.p.tw)
                o.say = lambda m: (o.log.append(m), rt.hub.emit("outputs.progress", text=m))
                z = await asyncio.to_thread(o.all, bool(body.get("renders", True)))
                rt.hub.emit("outputs.done", release=os.path.relpath(z, rt.p.root), log=o.log)
            except Exception as e:
                rt.hub.emit("outputs.done", error=f"{type(e).__name__}: {e}")
            finally:
                rt.mark_self(4)
        app.jobs[key] = asyncio.ensure_future(job())
        return jresp({"started": True})

    # ------------------------------------------------------------------ ordering (the Order tab)
    def _signed(rt):
        """Ordering needs the design signed off as it is now (Checks > Sign-off)."""
        from . import signoff
        so = signoff.current(rt.p)
        if not so:
            return err("Sign the design off first (Checks > Sign-off): ordering needs it.", 409)
        if not so["valid"]:
            return err("The design changed after it was signed off: sign it off again (Checks > Sign-off).", 409)
        return None

    def _order_state(rt, qty):
        from . import order, bom as bomlib, signoff
        tw = rt.p.tw
        m = order.mode(tw)
        b = rt.board() if tw.has_pcb() else None
        sp = order.specs(b) if b is not None else None
        data = bomlib.bom_data(tw, b) if tw.has_sch() else {"empty": True}
        totals = data.get("totals", {}) if not data.get("empty") else {}
        fresh = order.fab_fresh(tw) if tw.has_pcb() else False
        files = {}
        od = os.path.join(tw.build, "order")
        for t in ("jlc", "pcbway", "parts"):
            d = os.path.join(od, t)
            if os.path.isdir(d):
                files[t] = [{"name": f, "path": os.path.relpath(os.path.join(d, f), rt.p.root), "mtime": os.path.getmtime(os.path.join(d, f))}
                            for f in sorted(os.listdir(d)) if os.path.isfile(os.path.join(d, f))]
        return {"mode": m, "modes": order.MODES, "specs": sp, "estimate": order.estimate(sp, totals, m, qty) if sp else None,
                "readiness": order.readiness(tw, data, rt.p.checks_summary(), m, fresh), "totals": totals, "fresh": fresh,
                "files": files, "links": order.LINKS, "has_pcb": tw.has_pcb(), "signoff": signoff.current(rt.p)}

    @routes.get("/api/projects/{pid}/bringup")
    async def bringup_get(request):
        from . import bringup
        rt = app.rt(request.match_info["pid"])
        return jresp(await asyncio.to_thread(bringup.state, rt.p.root))

    @routes.post("/api/projects/{pid}/bringup")
    async def bringup_post(request):
        """A step's result on one board: {board, key, done?, value?, note?}; or {add_board: serial}."""
        from . import bringup
        rt = app.rt(request.match_info["pid"])
        body = await request.json()
        if "add_board" in body:
            serial = await asyncio.to_thread(bringup.add_board, rt.p.root, body["add_board"])
            return jresp({"board": serial, **await asyncio.to_thread(bringup.state, rt.p.root)})
        if body.get("select"):
            d = bringup.load(rt.p.root)
            d["current"] = str(body["select"])
            await asyncio.to_thread(bringup.save, rt.p.root, d)
            return jresp(await asyncio.to_thread(bringup.state, rt.p.root))
        r = await asyncio.to_thread(bringup.record, rt.p.root, body.get("board") or "1", body["key"], body.get("done"), body.get("value"), body.get("note"))
        return jresp(r)

    @routes.get("/api/projects/{pid}/bringup/report")
    async def bringup_report(request):
        from . import bringup
        rt = app.rt(request.match_info["pid"])
        return web.Response(text=await asyncio.to_thread(bringup.report, rt.p.root), content_type="text/markdown",
                            headers={"Content-Disposition": _disposition(f"{rt.p.id}-bring-up-results.md", True)})

    @routes.get("/api/projects/{pid}/overview")
    async def project_overview(request):
        from . import overview
        rt = app.rt(request.match_info["pid"])
        return jresp(await asyncio.to_thread(overview.overview, app, rt))

    @routes.get("/api/projects/{pid}/stackup")
    async def stackup(request):
        """The board's layer stack for the calculators: copper per layer (mm), the dielectric under the top
        layer (height, er), the thickness."""
        rt = app.rt(request.match_info["pid"])

        def work():
            b = rt.board() if rt.p.tw.has_pcb() else None
            if b is None:
                return {"board": False}
            cu = {l: b.copper_mm(l) for l in b.copper}
            top = b.dielectric_between(b.copper[0], b.copper[1]) if len(b.copper) > 1 else None
            inner = b.dielectric_between(b.copper[1], b.copper[2]) if len(b.copper) > 3 else None
            return {"board": True, "layers": b.copper, "copper_mm": cu, "thickness": b.thickness, "stackup": b.stackup,
                    "top_dielectric": {"h": top[0], "er": top[1]} if top else None,
                    "inner_dielectric": {"h": inner[0], "er": inner[1]} if inner else None,
                    "rules": {"min_track": min((t.w for t in b.tracks), default=None)}}
        return jresp(await asyncio.to_thread(work))

    @routes.get("/api/projects/{pid}/order")
    async def order_get(request):
        rt = app.rt(request.match_info["pid"])
        qty = max(1, min(1000, int(request.query.get("qty", "5") or 5)))
        return jresp(await asyncio.to_thread(_order_state, rt, qty))

    @routes.put("/api/projects/{pid}/order/mode")
    async def order_mode(request):
        from . import order
        rt = app.rt(request.match_info["pid"])
        body = await request.json()
        try:
            order.set_mode(rt.p, body.get("mode"))
        except ValueError as e:
            return err(str(e))
        rt.hub.emit("project.changed", fab=rt.p.cfg.get("fab"))
        return jresp(await asyncio.to_thread(_order_state, rt, int(body.get("qty") or 5)))

    order_locks = {}

    async def _order_job(rt, fn):
        """One order package at a time per project; its steps go to the Order tab as order.progress."""
        lock = order_locks.setdefault(rt.p.id, asyncio.Lock())
        if lock.locked():
            raise RuntimeError("an order package is already being made")
        async with lock:
            rt.mark_self(300)
            say = lambda m: rt.hub.emit("order.progress", text=m)
            try:
                return await fn(say)
            finally:
                rt.mark_self(4)

    @routes.post("/api/projects/{pid}/order/jlc")
    async def order_jlc(request):
        """The package JLC's quote page takes, and the order sheet; the app opens the page and the folder."""
        from . import order, bom as bomlib
        rt = app.rt(request.match_info["pid"])
        if (refused := _signed(rt)):
            return refused
        body = await request.json() if request.can_read_body else {}
        qty = max(1, int(body.get("qty") or 5))
        tw = rt.p.tw
        if not tw.has_pcb():
            return err("no board yet")

        async def run(say):
            def work():
                b = rt.board()
                sp = order.specs(b)
                data = bomlib.bom_data(tw, b)
                m = order.mode(tw)
                est = order.estimate(sp, data.get("totals", {}), m, qty)
                return order.jlc_package(tw, sp, est, m, say)
            return await asyncio.to_thread(work)
        try:
            r = await _order_job(rt, run)
        except Exception as e:
            app.log(traceback.format_exc())
            return err(f"{type(e).__name__}: {e}")
        return jresp({**r, "dir_rel": os.path.relpath(r["dir"], rt.p.root)})

    @routes.post("/api/projects/{pid}/order/pcbway")
    async def order_pcbway(request):
        """One click: the package PCBWay's KiCad plugin makes, uploaded the same way; returns PCBWay's
        quote page for the board (the user's browser opens it)."""
        from . import order
        rt = app.rt(request.match_info["pid"])
        if (refused := _signed(rt)):
            return refused
        if not rt.p.tw.has_pcb():
            return err("no board yet")

        async def run(say):
            pk = await asyncio.to_thread(order.pcbway_zip, rt.p.tw, say)
            return await order.pcbway_upload(pk, say)
        try:
            r = await _order_job(rt, run)
        except Exception as e:
            app.log(traceback.format_exc())
            return err(f"PCBWay upload failed: {type(e).__name__}: {e}", 502)
        return jresp({**r, "zip_rel": os.path.relpath(r["zip"], rt.p.root)})

    @routes.post("/api/projects/{pid}/order/parts")
    async def order_parts(request):
        """Self-assembly: the parts in DigiKey's, Mouser's and LCSC's BOM formats, for N boards plus spares."""
        from . import order
        rt = app.rt(request.match_info["pid"])
        if (refused := _signed(rt)):
            return refused
        body = await request.json() if request.can_read_body else {}
        boards = max(1, min(500, int(body.get("boards") or 3)))
        if not rt.p.tw.has_sch():
            return err("no schematic yet")

        async def run(say):
            return await asyncio.to_thread(order.parts_files, rt.p.tw, boards, say)
        try:
            r = await _order_job(rt, run)
        except Exception as e:
            app.log(traceback.format_exc())
            return err(f"{type(e).__name__}: {e}")
        return jresp({**r, "dir_rel": os.path.relpath(r["dir"], rt.p.root)})

    @routes.post("/api/projects/{pid}/render3d")
    async def render3d(request):
        rt = app.rt(request.match_info["pid"])
        from tw.outputs import Outputs
        o = Outputs(rt.p.tw)
        files = await asyncio.to_thread(o.renders, "basic")
        return jresp({"files": [os.path.relpath(f, rt.p.root) for f in files]})

    @routes.post("/api/projects/{pid}/kicad/open")
    async def kicad_open(request):
        rt = app.rt(request.match_info["pid"])
        body = await request.json()
        await asyncio.to_thread(app.open_in_kicad, rt.p, body.get("what", "project"))
        return jresp({"ok": True})

    @routes.get("/api/projects/{pid}/thumb")
    async def thumb(request):
        rt = app.rt(request.match_info["pid"])
        f = rt.p.thumbnail()
        if not f:
            f = await asyncio.to_thread(_make_thumb, rt)
        if not f:
            return err("no board yet", 404)
        return web.FileResponse(f, headers={"Cache-Control": "no-cache"})

    @routes.get("/api/projects/{pid}/media/{name}")
    async def media(request):
        rt = app.rt(request.match_info["pid"])
        f = safe_join(os.path.join(rt.p.root, ".tracewright", "sessions", "media"), request.match_info["name"])
        return web.FileResponse(f)

    # ------------------------------------------------------------------ chat
    @routes.get("/api/projects/{pid}/sessions")
    async def sessions(request):
        a = app.agent(request.match_info["pid"])
        cur = a.session.sid if a.session else (a.index()[0]["sid"] if a.index() else None)
        return jresp({"sessions": a.index(), "current": cur, "busy": a.busy,
                      "pending": [info for _, info in a.pending.values()]})

    @routes.get("/api/projects/{pid}/sessions/{sid}")
    async def session(request):
        a = app.agent(request.match_info["pid"])
        s = a.get_session(request.match_info["sid"])
        from . import costs
        return jresp({"meta": s.meta, "transcript": costs.per_turn(s.transcript())[0]})

    @routes.post("/api/projects/{pid}/sessions")
    async def new_session(request):
        a = app.agent(request.match_info["pid"])
        if a.busy:
            return err("Claude is busy; stop it first")
        s = await a.new_session()
        return jresp(s.meta)

    @routes.post("/api/projects/{pid}/sessions/{sid}/use")
    async def use_session(request):
        a = app.agent(request.match_info["pid"])
        if a.busy:
            return err("Claude is busy; stop it first")
        s = await a.use_session(request.match_info["sid"])
        return jresp(s.meta)

    def _attached(rt, paths):
        """The message's attached files (their records), and the pictures among them to send with it."""
        recs = [rt.attached.pop(p) for p in paths or [] if p in rt.attached]
        items = [{"kind": "file", "ftype": r["kind"], "path": r["path"], "name": r["name"], "label": r["label"],
                  "size": r.get("size", 0)} for r in recs]
        images = [os.path.join(rt.p.root, r["path"]) for r in recs if r["kind"] == "image"]
        return items, images

    @routes.post("/api/projects/{pid}/chat")
    async def chat(request):
        pid = request.match_info["pid"]
        a, rt = app.agent(pid), app.rt(pid)
        body = await request.json()
        text = (body.get("text") or "").strip()
        files, images = _attached(rt, body.get("files"))
        if not text and not files:
            return err("empty message")
        if not text:
            text = "(Attached with no message.)"
        sid = await a.send(text, body.get("sid"), (body.get("attachments") or []) + files, images=images)
        return jresp({"sid": sid})

    # ------------------------------------------------------------------ sign-off and waivers
    def _who(request):
        u = app.user(request)
        return (u.get("name") or u.get("email")) if u else "you"

    @routes.get("/api/projects/{pid}/signoff")
    async def signoff_get(request):
        from . import signoff
        rt = app.rt(request.match_info["pid"])
        return jresp(await asyncio.to_thread(signoff.status, rt.p.reload()))

    @routes.post("/api/projects/{pid}/signoff")
    async def signoff_post(request):
        """The user signs the design off as it is now (409 with the reasons when it cannot be)."""
        from . import signoff, turns
        rt = app.rt(request.match_info["pid"])
        body = await request.json() if request.can_read_body else {}
        p = rt.p.reload()
        try:
            so = await asyncio.to_thread(signoff.sign, p, _who(request), body.get("note", ""), turns.head(p.root))
        except ValueError as e:
            return err(str(e), 409)
        rt.user_changes.append(f"the user signed the design off ({so['by']}, {so['at']}); ordering is open")
        rt.hub.emit("signoff", signoff=so)
        return jresp(await asyncio.to_thread(signoff.status, p))

    @routes.delete("/api/projects/{pid}/signoff")
    async def signoff_delete(request):
        from . import signoff
        rt = app.rt(request.match_info["pid"])
        p = rt.p.reload()
        await asyncio.to_thread(signoff.revoke, p)
        rt.user_changes.append("the user took their sign-off back")
        rt.hub.emit("signoff", signoff=None)
        return jresp(await asyncio.to_thread(signoff.status, p))

    @routes.post("/api/projects/{pid}/waivers")
    async def waivers_post(request):
        """The user on a waiver: approve (Claude's proposal on an error), reject (it goes), or add their own
        {key, reason}. Claude hears of it at its next turn."""
        from . import signoff
        rt = app.rt(request.match_info["pid"])
        body = await request.json()
        p, key, act = rt.p.reload(), str(body.get("key") or ""), body.get("action")
        try:
            if act == "approve":
                await asyncio.to_thread(signoff.approve, p, key)
                rt.user_changes.append(f"the user approved the waiver for {key}")
            elif act == "reject":
                await asyncio.to_thread(signoff.remove_waiver, p, key)
                rt.user_changes.append(f"the user rejected the waiver for {key}: fix the finding instead")
            elif act == "add":
                reason = str(body.get("reason") or "").strip()
                if not reason:
                    return err("a waiver needs its reason")
                await asyncio.to_thread(signoff.set_waiver, p, key, reason, "user", str(body.get("severity") or ""), str(body.get("message") or ""))
                rt.user_changes.append(f"the user waived {key}: {reason}")
            elif act == "prune":                          # waivers whose findings are gone
                gone = [w["key"] for w in (await asyncio.to_thread(signoff.status, p))["waivers"] if w["state"] == "unused"]
                for k in gone:
                    await asyncio.to_thread(signoff.remove_waiver, p, k)
                if gone:
                    rt.user_changes.append(f"the user removed {len(gone)} waiver{'s' if len(gone) != 1 else ''} no longer needed")
            else:
                return err("action: approve | reject | add | prune")
        except KeyError as e:
            return err(str(e), 404)
        rt.hub.emit("waivers")
        return jresp(await asyncio.to_thread(signoff.status, p))

    @routes.get("/api/projects/{pid}/signoff/packet")
    async def signoff_packet(request):
        """The review packet: the sign-off page as one printable page (save it as PDF from the print dialog), also
        written to build/signoff/review-packet.html."""
        from . import signoff
        rt = app.rt(request.match_info["pid"])
        html = await asyncio.to_thread(signoff.packet_html, rt.p.reload())
        name = f"{rt.p.name} review packet.html"
        return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-cache",
                            **({"Content-Disposition": _disposition(name, True)} if request.query.get("download") == "1" else {})})

    @routes.post("/api/projects/{pid}/turns/undo")
    async def undo_turn(request):
        """Undo Claude's last turn: the project's files as they were before it, as new commits (the History tab
        keeps everything). Only the latest turn, and only while Claude is idle; Claude hears of it next turn."""
        from . import turns
        pid = request.match_info["pid"]
        a, rt = app.agent(pid), app.rt(pid)
        body = await request.json()
        if a.busy:
            return err("Claude is working: stop it first", 409)
        sess = a.get_session(body.get("sid")) if body.get("sid") else (a.session or a.get_session())
        recs = [r for r in sess.transcript() if r.get("kind") in ("changes", "undone")]
        last = next((r for r in reversed(recs) if r.get("kind") == "changes"), None)
        if not last or last.get("turn") != body.get("turn"):
            return err("only Claude's last turn can be undone here; the History tab goes further back", 409)
        if any(r.get("kind") == "undone" and r.get("turn") == last["turn"] for r in recs):
            return err("that turn is already undone", 409)
        new = await asyncio.to_thread(turns.undo, rt.p, last["base"])
        sess.append({"kind": "undone", "turn": last["turn"], "head": new})
        a.head = new
        rt.user_changes.append(f"the user undid your last turn: the project's files are back as they were before it (commit {last['base']})")
        rt.hub.emit("agent.undone", sid=sess.sid, turn=last["turn"], head=new)
        return jresp({"undone": last["turn"], "head": new})

    @routes.get("/api/library")
    async def library_list(request):
        """My parts: the parts saved from projects (library.py), matching ?q= when given."""
        from . import library
        return jresp({"items": await asyncio.to_thread(library.search, request.query.get("q", ""))})

    @routes.post("/api/projects/{pid}/library")
    async def library_save(request):
        """Save one of the project's parts to my library: {ref, notes?}."""
        from . import library
        rt = app.rt(request.match_info["pid"])
        body = await request.json()
        try:
            it = await asyncio.to_thread(library.save, rt.p, str(body.get("ref") or ""), body.get("notes") or "")
        except ValueError as e:
            return err(str(e))
        return jresp(it)

    @routes.delete("/api/library/{lid}")
    async def library_remove(request):
        from . import library
        try:
            await asyncio.to_thread(library.remove, request.match_info["lid"])
        except KeyError:
            return err("not in the library", 404)
        return jresp({"removed": request.match_info["lid"]})

    @routes.get("/api/projects/{pid}/estimate")
    async def estimate_request(request):
        """What a message would likely cost, before it is sent (estimate.py): ?text=..."""
        from . import estimate
        text = request.query.get("text", "")[:2000]
        return jresp(await asyncio.to_thread(estimate.estimate, app.store, text))

    @routes.get("/api/projects/{pid}/approvals")
    async def approvals_list(request):
        """What Claude's runs did that needs the user's OK (approvals.py): the pending ones first."""
        from . import approvals
        rt = app.rt(request.match_info["pid"])
        items = await asyncio.to_thread(approvals.Approvals(rt.p).items)
        return jresp({"items": items, "pending": [x for x in items if x["status"] == "pending"],
                      "kinds": approvals.kinds_on(app.settings), "labels": approvals.LABELS})

    @routes.post("/api/projects/{pid}/approvals/{aid}")
    async def approvals_decide(request):
        """The user's call on one item: keep or undo (what Claude went ahead with: undo asks Claude to put it back and
        redo what hung on it), approve or decline (what was asked: approve applies it)."""
        from . import approvals
        pid = request.match_info["pid"]
        a, rt = app.agent(pid), app.rt(pid)
        body = await request.json()
        action = body.get("action")
        st = approvals.Approvals(rt.p)
        try:
            it = st.get(request.match_info["aid"])
        except KeyError:
            return err("no such item", 404)
        if it["status"] != "pending":
            return err("already decided", 409)
        ok = {"confirm": ("keep", "undo"), "ask": ("approve", "decline")}[it["group"]]
        if action not in ok:
            return err(f"{action}: {' or '.join(ok)}")
        if action == "undo" and a.busy:
            return err("Claude is working: undo this when it has finished", 409)
        status = {"keep": "kept", "undo": "undone", "approve": "approved", "decline": "declined"}[action]
        if action == "approve" and it["kind"] == "limits":
            rt.p.reload()
            rt.p.cfg["constraints"] = it.get("proposed") or {}
            await asyncio.to_thread(rt.p.save)
        it = await asyncio.to_thread(st.set, it["id"], status)
        rt.hub.emit("approvals.changed", items=st.items())
        if action == "undo":                              # the follow-up run: Claude puts it back
            await a.send(f"Undo: {it['title']}", hidden=approvals.undo_text(it))
        else:
            rt.user_changes.append(approvals.decided_line(it, status))
        return jresp(it)

    @routes.post("/api/projects/{pid}/chat/steer")
    async def steer(request):
        """A note for Claude while it works: read at its next tool call (409 when it has stopped, so
        the app sends it as a new message instead)."""
        pid = request.match_info["pid"]
        a, rt = app.agent(pid), app.rt(pid)
        body = await request.json()
        text = (body.get("text") or "").strip()
        if not text and not body.get("files"):
            return err("empty message")
        if not a.busy:
            return err("Claude is not working now", 409)
        files, images = _attached(rt, body.get("files"))
        if files:
            text = (text + "\n\n" if text else "") + "\n".join("Attached: " + f["label"] for f in files)
        return jresp({"id": await a.steer(text, images=images)})

    @routes.post("/api/projects/{pid}/chat/interrupt")
    async def interrupt(request):
        a = app.agent(request.match_info["pid"])
        await a.interrupt()
        return jresp({"ok": True})

    @routes.post("/api/projects/{pid}/chat/answer")
    async def answer(request):
        a = app.agent(request.match_info["pid"])
        body = await request.json()
        if not a.answer_question(body["id"], body.get("answers") or {}):
            return err("that question is no longer open", 404)
        return jresp({"ok": True})

    @routes.post("/api/projects/{pid}/chat/permission")
    async def permission(request):
        a = app.agent(request.match_info["pid"])
        body = await request.json()
        ok = a.answer_permission(body["id"], body.get("allow"), body.get("always"))
        return jresp({"ok": ok})

    # ------------------------------------------------------------------ design ideas (home screen)
    def _ideas_file():
        return os.path.join(config.data_dir(), "ideas.json")

    @routes.get("/api/ideas")
    async def ideas_get(request):
        from . import ideas
        try:
            with open(_ideas_file()) as f:
                recent = json.load(f)
        except (OSError, ValueError):
            recent = []
        return jresp({"questions": ideas.QUESTIONS, "recent": recent[:12]})

    @routes.post("/api/ideas")
    async def ideas_post(request):
        """Three board ideas for the questionnaire's answers (seed: an idea to vary)."""
        from . import ideas
        body = await request.json()
        try:
            out = await asyncio.wait_for(ideas.generate(app.settings, body.get("answers") or {}, body.get("seed")), timeout=150)
        except asyncio.TimeoutError:
            return err("Claude took too long to answer; try again", 504)
        except Exception as e:
            app.log(traceback.format_exc())
            msg = f"{type(e).__name__}: {e}"
            if "auth" in msg.lower() or "login" in msg.lower() or "api key" in msg.lower():
                msg += " -- sign in with `claude` (Claude Code) or add an Anthropic API key in Settings."
            return err(msg, 502)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        try:
            with open(_ideas_file()) as f:
                recent = json.load(f)
        except (OSError, ValueError):
            recent = []
        recent = [{**i, "made": stamp} for i in out] + recent
        with open(_ideas_file(), "w") as f:
            json.dump(recent[:30], f, indent=1)
        return jresp({"ideas": out})

    # ------------------------------------------------------------------ lessons
    @routes.get("/api/lessons")
    async def get_lessons(request):
        q = request.query.get("q")
        ls = knowledge.search(q, limit=500) if q else knowledge.all_lessons()
        return jresp([{k: v for k, v in l.items() if k != "raw"} for l in ls])

    @routes.post("/api/lessons")
    async def add_lesson(request):
        body = await request.json()
        l = knowledge.add(body["title"], body.get("body", ""), body.get("tags") or [], body.get("source", "added in the app"),
                          lid=body.get("id") or None)
        return jresp({k: v for k, v in l.items() if k != "raw"})

    @routes.delete("/api/lessons/{lid}")
    async def del_lesson(request):
        return jresp({"ok": knowledge.delete(request.match_info["lid"])})

    # ------------------------------------------------------------------ websocket
    @routes.get("/api/projects/{pid}/ws")
    async def ws(request):
        rt = app.rt(request.match_info["pid"])
        sock = web.WebSocketResponse(heartbeat=25)
        await sock.prepare(request)
        rt.hub.clients.add(sock)
        rt.viewers += 1
        since = int(request.query.get("since", "0") or 0)
        for ev in rt.hub.since(since) if since else []:
            await sock.send_str(json.dumps(ev, default=str))
        await sock.send_str(json.dumps({"type": "hello", "seq": rt.hub.seq, "live": rt.live}))
        try:
            async for msg in sock:
                if msg.type == WSMsgType.TEXT:
                    try:
                        m = json.loads(msg.data)
                    except ValueError:
                        continue
                    if m.get("type") == "selection":
                        rt.selection["app"] = m.get("items", [])
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            rt.hub.clients.discard(sock)
            rt.viewers = max(0, rt.viewers - 1)
        return sock

    # ------------------------------------------------------------------ static UI
    @routes.get("/")
    async def index(request):
        if app.local_key and not app.window_ok(request):
            return web.Response(text=auth.LOCKED_PAGE, content_type="text/html", status=401, headers={"Cache-Control": "no-store"})
        with open(os.path.join(WEB, "index.html"), encoding="utf-8") as f:
            html = f.read().replace("/static/", f"/static/{_build_id()}/")
        r = web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-cache"})
        if app.local_key and request.headers.get(auth.KEY_HEADER):       # the Mac app's first request: its cookie
            r.set_cookie(auth.LOCAL_COOKIE, auth.window_cookie(app.local_key), httponly=True, samesite="Strict", path="/")
        return r

    @routes.get("/static/{path:.+}")
    async def static(request):
        # /static/<build id>/... : the build id changes whenever a UI file does, so updates are never stale
        path = request.match_info["path"]
        first, _, rest = path.partition("/")
        if first.startswith("b-") and rest:
            path = rest
        f = safe_join(WEB, path)
        if not os.path.isfile(f):
            raise web.HTTPNotFound()
        ct = mimetypes.guess_type(f)[0] or "application/octet-stream"
        if f.endswith(".js"):
            ct = "text/javascript"
        cache = "public, max-age=31536000, immutable" if first.startswith("b-") else "no-cache"
        return web.FileResponse(f, headers={"Cache-Control": cache, "Content-Type": ct})

    webapp = web.Application(middlewares=[local_guard, gate, errors], client_max_size=64 * 1024 * 1024)
    webapp.add_routes(routes)
    webapp["app"] = app

    async def on_startup(_):
        app.hubs.loop = asyncio.get_running_loop()

    async def on_shutdown(_):
        for a in list(app.agents.values()):
            await a.disconnect()
        for rt in app.runtimes.values():
            rt.stop()
    webapp.on_startup.append(on_startup)
    webapp.on_shutdown.append(on_shutdown)
    return webapp


def _claude_auth(settings):
    """How Claude will sign in: an API key, a Claude subscription token (claude setup-token), or the
    Claude Code login on this machine."""
    if (settings.get("anthropic_api_key") or "").strip() or os.environ.get("ANTHROPIC_API_KEY"):
        return "api_key"
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return "subscription_token"
    if shutil.which("claude") or os.path.exists(os.path.expanduser("~/.claude")):
        return "claude_code"
    return None


async def _stream(part, dest, limit):
    size = 0
    with open(dest, "wb") as f:
        while True:
            chunk = await part.read_chunk(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                f.close()
                os.remove(dest)
                raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=size)
            f.write(chunk)
    return size


SKIP_ZIP = {".git", "build", ".tracewright", "__pycache__", "node_modules"}


def _zip_project(p, with_git=False):
    tmp = os.path.join(config.data_dir(), "tmp")
    os.makedirs(tmp, exist_ok=True)
    out = os.path.join(tmp, f"{p.id}-{int(time.time())}.zip")
    import zipfile
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for dp, dns, fns in os.walk(p.root):
            dns[:] = [d for d in dns if (with_git and d == ".git") or (d not in SKIP_ZIP and not d.endswith("-backups"))]
            for f in fns:
                if f.endswith((".lck", ".pyc")) or f == "fp-info-cache":
                    continue
                full = os.path.join(dp, f)
                z.write(full, os.path.join(p.id, os.path.relpath(full, p.root)))
    return out


def guided_kickoff_text():
    """The first message of a guided start (the canvas beside the chat shows what Claude works out)."""
    return ("The message is the user's brief for this new board (also saved in BRIEF.md). This is a guided start: the user "
            "sees the chat beside a live canvas. Run the intake (skill new-design): read the brief, fill in the canvas's requirements from "
            "what it says, then ask me now every question whose answer changes the design -- with your question tool, up "
            "to four per call, your recommended option first. As the answers come in, draw the block diagram, the "
            "connectors with their pinouts and board edges, the floorplan (the board to scale with its holes, "
            "connectors and main blocks), and the key parts with LCSC codes. When the requirements "
            "are settled, write docs/requirements.md and call ready_to_start; then wait for the user to press Start.")


FLOORPLAN_NOTE = (" Lay the board out as the floorplan on the canvas says (./tw floorplan shows it in board coordinates; "
                  "once the board exists, ./tw floorplan apply draws the outline and the blocks' areas and puts the "
                  "connectors and holes in their places): what I placed there is what I want.")


def start_text(mode, floorplan=False):
    """What Claude hears when the user presses Start."""
    fp = FLOORPLAN_NOTE if floorplan else ""
    if mode == "check_in":
        return ("I pressed Start. Begin the design from the agreed requirements, stage by stage, and check in with me at "
                "the end of each stage." + fp)
    return ("I pressed Start. Carry the design through from the agreed requirements to a checked board that is ready to "
            "order, without waiting on me: record each assumption in docs/decisions.md, keep the agenda up to date, and "
            "tell me what needs the built board." + fp)


def kickoff_text(mode):
    """The first message of a new project's conversation."""
    if mode == "check_in":
        return ("The message is the user's brief for this new board (also saved in BRIEF.md). Start the design: read it, "
                "ask the user only what changes the design, then write the requirements.")
    return ("The message is the user's brief for this new board (also saved in BRIEF.md). Start with the intake (skill "
            "new-design): read it, then ask the user now every question whose answer changes the design -- with your "
            "question tool, up to four per call and two or three calls at most, your recommended option first -- so the "
            "rest of the run needs nothing from them. Then write docs/requirements.md with their answers and each "
            "assumption you made, and ask them once to confirm it. When they confirm, carry the design through to the "
            "end without waiting on them.")


def _build_id():
    """A short id of the UI files' contents (their times and sizes)."""
    import hashlib
    hsh = hashlib.sha1()
    for dp, dn, fn in os.walk(WEB):
        for f in sorted(fn):
            st = os.stat(os.path.join(dp, f))
            hsh.update(f"{f}{st.st_mtime_ns}{st.st_size}".encode())
    return "b-" + hsh.hexdigest()[:10]


def _annotations(p):
    f = os.path.join(p.root, ".tracewright", "annotations.json")
    try:
        return json.load(open(f))
    except (OSError, ValueError):
        return []


def _selftest():
    import io, contextlib
    from tw.checks import selftest
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = selftest.main(verbose=True)
    return rc, buf.getvalue()


def _make_thumb(rt):
    b = rt.board()
    if b is None or not b.fp_list:
        return None
    from tw import render
    im = render.board_image(b, max_px=640)
    f = os.path.join(rt.p.state_dir(), "thumb.png")
    im.save(f)
    return f


def open_running(open_browser=True):
    """A Tracewright already runs on this machine: open a new browser window on it (with a one-time
    ticket). True when there was one to open."""
    info = auth.read_server_file()
    if not info or not info.get("key"):
        return False
    import urllib.request, webbrowser
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{info['port']}/api/ticket", data=b"{}", method="POST",
                                     headers={auth.KEY_HEADER: info["key"], "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as r:
            url = json.load(r)["url"]
    except (OSError, ValueError, KeyError):
        return False
    print(f"Tracewright is already running (pid {info['pid']}, port {info['port']}).", flush=True)
    if open_browser:
        webbrowser.open(url)
    return True


def _port_free(host, port):
    import socket
    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)     # as the server binds: a closed port's TIME_WAIT is no obstacle
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _free_port(host):
    import socket
    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def run(host="127.0.0.1", port=None, open_browser=True, app_mode=False):
    """Serve Tracewright. On this machine (loopback) the server takes a launch key (TW_LOCAL_KEY from
    the Mac app, or a new one), records itself in server.json and answers only windows holding the
    key. app_mode: started by the Mac app -- no browser, any free port if the usual one is taken,
    and it stops by itself once the app has gone and Claude is idle."""
    s = config.settings()
    local = host in auth.LOOPBACK
    if not local and not auth.password_set():
        raise SystemExit("Tracewright listens beyond this machine only with a password: set TW_PASSWORD (or "
                         "`tracewright password` / Settings > Server) first.")
    if local and not app_mode and open_running(open_browser and s.get("open_browser", True)):
        return
    want = port or int(s.get("port") or 8764)
    if local and not _port_free(host, want):
        if port and not app_mode:
            raise SystemExit(f"Port {want} is in use by another program.")
        want = _free_port(host)
    port = want
    webapp = make_app()
    app = webapp["app"]
    app.require_auth = auth.password_set() or not local
    app.port = port
    url = f"http://127.0.0.1:{port}/" if local else f"http://{host}:{port}/"
    if local:
        app.local_key = os.environ.pop("TW_LOCAL_KEY", "") or auth.new_key()

        async def record(_):
            auth.write_server_file({"pid": os.getpid(), "port": port, "key": app.local_key, "url": url,
                                    "version": __version__, "started": time.time(), "app": app_mode})

        async def forget(_):
            auth.remove_server_file(os.getpid())
        webapp.on_startup.append(record)
        webapp.on_cleanup.append(forget)
        if app_mode:
            parent = int(os.environ.get("TW_PARENT_PID") or os.getppid())
            webapp.on_startup.append(lambda _: _watch_parent(app, parent))
    if open_browser and s.get("open_browser", True) and local and not app_mode:
        import threading, webbrowser
        threading.Timer(1.2, lambda: webbrowser.open(f"{url}auth?t={app.ticket()}")).start()
    print(f"Tracewright {__version__} on {url}  (data: {config.data_dir()}, projects: {config.workspace()})", flush=True)
    logging.basicConfig(level=logging.WARNING)
    web.run_app(webapp, host=host, port=port, print=None, access_log=None)


async def _watch_parent(app, parent):
    """Started by the Mac app: when the app has gone (this process was handed to launchd) and Claude
    is idle, stop -- a crashed app leaves no server behind, and a run in progress is never cut off."""
    import signal

    async def watch():
        while True:
            await asyncio.sleep(5)
            if os.getppid() == parent:
                continue
            busy = any(a.busy for a in app.agents.values()) or any(
                not j.done() for k, j in app.jobs.items() if k[0] in ("checks", "outputs"))
            if not busy:
                app.log("the Tracewright app has gone and Claude is idle: stopping the server")
                os.kill(os.getpid(), signal.SIGTERM)
                return
    asyncio.ensure_future(watch())
