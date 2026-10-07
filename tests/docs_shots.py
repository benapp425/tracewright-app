"""The README's pictures, taken from the demo board in headless Chrome at 1600 x 1000:

    python3 tests/docs_shots.py [docs/images]

overview, board (with its routing plan), simulate (the board's heat), signal (a net's edge), make (getting it built),
schematic (a design note open), 3d, checks, then space (where tracks fit, on the copy with its copper cleared).
Only the demo board: no one's own designs."""
import asyncio, glob, os, sys, json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ui_tests import Server, Ctx, BV  # noqa: E402
from cdp import Chrome  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# design notes for the demo's copy (the fixture has none): the reasoning behind three of its parts, as Claude writes them
DEMO_NOTES = [
    {"id": "n1", "anchor": {"ref": "R4"}, "short": "Holds RESET high", "sheet": "mcu.kicad_sch", "by": "claude",
     "why": "The ATtiny85's own reset pull-up is weak (30 to 60 kΩ), so noise on the line can reset it. 10 kΩ to "
            "3.3 V holds it high and still lets an ISP programmer pull RESET low (Microchip AN2519, AVR hardware design "
            "considerations)."},
    {"id": "n2", "anchor": {"ref": "R8"}, "short": "SDA/SCL pull-ups: 4.7k to 3.3 V", "sheet": "mcu.kicad_sch", "by": "claude",
     "why": "SDA and SCL are open-drain, so each needs a pull-up. The largest that still meets the rise time is "
            "Rp = tr / (0.8473 × Cb) (I2C specification UM10204): with 4.7 kΩ, about 250 pF of bus at 100 kHz and 75 pF at "
            "400 kHz, which a short Qwiic cable stays under. Boards on the cable may add their own pull-ups; in parallel "
            "that stays fine down to about 1 kΩ (3 mA at 0.4 V)."},
    {"id": "n3", "anchor": {"ref": "R1"}, "short": "Rd on CC1: asks the source for 5 V", "sheet": "power.kicad_sch", "by": "claude",
     "why": "A USB-C sink shows itself with a 5.1 kΩ pull-down (Rd) on each CC pin; a C-to-C charger keeps VBUS off "
            "until it sees one. Each CC pin needs its own, since the plug's orientation decides which one is used "
            "(USB Type-C specification)."},
]


async def shoot(out):
    s = Server()
    try:
        version = s.get("api/info")["version"]
        async with Chrome(width=1600, height=1000) as browser:
            page = await browser.page()
            await page.goto(s.url)
            await page.js("localStorage.setItem('tw.toured.local', '1'); localStorage.setItem('tw.theme', 'dark'); "
                          f"localStorage.setItem('tw.seenVersion', {json.dumps(version)}); localStorage.setItem('tw.updateSeen', '99'); "
                          "localStorage.setItem('tw.layers.collapsed', '0'); 1")
            t = Ctx(s, page, out)
            hw = os.path.dirname(glob.glob(os.path.join(s.ws, "*", "hardware", "*", "mcu.kicad_sch"))[0])
            with open(os.path.join(hw, "design-notes.json"), "w") as f:
                json.dump({"notes": DEMO_NOTES}, f, indent=1)
            calm = ("(() => { document.querySelectorAll('.toast').forEach((e) => e.remove()); "
                    "document.querySelectorAll('.vtip').forEach((e) => { e.style.display = 'none'; }); "
                    "document.querySelectorAll('.tooltip.on').forEach((e) => e.classList.remove('on')); "
                    "const c = document.querySelector('.viewer canvas'); if (c && c.__view) { c.__view.anim = {}; c.__view.draw(); } return 1; })()")

            async def snap(name):
                await page.js(calm)
                await asyncio.sleep(0.6)
                await page.shot(os.path.join(out, name + ".png"))
                print("  " + name)

            await t.open_project("overview")
            await page.wait("document.querySelector('.view.on')", 30)
            await asyncio.sleep(2.0)
            await snap("overview")

            await t.place("design", "board")
            await page.wait(f"!!({BV} && {BV}.data)", 30)
            await page.js(f"{BV}.setPanel('routing'); 1")
            await page.wait("document.querySelectorAll('.rt-net').length > 3", 20)
            await page.js(f"{BV}.fit(false); 1")
            await snap("board")


            await t.place("simulate")
            await page.wait("document.querySelector('.sm-tabs button[data-sim=heat]')", 20)
            await page.js("document.querySelector('.sm-tabs button[data-sim=heat]').click(); 1")
            await page.wait("document.querySelector('.sm-src button')", 10)
            await page.js("[...document.querySelectorAll('.sm-src button')].pop().click(); 1")
            await page.js("(() => { const r = [...document.querySelectorAll('.sm-srow')].pop(), i = r.querySelectorAll('input'); "
                          "i[0].value = 'U1'; i[0].dispatchEvent(new Event('input')); i[1].value = '0.6'; i[1].dispatchEvent(new Event('input')); return 1; })()")
            await page.js("document.querySelector('.sm-run').click(); 1")
            await page.wait("document.querySelector('.sm-legend .sm-bar')", 60)
            await snap("simulate")
            await page.js("document.querySelector('.sm-tabs button[data-sim=signal]').click(); 1")
            await page.wait("document.querySelector('.sm-run')", 10)
            await page.js("(() => { const s = document.querySelector('.sm-ctl select'); s.value = 'USB_D_P'; s.dispatchEvent(new Event('change')); "
                          "return 1; })()")                # the edge from what the net is (USB: 4 ns)
            await page.js("document.querySelector('.sm-run').click(); 1")
            await page.wait("document.querySelector('.sm-svg svg')", 60)
            await snap("signal")

            await t.place("parts", "make")
            await page.wait("document.querySelector('.mk-score b') && document.querySelector('[data-sec=tp] table')", 60)
            await page.js("[...document.querySelectorAll('[data-sec=panel] button')].find((b) => b.textContent.includes('Make the panel')).click(); 1")
            await page.wait("document.querySelector('.mk-panel') && document.querySelector('.mk-panel').naturalWidth > 0", 90)
            await page.js("document.querySelector('[data-sec=tp]').scrollIntoView(); 1")
            await snap("make")

            await t.place("design", "schematic")
            sv = f"{BV}.ws.views.schematic"
            await page.wait(f"{sv} && {sv}.sheets && {sv}.sheets.length > 0", 30)
            await page.js(f"{sv}.showSheet({sv}.sheets.find((x) => x.file === 'mcu.kicad_sch').name_path); 1")
            await page.wait("document.querySelectorAll('.sch-note').length >= 2", 30)
            await page.js(f"(() => {{ const v = {sv}; let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9; "      # the microcontroller's block
                          f"for (const y of v.sheet().symbols || []) {{ const b = y.bbox; if (!b || !['U2', 'C4', 'R4', 'R6', 'R7'].includes(y.ref)) continue; "
                          f"x0 = Math.min(x0, b[0]); y0 = Math.min(y0, b[1]); x1 = Math.max(x1, b[2]); y1 = Math.max(y1, b[3]); }} "
                          f"v.setVB(x0 - 25, y0 - 30, x1 - x0 + 73, y1 - y0 + 52); return 1; }})()")     # room right of R4 for its note
            await asyncio.sleep(0.6)
            await page.js("document.querySelector('.sch-note').dispatchEvent(new MouseEvent('click', {bubbles: true})); 1")
            await asyncio.sleep(1.0)
            await snap("schematic")

            await t.place("design", "3d")
            await page.wait("(() => { const l = document.querySelector('.v3d .v3d-load'); "
                            "return !!(l && getComputedStyle(l).display === 'none' && document.querySelector('.v3d canvas')); })()", 240)
            await asyncio.sleep(2.0)
            await snap("3d")

            await t.place("checks", "checks")
            await page.wait("document.querySelector('.view.on')", 10)
            await page.js("[...document.querySelectorAll('.vbtns button')].find((b) => b.textContent.includes('Run all checks')).click(); 1")
            await page.wait("document.querySelector('.checks-head .vt') && !document.querySelector('.checks-head .vt').textContent.includes('Running') "
                            "&& !document.querySelector('.checks-head .vt').textContent.includes('Not checked')", 300)
            await asyncio.sleep(2.0)
            await snap("checks")

            # where tracks fit is a picture of a board still to route: the copy's copper cleared in the board editor
            r = s.post(f"api/projects/{t.pid}/board/edit", {"ops": [{"op": "delete", "kinds": ["track", "via", "zone"], "all": True}],
                                                          "label": "Clear the copper"})
            assert r.get("ok", True) and not r.get("error"), r
            await t.place("design", "board")
            await page.wait(f"!!({BV} && {BV}.data) && !({BV}.data.tracks || []).length", 30)
            await page.js(f"{BV}.setPanel('layers'); {BV}.fit(false); 1")
            await page.js("[...document.querySelectorAll('.lrow')].find((r) => r.textContent.includes('Routing space')).click(); 1")
            await page.wait("document.querySelector('.sp-legend')", 60)
            await asyncio.sleep(1.0)
            await page.js(calm)
            await page.js(f"(() => {{ const b = {BV}, f = b.byRef.J1, a = f.pads.find((q) => String(q.n) === 'A6'), "
                          f"c = f.pads.find((q) => String(q.n) === 'A7'), x = (a.x + c.x) / 2, y = (a.y + c.y) / 2; "
                          f"const [px, py] = b.toScreen(x, y); b.whyAt(px, py, x, y); return 1; }})()")
            await page.wait(f"{BV}.tip.style.display === 'block' && {BV}.tip.isConnected && {BV}.tip.textContent.length > 10", 30)
            await asyncio.sleep(0.5)
            await page.shot(os.path.join(out, "space.png"))
            print("  space")
    finally:
        s.stop()


if __name__ == "__main__":
    out = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "docs", "images"))
    os.makedirs(out, exist_ok=True)
    asyncio.run(shoot(out))
