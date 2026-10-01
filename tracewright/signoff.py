"""Signing a design off before it is ordered. The Sign-off page (Checks > Sign-off) brings together what the user
needs to decide: whether the checks ran on the design as it is now and what they found, each waiver (a waiver
on an error only counts once the user approves it), the stages, what only the built board can show, and how
the run went. Signing off records who, when, the history commit and a fingerprint of the design files
(tracewright.json "signoff"); ordering needs a sign-off whose fingerprint still matches.

Waivers live in tracewright.json checks.waive: {key, reason, by: claude | user, at, severity, message, title,
source, approved, approved_at}; Claude writes them with its `waive` tool, the user approves, rejects or adds them.

Requirements (the guided start's list, else the bullets of docs/requirements.md, and the limits the user set) are
shown with their evidence (tracewright.json "evidence": {req, kind: check | calc | datasheet | sim | hardware,
label, ref, status: ok | warn | fail | open, by, at}, written by Claude's `evidence` tool; each limit's evidence comes
from the req.limits check itself)."""
import datetime, hashlib, json, os, re, time

from . import gates


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def design_hash(project):
    """A fingerprint of the design: every schematic sheet, the board and the KiCad project, by content."""
    h = hashlib.sha256()
    files = []
    for d, dirs, fs in os.walk(project.tw.hw):
        dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x not in ("backups", "build"))
        files += [os.path.join(d, f) for f in fs if f.endswith((".kicad_sch", ".kicad_pcb", ".kicad_pro"))]
    for f in sorted(files):
        h.update(os.path.relpath(f, project.root).encode() + b"\0")
        try:
            with open(f, "rb") as fh:
                h.update(fh.read())
        except OSError:
            pass
    return h.hexdigest()[:16]


def current(project):
    """The sign-off with valid: False when the design changed after it (None when there is none)."""
    s = project.cfg.get("signoff")
    if not s:
        return None
    return {**s, "valid": s.get("hash") == design_hash(project)}


# ------------------------------------------------------------------ waivers
def waivers(project):
    out = []
    for x in (project.cfg.get("checks") or {}).get("waive") or []:
        w = dict(x) if isinstance(x, dict) else {"key": str(x)}
        if w.get("key"):
            out.append(w)
    return out


def _save_waivers(project, items):
    project.cfg.setdefault("checks", {})["waive"] = items
    project.save()


def set_waiver(project, key, reason, by, severity="", message="", title="", source=""):
    """Add or replace the waiver for a finding. By the user it holds at once; by Claude, on an error it waits for
    the user's approval. title: the decision in a few words ("J201 and J202 share a land pattern on purpose");
    source: where it is shown (a data sheet's page, an app note)."""
    items = [w for w in waivers(project) if w["key"] != key]
    w = {"key": key, "reason": reason.strip(), "by": by, "at": now()}
    if severity:
        w["severity"] = severity
    if message:
        w["message"] = message[:200]
    if title.strip():
        w["title"] = title.strip()[:120]
    if source.strip():
        w["source"] = source.strip()[:160]
    if by == "user":
        w["approved"] = True
    items.append(w)
    _save_waivers(project, items)
    return w


def approve(project, key):
    items = waivers(project)
    w = next((x for x in items if x["key"] == key), None)
    if not w:
        raise KeyError(f"no waiver for {key}")
    w["approved"], w["approved_at"] = True, now()
    _save_waivers(project, items)
    return w


def remove_waiver(project, key):
    items = waivers(project)
    kept = [x for x in items if x["key"] != key]
    if len(kept) == len(items):
        raise KeyError(f"no waiver for {key}")
    _save_waivers(project, kept)


# ------------------------------------------------------------------ status
# a citation in a reason: "(PDS 10164227 rev C sheet 4)", "UBX-22020019 R02 s4.4 p.61", "docs/06 s8"
_CITE = re.compile(r"\b(?:p\.\s?\d+|page\s\d+|sheet\s\d+|rev\s[A-Z0-9]+\b|s\d+(?:\.\d+)+|UBX-\d+|AN\d{2,}|data ?sheet|manual|docs/\S+)", re.I)


def sources(text):
    """Up to two citations from a reason, as written: a document number with the words around it ("SAM-M10Q
    integration manual UBX-22020019 R02 s4.4 p.61", "docs/06 s8"), then a parenthesised citation ("PDS 10164227 rev
    C sheet 4"); a bare section number only when there is nothing better."""
    text = text or ""
    docs, paren = [], []
    for m in re.finditer(r"(?:[A-Za-z][\w.-]*\s+){0,3}(?:UBX-\d+[\w-]*|AN\d{2,}\b)(?:\s+(?:R\d+|rev\s?\w+|s\d+(?:\.\d+)*|p\.\s?\d+|page\s\d+|sheet\s\d+))*"
                         r"|docs/[\w.-]+(?:\s+s\d+(?:\.\d+)*)?", text, re.I):
        docs.append(m.group(0).strip().rstrip(".,;:'"))
    for m in re.finditer(r"\(([^()]{4,90})\)", text):
        if _CITE.search(m.group(1)) and not any(m.group(1) in d for d in docs):
            paren.append(m.group(1).strip())
    bare = [p for p in paren if re.fullmatch(r"s\d+(?:\.\d+)*|p\.\s?\d+", p)]
    out = [p for p in paren if p not in bare] + docs + ([] if docs or len(paren) > len(bare) else bare)
    seen, uniq = set(), []
    for x in out:
        if x.lower() not in seen:
            seen.add(x.lower())
            uniq.append(x)
    return uniq[:2]


def first_clause(text, n=120):
    """A reason's opening clause, as a title: 'Deliberate: J201 and J202 ... sheet 4); the CM5 ...' ->
    'J201 and J202 ... (PDS 10164227 rev C sheet 4)'."""
    t = re.sub(r"^(deliberate|intentional|by design|accepted|ok|fine)\s*[:,.-]\s*", "", (text or "").strip(), flags=re.I)
    depth, cut = 0, len(t)
    for i, ch in enumerate(t):                          # the first ';' or sentence end outside brackets
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and (ch == ";" or (ch == "." and (i + 1 == len(t) or t[i + 1] == " ") and not re.search(r"\b(?:p|s|e\.g|i\.e|vs|approx|rev)$", t[:i], re.I))):
            cut = i
            break
    c = t[:cut].strip()
    if len(c) > n:
        c = c[:n - 1].rsplit(" ", 1)[0] + "…"
    return c


def _results_index(res):
    """{key: finding (with check_title, group)} for the findings that count, and for the ones a waiver holds."""
    found, held, can_tell = {}, {}, True
    for c in (res or {}).get("checks", []):
        meta = {"check_title": c.get("title", ""), "group": c.get("group", ""), "check": c.get("id", "")}
        for f in c.get("findings", []):
            found[f["key"]] = {**f, **meta}
        for f in c.get("waived_findings", []):
            held[f["key"]] = {**f, **meta}
        if c.get("waived") and "waived_findings" not in c:
            can_tell = False                              # results from before the waived findings were kept
    return found, held, can_tell


def waiver_view(w, found, held, can_tell, fresh):
    f = found.get(w["key"]) or held.get(w["key"])
    sev = (f or {}).get("severity") or w.get("severity") or ""
    state = "proposed" if f and f.get("waiver") else "applies" if (w.get("approved") or w.get("by") == "user" or sev != "error") else "proposed"
    if not f and state != "proposed":
        state = "applies"
    if fresh and can_tell and not f:
        state = "unused"                                  # the finding is gone: the waiver no longer does anything
    message = (f or {}).get("message") or w.get("message", "")
    title = w.get("title") or message or first_clause(w.get("reason", "")) or w["key"]
    reason = w.get("reason", "")
    detail = reason if title != first_clause(reason) else reason[len(reason) - len(reason.lstrip()):]
    return {**w, "severity": sev, "state": state, "message": message, "title": title,
            "summary": first_clause(reason, 200) if title != first_clause(reason) else "", "detail": detail,
            "sources": [w["source"]] if w.get("source") else sources(reason),
            "check": (f or {}).get("check_title", ""), "group": (f or {}).get("group", "")}


# the req.limits finding keys for each limit
_LIMIT_KEYS = {"max_size_mm": ("req:size",), "layers": ("req:layers",), "thickness_mm": ("req:thickness",), "copper_oz": ("req:copper",),
               "max_height_mm": ("req:height",), "temp_c": ("req:temp",), "cost_usd": ("req:cost",)}


def requirements(project, res=None, fresh=False):
    """[{id, text, value, kind: brief | limit, evidence: [{kind, label, ref, status, by}]}]: the guided start's
    requirements (else the bullets of docs/requirements.md) and the limits, each with what shows it is met."""
    from tw import constraints
    reqs = []
    try:
        from . import canvas
        items = ((canvas.load(project.root).get("requirements") or {}).get("items")) or []
    except Exception:
        items = []
    for i, it in enumerate(items[:40]):
        if it.get("label") or it.get("value"):
            reqs.append({"id": f"r{i + 1}", "text": str(it.get("label") or ""), "value": str(it.get("value") or ""), "kind": "brief"})
    if not reqs:                                          # the bullets of docs/requirements.md
        try:
            with open(os.path.join(project.root, "docs", "requirements.md"), encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except OSError:
            lines = []
        for ln in lines:
            m = re.match(r"^\s{0,1}[-*]\s+(?!\[)(.{6,})$", ln) or re.match(r"^\s{0,1}\d+[.)]\s+(.{6,})$", ln)
            if m and not ln.lstrip().startswith(("#", ">")):
                txt = re.sub(r"\*\*|__|`", "", m.group(1)).strip()
                label, _, value = txt.partition(":") if ":" in txt[:40] else (txt, "", "")
                reqs.append({"id": f"r{len(reqs) + 1}", "text": label.strip(), "value": value.strip(), "kind": "brief"})
            if len(reqs) >= 40:
                break
    lim = constraints.get(project.cfg)
    for k in constraints.ORDER:
        if k in lim:
            label = constraints.OPTIONS[k][0]
            val = constraints.fmt(k, lim[k])
            val = val[len(label):].strip() if val.lower().startswith(label.lower()) else val
            reqs.append({"id": f"lim:{k}", "text": label, "value": val, "kind": "limit", "limit": k})
    for r in reqs:
        r["evidence"] = []
    by_id = {r["id"]: r for r in reqs}
    by_text = {r["text"].strip().lower(): r for r in reqs if r["text"]}
    for e in (project.cfg.get("evidence") or []):
        if not isinstance(e, dict):
            continue
        r = by_id.get(str(e.get("req"))) or by_text.get(str(e.get("req") or "").strip().lower())
        if r is None:                                     # a requirement named by its start
            q = str(e.get("req") or "").strip().lower()
            r = next((x for x in reqs if q and (x["text"].lower().startswith(q) or q.startswith(x["text"].lower()))), None)
        if r is not None:
            r["evidence"].append({k: e.get(k) for k in ("kind", "label", "ref", "status", "by", "at")})
    # each limit's evidence: the limits check, when it ran
    chk = next((c for c in (res or {}).get("checks", []) if c.get("id") == "req.limits"), None)
    if chk and chk.get("status") in ("pass", "warn", "fail"):
        keys = [f.get("key", "") for f in chk.get("findings", [])]
        for r in reqs:
            if r["kind"] != "limit" or r["limit"] not in _LIMIT_KEYS:
                continue
            hit = [f for f in chk.get("findings", []) if any(f.get("key", "").startswith(p) for p in _LIMIT_KEYS[r["limit"]])]
            if hit:
                worst = "fail" if any(f.get("severity") == "error" for f in hit) else "warn"
                r["evidence"].append({"kind": "check", "label": hit[0].get("message", ""), "ref": "req.limits", "status": worst, "by": "check"})
            else:
                r["evidence"].append({"kind": "check", "label": "The limits check found it met" + ("" if fresh else " (before the last change)"),
                                      "ref": "req.limits", "status": "ok" if fresh else "warn", "by": "check"})
    return reqs


def add_evidence(project, req, kind, label, ref="", status="ok", by="claude"):
    items = [e for e in (project.cfg.get("evidence") or []) if isinstance(e, dict)
             and not (str(e.get("req")) == str(req) and e.get("label") == label)]
    e = {"req": str(req)[:120], "kind": kind if kind in ("check", "calc", "datasheet", "sim", "hardware", "note") else "note",
         "label": str(label).strip()[:240], "ref": str(ref or "")[:160],
         "status": status if status in ("ok", "warn", "fail", "open") else "ok", "by": by, "at": now()}
    items.append(e)
    project.cfg["evidence"] = items
    project.save()
    return e


def remove_evidence(project, req, label=None):
    items = project.cfg.get("evidence") or []
    kept = [e for e in items if not (isinstance(e, dict) and str(e.get("req")) == str(req) and (label is None or e.get("label") == label))]
    project.cfg["evidence"] = kept
    project.save()
    return len(items) - len(kept)


def bringup_steps(project):
    """(steps, done) in docs/bring-up.md ('- [ ]' / '- [x]'), or None without a plan."""
    try:
        with open(os.path.join(project.root, "docs", "bring-up.md"), encoding="utf-8") as fh:
            t = fh.read()
    except OSError:
        return None
    todo = len(re.findall(r"^\s*[-*]\s+\[ \]", t, re.M))
    done = len(re.findall(r"^\s*[-*]\s+\[[xX]\]", t, re.M))
    return {"steps": todo + done, "done": done}


def status(project):
    """Everything the Sign-off page shows, and what stands in the way of signing off."""
    res, fresh = gates.checks_fresh(project)
    found, held, can_tell = _results_index(res)
    items = [waiver_view(w, found, held, can_tell, bool(res and fresh)) for w in waivers(project)]
    counts = (res or {}).get("counts") or {}
    blockers, todo = [], []                              # blockers (text), and the same as {text, action} for the page
    if not res:
        blockers.append("The checks have not run yet.")
        todo.append({"text": "Run the checks.", "action": "run"})
    elif not fresh:
        blockers.append("The design changed after the checks last ran: run them again.")
        todo.append({"text": "Run the checks again: the design changed after the last run.", "action": "run"})
    prop = [w for w in items if w["state"] == "proposed"]
    if counts.get("error"):
        n = counts["error"]
        blockers.append(f"{n} error{'s' if n != 1 else ''} left" + (f", {len(prop)} with a waiver waiting for your approval" if prop else "") + ".")
        bare = n - len(prop)
        if bare > 0:
            todo.append({"text": f"Fix {bare} error{'s' if bare != 1 else ''} the checks found.", "action": "findings"})
    if prop:
        todo.append({"text": f"Approve or reject {len(prop)} waiver{'s' if len(prop) != 1 else ''} Claude proposed for errors.", "action": "waivers"})
    try:
        from .approvals import Approvals
        waiting = Approvals(project).pending()
    except Exception:
        waiting = []
    if waiting:
        blockers.append(f"{len(waiting)} change{'s' if len(waiting) != 1 else ''} Claude made wait{'s' if len(waiting) == 1 else ''} for your OK.")
        todo.append({"text": f"Keep or undo {len(waiting)} change{'s' if len(waiting) != 1 else ''} that need your OK.", "action": "approvals"})
    so = current(project)
    reqs = requirements(project, res, bool(res and fresh))
    return {"checks": {"generated": (res or {}).get("generated"), "fresh": bool(res and fresh), "counts": counts,
                       "waived": (res or {}).get("waived") or {}},
            "waivers": sorted(items, key=lambda w: ({"proposed": 0, "applies": 1, "unused": 2}.get(w["state"], 3),
                                                    {"error": 0, "warning": 1}.get(w.get("severity"), 2), w["title"].lower())),
            "requirements": reqs, "stages": project.stages(), "signoff": so, "can_sign": not blockers, "blockers": blockers,
            "todo": todo, "bringup": os.path.exists(os.path.join(project.root, "docs", "bring-up.md")),
            "bringup_steps": bringup_steps(project), "report": run_report(project)}


def sign(project, who, note="", commit=None):
    st = status(project)
    if not st["can_sign"]:
        raise ValueError(" ".join(st["blockers"]))
    project.cfg["signoff"] = {"by": who, "at": now(), "hash": design_hash(project), "commit": commit, "note": note.strip()[:400],
                              "counts": st["checks"]["counts"], "waivers": sum(1 for w in st["waivers"] if w["state"] == "applies")}
    project.save()
    return current(project)


def revoke(project):
    project.cfg.pop("signoff", None)
    project.save()


# ------------------------------------------------------------------ how the run went
def _intervals(project):
    """(stage id, start, end) for each stage that ran, from the stage log (else from each stage's last update)."""
    from .projects import STAGES
    ids = [s[0] for s in STAGES]
    log = project.cfg.get("stage_log") or []
    out = {}
    if log:
        for e in log:
            sid, st, t = e.get("s"), e.get("st"), e.get("t", 0)
            if sid not in ids:
                continue
            cur = out.setdefault(sid, [None, None])
            if st == "active" and cur[0] is None:
                cur[0] = t
            if st in ("done", "skipped", "blocked"):
                cur[1] = t
                if cur[0] is None:
                    cur[0] = t
    else:                                                  # older projects: consecutive stage updates
        prev = None
        for sid in ids:
            s = (project.cfg.get("stages") or {}).get(sid) or {}
            if s.get("status") == "done" and s.get("updated"):
                try:
                    end = datetime.datetime.fromisoformat(s["updated"]).timestamp()
                except ValueError:
                    continue
                out[sid] = [prev if prev is not None else end, end]
                prev = end
    return [(sid, a, b) for sid, (a, b) in out.items() if a is not None]


def run_report(project):
    """Per stage: when it ran, how long, Claude's working time, turns and cost; and the totals."""
    from .projects import STAGES
    titles = {s[0]: s[1] for s in STAGES}
    d = os.path.join(project.root, ".tracewright", "sessions")
    turns = []
    try:
        names = [n for n in os.listdir(d) if n.endswith(".jsonl")]
    except OSError:
        names = []
    from . import costs
    for n in names:
        done = []
        try:
            with open(os.path.join(d, n), encoding="utf-8") as f:
                for line in f:
                    if '"kind": "done"' not in line:
                        continue
                    try:
                        done.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            pass
        for r in costs.per_turn(done)[0]:                  # each turn's own cost, not the CLI's running total
            turns.append((r.get("t", 0), (r.get("duration_ms") or 0) / 1000, r.get("cost") or 0.0, r.get("stage")))
    iv = _intervals(project)
    rows = []
    for sid, a, b in iv:
        end = b or time.time()
        mine = [x for x in turns if (x[3] == sid) or (x[3] is None and a - 1 <= x[0] <= end + 1)]
        rows.append({"stage": sid, "title": titles.get(sid, sid), "start": a, "end": b, "wall_s": round(end - a),
                     "claude_s": round(sum(x[1] for x in mine)), "turns": len(mine), "cost": round(sum(x[2] for x in mine), 2)})
    rows.sort(key=lambda r: r["start"])
    return {"stages": rows, "turns": len(turns), "claude_s": round(sum(x[1] for x in turns)), "cost": round(sum(x[2] for x in turns), 2),
            "first": min((x[0] for x in turns), default=None), "last": max((x[0] for x in turns), default=None)}


# ------------------------------------------------------------------ the review packet
def packet_html(project):
    """The sign-off as one printable page: verdict, requirements and their evidence, what the checks still find, the
    waivers with their reasons, what needs the built board, the stages and how the run went. Written to
    build/signoff/review-packet.html as well."""
    import html as H
    st = status(project)
    e = lambda v: H.escape(str(v if v is not None else ""))
    so, c = st["signoff"], st["checks"]
    n = c.get("counts") or {}
    if so and so.get("valid"):
        verdict = f"Signed off by {e(so.get('by'))} on {e(so.get('at', '')[:16].replace('T', ' '))}" + (f": “{e(so['note'])}”" if so.get("note") else "")
        vcls = "ok"
    elif st["can_sign"]:
        verdict, vcls = "Ready to sign off", "ok"
    else:
        verdict, vcls = "Not ready: " + " ".join(e(b) for b in st["blockers"]), "bad"
    rows = []
    for r in st["requirements"]:
        ev = "".join(f'<div class="ev {e(x.get("status"))}"><b>{e(x.get("kind"))}</b> {e(x.get("label"))}'
                     f'{" <span class=ref>" + e(x.get("ref")) + "</span>" if x.get("ref") else ""}</div>' for x in r["evidence"]) or '<div class="ev none">No evidence recorded</div>'
        rows.append(f"<tr><td>{e(r['text'])}{'<div class=val>' + e(r['value']) + '</div>' if r['value'] else ''}</td><td>{ev}</td></tr>")
    open_findings = []
    try:
        with open(os.path.join(project.tw.build, "checks.json")) as fh:
            res = json.load(fh)
        for ch in res.get("checks", []):
            for f in ch.get("findings", []):
                if f.get("severity") in ("error", "warning"):
                    open_findings.append(f"<li class='{e(f['severity'])}'><b>{e(f['severity'])}</b> {e(ch.get('title'))}: {e(f.get('message'))}</li>")
    except (OSError, ValueError):
        pass
    wv = []
    for w in st["waivers"]:
        if w["state"] == "unused":
            continue
        who = "you" if w.get("by") == "user" else "Claude"
        appr = " · approved" + (f" {e(w.get('approved_at', '')[:10])}" if w.get("approved_at") else "") if w.get("approved") and w.get("by") != "user" else ""
        wait = " · waiting for approval" if w["state"] == "proposed" else ""
        wv.append(f"<div class='wv'><div class='wt'>{e(w['title'])}</div><div class='wm'>{e(w.get('severity') or 'finding')}"
                  f"{' · ' + e(w['check']) if w.get('check') else ''} · by {who}{appr}{wait}</div>"
                  f"<div class='wr'>{e(w.get('reason'))}</div>"
                  f"{''.join('<div class=src>Source: ' + e(x) + '</div>' for x in w.get('sources') or [])}</div>")
    bu = st.get("bringup_steps")
    steps = []
    try:
        with open(os.path.join(project.root, "docs", "bring-up.md"), encoding="utf-8") as fh:
            for ln in fh:
                m = re.match(r"^\s*[-*]\s+\[([ xX])\]\s+(.*)$", ln)
                if m:
                    steps.append(f"<li class='{'done' if m.group(1) != ' ' else ''}'>{e(m.group(2).strip())}</li>")
    except OSError:
        pass
    stages = " · ".join(f"{e(x['title'])}: {e(x['status'])}" for x in st["stages"])
    rep = st.get("report") or {}
    rep_rows = "".join(f"<tr><td>{e(x['title'])}</td><td>{e(round(x['wall_s'] / 60))} min</td><td>{e(x['turns'])}</td>"
                       f"<td>{'$%.2f' % x['cost'] if x.get('cost') else '—'}</td></tr>" for x in rep.get("stages", []))
    name = e(project.name)
    out = f"""<!doctype html><html><head><meta charset="utf-8"><title>{name}: design review</title>
<style>
body{{font:13px/1.5 -apple-system,BlinkMacSystemFont,"Helvetica Neue",sans-serif;color:#1d1d1f;max-width:900px;margin:32px auto;padding:0 28px}}
h1{{font-size:24px;margin:0}} h2{{font-size:15px;margin:26px 0 8px;border-bottom:1px solid #ddd;padding-bottom:4px}}
.sub{{color:#666;margin:4px 0 16px}} .verdict{{padding:10px 14px;border-radius:8px;font-weight:600}}
.verdict.ok{{background:#e7f6ec;color:#17602f}} .verdict.bad{{background:#fdecea;color:#8a1f17}}
table{{width:100%;border-collapse:collapse}} td,th{{text-align:left;vertical-align:top;padding:6px 8px;border-bottom:1px solid #eee}}
th{{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#777}} .val{{color:#666}}
.ev{{margin:1px 0}} .ev b{{font-weight:600;text-transform:capitalize;margin-right:4px}} .ev.ok{{color:#17602f}} .ev.warn{{color:#8a5a00}}
.ev.fail{{color:#8a1f17}} .ev.open{{color:#555}} .ev.none{{color:#999}} .ref{{color:#888;font-size:12px}}
.wv{{border:1px solid #e5e5e5;border-radius:8px;padding:8px 12px;margin:8px 0;break-inside:avoid}} .wt{{font-weight:600}}
.wm{{color:#777;font-size:12px}} .wr{{margin-top:4px}} .src{{color:#555;font-size:12px;margin-top:2px}}
li.error b{{color:#8a1f17}} li.warning b{{color:#8a5a00}} li b{{text-transform:capitalize;margin-right:4px}} li.done{{color:#777;text-decoration:line-through}}
.print{{float:right;font:inherit;padding:6px 12px;border-radius:6px;border:1px solid #ccc;background:#fff;cursor:pointer}}
@media print{{.print{{display:none}} body{{margin:0;max-width:none}}}}
</style></head><body>
<button class="print" onclick="window.print()">Print or save as PDF</button>
<h1>{name}: design review</h1>
<div class="sub">{e(time.strftime('%B %-d, %Y'))} · design fingerprint {e(design_hash(project))}{' · revision ' + e(so.get('commit')) if so and so.get('commit') else ''}</div>
<div class="verdict {vcls}">{verdict}</div>
<h2>Requirements and evidence</h2>
<table><thead><tr><th>Requirement</th><th>Evidence</th></tr></thead><tbody>{''.join(rows) or '<tr><td colspan=2>No requirements recorded.</td></tr>'}</tbody></table>
<h2>Checks</h2>
<p>{'Ran ' + e((c.get('generated') or '')[:16].replace('T', ' ')) + ('' if c.get('fresh') else ' (before the last change to the design)') if c.get('generated') else 'Not run.'}
{e(n.get('error', 0))} errors, {e(n.get('warning', 0))} warnings, {e(sum(v for v in (c.get('waived') or {}).values() if isinstance(v, (int, float))))} waived.</p>
{'<ul>' + ''.join(open_findings) + '</ul>' if open_findings else ''}
<h2>Waivers</h2>{''.join(wv) or '<p>None.</p>'}
<h2>Needs the built board</h2>{('<p>' + e(bu['done']) + ' of ' + e(bu['steps']) + ' bring-up steps done.</p><ul>' + ''.join(steps) + '</ul>') if bu else '<p>No bring-up plan yet.</p>'}
<h2>Stages</h2><p>{stages}</p>
{('<h2>How the run went</h2><table><thead><tr><th>Stage</th><th>Took</th><th>Turns</th><th>Cost</th></tr></thead><tbody>' + rep_rows + '</tbody></table>') if rep_rows else ''}
</body></html>"""
    try:
        d = os.path.join(project.tw.build, "signoff")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "review-packet.html"), "w", encoding="utf-8") as fh:
            fh.write(out)
    except OSError:
        pass
    return out
