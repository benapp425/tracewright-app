"""Claude sessions for a project (Claude Agent SDK): one live conversation at a time per project,
streamed to the UI, with tool permissions asked in the UI, a git checkpoint before every turn, and
every conversation kept (and resumable) in .tracewright/sessions/."""
import os, re, json, time, uuid, asyncio, base64, shlex, traceback
from claude_agent_sdk import (ClaudeSDKClient, ClaudeAgentOptions, AssistantMessage, UserMessage, ResultMessage,
                              SystemMessage, StreamEvent, TextBlock, ToolUseBlock, ToolResultBlock, ThinkingBlock,
                              PermissionResultAllow, PermissionResultDeny)
from . import prompts, history, agent_tools

SAFE_CMDS = {"cd", "ls", "cat", "head", "tail", "wc", "grep", "rg", "pwd", "echo", "which", "file", "stat", "du", "sort",
             "uniq", "diff", "tree", "cut", "tr", "basename", "dirname", "realpath", "date", "true", "sed", "awk", "jq",
             "python3", "python", "./tw", "tw", "kicad-cli", "git", "mkdir", "touch", "cp", "mv", "unzip", "zip", "find",
             "ps", "env", "printf", "test", "["}
GIT_SAFE = {"status", "log", "diff", "show", "add", "commit", "rev-parse", "ls-files", "blame", "branch", "tag",
            "describe", "shortlog", "grep", "stash"}           # not push / reset / clean / checkout / rebase
OPERATORS = {"|", "||", "&&", ";", "&", "|&", ";;"}
# A message the user sends while Claude works reaches it with this header (the CLI takes it in at
# Claude's next tool call, or as the next turn when none is left).
STEER_HEAD = ("[The user sent this while you were working. Take it into account from here on; if it changes "
              "the plan, update the agenda.]\n\n")
# Claude Code's own to-do tools: the agenda tool replaces them, so the user sees one checklist.
NOT_TOOLS = ["TodoWrite", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet"]
DANGEROUS = re.compile(r"\b(sudo|rm\s+-rf?\s+[/~]|mkfs|dd\s+if=|shutdown|reboot|chmod\s+-R\s+777|curl[^|]*\|\s*(ba)?sh|"
                       r"wget[^|]*\|\s*(ba)?sh|:\(\)\s*\{)", re.I)


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# Claude Code ends a turn with "You've hit your session limit · resets 5:10am (America/Chicago)" (or a
# weekly one, "resets Oct 3, 9am (...)") when the account's usage runs out.
LIMIT_RE = re.compile(r"\b(hit|reached|out of)\b[^.\n]{0,40}\blimit\b|\busage limit\b|\blimit reached\b", re.I)
RESET_RE = re.compile(r"resets?\s+(?:at\s+)?(?:(?P<mon>[A-Za-z]{3,9})\.?\s+(?P<day>\d{1,2}),?\s+(?:at\s+)?)?"
                      r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>[ap])\.?m\.?(?:\s*\((?P<tz>[^)]+)\))?", re.I)
LIMIT_MARGIN_S = 90.0          # resume this long after the reset time, in case the clocks differ
LIMIT_UNKNOWN_S = 30 * 60.0    # when the message names no time, try again after this long
LIMIT_MAX_WAITS = 8            # waits in a row before an unattended run gives up
RESUME_TEXT = "Carry on from where you stopped."
RESUME_HIDDEN = ("The account's usage limit stopped your last turn part-way; it has reset now. Carry on from where "
                 "you stopped: check the agenda and the files for what is already done, and don't redo finished work.")


def is_limit(text):
    return bool(text and LIMIT_RE.search(text))


def limit_reset(text, now=None):
    """The epoch time the account's usage limit resets, read from Claude Code's message; None when the
    text is not about a limit or names no time. A time already past by a few minutes means now."""
    import datetime as dt
    if not is_limit(text):
        return None
    m = RESET_RE.search(text)
    if not m:
        return None
    tz = None
    if m.group("tz"):
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(m.group("tz").strip())
        except Exception:
            tz = None
    t0 = time.time() if now is None else now
    cur = dt.datetime.fromtimestamp(t0, tz) if tz else dt.datetime.fromtimestamp(t0).astimezone()
    h = int(m.group("h")) % 12 + (12 if m.group("ap").lower() == "p" else 0)
    mi = int(m.group("m") or 0)
    try:
        if m.group("mon"):
            month = dt.datetime.strptime(m.group("mon")[:3].title(), "%b").month
            cand = cur.replace(month=month, day=int(m.group("day")), hour=h, minute=mi, second=0, microsecond=0)
            if cand < cur - dt.timedelta(days=2):
                cand = cand.replace(year=cand.year + 1)
        else:
            cand = cur.replace(hour=h, minute=mi, second=0, microsecond=0)
            if cand < cur - dt.timedelta(minutes=20):
                cand += dt.timedelta(days=1)
    except ValueError:
        return None
    return max(cand.timestamp(), t0)


class Session:
    """One conversation (our transcript + the SDK's session id for resume)."""

    def __init__(self, rt, sid=None, meta=None):
        self.rt = rt
        self.dir = rt.p.state_dir("sessions")
        self.sid = sid or uuid.uuid4().hex[:12]
        self.meta = meta or {"sid": self.sid, "title": "New conversation", "created": _now(), "updated": _now(),
                             "sdk_session": None, "cost": 0.0, "turns": 0}

    @property
    def path(self):
        return os.path.join(self.dir, f"{self.sid}.jsonl")

    def append(self, rec):
        rec.setdefault("t", time.time())
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")

    def transcript(self):
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
        return out


class AgentManager:
    """Per project: the sessions index, the connected client, the running turn, pending permissions."""

    def __init__(self, app, rt):
        self.app, self.rt = app, rt
        self.hub = rt.hub
        self.client = None
        self.client_key = None
        self.session = None
        self.task = None
        self.pending = {}          # permission request id -> (future, info)
        self.reader = None         # reads everything the CLI sends while it runs
        self.turn = None           # the turn in progress: {"tid", "sess", "done", "auto", "blocks", "started"}
        self.tasks = {}            # CLI task id -> {desc, tool, shown}: commands and subagents it runs
        self.steers = []           # messages sent while Claude works, not yet read: {id, text, sent}
        self.steered = False       # a note went to the CLI during this turn (a stop then also ends its queue)
        self.stopping = False
        self.stop_asked = False        # Stop pressed while a turn was still starting
        self.resume_at = None          # an unattended run waiting for the usage limit to reset: when it carries on
        self._resume_h = None
        self.limit_waits = 0
        self._snap_lock = asyncio.Lock()
        self.allowed = set(self._load_allowed())
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ sessions index
    def _index_path(self):
        return os.path.join(self.rt.p.state_dir("sessions"), "index.json")

    def index(self):
        try:
            with open(self._index_path()) as f:
                return json.load(f)
        except (OSError, ValueError):
            return []

    def _save_meta(self, meta):
        idx = [m for m in self.index() if m["sid"] != meta["sid"]]
        idx.insert(0, meta)
        with open(self._index_path(), "w") as f:
            json.dump(idx, f, indent=1)

    def get_session(self, sid=None):
        if sid:
            for m in self.index():
                if m["sid"] == sid:
                    return Session(self.rt, sid, m)
        if self.session and (sid is None or self.session.sid == sid):
            return self.session
        idx = self.index()
        if idx and not sid:
            return Session(self.rt, idx[0]["sid"], idx[0])
        return Session(self.rt)

    async def new_session(self):
        await self.disconnect()
        self.session = Session(self.rt)
        self._save_meta(self.session.meta)
        return self.session

    async def use_session(self, sid):
        if self.session and self.session.sid == sid:
            return self.session
        await self.disconnect()
        self.session = self.get_session(sid)
        return self.session

    @property
    def busy(self):
        return (self.task is not None and not self.task.done()) or bool(self.turn and self.turn["auto"])

    # ------------------------------------------------------------------ permissions
    def _allowed_path(self):
        return os.path.join(self.rt.p.state_dir(), "permissions.json")

    def _load_allowed(self):
        try:
            with open(self._allowed_path()) as f:
                return json.load(f)
        except (OSError, ValueError):
            return []

    def _remember(self, key):
        self.allowed.add(key)
        with open(self._allowed_path(), "w") as f:
            json.dump(sorted(self.allowed), f, indent=1)

    def _bash_safe(self, cmd):
        """Runs without asking: read-only tools, the toolkit, KiCad, git's read and commit commands,
        writes only inside the project. Split like the shell does -- quotes respected ("a|b" is one
        argument), every line checked, heredoc bodies treated as data -- and anything that runs a
        command inside another ($(...), backticks) asked about."""
        root = self.rt.p.root
        lines = _strip_heredocs(cmd)
        if DANGEROUS.search(lines) or any(x in lines for x in ("$(", "`", "<(", ">(")):
            return False
        try:
            lex = shlex.shlex(lines.replace("\n", " ; "), posix=True, punctuation_chars=True)
            lex.whitespace_split = True
            toks = list(lex)
        except ValueError:
            return False

        def inside(path):
            if path == "/dev/null" or path.isdigit():
                return True
            if path.startswith(("/", "~")):
                return os.path.abspath(os.path.expanduser(path)).startswith(root + os.sep)
            return ".." not in path.split("/")

        segs, cur = [], []
        for tok in toks:
            if tok in OPERATORS:
                segs.append(cur)
                cur = []
            else:
                cur.append(tok)
        segs.append(cur)
        for words in segs:
            clean, i = [], 0
            while i < len(words):                         # redirections: output only into the project
                w = words[i]
                if w and set(w) <= set("<>&|") and ("<" in w or ">" in w):
                    target = words[i + 1] if i + 1 < len(words) else ""
                    if ">" in w and not inside(target):
                        return False
                    i += 2
                    continue
                clean.append(w)
                i += 1
            words = clean
            while words and "=" in words[0] and not words[0].startswith(("./", "/", "-")):
                words = words[1:]                         # VAR=value prefixes
            if not words:
                continue
            exe = words[0] if words[0] == "./tw" else os.path.basename(words[0])
            args = words[1:]
            if exe not in SAFE_CMDS:
                return False
            if exe == "find" and any(a in ("-delete", "-exec", "-execdir", "-ok", "-fprint") for a in args):
                return False
            if exe in ("python3", "python") and "pip" in args[:2]:
                return False
            if exe == "git" and (not args or args[0] not in GIT_SAFE):
                return False
            if exe in ("cp", "mv", "mkdir", "touch", "zip", "unzip", "sed") and \
                    any(a.startswith(("/", "~")) and not inside(a) for a in args):
                return False
        return True

    async def _can_use_tool(self, name, inp, ctx):
        s = self.app.settings
        root = self.rt.p.root
        if name == "AskUserQuestion":
            return await self._ask(inp)
        if name.startswith("mcp__tw__"):
            return PermissionResultAllow()
        if name in ("Read", "Glob", "Grep", "LS", "TodoWrite", "Skill", "NotebookRead", "ToolSearch"):
            return PermissionResultAllow()
        if name in ("WebFetch", "WebSearch") and s.get("allow_web"):
            return PermissionResultAllow()
        if name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            path = inp.get("file_path") or inp.get("notebook_path") or ""
            if path and os.path.abspath(path).startswith(root + os.sep):
                return PermissionResultAllow()
        if name == "Bash":
            cmd = inp.get("command", "")
            if s.get("auto_allow_bash") and self._bash_safe(cmd):
                return PermissionResultAllow()
            first = cmd.strip().split()[0] if cmd.strip() else ""
            if f"Bash:{first}" in self.allowed:
                return PermissionResultAllow()
        if name in self.allowed:
            return PermissionResultAllow()
        # ask the user in the UI
        rid = uuid.uuid4().hex[:10]
        fut = asyncio.get_running_loop().create_future()
        info = {"id": rid, "tool": name, "input": _short(inp), "reason": getattr(ctx, "decision_reason", None)}
        self.pending[rid] = (fut, info)
        self.hub.emit("agent.permission", **info)
        if self.session:
            self.session.append({"kind": "permission", **info})
        try:
            allow, always = await asyncio.wait_for(fut, timeout=900)
        except asyncio.TimeoutError:
            allow, always = False, False
        finally:
            self.pending.pop(rid, None)
        self.hub.emit("agent.permission_done", id=rid, allow=allow)
        if allow:
            if always:
                self._remember(f"Bash:{inp.get('command', '').split()[0]}" if name == "Bash" else name)
            return PermissionResultAllow()
        return PermissionResultDeny(message="The user declined this action. Ask, or find another way.")

    def answer_permission(self, rid, allow, always=False):
        ent = self.pending.get(rid)
        if not ent or ent[1].get("kind") == "question":
            return False
        fut, _ = ent
        if not fut.done():
            fut.set_result((bool(allow), bool(always)))
        return True

    async def _ask(self, inp):
        """Claude's AskUserQuestion: show the questions in the chat and hand the answers to the CLI,
        which reads them from the tool input's `answers` (question text -> option label, a list of
        labels for multiSelect, or the user's own words). In an unattended run (autonomous mode, past
        the intake) nobody is waiting to answer: the question is logged and Claude decides."""
        rid = uuid.uuid4().hex[:10]
        qs = inp.get("questions") or []
        if self.rt.p.unattended():
            rec = {"kind": "question_skipped", "id": rid, "questions": qs}
            if self.session:
                self.session.append(rec)
            self.hub.emit("agent.question_skipped", id=rid, questions=qs)
            return PermissionResultDeny(message="Autonomous run: the user is not waiting to answer. Take your recommended "
                                                "option for each question, record it under 'Assumptions to review' in "
                                                "docs/decisions.md, and continue.")
        fut = asyncio.get_running_loop().create_future()
        info = {"id": rid, "kind": "question", "questions": qs}
        self.pending[rid] = (fut, info)
        self.hub.emit("agent.question", **info)
        if self.session:
            self.session.append({"kind": "question", "id": rid, "questions": qs})
        try:
            answers = await asyncio.wait_for(fut, timeout=3600)
        except asyncio.TimeoutError:
            answers = None
        finally:
            self.pending.pop(rid, None)
        answers = answers or {}
        if self.session:
            self.session.append({"kind": "answer", "id": rid, "answers": answers})
        self.hub.emit("agent.question_done", id=rid, answers=answers)
        if not answers:
            return PermissionResultDeny(message="The user did not answer these questions. Go on with your "
                                                "recommended choices, saying which you took, or ask in the chat.")
        return PermissionResultAllow(updated_input={**inp, "answers": answers})

    def answer_question(self, rid, answers):
        ent = self.pending.get(rid)
        if not ent or ent[1].get("kind") != "question":
            return False
        fut, _ = ent
        if not fut.done():
            fut.set_result({str(k): v for k, v in (answers or {}).items() if v not in (None, "", [])})
        return True

    # ------------------------------------------------------------------ the client
    def _options(self, resume):
        s = self.app.settings
        p = self.rt.p
        # the design tools are always loaded (no deferred tool search round-trips)
        env = {"CLAUDE_AGENT_SDK_CLIENT_APP": "tracewright/0.1.0", "ENABLE_TOOL_SEARCH": "false"}
        key = (s.get("anthropic_api_key") or "").strip()
        if key:
            env["ANTHROPIC_API_KEY"] = key
        server = agent_tools.build(self.rt, self.app)
        tool_names = [f"mcp__tw__{n}" for n in sorted(set(agent_tools.TOOL_NAMES))]
        model = s.get("model") or None
        opts = ClaudeAgentOptions(
            cwd=p.root,
            model=model if model != "default" else None,
            system_prompt={"type": "preset", "preset": "claude_code", "append": prompts.system_append(p)},
            setting_sources=["project"],
            mcp_servers={"tw": server},
            allowed_tools=tool_names,
            permission_mode=s.get("permission_mode") or "acceptEdits",
            can_use_tool=self._can_use_tool,
            include_partial_messages=True,
            disallowed_tools=NOT_TOOLS,
            extra_args={"replay-user-messages": None},    # echoes each message as the CLI takes it in
            resume=resume,
            env=env,
            stderr=lambda line: self.app.log(f"[claude] {line}"),
        )
        try:
            opts.effort = s.get("effort") or None
        except Exception:
            pass
        return opts

    async def connect(self):
        sess = self.session or self.get_session()
        self.session = sess
        key = (sess.sid, self.app.settings.get("model"), self.app.settings.get("permission_mode"),
               bool(self.app.settings.get("anthropic_api_key")), self.app.settings.get("effort"))
        if self.client and self.client_key == key and self.reader and not self.reader.done():
            return self.client
        await self.disconnect()
        client = ClaudeSDKClient(options=self._options(sess.meta.get("sdk_session")))
        try:
            await client.connect()
        except BaseException:                                  # failed or stopped half way: no stray CLI process
            try:
                await client.disconnect()
            except Exception:
                pass
            raise
        self.client, self.client_key = client, key
        self.reader = asyncio.ensure_future(self._read(client, sess))
        return client

    async def disconnect(self):
        t = self.turn
        if t and not t["auto"] and not t["done"].done():
            t["done"].set_exception(RuntimeError("the connection to Claude was closed"))
        elif t and t["auto"]:
            self._end_turn(t, error="stopped")
        reader, self.reader = self.reader, None
        if reader and not reader.done():
            reader.cancel()
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
        self.client = None
        self.client_key = None
        self.tasks = {}

    # ------------------------------------------------------------------ a turn
    async def send(self, text, sid=None, attachments=None, title=None, images=None, hidden=None, by=None):
        """Start a turn. images: PNG files sent with the message (review snapshots), which Claude
        sees directly. hidden: instructions Claude gets with the message that the chat does not show
        (the kickoff's intake instructions: the user sees their own brief)."""
        if self.busy:
            raise RuntimeError("Claude is still working on the last message (interrupt it first)")
        if by is None:
            self.cancel_wait()
        self._drop_steers()
        if sid:
            await self.use_session(sid)
        if not self.session:
            self.session = self.get_session()
        sess = self.session
        if title:
            sess.meta["title"] = title
        elif sess.meta.get("title") in (None, "", "New conversation"):
            sess.meta["title"] = text.strip().splitlines()[0][:70] if text.strip() else "Conversation"
        self._save_meta(sess.meta)
        self.stop_asked = False
        self.task = asyncio.ensure_future(self._turn(sess, text, attachments or [], images or [], hidden, by))
        return sess.sid

    # ------------------------------------------------------------------ the usage limit, unattended
    def _limit_hit(self, sess, text):
        """A turn ended on the account's usage limit. Unattended (autonomous, past the intake), nobody is
        there to say "continue": wait for the reset and carry on. Watched runs just show the message."""
        if not is_limit(text):
            self.limit_waits = 0
            return
        if not self.rt.p.unattended() or self._resume_h is not None:
            return
        self.limit_waits += 1
        now = time.time()
        if self.limit_waits > LIMIT_MAX_WAITS:
            rec = {"kind": "waiting", "until": None, "text": "The usage limit stopped the run again; carry on when it has reset."}
            sess.append(rec)
            self.hub.emit("agent.waiting", sid=sess.sid, until=None, text=rec["text"])
            return
        when = limit_reset(text, now)
        until = (when + LIMIT_MARGIN_S) if when else now + LIMIT_UNKNOWN_S
        self.resume_at = until
        at = time.strftime("%-I:%M %p", time.localtime(until)).lower().replace(" am", " am").replace(" pm", " pm")
        rec = {"kind": "waiting", "until": until, "text": f"Usage limit reached. Claude carries on at {at}."}
        sess.append(rec)
        self.hub.emit("agent.waiting", sid=sess.sid, until=until, text=rec["text"])
        sid = sess.sid
        self._resume_h = asyncio.get_running_loop().call_later(max(1.0, until - now),
                                                               lambda: asyncio.ensure_future(self._resume(sid)))

    def cancel_wait(self):
        if self._resume_h is not None:
            self._resume_h.cancel()
            self._resume_h = None
            if self.resume_at and self.session:
                self.hub.emit("agent.waiting", sid=self.session.sid, until=None, text="")
        self.resume_at = None

    async def _resume(self, sid):
        self._resume_h = None
        self.resume_at = None
        if self.busy or not self.rt.p.unattended():
            return
        try:
            await self.send(RESUME_TEXT, sid=sid, hidden=RESUME_HIDDEN, by="app")
        except Exception as e:
            self.app.log(f"resume after the usage limit: {e}")

    async def steer(self, text, images=None):
        """A message sent while Claude works, as in Claude Code: the CLI takes it in at Claude's next
        tool call, inside the same turn, or as the next turn when Claude was already writing its
        answer. The CLI echoes each message as it reads it, which marks the note read."""
        if not self.busy:
            raise RuntimeError("Claude is not working now; send it as a new message")
        sess = self.session or self.get_session()
        st = {"id": uuid.uuid4().hex[:8], "text": text, "sent": False, "images": list(images or [])}
        self.steers.append(st)
        turn = self.turn["tid"] if self.turn else None
        sess.append({"kind": "steer", "id": st["id"], "text": text, "turn": turn})
        self.hub.emit("agent.steer", sid=sess.sid, id=st["id"], text=text, status="queued", turn=turn)
        await self._send_steers()
        return st["id"]

    async def _send_steers(self):
        """Hand queued notes to the CLI (while a turn is still starting they wait for its prompt)."""
        client = self.client
        if not client or not self.turn:
            return
        for st in self.steers:
            if not st["sent"]:
                st["sent"] = True
                self.steered = True
                note = STEER_HEAD + st["text"]
                await client.query(self._with_images(note, st["images"]) if st.get("images") else note)

    def _steer_read(self, said, sess, tid):
        said = said.strip()
        for st in self.steers:
            if st["sent"] and (said == (STEER_HEAD + st["text"]).strip() or said.endswith(st["text"].strip())):
                self.steers.remove(st)
                sess.append({"kind": "steer_read", "id": st["id"], "turn": tid})
                self.hub.emit("agent.steer", sid=sess.sid, id=st["id"], status="read", turn=tid)
                return True
        return False

    def _drop_steers(self):
        for st in self.steers:
            if self.session:
                self.session.append({"kind": "steer_dropped", "id": st["id"]})
                self.hub.emit("agent.steer", sid=self.session.sid, id=st["id"], status="dropped")
        self.steers = []

    @staticmethod
    async def _with_images(text, images):
        """The message as content blocks: the text, then each picture (streaming input) -- PNG, JPEG, GIF or
        WebP, scaled down when it is bigger than Claude takes (attach.image_for_claude)."""
        from .attach import image_for_claude
        content = [{"type": "text", "text": text}]
        for path in images:
            try:
                got = await asyncio.to_thread(image_for_claude, path)
            except OSError:
                continue
            if got:
                content.append({"type": "image", "source": {"type": "base64", "media_type": got[0],
                                                            "data": base64.b64encode(got[1]).decode()}})
        yield {"type": "user", "message": {"role": "user", "content": content}, "parent_tool_use_id": None}

    async def _turn(self, sess, text, attachments, images=(), hidden=None, by=None):
        hub = self.hub
        tid = uuid.uuid4().hex[:8]
        started = time.time()
        sess.append({"kind": "user", "text": text, "attachments": attachments, "turn": tid, **({"by": by} if by else {})})
        hub.emit("agent.user", sid=sess.sid, text=text, turn=tid, attachments=attachments, **({"by": by} if by else {}))
        self.steered = False
        hub.emit("agent.status", sid=sess.sid, busy=True, turn=tid, phase="starting")
        starting = True
        t = None
        try:
            if self.app.settings.get("snapshot_each_turn"):
                await self._snapshot(f"Before: {text.strip()[:60]}")
            if self.stop_asked:
                raise _Stopped()
            client = await self.connect()
            starting = False
            if self.stop_asked:                           # stopped while connecting: the message never goes out
                raise _Stopped()
            extra = [f"Attached by the user: {a.get('label') or a}" for a in attachments
                     if not (isinstance(a, dict) and a.get("kind") == "flag")] if attachments else None
            if hidden:
                extra = (extra or []) + [hidden]
            prompt = prompts.turn_context(self.rt, extra) + text
            t = {"tid": tid, "sess": sess, "done": asyncio.get_running_loop().create_future(), "auto": False,
                 "blocks": {}, "started": started}
            self.turn = t
            await client.query(self._with_images(prompt, images) if images else prompt)
            await self._send_steers()
            hub.emit("agent.status", sid=sess.sid, busy=True, turn=tid, phase="thinking")
            await t["done"]                               # the reader resolves it on this turn's result
        except asyncio.CancelledError:
            hub.emit("agent.error", sid=sess.sid, turn=tid, message="stopped")
            raise
        except _Stopped:
            hub.emit("agent.error", sid=sess.sid, turn=tid, message="stopped")
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            self.app.log(traceback.format_exc())
            if "auth" in msg.lower() or "api key" in msg.lower() or "login" in msg.lower():
                msg += " -- sign in with `claude` (Claude Code) or add an Anthropic API key in Settings."
            elif starting:
                msg = f"Claude could not be started ({msg}). Details: {self.app.logfile}"
            sess.append({"kind": "error", "text": msg, "turn": tid})
            hub.emit("agent.error", sid=sess.sid, turn=tid, message=msg)
            await self.disconnect()
        finally:
            if self.turn is t:
                self.turn = None
            sess.meta["updated"] = _now()
            sess.meta["turns"] = sess.meta.get("turns", 0) + 1
            self._save_meta(sess.meta)
            if self.app.settings.get("snapshot_each_turn"):          # after the turn, not holding it open
                asyncio.ensure_future(self._snapshot(f"Claude: {text.strip()[:60]}", push=True))
            if self.turn is None:                          # else a turn follows (a note it had not read yet)
                hub.emit("agent.status", sid=sess.sid, busy=False, turn=tid, seconds=round(time.time() - started, 1))

    async def _read(self, client, sess):
        """Everything the CLI sends, for as long as it runs: the user's turns, the turns it starts by
        itself when background work (a subagent, a background command) finishes, and background task
        updates. Reading only during the user's turns would leave those queued, and every later reply
        would then arrive one turn late."""
        err = None
        try:
            async for m in client.receive_messages():
                try:
                    self._handle(m, sess)
                except Exception:
                    self.app.log(traceback.format_exc())
            err = RuntimeError("Claude stopped (its process ended)")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.app.log(traceback.format_exc())
            err = e
        t = self.turn
        if t and not t["auto"] and not t["done"].done():
            t["done"].set_exception(err)
        elif t and t["auto"]:
            self._end_turn(t, error=str(err))
        if self.client is client:                          # gone: the next message connects again
            self.client, self.client_key = None, None

    async def _snapshot(self, message, push=False):
        """A history checkpoint; one at a time, so a turn's closing checkpoint and the next turn's
        opening one never commit together. push: then send it to GitHub, when the project is linked
        with auto-push on (a failed push is reported, never fatal)."""
        async with self._snap_lock:
            h = await asyncio.to_thread(history.snapshot, self.rt.p.root, message)
        if h:
            self.hub.emit("history.snapshot", hash=h, message=message)
        gh = self.rt.p.cfg.get("github") or {}
        if push and gh.get("auto_push", True) and gh.get("repo"):      # outside the lock: the network is slow
            from . import github
            try:
                st = await asyncio.to_thread(github.push, self.rt.p)
                self.hub.emit("github.status", **st)
            except Exception as e:
                self.hub.emit("github.status", error=str(e), connected=True, repo=gh.get("repo"))

    def _auto_turn(self, sess, reason="background"):
        """A turn the CLI starts by itself: when background work finishes, or to read a note the user
        sent after Claude's last tool call (reason 'steer')."""
        t = {"tid": uuid.uuid4().hex[:8], "sess": sess, "done": None, "auto": True, "blocks": {}, "started": time.time(),
             "tools": {}}
        self.turn = t
        sess.append({"kind": "auto", "turn": t["tid"], "reason": reason})
        self.hub.emit("agent.auto", sid=sess.sid, turn=t["tid"], reason=reason)
        self.hub.emit("agent.status", sid=sess.sid, busy=True, turn=t["tid"],
                      phase="reading your note" if reason == "steer" else "continuing after background work")
        return t

    def _end_turn(self, t, error=None, quiet=False):
        """The end of a turn the CLI started by itself (quiet: another follows at once)."""
        if self.turn is t:
            self.turn = None
        sess = t["sess"]
        if error:
            if error != "stopped":
                sess.append({"kind": "error", "text": error, "turn": t["tid"]})
            self.hub.emit("agent.error", sid=sess.sid, turn=t["tid"], message=error)
        sess.meta["updated"] = _now()
        sess.meta["turns"] = sess.meta.get("turns", 0) + 1
        self._save_meta(sess.meta)
        if not quiet:
            self.hub.emit("agent.status", sid=sess.sid, busy=False, turn=t["tid"], seconds=round(time.time() - t["started"], 1))
        if self.app.settings.get("snapshot_each_turn"):
            asyncio.ensure_future(self._snapshot("Claude: after background work", push=True))

    def _handle(self, m, sess):
        hub = self.hub
        t = self.turn
        starts = (isinstance(m, SystemMessage) and m.subtype == "init") or \
                 (isinstance(m, (StreamEvent, AssistantMessage)) and not m.parent_tool_use_id)
        if t is None and self.stopping:                   # the CLI starting on a queued note after a stop
            return
        if t is None and starts:
            t = self._auto_turn(sess)
        tid = t["tid"] if t else None
        if isinstance(m, StreamEvent):
            ev = m.event or {}
            et = ev.get("type")
            if m.parent_tool_use_id or t is None:
                return
            blocks = t["blocks"]
            if et == "content_block_start":
                cb = ev.get("content_block", {})
                blocks[ev.get("index")] = cb.get("type")
                if cb.get("type") == "text":
                    hub.emit("agent.text_start", sid=sess.sid, turn=tid, index=ev.get("index"))
                elif cb.get("type") == "thinking":
                    hub.emit("agent.status", sid=sess.sid, busy=True, turn=tid, phase="thinking")
                elif cb.get("type") == "tool_use":
                    hub.emit("agent.status", sid=sess.sid, busy=True, turn=tid, phase=f"using {cb.get('name', 'a tool')}")
            elif et == "content_block_delta":
                d = ev.get("delta", {})
                if d.get("type") == "text_delta":
                    hub.emit("agent.text", sid=sess.sid, turn=tid, index=ev.get("index"), delta=d.get("text", ""))
        elif isinstance(m, SystemMessage):
            d = m.data or {}
            if m.subtype == "init":
                sid = d.get("session_id")
                if sid and sess.meta.get("sdk_session") != sid:
                    sess.meta["sdk_session"] = sid
                    self._save_meta(sess.meta)
                sess.meta["model"] = d.get("model")
            elif m.subtype in ("task_started", "task_notification", "task_updated") and not d.get("owned_by_subagent"):
                self._task_event(m.subtype, d, sess)
        elif isinstance(m, AssistantMessage):
            if m.parent_tool_use_id or t is None:
                return
            for b in m.content:
                if isinstance(b, TextBlock) and b.text.strip():
                    t["last_text"] = b.text
                    sess.append({"kind": "assistant", "text": b.text, "turn": tid})
                    hub.emit("agent.text_done", sid=sess.sid, turn=tid, text=b.text)
                elif isinstance(b, ToolUseBlock):
                    rec = {"kind": "tool", "id": b.id, "name": b.name, "input": _short(b.input), "turn": tid}
                    sess.append(rec)
                    hub.emit("agent.tool", sid=sess.sid, **{k: v for k, v in rec.items() if k != "kind"})
        elif isinstance(m, UserMessage):
            if m.parent_tool_use_id:
                return
            said = m.content if isinstance(m.content, str) else \
                "".join(b.text for b in m.content if isinstance(b, TextBlock))
            if said and self.steers:                      # the CLI's echo of a message it just took in
                self._steer_read(said, sess, tid)
            if not isinstance(m.content, list):
                return
            for b in m.content:
                if isinstance(b, ToolResultBlock):
                    text, image = _result_text(b.content)
                    media = self._save_media(image) if image else None
                    rec = {"kind": "tool_result", "id": b.tool_use_id, "text": text[:4000], "is_error": bool(b.is_error),
                           "media": media, "turn": tid}
                    sess.append(rec)
                    hub.emit("agent.tool_result", sid=sess.sid, **{k: v for k, v in rec.items() if k != "kind"})
                    for task, info in list(self.tasks.items()):   # returned while its task runs on: backgrounded
                        if info["tool"] == b.tool_use_id and not info["shown"]:
                            self._task_line(task, "running", sess)
        elif isinstance(m, ResultMessage):
            cost = m.total_cost_usd or 0.0
            sess.meta["cost"] = round(sess.meta.get("cost", 0.0) + cost, 4)
            if m.session_id:
                sess.meta["sdk_session"] = m.session_id
            if t is None:                                  # e.g. the tail of an interrupted turn
                self._save_meta(sess.meta)
                return
            rec = {"kind": "done", "turn": tid, "cost": cost, "duration_ms": m.duration_ms, "turns": m.num_turns,
                   "is_error": m.is_error, "subtype": m.subtype}
            sess.append(rec)
            hub.emit("agent.done", sid=sess.sid, **{k: v for k, v in rec.items() if k != "kind"}, session_cost=sess.meta["cost"])
            follow = not self.stopping and any(st["sent"] for st in self.steers)   # an unread note: the CLI runs it next
            if not follow and not self.stopping:
                self._limit_hit(sess, (getattr(m, "result", None) or "") + "\n" + (t.get("last_text") or "") if m.is_error else "")
            if t["auto"]:
                self._end_turn(t, quiet=follow)
            else:
                self.turn = None                           # what follows belongs to a new turn
                if not t["done"].done():
                    t["done"].set_result(m)
            if follow:
                self._auto_turn(sess, reason="steer")

    def _task_event(self, kind, d, sess):
        """Commands and subagents the CLI runs as tasks. Every slow foreground command is one too, and
        its step already shows it, so only work that goes on in the background gets a line."""
        task = d.get("task_id")
        if not task:
            return
        patch = d.get("patch") or {}
        if kind == "task_started":
            if task not in self.tasks:
                self.tasks[task] = {"desc": d.get("description") or "background task", "tool": d.get("tool_use_id"),
                                    "shown": False}
                if d.get("is_backgrounded") or not d.get("tool_use_id"):
                    self._task_line(task, "running", sess)
            return
        info = self.tasks.get(task)
        if info is None:
            return
        if (d.get("is_backgrounded") or patch.get("is_backgrounded")) and not info["shown"]:
            self._task_line(task, "running", sess)
        status = d.get("status") or patch.get("status")
        if status in ("completed", "failed", "stopped", "killed"):
            self.tasks.pop(task, None)
            if info["shown"]:
                self._task_line(task, status, sess, info)

    def _task_line(self, task, status, sess, info=None):
        info = info or self.tasks[task]
        info["shown"] = True
        rec = {"kind": "task", "id": task, "description": info["desc"], "status": status}
        sess.append(rec)
        self.hub.emit("agent.task", sid=sess.sid, **{k: v for k, v in rec.items() if k != "kind"})

    def _save_media(self, image):
        d = self.rt.p.state_dir("sessions", "media")
        name = uuid.uuid4().hex[:12] + ".png"
        with open(os.path.join(d, name), "wb") as f:
            f.write(base64.b64decode(image))
        return name

    async def interrupt(self):
        self.cancel_wait()
        if self.busy and not self.turn:
            # Still starting (the checkpoint before the message, or connecting to Claude): the turn ends
            # there, before its message reaches Claude. Cancelling it mid-connect could strand the CLI.
            self.stop_asked = True
        elif self.client and self.busy:
            self.stopping = True
            try:
                await self.client.interrupt()
            except Exception:
                pass
            await asyncio.sleep(0.5)
            if self.task is not None and not self.task.done():
                self.task.cancel()
            t = self.turn
            if t and t["auto"]:
                self._end_turn(t, error="stopped")
            if self.steered:        # the CLI runs a queued note as a new turn even after a stop: end its session
                await self.disconnect()                   # (the next message resumes the conversation)
                self.steered = False
            self._drop_steers()
            self.stopping = False
        for rid, (_, info) in list(self.pending.items()):
            if info.get("kind") == "question":
                self.answer_question(rid, {})
            else:
                self.answer_permission(rid, False)


class _Stopped(Exception):
    """A turn stopped before its message reached Claude."""


def _strip_heredocs(cmd):
    """The command lines of a shell command, without heredoc bodies (they are data, not commands)."""
    lines, out, i = cmd.split("\n"), [], 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        for m in re.finditer(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1", line):
            while i < len(lines) and lines[i].strip() != m.group(2):
                i += 1
            i += 1                                          # the closing delimiter
    return "\n".join(out)


def _short(obj, limit=6000):
    s = json.dumps(obj, default=str)
    if len(s) <= limit:
        return obj
    return {"_truncated": s[:limit] + "..."}


def _result_text(content):
    if isinstance(content, str):
        return content, None
    texts, image = [], None
    for c in content or []:
        if isinstance(c, dict):
            if c.get("type") == "text":
                texts.append(c.get("text", ""))
            elif c.get("type") == "image":
                src = c.get("source") or {}
                image = c.get("data") or src.get("data")
    return "\n".join(texts), image
