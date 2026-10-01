"""The board editor worker, from the app's side: one long-running KiCad Python process (kpy_worker.py) that keeps the
board loaded and applies op lists to it, so an edit takes milliseconds instead of starting KiCad's Python for each.
Started on first use, started again if it dies; a request that fails or times out kills it, and the caller falls
back to the one-shot kpy_ops.py.

    from tw.pcb import worker
    res = worker.apply("x.kicad_pcb", ops, save=True)        # same result as kpy_ops.py: {ok, saved, results, changes}
"""
import atexit, json, os, queue, subprocess, tempfile, threading
from .. import env, kicad

_lock = threading.Lock()
_one = None


class Worker:
    def __init__(self):
        self.proc = None
        self.lines = None
        self.n = 0
        self.io = threading.Lock()
        self.log = os.path.join(tempfile.gettempdir(), "tracewright-board-worker.log")

    def _start(self):
        py = env.require("python")
        e = dict(os.environ)
        e["PYTHONPATH"] = kicad.TOOLS_DIR + os.pathsep + e.get("PYTHONPATH", "")
        e.setdefault("PYTHONDONTWRITEBYTECODE", "1")
        self.proc = subprocess.Popen([py, os.path.join(kicad.TW_DIR, "pcb", "kpy_worker.py")], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=open(self.log, "a"), text=True, bufsize=1, env=e,
                                     cwd=os.path.expanduser("~"))
        self.lines = queue.Queue()
        proc, q = self.proc, self.lines

        def read():
            for ln in proc.stdout:
                if ln.startswith("TWJSON "):
                    try:
                        q.put(json.loads(ln[7:]))
                    except ValueError:
                        pass
            q.put(None)                                   # the worker went away
        threading.Thread(target=read, daemon=True, name="board-worker").start()

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def call(self, req, timeout=180):
        with self.io:
            if not self.alive():
                self._start()
            self.n += 1
            rid = self.n
            try:
                self.proc.stdin.write(json.dumps({**req, "id": rid}) + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self.stop()
                raise RuntimeError("the board worker stopped")
            while True:
                try:
                    out = self.lines.get(timeout=timeout)
                except queue.Empty:
                    self.stop()
                    raise TimeoutError("the board worker did not answer")
                if out is None:
                    self.stop()
                    raise RuntimeError("the board worker stopped")
                if out.get("id") == rid:
                    return out

    def stop(self):
        p, self.proc = self.proc, None
        if p is not None and p.poll() is None:
            try:
                p.stdin.write(json.dumps({"cmd": "close"}) + "\n")
                p.stdin.flush()
                p.wait(timeout=3)
            except Exception:
                p.kill()


def get():
    global _one
    with _lock:
        if _one is None:
            _one = Worker()
            atexit.register(_one.stop)
        return _one


def apply(board, ops, save=True, fill_after=False, stop_on_error=True, timeout=180):
    """Ops on the board file through the worker: {ok, saved, results, changes}. Raises when the worker cannot
    answer (the caller then runs kpy_ops.py once instead)."""
    out = get().call({"cmd": "apply", "board": os.path.abspath(board), "ops": ops, "save": save, "fill_after": fill_after,
                      "stop_on_error": stop_on_error}, timeout=timeout)
    if "error" in out and "results" not in out:
        raise RuntimeError(out["error"])
    out.pop("id", None)
    return out


def enabled():
    """The worker is used unless TW_BOARD_WORKER=0."""
    return os.environ.get("TW_BOARD_WORKER", "1") != "0"
