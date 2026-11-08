"""HIL testbench: powers the 20 cabinets (controller + safety unit processes),
runs the plant, the detector cards, the peer network and the WAN, injects faults,
and streams what a bench engineer would see to the GUI (ws://127.0.0.1:8091).

None of this is product code: it replaces the street, the cabinets' hardware and
the networks so the real controller, safety unit and control-center software can
be exercised end to end.
"""
import asyncio
import json
import logging
import os
import random
import signal
import struct
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from websockets.asyncio.server import broadcast, serve  # noqa: E402

from common import city as C  # noqa: E402
from testbench.net import PeerSwitch, WanEmulator  # noqa: E402
from testbench.plant import Plant  # noqa: E402

RT = os.path.join(ROOT, "runtime")
BIN = os.path.join(ROOT, "controller", "build")
logging.basicConfig(filename=os.path.join(RT, "logs", "testbench.log"), level=logging.INFO,
                    format="%(asctime)s.%(msecs)03d BENCH %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
log = logging.getLogger("bench")
LOG_STREAMS = ["mosquitto.log", "cc.log", "testbench.log"] + \
              [f"tsc{t:02d}.log" for t in C.CITY] + [f"mmu{t:02d}.log" for t in C.CITY] + \
              [f"can/tsc{t:02d}.candump" for t in C.CITY] + [f"nmea/tsc{t:02d}.nmea" for t in C.CITY]


class Proc:
    def __init__(self, name, argv_fn):
        self.name, self.argv_fn = name, argv_fn
        self.proc, self.wanted, self.starts, self.state = None, True, 0, "starting"

    async def run(self, bench):
        while self.wanted:
            self.starts += 1
            out = open(os.path.join(RT, "logs", "procs", f"{self.name}.out"), "a")
            self.proc = await asyncio.create_subprocess_exec(*self.argv_fn(), stdout=out, stderr=out)
            self.state = "running"
            rc = await self.proc.wait()
            out.close()
            self.state = "rebooting" if self.wanted else "off"
            msg = {-9: "killed (power loss)", 70: "watchdog reset"}.get(rc, f"exit code {rc}")
            log.info("%s stopped: %s", self.name, msg)
            bench.event(f"{self.name}: {msg}" + ("; rebooting" if self.wanted else ""))
            if self.wanted:
                await asyncio.sleep(2.0)       # boot time
                bench.reapply(self.name)


class Bench:
    def __init__(self):
        self.clients = set()
        self.logsubs = {}
        self.events = []
        self.probes, self.field = {}, {}
        self.faults = {t: {} for t in C.CITY}
        self.ppm = {t: round(random.uniform(-1.8, 1.8), 2) for t in C.CITY}   # each cabinet's TCXO
        self.procs = {}
        self.loop = None

    def event(self, text):
        self.events.append(dict(t=time.time(), text=text))
        self.events = self.events[-200:]
        broadcast(self.clients, json.dumps(dict(type="bench_event", t=time.time(), text=text)))

    # ---------------------------------------------------------------- cabinets
    def tsc_argv(self, t):
        x = C.CITY[t]
        return [os.path.join(BIN, "tsc"), "--id", str(t), "--runtime", RT,
                "--nb", ",".join(map(str, x.neighbours)), "--gnss", "1" if x.gnss else "0",
                "--boot-offset-ms", str(round(random.uniform(-1500, 1500))), "--ppm", str(self.ppm[t])]

    def mmu_argv(self, t):
        return [os.path.join(BIN, "mmu"), "--id", str(t), "--runtime", RT]

    def backplane(self, t, cmd, value=0):
        self.udp.sendto(json.dumps(dict(cmd=cmd, value=value)).encode(), ("127.0.0.1", 34000 + t))

    def mmu_cmd(self, t, cmd, value=0):
        self.udp.sendto(json.dumps(dict(cmd=cmd, value=value)).encode(), ("127.0.0.1", 34100 + t))

    def reapply(self, name):
        """After a controller reboot, hardware faults on the bench are still present."""
        if not name.startswith("tsc"):
            return
        t = int(name[3:])
        f = self.faults[t]

        def later():
            if f.get("gnss_off"):
                self.backplane(t, "gnss", 0)
            if f.get("ptp_off"):
                self.backplane(t, "ptp", 0)
            if "tcxo" in f:
                self.backplane(t, "ppm", f["tcxo"])
            if "spoof" in f:
                self.backplane(t, "spoof", f["spoof"])
        self.loop.call_later(1.0, later)

    # ---------------------------------------------------------------- faults
    def fault(self, c):
        k, t = c.get("kind"), int(c.get("tsc", 0) or 0)
        v = c.get("value")
        f = self.faults.get(t, {})
        msg = None
        if k == "det_stuck":
            self.plant.set_det_fault(t, int(v), "stuck"); f[f"D{v}"] = "stuck-on"
            msg = f"I{t} D{v}: loop stuck ON (detector card keeps reporting presence)"
        elif k == "det_open":
            self.plant.set_det_fault(t, int(v), "open"); f[f"D{v}"] = "loop open"
            msg = f"I{t} D{v}: loop cable cut (card sends CANopen EMCY)"
        elif k == "card_off":
            self.plant.card_power(t, int(v), False); f[f"card{int(v)}"] = "off"
            msg = f"I{t} detector card {'AB'[int(v)]} (node 0x{0x11 + int(v):02X}) lost power: heartbeat stops"
        elif k == "det_repair":
            for d in range(1, 9):
                self.plant.set_det_fault(t, d, None)
            for i in range(2):
                self.plant.card_power(t, i, True)
            for key in [x for x in f if x.startswith("D") or x.startswith("card")]:
                f.pop(key)
            msg = f"I{t}: detectors and cards repaired"
        elif k == "tsc_kill":
            p = self.procs.get(f"tsc{t:02d}")
            if p and p.proc:
                p.proc.kill()
            msg = f"I{t} controller: crash (process killed); the bench reboots it in 2 s"
        elif k == "tsc_power":
            p = self.procs.get(f"tsc{t:02d}")
            if p and not v:
                p.wanted = False
                if p.proc and p.proc.returncode is None:
                    p.proc.kill()
                f["power"] = "off"
            elif p and v and not p.wanted:
                p.wanted = True
                f.pop("power", None)
                asyncio.ensure_future(p.run(self))
            msg = f"I{t} controller power {'ON (booting)' if v else 'OFF (stays down)'}"
        elif k == "tsc_hang":
            self.backplane(t, "hang")
            msg = f"I{t} controller: software hang injected (watchdog should reset it)"
        elif k == "bug_conflict":
            self.backplane(t, "bug_conflict", 1 if v else 0)
            if v: f["bug"] = "conflicting greens"
            else: f.pop("bug", None)
            msg = f"I{t} controller: software defect {'commands N/S green during main green' if v else 'removed'}"
        elif k == "weld":
            self.mmu_cmd(t, "weld", 8 if v else -1)
            if v: f["weld"] = "N green welded"
            else: f.pop("weld", None)
            msg = f"I{t} load switch for head N green {'WELDED ON' if v else 'replaced'}"
        elif k == "mmu_reset":
            self.mmu_cmd(t, "reset")
            msg = f"I{t}: field technician pressed the safety-unit RESET"
        elif k == "gnss":
            self.backplane(t, "gnss", 1 if v else 0)
            if v: f.pop("gnss_off", None)
            else: f["gnss_off"] = True
            msg = f"I{t} GNSS antenna {'restored' if v else 'blocked (no satellites)'}"
        elif k == "ptp":
            self.backplane(t, "ptp", 1 if v else 0)
            if v: f.pop("ptp_off", None)
            else: f["ptp_off"] = True
            msg = f"I{t} PTP port {'restored' if v else 'failed'}"
        elif k == "tcxo":
            ppm = float(v) if v else self.ppm[t]
            self.backplane(t, "ppm", ppm)
            if v: f["tcxo"] = ppm
            else: f.pop("tcxo", None)
            msg = f"I{t} oscillator {'drifting at %+.0f ppm (%.1f ms/s)' % (ppm, ppm / 1000) if v else 'healthy again'}"
        elif k == "spoof":
            self.backplane(t, "spoof", float(v or 0))
            if v: f["spoof"] = float(v)
            else: f.pop("spoof", None)
            msg = f"I{t} GNSS {'spoofed: receiver time shifted by %+d ms' % int(v) if v else 'spoofing stopped'}"
        elif k == "clock_step":
            self.backplane(t, "clock_step", float(v))
            msg = f"I{t} local oscillator counter jumped {float(v):+.0f} ms"
        elif k == "peer_link":
            self.switch.link_up[t] = bool(v)
            if v: f.pop("peer_link", None)
            else: f["peer_link"] = "down"
            msg = f"I{t} peer-network link {'restored' if v else 'DOWN (no channel messages, no PTP)'}"
        elif k == "center_link":
            ts = list(C.CITY) if t == 0 else [t]
            for x in ts:
                self.wan.set_down(x, not v)
                if v: self.faults[x].pop("center_link", None)
                else: self.faults[x]["center_link"] = "down"
            msg = f"{'ALL intersections' if t == 0 else 'I%d' % t}: center link {'restored' if v else 'DOWN'}"
        elif k == "wan_latency":
            self.wan.one_way = float(v) / 2
            msg = f"center link round trip set to {float(v):.1f} s"
        elif k == "forge":
            msg = "ATTACK: " + self.switch.forge(t)
        elif k == "replay":
            msg = "ATTACK: " + self.switch.replay(t)
        elif k == "legacy":
            self.backplane(t, "legacy", 1 if v else 0)
            if v: f["legacy"] = "proposal: previous-cycle upstream data"
            else: f.pop("legacy", None)
            msg = f"I{t}: proposal behaviour (previous-cycle upstream readings) {'ON' if v else 'OFF'}"
        elif k == "buffer_n":
            self.backplane(t, "buffer_n", int(v))
            if int(v) > 1: f["buffer_n"] = int(v)
            else: f.pop("buffer_n", None)
            msg = f"I{t}: buffer emptied every {int(v)} cycle(s)"
        elif k == "demand":
            self.plant.demand = float(v)
            msg = f"traffic demand x{float(v):.1f}"
        elif k == "clear_all":
            for x in C.CITY:
                for d in range(1, 9):
                    self.plant.set_det_fault(x, d, None)
                for i in range(2):
                    self.plant.card_power(x, i, True)
                self.backplane(x, "gnss", 0 if not C.CITY[x].gnss else 1)
                self.backplane(x, "ptp", 1)
                self.backplane(x, "ppm", self.ppm[x])
                self.backplane(x, "spoof", 0)
                self.backplane(x, "bug_conflict", 0)
                self.backplane(x, "legacy", 0)
                self.backplane(x, "buffer_n", 0)
                self.mmu_cmd(x, "weld", -1)
                self.switch.link_up[x] = True
                self.wan.set_down(x, False)
                self.faults[x] = {}
            msg = "all injected faults cleared (safety units stay latched until reset)"
        if msg:
            log.info("FAULT INJECTION %s", msg)
            self.event(msg)
        return msg

    # ---------------------------------------------------------------- GUI
    async def ws_handler(self, ws):
        self.clients.add(ws)
        await ws.send(json.dumps(dict(type="hello", events=self.events[-40:], streams=LOG_STREAMS)))
        try:
            async for raw in ws:
                c = json.loads(raw)
                if c.get("cmd") == "fault":
                    self.fault(c)
                elif c.get("cmd") == "log_sub":
                    stream = c.get("stream")
                    if stream in LOG_STREAMS:
                        p = os.path.join(RT, "logs", stream)
                        size = os.path.getsize(p) if os.path.exists(p) else 0
                        self.logsubs[ws] = dict(stream=stream, pos=max(0, size - 6000), first=True)
        finally:
            self.clients.discard(ws)
            self.logsubs.pop(ws, None)

    async def push_world(self):
        while True:
            await asyncio.sleep(0.1)
            snap = self.plant.snapshot()
            snap.update(type="world", t=time.time(),
                        field={t: f for t, f in self.field.items()},
                        msgs=self.switch.events[-60:], counts=self.switch.counts)
            self.switch.events = []
            broadcast(self.clients, json.dumps(snap))

    async def push_probes(self):
        while True:
            await asyncio.sleep(0.5)
            procs = {n: dict(state=p.state, starts=p.starts, pid=p.proc.pid if p.proc and p.state == "running" else None)
                     for n, p in self.procs.items()}
            broadcast(self.clients, json.dumps(dict(
                type="probes", t=time.time(), probes=self.probes, faults=self.faults, procs=procs,
                wan=dict(one_way=self.wan.one_way, down=self.wan.down), peer=self.switch.link_up)))

    async def tail_logs(self):
        while True:
            await asyncio.sleep(0.4)
            for ws, s in list(self.logsubs.items()):
                p = os.path.join(RT, "logs", s["stream"])
                if not os.path.exists(p):
                    continue
                with open(p, "rb") as f:
                    f.seek(s["pos"])
                    data = f.read(200000)
                    s["pos"] = f.tell()
                if not data:
                    continue
                text = data.decode(errors="replace")
                if s.pop("first", False) and "\n" in text:
                    text = text.split("\n", 1)[1]
                try:
                    await ws.send(json.dumps(dict(type="log", stream=s["stream"], text=text[-60000:])))
                except Exception:
                    pass

    # ---------------------------------------------------------------- UDP endpoints
    class _Rx(asyncio.DatagramProtocol):
        def __init__(self, fn):
            self.fn = fn

        def datagram_received(self, data, addr):
            self.fn(data)

    def on_probe(self, data):
        try:
            p = json.loads(data)
        except ValueError:
            return
        p["rx"] = time.time()
        self.probes[p["id"]] = p

    def on_field(self, data):
        if len(data) < 80 or data[:4] != b"FLD1":
            return
        t, state = data[4], data[5]
        lamps, welded = struct.unpack_from("<HH", data, 6)
        trips = struct.unpack_from("<I", data, 10)[0]
        fault = data[14:77].split(b"\0", 1)[0].decode(errors="replace")
        self.field[t] = dict(lamps=lamps, state=state, welded=welded, trips=trips, fault=fault, t=time.time())
        self.plant.field[t] = self.field[t]

    async def main(self):
        self.loop = asyncio.get_running_loop()
        self.udp = __import__("socket").socket(2, 2)
        loop = self.loop
        can_out, _ = await loop.create_datagram_endpoint(asyncio.DatagramProtocol, local_addr=("127.0.0.1", 0))
        self.plant = Plant(lambda t, fr: can_out.sendto(fr, ("127.0.0.1", 32000 + t)))
        for t in C.CITY:
            await loop.create_datagram_endpoint(lambda t=t: self._Rx(lambda d, t=t: self.plant.can_rx(t, d)),
                                                local_addr=("127.0.0.1", 32100 + t))
        self.switch = PeerSwitch(self)
        await loop.create_datagram_endpoint(lambda: self.switch, local_addr=("127.0.0.1", 30000))
        await loop.create_datagram_endpoint(lambda: self._Rx(self.on_probe), local_addr=("127.0.0.1", 35000))
        await loop.create_datagram_endpoint(lambda: self._Rx(self.on_field), local_addr=("127.0.0.1", 33500))
        self.wan = WanEmulator(self)
        await self.wan.start()
        for t in C.CITY:
            self.procs[f"mmu{t:02d}"] = Proc(f"mmu{t:02d}", lambda t=t: self.mmu_argv(t))
        for t in C.CITY:
            self.procs[f"tsc{t:02d}"] = Proc(f"tsc{t:02d}", lambda t=t: self.tsc_argv(t))
        for p in self.procs.values():
            asyncio.ensure_future(p.run(self))
        log.info("testbench up: 20 cabinets powered, plant running, peer switch :30000, WAN :18801-18820, GUI ws :8091")
        asyncio.ensure_future(self.push_world())
        asyncio.ensure_future(self.push_probes())
        asyncio.ensure_future(self.tail_logs())
        stop = asyncio.Event()
        for s in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(s, stop.set)
        async with serve(self.ws_handler, "127.0.0.1", 8091, max_size=2 ** 22):
            last = time.monotonic()
            while not stop.is_set():
                await asyncio.sleep(0.1)
                now = time.monotonic()
                self.plant.step(min(now - last, 0.3))
                last = now
        for p in self.procs.values():
            p.wanted = False
            if p.proc and p.proc.returncode is None:
                p.proc.terminate()
        await asyncio.sleep(0.5)


if __name__ == "__main__":
    asyncio.run(Bench().main())
