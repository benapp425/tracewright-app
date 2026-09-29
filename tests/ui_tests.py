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
