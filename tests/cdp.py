"""A small Chrome DevTools Protocol client for the browser tests (tests/ui_tests.py): headless Chrome with
its own throwaway profile, pages driven over the DevTools WebSocket, and every console error and
uncaught exception collected so a test can fail on them."""
import asyncio, base64, json, os, shutil, socket, subprocess, tempfile, time

import aiohttp

CHROMES = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
           "/Applications/Chromium.app/Contents/MacOS/Chromium",
           "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
           "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium", "/usr/bin/chromium-browser"]


def find_chrome():
    """Chrome, Chromium or Edge: TW_CHROME if set, else the usual places."""
    for p in [os.environ.get("TW_CHROME")] + CHROMES + [shutil.which(n) for n in ("google-chrome", "chromium", "chrome")]:
        if p and os.path.exists(p):
            return p
    return None


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Chrome:
    """Headless Chrome on a free debugging port.  async with Chrome() as b: page = await b.page()"""

    def __init__(self, path=None, width=1440, height=900, scale=1):
        self.path = path or find_chrome()
        if not self.path:
            raise RuntimeError("no Chrome found (set TW_CHROME)")
        self.width, self.height, self.scale = width, height, scale
        self.port = free_port()
        self.profile = tempfile.mkdtemp(prefix="tw-chrome-")
        self.proc = None
        self.http = None

    async def __aenter__(self):
        self.proc = subprocess.Popen(
            [self.path, "--headless=new", f"--remote-debugging-port={self.port}", f"--user-data-dir={self.profile}",
             "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--disable-background-networking",
             "--disable-component-update", "--disable-sync", "--mute-audio", "--hide-scrollbars",
             f"--window-size={self.width},{self.height}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.http = aiohttp.ClientSession()
        t0 = time.time()
        while True:
            try:
                async with self.http.get(f"http://127.0.0.1:{self.port}/json/version") as r:
                    if r.status == 200:
                        break
            except aiohttp.ClientError:
                pass
            if time.time() - t0 > 20 or self.proc.poll() is not None:
                await self.__aexit__()
                raise RuntimeError("Chrome did not start")
            await asyncio.sleep(0.15)
        return self

    async def __aexit__(self, *exc):
        if self.http:
            await self.http.close()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        shutil.rmtree(self.profile, ignore_errors=True)

    async def page(self):
        async with self.http.put(f"http://127.0.0.1:{self.port}/json/new?about:blank") as r:
            t = await r.json()
        p = Page(self, t)
        await p.open()
        return p


class Page:
    def __init__(self, browser, target):
        self.browser, self.target = browser, target
        self.ws = None
        self.n = 0
        self.waiting = {}
        self.errors = []          # console errors and uncaught exceptions, as text
        self.logs = []            # every console message: (type, text)
        self.reader = None
        self.held = None          # requests held by intercept()

    async def open(self):
        self.ws = await self.browser.http.ws_connect(self.target["webSocketDebuggerUrl"], max_msg_size=0)
        self.reader = asyncio.ensure_future(self._read())
        for m in ("Page.enable", "Runtime.enable", "Log.enable"):
            await self.call(m)
        b = self.browser
        await self.call("Emulation.setDeviceMetricsOverride", width=b.width, height=b.height, deviceScaleFactor=b.scale, mobile=False)
        await self.call("Emulation.setEmulatedMedia", features=[{"name": "prefers-reduced-motion", "value": "reduce"}])

    async def _read(self):
        async for msg in self.ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            d = json.loads(msg.data)
            if "id" in d:
                f = self.waiting.pop(d["id"], None)
                if f and not f.done():
                    f.set_result(d)
                continue
            m, p = d.get("method"), d.get("params", {})
            if m == "Runtime.consoleAPICalled":
                text = " ".join(str(a.get("value", a.get("description", ""))) for a in p.get("args", []))
                self.logs.append((p.get("type"), text))
                if p.get("type") == "error":
                    self.errors.append(text)
            elif m == "Runtime.exceptionThrown":
                e = p.get("exceptionDetails", {})
                self.errors.append((e.get("exception") or {}).get("description") or e.get("text", "exception"))
            elif m == "Fetch.requestPaused" and self.held is not None:
                self.held.put_nowait(p)
            elif m == "Log.entryAdded" and p.get("entry", {}).get("level") == "error":
                en = p["entry"]
                self.errors.append(f"{en.get('text', '')} {en.get('url', '')}".strip())

    async def call(self, method, timeout=30, **params):
        self.n += 1
        me = self.n
        fut = asyncio.get_event_loop().create_future()
        self.waiting[me] = fut
        await self.ws.send_json({"id": me, "method": method, "params": params})
        d = await asyncio.wait_for(fut, timeout)
        if "error" in d:
            raise RuntimeError(f"{method}: {d['error'].get('message')}")
        return d.get("result", {})

    async def goto(self, url, ready="document.readyState === 'complete'", timeout=30):
        await self.call("Page.navigate", url=url)
        await asyncio.sleep(0.2)
        return await self.wait(ready, timeout)

    async def js(self, expr, timeout=30):
        """The value of a JavaScript expression (promises awaited); raises on a thrown error."""
        r = await self.call("Runtime.evaluate", timeout=timeout, expression=expr, awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in r:
            e = r["exceptionDetails"]
            raise RuntimeError(f"JS error: {(e.get('exception') or {}).get('description') or e.get('text')}")
        return r.get("result", {}).get("value")

    async def wait(self, expr, timeout=20, interval=0.1):
        """Polls until the expression is truthy; returns its value (raises TimeoutError with the expression)."""
        t0 = time.time()
        last = None
        while time.time() - t0 < timeout:
            try:
                r = await self.js(f"""(() => {{ try {{ const v = ({expr});
                    return {{ ok: !!v, v: v instanceof Node ? true : v }}; }} catch (e) {{ return {{ ok: false, v: String(e) }}; }} }})()""")
                if r and r.get("ok"):
                    return r.get("v")
                last = r and r.get("v")
            except RuntimeError as e:
                last = str(e)
            await asyncio.sleep(interval)
        raise TimeoutError(f"waited {timeout}s for: {expr} (last: {last!r})")

    async def click(self, selector, index=0):
        """A real mouse click at the centre of the element (scrolled into view)."""
        box = await self.js(f"""(() => {{ const el = document.querySelectorAll({json.dumps(selector)})[{index}];
            if (!el) return null; el.scrollIntoView({{block: 'center'}}); const r = el.getBoundingClientRect();
            return [r.x + r.width / 2, r.y + r.height / 2]; }})()""")
        if not box:
            raise RuntimeError(f"no element {selector!r}")
        await self.mouse(*box)
        return box

    async def mouse(self, x, y, button="left", clicks=1):
        await self.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
        await self.call("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button=button, clickCount=clicks)
        await self.call("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button=button, clickCount=clicks)

    async def wheel(self, x, y, dy):
        await self.call("Input.dispatchMouseEvent", type="mouseWheel", x=x, y=y, deltaX=0, deltaY=dy)

    async def key(self, key, code=None, modifiers=0, text=None):
        """modifiers: 1 Alt, 2 Ctrl, 4 Meta, 8 Shift."""
        k = dict(key=key, code=code or key, modifiers=modifiers)
        if text is not None:
            k["text"] = text
        await self.call("Input.dispatchKeyEvent", type="keyDown", **k)
        await self.call("Input.dispatchKeyEvent", type="keyUp", key=key, code=code or key, modifiers=modifiers)

    async def type(self, text):
        await self.call("Input.insertText", text=text)

    async def shot(self, path, selector=None):
        """A PNG of the page (or of one element)."""
        params = {"format": "png"}
        if selector:
            r = await self.js(f"""(() => {{ const el = document.querySelector({json.dumps(selector)}); if (!el) return null;
                const b = el.getBoundingClientRect(); return {{x: b.x, y: b.y, width: b.width, height: b.height}}; }})()""")
            if r:
                params["clip"] = dict(r, scale=1)
        d = await self.call("Page.captureScreenshot", **params)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "wb") as f:
            f.write(base64.b64decode(d["data"]))
        return path

    # ---------------------------------------------------------------- holding requests back
    async def intercept(self, url_pattern):
        """Hold the requests whose URL matches the pattern (* wildcards); take them with held_request(), then
        release each with resume() or answer it with fulfill()."""
        self.held = asyncio.Queue()
        await self.call("Fetch.enable", patterns=[{"urlPattern": url_pattern, "requestStage": "Request"}])

    async def held_request(self, timeout=15):
        return await asyncio.wait_for(self.held.get(), timeout)

    async def resume(self, req):
        await self.call("Fetch.continueRequest", requestId=req["requestId"])

    async def fulfill(self, req, body, status=200, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        await self.call("Fetch.fulfillRequest", requestId=req["requestId"], responseCode=status,
                        responseHeaders=[{"name": "Content-Type", "value": ctype}], body=base64.b64encode(data).decode())

    async def stop_intercept(self):
        await self.call("Fetch.disable")
        self.held = None

    async def close(self):
        try:
            await self.call("Page.close", timeout=5)
        except Exception:
            pass
        if self.reader:
            self.reader.cancel()
        if self.ws:
            await self.ws.close()
