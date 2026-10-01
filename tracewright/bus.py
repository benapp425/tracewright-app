"""Per-project event hub: everything the UI shows live goes through here (agent output, board and
schematic changes, router progress, check progress, KiCad link state, highlights)."""
import asyncio, json, time, collections, threading


class Hub:
    def __init__(self, pid, loop=None):
        self.pid = pid
        self.loop = loop or asyncio.get_event_loop()
        self.clients = set()
        self.recent = collections.deque(maxlen=400)     # replayed to a client that connects mid-turn
        self.seq = 0
        self._lock = threading.Lock()
        self.listeners = []                               # server-side taps (the timelapse), called with each event

    def emit(self, type_, **data):
        """Thread-safe: may be called from worker threads (router, checks, KiCad link)."""
        with self._lock:
            self.seq += 1
            ev = {"type": type_, "seq": self.seq, "t": time.time(), **data}
        self.recent.append(ev)
        for fn in list(self.listeners):
            try:
                fn(ev)
            except Exception:
                pass
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            self._fanout(ev)
        else:
            try:
                self.loop.call_soon_threadsafe(self._fanout, ev)
            except RuntimeError:                          # the loop has closed (shutting down): nobody left to tell
                pass
        return ev

    def _fanout(self, ev):
        msg = json.dumps(ev, default=str)
        dead = []
        for ws in list(self.clients):
            if ws.closed:
                dead.append(ws)
                continue
            asyncio.ensure_future(self._send(ws, msg))
        for ws in dead:
            self.clients.discard(ws)

    async def _send(self, ws, msg):
        try:
            await ws.send_str(msg)
        except Exception:
            self.clients.discard(ws)

    def since(self, seq):
        return [e for e in self.recent if e["seq"] > seq]


class Hubs:
    def __init__(self):
        self._hubs = {}
        self.loop = None

    def get(self, pid):
        if pid not in self._hubs:
            self._hubs[pid] = Hub(pid, self.loop)
        return self._hubs[pid]

    def all(self):
        return list(self._hubs.values())
