"""Network emulation for the HIL testbench (not part of the product).

PeerSwitch  - the reliable inter-intersection network. Channel messages are
              delivered to every subscriber of the channel after the brief's
              delivery times (1 s within an intersection, 3 s between). PTP is
              forwarded immediately. A SPAN port writes everything to a pcap with
              the field framing (IPv4 multicast per channel, PTP on UDP 319/320).
WanEmulator - the unreliable center link: a TCP proxy per controller in front of
              the broker's mTLS port, adding the one-way latency (10 s round trip)
              and able to cut the link for as long as needed.
"""
import asyncio
import random
import struct
import time

from common import city as C
from common import formats as F


class PeerSwitch(asyncio.DatagramProtocol):
    def __init__(self, bench):
        self.b = bench
        self.subs = {t: set() for t in C.CITY}
        self.link_up = {t: True for t in C.CITY}
        self.last = {}                    # channel -> recent raw envelopes (for the replay attack)
        self.pcap = F.PcapWriter("runtime/logs/pcap/peer_network.pcap", F.LINKTYPE_ETHERNET)
        self.counts = dict(chan=0, ptp=0, delivered=0, dropped=0)
        self.transport = None
        self.events = []

    def connection_made(self, transport):
        self.transport = transport

    def _span(self, typ, src, dst, aux, payload):
        if typ == F.ENV_CHAN:
            grp = F.chan_group(aux)
            frame = F.eth_udp(F.mac_of(src), F.mcast_mac(grp), F.ip_of(src), grp, F.CHAN_UDP_PORT,
                              F.CHAN_UDP_PORT, payload)
        else:
            port = 319 if typ == F.ENV_PTP_EVENT else 320
            if dst:
                frame = F.eth_udp(F.mac_of(src), F.mac_of(dst), F.ip_of(src), F.ip_of(dst), port, port, payload)
            else:
                frame = F.eth_udp(F.mac_of(src), F.mcast_mac(F.PTP_MCAST), F.ip_of(src), F.PTP_MCAST, port, port,
                                  payload)
        self.pcap.write(frame)

    def ok(self, a, b):
        return self.link_up.get(a, False) and self.link_up.get(b, False)

    def deliver(self, t, data):
        if self.transport:
            self.transport.sendto(data, ("127.0.0.1", 31000 + t))
            self.counts["delivered"] += 1

    def datagram_received(self, data, addr):
        if len(data) < F.ENV_LEN or data[:4] != b"PNET":
            return
        _, typ, src, dst, aux, ln = struct.unpack_from(F.ENV, data)
        payload = data[F.ENV_LEN:F.ENV_LEN + ln]
        if typ == F.ENV_SUBSCRIBE:
            self.subs[src] = set(payload)
            return
        if not self.link_up.get(src, False):
            self.counts["dropped"] += 1
            return
        self._span(typ, src, dst, aux, payload)
        loop = asyncio.get_running_loop()
        if typ == F.ENV_CHAN:
            self.counts["chan"] += 1
            self.last.setdefault(aux, []).append(data)
            self.last[aux] = self.last[aux][-6:]
            self.fanout(src, aux, data, loop)
        else:
            self.counts["ptp"] += 1
            targets = [dst] if dst else C.CITY[src].neighbours
            for t in targets:
                if self.ok(src, t):
                    self.deliver(t, data)

    def fanout(self, src, ch, data, loop, tag="msg"):
        dsts = []
        for t, chs in self.subs.items():
            if ch in chs:
                if not self.ok(src, t):
                    self.counts["dropped"] += 1
                    continue
                delay = (C.DELIVERY_INTRA_S if t == src else C.DELIVERY_INTER_S) + random.uniform(-0.02, 0.02)
                loop.call_later(delay, self.deliver, t, data)
                dsts.append(t)
        m = F.parse_chan(data[F.ENV_LEN:])
        self.events.append(dict(kind=tag, src=src, ch=ch, dsts=dsts, t=time.time(),
                                seq=m["seq"] if m else None, sync=m["sync"] if m else None))

    # ---- attacks from a device plugged into the peer network
    def forge(self, ch):
        if not self.last.get(ch):
            return "no traffic captured on that channel yet"
        data = bytearray(self.last[ch][-1])
        p = F.ENV_LEN
        seq = struct.unpack_from("<I", data, p + 8)[0]
        struct.pack_into("<I", data, p + 8, seq + 1)              # plausible next sequence number
        for i in range(8):                                          # fake an approaching platoon
            struct.pack_into("<BBBBHH", data, p + 32 + 8 * i, i + 1, 0, 1, 9, 900, 3)
        self._span(F.ENV_CHAN, data[5], 0, ch, bytes(data[p:]))
        self.fanout(data[5], ch, bytes(data), asyncio.get_running_loop(), tag="forged")
        return f"forged message injected on channel {ch} (seq {seq + 1}, MAC not recomputable without the key)"

    def replay(self, ch):
        if len(self.last.get(ch, [])) < 2:
            return "not enough traffic captured on that channel yet"
        data = self.last[ch][0]
        self.fanout(data[5], ch, data, asyncio.get_running_loop(), tag="replayed")
        return f"replayed an old genuine message on channel {ch} (valid MAC, old sequence number)"


class WanEmulator:
    def __init__(self, bench, broker_port=8883):
        self.b = bench
        self.broker_port = broker_port
        self.one_way = C.CENTER_RTT_S / 2
        self.down = {t: False for t in C.CITY}
        self.conns = {t: [] for t in C.CITY}
        self.servers = []

    async def start(self):
        for t in C.CITY:
            srv = await asyncio.start_server(lambda r, w, t=t: self.handle(t, r, w), "127.0.0.1", 18800 + t)
            self.servers.append(srv)

    def set_down(self, t, down):
        self.down[t] = down
        if down:
            for a, b in self.conns[t]:
                a.close()
                b.close()
            self.conns[t] = []

    async def handle(self, t, reader, writer):
        if self.down[t]:
            writer.close()
            return
        try:
            ur, uw = await asyncio.open_connection("127.0.0.1", self.broker_port)
        except OSError:
            writer.close()
            return
        pair = (writer, uw)
        self.conns[t].append(pair)

        async def pump(src, dst):
            q = asyncio.Queue()

            async def rd():
                try:
                    while True:
                        data = await src.read(65536)
                        q.put_nowait((time.monotonic() + self.one_way, data))
                        if not data:
                            return
                except (ConnectionError, OSError):
                    q.put_nowait((time.monotonic(), b""))

            async def wr():
                try:
                    while True:
                        due, data = await q.get()
                        d = due - time.monotonic()
                        if d > 0:
                            await asyncio.sleep(d)
                        if not data:
                            dst.close()
                            return
                        dst.write(data)
                        await dst.drain()
                except (ConnectionError, OSError):
                    pass

            await asyncio.gather(rd(), wr())

        await asyncio.gather(pump(reader, uw), pump(ur, writer), return_exceptions=True)
        if pair in self.conns[t]:
            self.conns[t].remove(pair)
