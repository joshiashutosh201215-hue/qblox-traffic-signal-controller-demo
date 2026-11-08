"""End-to-end fault-injection test against the running demo (./demo.sh start first).

Drives the HIL testbench over its WebSocket and the control center over REST, and
checks that every behaviour the design claims actually happens. Takes ~6-9 minutes.
"""
import asyncio
import json
import sys
import time
import urllib.request

from websockets.asyncio.client import connect

CC = "http://127.0.0.1:8080"
RESULTS = []


def http(method, path, body=None):
    req = urllib.request.Request(CC + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


class Bench:
    def __init__(self):
        self.probes, self.field, self.events = {}, {}, []

    async def run(self):
        self.ws = await connect("ws://127.0.0.1:8091", max_size=2 ** 24)
        asyncio.ensure_future(self.reader())

    async def reader(self):
        async for raw in self.ws:
            m = json.loads(raw)
            if m["type"] == "probes":
                self.probes = {int(k): v for k, v in m["probes"].items()}
            elif m["type"] == "world":
                self.field = {int(k): v for k, v in m["field"].items()}
            elif m["type"] == "bench_event":
                self.events.append(m["text"])

    async def fault(self, kind, tsc=0, value=None):
        await self.ws.send(json.dumps(dict(cmd="fault", kind=kind, tsc=tsc, value=value)))

    def p(self, t):
        return self.probes.get(t, {})


async def check(name, pred, timeout, poll=0.5):
    t0 = time.time()
    last_err = ""
    while time.time() - t0 < timeout:
        try:
            r = pred()
            if r:
                el = time.time() - t0
                RESULTS.append((True, name, f"{el:.1f} s" + (f" ({r})" if isinstance(r, str) else "")))
                print(f"  PASS  {name}  [{el:.1f} s]" + (f"  {r}" if isinstance(r, str) else ""), flush=True)
                return True
        except Exception as e:  # data not there yet
            last_err = repr(e)
        await asyncio.sleep(poll)
    RESULTS.append((False, name, f"timeout {timeout} s {last_err}"))
    print(f"  FAIL  {name}  [timeout {timeout} s] {last_err}", flush=True)
    return False


def records(t, n=8):
    return http("GET", f"/api/records?tsc={t}&limit={n}")


async def main():
    b = Bench()
    await b.run()
    await asyncio.sleep(2)
    print("Baseline")
    await check("20 controllers running and reporting", lambda: len([p for p in b.probes.values() if time.time() - p["rx"] < 2]) == 20, 30)
    await check("all 20 controllers synchronized (GNSS or PTP)", lambda: all(b.p(t)["sync"]["q"] in ("GNSS", "PTP") for t in range(1, 21)), 30)
    await check("cabinets without sky view (I8, I13, I14) use PTP", lambda: all(b.p(t)["sync"]["q"] == "PTP" for t in (8, 13, 14)), 20)
    await check("actual clock error < 5 ms everywhere (target 100 ms)",
                lambda: f"max {max(abs(b.p(t)['truth_offset_ms']) for t in range(1, 21)):.3f} ms"
                if max(abs(b.p(t)["truth_offset_ms"]) for t in range(1, 21)) < 5 else False, 30)
    await check("no C++ heap allocation after start-up", lambda: all(b.p(t)["heap_allocs_after_init"] == 0 for t in range(1, 21)), 5)
    await check("20/20 online at the control center (MQTT mTLS)", lambda: sum(s == "online" for s in http("GET", "/api/state")["status"].values()) == 20, 60)

    print("Inject faults")
    await b.fault("det_open", 17, 1)
    await b.fault("card_off", 4, 0)
    await b.fault("gnss", 12, 0)
    await b.fault("spoof", 7, 800)
    await b.fault("bug_conflict", 9, 1)
    await b.fault("tsc_power", 16, 0)
    await b.fault("center_link", 20, 0)
    await b.fault("forge", 16)
    t_inj = time.time()
    await check("I9 software defect -> safety unit FLASH at its next main green (conflict)", lambda: b.field[9]["state"] == 2 and "CONFLICT" in b.field[9]["fault"] and b.field[9]["fault"], 45, 0.1)
    await check("I16 power loss -> its safety unit FLASH (watchdog)", lambda: b.field[16]["state"] == 2 and b.field[16]["fault"], 6, 0.1)
    await check("I17 D1 loop cut -> LOOP_FAULT via CANopen EMCY", lambda: b.p(17)["det"][0]["q"] == "LOOP_FAULT", 5, 0.2)
    await check("I4 detector card A off -> D1-D4 SILENT (heartbeat timeout)", lambda: all(d["q"] == "SILENT" for d in b.p(4)["det"][:4]), 6, 0.2)
    await check("I12 GNSS lost -> PTP from a neighbour", lambda: b.p(12)["sync"]["q"] == "PTP" and f"master I{b.p(12)['sync']['master']}", 10)
    await check("I7 GNSS spoof +800 ms rejected (peer disagrees), clock kept",
                lambda: b.p(7)["sync"]["gnss_rejected"] and abs(b.p(7)["truth_offset_ms"]) < 5 and f"error {b.p(7)['truth_offset_ms']:.3f} ms", 15)
    await check("I17 forged message on channel 16 rejected (AES-CMAC)", lambda: b.p(17)["security"]["mac_fail"] >= 1, 10)
    await b.fault("replay", 16)
    await check("I17 replayed message rejected (sequence)", lambda: b.p(17)["security"]["replay"] >= 1, 10)
    await check("I20 center link down -> control center sees it offline (LWT)", lambda: http("GET", "/api/state")["status"]["20"] == "offline", 20)
    await check("I17 detects silent upstream I16 locally within one cycle",
                lambda: [n for n in b.p(17)["neighbours"] if n["tsc"] == 16][0]["live"] == "SILENT" and f"{time.time() - t_inj:.0f} s after the crash", 95)
    await check("I17 record: R17-01 on fallback (D1), R17-02 on fallback (I16 silent)",
                lambda: any([r for r in records(17) if {x["id"]: x["status"] for x in r["rules"]}.get("R17-01") == "MISSING"
                             and {x["id"]: x["status"] for x in r["rules"]}.get("R17-02") == "MISSING" and r["t_ns"] / 1e9 > t_inj]), 100, 2)
    await check("I4 with 4 stop-bar detectors silent -> local FIXED_TIME plan",
                lambda: any(r["mode"] == "FIXED_TIME" and r["t_ns"] / 1e9 > t_inj for r in records(4)), 70, 2)
    await check("I20 keeps deciding without the center (records buffered on flash)", lambda: b.p(20)["records"]["backlog"] >= 1 and f"backlog {b.p(20)['records']['backlog']}", 60)

    print("Restore")
    await b.fault("tsc_power", 16, 1)
    t_on = time.time()
    for k, t, v in (("det_repair", 17, None), ("det_repair", 4, None), ("gnss", 12, 1), ("spoof", 7, 0),
                    ("bug_conflict", 9, 0), ("center_link", 20, 1)):
        await b.fault(k, t, v)
    await asyncio.sleep(1)
    await b.fault("mmu_reset", 9)
    await check("I16 powered on and booted from local flash", lambda: b.p(16)["uptime_s"] < time.time() - t_on + 5 and time.time() - b.p(16)["rx"] < 2, 20)
    await b.fault("mmu_reset", 16)
    await check("I9 back to normal after technician reset", lambda: b.field[9]["state"] == 0, 10)
    await check("I16 back to normal after technician reset", lambda: b.field[16]["state"] == 0, 10)
    await check("I12 back on GNSS", lambda: b.p(12)["sync"]["q"] == "GNSS", 10)
    await check("I20 reconnects and uploads its backlog",
                lambda: http("GET", "/api/state")["status"]["20"] == "online" and b.p(20)["records"]["backlog"] == 0, 60)
    await check("I20 buffered records arrived with their original timestamps",
                lambda: any(r["_rx_delay_s"] > 15 for r in records(20, 10)) and
                f"max upload delay {max(r['_rx_delay_s'] for r in records(20, 10)):.0f} s", 20, 2)

    print("Proposal behaviour injected for comparison (checked at the end)")
    await b.fault("legacy", 18, 1)
    await b.fault("buffer_n", 13, 4)
    t_prop = time.time()

    print("Rule change -> validate -> release -> deploy")
    rs = http("GET", "/api/rules/17")
    rules = rs["rules"]
    for r in rules:
        if r["id"] == "R17-02":
            r["action_s"] = 7.0 if r["action_s"] != 7.0 else 6.5
    put = http("PUT", "/api/rules/17", dict(author="k.smit", message="I17: longer platoon extension (self-test)", rules=rules))
    await check("rule change committed to Git", lambda: put["changed"] and f"commit {put['commit']}", 1)
    rep = http("POST", "/api/validate")
    await check("validation passes for all 20 controllers", lambda: rep["ok"], 1)
    try:
        http("POST", "/api/release", dict(author="k.smit", approver="k.smit", note="self-test"))
        ok = False
    except Exception:
        ok = True
    await check("release refused without a second engineer (two-person rule)", lambda: ok, 1)
    rel = http("POST", "/api/release", dict(author="k.smit", approver="r.vermeer", note="self-test"))
    await check("release compiled + Ed25519 signed", lambda: 17 in rel["changed"] and f"v{rel['version']} changed {rel['changed']}", 1)
    dep = http("POST", "/api/deploy", dict(version=rel["version"], targets="changed"))
    await check("deployment staged, confirmed and activated at a cycle boundary",
                lambda: b.p(17)["cfg"]["ver"] == rel["version"] and f"I17 now v{rel['version']} slot {chr(b.p(17)['cfg']['slot'])}", 160, 1)
    await check("deployment record complete at the control center",
                lambda: [d for d in http("GET", "/api/state")["deployments"] if d["version"] == rel["version"]][-1]["phase"] == "complete", 60, 1)

    print("Integrity")
    allr = http("GET", "/api/records?limit=500")
    await check("all uploaded decision records hash-verified", lambda: all(r["_hash_ok"] for r in allr) and f"{len(allr)} records", 1)
    await check("hash chains intact where both records were received", lambda: all(r["_chain_ok"] in (None, 1) for r in allr), 1)
    design = [r for r in allr if not (r["tsc"] in (13, 18) and r["t_ns"] / 1e9 > t_prop)]
    await check("every decision under the design met its deadline (2 s local / 5 s upstream)",
                lambda: all(r["decision"].get("deadline_met", True) for r in design) and f"{len(design)} decisions", 1)

    print("The proposal, side by side")
    await check("I18 proposal mode: upstream rule fed a previous-cycle reading (audit flags it)",
                lambda: [f"I{i['src']} D{i['det']} reading {i['age_s']:.0f} s old used" for r in records(18) if r["t_ns"] / 1e9 > t_prop
                         and r["cfg"]["legacy_prev_cycle"] for i in r["inputs"] if i["src"] != 18 and i["status"] == "OK" and i["age_s"] > 30][0], 120, 2)
    await check("I13 buffer emptied every 4 cycles: 3 x 4 = 12 > 10 -> loss detected only via sequence gaps",
                lambda: [a["text"][:90] for a in http("GET", "/api/state")["alarms"] if a["tsc"] == 13 and a["code"] == "MSG_LOSS"
                         and a["t_ns"] / 1e9 > t_prop][0], 300, 3)
    await check("the proposal's behaviour misses deadlines, as the review predicted",
                lambda: [f"{r['rec']}: {r['decision']['latency_s']} s after the last usable reading (limit {r['decision']['deadline_s']} s)"
                         for r in http("GET", "/api/records?limit=500") if r["tsc"] in (13, 18) and r["t_ns"] / 1e9 > t_prop
                         and not r["decision"].get("deadline_met", True)][0], 30, 2)
    await b.fault("legacy", 18, 0)
    await b.fault("buffer_n", 13, 1)

    n_ok = sum(r[0] for r in RESULTS)
    print(f"\n{n_ok}/{len(RESULTS)} checks passed")
    with open("runtime/logs/selftest_report.txt", "w") as f:
        f.write(f"self-test {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}: {n_ok}/{len(RESULTS)} passed\n")
        for ok, name, detail in RESULTS:
            f.write(f"{'PASS' if ok else 'FAIL'}  {name}  [{detail}]\n")
    return 0 if n_ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
