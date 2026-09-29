"""A scripted stand-in for the Claude Code CLI, for tests of the app's agent (tracewright/agent.py) without a
network, an account or a model: it records every prompt it is given (text, and any pictures with their media
types) and answers each turn from a script.

    fake = FakeClaude([reply("Done."), [text("Placing parts"), tool("place", {...}), result()]])
    agent = AgentManager(app, rt); fake.plug(agent)
    await agent.send("place the parts"); await fake.settle(agent)
    fake.prompts[-1].text, fake.prompts[-1].images   # what Claude was sent

A script item is a list of SDK messages for one turn, or a function(prompt) returning one; when the script
runs out, each turn is answered "OK". Messages are built with the helpers below."""
import asyncio, itertools, json

from claude_agent_sdk import (AssistantMessage, ResultMessage, SystemMessage, TextBlock, ToolResultBlock,
                              ToolUseBlock, UserMessage)

_ids = itertools.count(1)
SESSION = "FAKE-SESSION"


def init(session=SESSION, model="claude-opus-5-5"):
    return SystemMessage(subtype="init", data={"session_id": session, "model": model})


def text(t, model="claude-opus-5-5"):
    return AssistantMessage(content=[TextBlock(text=t)], model=model)


def tool(name, args=None, output="ok", is_error=False, model="claude-opus-5-5"):
    """Claude calls a tool (MCP tools by their short name: "place" -> mcp__tw__place) and gets its output."""
    tid = f"toolu_{next(_ids):04d}"
    full = name if name[0].isupper() or name.startswith("mcp__") else f"mcp__tw__{name}"
    return [AssistantMessage(content=[ToolUseBlock(id=tid, name=full, input=args or {})], model=model),
            UserMessage(content=[ToolResultBlock(tool_use_id=tid, content=output, is_error=is_error)])]


def result(cost=0.01, session=SESSION, is_error=False, text=None):
    return ResultMessage(subtype="error_during_execution" if is_error else "success", duration_ms=20, duration_api_ms=15,
                         is_error=is_error, num_turns=1, session_id=session, total_cost_usd=cost, result=text)


def reply(t, cost=0.01):
    """A whole turn that just answers."""
    return [init(), text(t), result(cost)]


def flatten(msgs):
    out = []
    for m in msgs:
        out.extend(flatten(m) if isinstance(m, list) else [m])
    return out


class Prompt:
    """One query as the CLI got it: its text (every text block joined), and its pictures as (media type, bytes)."""

    def __init__(self, text, images=()):
        self.text, self.images = text, list(images)

    def __repr__(self):
        return f"Prompt({self.text[-80:]!r}, {len(self.images)} images)"


class FakeClaude:
    def __init__(self, script=()):
        self.script = list(script)
        self.prompts = []
        self.q = asyncio.Queue()
        self.interrupted = 0
        self.connected = 0
        self.echo_notes = True       # False: notes stay unread, so the CLI runs each as the next turn

    # the SDK client's interface, as the agent uses it
    async def receive_messages(self):
        while True:
            m = await self.q.get()
            if m is None:
                return
            yield m

    async def query(self, prompt):
        if isinstance(prompt, str):
            p = Prompt(prompt)
        else:                                            # streaming input: content blocks (text, then pictures)
            texts, images = [], []
            async for msg in prompt:
                content = msg["message"]["content"]
                for b in (content if isinstance(content, list) else [{"type": "text", "text": content}]):
                    if b["type"] == "text":
                        texts.append(b["text"])
                    elif b["type"] == "image":
                        import base64
                        images.append((b["source"]["media_type"], base64.b64decode(b["source"]["data"])))
            p = Prompt("\n".join(texts), images)
        self.prompts.append(p)
        if p.text.lstrip().startswith("[The user sent this while you were working"):
            # a note mid-turn: the CLI echoes it once Claude takes it in (its read receipt), no turn of its own
            if self.echo_notes:
                self.q.put_nowait(UserMessage(content=p.text))
            return
        step = self.script.pop(0) if self.script else reply("OK")
        msgs = step(p) if callable(step) else step
        for m in flatten(msgs):
            self.q.put_nowait(m)

    async def interrupt(self):
        self.interrupted += 1

    async def disconnect(self):
        self.q.put_nowait(None)

    # wiring into an AgentManager
    def plug(self, agent):
        async def connect():
            if not agent.client:
                self.connected += 1
                agent.session = agent.session or agent.get_session()
                agent.client, agent.client_key = self, "fake"
                agent.reader = asyncio.ensure_future(agent._read(self, agent.session))
            return agent.client
        agent.connect = connect
        return self

    @staticmethod
    async def settle(agent, timeout=5.0):
        """Wait until the agent is idle again (its turn read to the end)."""
        await asyncio.sleep(0.02)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        while agent.busy:
            if loop.time() - t0 > timeout:
                raise TimeoutError("the agent is still busy")
            await asyncio.sleep(0.01)


def events_of(rt):
    """Record what the agent emits on the project's hub: returns the list it fills with (type, fields)."""
    seen = []
    rt.hub.emit = lambda type_, **kw: seen.append((type_, kw))
    return seen


def dumps(events, kinds=None):
    return json.dumps([(t, {k: v for k, v in kw.items() if k in ("text", "message", "status", "busy")})
                       for t, kw in events if not kinds or t in kinds], indent=0)[:3000]
