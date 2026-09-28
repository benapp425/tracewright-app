"""GitHub sync for a project: create (or link) a repository, push the project's history to it, pull
what was changed elsewhere (a teammate, your laptop's KiCad, a server running Tracewright).

The token -- a fine-grained personal access token with Contents and Administration read/write on
your repositories, or a classic token with `repo` -- comes from Settings, TW_GITHUB_TOKEN or
GITHUB_TOKEN, or the GitHub CLI's login (`gh auth token`). It reaches git through a credential helper
for each command and is never written into a repository's config or a remote URL.

Projects opened in place keep their own git and remotes; sync is for the projects Tracewright holds.
"""
import os, json, shutil, subprocess, urllib.request, urllib.error
from . import config, history

API = "https://api.github.com"
BRANCH = "main"
# git asks the helper for credentials; it answers with the token from the environment of this one command
HELPER = '!f() { test "$1" = get && echo username=x-access-token && echo "password=$TW_GIT_TOKEN"; }; f'


class GitHubError(RuntimeError):
    pass


def token():
    t = (config.settings().get("github_token") or os.environ.get("TW_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if not t and shutil.which("gh"):
        try:
            t = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            t = ""
    return t


def _api(method, path, body=None, tok=None):
    tok = tok or token()
    if not tok:
        raise GitHubError("no GitHub token: add one in Settings (fine-grained, Contents + Administration read/write)")
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Tracewright"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except urllib.error.URLError as e:
        raise GitHubError(f"GitHub is not reachable: {e.reason}")


def whoami(tok=None):
    code, d = _api("GET", "/user", tok=tok)
    if code != 200:
        raise GitHubError(f"GitHub did not accept the token ({code}: {d.get('message', '')})")
    return d.get("login", "")


def create_repo(name, private=True, owner=None, description=""):
    """A new repository (or the existing one of that name, if you can reach it): {full_name, html_url, clone_url}."""
    body = {"name": name, "private": bool(private), "description": description[:300], "auto_init": False}
    me = whoami()
    path = f"/orgs/{owner}/repos" if owner and owner != me else "/user/repos"
    code, d = _api("POST", path, body)
    if code == 201:
        return {k: d[k] for k in ("full_name", "html_url", "clone_url")}
    if code == 422:                                       # the name is taken: reuse it if it is ours
        code2, d2 = _api("GET", f"/repos/{owner or me}/{name}")
        if code2 == 200:
            return {k: d2[k] for k in ("full_name", "html_url", "clone_url")}
    raise GitHubError(f"could not create the repository ({code}: {d.get('message', '')})")


def _git(root, *args, timeout=300):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", TW_GIT_TOKEN=token())
    r = subprocess.run(["git", *history.IDENT, "-c", "credential.helper=", "-c", f"credential.helper={HELPER}",
                        *history._where(root), *args], cwd=root, capture_output=True, text=True, timeout=timeout, env=env)
    return r


def _check(r, what):
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip().splitlines()
        raise GitHubError(f"{what} failed: {msg[-1] if msg else r.returncode}")
    return r


def _usable(p):
    if p.cfg.get("kind") == "in_place":
        raise GitHubError("this project is opened in place: it keeps your own git, so push it with your own remote")
    if not os.path.isdir(os.path.join(p.root, ".git")):
        raise GitHubError("this project has no git history yet")


def connect(p, repo=None, private=True):
    """Link the project to `repo` (a URL, or "owner/name"), or create a private repository named after
    it, then push. Saves {repo, url, auto_push} in tracewright.json under "github"."""
    _usable(p)
    if repo and ("://" in repo or repo.startswith("git@")):     # an existing repository by URL
        url = repo
        tail = repo.rstrip("/").replace(":", "/").split("/")
        full = "/".join(tail[-2:]).removesuffix(".git")
        html = f"https://github.com/{full}" if "github.com" in repo else repo
    else:
        owner, name = (repo.split("/", 1) if repo and "/" in repo else (None, repo or p.id))
        info = create_repo(name, private=private, owner=owner, description=p.cfg.get("description", ""))
        url, full, html = info["clone_url"], info["full_name"], info["html_url"]
    remotes = _git(p.root, "remote").stdout.split()
    _check(_git(p.root, "remote", "set-url" if "origin" in remotes else "add", "origin", url), "setting the remote")
    p.cfg["github"] = {"repo": full, "url": html, "remote": url,
                       "auto_push": p.cfg.get("github", {}).get("auto_push", True)}
    p.save()
    history.snapshot(p.root, "Connected to GitHub")
    push(p)
    return p.cfg["github"]


def push(p):
    _usable(p)
    if not p.cfg.get("github"):
        raise GitHubError("not connected to GitHub")
    _check(_git(p.root, "push", "-u", "origin", f"HEAD:refs/heads/{BRANCH}"), "push")
    return status(p, fetch=False)


def pull(p):
    """Bring in commits made elsewhere: fast-forward when possible, otherwise merge; on a conflict the
    merge is undone and the files are named, and nothing changes."""
    _usable(p)
    _check(_git(p.root, "fetch", "origin", BRANCH), "fetch")
    st = status(p, fetch=False)
    if not st["behind"]:
        return {**st, "pulled": 0}
    history.snapshot(p.root, "Before pulling from GitHub")          # (nothing, when nothing changed)
    r = _git(p.root, "merge", "--ff-only", f"origin/{BRANCH}")
    if r.returncode != 0:
        r = _git(p.root, "merge", "--no-edit", f"origin/{BRANCH}")
        if r.returncode != 0:
            files = _git(p.root, "diff", "--name-only", "--diff-filter=U").stdout.split()
            _git(p.root, "merge", "--abort")
            raise GitHubError("the changes on GitHub conflict with this project's in " + (", ".join(files[:8]) or "some files")
                              + ": resolve them in a clone, or pick one side and push")
    return {**status(p, fetch=False), "pulled": st["behind"]}


def status(p, fetch=True):
    g = p.cfg.get("github") or {}
    out = {"connected": bool(g), "repo": g.get("repo"), "url": g.get("url"), "auto_push": g.get("auto_push", True),
           "ahead": 0, "behind": 0, "token": bool(token())}
    if not g:
        return out
    if fetch:
        r = _git(p.root, "fetch", "origin", BRANCH)
        if r.returncode != 0:
            out["error"] = (r.stderr or "").strip().splitlines()[-1:] or ["fetch failed"]
            out["error"] = out["error"][0]
            return out
    r = _git(p.root, "rev-list", "--left-right", "--count", f"HEAD...origin/{BRANCH}")
    if r.returncode == 0 and r.stdout.split():
        a, b = r.stdout.split()[:2]
        out["ahead"], out["behind"] = int(a), int(b)
    else:
        out["ahead"] = int(_git(p.root, "rev-list", "--count", "HEAD").stdout.strip() or 0)   # nothing pushed yet
    return out


def clone(url, dest):
    """Clone a repository (private ones through the token) into dest."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", TW_GIT_TOKEN=token())
    r = subprocess.run(["git", "-c", "credential.helper=", "-c", f"credential.helper={HELPER}", "clone", "-q", url, dest],
                       capture_output=True, text=True, timeout=900, env=env)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip().splitlines()
        raise GitHubError(f"could not clone {url}: {msg[-1] if msg else r.returncode}")
    return dest


def is_git_url(s):
    s = (s or "").strip()
    return s.startswith(("https://github.com/", "git@", "ssh://")) or (s.startswith("https://") and s.endswith(".git"))
