"""The README's pictures, taken from the demo board in headless Chrome at 1600 x 1000:

    python3 tests/docs_shots.py [docs/images]

overview, board (with its routing plan), space (where tracks fit), simulate (the board's heat), signal (a net's edge),
make (getting it built), schematic (a design note open), checks. Only the demo board: no one's own designs."""
import asyncio, os, sys, json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ui_tests import Server, Ctx, BV  # noqa: E402
from cdp import Chrome  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
            calm = ("(() => { document.querySelectorAll('.toast, .vtip').forEach((e) => e.remove()); "
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

            await page.js(f"{BV}.setPanel('layers'); 1")
            await page.js("[...document.querySelectorAll('.lrow')].find((r) => r.textContent.includes('Routing space')).click(); 1")
            await page.wait("document.querySelector('.sp-legend')", 30)
            await page.js(f"(() => {{ const b = {BV}, f = b.byRef.U2, p = f.pads.find((q) => String(q.n) === '1'); "
                          f"const [px, py] = b.toScreen(p.x, p.y); b.whyAt(px, py, p.x, p.y); return 1; }})()")
            await page.wait(f"{BV}.tip.style.display === 'block'", 15)
            await asyncio.sleep(0.4)
            await page.shot(os.path.join(out, "space.png"))
            print("  space")
            await page.js("[...document.querySelectorAll('.lrow')].find((r) => r.textContent.includes('Routing space')).click(); 1")

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
                          "const i = document.querySelectorAll('.sm-ctl input'); i[0].value = '0.3'; i[0].dispatchEvent(new Event('input')); return 1; })()")
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
            await page.wait("document.querySelector('.sch-note')", 30)
            await page.js("document.querySelector('.sch-note').dispatchEvent(new MouseEvent('click', {bubbles: true})); 1")
            await asyncio.sleep(1.0)
            await snap("schematic")

            await t.place("checks", "checks")
            await page.wait("document.querySelector('.view.on')", 10)
            await page.js("[...document.querySelectorAll('.vbtns button')].find((b) => b.textContent.includes('Run all checks')).click(); 1")
            await page.wait("document.querySelector('.checks-head .vt') && !document.querySelector('.checks-head .vt').textContent.includes('Running') "
                            "&& !document.querySelector('.checks-head .vt').textContent.includes('Not checked')", 300)
            await asyncio.sleep(2.0)
            await snap("checks")
    finally:
        s.stop()


if __name__ == "__main__":
    out = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "docs", "images"))
    os.makedirs(out, exist_ok=True)
    asyncio.run(shoot(out))
