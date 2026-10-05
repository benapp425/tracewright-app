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
            json.dump({"update_check": False, "open_browser": False, "workspace": self.ws, "stock_watch": False}, f)
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}/"
        env = dict(os.environ, TRACEWRIGHT_HOME=self.home, TRACEWRIGHT_CACHE=os.path.join(self.home, "cache"), TW_WORKSPACE=self.ws, TW_ACCOUNTS="0", PYTHONUNBUFFERED="1")
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
                         ("checks", ["checks", "signoff", "rules"]), ("project", ["docs", "files", "history"])):
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
                         ("checks", ["checks", "signoff", "rules"]), ("project", ["docs", "files", "history"])):
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


@test
async def attachments_paste_drop_and_take_back(t):
    await t.open_project("board")
    await t.page.wait("document.querySelector('.composer textarea')", 10)
    # a screenshot pasted into the message box
    await t.page.js("""(async () => {
        const c = document.createElement('canvas'); c.width = 120; c.height = 80;
        const g = c.getContext('2d'); g.fillStyle = '#c33'; g.fillRect(0, 0, 120, 80);
        const blob = await new Promise((r) => c.toBlob(r, 'image/png'));
        const dt = new DataTransfer(); dt.items.add(new File([blob], 'image.png', { type: 'image/png' }));
        const ta = document.querySelector('.composer textarea'); ta.focus();
        ta.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }));
        return true; })()""")
    await t.page.wait("[...document.querySelectorAll('.attachrow .attach')].some((a) => /pasted-.*\\.png/.test(a.textContent) && !a.classList.contains('busy'))", 15)
    # a data sheet dropped on the window
    await t.page.js("""(() => {
        const dt = new DataTransfer(); dt.items.add(new File(['%PDF-1.4 test'], 'LM7805.pdf', { type: 'application/pdf' }));
        for (const type of ['dragenter', 'dragover', 'drop']) window.dispatchEvent(new DragEvent(type, { dataTransfer: dt, bubbles: true, cancelable: true }));
        return true; })()""")
    await t.page.wait("[...document.querySelectorAll('.attachrow .attach')].some((a) => a.textContent.includes('LM7805.pdf') && !a.classList.contains('busy'))", 15)
    check(not await t.page.js("!!document.querySelector('.dropzone')"), "the drop overlay stayed up")
    pics = t.s.get(f"api/projects/{t.pid}/files?path=uploads/images")["entries"]
    docs = t.s.get(f"api/projects/{t.pid}/files?path=docs/datasheets")["entries"]
    check(any(e["name"].startswith("pasted-") for e in pics), f"pasted picture not in uploads/images: {pics}")
    check(any(e["name"] == "LM7805.pdf" for e in docs), f"PDF not in docs/datasheets: {docs}")
    check(await t.page.js("!document.querySelector('.sendbtn').classList.contains('idle')"), "Send stays grey with files attached")
    await t.shot("attachments")
    # taken back: the chip goes and so does the file
    await t.page.js("[...document.querySelectorAll('.attachrow .attach')].find((a) => a.textContent.includes('LM7805.pdf')).querySelector('.ax').click()")
    await t.page.wait("![...document.querySelectorAll('.attachrow .attach')].some((a) => a.textContent.includes('LM7805.pdf'))", 5)
    await asyncio.sleep(0.4)
    docs = t.s.get(f"api/projects/{t.pid}/files?path=docs/datasheets")["entries"]
    check(not any(e["name"] == "LM7805.pdf" for e in docs), f"the taken-back PDF is still there: {docs}")


DEMO_FLOORPLAN = {
    "board": {"w": 50, "h": 35, "radius": 1},
    "holes": [{"id": "H1", "ref": "H1", "x": 3.5, "y": 3.5, "d": 3.2}, {"id": "H2", "ref": "H2", "x": 46.5, "y": 3.5, "d": 3.2},
              {"id": "H3", "ref": "H3", "x": 3.5, "y": 31.5, "d": 3.2}, {"id": "H4", "ref": "H4", "x": 46.5, "y": 31.5, "d": 3.2}],
    "items": [{"id": "usb", "ref": "J1", "label": "USB-C", "kind": "connector", "edge": "left", "at": 17.5, "w": 9.6, "h": 6.3},
              {"id": "qwiic", "ref": "J2", "label": "Qwiic", "kind": "connector", "edge": "right", "at": 17.5, "w": 6.8, "h": 5.6},
              {"id": "mcu", "label": "MCU", "kind": "mcu", "x": 30, "y": 16, "w": 12, "h": 10, "note": "ATtiny85"},
              {"id": "power", "label": "Power", "kind": "power", "x": 16, "y": 26, "w": 13, "h": 9, "note": "AMS1117 3.3 V"}],
    "keepouts": [{"label": "logo", "x": 40, "y": 29, "w": 8, "h": 5}], "note": ""}


async def fp_drag(page, target, to_mm):
    """Drag a floorplan item (data-id, or the board's corner) to a spot given in board millimetres, once the
    drawing has stopped moving (it redraws when its column settles)."""
    last = None
    for _ in range(30):
        pts = await page.js("""((id, tx, ty) => { const v = document.querySelector('.fp').__fp, r = v.svg.getBoundingClientRect(), vb = v.svg.viewBox.baseVal;
        const g = id === 'corner' ? v.svg.querySelector('.fp-corner') : v.svg.querySelector(`[data-id="${id}"] rect, [data-id="${id}"] circle:not(.ring)`);
        const b = g.getBoundingClientRect();
        return { from: [b.x + b.width / 2, b.y + b.height / 2],
                 to: [r.left + (v.ox + tx * v.S) * r.width / vb.width, r.top + (v.oy + ty * v.S) * r.height / vb.height] }; })(%s, %s, %s)"""
                        % (json.dumps(target), to_mm[0], to_mm[1]))
        if pts == last:
            break
        last = pts
        await asyncio.sleep(0.15)
    (x0, y0), (x1, y1) = pts["from"], pts["to"]
    await page.call("Input.dispatchMouseEvent", type="mouseMoved", x=x0, y=y0)
    await page.call("Input.dispatchMouseEvent", type="mousePressed", x=x0, y=y0, button="left", clickCount=1)
    for i in range(1, 9):
        await page.call("Input.dispatchMouseEvent", type="mouseMoved", x=x0 + (x1 - x0) * i / 8, y=y0 + (y1 - y0) * i / 8, button="left", buttons=1)
    await page.call("Input.dispatchMouseEvent", type="mouseReleased", x=x1, y=y1, button="left", clickCount=1)
    await asyncio.sleep(0.4)


@test
async def floorplan_drag_to_place(t):
    root = t.s.demo["root"]
    cfgp = os.path.join(root, "tracewright.json")
    before = open(cfgp).read()
    cfg = json.loads(before)
    cfg["start"] = {"mode": "guided", "phase": "ready"}
    with open(cfgp, "w") as f:
        json.dump(cfg, f, indent=1)
    os.makedirs(os.path.join(root, ".tracewright"), exist_ok=True)
    with open(os.path.join(root, ".tracewright", "canvas.json"), "w") as f:
        json.dump({"requirements": {"items": [{"label": "Board", "value": "50 × 35 mm, 2 layers"}]}, "floorplan": DEMO_FLOORPLAN,
                   "plan": {"summary": "The demo board.", "steps": ["Schematic", "Board"]}}, f)
    try:
        await t.page.goto(t.s.url + f"#/p/{t.pid}")
        await t.page.wait("document.querySelector('.fp-svg .fp-board')", 20)
        n = await t.page.js("({conn: document.querySelectorAll('.fp-item.conn').length, blk: document.querySelectorAll('.fp-item.blk').length, "
                            "holes: document.querySelectorAll('.fp-hole').length, dims: [...document.querySelectorAll('.fp-dim')].map((e) => e.textContent)})")
        check(n["conn"] == 2 and n["blk"] == 2 and n["holes"] == 4 and n["dims"] == ["50 mm", "35 mm"], n)
        await t.shot("floorplan")
        cv = lambda: t.s.get(f"api/projects/{t.pid}/canvas")["floorplan"]
        # the Qwiic connector from the right edge to the top edge, 30 mm along it
        await fp_drag(t.page, "qwiic", (30, 1))
        q = next(i for i in cv()["items"] if i["id"] == "qwiic")
        check(q["edge"] == "top" and abs(q["at"] - 30) <= 0.5 and q["moved"], q)
        # the MCU 5 mm to the right
        await fp_drag(t.page, "mcu", (35, 16))
        m = next(i for i in cv()["items"] if i["id"] == "mcu")
        check(abs(m["x"] - 35) <= 0.5 and abs(m["y"] - 16) <= 0.5 and m["moved"], m)
        # the board's corner out to 60 x 40
        await fp_drag(t.page, "corner", (60, 40))
        got = cv()
        b = got["board"]
        check(abs(b["w"] - 60) <= 0.5 and abs(b["h"] - 40) <= 0.5, b)
        h4 = next(o for o in got["holes"] if o["id"] == "H4")                        # the corner hole kept to its corner
        check(abs(h4["x"] - (b["w"] - 3.5)) <= 0.01 and abs(h4["y"] - (b["h"] - 3.5)) <= 0.01, h4)
        check("placed by you" in await t.page.js("document.querySelector('.gd-fpnote').innerText"), "the card does not say what the user placed")
        await t.shot("floorplan-moved")
    finally:
        with open(cfgp, "w") as f:
            f.write(before)
        os.remove(os.path.join(root, ".tracewright", "canvas.json"))


async def fp_click(page, target):
    """Click a floorplan item (select it) without moving it."""
    b = await page.js("""((id) => { const g = document.querySelector(`.fp-svg [data-id="${id}"] rect, .fp-svg [data-id="${id}"] circle:not(.ring)`);
        const r = g.getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2]; })(%s)""" % json.dumps(target))
    await page.mouse(b[0], b[1])
    await asyncio.sleep(0.25)


@test
async def floorplan_turn_lock_nudge_and_keepouts(t):
    """Select by clicking: R turns a block a quarter (a connector to the next edge), L locks, the arrows nudge;
    keep-outs drag like blocks; and the page keeps its place when a drop redraws the cards."""
    root = t.s.demo["root"]
    cfgp = os.path.join(root, "tracewright.json")
    before = open(cfgp).read()
    cfg = json.loads(before)
    cfg["start"] = {"mode": "guided", "phase": "ready"}
    with open(cfgp, "w") as f:
        json.dump(cfg, f, indent=1)
    os.makedirs(os.path.join(root, ".tracewright"), exist_ok=True)
    reqs = [{"label": f"Requirement {i}", "value": "a long line of text to make the page scroll " * 2} for i in range(30)]
    with open(os.path.join(root, ".tracewright", "canvas.json"), "w") as f:
        json.dump({"requirements": {"items": reqs}, "floorplan": DEMO_FLOORPLAN}, f)
    cv = lambda: t.s.get(f"api/projects/{t.pid}/canvas")["floorplan"]
    item = lambda iid: next(i for i in cv()["items"] + cv()["holes"] + cv()["keepouts"] if i.get("id") == iid)
    try:
        await t.page.goto(t.s.url + f"#/p/{t.pid}")
        await t.page.wait("document.querySelector('.fp-svg .fp-board')", 20)
        # the page settled, then scrolled down to the floorplan at the bottom
        await asyncio.sleep(1.0)
        top = await t.page.js("(() => { const sc = document.querySelector('.gdpane'); sc.scrollTop = sc.scrollHeight; return sc.scrollTop; })()")
        check(top > 200, f"the setup page did not scroll ({top})")
        await asyncio.sleep(0.3)
        top = await t.page.js("document.querySelector('.gdpane').scrollTop")
        await fp_click(t.page, "mcu")
        check(await t.page.js("!!document.querySelector('.fp-item.sel[data-id=mcu]') && document.querySelector('.fp-bar').style.display !== 'none'"),
              "a click does not select the block or show its bar")
        await t.page.key("r")
        await asyncio.sleep(0.6)
        m = item("mcu")
        check(m.get("rot") == 90 and m["moved"], m)
        rect = await t.page.js("(() => { const r = document.querySelector('.fp-item[data-id=mcu] rect'); return [+r.getAttribute('width'), +r.getAttribute('height')]; })()")
        check(rect[1] > rect[0], f"the turned block is not drawn turned: {rect}")
        await t.page.key("l")
        await asyncio.sleep(0.6)
        check(item("mcu").get("locked") is True, item("mcu"))
        check(await t.page.js("!!document.querySelector('.fp-item.locked[data-id=mcu] .fp-lock')"), "no lock mark on the locked block")
        x0 = item("mcu")["x"]
        for _ in range(4):
            await t.page.key("ArrowRight")
        await asyncio.sleep(0.9)
        check(abs(item("mcu")["x"] - (x0 + 2)) <= 0.01, (x0, item("mcu")))
        # the scroll held through every redraw
        now = await t.page.js("document.querySelector('.gdpane').scrollTop")
        check(abs(now - top) < 2, f"the page jumped from {top} to {now}")
        # a connector turns to the next edge
        await fp_click(t.page, "usb")
        await t.page.key("r")
        await asyncio.sleep(0.6)
        check(item("usb")["edge"] == "top", item("usb"))
        # keep-outs drag like blocks
        await fp_drag(t.page, "K1", (36, 27))
        k = item("K1")
        check(abs(k["x"] - 36) <= 0.5 and abs(k["y"] - 27) <= 0.5 and k["moved"], k)
        now = await t.page.js("document.querySelector('.gdpane').scrollTop")
        check(abs(now - top) < 2, f"the page jumped after a drop from {top} to {now}")
        await t.shot("floorplan-turned")
    finally:
        with open(cfgp, "w") as f:
            f.write(before)
        os.remove(os.path.join(root, ".tracewright", "canvas.json"))


@test
async def signoff_reads_like_a_review(t):
    """The sign-off page: the verdict with what is left and its buttons, each requirement with its evidence, the
    waivers as cards with plain titles (not finding keys), the reasoning a click away, citations, Approve on the
    ones waiting for you."""
    root = t.s.demo["root"]
    cfgp = os.path.join(root, "tracewright.json")
    before = open(cfgp).read()
    cfg = json.loads(before)
    cfg.setdefault("checks", {})["waive"] = [
        {"key": "bom:mpnfp:10164227-1004A1RLF", "by": "claude", "severity": "error", "title": "J201 and J202 share a land pattern on purpose",
         "reason": "Deliberate: J201 and J202 are the same Amphenol part with the same land pattern (PDS 10164227 rev C sheet 4); the CM5-J2 "
                   "footprint is numbered 101-200 so its pads match the CM5 pin numbers (docs/10). JLC's J2 numbering is mapped by an offset."},
        {"key": "decoup:far:U303:+3V3", "by": "claude", "severity": "warning",
         "reason": "u-blox SAM-M10Q integration manual UBX-22020019 R02 s4.4 p.61: nothing closer than 10 mm to each edge of the patch antenna; "
                   "its supply section asks for no external decoupling. C309 and C310 sit just outside the 10 mm ring."}]
    cfg["evidence"] = [{"req": "r1", "kind": "calc", "label": "AMS1117 at 120 mA: 0.2 W, 30 °C rise", "ref": "docs/power.md", "status": "ok"}]
    with open(cfgp, "w") as f:
        json.dump(cfg, f, indent=1)
    os.makedirs(os.path.join(root, ".tracewright"), exist_ok=True)
    with open(os.path.join(root, ".tracewright", "canvas.json"), "w") as f:
        json.dump({"requirements": {"items": [{"label": "Power", "value": "5 V from USB-C, 3.3 V for the MCU"}, {"label": "Port", "value": "Qwiic I2C"}]}}, f)
    try:
        await t.open_project()
        await t.place("checks", "signoff")
        await t.page.wait("document.querySelector('.so-verdict') && document.querySelectorAll('.so-card').length === 2", 10)
        got = await t.page.js("""(() => ({
          todo: [...document.querySelectorAll('.so-todo')].map((e) => e.innerText),
          titles: [...document.querySelectorAll('.so-ct')].map((e) => e.innerText),
          groups: [...document.querySelectorAll('.so-wgh')].map((e) => e.innerText),
          reqs: [...document.querySelectorAll('.so-req')].map((e) => e.innerText),
          srcs: [...document.querySelectorAll('.so-src')].map((e) => e.innerText),
          approve: !!document.querySelector('.so-card.proposed button.primary'),
          sign: document.querySelector('.so-signrow .btn.primary') && document.querySelector('.so-signrow .btn.primary').disabled }))()""")
        check(any("Approve or reject 1 waiver" in x for x in got["todo"]) and any("Run the checks" in x for x in got["todo"]), got["todo"])
        check(got["titles"][0] == "J201 and J202 share a land pattern on purpose" and got["titles"][1].startswith("u-blox SAM-M10Q integration manual"), got["titles"])
        check(not any(":" in ti and " " not in ti for ti in got["titles"]), f"a finding key used as a title: {got['titles']}")
        check(got["groups"][0].startswith("WAITING FOR YOUR APPROVAL") or got["groups"][0].lower().startswith("waiting for your approval"), got["groups"])
        check(any("AMS1117 at 120 mA" in x for x in got["reqs"]) and any("No evidence yet" in x for x in got["reqs"]), got["reqs"])
        check(any("PDS 10164227 rev C sheet 4" in x for x in got["srcs"]) and any("UBX-22020019" in x for x in got["srcs"]), got["srcs"])
        check(got["approve"] and got["sign"] is True, got)
        # the reasoning, a click away
        n0 = await t.page.js("document.querySelectorAll('.so-card')[0].innerText.length")
        await t.page.js("document.querySelectorAll('.so-card')[0].querySelector('.so-more').click(); 1")
        await asyncio.sleep(0.2)
        n1 = await t.page.js("document.querySelectorAll('.so-card')[0].innerText.length")
        check(n1 > n0 + 40 and "numbered 101-200" in await t.page.js("document.querySelectorAll('.so-card')[0].innerText"), (n0, n1))
        await t.shot("signoff-review")
        # the packet, one printable page
        html = urllib.request.urlopen(urllib.request.Request(t.s.url + f"api/projects/{t.pid}/signoff/packet",
                                                             headers={"Origin": t.s.url.rstrip("/")})).read().decode()
        check("design review" in html and "J201 and J202 share a land pattern" in html, html[:300])
    finally:
        with open(cfgp, "w") as f:
            f.write(before)
        os.remove(os.path.join(root, ".tracewright", "canvas.json"))


@test
async def signoff_page_scrolls(t):
    """A long sign-off page (many waivers) scrolls inside its view."""
    root = t.s.demo["root"]
    cfgp = os.path.join(root, "tracewright.json")
    before = open(cfgp).read()
    cfg = json.loads(before)
    cfg.setdefault("checks", {})["waive"] = [{"key": f"test:waiver:{i}", "reason": "A reason long enough to take a few lines on the page. " * 3,
                                              "by": "claude", "severity": "warning", "message": f"Finding number {i}"} for i in range(24)]
    with open(cfgp, "w") as f:
        json.dump(cfg, f, indent=1)
    try:
        await t.open_project()
        await t.place("checks", "signoff")
        await t.page.wait("document.querySelector('.so .page-head')", 10)
        got = await t.page.js("(() => { const p = document.querySelector('.so').closest('.panel'); if (!p) return null;"
                              " const before = p.scrollTop; p.scrollTop = 400; return [p.scrollHeight > p.clientHeight + 100, p.scrollTop - before]; })()")
        check(got and got[0] and got[1] > 100, f"the sign-off page does not scroll: {got}")
    finally:
        with open(cfgp, "w") as f:
            f.write(before)


@test
async def floorplan_shows_on_the_board_until_there_is_one(t):
    pr = t.s.post("api/projects", {"name": "Floorplan board", "brief": ""})     # no start: nothing goes to Claude
    check(not pr.get("has_pcb"), f"a new project already has a board: {pr}")
    os.makedirs(os.path.join(pr["root"], ".tracewright"), exist_ok=True)
    with open(os.path.join(pr["root"], ".tracewright", "canvas.json"), "w") as f:
        json.dump({"floorplan": DEMO_FLOORPLAN}, f)
    await t.page.goto(t.s.url + f"#/p/{pr['id']}/board")
    await t.page.wait("document.querySelector('.vb-fp .fp-svg .fp-board')", 20)
    check("Start the layout" in await t.page.js("document.querySelector('.vb-fp').innerText"), "no Start the layout button")
    await t.shot("board-floorplan")
    await fp_drag(t.page, "power", (20, 24))
    pw = next(i for i in t.s.get(f"api/projects/{pr['id']}/canvas")["floorplan"]["items"] if i["id"] == "power")
    check(abs(pw["x"] - 20) <= 0.5 and abs(pw["y"] - 24) <= 0.5 and pw["moved"], pw)


@test
async def turn_changes_card(t):
    await t.open_project("board")
    await t.page.wait("[...document.querySelectorAll('*')].some((e) => e.__chat)", 10)
    card = {"turn": "T1", "base": "a1", "head": "b2", "files": [["M", "hardware/demo/demo.kicad_pcb"]],
            "board": {"added": ["C12"], "removed": [], "moved": ["J1", "U1"], "tracks": 48, "vias": 6, "routed": ["SDA", "SCL", "VBUS"], "unrouted": [], "outline": False},
            "schematic": {"added": ["C12"], "removed": [], "values": [["R8", "4.7k", "10k"]]}, "docs": ["docs/decisions.md"], "other": []}
    await t.page.js("(() => { const c = [...document.querySelectorAll('*')].find((e) => e.__chat).__chat; c.changesCard(%s, false);"
                    " c.changesCard(Object.assign({}, %s, { turn: 'T2' }), false); return 1; })()" % (json.dumps(card), json.dumps(card)))
    txt = await t.page.js("[...document.querySelectorAll('.changes')].map((e) => e.innerText)")
    check(len(txt) == 2 and "moved 2 parts" in txt[1] and "routed 3 nets" in txt[1] and "+48 tracks, +6 vias" in txt[1] and "R8 4.7k → 10k" in txt[1]
          and "decisions.md" in txt[1], txt)
    check(await t.page.js("document.querySelectorAll('.changes .undo').length") == 1 and
          await t.page.js("!!document.querySelector('.changes[data-turn=T2] .undo')"), "Undo should be on the latest turn only")
    await t.shot("turn-changes")
    await t.page.js("[...document.querySelectorAll('.changes[data-turn=T2] button')].find((b) => b.textContent.includes('Show on the board')).click(); 1")
    hl = await t.page.wait("(() => { const v = document.querySelector('.viewer canvas').__view; return v.hl && [...v.hl.refs]; })()", 5)
    check(sorted(hl) == ["C12", "J1", "U1"], hl)


@test
async def plan_usage_in_the_header_and_the_run_monitor(t):
    # the last reading of the plan's limit (tracewright/usage.py): 86 % used, so the chat header says so too
    with open(os.path.join(t.s.home, "plan_usage.json"), "w") as f:
        json.dump({"status": "allowed_warning", "window": "five_hour", "used": 0.86, "resets_at": int(time.time()) + 5400,
                   "at": int(time.time())}, f)
    await t.open_project("board")
    pill = await t.page.wait("(() => { const p = document.querySelector('.chathead .planpill'); return p && !p.hidden && p.innerText; })()", 10)
    check(pill == "86 %", pill)
    tip = await t.page.js("document.querySelector('.chathead .planpill').dataset.tip")
    check(tip.startswith("86 % of the 5-hour limit, resets ") and "share your plan" in tip, tip)
    await t.page.js("(() => { document.querySelector('.mcbtn').click(); return 1; })()")
    meter = await t.page.wait("(() => { const m = document.querySelector('.mc-plan'); return m && !m.hidden && m.innerText; })()", 10)
    check(meter.startswith("86 % of the 5-hour limit, resets ") and "warn" in await t.page.js("document.querySelector('.mc-plan').className"), meter)
    await t.shot("plan-usage")
    await t.page.key("Escape")
    os.remove(os.path.join(t.s.home, "plan_usage.json"))


@test
async def sign_off_before_ordering(t):
    await t.open_project()
    await t.place("checks", "signoff")
    head = await t.page.wait("document.querySelector('.so .page-head') && document.querySelector('.so .page-head').innerText", 10)
    check("Sign-off" in head and ("Not ready" in head or "Ready to sign off" in head), head)
    txt = await t.page.js("document.querySelector('.so').innerText")
    check("Checks" in txt and "Waivers" in txt and "Needs the built board" in txt, txt[:400])
    await t.shot("signoff")
    await t.place("parts", "outputs")
    lock = await t.page.wait("document.querySelector('.or-lock') && document.querySelector('.or-lock').innerText", 10)
    check("Sign the design off first" in lock, lock)
    check(await t.page.js("[...document.querySelectorAll('.or-acts button')].filter((b) => !b.closest('.or-lock')).every((b) => b.disabled)"),
          "order buttons enabled before sign-off")
    await t.shot("order-locked")


@test
async def mentions_point_claude_at_parts_and_nets(t):
    await t.open_project("board")
    await t.page.wait("document.querySelector('.viewer canvas').__view.data", 20)
    ta = "document.querySelector('.composer textarea')"
    await t.page.js(f"{ta}.focus(); 1")
    await t.page.type("Check @U")
    rows = await t.page.wait("[...document.querySelectorAll('.mentions .mrow b')].map((b) => b.textContent).join(',') || null", 10)
    check(rows.split(",")[0].startswith("U"), rows)
    await t.shot("mentions")
    await t.page.key("ArrowDown", code="ArrowDown")
    await t.page.key("Enter", code="Enter")
    val = await t.page.js(f"{ta}.value")
    pick = rows.split(",")[1]
    check(val == f"Check @{pick} ", f"{val!r} (picked {pick})")
    check(await t.page.js("document.querySelector('.mentions').style.display") == "none", "the list stayed open")
    # what the message would carry: the part, where it is, which sheet
    ment = await t.page.js(f"(() => {{ const c = [...document.querySelectorAll('*')].find((e) => e.__chat).__chat; return c.takeMentions({ta}.value); }})()")
    check(len(ment) == 1 and ment[0]["mtype"] == "part" and ment[0]["ref"] == pick and "on the top of the board" in ment[0]["label"], ment)
    # a sent message's mention shows the part when clicked
    await t.page.js("(() => { const c = [...document.querySelectorAll('*')].find((e) => e.__chat).__chat; c.userMsg('Check @%s now', %s); return 1; })()"
                    % (pick, json.dumps(ment)))
    await t.page.click(".msg.user .mention")
    hl = await t.page.wait("(() => { const v = document.querySelector('.viewer canvas').__view; return v.hl && [...v.hl.refs]; })()", 5)
    check(hl == [pick], hl)


@test
async def part_card_without_ask(t):
    await t.open_project("board")
    await t.page.wait("document.querySelector('.viewer canvas').__view.data", 20)
    await t.page.js("document.querySelector('.viewer canvas').__view.ws.select([{ ref: 'U1' }], 'board'); 1")
    card = await t.page.wait("document.querySelector('.in-acts') && document.querySelector('.in-acts').innerText", 15)
    check("Ask" not in card and "Open" in card, card)
    await t.shot("part-card")


# a speech recognizer the test speaks through (headless Chrome has no microphone or speech service)
FAKE_SPEECH = """
window.SpeechRecognition = window.webkitSpeechRecognition = class {
  constructor() { window.__sr = this; this.state = 'new'; }
  start() { this.state = 'on'; setTimeout(() => this.onstart && this.onstart(), 5); }
  stop() { this.state = 'stopped'; setTimeout(() => this.onend && this.onend(), 5); }
  abort() { this.state = 'aborted'; this.onend && this.onend(); }
  say(parts, final) {
    const results = parts.map((t, i) => Object.assign([{ transcript: t }], { isFinal: !!final || i < parts.length - 1 }));
    this.onresult && this.onresult({ results });
  }
};
"""


@test
async def dictation_writes_into_the_message_box(t):
    await t.page.call("Page.addScriptToEvaluateOnNewDocument", source=FAKE_SPEECH)
    await t.page.call("Page.reload")
    await asyncio.sleep(0.5)
    await t.open_project("board")
    await t.page.wait("document.querySelector('.composer .micbtn')", 10)
    ta = "document.querySelector('.composer textarea')"
    await t.page.js(f"{ta}.value = 'Route '; {ta}.dispatchEvent(new Event('input')); 1")
    # a click starts it; words land after what was typed
    await t.page.click(".composer .micbtn")
    await t.page.wait("document.querySelector('.micbtn.rec') && window.__sr && window.__sr.state === 'on'", 5)
    await t.page.js("window.__sr.say(['the USB pair'], false); 1")
    check(await t.page.js(f"{ta}.value") == "Route the USB pair", await t.page.js(f"{ta}.value"))
    await t.page.js("window.__sr.say(['the USB pair', ' first.'], true); 1")
    check(await t.page.js(f"{ta}.value") == "Route the USB pair first.", await t.page.js(f"{ta}.value"))
    await t.page.wait("document.querySelector('.composer-bar').innerText.includes('Listening')", 5)   # the bar says it is listening
    await t.shot("dictation")
    # a second click stops it; the text stays
    await t.page.click(".composer .micbtn")
    await t.page.wait("!document.querySelector('.micbtn.rec')", 5)
    check(await t.page.js(f"{ta}.value") == "Route the USB pair first.", "the dictated text did not stay")
    # Esc while listening drops what was said
    await t.page.click(".composer .micbtn")
    await t.page.wait("window.__sr.state === 'on'", 5)
    await t.page.js("window.__sr.say(['scratch that'], false); 1")
    await t.page.js(f"{ta}.focus(); 1")
    await t.page.key("Escape", code="Escape")
    await t.page.wait("!document.querySelector('.micbtn.rec')", 5)
    check(await t.page.js(f"{ta}.value") == "Route the USB pair first.", f"Esc left: {await t.page.js(ta + '.value')!r}")
    # held down: listens until let go
    box = await t.page.js("(() => { const r = document.querySelector('.composer .micbtn').getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2]; })()")
    t0 = time.time()
    await t.page.call("Input.dispatchMouseEvent", type="mouseMoved", x=box[0], y=box[1], timestamp=t0)
    await t.page.call("Input.dispatchMouseEvent", type="mousePressed", x=box[0], y=box[1], button="left", clickCount=1, timestamp=t0)
    await t.page.wait("!!document.querySelector('.micbtn.rec') && window.__sr.state === 'on'", 5)   # listening while held
    await t.page.js("window.__sr.say(['and the power'], true); 1")
    await t.page.call("Input.dispatchMouseEvent", type="mouseReleased", x=box[0], y=box[1], button="left", clickCount=1, timestamp=t0 + 0.9)
    await t.page.wait("!document.querySelector('.micbtn.rec')", 5)
    check(await t.page.js(f"{ta}.value") == "Route the USB pair first. and the power", await t.page.js(f"{ta}.value"))


BV = "document.querySelector('.viewer canvas').__view"


async def board_point(page, wx, wy):
    """Where a board point (mm) is on the page."""
    return await page.js(f"(() => {{ const v = {BV}, r = v.canvas.getBoundingClientRect(), [sx, sy] = v.toScreen({wx}, {wy}); return [r.left + sx, r.top + sy]; }})()")


async def board_drag(page, a, b, steps=10):
    """A press at board point a, a drag to b, a release (board mm)."""
    (x0, y0), (x1, y1) = await board_point(page, *a), await board_point(page, *b)
    await page.call("Input.dispatchMouseEvent", type="mouseMoved", x=x0, y=y0)
    await page.call("Input.dispatchMouseEvent", type="mousePressed", x=x0, y=y0, button="left", clickCount=1)
    for i in range(1, steps + 1):
        await page.call("Input.dispatchMouseEvent", type="mouseMoved", x=x0 + (x1 - x0) * i / steps, y=y0 + (y1 - y0) * i / steps, button="left", buttons=1)
    await page.call("Input.dispatchMouseEvent", type="mouseReleased", x=x1, y=y1, button="left", clickCount=1)


async def settle(page):
    """Wait for the board view's camera to stop moving (it flies to what is found or picked)."""
    last = None
    for _ in range(40):
        cam = await page.js(f"[{BV}.scale, {BV}.ox, {BV}.oy].map((v) => Math.round(v * 100))")
        if cam == last:
            return
        last = cam
        await asyncio.sleep(0.12)


async def board_click(page, wx, wy):
    await page.mouse(*(await board_point(page, wx, wy)))


@test
async def board_editor_moves_routes_and_undoes(t):
    """Editing in the board view: a part dragged 2 mm (saved to the board, one undo step) and put back with ⌘Z; a net's
    copper deleted and routed again with the route tool, pad to pad, round everything in the way; a keep-out drawn,
    picked by its edge and deleted; the board as it was at the end."""
    base = f"api/projects/{t.pid}/board"
    await t.open_project("board")
    await t.page.wait(f"{BV} && {BV}.data && {BV}.data.footprints.length", 30)
    await t.page.click(".tbtn.editbtn")
    await t.page.wait("document.querySelector('.editbar .tbtn') && document.querySelector('.viewer.editing')", 5)
    await t.shot("board-edit-bar")
    board = lambda: t.s.get(base)
    fp = lambda b, ref: next(f for f in b["footprints"] if f["ref"] == ref)
    b0 = board()
    c4 = fp(b0, "C4")
    cx, cy = (c4["bbox"][0] + c4["bbox"][2]) / 2, (c4["bbox"][1] + c4["bbox"][3]) / 2
    # 1. C4 found and picked, then dragged 2 mm to the right (from its middle, where a track runs over it)
    await t.page.click(".findbox input")
    await t.page.type("C4")
    await t.page.wait("document.querySelector('.findres .fr')", 5)
    await t.page.key("Enter", "Enter")
    await t.page.wait(f"{BV}.sel.has('C4')", 5)
    await settle(t.page)
    await board_drag(t.page, (cx, cy), (cx + 2, cy))
    await t.page.wait(f"!document.querySelector('.estat .spinner')", 20)
    for _ in range(50):
        moved = fp(board(), "C4")
        if abs(moved["x"] - (c4["x"] + 2)) < 0.05:
            break
        await asyncio.sleep(0.2)
    check(abs(moved["x"] - (c4["x"] + 2)) < 0.05 and abs(moved["y"] - c4["y"]) < 0.05, f"C4 at {moved['x']}, {moved['y']}, wanted {c4['x'] + 2}, {c4['y']}")
    h = t.s.get(base + "/history")
    check(h["undo"] == 1 and h["undo_label"] == "Move C4", h)
    # 2. ⌘Z puts it back
    await t.page.wait(f"{BV}.data && Math.abs({BV}.byRef.C4.x - {c4['x'] + 2}) < 0.05", 10)
    await t.page.key("z", "KeyZ", modifiers=4)
    for _ in range(50):
        back = fp(board(), "C4")
        if abs(back["x"] - c4["x"]) < 0.01:
            break
        await asyncio.sleep(0.2)
    check(abs(back["x"] - c4["x"]) < 0.01, f"undo left C4 at {back['x']}")
    await t.page.key("f", "KeyF", text="f")                                          # the whole board in view
    await settle(t.page)
    # 3. a two-pad net's copper deleted, then routed again in the view
    pads = {}
    for f in b0["footprints"]:
        for pd in f["pads"]:
            if pd["net"] and "F.Cu" in pd["l"]:
                pads.setdefault(pd["net"], []).append((f["ref"], pd))
    net = next(n for n in ("/MCU/PB3_USB_N", "/MCU/PB4_USB_P") if len(pads.get(n, [])) == 2)
    (ra, pa), (rb, pb) = pads[net]
    t.s.post(base + "/edit", {"ops": [{"op": "delete", "nets": [net]}], "label": "Clear " + net})
    await t.page.wait(f"{BV}.data && !{BV}.data.tracks.some((x) => x[6] === {json.dumps(net)})", 15)
    rats = [l for l in board()["ratsnest"] if l[4] == net]
    check(rats, f"no ratsnest line for {net} with its tracks gone")
    await t.page.key("x", "KeyX", text="x")
    await t.page.wait("document.querySelector('.editbar .tbtn.on') && " + BV + ".ed.tool === 'route'", 5)
    await board_click(t.page, pa["x"], pa["y"])
    await t.page.wait(f"{BV}.ed.route && {BV}.ed.route.net === {json.dumps(net)}", 5)
    (sx, sy) = await board_point(t.page, pb["x"], pb["y"])
    await t.page.call("Input.dispatchMouseEvent", type="mouseMoved", x=sx, y=sy)
    await t.page.wait(f"{BV}.ed.route && {BV}.ed.route.preview && !{BV}.ed.route.blocked", 10)
    await t.shot("board-edit-route")
    await t.page.mouse(sx, sy)
    for _ in range(60):
        b1 = board()
        if any(x[6] == net for x in b1["tracks"]) and not any(l[4] == net for l in b1["ratsnest"] or []):
            break
        await asyncio.sleep(0.25)
    check(any(x[6] == net for x in b1["tracks"]), f"{net} was not routed again")
    check(not any(l[4] == net for l in b1["ratsnest"] or []), f"{net} still has a ratsnest line: {[l for l in b1['ratsnest'] if l[4] == net]}")
    # 4. a keep-out in a corner of the board, picked by its edge, then deleted
    bb = b0["bbox"]
    k0 = [bb[0] + 3, bb[1] + 3]
    corners = [k0, [k0[0] + 3, k0[1]], [k0[0] + 3, k0[1] + 2]]
    await t.page.key("Escape", "Escape")
    await t.page.key("k", "KeyK", text="k")
    for c in corners:
        await board_click(t.page, *c)
    await t.page.key("Enter", "Enter")
    await t.page.wait("document.querySelector('.epop')", 5)
    await t.shot("board-edit-keepout")
    await t.page.js("[...document.querySelectorAll('.epop button')].find((b) => b.textContent.includes('Add the keep-out')).click(); 1")
    n0 = len([z for z in b0["zones"] if z["rule"]])
    for _ in range(60):
        b2 = board()
        if len([z for z in b2["zones"] if z["rule"]]) == n0 + 1:
            break
        await asyncio.sleep(0.25)
    ko = [z for z in b2["zones"] if z["rule"] and z["name"] == "Keep-out"]
    check(ko and ko[0]["ko"]["tracks"], f"no keep-out drawn: {[z['name'] for z in b2['zones'] if z['rule']]}")
    await t.page.wait(f"{BV}.data.zones.some((z) => z.id === {json.dumps(ko[0]['id'])})", 10)
    await t.page.key("Escape", "Escape")
    await board_click(t.page, (corners[0][0] + corners[1][0]) / 2, corners[0][1])         # the middle of its top edge
    await t.page.wait(f"{BV}.ed.items.has('z:' + {json.dumps(ko[0]['id'])})", 5)
    await t.page.key("Delete", "Delete")
    for _ in range(60):
        b3 = board()
        if not any(z["id"] == ko[0]["id"] for z in b3["zones"]):
            break
        await asyncio.sleep(0.25)
    check(not any(z["id"] == ko[0]["id"] for z in b3["zones"]), "the keep-out was not deleted")
    # back to the board as it was
    for _ in range(20):
        h = t.s.get(base + "/history")
        if not h["undo"]:
            break
        t.s.post(base + "/undo")
    end = board()
    check(len(end["tracks"]) == len(b0["tracks"]) and len(end["zones"]) == len(b0["zones"]), "undoing everything did not give the board back")
    await t.page.click(".tbtn.editbtn")


@test
async def flags_are_threads_with_drawings(t):
    """A flag drawn as a route sketch and asked as a question: the sketch shows on the board and is saved with the
    flag; Claude's disagreement turns it red with its reasons in the thread; Do it anyway sends it back open."""
    base = f"api/projects/{t.pid}/review"
    await t.open_project("board")
    await t.page.wait(f"{BV} && {BV}.data && {BV}.data.footprints.length", 30)
    await t.page.key("f", "KeyF", text="f")
    await settle(t.page)
    await t.page.key("c", "KeyC", text="c")
    await t.page.wait("document.querySelector('.vhint .fmodes')", 5)
    await t.page.js("[...document.querySelectorAll('.vhint .fmodes .tbtn')][3].click(); 1")             # the route sketch
    bb = (await t.page.js(f"{BV}.data.bbox"))
    pts = [(bb[0] + 12, bb[1] + 8), (bb[0] + 20, bb[1] + 8), (bb[0] + 20, bb[1] + 14)]
    for x, y in pts:
        await board_click(t.page, x, y)
    await t.page.wait("document.querySelectorAll('.flagmarks .fmark.route circle').length === 3", 5)
    await t.page.key("Enter", "Enter")
    await t.page.wait("document.querySelector('.flag-editor .fe-ask')", 5)
    chips = await t.page.js("[...document.querySelectorAll('.flag-editor .fe-ctx .ctx')].map((c) => c.textContent).join('|')")
    check("route sketch" in chips, chips)
    await t.page.js("[...document.querySelectorAll('.flag-editor .fe-ask button')].find((b) => b.textContent === 'Ask first').click(); 1")
    await t.page.js("const ta = document.querySelector('.flag-editor textarea'); ta.value = 'Would this way keep the pair away from the crystal?'; 1")
    await t.shot("flag-route-sketch")
    await t.page.js("[...document.querySelectorAll('.flag-editor button')].find((b) => b.textContent === 'Add flag').click(); 1")
    for _ in range(40):
        fl = t.s.get(base)["flags"]
        if fl:
            break
        await asyncio.sleep(0.2)
    f = fl[-1]
    check(f["ask"] == "question" and f["marks"] and f["marks"][0]["t"] == "route" and len(f["marks"][0]["p"]) == 3, f)
    await t.page.wait("document.querySelectorAll('.flagmarks .fmark.route.open').length === 1", 5)
    # Claude disagrees (written as its review tool would)
    root = t.s.demo["root"]
    path = os.path.join(root, ".tracewright", "review.json")
    d = json.load(open(path))
    for x in d["flags"]:
        if x["id"] == f["id"]:
            x["status"] = "declined"; x["resolved_by"] = "claude"; x["resolution"] = "It would pass right over the crystal's guard ring."
            x["thread"].append({"who": "claude", "text": x["resolution"], "at": "2026-10-01T09:00:00", "outcome": "declined"})
    json.dump(d, open(path, "w"))
    await t.page.js(f"{BV}.ws.review.load(); 1")
    await t.page.wait(f"document.querySelector('.fpin.declined')", 5)
    await t.page.click(".fpin.declined")
    await t.page.wait("document.querySelector('.flag-editor .fe-thread .fe-msg.claude.declined')", 5)
    say = await t.page.js("document.querySelector('.flag-editor .fe-msg.claude .fe-say').textContent")
    check("guard ring" in say, say)
    await t.shot("flag-thread-declined")
    await t.page.js("[...document.querySelectorAll('.flag-editor button')].find((b) => b.textContent.includes('Do it anyway')).click(); 1")
    for _ in range(40):
        g = next(x for x in t.s.get(base)["flags"] if x["id"] == f["id"])
        if g["status"] == "open":
            break
        await asyncio.sleep(0.2)
    check(g["status"] == "open" and g["thread"][-1].get("anyway"), g)
    await t.page.key("Escape", "Escape")
    req = urllib.request.Request(t.s.url + f"{base}/{f['id']}", method="DELETE", headers={"Origin": t.s.url.rstrip("/")})
    urllib.request.urlopen(req, timeout=30).read()


@test
async def needs_your_ok_list_in_review(t):
    """What a run went ahead with shows at the top of the Review panel: Keep marks it kept; an asked change has
    Approve and Decline instead."""
    root = t.s.demo["root"]
    os.makedirs(os.path.join(root, ".tracewright"), exist_ok=True)
    path = os.path.join(root, ".tracewright", "approvals.json")
    with open(path, "w") as f:
        json.dump({"next": 3, "items": [
            {"id": "A1", "kind": "floorplan", "group": "confirm", "title": "J1 (connector) moved", "ref": "J1", "status": "pending",
             "detail": "Its place was agreed with the floorplan.", "at": "2026-10-01T09:00:00", "from": [103.675, 117.5, -90, "F"]},
            {"id": "A2", "kind": "limits", "group": "ask", "title": "Change the agreed limits: largest board 50 × 35 -> 60 × 40",
             "status": "pending", "agreed": {"max_size_mm": [50, 35]}, "proposed": {"max_size_mm": [60, 40]}, "at": "2026-10-01T09:00:00"}]}, f)
    try:
        await t.open_project("board")
        await t.page.wait(f"!!({BV} && {BV}.ws)", 30)
        await t.page.js(f"{BV}.ws.approvals.load(); {BV}.ws.toggleReview(true); 1")
        await t.page.wait("document.querySelector('.drawer .oklist .ok-row')", 10)
        txt = await t.page.js("document.querySelector('.drawer .oklist').innerText")
        check("Needs your OK (2)" in txt and "J1 (connector) moved" in txt and "Approve" in txt and "Keep" in txt, txt)
        await t.shot("needs-your-ok")
        await t.page.js("[...document.querySelectorAll('.drawer .ok-row')][0].querySelector('.btn.primary').click(); 1")
        for _ in range(40):
            items = t.s.get(f"api/projects/{t.pid}/approvals")["items"]
            if items[0]["status"] == "kept":
                break
            await asyncio.sleep(0.2)
        check(items[0]["status"] == "kept", items[0])
        await t.page.wait("document.querySelector('.drawer .oklist') && document.querySelector('.drawer .oklist').innerText.includes('Needs your OK (1)')", 5)
    finally:
        os.remove(path)
        await t.page.js(f"{BV}.ws.toggleReview(false); 1")


@test
async def suggested_layout_as_ghosts(t):
    """Claude's suggested layout shows as numbered ghosts on the floorplan with each move's reason; leaving one out
    and taking the rest moves only those."""
    root = t.s.demo["root"]
    cfgp = os.path.join(root, "tracewright.json")
    before = open(cfgp).read()
    cfg = json.loads(before)
    cfg["start"] = {"mode": "guided", "phase": "ready"}
    with open(cfgp, "w") as f:
        json.dump(cfg, f, indent=1)
    cvp = os.path.join(root, ".tracewright", "canvas.json")
    os.makedirs(os.path.dirname(cvp), exist_ok=True)
    with open(cvp, "w") as f:
        json.dump({"floorplan": DEMO_FLOORPLAN, "proposal": {"asked": 1, "notes": ["MCU nearer the top"], "ready": 2, "summary": "Shorter runs.",
                   "moves": [{"id": "mcu", "x": 30, "y": 10, "why": "near the top, as asked"}, {"id": "power", "x": 14, "y": 22, "why": "beside the USB"}],
                   "replies": [{"note": 1, "text": "Moved it up 6 mm.", "outcome": "followed"}]}}, f)
    try:
        await t.page.goto(t.s.url + f"#/p/{t.pid}")
        await t.page.wait("document.querySelectorAll('.fp-svg .fp-ghost').length === 2", 20)
        txt = await t.page.js("document.querySelector('.gd-suggest').innerText")
        check("near the top, as asked" in txt and "Followed: Moved it up 6 mm." in txt and "Take all of it" in txt, txt)
        await t.shot("suggested-layout")
        await t.page.js("document.querySelectorAll('.gd-sg input')[1].click(); 1")                      # leave the power block out
        await t.page.wait("[...document.querySelectorAll('.gd-sgacts .btn.primary')].some((b) => b.textContent === 'Take 1 of 2')", 5)
        await t.page.js("[...document.querySelectorAll('.gd-sgacts .btn.primary')].find((b) => b.textContent === 'Take 1 of 2').click(); 1")
        for _ in range(40):
            fp = t.s.get(f"api/projects/{t.pid}/canvas")["floorplan"]
            mcu = next(i for i in fp["items"] if i["id"] == "mcu")
            if mcu["y"] == 10:
                break
            await asyncio.sleep(0.2)
        pw = next(i for i in fp["items"] if i["id"] == "power")
        check(mcu["y"] == 10 and mcu.get("moved") and pw["y"] == 26, (mcu, pw))
    finally:
        with open(cfgp, "w") as f:
            f.write(before)
        os.remove(cvp)


@test
async def placement_suggestion_on_the_board(t):
    """Claude's suggested placement shows on the board as numbered ghosts with a list to take from; taking one moves
    that part only (an undoable edit)."""
    root = t.s.demo["root"]
    b = t.s.get(f"api/projects/{t.pid}/board")
    fp = {f["ref"]: f for f in b["footprints"]}
    c3, c4 = fp["C3"], fp["C4"]
    path = os.path.join(root, ".tracewright", "board_proposal.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"asked": 1, "ready": 2, "notes": ["Decoupling right at U2"], "refs": ["C3", "C4"], "summary": "Tighter decoupling.",
                   "moves": [{"ref": "C3", "x": c3["x"] + 1.5, "y": c3["y"], "rot": c3["a"], "side": "F", "why": "next to U2 pin 8"},
                             {"ref": "C4", "x": c4["x"], "y": c4["y"] + 1.5, "rot": c4["a"], "side": "F", "why": "shorter GND return"}],
                   "replies": [{"note": 1, "text": "Both within 1 mm.", "outcome": "followed"}]}, f)
    try:
        await t.open_project("board")
        await t.page.wait(f"!!({BV} && {BV}.data && {BV}.ed.proposal)", 30)
        await t.page.wait("document.querySelector('.eprop .epr-row')", 10)
        txt = await t.page.js("document.querySelector('.eprop').innerText")
        check("next to U2 pin 8" in txt and "Followed: Both within 1 mm." in txt and "Take all of it" in txt, txt)
        await t.shot("placement-suggestion")
        await t.page.js("document.querySelectorAll('.eprop .epr-row input')[1].click(); 1")
        await t.page.wait("[...document.querySelectorAll('.eprop .btn.primary')].some((b) => b.textContent === 'Take 1 of 2')", 5)
        await t.page.js("[...document.querySelectorAll('.eprop .btn.primary')].find((b) => b.textContent === 'Take 1 of 2').click(); 1")
        for _ in range(50):
            now = {f["ref"]: f for f in t.s.get(f"api/projects/{t.pid}/board")["footprints"]}
            if abs(now["C3"]["x"] - (c3["x"] + 1.5)) < 0.01:
                break
            await asyncio.sleep(0.2)
        check(abs(now["C3"]["x"] - (c3["x"] + 1.5)) < 0.01 and abs(now["C4"]["y"] - c4["y"]) < 0.01, (now["C3"]["x"], now["C4"]["y"]))
        h = t.s.get(f"api/projects/{t.pid}/board/history")
        check(h["undo"] >= 1 and "suggested placement" in h["undo_label"], h)
    finally:
        t.s.post(f"api/projects/{t.pid}/board/proposal", {"action": "dismiss"})
        for _ in range(5):
            if not t.s.get(f"api/projects/{t.pid}/board/history")["undo"]:
                break
            t.s.post(f"api/projects/{t.pid}/board/undo")


@test
async def board_editor_routes_a_pair_and_tunes_a_length(t):
    """A differential pair routed together from its connector: both halves side by side at the pair's pitch; and a
    track lengthened by a meander to the length asked for."""
    base = f"api/projects/{t.pid}/board"
    b0 = t.s.get(base)
    nets = [n for n in b0["nets"] if n.split("/")[-1] in ("USB_D_P", "USB_D_N")]
    check(len(nets) == 2, nets)
    t.s.post(base + "/edit", {"ops": [{"op": "delete", "nets": nets}], "label": "Clear the USB pair"})
    try:
        await t.open_project("board")
        await t.page.wait(f"!!({BV} && {BV}.data && {BV}.netKinds && Object.keys({BV}.netKinds).length)", 30)
        await t.page.wait(f"!{BV}.data.tracks.some((x) => {json.dumps(nets)}.includes(x[6]))", 15)
        await t.page.key("f", "KeyF", text="f")
        await settle(t.page)
        await t.page.click(".tbtn.editbtn")
        await t.page.key("x", "KeyX", text="x")
        j1 = next(f for f in b0["footprints"] if f["ref"] == "J1")
        pd = next(p for p in j1["pads"] if p["net"] == nets[0] and "F.Cu" in p["l"])
        await board_click(t.page, pd["x"], pd["y"])
        await t.page.wait(f"!!({BV}.ed.route && {BV}.ed.route.pair)", 5)
        check(await t.page.js(f"{BV}.ed.route.pair.net") == nets[1], "the pair's other half was not found")
        tx, ty = pd["x"] + 4, pd["y"]                       # a clear spot east of the connector
        sx, sy = await board_point(t.page, tx, ty)
        await t.page.call("Input.dispatchMouseEvent", type="mouseMoved", x=sx, y=sy)
        await t.page.wait(f"!!({BV}.ed.route.halves && !{BV}.ed.route.blocked)", 10)
        await t.shot("board-edit-pair")
        await t.page.mouse(sx, sy)
        await t.page.key("Enter", "Enter")
        for _ in range(60):
            b1 = t.s.get(base)
            got = {n: [x for x in b1["tracks"] if x[6] == n] for n in nets}
            if all(got.values()):
                break
            await asyncio.sleep(0.25)
        check(all(got.values()), f"the pair was not routed: {[(n, len(v)) for n, v in got.items()]}")
        # the halves run side by side: their longest segments are parallel at the pair's pitch
        import math
        la = max(got[nets[0]], key=lambda x: math.hypot(x[2] - x[0], x[3] - x[1]))
        lb = max(got[nets[1]], key=lambda x: math.hypot(x[2] - x[0], x[3] - x[1]))
        da = (la[2] - la[0], la[3] - la[1]); db = (lb[2] - lb[0], lb[3] - lb[1])
        cross = abs(da[0] * db[1] - da[1] * db[0]) / (math.hypot(*da) * math.hypot(*db))
        check(cross < 0.02, f"the halves are not parallel ({cross:.3f})")
        # tune: the longest straight run on the board, 2 mm longer
        await t.page.key("Escape", "Escape")
        await t.page.key("t", "KeyT", text="t")
        cand = max((x for x in b1["tracks"] if len(x) == 7 and x[6] not in nets and x[5] == "F.Cu"), key=lambda x: math.hypot(x[2] - x[0], x[3] - x[1]))
        net = cand[6]
        before = sum(math.hypot(x[2] - x[0], x[3] - x[1]) for x in b1["tracks"] if x[6] == net)
        await board_click(t.page, (cand[0] + cand[2]) / 2, (cand[1] + cand[3]) / 2)
        await t.page.wait("document.querySelector('.epop input.einp')", 5)
        await t.page.js(f"const i = document.querySelector('.epop input.einp'); i.value = '{before + 2:.3f}'; 1")
        await t.page.js("[...document.querySelectorAll('.epop button')].find((b) => b.textContent === 'Tune it').click(); 1")
        for _ in range(60):
            b2 = t.s.get(base)
            after = sum(math.hypot(x[2] - x[0], x[3] - x[1]) for x in b2["tracks"] if x[6] == net)
            if after > before + 0.5:
                break
            await asyncio.sleep(0.25)
        check(abs(after - (before + 2)) < 0.05, f"{net}: {before:.3f} -> {after:.3f}, wanted {before + 2:.3f}")
        await t.shot("board-edit-tuned")
    finally:
        for _ in range(10):
            if not t.s.get(base + "/history")["undo"]:
                break
            t.s.post(base + "/undo")


@test
async def second_opinion_card(t):
    """A second opinion's concerns show under the run in the chat; Ask Claude about these puts them in the message
    box for the user to send (or not)."""
    await t.open_project("board")
    await t.page.wait(f"!!({BV} && {BV}.ws && {BV}.ws.chat)", 30)
    await t.page.js(f"""(() => {{ const ws = {BV}.ws; ws.ev.emit("agent.review", {{ turn: "T1", state: "running" }});
        ws.ev.emit("agent.review", {{ turn: "T1", state: "done", cost: 0.03, concerns: [{{ what: "R8 raised to 10k", why: "the I2C rise time at 400 kHz", where: "R8" }}] }}); return 1; }})()""")
    await t.page.wait("document.querySelector('.review2.card2 .r2-list li')", 5)
    check(await t.page.js("document.querySelectorAll('.review2').length") == 1, "the reading line was not replaced by the verdict")
    await t.page.js("document.querySelector('.review2 .r2-acts .btn').click(); 1")
    val = await t.page.js("document.querySelector('.composer textarea').value")
    check("R8 raised to 10k (R8): the I2C rise time" in val, val)
    await t.shot("second-opinion")
    await t.page.js("document.querySelector('.composer textarea').value = ''; 1")


@test
async def simulation_under_docs_and_in_signoff(t):
    """A simulation Claude kept (docs/sim) is listed under Docs > Simulations with its verdict: the plot, the pass
    criterion's PASS line, each probe's values and the netlist; the sign-off page's evidence opens it."""
    root = t.s.demo["root"]
    cfgp = os.path.join(root, "tracewright.json")
    before = open(cfgp).read()
    d = os.path.join(root, "docs", "sim")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "sense-filter.cir"), "w") as f:
        f.write("lowpass\nV1 in 0 AC 1\nR1 in out 1k\nC1 out 0 100n\n.ac dec 50 10 1meg\n.end\n")
    with open(os.path.join(d, "sense-filter.json"), "w") as f:
        json.dump({"name": "sense-filter", "title": "Battery sense RC filter", "probes": ["v(out)"], "requirement": "r1",
                   "criteria": {"probe": "v(out)", "corner": {"min": 1000, "max": 2000}}}, f)
    cfg = json.loads(before)
    cfg["evidence"] = [{"req": "r1", "kind": "sim", "label": "Battery sense RC filter", "ref": "docs/sim/sense-filter.cir", "status": "open"}]
    with open(cfgp, "w") as f:
        json.dump(cfg, f, indent=1)
    os.makedirs(os.path.join(root, ".tracewright"), exist_ok=True)
    with open(os.path.join(root, ".tracewright", "canvas.json"), "w") as f:
        json.dump({"requirements": {"items": [{"label": "Battery sense", "value": "filtered, corner 1 to 2 kHz"}]}}, f)
    try:
        info = t.s.post(f"api/projects/{t.pid}/sims/sense-filter", {"action": "run"})
        check(info["status"] == "ok" and info["check"].startswith("PASS: v(out) -3 dB at 15"), info)
        await t.open_project()
        await t.place("project", "docs")
        await t.page.wait("[...document.querySelectorAll('.flist .listhead b')].some((b) => b.textContent === 'Simulations')", 10)
        await t.page.js("[...document.querySelectorAll('.flist .fitem')].find((e) => e.textContent.includes('Battery sense RC filter')).click(); 1")
        await t.page.wait("document.querySelector('.sim-plot') && document.querySelector('.sim-plot').complete && document.querySelector('.sim-plot').naturalWidth > 0", 10)
        await t.page.wait("document.querySelector('.sim-net') && document.querySelector('.sim-net').textContent.includes('.ac dec')", 10)
        got = await t.page.js("""(() => ({ verdict: document.querySelector('.sim-verdict.ok') && document.querySelector('.sim-verdict.ok').innerText,
          tag: document.querySelector('.sim-tag').innerText, rows: [...document.querySelectorAll('.sim-tab tbody tr')].map((r) => r.innerText) }))()""")
        check(got["verdict"] and got["verdict"].startswith("PASS: v(out) -3 dB at 15") and got["tag"].strip() == "Passes", got)
        check(len(got["rows"]) == 1 and "v(out)" in got["rows"][0] and "kHz" in got["rows"][0], got["rows"])
        await t.shot("simulation")
        # the sign-off page: the evidence followed the run, and opens it
        await t.place("checks", "signoff")
        await t.page.wait("[...document.querySelectorAll('.so-evr.link')].some((e) => e.textContent.includes('docs/sim/'))", 10)
        ev = await t.page.js("[...document.querySelectorAll('.so-ev')].map((e) => e.innerText).join(' | ')")
        check("Simulation" in ev and "Battery sense RC filter (PASS: v(out) -3 dB at 15" in ev, ev)
        await t.page.js("[...document.querySelectorAll('.so-evr.link')].find((e) => e.textContent.includes('docs/sim/')).click(); 1")
        await t.page.wait("document.querySelector('.view.on .sim-title') && document.querySelector('.view.on .sim-title').innerText.includes('Battery sense RC filter')", 10)
    finally:
        with open(cfgp, "w") as f:
            f.write(before)
        os.remove(os.path.join(root, ".tracewright", "canvas.json"))
        shutil.rmtree(d, ignore_errors=True)


@test
async def schematic_edits_from_the_card_and_labels(t):
    """A part's card has Edit: the value changed there is written into the schematic; a net label clicked on the
    sheet renames the net; ⌘Z in the schematic view undoes it."""
    root = t.s.demo["root"]
    hw = os.path.join(root, "hardware", "demo")
    files = {f: open(os.path.join(hw, f)).read() for f in os.listdir(hw) if f.endswith(".kicad_sch")}
    try:
        await t.open_project("board")
        await t.page.wait(f"!!({BV} && {BV}.ws)", 30)
        await t.page.js(f"{BV}.ws.show('schematic'); 1")
        SV = f"{BV}.ws.views.schematic"
        await t.page.wait(f"{SV} && {SV}.sheets && {SV}.sheets.length > 0", 20)
        mcu = await t.page.js(f"{SV}.sheets.find((s) => s.file === 'mcu.kicad_sch').name_path")
        await t.page.js(f"{SV}.showSheet({json.dumps(mcu)}); {SV}.toggle('R8'); 1")
        await t.page.wait("[...document.querySelectorAll('.inspector .in-acts button')].some((b) => b.textContent.includes('Edit'))", 15)
        await t.page.js("[...document.querySelectorAll('.inspector .in-acts button')].find((b) => b.textContent.includes('Edit')).click(); 1")
        await t.page.wait("document.querySelector('.modal.pe input')", 5)
        await t.shot("part-edit")
        await t.page.js("""(() => { const i = document.querySelector('.modal.pe input'); i.value = '3.3k'; i.dispatchEvent(new Event('input')); 
          [...document.querySelectorAll('.modal.pe .modal-foot button')].find((b) => b.textContent === 'Save').click(); return 1; })()""")
        # a new value with the old LCSC code: said first, saved on the second press
        await t.page.wait("document.querySelector('.modal.pe .pe-warn') && document.querySelector('.modal.pe .pe-warn').style.display !== 'none'", 5)
        warn = await t.page.js("document.querySelector('.modal.pe .pe-warn').textContent")
        check("C25900 is the part for 4.7k" in warn, warn)
        await t.page.js("[...document.querySelectorAll('.modal.pe .modal-foot button')].find((b) => b.textContent === 'Save anyway').click(); 1")
        await t.page.wait("!document.querySelector('.modal.pe')", 10)
        for _ in range(50):
            if '(property "Value" "3.3k"' in open(os.path.join(hw, "mcu.kicad_sch")).read():
                break
            await asyncio.sleep(0.2)
        check('(property "Value" "3.3k"' in open(os.path.join(hw, "mcu.kicad_sch")).read(), "the value was not written")
        await t.page.wait("document.querySelector('.inspector .in-title') && document.querySelector('.inspector .in-title').textContent.includes('3.3k')", 15)
        # a label on the sheet: rename its net
        await t.page.wait(f"{SV}.cur === {json.dumps(mcu)} && document.querySelector('.sch-label[data-text=\"I2C_SDA\"]')", 15)
        await t.page.js("document.querySelector('.sch-label[data-text=\"I2C_SDA\"]').dispatchEvent(new MouseEvent('click', { bubbles: true })); 1")
        await t.page.wait("document.querySelector('.rn-pop input')", 5)
        await t.shot("rename-net")
        await t.page.js("""(() => { const i = document.querySelector('.rn-pop input'); i.value = 'SDA';
          [...document.querySelectorAll('.rn-pop button')].find((b) => b.textContent === 'Rename').click(); return 1; })()""")
        for _ in range(80):
            if open(os.path.join(hw, "mcu.kicad_sch")).read().count('(label "SDA"') == 2:
                break
            await asyncio.sleep(0.25)
        check(open(os.path.join(hw, "mcu.kicad_sch")).read().count('(label "SDA"') == 2, "the net was not renamed")
        await t.page.wait(f"{SV}.history && {SV}.history.undo >= 2 && !document.querySelector('.rn-pop')", 10)
        # ⌘Z in the schematic view
        await t.page.js("document.activeElement && document.activeElement.blur && document.activeElement.blur(); document.dispatchEvent(new KeyboardEvent('keydown', { key: 'z', metaKey: true, bubbles: true })); 1")
        for _ in range(60):
            if open(os.path.join(hw, "mcu.kicad_sch")).read().count('(label "I2C_SDA"') == 2:
                break
            await asyncio.sleep(0.25)
        check(open(os.path.join(hw, "mcu.kicad_sch")).read().count('(label "I2C_SDA"') == 2, "⌘Z did not undo the rename")
        await t.page.wait(f"{SV}.history && {SV}.history.redo === 1", 10)
        await t.shot("schematic-edited")
    finally:
        for f, txt in files.items():
            with open(os.path.join(hw, f), "w") as fh:
                fh.write(txt)


@test
async def expired_sign_in_card(t):
    """A conversation where Claude's sign-in had expired: each failed turn shows how to sign in again instead of the
    CLI's error as Claude's words, and the last one offers to send the message again."""
    root = t.s.demo["root"]
    d = os.path.join(root, ".tracewright", "sessions")
    os.makedirs(d, exist_ok=True)
    said = "Failed to authenticate: OAuth session expired and could not be refreshed"
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    ask = "Are the orientations of the THT connectors right on JLC's 3D render?"
    recs = [{"kind": "user", "text": "Hello", "turn": "t1", "t": 1.0}, {"kind": "assistant", "text": said, "turn": "t1", "t": 1.1},
            {"kind": "done", "turn": "t1", "cost": 0.0, "is_error": True, "subtype": "success", "t": 1.2},
            {"kind": "user", "text": ask, "turn": "t2", "t": 2.0}, {"kind": "assistant", "text": said, "turn": "t2", "t": 2.1},
            {"kind": "done", "turn": "t2", "cost": 0.0, "is_error": True, "subtype": "success", "t": 2.2}]
    idx = os.path.join(d, "index.json")
    before = open(idx).read() if os.path.exists(idx) else None
    with open(os.path.join(d, "signin0test1.jsonl"), "w") as f:
        f.write("\n".join(json.dumps(r) for r in recs) + "\n")
    with open(idx, "w") as f:
        json.dump([{"sid": "signin0test1", "title": "JLC question", "created": now, "updated": now, "turns": 2, "cost": 0.0}], f)
    try:
        await t.open_project("board")
        await t.page.wait("document.querySelectorAll('.signin').length === 2", 15)
        got = await t.page.js("""(() => ({ cards: [...document.querySelectorAll('.signin')].map((c) => c.innerText),
          again: [...document.querySelectorAll('.signin')].map((c) => [...c.querySelectorAll('button')].some((b) => b.textContent.includes('Send again'))),
          claude: [...document.querySelectorAll('.msgs')].map((m) => m.innerText).join(' ') }))()""")
        check(all("sign in again" in c and "/login" in c and "OAuth session expired" in c for c in got["cards"]), got["cards"])
        check(got["again"] == [False, True], got["again"])
        check(got["claude"].count(said) == 2, "the error should appear only inside the two cards")
        await t.shot("signin-card")
    finally:
        os.remove(os.path.join(d, "signin0test1.jsonl"))
        if before is None:
            os.remove(idx)
        else:
            with open(idx, "w") as f:
                f.write(before)


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
