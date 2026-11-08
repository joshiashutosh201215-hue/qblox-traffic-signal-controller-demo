"""Video 1: five-minute black-box walkthrough of the live system (recorded from the real GUI).

    python3 media/tools/v1_demo.py      (demo must be running and freshly reset; see media/README.md)
"""
import asyncio, json, os, subprocess, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import director

OUT = os.path.join(HERE, "..", "build", "v1")
F = lambda kind, t, v="null": f"fault('{kind}', {t}, {v})"
TAB = lambda t: f"__ov.click('#tabs button[data-tab={t}]', 400)"
SEL = lambda t: f"(selectTsc({t}), true)"
PROBE = lambda t, expr: f"!!(S.probes[{t}] && Date.now()/1000 - S.probes[{t}].rx < 2 && ({expr}))"

CARD = """<h1>City Traffic Control</h1><h2>Assignment MVP — live demo, black-box view</h2>
<div class="tags"><span>20 C++ intersection controllers</span><span>20 independent safety units</span><span>Linux control center</span>
<span>MQTT over mutual TLS</span><span>Peer Ethernet + AES-CMAC</span><span>GNSS → PTP → TCXO</span><span>CANopen detectors</span>
<span>Rules as data · signed releases</span><span>Hash-chained decision records</span></div>
<div class="foot">Recorded from the running prototype · real processes, real protocols · street and cabinet I/O simulated</div>"""
END = """<h1>What you saw</h1><h2>Every fault was injected live; every reaction came from the real software</h2>
<div class="tags"><span>Local decisions on current, time-stamped data</span><span>Per-rule fallback → fixed time</span>
<span>Safety unit independent of software</span><span>Time: GNSS → PTP → holdover</span><span>Center never in the decision path</span>
<span>Validate · two-person release · signed · coordinated activation</span><span>Every decision traceable to rule, input, commit</span>
<span>Self-test: 37 / 37 checks</span></div>
<div class="foot">Next: code walkthrough — where each part is implemented and why</div>"""

S = [
    {"js": f"(__ov.card({json.dumps(CARD)}), showTab('city'), selectTsc(17), true)"},
    {"chap": ("", "Black-box demo")},
    {"t": "A five-minute black-box tour of the traffic signal control prototype, built from the architecture in the assignment answer."},
    {"t": "Decide locally on current, synchronised data. Plan, validate and observe centrally."},
    {"js": "(__ov.card(null), true)"},
    {"pause": 0.8},

    {"chap": ("1", "The city")},
    {"t": "Twenty intersections, each a real C++ controller process with its own independent safety unit, written in C.",
     "do": [(1.0, "__ov.hl('#world', 3500)")]},
    {"t": "A hardware-in-the-loop bench simulates the street and detectors. Vehicles obey the lamps after the safety unit."},
    {"t": "A shared forty-second cycle, offset per column, gives the green wave on the main road."},
    {"t": "Blue dots are authenticated channel messages on the peer Ethernet: eight detector readings per controller, once per cycle.",
     "do": [(0.5, "__ov.hl('.legend', 3000)")]},
    {"t": "Dashed lines are PTP links where there is no GNSS sky view. All twenty controllers are online at the center, over MQTT with mutual TLS.",
     "do": [(0.3, "(__ov.hlTsc(8), __ov.hlTsc(13), __ov.hlTsc(14), true)"), (4.2, "__ov.hl('#pill-mqtt', 3000)")]},

    {"chap": ("2", "Inside a cabinet")},
    {"t": "Intersection seventeen's cabinet: signal heads, then the controller's mode, phase and cycle position.",
     "do": [(0.2, "(__ov.hlTsc(17), true)"), (1.8, "__ov.hl(__ov.cabBox('Signal heads'), 2500)"), (4.5, "__ov.hl(__ov.cabBox('Intersection controller'), 3500)")]},
    {"t": "It samples at thirteen seconds and decides at fourteen whether to extend green. Zero C++ heap allocations after start-up.",
     "do": [(0.5, "__ov.hl('.cyclebar', 4000, 4)"), (5.0, "__ov.hl([...document.querySelectorAll('#cab .kv span')].find(e => e.textContent.includes('allocations')), 3000, 3)")]},
    {"t": "Two CANopen cards serve eight loops: stop bars, and exits for queue and gridlock detection.",
     "do": [(0.0, "__ov.scrollTo('#cab .leds', 'center')"), (0.6, "__ov.hl(__ov.cabBox('Detector cards'), 4500)")]},
    {"t": "Then time hardware, peer channels with sequence numbers, and the MQTT link with its store-and-forward backlog.",
     "do": [(0.0, "__ov.hl(__ov.cabBox('Time hardware'), 2600)"), (2.6, "__ov.hl(__ov.cabBox('Peer network'), 2600)"), (5.2, "__ov.hl(__ov.cabBox('Center link'), 2800)")]},

    {"chap": ("3", "Decisions and traceability")},
    {"t": "Every decision becomes a hash-chained record, uploaded to the center.",
     "do": [(0.0, TAB("audit")), (1.2, "(document.querySelector('#a-tsc').value = 13, loadRecords().then(() => true))"),
            (3.0, "__ov.click([...document.querySelectorAll('#a-list .rec')].find(e => e.textContent.includes('R13-02')) || document.querySelector('#a-list .rec'), 600)")]},
    {"t": "Intersection thirteen extended green by six seconds: rule R13-02 fired because upstream intersection eight saw a short gap. A platoon is coming.",
     "do": [(0.5, "__ov.hl('#a-trace .expl', 6000)")]},
    {"t": "The trace shows every input with sample time, sequence and age, the deadline, the release, the Git commit, and the intact hash chain.",
     "do": [(0.3, "__ov.hl('#a-trace .row', 3500)")]},
    {"t": "Amber and all-red are fixed, an empty side street is skipped, and a gridlock rule holds instead of extending when the exit is blocked."},

    {"chap": ("4", "Detector fault")},
    {"t": "Faults. Cut loop one at intersection seventeen, and its CANopen card sends an emergency frame.",
     "do": [(0.0, TAB("city")), (0.4, "(__ov.scrollTo('#cab .leds', 'center'), true)"), (2.2, "__ov.click('#cab button[data-f=det_open][data-v=\"1\"]', 600)"),
            (3.6, "__ov.hl(__ov.cabBox('Detector cards'), 4000)")]},
    {"t": "Only the rule using that reading falls back to its if-missing action; the others keep working, and an alarm goes to the center.",
     "do": [(2.5, "__ov.hl('#ticker', 3500, 2)")]},
    {"t": "A failed side-street detector counts as permanent demand.",
     "do": [(1.2, "__ov.click('#cab button[data-f=det_repair]', 500)")]},

    {"chap": ("5", "Upstream controller fails")},
    {"t": "Intersection sixteen loses power. Without a watchdog signal, its safety unit flashes it within a second.",
     "do": [(0.5, F("tsc_power", 16, 0)), (1.0, "(__ov.hlTsc(16, 5000), true)"), (1.2, "__ov.scrollTo(__ov.cabBox('Peer network') ? '#cab table' : '#cab', 'center')")]},
    {"cut": PROBE(17, "S.probes[17].neighbours.some(n => n.tsc === 16 && n.live === 'SILENT') && S.probes[17].mode === 'DEGRADED'"),
     "timeout": 90, "post": ["(__ov.scrollTo('#cab table', 'center'), true)"]},
    {"t": "Seventeen detects the silent neighbour by itself, from the missing messages. No center involved.",
     "do": [(0.3, "__ov.hl('#cab table', 4500)")]},
    {"t": "Its upstream rule falls back to no extension: background timing, degraded mode.",
     "do": [(0.5, "__ov.hl(__ov.cabBox('Intersection controller'), 4000)")]},
    {"t": "Lose its own stop-bar detectors as well, and it runs a full local fixed-time plan.",
     "do": [(1.0, F("card_off", 17, 0)), (1.2, F("card_off", 17, 1)), (1.5, "(__ov.scrollTo('#cab .leds', 'center'), true)")]},
    {"cut": PROBE(17, "S.probes[17].mode === 'FIXED_TIME'"), "timeout": 70},
    {"t": "Fixed time: safe, and still serving every approach.",
     "do": [(0.0, "(__ov.scrollTo('#cab', 'start'), true)"), (0.3, "__ov.hl([...document.querySelectorAll('#cab .chip')].find(e => e.textContent === 'FIXED_TIME'), 4000, 4)")]},
    {"cut": PROBE(16, "true") + " && " + PROBE(17, "S.probes[17].mode !== 'FIXED_TIME'"), "timeout": 80, "badge": False,
     "pre": [F("det_repair", 17), F("tsc_power", 16, 1)],
     "post": ["new Promise(r => setTimeout(() => r(fault('mmu_reset', 16)), 6000))"]},

    {"chap": ("6", "Time synchronisation")},
    {"t": "Time has three tiers: GNSS pulse per second, IEEE 1588 PTP from a neighbour, then holdover on the temperature-compensated crystal.",
     "do": [(0.0, "(S.sel = 12, true)"), (0.2, TAB("time")), (2.5, "__ov.hl('#t-chart', 3500)")]},
    {"t": "GNSS off at intersection twelve: it becomes a PTP slave.",
     "do": [(0.5, F("gnss", 12, 0)), (1.8, "__ov.hl('#t-table tr.sel', 4500, 2)")]},
    {"cut": PROBE(12, "S.probes[12].sync.q === 'PTP'"), "timeout": 30},
    {"t": "PTP off too: holdover. A two ppm crystal stays within a hundred milliseconds for almost fourteen hours.",
     "do": [(0.5, F("ptp", 12, 0)), (3.0, "__ov.hl('#t-table tr.sel', 5000, 2)")]},
    {"t": "A GNSS spoof of eight hundred milliseconds at intersection seven is rejected, because its PTP peer disagrees.",
     "do": [(0.0, "(S.sel = 7, true)"), (0.5, F("spoof", 7, 800)), (3.0, "__ov.hl('#t-table tr.sel', 5000, 2)"), (4.5, "__ov.hl('#ticker', 3000, 2)")]},
    {"t": "The center only estimates clocks, to plus or minus five seconds. It never sets them.",
     "do": [(4.0, F("spoof", 7, 0)), (4.1, F("gnss", 12, 1)), (4.2, F("ptp", 12, 1))]},

    {"chap": ("7", "Control-center link lost")},
    {"t": "Now every link to the control center goes down.",
     "do": [(0.0, "(selectTsc(17), true)"), (0.1, TAB("city")), (1.0, F("center_link", 0, 0)), (1.6, "__ov.hl('#pill-mqtt', 3000)")]},
    {"cut": "document.querySelector('#pill-mqtt').textContent.includes(' 0/20')", "timeout": 40},
    {"t": "The broker publishes each controller's last will. On the street, nothing changes: the center is never in the decision path.",
     "do": [(0.3, "__ov.hl('#pill-mqtt', 3000)"), (3.0, "__ov.hl('#world', 3500)")]},
    {"t": "Records queue on flash, and upload later with their original timestamps.",
     "do": [(0.0, "(__ov.scrollTo(__ov.cabBox('Center link') ? '#cab .box:nth-last-child(2)' : '#cab', 'center'), true)"),
            (0.4, "__ov.hl(__ov.cabBox('Center link'), 5000)")]},
    {"cut": "document.querySelector('#pill-mqtt').textContent.includes('20/20')", "timeout": 60, "badge": False,
     "pre": [F("center_link", 0, 1)]},

    {"chap": ("8", "Independent safety unit")},
    {"t": "A software bug at intersection nine commands conflicting greens.",
     "do": [(0.0, "(selectTsc(9), __ov.scrollTo('#cab', 'start'), true)"), (0.5, F("bug_conflict", 9, 1)), (0.6, "(__ov.hlTsc(9, 4000), true)")]},
    {"cut": "!!(S.world && S.world.field[9] && S.world.field[9].state === 2)", "timeout": 90, "settle": 1.5},
    {"t": "The independent safety unit, watching the real lamp outputs, caught it within a hundred and fifty milliseconds and latched flashing.",
     "do": [(0.3, "__ov.hl(__ov.cabBox('Signal heads'), 5000)"), (3.5, "__ov.hl('#zoom', 4000)")]},
    {"t": "It also checks multiple indications, dark heads, short ambers and the watchdog. Only a technician reset releases it.",
     "do": [(1.0, F("bug_conflict", 9, 0)), (4.5, "__ov.click('#cab button[data-f=mmu_reset]', 900)")]},

    {"chap": ("9", "Message security")},
    {"t": "A forged and a replayed message on channel sixteen, the red dots: the AES-CMAC exposes the forgery, the sequence number the replay. Both are dropped.",
     "do": [(0.0, "(selectTsc(17), true)"), (0.5, F("forge", 16)), (2.5, F("replay", 16)),
            (4.0, "(__ov.scrollTo('#cab table', 'center'), true)"), (4.5, "__ov.hl(__ov.cabBox('Peer network'), 5000)")]},

    {"chap": ("10", "Write · validate · release · deploy")},
    {"t": "Rules are data, not code: a table, with an if-missing fallback on every rule.",
     "do": [(0.0, TAB("rules")), (1.5, "__ov.hl('#r-table .rule', 4000)")]},
    {"t": "R17-01 now extends at twenty percent occupancy instead of twenty-five, by five seconds instead of four.",
     "do": [(0.5, "__ov.setRule('R17-01', 'input[data-c=value]', 20)"), (3.0, "__ov.setRule('R17-01', 'input[data-k=action_s]', 5)")]},
    {"t": "Saving commits readable rule text to Git, under the engineer's name.",
     "do": [(0.0, "(document.querySelector('#r-msg').value = 'I17: react earlier to stop-bar queues', true)"), (0.5, "__ov.click('#r-save', 600)"),
            (2.0, "__ov.hl('#r-result', 3500)")]},
    {"t": "Validation checks all twenty controllers: interpreter bounds, extension limits, buffer load, deadlines, current-cycle data and channel budget.",
     "do": [(0.0, "__ov.click('#v-run', 600)"), (2.0, "(__ov.scrollTo('#v-out', 'start'), true)"), (2.4, "__ov.hl('#v-out', 6000)")]},
    {"t": "Replay shows which recorded decisions the edit would change.",
     "do": [(0.0, "__ov.click('#rp-run', 600)"), (2.0, "__ov.hl('#rp-out', 4000)")]},
    {"t": "Approving your own release is refused.", "min": 3.6,
     "do": [(0.0, "(__ov.scrollTo('#rel-run', 'center'), document.querySelector('#rel-approver').value = 'a.joshi', document.querySelector('#rel-note').value = 'React earlier to stop-bar queues at I17', true)"),
            (0.3, "__ov.hl('#rel-approver', 1500)"), (0.9, "__ov.click('#rel-run', 500)"), (2.0, "__ov.hl('#rel-out', 2000)")]},
    {"t": "A second engineer approves; the rules compile to a binary table, signed with Ed25519.",
     "do": [(0.0, "(document.querySelector('#rel-approver').value = 'r.vermeer', true)"), (0.2, "__ov.hl('#rel-approver', 1800)"),
            (1.0, "__ov.click('#rel-run', 500)"), (3.4, "__ov.hl('#rel-out', 3000)")]},
    {"t": "Deployment stages it in the inactive flash slot; the controller verifies the signature and acknowledges.",
     "do": [(0.0, TAB("deploy")), (1.5, "__ov.click('#d-rel button[data-tg=changed]', 700)"), (4.0, "__ov.hl('#d-deps', 4000)")]},
    {"cut": "!!(S.deployments.length && ['scheduled','active'].includes((S.deployments.at(-1).targets[17]||{}).state))", "timeout": 60,
     "post": ["(__ov.scrollTo('#d-deps', 'start'), true)"]},
    {"t": "One signed activation command sets a common time; each controller switches at its next cycle boundary, on its synchronised clock.",
     "do": [(0.3, "__ov.hl('#d-deps .box', 6000)")]},
    {"cut": "!!(S.deployments.length && S.deployments.at(-1).phase === 'complete')", "timeout": 120,
     "post": ["(__ov.scrollTo('#d-grid', 'center'), true)"]},
    {"t": "Seventeen now runs version two from slot B; version one stays in slot A for rollback.",
     "do": [(0.3, "__ov.hl([...document.querySelectorAll('#d-grid .tile')].find(e => e.textContent.startsWith('I17')), 4500)")]},

    {"chap": ("11", "Telemetry and logs")},
    {"t": "The monitor shows what the center knows: mode, sync, detectors, neighbours, buffers, security, configuration and safety state, plus every alarm.",
     "do": [(0.0, TAB("monitor")), (2.0, "__ov.hl('#m-table', 5000)"), (6.5, "__ov.hl('#m-alarms', 3500)")]},
    {"t": "Every protocol is real and capturable: logs, CAN dumps, NMEA, the broker log, and Wireshark captures of PTP and channel traffic.",
     "do": [(0.0, TAB("logs")), (0.5, "(subLog('tsc17.log'), true)"), (4.5, "__ov.hl('#l-pcaps', 3500)")]},

    {"chap": ("", "Summary")},
    {"t": "A self-test injects all of these faults automatically: thirty-seven of thirty-seven checks pass.",
     "do": [(0.0, f"(__ov.card({json.dumps(END)}), true)")]},
    {"t": "Only the street, CAN wiring, network switch and clock hardware are simulated. The rest is the real software of the design."},
    {"pause": 1.2},
]


def sentences(script):
    return [it["t"] for it in script if "t" in it]


def tts(texts, outdir):
    sp = os.environ.get("TTS_DIR", os.path.join(HERE, "..", "build", "tts"))   # holds ttsvenv/ and voices/ (media/README.md)
    voice = os.environ.get("TTS_VOICE", os.path.join(sp, "voices", "en_US-ryan-high.onnx"))
    r = subprocess.run([os.path.join(sp, "ttsvenv", "bin", "python"), os.path.join(HERE, "tts.py"), voice, outdir],
                       input=json.dumps(texts), capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


if __name__ == "__main__":
    sys.path.insert(0, HERE)
    import tts as T
    wavdir = os.path.join(OUT, "..", "voice")
    texts = sentences(S)
    dur = tts(texts, wavdir)
    total = sum(dur.values()) + director.GAP * len(dur)
    print(f"{len(texts)} sentences, {sum(len(t.split()) for t in texts)} words, narration {total:.0f} s")
    if "--tts-only" in sys.argv:
        sys.exit(0)
    wavs = {t: os.path.join(wavdir, T.key(t) + ".wav") for t in texts}
    asyncio.run(director.run(S, dur, wavs, OUT, "http://127.0.0.1:8080/?sel=17#city"))
