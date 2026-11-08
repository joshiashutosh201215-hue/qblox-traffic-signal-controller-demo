"""Plant model for the HIL testbench: road network, vehicles, inductive loops and
the CANopen detector cards in each cabinet. Not part of the product.

Vehicles follow the Intelligent Driver Model, obey the FIELD lamp outputs coming
from each cabinet's safety unit (not the controller's commands), stop on red,
decide on amber, treat flashing red / dark heads as a stop sign and never drive
into a box occupied by crossing traffic.
"""
import math
import random
import struct
import time

from common import city as C
from common import formats as F

MARGIN = 70.0
S = C.SPACING_M
WIDTH = 2 * MARGIN + (C.COLS - 1) * S
HEIGHT = 2 * MARGIN + (C.ROWS - 1) * S
LANE_OFF = 3.0
BOX = 7.0              # half width of the intersection box
STOP = BOX + 1.5       # stop line distance from the centre
VLEN = 4.5
V0, A_MAX, B_COMF, S0, T_HW = 13.9, 1.8, 2.5, 2.0, 1.2

DIRS = {  # travel direction -> (head index facing it, stop-bar detector, exit detector)
    "E": (0, 1, 5), "W": (1, 2, 6), "N": (3, 3, 7), "S": (2, 4, 8)}
DEMAND = {"E": 0.22, "W": 0.10, "N": 0.05, "S": 0.06}   # veh/s per entry lane


def centre(tid):
    r, c = C.rc(tid)
    return MARGIN + c * S, MARGIN + r * S


class Lane:
    def __init__(self, d, idx):
        self.d, self.idx = d, idx
        self.length = WIDTH if d in "EW" else HEIGHT
        self.veh = []
        # intersections along the lane in travel order: (s_centre, tsc)
        pts = []
        for t in C.CITY:
            r, c = C.rc(t)
            x, y = centre(t)
            if d in "EW" and r == idx:
                pts.append((x if d == "E" else WIDTH - x, t))
            if d in "NS" and c == idx:
                pts.append((y if d == "S" else HEIGHT - y, t))
        self.ints = sorted(pts)

    def xy(self, s):
        if self.d == "E":
            return s, MARGIN + self.idx * S + LANE_OFF
        if self.d == "W":
            return WIDTH - s, MARGIN + self.idx * S - LANE_OFF
        if self.d == "S":
            return MARGIN + self.idx * S - LANE_OFF, s
        return MARGIN + self.idx * S + LANE_OFF, HEIGHT - s


class Vehicle:
    __slots__ = ("id", "lane", "s", "v", "committed", "stopped_at", "cleared", "t0", "stops", "was_moving")

    def __init__(self, vid, lane):
        self.id, self.lane, self.s, self.v = vid, lane, 0.0, V0 * 0.8
        self.committed, self.cleared = set(), set()
        self.stopped_at, self.t0, self.stops, self.was_moving = {}, time.time(), 0, True


class Card:
    """CANopen detector card (CiA 301 subset): boot-up, heartbeat, NMT, TPDO1, EMCY."""

    def __init__(self, plant, tsc, node, dets):
        self.p, self.tsc, self.node, self.dets = plant, tsc, node, dets
        self.state, self.powered = "off", True
        self.last_hb = self.last_pdo = 0.0
        self.bits, self.seq = 0, 0
        self.power_on()

    def power_on(self):
        self.powered, self.state = True, "preop"
        self.send(F.HB_BASE + self.node, bytes([F.HB_BOOTUP]))

    def power_off(self):
        self.powered, self.state = False, "off"

    def nmt(self, cmd, node):
        if not self.powered or node not in (0, self.node):
            return
        if cmd == F.NMT_START:
            self.state = "op"
        elif cmd == F.NMT_PREOP:
            self.state = "preop"
        elif cmd == F.NMT_STOP:
            self.state = "stopped"
        elif cmd == F.NMT_RESET:
            self.power_on()

    def send(self, can_id, data):
        self.p.can_tx(self.tsc, can_id, data)

    def emcy(self, ch, kind):
        code = 0x5000 if kind else 0x0000
        self.send(F.EMCY_BASE + self.node, struct.pack("<HBBBxxx", code, 0x01 if kind else 0, ch, kind))

    def tick(self, now, presence):
        if not self.powered:
            return
        if now - self.last_hb >= 1.0:
            self.last_hb = now
            st = {"op": F.HB_OPERATIONAL, "preop": F.HB_PREOP, "stopped": F.HB_STOPPED}[self.state]
            self.send(F.HB_BASE + self.node, bytes([st]))
        if self.state != "op":
            return
        bits = 0
        for k, d in enumerate(self.dets):
            if presence[d - 1]:
                bits |= 1 << k
        if bits != self.bits or now - self.last_pdo >= 0.1:     # send on change + 100 ms event timer
            self.bits, self.last_pdo = bits, now
            self.seq = (self.seq + 1) & 0xFF
            ms = int(now * 1000) & 0xFFFFFFFF
            self.send(F.TPDO1_BASE + self.node, struct.pack("<BBI", bits, self.seq, ms))


class Plant:
    def __init__(self, can_send):
        self.can_send = can_send          # (tsc, frame_bytes) -> None
        self.lanes = [Lane(d, i) for d in "EW" for i in range(C.ROWS)] + \
                     [Lane(d, i) for d in "NS" for i in range(C.COLS)]
        self.field = {t: dict(lamps=0, state=1, t=0.0) for t in C.CITY}
        self.presence = {t: [False] * 8 for t in C.CITY}
        self.faults = {t: {} for t in C.CITY}       # det -> "stuck" | "open"
        self.cards = {}
        self.pcaps = {}
        self.demand = 1.0
        self.next_id = 1
        self.rng = random.Random(7)
        self.stats = dict(spawned=0, exited=0, avg_speed=0.0, waiting=0, travel_s=[], stops=[])
        for t in C.CITY:
            self.pcaps[t] = F.PcapWriter(f"runtime/logs/pcap/can_tsc{t:02d}.pcap", F.LINKTYPE_CAN_SOCKETCAN)
            self.cards[t] = (Card(self, t, 0x11, (1, 2, 3, 4)), Card(self, t, 0x12, (5, 6, 7, 8)))

    # ---- CAN
    def can_tx(self, tsc, can_id, data):
        self.pcaps[tsc].write(F.can_pcap_record(can_id, data))
        self.can_send(tsc, F.can_frame(can_id, data))

    def can_rx(self, tsc, frame):
        can_id, data = F.parse_can_frame(frame)
        self.pcaps[tsc].write(F.can_pcap_record(can_id, data))
        if can_id == F.NMT_COB and len(data) >= 2:
            for card in self.cards[tsc]:
                card.nmt(data[0], data[1])

    # ---- faults on the detector side
    def set_det_fault(self, tsc, det, kind):
        card = self.cards[tsc][(det - 1) // 4]
        ch = (det - 1) % 4 + 1
        prev = self.faults[tsc].get(det)
        if kind:
            self.faults[tsc][det] = kind
            if kind == "open":
                card.emcy(ch, 1)
        else:
            self.faults[tsc].pop(det, None)
            if prev == "open":
                card.emcy(ch, 0)

    def card_power(self, tsc, idx, on):
        card = self.cards[tsc][idx]
        if on and not card.powered:
            card.power_on()
        elif not on:
            card.power_off()

    # ---- signals as the road user sees them
    def head_state(self, tsc, head, now):
        f = self.field[tsc]
        if now - f["t"] > 1.5:
            return "dark"
        L = f["lamps"]
        r, a, g = (L >> (head * 3)) & 1, (L >> (head * 3 + 1)) & 1, (L >> (head * 3 + 2)) & 1
        if f["state"] != 0:   # safety unit flashing: main heads flash amber, side heads flash red
            return "flash_amber" if head in (0, 1) else "flash_red"
        if g:
            return "green"
        if a:
            return "amber"
        if r:
            return "red"
        return "dark"

    def box_axes(self):
        occ = {t: set() for t in C.CITY}
        for ln in self.lanes:
            axis = "EW" if ln.d in "EW" else "NS"
            for v in ln.veh:
                for sc, t in ln.ints:
                    if v.s > sc - BOX and v.s - VLEN < sc + BOX:
                        occ[t].add(axis)
        return occ

    def step(self, dt):
        now = time.time()
        occ = self.box_axes()
        moving_speed, n_v, waiting = 0.0, 0, 0
        for ln in self.lanes:
            head, _, _ = DIRS[ln.d]
            axis = "EW" if ln.d in "EW" else "NS"
            other = "NS" if axis == "EW" else "EW"
            ln.veh.sort(key=lambda v: -v.s)
            leader = None
            for v in ln.veh:
                obstacle = 1e9
                dv = 0.0
                if leader is not None:
                    obstacle = leader.s - VLEN - v.s
                    dv = v.v - leader.v
                # next intersection whose stop line is still ahead of the front bumper
                for sc, t in ln.ints:
                    line = sc - STOP
                    if v.s > line + 0.5:
                        continue
                    dist = line - v.s
                    if dist > 60:
                        break
                    st = self.head_state(t, head, now)
                    must_stop = False
                    if st == "green" or st == "flash_amber":
                        must_stop = other in occ[t] and dist < 4
                    elif st == "amber":
                        if t not in v.committed:
                            if dist < v.v * v.v / (2 * 3.5) + 1:
                                v.committed.add(t)
                            else:
                                must_stop = True
                    else:  # red, flashing red, dark: red stops; flashing red / dark behave like a stop sign
                        if st in ("flash_red", "dark"):
                            if t in v.cleared:
                                must_stop = bool(occ[t])
                            else:
                                must_stop = True
                                if dist < 1.5 and v.v < 0.3:
                                    v.stopped_at.setdefault(t, now)
                                    if now - v.stopped_at[t] > 1.2 and not occ[t]:
                                        v.cleared.add(t)
                                        must_stop = False
                        else:
                            must_stop = True
                    if st == "flash_amber" and dist < 30:
                        v.v = min(v.v, 7.0)
                    if must_stop and dist < obstacle:
                        obstacle, dv = dist, v.v
                    break
                # IDM
                s_star = S0 + max(0.0, v.v * T_HW + v.v * dv / (2 * math.sqrt(A_MAX * B_COMF)))
                acc = A_MAX * (1 - (v.v / V0) ** 4 - (s_star / max(obstacle, 0.1)) ** 2)
                acc = max(acc, -8.0)
                v.v = max(0.0, v.v + acc * dt)
                v.s += v.v * dt
                if v.v < 0.5 and v.was_moving:
                    v.stops += 1
                v.was_moving = v.v >= 0.5
                moving_speed += v.v
                n_v += 1
                waiting += v.v < 0.5
                leader = v
            done = [v for v in ln.veh if v.s > ln.length + 10]
            for v in done:
                ln.veh.remove(v)
                self.stats["exited"] += 1
                self.stats["travel_s"].append(now - v.t0)
                self.stats["stops"].append(v.stops)
            # arrivals
            if self.rng.random() < DEMAND[ln.d] * self.demand * dt:
                if not ln.veh or min(x.s for x in ln.veh) > 12:
                    ln.veh.append(Vehicle(self.next_id, ln))
                    self.next_id += 1
                    self.stats["spawned"] += 1
        self.stats["avg_speed"] = moving_speed / n_v if n_v else 0.0
        self.stats["waiting"] = waiting
        self.stats["vehicles"] = n_v
        self.stats["travel_s"] = self.stats["travel_s"][-200:]
        self.stats["stops"] = self.stats["stops"][-200:]
        self.update_detectors(now)

    def update_detectors(self, now):
        for t in C.CITY:
            pres = [False] * 8
            for ln in self.lanes:
                _, d_stop, d_exit = DIRS[ln.d]
                for sc, tt in ln.ints:
                    if tt != t:
                        continue
                    sb = (sc - STOP - 6.0, sc - STOP - 1.0)
                    ex = (sc + BOX + 6.0, sc + BOX + 11.0)
                    for v in ln.veh:
                        front, rear = v.s, v.s - VLEN
                        if front > sb[0] and rear < sb[1]:
                            pres[d_stop - 1] = True
                        if front > ex[0] and rear < ex[1]:
                            pres[d_exit - 1] = True
            for det, kind in self.faults[t].items():
                pres[det - 1] = kind == "stuck"
            self.presence[t] = pres
            for card in self.cards[t]:
                card.tick(now, pres)

    def snapshot(self):
        vs = []
        for ln in self.lanes:
            dcode = "EWNS".index(ln.d)
            for v in ln.veh:
                x, y = ln.xy(v.s - VLEN / 2)
                vs.append([round(x, 1), round(y, 1), dcode, round(v.v, 1), v.id % 7])
        ts = self.stats["travel_s"]
        sp = self.stats["stops"]
        return dict(vehicles=vs, w=WIDTH, h=HEIGHT, margin=MARGIN, spacing=S, box=BOX, stop=STOP,
                    presence={t: [int(b) for b in p] for t, p in self.presence.items()},
                    cards={t: [c.state if c.powered else "off" for c in cs] for t, cs in self.cards.items()},
                    det_faults={t: {str(k): v for k, v in f.items()} for t, f in self.faults.items() if f},
                    stats=dict(vehicles=self.stats.get("vehicles", 0), waiting=self.stats["waiting"],
                               avg_speed=round(self.stats["avg_speed"] * 3.6, 1), exited=self.stats["exited"],
                               avg_travel=round(sum(ts) / len(ts), 1) if ts else 0,
                               avg_stops=round(sum(sp) / len(sp), 2) if sp else 0, demand=self.demand))
