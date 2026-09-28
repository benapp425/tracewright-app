"""The home screen's design ideas generator: a few answers (what it should do, how ambitious, how it
gets built, the budget, anything you want in it) become three fun boards to mock up, each with the
brief a new project starts from. One short call to Claude (the fast model in Settings), no tools."""
import json, re, random
from claude_agent_sdk import query, ClaudeAgentOptions, AssistantMessage, TextBlock, ResultMessage

QUESTIONS = [
    {"id": "themes", "question": "What should it do?", "kind": "multi", "options": [
        ["lights", "Lights and LEDs", "sun"], ["sound", "Sound and music", "audio-waveform"], ["sense", "Sensors and data", "activity"],
        ["wireless", "Wi-Fi or Bluetooth", "radio"], ["motion", "Motors and robots", "rotate-3d"], ["games", "Games and toys", "sparkles"],
        ["power", "Power and batteries", "zap"], ["bench", "Tools for the bench", "wrench"], ["wear", "Wearable", "user"],
        ["home", "Home and garden", "house"], ["surprise", "Surprise me", "flask-conical"]]},
    {"id": "level", "question": "How complex?", "kind": "single", "options": [
        ["weekend", "Simple", "2 layers and a few parts"],
        ["stretch", "Moderate", "A microcontroller, a few peripherals and firmware"],
        ["showpiece", "Advanced", "4 layers, dense, possibly RF or high-speed"]]},
    {"id": "build", "question": "How will it be built?", "kind": "single", "options": [
        ["jlc", "JLC assembly", "JLCPCB builds and assembles the board"],
        ["self", "Self-assembly", "Bare boards and a stencil, parts from DigiKey or Mouser"],
        ["either", "Either", ""]]},
    {"id": "budget", "question": "Budget for five boards?", "kind": "single", "options": [
        ["30", "Under $30", ""], ["80", "Under $80", ""], ["200", "Under $200", ""], ["any", "No limit", ""]]},
    {"id": "extra", "question": "Anything specific?", "kind": "text",
     "placeholder": "A part, shape or theme (optional)"},
]

SYSTEM = """You invent fun, buildable printed circuit boards for a hobbyist electronics designer who
uses KiCad and orders from JLCPCB. Every idea must be something one person can design in a few
evenings and that works when built: proven parts, no exotic processes, nothing unsafe (no mains
voltage, no lithium charging without a dedicated charger IC). Favour ideas with a clear "wow"
moment when the board powers up.

Answer with JSON only, no prose: {"ideas": [idea, idea, idea]} where each idea is
{"title": short name,
 "pitch": one sentence on what it is and why it is fun,
 "wow": the moment it comes alive, one sentence,
 "difficulty": 1-5,
 "size": board size like "50 x 35 mm" (or a shape: "round, 60 mm"),
 "layers": 2 or 4,
 "cost": estimated USD for five assembled boards (or bare boards plus parts when self-built), a number,
 "parts": [{"name": the key part (a real, available part number), "role": what it does}] (3-6 entries),
 "features": 3-5 short bullet phrases,
 "skills": 2-3 things the builder learns,
 "sourcing": "jlc" or "self",
 "brief": a design brief of 120-220 words for the project: purpose, inputs and outputs, power
          (source, rails, currents), key parts, connectors, size and mounting, what to show on the
          silkscreen, and what "done" means. Plain text, written to the designer.}
Make the three ideas clearly different from each other."""


def _prompt(answers, seed=None):
    a = answers or {}
    themes = [t for t in a.get("themes") or [] if isinstance(t, str)]
    labels = {o[0]: o[1] for o in QUESTIONS[0]["options"]}
    lines = ["Suggest three board ideas."]
    if themes:
        lines.append("It should involve: " + ", ".join(labels.get(t, t) for t in themes) + ".")
    lv = {o[0]: f"{o[1]} ({o[2]})" for o in QUESTIONS[1]["options"]}.get(a.get("level"))
    if lv:
        lines.append(f"Ambition: {lv}.")
    b = a.get("build")
    if b == "jlc":
        lines.append("Built by JLCPCB's turnkey assembly: every part from JLC/LCSC stock, basic parts preferred, "
                     "no hand soldering needed.")
    elif b == "self":
        lines.append("Hand-assembled by the designer from DigiKey/Mouser parts: 0603 or larger passives, no BGAs, "
                     "no QFNs smaller than 4x4 mm, through-hole where it makes sense.")
    bud = a.get("budget")
    if bud and bud != "any":
        lines.append(f"Budget: under ${bud} for five boards including parts.")
    extra = str(a.get("extra") or "").strip()
    if extra:
        lines.append(f"The designer also wants: {extra[:400]}")
    if seed:
        lines.append(f"Make them variations on this idea, each taking it somewhere different: {json.dumps(seed)[:1500]}")
    lines.append(f"(Variation {random.randint(1, 10_000)}: be inventive, avoid the most obvious project for these answers.)")
    return "\n".join(lines)


def _loads(text):
    """JSON from a model's answer, forgiving the usual slips (code fences, trailing commas, smart quotes)."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("Claude did not answer with ideas")
    raw = m.group(0)
    try:
        return json.loads(raw)
    except ValueError:
        fixed = re.sub(r",\s*([}\]])", r"\1", raw.replace("\u201c", '"').replace("\u201d", '"'))
        return json.loads(fixed)


def _parse(text):
    d = _loads(text)
    ideas = d.get("ideas") if isinstance(d, dict) else d
    out = []
    for it in ideas or []:
        if not isinstance(it, dict) or not it.get("title") or not it.get("brief"):
            continue
        out.append({
            "title": str(it["title"])[:80], "pitch": str(it.get("pitch", ""))[:300], "wow": str(it.get("wow", ""))[:300],
            "difficulty": max(1, min(5, int(it.get("difficulty") or 2))), "size": str(it.get("size", ""))[:40],
            "layers": 4 if str(it.get("layers")) == "4" else 2,
            "cost": float(it["cost"]) if isinstance(it.get("cost"), (int, float)) else None,
            "parts": [{"name": str(p.get("name", ""))[:60], "role": str(p.get("role", ""))[:120]}
                      for p in (it.get("parts") or [])[:6] if isinstance(p, dict)],
            "features": [str(f)[:120] for f in (it.get("features") or [])[:5]],
            "skills": [str(f)[:80] for f in (it.get("skills") or [])[:3]],
            "sourcing": "self" if it.get("sourcing") == "self" else "jlc",
            "brief": str(it["brief"])[:3000]})
    if not out:
        raise ValueError("Claude's answer had no usable ideas")
    return out


async def generate(settings, answers, seed=None):
    """Three ideas for these answers (seed: an idea to vary); asks once more if the answer was not
    usable JSON."""
    try:
        return await _generate(settings, _prompt(answers, seed))
    except ValueError:
        return await _generate(settings, _prompt(answers, seed) + "\n\nAnswer with strictly valid JSON: double quotes, "
                                                                  "no trailing commas, no comments.")


async def _generate(settings, prompt):
    env = {"CLAUDE_AGENT_SDK_CLIENT_APP": "tracewright/ideas"}
    key = (settings.get("anthropic_api_key") or "").strip()
    if key:
        env["ANTHROPIC_API_KEY"] = key
    opts = ClaudeAgentOptions(model=settings.get("fast_model") or None, system_prompt=SYSTEM, tools=[], max_turns=1,
                              setting_sources=[], env=env, effort="low", thinking={"type": "disabled"})
    text, err = "", None
    async for m in query(prompt=prompt, options=opts):
        if isinstance(m, AssistantMessage):
            text += "".join(b.text for b in m.content if isinstance(b, TextBlock))
        elif isinstance(m, ResultMessage) and m.is_error:
            err = m.result or m.subtype
    if not text.strip():
        raise RuntimeError(err or "no answer from Claude")
    return _parse(text)
