"""Accounts: who uses Tracewright. Sign in with an email and a password, or with Google.

Accounts live in <data>/accounts.json (owner-only): each person's name, email, a scrypt hash of the
password, the Google account linked to it, and whether they have been through the first-run setup.
Sessions are random tokens in an HttpOnly cookie; only their SHA-256 is stored, so the file never
holds a usable key. "Keep me signed in" makes the cookie last 30 days, otherwise it ends with the
window. The first account on a Mac or a server is its owner: the owner decides whether others may
create accounts (open on your own Mac, closed on a server until the owner opens it) and sets up
Google sign-in.

Google sign-in is OAuth 2.0 with PKCE. The window asks for a sign-in address and opens it in the
system browser (Google refuses sign-in inside an app's web view); Google sends the browser back to
/api/auth/google/callback on this server with a one-time code; the server trades it at Google's token
endpoint (over TLS, so the ID token in the reply comes from Google, OpenID Connect Core 3.1.3.7) and
checks its audience, issuer, expiry, nonce and that the email is verified. The waiting window then
collects its session by polling with a secret only it was given.
"""
import os, re, json, time, hmac, base64, hashlib, secrets, threading
from urllib.parse import urlencode
from . import config
try:
    import fcntl
except ImportError:                    # Windows: threads only
    fcntl = None

COOKIE = "tw_account"
DAYS = 30
GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
class _Lock:
    """One writer at a time, among this process's threads and other processes (the app and
    `tracewright account reset` at the same moment)."""
    def __init__(self):
        self.t, self.f, self.depth = threading.RLock(), None, 0

    def __enter__(self):
        self.t.acquire()
        if self.depth == 0 and fcntl:
            os.makedirs(os.path.dirname(_path()), exist_ok=True)
            self.f = open(_path() + ".lock", "a")
            fcntl.flock(self.f, fcntl.LOCK_EX)
        self.depth += 1
        return self

    def __exit__(self, *exc):
        self.depth -= 1
        if self.depth == 0 and self.f:
            fcntl.flock(self.f, fcntl.LOCK_UN)
            self.f.close()
            self.f = None
        self.t.release()


_lock = _Lock()


class AccountError(ValueError):
    """A sign-in or sign-up problem to show the person as it is."""


# ---------------------------------------------------------------------- the store
def _path():
    return os.path.join(config.data_dir(), "accounts.json")


def _load():
    try:
        with open(_path()) as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    d.setdefault("users", [])
    d.setdefault("sessions", {})
    return d


def _save(d):
    tmp = _path() + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, _path())


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def public(u):
    """What a window may know about an account."""
    if not u:
        return None
    g = u.get("google") or {}
    return {"id": u["id"], "email": u["email"], "name": u.get("name") or u["email"].split("@")[0], "role": u.get("role", "member"),
            "created": u.get("created"), "has_password": bool(u.get("pw")), "google": bool(g.get("sub")),
            "google_email": g.get("email") or "",
            "picture": g.get("picture") or "", "onboarded": bool(u.get("onboarded")), "prefs": u.get("prefs") or {}}


def users():
    with _lock:
        return [public(u) for u in _load()["users"]]


def count():
    with _lock:
        return len(_load()["users"])


def _find(d, email=None, uid=None, sub=None):
    for u in d["users"]:
        if uid and u["id"] == uid:
            return u
        if email and u["email"].lower() == email.lower():
            return u
        if sub and (u.get("google") or {}).get("sub") == sub:
            return u
    return None


def get(uid):
    with _lock:
        return public(_find(_load(), uid=uid))


# ---------------------------------------------------------------------- passwords
def hash_password(pw):
    salt = secrets.token_bytes(16)
    d = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${base64.b64encode(salt).decode()}${base64.b64encode(d).decode()}"


def check_password(pw, stored):
    try:
        kind, n, r, p, salt, digest = (stored or "").split("$")
        if kind != "scrypt":
            return False
        got = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(got, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


def password_problem(pw, email=""):
    """Why a password will not do, or None."""
    if len(pw) < 8:
        return "use at least 8 characters"
    if len(set(pw)) < 4:
        return "use a less repetitive password"
    if email and pw.lower() in (email.lower(), email.split("@")[0].lower()):
        return "the password cannot be your email"
    if pw.lower() in ("password", "password1", "12345678", "123456789", "qwertyui", "tracewright"):
        return "that password is too common"
    return None


# ---------------------------------------------------------------------- signing up and in
def signups_open(settings, server_mode=False):
    """May someone create an account now? Always when there is none yet; after that the owner's choice
    (open on your own Mac, closed on a server unless the owner opens it)."""
    if count() == 0:
        return True
    v = settings.get("allow_signups")
    return (not server_mode) if v is None else bool(v)


def register(email, name, password):
    email = (email or "").strip()
    name = (name or "").strip()
    if not EMAIL_RE.match(email):
        raise AccountError("enter a valid email address")
    why = password_problem(password or "", email)
    if why:
        raise AccountError(why)
    with _lock:
        d = _load()
        if _find(d, email=email):
            raise AccountError("an account with this email already exists. Sign in instead.")
        u = {"id": secrets.token_hex(6), "email": email, "name": name[:80] or email.split("@")[0], "pw": hash_password(password),
             "role": "owner" if not d["users"] else "member", "created": _now(), "last_login": _now(), "onboarded": False}
        d["users"].append(u)
        _save(d)
        return public(u)


_fails = {}


def _throttled(key, since=0):
    """Six wrong tries in ten minutes; a new password (a reset from the terminal) clears those before it."""
    now = time.time()
    _fails[key] = [t for t in _fails.get(key, []) if now - t < 600 and t > since]
    return len(_fails[key]) >= 6


def authenticate(email, password, ip=""):
    """The account for this email and password; six wrong tries (per email and per address) lock
    that out for ten minutes."""
    email = (email or "").strip()
    with _lock:
        since = (_find(_load(), email=email) or {}).get("pw_changed", 0)
    for key in (f"e:{email.lower()}", f"i:{ip}"):
        if _throttled(key, since):
            raise AccountError("too many attempts. Try again in 10 minutes.")
    with _lock:
        d = _load()
        u = _find(d, email=email)
        if u and u.get("pw") and check_password(password or "", u["pw"]):
            u["last_login"] = _now()
            _save(d)
            return public(u)
    for key in (f"e:{email.lower()}", f"i:{ip}"):
        _fails.setdefault(key, []).append(time.time())
    if u and not u.get("pw") and (u.get("google") or {}).get("sub"):
        raise AccountError("this account signs in with Google")
    raise AccountError("incorrect email or password")


def update(uid, **fields):
    with _lock:
        d = _load()
        u = _find(d, uid=uid)
        if not u:
            raise AccountError("no such account")
        if "name" in fields and fields["name"] is not None:
            u["name"] = str(fields["name"]).strip()[:80] or u["name"]
        if fields.get("onboarded") is not None:
            u["onboarded"] = bool(fields["onboarded"])
        if isinstance(fields.get("prefs"), dict):
            u["prefs"] = {**(u.get("prefs") or {}), **fields["prefs"]}
        _save(d)
        return public(u)


def change_password(uid, old, new):
    with _lock:
        d = _load()
        u = _find(d, uid=uid)
        if not u:
            raise AccountError("no such account")
        if u.get("pw") and not check_password(old or "", u["pw"]):
            raise AccountError("the current password is incorrect")
        why = password_problem(new or "", u["email"])
        if why:
            raise AccountError(why)
        u["pw"] = hash_password(new)
        u["pw_changed"] = time.time()
        _save(d)


def remove(uid, by):
    """The owner removes someone else's account (and its sessions)."""
    with _lock:
        d = _load()
        me = _find(d, uid=by)
        if not me or me.get("role") != "owner":
            raise AccountError("only the owner can remove accounts")
        if uid == by:
            raise AccountError("the owner account can't be removed")
        d["users"] = [u for u in d["users"] if u["id"] != uid]
        d["sessions"] = {k: v for k, v in d["sessions"].items() if v.get("uid") != uid}
        _save(d)


def reset_password(email, new):
    """From the command line on the machine itself (`tracewright account reset EMAIL`)."""
    with _lock:
        d = _load()
        u = _find(d, email=email)
        if not u:
            raise AccountError(f"no account for {email}")
        why = password_problem(new, u["email"])
        if why:
            raise AccountError(why)
        u["pw"] = hash_password(new)
        u["pw_changed"] = time.time()
        d["sessions"] = {k: v for k, v in d["sessions"].items() if v.get("uid") != u["id"]}
        _save(d)


# ---------------------------------------------------------------------- sessions
def _h(token):
    return hashlib.sha256((token or "").encode()).hexdigest()


def new_session(uid, remember=True, agent=""):
    token = secrets.token_urlsafe(32)
    with _lock:
        d = _load()
        now = time.time()
        d["sessions"] = {k: v for k, v in d["sessions"].items() if v.get("exp", 0) > now}        # drop the expired
        d["sessions"][_h(token)] = {"uid": uid, "created": now, "exp": now + (DAYS * 86400 if remember else 86400),
                                    "remember": bool(remember), "agent": (agent or "")[:120]}
        _save(d)
    return token


def session_user(token):
    if not token:
        return None
    with _lock:
        d = _load()
        s = d["sessions"].get(_h(token))
        if not s or s.get("exp", 0) < time.time():
            return None
        return public(_find(d, uid=s["uid"]))


def end_session(token):
    with _lock:
        d = _load()
        if d["sessions"].pop(_h(token), None) is not None:
            _save(d)


def sessions_of(uid, current=None):
    with _lock:
        d = _load()
        now = time.time()
        return [{"id": k[:12], "created": v["created"], "remember": v.get("remember"), "agent": v.get("agent", ""),
                 "current": k == _h(current)} for k, v in d["sessions"].items() if v["uid"] == uid and v.get("exp", 0) > now]


def end_other_sessions(uid, current):
    with _lock:
        d = _load()
        keep = _h(current)
        d["sessions"] = {k: v for k, v in d["sessions"].items() if v["uid"] != uid or k == keep}
        _save(d)


# ---------------------------------------------------------------------- Google
def google_config(settings):
    cid = (os.environ.get("TW_GOOGLE_CLIENT_ID") or settings.get("google_client_id") or "").strip()
    secret = (os.environ.get("TW_GOOGLE_CLIENT_SECRET") or settings.get("google_client_secret") or "").strip()
    return (cid, secret) if cid else None


_pending = {}          # state -> a Google sign-in in progress


def google_start(settings, redirect_uri, link_uid=None, shared=False):
    """A Google sign-in address for the system browser, and how the window collects the result.
    link_uid: the signed-in account that asked to link Google (Settings); shared: other people can reach
    this Tracewright (a server), so an existing account is linked only by its own signed-in owner."""
    cfg = google_config(settings)
    if not cfg:
        raise AccountError("Google sign-in is not set up")
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state, nonce, poll, poll_secret = (secrets.token_urlsafe(24) for _ in range(4))
    now = time.time()
    for k in [k for k, v in _pending.items() if now - v["t"] > 900]:
        del _pending[k]
    _pending[state] = {"t": now, "verifier": verifier, "nonce": nonce, "redirect": redirect_uri, "poll": poll,
                       "poll_hash": _h(poll_secret), "result": None, "link": link_uid, "shared": bool(shared)}
    url = GOOGLE_AUTH + "?" + urlencode({"client_id": cfg[0], "redirect_uri": redirect_uri, "response_type": "code",
                                         "scope": "openid email profile", "state": state, "nonce": nonce,
                                         "code_challenge": challenge, "code_challenge_method": "S256",
                                         "prompt": "select_account"})
    return {"url": url, "poll": poll, "secret": poll_secret}


def _claims(id_token):
    try:
        payload = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        raise AccountError("couldn't read Google's response")


async def google_finish(settings, state, code, token_url=None):
    """The browser came back from Google: trade the code, check the ID token, sign the person in. An
    account already linked to this Google account signs in; one being linked from Settings is linked; an
    account with the same email is linked on your own Mac, but on a server only from its own Settings
    (emails are not verified here: someone could have made an account with yours); otherwise a new
    account is made, when sign-ups are open."""
    import aiohttp
    p = _pending.get(state or "")
    if not p or time.time() - p["t"] > 900:
        raise AccountError("this sign-in expired. Start again from Tracewright.")
    cfg = google_config(settings)
    if not cfg:
        raise AccountError("Google sign-in is not set up")
    form = {"code": code, "client_id": cfg[0], "redirect_uri": p["redirect"], "grant_type": "authorization_code",
            "code_verifier": p["verifier"]}
    if cfg[1]:
        form["client_secret"] = cfg[1]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
        async with s.post(token_url or GOOGLE_TOKEN, data=form) as r:
            body = await r.json(content_type=None)
            if r.status != 200 or "id_token" not in body:
                raise AccountError(f"Google did not sign you in ({body.get('error_description') or body.get('error') or r.status})")
    c = _claims(body["id_token"])
    if c.get("aud") != cfg[0] or c.get("iss") not in GOOGLE_ISSUERS:
        raise AccountError("that sign-in was not meant for Tracewright")
    if float(c.get("exp", 0)) < time.time() or c.get("nonce") != p["nonce"]:
        raise AccountError("this sign-in expired. Start again.")
    if not c.get("email") or not c.get("email_verified"):
        raise AccountError("Google did not confirm this account's email")
    with _lock:
        d = _load()
        u = _find(d, sub=c["sub"])
        if p.get("link"):
            me = _find(d, uid=p["link"])
            if me is None:
                raise AccountError("the account to link no longer exists")
            if u is not None and u["id"] != me["id"]:
                raise AccountError("this Google account is linked to another account here")
            u = me
        if u is None:
            u = _find(d, email=c["email"])
            if u is not None and p.get("shared"):
                raise AccountError("an account with this email exists. Sign in with its password, then link Google in Settings.")
        if u is None:
            if not signups_open(settings, p.get("shared")):
                raise AccountError("new accounts are closed here")
            u = {"id": secrets.token_hex(6), "email": c["email"], "name": c.get("name") or c["email"].split("@")[0], "pw": None,
                 "role": "owner" if not d["users"] else "member", "created": _now(), "onboarded": False}
            d["users"].append(u)
        u["google"] = {"sub": c["sub"], "email": c["email"], "picture": c.get("picture", "")}
        u["last_login"] = _now()
        _save(d)
        p["result"] = {"uid": u["id"]}
        return public(u)


def google_fail(state, message):
    p = _pending.get(state or "")
    if p:
        p["result"] = {"error": message}


def google_poll(poll, poll_secret):
    """('pending' | 'ok' | 'error', uid or message) for the window that started a sign-in."""
    for state, p in list(_pending.items()):
        if p["poll"] == poll and hmac.compare_digest(p["poll_hash"], _h(poll_secret)):
            if time.time() - p["t"] > 900:
                del _pending[state]
                return "error", "the sign-in expired"
            if not p["result"]:
                return "pending", None
            del _pending[state]
            if p["result"].get("error"):
                return "error", p["result"]["error"]
            return "ok", p["result"]["uid"]
    return "error", "no such sign-in"
