"""Records the live operator console with narration-synchronised actions.

A script is a list of items:
  {"chap": ("4", "Detector fault")}                    chapter chip
  {"t": "sentence", "do": [(delay_s, js), ...]}       subtitle + narration; actions relative to the sentence start
  {"js": "..."}                                        run immediately (recorded)
  {"pause": s}                                         keep recording without speech
  {"cut": "js predicate", "timeout": s, "pre": [js]}   stop recording, run pre, wait for predicate, resume
                                                       (a "+N s" badge marks the skipped time)
Output: frames/*.jpg with wall-clock timestamps, chunks (recorded intervals) and the narration
timeline, in timeline.json; assemble.py turns it into the MP4.
"""
import asyncio, json, os, sys, time
sys.path.insert(0, os.path.dirname(__file__))
from cdp import Browser

GAP = 0.45      # silence between sentences


async def run(script, durations, wavs, out, url):
    os.makedirs(out, exist_ok=True)
    b = Browser()
    await b.start()
    await b.call("Page.navigate", url=url)
    await asyncio.sleep(6)
    await b.js(open(os.path.join(os.path.dirname(__file__), "overlay.js")).read())
    chunks, speech, log = [], [], []
    rec_t0 = None

    async def rec_on():
        nonlocal rec_t0
        await b.rec_start(os.path.join(out, "frames"))
        rec_t0 = time.time()

    async def rec_off():
        await b.rec_stop()
        chunks.append((rec_t0, time.time()))

    async def act(js, delay):
        await asyncio.sleep(delay)
        r = await b.js(js)
        if r is False:
            print("  action returned false:", js[:90], flush=True)

    await rec_on()
    for it in script:
        if "chap" in it:
            await b.js(f"__ov.chap({json.dumps(it['chap'][0])}, {json.dumps(it['chap'][1])})")
        if "js" in it:
            await b.js(it["js"])
        if "pause" in it:
            await asyncio.sleep(it["pause"])
        if "cut" in it:
            await b.js("__ov.sub('')")
            await rec_off()
            for js in it.get("pre", []):
                await b.js(js)
            t0 = time.time()
            ok = False
            while time.time() - t0 < it.get("timeout", 90):
                if await b.js(it["cut"]):
                    ok = True
                    break
                await asyncio.sleep(0.5)
            await asyncio.sleep(it.get("settle", 0.8))
            waited = time.time() - t0
            print(f"  cut: {'ok' if ok else 'TIMEOUT'} after {waited:.1f} s  [{it['cut'][:70]}]", flush=True)
            log.append(dict(cut=it["cut"], ok=ok, waited=round(waited, 1)))
            for js in it.get("post", []):
                await b.js(js)
            await rec_on()
            if it.get("badge", True):
                await b.js(f"__ov.skip('⏩ {round(waited)} s later')")
        if "t" in it:
            d = durations[it["t"]]
            await b.js(f"__ov.sub({json.dumps(it.get('sub', it['t']))})")
            speech.append(dict(t=time.time(), wav=wavs[it["t"]], text=it.get("sub", it["t"]), dur=d))
            tasks = [asyncio.create_task(act(js, dl)) for dl, js in it.get("do", [])]
            await asyncio.sleep(max(d, it.get("min", 0)) + GAP)
            for tk in tasks:
                await tk
    await asyncio.sleep(0.6)
    await rec_off()
    frames = [(t, os.path.basename(f)) for t, f in b.frames]
    json.dump(dict(chunks=chunks, speech=speech, frames=frames, log=log), open(os.path.join(out, "timeline.json"), "w"))
    b.close()
    print(f"recorded {len(frames)} frames in {len(chunks)} chunk(s), "
          f"{sum(e - s for s, e in chunks):.1f} s of video", flush=True)
