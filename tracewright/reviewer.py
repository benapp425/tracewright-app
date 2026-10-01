"""A second opinion on a run: a separate Claude, with no tools and only what the run left behind -- what the user
asked, what the designer said it did, what changed, what waits for the user's OK, and what the checks say -- reads it
as a reviewer would, and says what looks wrong or risky, or that it looks right. Its concerns go in the run's card
for the user to send on to the designer, or not. Off unless the user turns it on (Settings): it uses the plan too.

    await reviewer.review(settings, context)  ->  {"verdict": "fine" | "concerns", "concerns": [{what, why, where}],
                                                   "cost", "model"}
"""
import json, os, re

SYSTEM = """You review the work of an electronics design assistant on a KiCad project, as a careful senior engineer
would, from a short account of one run: what the user asked, what the assistant said it did, what changed in the
files, what is waiting for the user's approval, and what the design checks report now. You cannot open the files.

Say what looks wrong or risky in what was done: a change that does not do what was asked, one that breaks something
the checks or the account point to, a part or value that looks unsuitable, an approval the user should look at hard,
work claimed but not shown in the changes. Not style, not things that are fine, not guesses with nothing behind them.

Answer with JSON only, no prose around it:
{"verdict": "fine" | "concerns", "concerns": [{"what": "one line", "why": "one line: what points to it", "where": "a ref, a net, a sheet or a file"}]}
At most 4 concerns, the most important first; "fine" with an empty list when nothing stands out."""

MODELS = {"sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5", "haiku": "claude-haiku-4-5-20251001"}


def context(project, request, answer, changes, approvals=()):
    """The account of one run the reviewer reads (plain text, a few thousand characters at most)."""
    lines = ["What the user asked:", (request or "(a run the assistant carried on by itself)").strip()[:1500], "",
             "What the assistant said at the end:", (answer or "(nothing)").strip()[:2500], "", "What changed:"]
    ch = changes or {}
    b = ch.get("board") or {}
    if b and not b.get("error"):
        bits = [f"{k} {', '.join(b[k][:12])}" for k in ("added", "removed", "moved") if b.get(k)]
        if b.get("tracks"):
            bits.append(f"{b['tracks']:+d} track segments")
        if b.get("vias"):
            bits.append(f"{b['vias']:+d} vias")
        if b.get("routed"):
            bits.append("routed " + ", ".join(n.rsplit("/", 1)[-1] for n in b["routed"][:12]))
        if b.get("outline"):
            bits.append("the outline changed")
        lines.append("- board: " + ("; ".join(bits) or "edited"))
    s = ch.get("schematic") or {}
    if s and not s.get("error"):
        bits = [f"{k} {', '.join(s[k][:12])}" for k in ("added", "removed") if s.get(k)]
        bits += [f"{r} {a} -> {z}" for r, a, z in (s.get("values") or [])[:12]]
        lines.append("- schematic: " + ("; ".join(bits) or "edited"))
    if ch.get("docs"):
        lines.append("- documents: " + ", ".join(ch["docs"][:10]))
    if ch.get("other"):
        lines.append("- other files: " + ", ".join(ch["other"][:10]))
    if approvals:
        lines += ["", "Waiting for the user's approval:"] + [f"- {a['title']}" for a in approvals][:8]
    res = _checks(project)
    if res:
        lines += ["", res]
    return "\n".join(lines)[:9000]


def _checks(project):
    try:
        with open(os.path.join(project.tw.build, "checks.json")) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return ""
    out, n = [], {"error": 0, "warning": 0}
    for c in d.get("checks") or []:
        for f in c.get("findings") or []:
            sev = f.get("severity")
            if sev in n:
                n[sev] += 1
                if len(out) < 12:
                    out.append(f"- {sev}: {c.get('id')}: {str(f.get('message', ''))[:200]}")
    head = f"The design checks (last run {d.get('generated', '?')}): {n['error']} errors, {n['warning']} warnings"
    return head + (":\n" + "\n".join(out) if out else ".")


def parse(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError("no JSON in the answer")
    d = json.loads(m.group(0))
    concerns = [{k: str(c.get(k) or "").strip()[:300] for k in ("what", "why", "where")}
                for c in d.get("concerns") or [] if isinstance(c, dict) and str(c.get("what") or "").strip()][:4]
    return {"verdict": "concerns" if concerns else "fine", "concerns": concerns}


async def review(settings, text):
    from claude_agent_sdk import query, ClaudeAgentOptions, AssistantMessage, ResultMessage, TextBlock
    which = settings.get("second_opinion") or "off"
    model = MODELS.get(which, which)
    env = {"CLAUDE_AGENT_SDK_CLIENT_APP": "tracewright/reviewer"}
    key = (settings.get("anthropic_api_key") or "").strip()
    if key:
        env["ANTHROPIC_API_KEY"] = key
    opts = ClaudeAgentOptions(model=model, system_prompt=SYSTEM, tools=[], max_turns=1, setting_sources=[], env=env)
    out, cost, err = "", None, None
    async for m in query(prompt=text, options=opts):
        if isinstance(m, AssistantMessage):
            out += "".join(b.text for b in m.content if isinstance(b, TextBlock))
        elif isinstance(m, ResultMessage):
            cost = m.total_cost_usd
            if m.is_error:
                err = m.result or m.subtype
    if not out.strip():
        raise RuntimeError(err or "no answer")
    d = parse(out)
    d.update(cost=round(cost or 0.0, 4), model=which)
    return d
