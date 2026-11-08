"""Video 2: ten-minute code walkthrough — where each part is implemented, and why these choices.

    python3 media/tools/v2_code.py
"""
import asyncio, json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import slides as SL
from v1_demo import tts

OUT = os.path.join(HERE, "..", "build", "v2")
PC, MS, TS = "controller/src/app/phase_controller.cpp", "controller/src/services/message_service.cpp", "controller/src/services/time_service.cpp"


def code(ch, title, parts, why, steps, path=None):
    return dict(kind="code", ch=ch, title=title, parts=parts, why=why, path=path or parts[0][0], steps=steps)


SLIDES = [
    dict(kind="title", h1="Code walkthrough", h2="Where each part of the design is implemented — and why these technologies",
         tags=["controller/ · C++17", "mmu/ · C99", "control_center/ · Python", "common/ · rule language", "testbench/ · HIL", "gui/ · browser console"],
         steps=[{"t": "This is the code walkthrough of the traffic signal control prototype: where each part of the design is implemented, and why these technologies were chosen."}]),

    dict(kind="diagram", ch="1", title="Repository = architecture", path="~/Qblox_TrafficSignalController_Demo",
         boxes=[(0, 0, 560, 250, "<b>controller/ — C++17</b>Intersection controller ×20<br><code>hal/ services/ app/ main.cpp</code><br>tests: RFC vectors, 400 rule vectors"),
                (0, 290, 560, 150, "<b>mmu/ — C99</b>Independent safety unit ×20<br><code>mmu.c</code> — shares no code"),
                (640, 0, 560, 250, "<b>control_center/ — Python</b>FastAPI + paho-mqtt<br><code>server.py</code> deploy · monitor · audit<br><code>release.py</code> Git · two-person · Ed25519"),
                (640, 290, 560, 150, "<b>common/ — shared</b><code>city.py rules.py formats.py</code><br>city model · rule language · wire formats"),
                (1280, 0, 540, 250, "<b>testbench/ — HIL bench</b><code>plant.py</code> vehicles + CANopen cards<br><code>net.py</code> peer switch · WAN emulator<br><code>bench.py</code> supervisor · faults"),
                (1280, 290, 540, 150, "<b>gui/ · tools/ · scripts/</b>browser console · self-test<br>PKI + broker setup · <code>demo.sh</code>"),
                (0, 500, 1820, 220, "<b>At run time: 43 processes on one Linux host</b>20 × <code>tsc</code> (C++) · 20 × <code>mmu</code> (C) · Mosquitto 2.0.18 (mTLS 8883) · control center (:8080) · testbench (:8091)<br>"
                                    "Real protocols: MQTT 3.1.1 over TLS · UDP multicast channel messages with AES-CMAC · IEEE 1588 PTP · CANopen CiA 301 frames · NMEA 0183")],
         steps=[{"t": "The repository mirrors the architecture: one folder per box in the design."},
                {"t": "controller holds the C++17 intersection controller, and mmu the independent safety unit, in C.", "rows": [0, 1]},
                {"t": "control_center is the Python back office. common holds the city model, the rule language and the wire formats, shared by the center and the bench.", "rows": [2, 3]},
                {"t": "testbench is the hardware-in-the-loop rig, and gui the browser console. demo.sh builds, starts and tests everything.", "rows": [4, 5]},
                {"t": "At run time that is forty-three processes: twenty controllers, twenty safety units, the broker, the control center and the bench, talking real protocols over local sockets.", "rows": [6]}]),

    dict(kind="table", ch="2", title="Technology choices", path="from the answer's technology table",
         head=["Choice", "Where", "Why"],
         rows=[["C++17, restricted subset", "controller", "<code>-fno-exceptions -fno-rtti</code>, no heap after start-up: predictable time and memory on a small embedded target"],
               ["C99, separate program", "safety unit", "small enough to review line by line; diverse from the controller, shares no code"],
               ["Python 3 · FastAPI · paho", "control center", "standard Linux server, fast to change; validator, compiler and replay share one rule module"],
               ["MQTT 3.1.1 · Mosquitto · mutual TLS", "center link", "outbound-only connections, QoS 1 store-and-forward, last will = liveness, per-device ACL"],
               ["UDP multicast · AES-128-CMAC · sequence", "peer network", "one 104-byte message per cycle; authenticated; replay and loss detectable"],
               ["GNSS 1PPS · IEEE 1588 PTP · TCXO", "time", "100 ms target with margin, independent of the center"],
               ["CANopen CiA 301", "detector cards", "standard field bus with heartbeat (liveness) and emergency (loop faults)"],
               ["OpenSSL · Git · SQLite · plain JS", "crypto · rules · audit · GUI", "standard primitives; review history and authorship for free; nothing to build"]],
         steps=[{"t": "The technology choices follow the technology table in the answer."},
                {"t": "Controllers use restricted C++17: no exceptions, no RTTI, and no heap after start-up, so timing and memory are predictable on a small embedded target.", "rows": [0]},
                {"t": "The safety unit is a separate C program that shares no code with the controller. It is small enough to review line by line, and a bug in one cannot hide in the other.", "rows": [1]},
                {"t": "The control center is Python with FastAPI: a standard Linux server, quick to change, where the validator and the replay use the same rule code.", "rows": [2]},
                {"t": "The center link is MQTT over mutual TLS. Controllers connect outbound only, quality-of-service one gives store-and-forward, and the last will reports a lost controller without polling.", "rows": [3]},
                {"t": "Peers exchange one small authenticated message per cycle. Time, detectors, cryptography, rules and audit use standard parts: PTP, CANopen, OpenSSL, Git and SQLite.", "rows": [4, 5, 6, 7]}]),

    code("3", "Controller entry point", [("controller/src/main.cpp", "// ---- heap instrumentation", 10), ("controller/src/main.cpp", "g_wd.start(2000);", 3)],
         ["Proves the “no heap after start-up” rule at run time, not just by review", "Everything else is static storage, sized at compile time", "Self-test asserts 0 allocations on all 20"],
         [{"t": "main.cpp is the controller's entry point. It replaces the global operator new with a counter, so any C++ heap allocation after start-up is counted.", "hl": [("void* operator new(size_t n)", 6)]},
          {"t": "Start-up builds everything from static storage, starts a two-second watchdog, and then freezes the heap. The cabinet view shows the counter, and the self-test asserts it stays zero.", "hl": [("g_wd.start(2000);", 3)]}]),

    code("3", "Cyclic executive — stands in for the RTOS", [("controller/src/main.cpp", "pollfd fds[6]", 35)],
         ["One thread, fixed order, bounded work per pass", "Lamp commands carry a watchdog toggle the safety unit checks", "A hang stops the toggle → safety unit flashes"],
         [{"t": "The main loop is a ten-millisecond cyclic executive standing in for the RTOS: one poll over all sockets, then every service ticks in a fixed order.", "hl": [("pollfd fds[6]", 3)]},
          {"t": "Time first, then detectors, messaging and the phase controller. Lamp commands go to the safety unit with a toggling watchdog bit, at least every hundred milliseconds.", "hl": [("g_ts.tick();", 4), ("if (L != last_lamps", 6)]},
          {"t": "The loop also kicks a software watchdog. If it ever hangs, the process exits, and the safety unit sees the toggle stop and goes to flash.", "hl": [("g_wd.kick();", 1)]}]),

    code("4", "Hardware abstraction layer", [("controller/src/hal/hal.hpp", "class Oscillator {", 9), ("controller/src/hal/hal.hpp", "class PlatformBuffer {", 7), ("controller/src/hal/hal.hpp", "// ---- Virtual CAN bus", 12)],
         ["Every device behind one class: oscillator, GNSS, peer NIC, buffer, CAN, lamps, flash, crypto, watchdog", "Real hardware replaces a class, not the services", "The controller never reads host time — only its own drifting crystal"],
         [{"t": "hal.hpp hides every device: oscillator, GNSS receiver, peer network card, platform buffer, CAN bus, lamp outputs, flash, crypto and watchdog. The controller only sees time through its own drifting crystal.", "hl": [("class Oscillator {", 9)]},
          {"t": "The platform buffer reproduces a fact from the brief: ten slots, and a silent drop when full. Losses can only be found higher up, from sequence numbers.", "hl": [("class PlatformBuffer {", 3)]},
          {"t": "CAN frames use the Linux can_frame layout, carried over UDP because a virtual CAN interface needs root. A real SocketCAN port would change only this class.", "hl": [("// ---- Virtual CAN bus", 12)]}]),

    code("5", "Detectors over CANopen", [("controller/src/services/detector_service.cpp", "if (fn == 0x700 && f.dlc >= 1)", 35)],
         ["CiA 301 subset: NMT, heartbeat, TPDO1, EMCY", "Heartbeat = liveness of each card (1.5 s)", "EMCY = loop open/short → reading invalid", "Every frame logged in candump format + pcap"],
         [{"t": "detector_service.cpp speaks CANopen to the two detector cards. A heartbeat proves a card is alive; a card that boots or is pre-operational gets an NMT start command.", "hl": [("if (fn == 0x700 && f.dlc >= 1)", 14)]},
          {"t": "TPDO1 carries the presence bitmap of four loops.", "hl": [("} else if (fn == 0x180", 2)]},
          {"t": "An emergency frame marks a loop open or shorted. The reading becomes invalid, and every rule that uses that detector takes its fallback. Missing heartbeats make the card silent after one and a half seconds.", "hl": [("} else if (fn == 0x080", 19)]}]),

    code("6", "Peer messages: security checks", [(MS, "uint8_t ch = m[4], src = m[5];", 31)],
         ["104-byte message, one per cycle", "Order: schema → MAC → replay → loss", "Every rejection raises an alarm to the center", "The platform never reports drops; sequence gaps do"],
         [{"t": "message_service.cpp handles the 104-byte channel messages. Each one is checked in order. The schema version first, so older controllers ignore newer device types.", "hl": [("if (m[0] != 'T'", 4)]},
          {"t": "Then the AES-CMAC: a forged message is dropped and raises an alarm.", "hl": [("uint8_t mac[8];", 8)]},
          {"t": "Then the sequence number. Not newer means a replay. A gap means lost messages, for example from the platform buffer, which never reports it.", "hl": [("uint32_t seq = rd32(m + 8);", 17)]}]),

    code("6", "Peer messages: may this reading be used now?", [(MS, "ChanState* s = find_src(src);", 36)],
         ["“Current” is defined in time: sampled after this cycle started", "Bounded wait, then the rule's declared fallback", "Each refusal has a reason → decision record"],
         [{"t": "lookup decides whether an upstream reading may be used in this cycle, and every refusal carries a written reason into the decision record.", "hl": [("ChanState* s = find_src(src);", 3)]},
          {"t": "A silent neighbour, or one that came back but has not yet had two good cycles, counts as missing. That hysteresis prevents flapping.", "hl": [("if (s->live == ChanState::SILENT)", 8)]},
          {"t": "The key rule of the answer: the reading must be sampled after this controller's cycle started. Otherwise the controller waits, within a bound, and then applies the rule's fallback.", "hl": [("if (!legacy && smp < cycle_start)", 5)]},
          {"t": "Finally, the sender's clock must be synchronised, and the arrival must match the expected delivery time. That catches a sender or receiver whose clock has drifted.", "hl": [("if ((SyncQ)m[6] == SyncQ::UNSYNC", 11)]}]),

    code("7", "Time: GNSS → PTP → holdover", [(TS, "if (!ever_synced_) {", 37)],
         ["Step only once, at start-up; afterwards slew ≤ 10 %", "GNSS jump + PTP peer disagrees → spoof rejected", "PI servo; lower gains for software-timestamped PTP", "Center never sets clocks (±5 s estimate only)"],
         [{"t": "time_service.cpp implements the hierarchy: GNSS first, PTP from a neighbour second, and the crystal in holdover third.", "hl": []},
          {"t": "The clock may be stepped only once, at start-up, before actuated control begins. After that it is slewed, so the cycle never jumps.", "hl": [("if (!ever_synced_) {", 8)]},
          {"t": "If GNSS jumps while a PTP peer agrees with the local clock, GNSS is rejected as spoofed. Without a peer, a jump is accepted only after ten consistent readings.", "hl": [("if (from_gnss && llabs(theta) > kJump)", 15)]},
          {"t": "Corrections come from a PI servo, with lower gains for PTP because software timestamps are noisier. The PTP messages follow IEEE 1588 two-step, with a simplified best-master selection.", "hl": [("double kp = from_gnss", 13)]}]),

    code("8", "Phase controller: one decision per cycle", [(PC, "bool PhaseController::fixed_time_needed", 8), (PC, "int64_t cycle_start = ", 26)],
         ["Decision at the end of minimum main green (14 s)", "Local detection lost → fixed-time plan", "Upstream in flight → hold green ≤ 2.5 s", "Record written before the lamps change"],
         [{"t": "phase_controller.cpp runs the signal phases and makes one decision per cycle, at the end of the minimum main green.", "hl": []},
          {"t": "First, are the local detectors good enough? With both cards silent, or three of four stop-bar loops failed, it runs the local fixed-time plan.", "hl": [("bool PhaseController::fixed_time_needed", 8)]},
          {"t": "Otherwise it looks up every input the rules need. If an upstream reading is still in flight, it holds the green for at most two and a half seconds, then declares the input missing.", "hl": [("bool wait = false;", 15)]},
          {"t": "The interpreter evaluates, any missing input sets degraded mode, and the decision is written as a record.", "hl": [("evaluate(c, g_in, n, g_res, &d);", 12)]}]),

    code("9", "Rule interpreter and arbiter", [("controller/src/app/rule_interpreter.cpp", "void evaluate(", 38)],
         ["Rules are data: ≤ 16 rules × 3 conditions → bounded time", "Per-rule if-missing fallback", "Highest priority wins; extension capped at 8 s", "Python reference must agree: 400 shared vectors"],
         [{"t": "rule_interpreter.cpp is deliberately small. Rules arrive compiled and sorted by priority, at most sixteen rules of three conditions, so evaluation time is bounded.", "hl": [("for (int i = 0; i < c.n_rules; ++i)", 1)]},
          {"t": "A rule fires when all its conditions hold. If any input is missing, the rule's declared if-missing action applies instead. That is the per-rule fallback.", "hl": [("for (int k = 0; k < r.n; ++k)", 21)]},
          {"t": "The arbiter takes the highest-priority rule that wants an action, and caps the extension at eight seconds, so the side street keeps its minimum green.", "hl": [("if (d->winner < 0", 5)]},
          {"t": "The center's Python evaluator must give identical answers: four hundred shared test vectors run against both, and replay compares them on recorded cycles.", "hl": []}]),

    code("10", "Signed configuration, A/B slots, decision records", [("controller/src/services/config_store.cpp", "bool ConfigStore::stage", 23), ("controller/src/services/config_store.cpp", "void Recorder::append", 8)],
         ["Ed25519 verify on the controller; key never leaves the center", "Anti-rollback version counter", "Activation at agreed time T, at the next cycle boundary", "SHA-256 hash chain: edits and gaps visible"],
         [{"t": "config_store.cpp manages two flash slots, A and B. A staged configuration must parse, carry a valid Ed25519 signature, and be newer than the active one, which blocks rollback attacks.", "hl": [("if (!parse_config(", 5), ("if (c.version < act)", 5)]},
          {"t": "It is written to the inactive slot. A separate signed command names an activation time, and the switch happens at the first cycle boundary after it, so neighbours change together.", "hl": [("char slot = active_ == 'A'", 7)]},
          {"t": "The recorder appends each decision as a JSON line, with its own SHA-256 and the previous record's hash, so any edit or gap is visible at the center.", "hl": [("void Recorder::append", 8)]}]),

    code("11", "Center link: MQTT over mutual TLS", [("controller/src/services/center_agent.cpp", "mosquitto_lib_init();", 15), ("scripts/setup.py", "# Field devices and the control center", 13)],
         ["Outbound only: no open ports on controllers", "Last will → offline without polling", "QoS 1 records stay on flash until acknowledged", "ACL: a controller writes only field/&lt;its CN&gt;/#"],
         [{"t": "center_agent.cpp is the controller's MQTT client: libmosquitto, with the controller's own certificate and key.", "hl": [("mosquitto_tls_set", 2)]},
          {"t": "Its last will publishes offline if the connection drops. Health goes out every two seconds; alarms and records use quality-of-service one, and records stay on flash until acknowledged.", "hl": [("mosquitto_will_set", 1)]},
          {"t": "The broker requires client certificates and takes the user name from the certificate. The access list lets each controller write only its own topics.", "hl": [("require_certificate true", 4)]}]),

    code("12", "Independent safety unit", [("mmu/mmu.c", "if (have_cmd && !latched && now - last_cmd", 12), ("mmu/mmu.c", "field = (uint16_t)(cmd | welded);", 10), ("mmu/mmu.c", "if (fault[0]) {", 5)],
         ["Monitors field outputs, not intentions", "Conflict ≥ 150 ms · multiple indication · dark head · short amber · watchdog", "Flash transfer disconnects load switches", "Latched until technician reset"],
         [{"t": "mmu.c is the independent safety unit. It sees the lamp outputs, not the controller's intentions.", "hl": []},
          {"t": "If the controller's watchdog toggle stops for one second, it latches flash. Flash transfer disconnects the load switches, so even a welded output goes dark.", "hl": [("if (have_cmd && !latched && now - last_cmd", 12)]},
          {"t": "It flags conflicting greens or ambers held for a hundred and fifty milliseconds, multiple indications, dark heads, and clearances shorter than the minimum amber.", "hl": [("field = (uint16_t)(cmd | welded);", 10)]},
          {"t": "Every fault latches until a technician reset. The controller cannot clear it.", "hl": [("if (fault[0]) {", 5)]}]),

    code("13", "Rule language and validator", [("common/rules.py", "    [R17-02]", 8), ("common/rules.py", "# buffers: M messages per cycle", 21)],
         ["Readable text in Git, edited as a table", "Validator proves the answer's limits before release", "Compiler: fixed binary layout per controller", "Same module drives replay"],
         [{"t": "On the center side, common/rules.py defines the rule language: one readable block per rule, with conditions, an action, an if-missing fallback, and a priority.", "hl": [("[R17-02]", 8)]},
          {"t": "The validator proves the limits from the answer for all twenty controllers before anything is released. Buffer load: messages per cycle, times N plus one, must fit in ten slots.", "hl": [("# buffers: M messages per cycle", 6)]},
          {"t": "Deadline: one decision per cycle; five seconds if any rule needs an upstream reading, otherwise two. And each upstream reading must be sampled within the receiver's current cycle.", "hl": [("# deadline: the arbiter", 15)]}]),

    code("14", "Release: two-person rule, signing", [("control_center/release.py", "def make_release", 14), ("control_center/release.py", "def activation_command", 4)],
         ["Author ≠ approver, enforced in code", "Must validate; working copy committed", "Ed25519 per controller; key file stands in for an HSM", "Approver recorded in a signed Git tag"],
         [{"t": "release.py enforces the two-person rule: the approver must be a different engineer. The rules must validate, and the working copy must be committed.", "hl": [("if not approver or approver == author", 7)]},
          {"t": "Each controller's table is compiled and signed with Ed25519; the key file stands in for an HSM. Activation and rollback commands are signed the same way.", "hl": [("def activation_command", 4)]}]),

    code("14", "Deployment and audit", [("control_center/server.py", "async def deploy(self, version", 30), ("control_center/server.py", "cut = raw.rfind", 4)],
         ["Stage everywhere → all ack → one activation time", "Any reject → abort, nothing activated", "20 s lead covers the 10 s link", "Records: hash + chain verified into SQLite"],
         [{"t": "server.py orchestrates a deployment: stage everywhere, wait for every acknowledgement, and abort without activating anything if one controller rejects.", "hl": [("for t in targets:", 8), ("bad = [t for t", 5)]},
          {"t": "Only then does it send one activation time to all targets, with a twenty-second lead to cover the ten-second link.", "hl": [("at = time.time_ns() + int(lead_s * 1e9)", 2)]},
          {"t": "Uploaded records are hash-checked and chain-checked into SQLite, and the trace view explains each decision from the record, the release and Git.", "hl": [("cut = raw.rfind", 4)]}]),

    code("15", "Testbench: the field, emulated", [("testbench/net.py", "def fanout", 13), ("testbench/plant.py", "# IDM", 5)],
         ["Peer switch applies the brief's 1 s / 3 s delivery", "SPAN port → peer_network.pcap", "WAN emulator: 5 s each way, can be cut", "IDM vehicles; CANopen card emulation"],
         [{"t": "The testbench replaces the field. net.py is the peer switch: it delivers channel messages after the brief's one and three seconds, and mirrors every frame to a pcap file, like a SPAN port.", "hl": [("delay = (C.DELIVERY_INTRA_S", 2)]},
          {"t": "plant.py moves vehicles with the intelligent driver model and generates loop occupancy; its detector cards are CANopen nodes on each cabinet bus.", "hl": [("# IDM", 5)]},
          {"t": "bench.py supervises all processes, emulates the WAN with five seconds each way, and injects every fault. The GUI shows both views: what the center knows over MQTT, and the truth inside the cabinets.", "hl": []}]),

    dict(kind="list", ch="16", title="Tests and what they found", path="controller/tests · tools/selftest.py",
         items=["<code>test_crypto</code>: AES-CMAC = RFC 4493 vectors · Ed25519 = RFC 8032 test 1",
                "<code>test_rules</code>: 400 golden vectors — C++ interpreter = Python reference",
                "<code>selftest.py</code>: 37 checks on the live system — sync, faults, security, center outage, release, deploy, hash chains, deadlines",
                "Found: SIGPIPE killed controllers when the center link dropped → ignored, errors as return codes",
                "Found: hash-chain head recovered from the wrong field after reboot → fixed",
                "Found: records lost while the center was down → persistent MQTT session + broker persistence",
                "Confirmed the review: previous-cycle data + buffer emptied every 4th cycle → message loss, deadlines missed"],
         steps=[{"t": "Tests come in three layers: crypto against RFC vectors, four hundred shared rule vectors, and an end-to-end self-test.", "rows": [0, 1]},
                {"t": "The self-test drives the live system through thirty-seven checks: synchronisation, faults, security, the center outage, release, deployment, hash chains and deadlines.", "rows": [2]},
                {"t": "It found real defects: a SIGPIPE that killed controllers when the center link dropped, a hash-chain recovery bug after reboot, and records lost while the center was down.", "rows": [3, 4, 5]},
                {"t": "It also confirmed the review of the original proposal: with previous-cycle data and a buffer emptied every fourth cycle, messages are lost and deadlines are missed.", "rows": [6]}]),

    dict(kind="table", ch="17", title="Real vs emulated — and the path to hardware", path="README.md",
         head=["Part", "In this MVP", "On real hardware"],
         rows=[["Controller, safety unit, center software", "real", "same code; RTOS port replaces the cyclic executive"],
               ["MQTT/TLS, Ed25519, AES-CMAC, SHA-256, Git", "real", "same; signing key moves into an HSM"],
               ["PTP", "real messages, software timestamps (~0.1–1 ms)", "hardware-timestamping NIC: sub-microsecond"],
               ["CAN bus", "can_frame layout over UDP", "SocketCAN driver behind hal::CanBus"],
               ["GNSS, TCXO, street, detectors, switch", "simulated by the bench", "real receiver, crystal, loops, managed switch"],
               ["Standards (EN 12675, NEMA TS 2, NTCIP…)", "not claimed", "to be confirmed and mapped"]],
         steps=[{"t": "To be clear about the limits: the street, the CAN wiring, the switch, the GNSS receiver and the crystal are simulated, and PTP uses software timestamps, so it is accurate to about a millisecond, not sub-microsecond.", "rows": [2, 3, 4]},
                {"t": "The RTOS is a cyclic executive on Linux, and the HSM is a protected key file. No conformance to traffic standards is claimed.", "rows": [0, 1, 5]},
                {"t": "Each of these sits behind one HAL class or one bench module, so real hardware replaces those parts and keeps the rest. demo.sh start runs it all, and demo.sh test reproduces every result shown."}]),
]


if __name__ == "__main__":
    import tts as T
    texts = [s["t"] for sl in SLIDES for s in sl["steps"] if s.get("t")]
    wavdir = os.path.join(OUT, "..", "voice")
    if "--no-tts" in sys.argv:
        dur = {t: 6.0 for t in texts}
    else:
        dur = tts(texts, wavdir)
    print(f"{len(texts)} sentences, {sum(len(t.split()) for t in texts)} words, narration {sum(dur.values()) + 0.45 * len(dur):.0f} s")
    if "--tts-only" in sys.argv:
        sys.exit(0)
    wavs = {t: os.path.join(wavdir, T.key(t) + ".wav") for t in texts}
    asyncio.run(SL.render(SLIDES, dur, wavs, OUT))
