"""The account's plan usage as Claude Code last reported it: which limit window applies, how much of it is used,
when it resets (the Agent SDK's RateLimitEvent, sent when the state changes). Kept in the data dir so the run
monitor shows it at once; a reading whose window has since reset is no longer shown.

In-app runs use the signed-in Claude account, so they share this limit with Claude Code anywhere else."""
import json, os, time
from . import config

FILE = "plan_usage.json"
LOG = "plan_usage_log.jsonl"                      # the readings over time (estimate.py learns the limit's share per dollar)
WINDOWS = {"five_hour": "5-hour", "seven_day": "weekly", "seven_day_opus": "weekly Opus", "seven_day_sonnet": "weekly Sonnet",
           "overage": "extra usage"}


def _path():
    return os.path.join(config.data_dir(), FILE)


def seen(info):
    """Record a reading (claude_agent_sdk.RateLimitInfo, or a dict of its fields) and return it as the UI shows it."""
    g = info.get if isinstance(info, dict) else lambda k, d=None: getattr(info, k, d)
    used = g("utilization")
    d = {"status": g("status") or "allowed", "window": g("rate_limit_type"), "used": float(used) if used is not None else None,
         "resets_at": g("resets_at"), "at": round(time.time())}
    d["label"] = label(d)
    try:
        os.makedirs(config.data_dir(), exist_ok=True)
        with open(_path() + ".tmp", "w") as f:
            json.dump(d, f)
        os.replace(_path() + ".tmp", _path())
        log = os.path.join(config.data_dir(), LOG)
        with open(log, "a") as f:
            f.write(json.dumps({k: d[k] for k in ("at", "used", "window", "status", "resets_at")}) + "\n")
        if os.path.getsize(log) > 120_000:                # the last few hundred readings are plenty
            with open(log) as f:
                keep = f.readlines()[-400:]
            with open(log, "w") as f:
                f.writelines(keep)
    except OSError:
        pass
    return d


def history():
    """The readings over time, oldest first: [{at, used, window, status, resets_at}]."""
    out = []
    try:
        with open(os.path.join(config.data_dir(), LOG)) as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def last(now=None):
    """The last reading, or None when there is none or its window has reset since."""
    now = now or time.time()
    try:
        with open(_path()) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    if d.get("resets_at") and d["resets_at"] <= now:
        return None
    d["label"] = label(d)
    return d


def reset_time(now=None):
    """When the limit that stopped a run resets, as last reported (None when not known)."""
    d = last(now)
    return d["resets_at"] if d and d.get("status") == "rejected" and d.get("resets_at") else None


def label(d):
    """"62 % of the 5-hour limit, resets 3:40 pm"; "5-hour limit reached, resets 3:40 pm"."""
    window = WINDOWS.get(d.get("window"), "plan")
    at = ""
    if d.get("resets_at"):
        t = time.localtime(d["resets_at"])
        at = time.strftime("%-I:%M %p", t).lower()
        if time.strftime("%Y%m%d", t) != time.strftime("%Y%m%d"):
            at = time.strftime("%a ", t) + at
        at = ", resets " + at
    if d.get("status") == "rejected":
        return f"{window} limit reached{at}"
    if d.get("used") is not None:
        return f"{round(d['used'] * 100)} % of the {window} limit{at}"
    return (f"near the {window} limit" if d.get("status") == "allowed_warning" else f"within the {window} limit") + at
