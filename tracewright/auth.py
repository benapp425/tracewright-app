"""Sign-in: the window key on your own machine, a password on a server.

On your own machine (127.0.0.1) Tracewright answers only its own window. Each launch has a secret
key: the Mac app passes it with its first request (header), the `tracewright` command opens the
browser with a one-time ticket; either way the window gets an HttpOnly cookie derived from the key.
Other web pages cannot use the app (the server also refuses foreign Host and Origin headers), and
other local users cannot either: the key is kept in server.json, readable only by you.

Anywhere else a password is required, and every request must carry a session cookie. The password
is stored as a salted PBKDF2 hash (Settings) or given as TW_PASSWORD; sessions are HMAC-signed
cookies (HttpOnly, SameSite=Strict, Secure over HTTPS) that survive restarts; failed sign-ins are
rate-limited per address.
"""
import os, json, time, hmac, hashlib, secrets, base64
from . import config

COOKIE = "tw_session"
DAYS = 30
LOOPBACK = ("127.0.0.1", "localhost", "::1")
LOCAL_COOKIE = "tw_window"
KEY_HEADER = "X-Tracewright-Key"


# ---------------------------------------------------------------------- the window key (this machine)
def new_key():
    return secrets.token_urlsafe(32)


def window_cookie(key):
    """The cookie value a window holds: derived from the launch key, so the key itself never sits in
    the browser's cookie store."""
    return hmac.new(key.encode(), b"tracewright window", hashlib.sha256).hexdigest()


def same(a, b):
    return bool(a) and bool(b) and hmac.compare_digest(str(a).encode(), str(b).encode())


def server_file():
    return os.path.join(config.data_dir(), "server.json")


def write_server_file(info):
    """Where the running server is, and its key (owner-only): the Mac app and the `tracewright`
    command read it to reach a server that is already running."""
    path = server_file()
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(info, f)
    os.replace(tmp, path)


def read_server_file():
    """The running server's record, or None when there is none (or its process is gone)."""
    try:
        with open(server_file()) as f:
            info = json.load(f)
        os.kill(int(info["pid"]), 0)
        return info
    except (OSError, ValueError, KeyError, TypeError):
        return None


def remove_server_file(pid):
    try:
        with open(server_file()) as f:
            info = json.load(f)
    except (OSError, ValueError):
        return
    if info.get("pid") == pid:
        try:
            os.remove(server_file())
        except OSError:
            pass


def _secret():
    path = os.path.join(config.data_dir(), "session.key")
    try:
        with open(path, "rb") as f:
            k = f.read()
        if len(k) >= 32:
            return k
    except OSError:
        pass
    k = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(k)
    return k


def hash_password(pw, salt=None, n=200_000):
    salt = salt or secrets.token_bytes(16)
    d = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, n)
    return f"pbkdf2${n}${base64.b64encode(salt).decode()}${base64.b64encode(d).decode()}"


def password_set():
    return bool(os.environ.get("TW_PASSWORD") or config.settings().get("server_password_hash"))


def check_password(pw):
    env = os.environ.get("TW_PASSWORD")
    if env:
        return hmac.compare_digest(pw.encode(), env.encode())
    stored = config.settings().get("server_password_hash") or ""
    try:
        _, n, salt, d = stored.split("$")
        got = hashlib.pbkdf2_hmac("sha256", pw.encode(), base64.b64decode(salt), int(n))
        return hmac.compare_digest(got, base64.b64decode(d))
    except ValueError:
        return False


def new_session():
    msg = f"{int(time.time()) + DAYS * 86400}.{secrets.token_hex(8)}"
    return f"{msg}.{hmac.new(_secret(), msg.encode(), hashlib.sha256).hexdigest()}"


def valid(cookie):
    try:
        exp, nonce, sig = (cookie or "").split(".")
        want = hmac.new(_secret(), f"{exp}.{nonce}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, want) and int(exp) > time.time()
    except ValueError:
        return False


_fails = {}


def throttled(ip):
    """Five wrong passwords from one address lock it out for ten minutes."""
    now = time.time()
    _fails[ip] = [t for t in _fails.get(ip, []) if now - t < 600]
    return len(_fails[ip]) >= 5


def failed(ip):
    _fails.setdefault(ip, []).append(time.time())


LOCKED_PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Tracewright</title><style>
:root{color-scheme:light dark}body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
background:#111214;color:#ececee;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Inter","Segoe UI",sans-serif}
div{max-width:380px;padding:28px 30px;border:1px solid #2b2c31;border-radius:14px;background:#17181b}
h1{font-size:17px;margin:0 0 8px}p{color:#9a9ca4;margin:0 0 10px}code{font:12.5px ui-monospace,Menlo,monospace;color:#ececee}
@media (prefers-color-scheme: light){body{background:#f5f5f7;color:#17181c}div{background:#fff;border-color:#e2e2e6}
p{color:#5f616a}code{color:#17181c}}</style></head><body><div>
<h1>Open Tracewright from its app</h1>
<p>Tracewright is running on this Mac, but it only answers its own window.</p>
<p>Open <b>Tracewright</b> from your Applications folder, or run <code>tracewright</code> in a terminal.</p>
</div></body></html>"""

LOGIN_PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Tracewright - sign in</title><style>
body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;background:#0e1015;color:#e7eaf0;
font:15px -apple-system,BlinkMacSystemFont,"Inter","Segoe UI",sans-serif}
form{background:#161a22;border:1px solid #2a3140;border-radius:12px;padding:28px 30px;width:300px}
h1{font-size:18px;margin:0 0 4px}p{color:#8b95a8;font-size:13px;margin:0 0 18px}
input{width:100%;box-sizing:border-box;padding:9px 11px;border-radius:8px;border:1px solid #2a3140;background:#12151c;
color:#e7eaf0;font:inherit}button{margin-top:14px;width:100%;padding:9px;border:0;border-radius:8px;color:#fff;
font:inherit;font-weight:600;background:linear-gradient(135deg,#4f8cff,#7c5cff);cursor:pointer}
.e{color:#ff5f5f;font-size:13px;min-height:18px;margin-top:10px}</style></head><body>
<form id="f"><h1>Tracewright</h1><p>Sign in to this server.</p>
<input id="p" type="password" placeholder="Password" autofocus autocomplete="current-password">
<button>Sign in</button><div class="e" id="e"></div></form>
<script>
document.getElementById("f").onsubmit = async (ev) => {
  ev.preventDefault();
  const r = await fetch("/api/login", {method: "POST", headers: {"Content-Type": "application/json"},
                                       body: JSON.stringify({password: document.getElementById("p").value})});
  if (r.ok) location.href = "/"; else document.getElementById("e").textContent = (await r.json()).error || "Wrong password";
};
</script></body></html>"""


def google_done_page(ok, message):
    """The page the browser shows when it comes back from Google: back to Tracewright from here."""
    import html as _html
    mark = ('<svg width="64" height="64" viewBox="100 100 824 824"><defs><linearGradient id="b" x1="0" y1="0" x2="0" y2="1">'
            '<stop offset="0" stop-color="#2a3530"/><stop offset="1" stop-color="#121815"/></linearGradient>'
            '<linearGradient id="c" x1="230" y1="270" x2="700" y2="840" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#ffc590"/>'
            '<stop offset=".55" stop-color="#e8864a"/><stop offset="1" stop-color="#b95a26"/></linearGradient></defs>'
            '<rect x="100" y="100" width="824" height="824" rx="190" fill="url(#b)"/><g fill="none" stroke="url(#c)" stroke-linecap="round" '
            'stroke-linejoin="round" stroke-width="84"><path d="M300 352H724"/><path d="M512 352V560L600 648V716"/></g><g fill="url(#c)">'
            '<circle cx="276" cy="352" r="78"/><circle cx="748" cy="352" r="78"/><circle cx="600" cy="742" r="86"/></g>'
            '<circle cx="600" cy="742" r="36" fill="#121815"/></svg>')
    title = "You're signed in" if ok else "Sign-in didn't finish"
    sub = "Go back to Tracewright: it has already picked this up. You can close this tab." if ok else \
        "Go back to Tracewright and try again."
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Tracewright</title><style>
:root{{color-scheme:light dark}}body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:radial-gradient(700px 400px at 50% 30%,rgba(232,134,74,.14),transparent 70%),#111214;color:#ececee;
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Inter","Segoe UI",sans-serif}}
.c{{text-align:center;max-width:420px;padding:32px}}h1{{font-size:21px;margin:18px 0 6px}}p{{color:#9a9ca4;margin:0 0 6px}}
.m{{display:inline-block;margin-top:14px;font-size:13px;color:{'#43c283' if ok else '#f06560'}}}
@media (prefers-color-scheme: light){{body{{background:#f5f5f7;color:#17181c}}p{{color:#5f616a}}}}</style></head>
<body><div class="c">{mark}<h1>{title}</h1><p>{sub}</p><span class="m">{_html.escape(message)}</span></div>
<script>setTimeout(() => {{ try {{ window.close(); }} catch (e) {{}} }}, {2500 if ok else 60000});</script></body></html>"""
