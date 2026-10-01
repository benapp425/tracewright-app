"""Cost before you run: what a request will likely cost, read from this app's own runs.

Each past turn's request (the user's message) is sorted into a kind -- a whole design, routing, placement, the
schematic, checks and review, parts, a question, other -- and the turn's cost read from the transcript. A new
request's estimate is the median of the same kind (with the middle half as its spread), across the projects on this
Mac, the newest 60 of a kind. The share of the plan's usage limit comes from how the limit's readings moved against
the cost spent between them (usage.history), once there are a few.

    estimate(store, text)  ->  {"kind", "label", "cost", "low", "high", "n", "minutes", "pct", "window"} | {"kind", "n": 0}
"""
import json, os, re, statistics, time

KINDS = [("design", "a whole design", r"\b(start the design|design (it|the board)|whole (board|design)|from scratch|full design|build the board)\b"),
         ("route", "routing", r"\brout(e|es|ed|ing)\b|\btracks?\b"),
         ("place", "placement", r"\b(plac(e|ing|ement)|layout|floorplan|move [a-z]+\d)\b"),
         ("schematic", "the schematic", r"\b(schematic|symbol|net ?label|sheet|wire)s?\b"),
         ("checks", "checks and review", r"\b(check|drc|erc|verify|review|flag)s?\b"),
         ("parts", "parts", r"\b(part|bom|stock|lcsc|jlc|datasheet|mpn|cheaper|alternative|substitut\w*)s?\b")]
LABEL = {k: l for k, l, _ in KINDS} | {"question": "a question", "other": "a request like this"}
_cache = {"at": 0.0, "turns": None}


def kind_of(text):
    t = (text or "").strip()
    low = t.lower()
    for k, _, rx in KINDS:
        if re.search(rx, low):
            return k
    if len(t) < 160 and t.endswith("?"):
        return "question"
    return "other"


def turns(store, ttl=60.0):
    """Every past turn the app ran: [(kind, cost, seconds, finished at)], cached a minute."""
    now = time.time()
    if _cache["turns"] is not None and now - _cache["at"] < ttl:
        return _cache["turns"]
    out = []
    for root in store.roots() if hasattr(store, "roots") else []:   # every project's folder (summaries would read more)
        d = os.path.join(root, ".tracewright", "sessions")
        try:
            names = [n for n in os.listdir(d) if n.endswith(".jsonl")]
        except OSError:
            continue
        for n in names:
            recs = []
            try:
                with open(os.path.join(d, n), encoding="utf-8") as f:
                    for line in f:
                        try:
                            recs.append(json.loads(line))
                        except ValueError:
                            pass
            except OSError:
                continue
            from .costs import per_turn
            recs, _ = per_turn(recs)
            asked = {}
            for r in recs:
                if r.get("kind") == "user" and r.get("turn"):
                    asked[r["turn"]] = r.get("text") or ""
                elif r.get("kind") == "done" and r.get("turn") in asked and (r.get("cost") or 0) > 0 and not r.get("is_error"):
                    out.append((kind_of(asked[r["turn"]]), float(r["cost"]), (r.get("duration_ms") or 0) / 1000.0, r.get("t") or 0.0))
    out.sort(key=lambda x: x[3])
    _cache.update(at=now, turns=out)
    return out


def pct_per_usd(store):
    """How much of the plan's limit a dollar of runs uses, from consecutive readings of one window and the turns that
    finished between them (median of the pairs), and the window; (None, None) without enough of them."""
    from . import usage
    hist = [h for h in usage.history() if h.get("used") is not None]
    done = turns(store)
    ratios, window = [], None
    for a, b in zip(hist, hist[1:]):
        if a.get("window") != b.get("window") or a.get("resets_at") != b.get("resets_at") or b["used"] < a["used"]:
            continue
        spent = sum(c for _, c, _, t in done if a["at"] < t <= b["at"] + 5)
        if spent >= 0.05:
            ratios.append((b["used"] - a["used"]) / spent)
            window = b.get("window")
    if len(ratios) < 2:
        return None, None
    return statistics.median(ratios), window


def estimate(store, text):
    k = kind_of(text)
    past = [t for t in turns(store) if t[0] == k][-60:]
    if len(past) < 3:                                    # too few of this kind: the app's turns as a whole
        past = turns(store)[-60:]
        basis = "other"
    else:
        basis = k
    if len(past) < 3:
        return {"kind": k, "label": LABEL[k], "n": 0}
    costs = sorted(c for _, c, _, _ in past)
    q = statistics.quantiles(costs, n=4) if len(costs) >= 4 else [costs[0], statistics.median(costs), costs[-1]]
    out = {"kind": k, "label": LABEL[k], "basis": basis, "n": len(past), "cost": round(statistics.median(costs), 2),
           "low": round(q[0], 2), "high": round(q[-1], 2), "minutes": round(statistics.median(s for _, _, s, _ in past) / 60.0, 1)}
    r, window = pct_per_usd(store)
    if r is not None:
        from .usage import WINDOWS
        out["pct"] = round(min(100.0, out["cost"] * r * 100), 1)
        out["window"] = WINDOWS.get(window, "plan")
    return out
