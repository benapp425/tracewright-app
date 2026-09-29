#!/usr/bin/env python3
"""Browser tests: the real UI in headless Chrome against a throwaway Tracewright -- a fresh data folder
holding only the demo project, no accounts, no update check, no Claude. Each test drives the app the way a
person would and checks what the page shows; a screenshot of each is saved for a look.

  python tests/ui_tests.py [-k name] [--out DIR] [--headful]

Needs Chrome, Chromium or Edge (TW_CHROME=path to choose). Exits 0 when every test passes."""
import argparse, asyncio, json, os, shutil, subprocess, sys, tempfile, time, traceback, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from cdp import Chrome, find_chrome, free_port  # noqa: E402

SERVER = """
import sys; sys.path.insert(0, {root!r})
from aiohttp import web
from tracewright.server import make_app
app = make_app(); app["app"].port = {port}
web.run_app(app, host="127.0.0.1", port={port}, print=None, access_log=None)
"""


class Server:
    """Tracewright from this source tree on a free port, with its own data and projects folders."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="tw-ui-")
        self.home, self.ws = os.path.join(self.tmp, "home"), os.path.join(self.tmp, "projects")
        os.makedirs(self.home), os.makedirs(self.ws)
        with open(os.path.join(self.home, "settings.json"), "w") as f:
            json.dump({"update_check": False, "open_browser": False, "workspace": self.ws}, f)
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}/"
        env = dict(os.environ, TRACEWRIGHT_HOME=self.home, TW_WORKSPACE=self.ws, TW_ACCOUNTS="0", PYTHONUNBUFFERED="1")
        env.pop("TW_LOCAL_KEY", None)
        self.log = open(os.path.join(self.tmp, "server.log"), "w")
        self.proc = subprocess.Popen([sys.executable, "-c", SERVER.format(root=ROOT, port=self.port)], env=env,
                                     stdout=self.log, stderr=subprocess.STDOUT)
        t0 = time.time()
        while True:
            try:
                urllib.request.urlopen(self.url + "api/health", timeout=2).read()
                break
            except OSError:
                if time.time() - t0 > 30 or self.proc.poll() is not None:
                    raise RuntimeError("the server did not start:\n" + open(self.log.name).read()[-3000:])
                time.sleep(0.2)
        self.demo = self.post("api/projects/demo")

    def post(self, path, body=None):
        req = urllib.request.Request(self.url + path, data=json.dumps(body or {}).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Origin": self.url.rstrip("/")})
        return json.loads(urllib.request.urlopen(req, timeout=120).read())

    def get(self, path):
        return json.loads(urllib.request.urlopen(self.url + path, timeout=60).read())

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


class Ctx:
    def __init__(self, server, page, out):
        self.s, self.page, self.out = server, page, out
        self.pid = server.demo["id"]

    async def open_project(self, view=None):
        await self.page.goto(self.s.url + f"#/p/{self.pid}" + (f"/{view}" if view else ""))
        await self.page.wait("document.querySelector('.tabs .tab[data-place]')", 30)
        if view:
            await self.page.wait(f"document.querySelector('.view.on[data-view={json.dumps(view)}], .view.on')", 20)

    async def place(self, place, view=None):
        await self.page.click(f".tab[data-place={json.dumps(place)}]")
        if view:
            await self.page.wait(f"document.querySelector('.subnav button[data-view={json.dumps(view)}]')", 10)
            await self.page.click(f".subnav button[data-view={json.dumps(view)}]")
        await asyncio.sleep(0.4)

    async def shot(self, name):
        return await self.page.shot(os.path.join(self.out, name + ".png"))


def check(ok, what):
    if not ok:
        raise AssertionError(what)


# ------------------------------------------------------------------ the tests
@test
async def home_lists_the_demo(t):
    await t.page.goto(t.s.url)
    names = await t.page.wait("[...document.querySelectorAll('.pcard')].map((c) => c.textContent).join('|') || null", 30)
    check("Demo" in names, f"no demo card on the home screen: {names!r}")
    await t.shot("home")


@test
async def home_draws_at_once_while_the_list_loads(t):
    # no saved list: placeholder cards while the server's list is on its way, then the projects
    await t.page.js("localStorage.removeItem('tw.projects'); 1")
    await t.page.intercept("*/api/projects")
    await t.page.call("Page.navigate", url=t.s.url)
    req = await t.page.held_request()
    await t.page.wait("document.querySelector('.pcard.skel') && document.querySelector('.page-head h1')", 10)
    await t.shot("home-loading")
    await t.page.resume(req)
    await t.page.wait("[...document.querySelectorAll('.pcard')].some((c) => c.textContent.includes('Demo'))", 10)
    check(not await t.page.js("!!document.querySelector('.pcard.skel')"), "placeholders left after the list arrived")
    # the next visit shows the last list before the server answers
    await t.page.call("Page.navigate", url=t.s.url)
    req = await t.page.held_request()
    await t.page.wait("[...document.querySelectorAll('.pcard')].some((c) => c.textContent.includes('Demo'))", 10)
    check(not await t.page.js("!!document.querySelector('.pcard.skel')"), "placeholders although the last list was saved")
    await t.page.resume(req)
    await t.page.stop_intercept()


@test
async def home_says_when_projects_are_in_icloud_only(t):
    real = t.s.get("api/projects")
    await t.page.intercept("*/api/projects")
    await t.page.call("Page.navigate", url=t.s.url)
    await t.page.fulfill(await t.page.held_request(), json.dumps([dict(real[0], cloud_only=3)]))
    txt = await t.page.wait("document.querySelector('.cloudnote') && document.querySelector('.cloudnote').innerText", 10)
    check("iCloud only" in txt and "Keep Downloaded" in txt, f"note: {txt!r}")
    check(await t.page.js("!!document.querySelector('.pcard .badge.cloud')"), "no iCloud badge on the card")
    await t.shot("home-icloud")
    await t.page.click(".cloudnote button.ghost")                 # hide it: gone, and stays gone on the next visit
    check(not await t.page.js("!!document.querySelector('.cloudnote')"), "the note did not hide")
    await t.page.call("Page.navigate", url=t.s.url)
    await t.page.fulfill(await t.page.held_request(), json.dumps([dict(real[0], cloud_only=3)]))
    await t.page.wait("document.querySelector('.pcard .badge.cloud')", 10)
    check(not await t.page.js("!!document.querySelector('.cloudnote')"), "the hidden note came back")
    await t.page.stop_intercept()
    await t.page.js("localStorage.removeItem('tw.projects'); localStorage.removeItem('tw.cloudNote.hidden'); 1")


@test
async def five_places_and_the_address_follows(t):
    await t.open_project()
    places = await t.page.js("[...document.querySelectorAll('.tabs .tab[data-place]')].map((e) => e.dataset.place)")
    check(places == ["overview", "design", "parts", "checks", "project"], f"places: {places}")
    for place, views in (("design", ["board", "schematic", "3d"]), ("parts", ["bom", "outputs"]),
                         ("checks", ["checks", "rules"]), ("project", ["docs", "files", "history"])):
        await t.place(place)
        sub = await t.page.js("[...document.querySelectorAll('.subnav button')].map((b) => b.dataset.view)")
        check(sub == views, f"{place}: sub-views {sub}, wanted {views}")
        for v in views:
            await t.page.click(f".subnav button[data-view={json.dumps(v)}]")
            await t.page.wait(f"location.hash.endsWith('/{v}')", 5)
    await t.place("overview")
    await t.page.wait("location.hash.endsWith('/overview')", 5)


@test
async def no_ask_claude_buttons_anywhere(t):
    await t.open_project()
    seen = []
    for place, views in (("overview", [None]), ("design", ["board", "schematic", "3d"]), ("parts", ["bom", "outputs"]),
                         ("checks", ["checks", "rules"]), ("project", ["docs", "files", "history"])):
        for v in views:
            await t.place(place, v)
            await asyncio.sleep(0.6)
            txt = await t.page.js("document.querySelector('.main, main, #app, body').innerText")
            if "Ask Claude" in txt:
                seen.append(v or place)
    check(not seen, f"'Ask Claude' still shown in: {seen}")


@test
async def board_names_nets_when_zoomed(t):
    await t.open_project("board")
    await t.page.wait("document.querySelector('.viewer canvas') && document.querySelector('.viewer canvas').__view && document.querySelector('.viewer canvas').__view.data", 30)
    # record the text the board draws, then zoom in on the USB connector's pads and the regulator
    await t.page.js("""(() => { const v = document.querySelector('.viewer canvas').__view; window.__texts = [];
        const c = v.ctx, orig = c.fillText.bind(c); c.fillText = (s, x, y) => { window.__texts.push(String(s)); return orig(s, x, y); };
        return true; })()""")
    fit = await t.page.js("""(() => { const v = document.querySelector('.viewer canvas').__view; window.__texts = []; v.draw();
        return window.__texts.length; })()""")
    zoomed = await t.page.js("""(() => { const v = document.querySelector('.viewer canvas').__view;
        const f = v.fps.find((f) => /^J/.test(f.ref) && f.pads.length >= 6) || v.fps[0];
        const b = f.bbox, cx = (b[0] + b[2]) / 2, cy = (b[1] + b[3]) / 2;
        v.scale = 70; v.ox = v.w / 2 - cx * v.scale; v.oy = v.h / 2 - cy * v.scale;
        window.__texts = []; v.draw(); return {ref: f.ref, texts: window.__texts}; })()""")
    names = set(zoomed["texts"])
    nets = await t.page.js("""(() => { const v = document.querySelector('.viewer canvas').__view;
        return [...new Set(v.fps.flatMap((f) => f.pads.map((p) => p.net)).filter(Boolean).map((n) => n.split('/').pop()))]; })()""")
    shown = sorted(n for n in names if n in nets)
    check(len(shown) >= 2, f"zoomed on {zoomed['ref']}: net names drawn {shown} (all text: {sorted(names)[:30]})")
    await t.shot("board-net-names")
    # the Layers panel switch turns them off
    off = await t.page.js("""(() => { const v = document.querySelector('.viewer canvas').__view; v.vis.netnames = false;
        window.__texts = []; v.draw(); const n = window.__texts.filter((s) => %s.includes(s)).length; v.vis.netnames = true; v.draw(); return n; })()"""
                          % json.dumps(shown))
    check(off < len([s for s in zoomed["texts"] if s in shown]), "turning net names off drew as many names")
    check(fit < len(zoomed["texts"]), f"as many texts at fit ({fit}) as zoomed in ({len(zoomed['texts'])})")


# ------------------------------------------------------------------ running
async def run(args):
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    chosen = [f for f in TESTS if not args.k or any(k in f.__name__ for k in args.k.split(","))]
    print(f"{len(chosen)} browser tests, Chrome: {find_chrome()}")
    server = Server()
    version = server.get("api/info")["version"]           # "what's new" already seen
    failed = []
    try:
        async with Chrome() as browser:
            for fn in chosen:
                page = await browser.page()
                await page.goto(server.url)
                await page.js("localStorage.setItem('tw.toured.local', '1'); localStorage.setItem('tw.theme', 'dark'); "
                              f"localStorage.setItem('tw.seenVersion', {json.dumps(version)}); localStorage.setItem('tw.updateSeen', '99'); 1")
                t0 = time.time()
                try:
                    await fn(Ctx(server, page, out))
                    errs = [e for e in page.errors if "favicon" not in e]
                    if errs:
                        raise AssertionError("console errors: " + " | ".join(errs[:5]))
                    print(f"  ok    {fn.__name__}  ({time.time() - t0:.1f}s)")
                except Exception as e:
                    failed.append(fn.__name__)
                    print(f"  FAIL  {fn.__name__}: {e}")
                    if not isinstance(e, AssertionError):
                        traceback.print_exc()
                    try:
                        await page.shot(os.path.join(out, fn.__name__ + "-FAILED.png"))
                    except Exception:
                        pass
                await page.close()
    finally:
        server.stop()
    print(f"{len(chosen) - len(failed)} passed, {len(failed)} failed" + (f": {', '.join(failed)}" if failed else "") + f"  (screenshots: {out})")
    return 1 if failed else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", help="only tests whose name contains one of these (comma separated)")
    ap.add_argument("--out", default=os.path.join(tempfile.gettempdir(), "tracewright-ui"))
    a = ap.parse_args()
    if not find_chrome():
        print("no Chrome found: set TW_CHROME to run the browser tests")
        sys.exit(0)
    sys.exit(asyncio.run(run(a)))
