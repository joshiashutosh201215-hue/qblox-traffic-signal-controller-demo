"""Minimal CDP driver: headless Chrome, screencast frames to disk with wall-clock timestamps."""
import asyncio, base64, json, subprocess, time, urllib.request, os
from websockets.asyncio.client import connect

class Browser:
    def __init__(self, w=1920, h=1080, port=9334):
        self.w, self.h, self.port = w, h, port
        self.p = subprocess.Popen(["google-chrome", "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
                                   f"--remote-debugging-port={port}", f"--window-size={w},{h}", "about:blank"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.n = 0; self.pending = {}; self.rec = None; self.frames = []

    async def start(self):
        for _ in range(100):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json")); break
            except Exception: await asyncio.sleep(0.2)
        url = [t for t in tabs if t["type"] == "page"][0]["webSocketDebuggerUrl"]
        self.ws = await connect(url, max_size=2**27)
        self.reader = asyncio.create_task(self._read())
        await self.call("Page.enable"); await self.call("Runtime.enable")
        await self.call("Emulation.setDeviceMetricsOverride", width=self.w, height=self.h, deviceScaleFactor=1, mobile=False)

    async def _read(self):
        async for raw in self.ws:
            m = json.loads(raw)
            if "id" in m and m["id"] in self.pending:
                self.pending.pop(m["id"]).set_result(m)
            elif m.get("method") == "Page.screencastFrame":
                pr = m["params"]
                asyncio.create_task(self.call("Page.screencastFrameAck", sessionId=pr["sessionId"]))
                if self.rec is not None:
                    fn = os.path.join(self.rec, f"f{len(self.frames):06d}.jpg")
                    open(fn, "wb").write(base64.b64decode(pr["data"]))
                    self.frames.append((time.time(), fn))
            elif m.get("method") == "Runtime.exceptionThrown":
                print("JS EXC", m["params"]["exceptionDetails"].get("exception", {}).get("description"))

    async def call(self, method, **params):
        self.n += 1; i = self.n
        fut = asyncio.get_running_loop().create_future(); self.pending[i] = fut
        await self.ws.send(json.dumps(dict(id=i, method=method, params=params)))
        r = await fut
        return r.get("result", r.get("error"))

    async def js(self, expr):
        r = await self.call("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
        return (r or {}).get("result", {}).get("value")

    async def rec_start(self, d):
        os.makedirs(d, exist_ok=True); self.rec = d
        await self.call("Page.startScreencast", format="jpeg", quality=82, maxWidth=self.w, maxHeight=self.h, everyNthFrame=1)

    async def rec_stop(self):
        await self.call("Page.stopScreencast"); self.rec = None

    def close(self): self.p.kill()
