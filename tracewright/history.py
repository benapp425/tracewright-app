"""Project history as git commits: a checkpoint before every agent turn, on request, and before
any restore -- so every change the agent (or the user) makes can be seen and undone.

Projects Tracewright created (or copied in) get a normal .git. A project opened in place keeps its
own git untouched: Tracewright's checkpoints go to a shadow repository in .tracewright/history.git
(excluded from the user's repository)."""
import os, subprocess, datetime

IDENT = ["-c", "user.name=Tracewright", "-c", "user.email=tracewright@localhost", "-c", "commit.gpgsign=false",
         "-c", "core.quotepath=false"]
SHADOW = os.path.join(".tracewright", "history.git")


def _where(root):
    sh = os.path.join(root, SHADOW)
    if os.path.isdir(sh):
        return ["--git-dir", sh, "--work-tree", root]
    return []


def has_repo(root):
    return os.path.isdir(os.path.join(root, SHADOW)) or os.path.isdir(os.path.join(root, ".git"))


def _git(root, *args, check=False, timeout=120):
    r = subprocess.run(["git", *IDENT, *_where(root), *args], cwd=root, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[-800:])
    return r


def available():
    try:
        return subprocess.run(["git", "--version"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def init(root, shadow=False):
    if not available():
        return False
    if shadow:
        sh = os.path.join(root, SHADOW)
        if not os.path.isdir(sh):
            os.makedirs(os.path.dirname(sh), exist_ok=True)
            subprocess.run(["git", "init", "-q", "--bare", sh], capture_output=True)
            subprocess.run(["git", "--git-dir", sh, "config", "core.bare", "false"], capture_output=True)
            subprocess.run(["git", "--git-dir", sh, "config", "core.worktree", root], capture_output=True)
            os.makedirs(os.path.join(sh, "info"), exist_ok=True)
            with open(os.path.join(sh, "info", "exclude"), "a") as f:
                f.write("/.tracewright/\n.git/\nbuild/\n*-backups/\n*.lck\nfp-info-cache\n")
        user_git = os.path.join(root, ".git")
        if os.path.isdir(user_git):                     # keep our files out of the user's repository
            ex = os.path.join(user_git, "info", "exclude")
            os.makedirs(os.path.dirname(ex), exist_ok=True)
            txt = open(ex).read() if os.path.exists(ex) else ""
            if ".tracewright/" not in txt:
                with open(ex, "a") as f:
                    f.write("\n# Tracewright (app state and checkpoints)\n.tracewright/\n")
        return True
    if not os.path.isdir(os.path.join(root, ".git")):
        _git(root, "init", "-q", "-b", "main")
        if _git(root, "rev-parse", "--abbrev-ref", "HEAD").returncode != 0:
            _git(root, "init", "-q")
    return True


LARGE = 10 * 1024 * 1024        # files above this stay out of the history (STEP models, jars, big PDFs)


def _exclude_large(root):
    """List big untracked files in the repository's exclude file (and .tracewright/large-files.txt)."""
    tracked = set(_git(root, "ls-files").stdout.splitlines())
    big = []
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in (".git", ".tracewright", "build", "node_modules", "__pycache__")]
        for f in fns:
            full = os.path.join(dp, f)
            try:
                if os.path.getsize(full) > LARGE:
                    rel = os.path.relpath(full, root)
                    if rel not in tracked:
                        big.append(rel)
            except OSError:
                pass
    if not big:
        return []
    gdir = os.path.join(root, SHADOW) if os.path.isdir(os.path.join(root, SHADOW)) else os.path.join(root, ".git")
    ex = os.path.join(gdir, "info", "exclude")
    os.makedirs(os.path.dirname(ex), exist_ok=True)
    cur = open(ex).read() if os.path.exists(ex) else ""
    new = [b for b in big if "/" + b not in cur.splitlines()]
    if new:
        with open(ex, "a") as f:
            f.write("".join("/" + b.replace(os.sep, "/") + "\n" for b in new))
        os.makedirs(os.path.join(root, ".tracewright"), exist_ok=True)
        with open(os.path.join(root, ".tracewright", "large-files.txt"), "a") as f:
            f.write("".join(f"{b}\n" for b in new))
    return big


# Tracewright's own caches: rebuilt from the design, big, and changed on every edit (a 20 MB 3D model per
# board revision was going into every checkpoint). Never committed; untracked if an older version was.
NOT_HISTORY = (".tracewright/cache/", ".tracewright/timelapse/", ".tracewright/tmp/", ".tracewright/sessions/")


def _exclude_state(root):
    gdir = os.path.join(root, SHADOW) if os.path.isdir(os.path.join(root, SHADOW)) else os.path.join(root, ".git")
    ex = os.path.join(gdir, "info", "exclude")
    try:
        os.makedirs(os.path.dirname(ex), exist_ok=True)
        cur = open(ex).read().splitlines() if os.path.exists(ex) else []
        new = ["/" + p for p in NOT_HISTORY if "/" + p not in cur]
        if new:
            with open(ex, "a") as f:
                f.write("".join(p + "\n" for p in new))
    except OSError:
        pass
    tracked = [l for l in _git(root, "ls-files", "--", *[p.rstrip("/") for p in NOT_HISTORY]).stdout.splitlines() if l]
    if tracked:
        _git(root, "rm", "-r", "--cached", "--quiet", "--ignore-unmatch", "--", *[p.rstrip("/") for p in NOT_HISTORY])


def snapshot(root, message, allow_empty=False):
    """Commit everything that changed; returns the short hash, or None when nothing changed."""
    if not available() or not has_repo(root):
        return None
    _exclude_large(root)
    _exclude_state(root)
    _git(root, "add", "-A")
    st = _git(root, "status", "--porcelain")
    if not st.stdout.strip() and not allow_empty:
        return None
    r = _git(root, "commit", "-q", "-m", message, *(["--allow-empty"] if allow_empty else []))
    if r.returncode != 0:
        return None
    return _git(root, "rev-parse", "--short", "HEAD").stdout.strip()


def log(root, n=80):
    if not has_repo(root):
        return []
    r = _git(root, "log", f"-{n}", "--date=iso-strict", "--pretty=format:%h%x1f%ad%x1f%an%x1f%s", "--shortstat")
    out, cur = [], None
    for line in r.stdout.splitlines():
        if "\x1f" in line:
            h, d, a, s = line.split("\x1f", 3)
            cur = {"hash": h, "date": d, "author": a, "message": s, "stat": ""}
            out.append(cur)
        elif line.strip() and cur is not None:
            cur["stat"] = line.strip()
    return out


def changed_files(root, rev):
    r = _git(root, "show", "--name-status", "--pretty=format:", rev)
    return [l.split("\t", 1) for l in r.stdout.splitlines() if "\t" in l]


def diff_stat(root, since="HEAD"):
    """Files changed in the working tree since `since` (the user's edits between agent turns)."""
    r = _git(root, "status", "--porcelain")
    return [l[3:] for l in r.stdout.splitlines() if l.strip()]


def restore(root, rev):
    """Put the files back as they were at `rev` (a new commit: nothing is lost)."""
    snapshot(root, f"Before restoring {rev}")
    _git(root, "checkout", rev, "--", ".", check=True)
    # files added after rev are removed too
    now = set(_git(root, "ls-files").stdout.splitlines())
    then = set(_git(root, "ls-tree", "-r", "--name-only", rev).stdout.splitlines())
    for f in now - then:
        p = os.path.join(root, f)
        if os.path.isfile(p):
            os.remove(p)
    return snapshot(root, f"Restored to {rev}") or rev


def show_file(root, rev, path):
    r = _git(root, "show", f"{rev}:{path}")
    return r.stdout if r.returncode == 0 else None
