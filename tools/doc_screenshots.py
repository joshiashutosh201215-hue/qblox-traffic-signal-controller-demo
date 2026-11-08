"""Screenshots for docs/GUIDE.md, taken from the live system (headless Chrome over the DevTools protocol).

    ./demo.sh reset && ./demo.sh start     # wait ~2 min until all 20 controllers are ACTUATED
    python3 tools/doc_screenshots.py       # ~6 min; writes docs/img/*.jpg; leaves release v2 deployed at I17

Yellow rings mark what the guide talks about. Keep the host otherwise idle (see docs/GUIDE.md, "Platform").
"""
import asyncio, base64, json, os, sys, time
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "media", "tools"))
from cdp import Browser

OUT = os.path.join(ROOT, "docs", "img")
W, H = 1600, 900
F = lambda kind, t, v="null": f"fault('{kind}', {t}, {v})"


async def main(only=None):
    os.makedirs(OUT, exist_ok=True)
    b = Browser(W, H, port=9336)
    await b.start()
    await b.call("Page.navigate", url="http://127.0.0.1:8080/?sel=17#city")
    await asyncio.sleep(6)
    await b.js(open(os.path.join(ROOT, "media", "tools", "overlay.js")).read())
    await b.js("document.querySelector('#__chap').remove(), document.querySelector('#__sub').remove(), true")

    async def js(x, wait=0.0):
        r = await b.js(x)
        if wait:
            await asyncio.sleep(wait)
        return r

    async def until(pred, timeout=90):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if await b.js(pred):
                return True
            await asyncio.sleep(0.5)
        print("  TIMEOUT:", pred[:80])
        return False

    async def shot(name, clip_sel=None, pad=0):
        if only and name not in only:
            return
        await asyncio.sleep(0.4)
        p = dict(format="jpeg", quality=86)
        if clip_sel:
            r = await b.js(f"(() => {{ const r = document.querySelector({json.dumps(clip_sel)}).getBoundingClientRect(); "
                           f"return [r.left, r.top, r.width, r.height]; }})()")
            p["clip"] = dict(x=max(0, r[0] - pad), y=max(0, r[1] - pad), width=r[2] + 2 * pad, height=min(r[3] + 2 * pad, H - r[1] + pad), scale=1)
        r = await b.call("Page.captureScreenshot", **p)
        open(os.path.join(OUT, name + ".jpg"), "wb").write(base64.b64decode(r["data"]))
        print("  shot", name, flush=True)

    hl = lambda sel, ms=60000, pad=6: f"__ov.hl({sel}, {ms}, {pad})"
    clear = "__ov.clear()"
    seq = lambda *xs: "(" + ", ".join(xs) + ", true)"
    cab = lambda title: f"__ov.cabBox({json.dumps(title)})"

    # ---- the console
    await js("showTab('demo')", 1)
    await shot("demo_script")
    await js("(showTab('city'), selectTsc(17), true)", 3)
    await shot("city_overview")
    await js(hl("'#zoom'"), 1.0)
    await shot("intersection_closeup", ".zoomcol", 4)
    await js(seq(clear))
    await shot("cabinet_top", "#right")
    await js("(document.querySelector('#right').scrollTop = 620, true)", 1)
    await shot("cabinet_bottom", "#right")
    await js("(document.querySelector('#right').scrollTop = 0, true)")

    # ---- decisions and audit
    await js("showTab('audit')", 1)
    await js("(document.querySelector('#a-tsc').value = 13, loadRecords().then(() => true))", 1.5)
    await js("(([...document.querySelectorAll('#a-list .rec')].find(e => e.textContent.includes('R13-02')) || document.querySelector('#a-list .rec')).click(), true)", 2)
    await js("(document.querySelector('#a-trace details').open = true, true)", 0.5)
    await shot("audit_trace")

    # ---- time, monitor, logs
    await js("(S.sel = 13, showTab('time'), true)", 3)
    await js(hl("'#t-table tr.sel'", 60000, 2), 0.8)
    await shot("time_sync")
    await js(seq(clear))
    await js("(S.sel = 17, showTab('monitor'), true)", 2)
    await shot("monitor")
    await js("(showTab('logs'), subLog('tsc17.log'), true)", 5)
    await shot("logs")

    # ---- rules: edit, stored text, validate, replay, release, deploy
    await js("showTab('rules')", 2)
    await shot("rules_table")
    await js("(__ov.setRule('R17-01', 'input[data-c=value]', 20), true)", 1)
    await js("(__ov.setRule('R17-01', 'input[data-k=action_s]', 5), true)", 1)
    await js("(document.querySelector('#r-msg').value = 'I17: react earlier to stop-bar queues', document.querySelector('#r-save').click(), true)", 2.5)
    await js("(document.querySelector('#r-text').classList.remove('hidden'), document.querySelector('#r-result').scrollIntoView(), true)", 1)
    await shot("rules_commit")
    await js("(document.querySelector('#r-text').classList.add('hidden'), document.querySelector('#v-run').click(), true)", 3)
    await js("(document.querySelector('#rp-run').click(), document.querySelector('#v-out').scrollIntoView(), true)", 3)
    await shot("validate_replay")
    await js("(document.querySelector('#rel-approver').value = 'a.joshi', document.querySelector('#rel-note').value = 'React earlier to stop-bar queues at I17', document.querySelector('#rel-run').click(), true)", 2)
    await js("(document.querySelector('#rel-run').scrollIntoView({block: 'center'}), true)", 0.5)
    await shot("release_refused", ".card:has(#rel-run)", 4)
    await js("(document.querySelector('#rel-approver').value = 'r.vermeer', document.querySelector('#rel-run').click(), true)", 4)
    await shot("release_signed", ".card:has(#rel-run)", 4)
    await js("showTab('deploy')", 1.5)
    await js("(document.querySelector('#d-rel button[data-tg=changed]').click(), true)", 2)
    await until("!!(S.deployments.length && S.deployments.at(-1).phase === 'complete')", 150)
    await js("refreshDeploy().then(() => true)", 1.5)
    await js(hl("[...document.querySelectorAll('#d-grid .tile')].find(e => e.textContent.startsWith('I17'))"), 1)
    await shot("deploy")
    await js(seq(clear))

    # ---- faults
    await js("(showTab('city'), selectTsc(17), true)", 2)
    await js(F("det_open", 17, 1), 3)
    await js("(__ov.scrollTo('#cab .leds', 'center'), true)", 1)
    await js(hl(cab("Detector cards")), 0.5)
    await shot("fault_detector")
    await js(seq(clear, F("det_repair", 17)), 1)

    await js("(document.querySelector('#right').scrollTop = 0, " + F("tsc_power", 16, 0) + ", true)")
    await until("!!(S.probes[17] && S.probes[17].neighbours.some(n => n.tsc === 16 && n.live === 'SILENT') && S.probes[17].mode === 'DEGRADED')", 100)
    await js("(__ov.hlTsc(16, 60000), __ov.hlTsc(17, 60000), __ov.scrollTo('#cab table', 'center'), true)", 1)
    await js(hl("'#cab table'"), 0.6)
    await shot("fault_upstream")
    await js(seq(clear, F("tsc_power", 16, 1)))

    await js("(selectTsc(9), document.querySelector('#right').scrollTop = 0, " + F("bug_conflict", 9, 1) + ", true)")
    await until("!!(S.world && S.world.field[9] && S.world.field[9].state === 2)", 90)
    await asyncio.sleep(1.5)
    await js("(__ov.hlTsc(9, 60000), true)")
    await js(hl(cab("Signal heads")), 0.6)
    await shot("fault_safety_unit")
    await js(seq(clear, F("bug_conflict", 9, 0), F("mmu_reset", 9), F("mmu_reset", 16)), 1)

    await js("(selectTsc(17), " + F("forge", 16) + ", true)", 1.2)
    await js(F("replay", 16), 1.6)
    await js("(__ov.scrollTo('#cab table', 'center'), true)", 0.3)
    await js(hl(cab("Peer network")), 0.4)
    await shot("fault_security")
    await js(seq(clear))

    await js("(document.querySelector('#right').scrollTop = 0, " + F("center_link", 0, 0) + ", true)")
    await until("document.querySelector('#pill-mqtt').textContent.includes(' 0/20')", 40)
    await js("showTab('monitor')", 2)
    await js(hl("'#pill-mqtt'"), 0.6)
    await shot("fault_center_link")
    await js(seq(clear, F("center_link", 0, 1)))

    await js("(S.sel = 12, showTab('time'), " + F("gnss", 12, 0) + ", " + F("ptp", 12, 0) + ", true)")
    await until("!!(S.probes[12] && S.probes[12].sync.q === 'HOLDOVER')", 30)
    await js(F("spoof", 7, 800), 6)
    await js(hl("[...document.querySelectorAll('#t-table tr')].find(e => e.firstChild && e.firstChild.textContent === 'I12')", 60000, 2), 0.2)
    await js(hl("[...document.querySelectorAll('#t-table tr')].find(e => e.firstChild && e.firstChild.textContent === 'I7')", 60000, 2), 0.6)
    await shot("fault_time")
    await js(seq(clear, F("spoof", 7, 0), F("gnss", 12, 1), F("ptp", 12, 1)))

    # ---- help tab (processes)
    await js("showTab('help')", 3)
    await shot("help_processes")
    b.close()


if __name__ == "__main__":
    asyncio.run(main(set(sys.argv[1:]) or None))
