"""Wire formats shared by the Python side (testbench, control center).

The C++ controller has its own encoders in controller/src/services/message_service.cpp;
both follow docs/PROTOCOLS.md.
"""
import struct
import time

# ---- channel message (peer network), 104 bytes, AES-CMAC-64 over bytes 0..95
CHAN_HDR = "<2sBBBBBBIIqII"
CHAN_RD = "<BBBBHH"
CHAN_LEN = 104
QUALITY = {0: "OK", 1: "STUCK_ON", 2: "LOOP_FAULT", 3: "SILENT", 4: "CHATTER"}
SYNCQ = {0: "GNSS", 1: "PTP", 2: "HOLDOVER", 3: "UNSYNC"}


def parse_chan(b: bytes):
    if len(b) < CHAN_LEN or b[:2] != b"TS":
        return None
    (_, schema, dclass, ch, src, syncq, n, seq, cfgv, ts, cyc, _r) = struct.unpack_from(CHAN_HDR, b)
    rds = []
    for i in range(min(n, 8)):
        det, q, pres, cnt, occ, gap = struct.unpack_from(CHAN_RD, b, 32 + 8 * i)
        rds.append(dict(det=det, q=QUALITY.get(q, q), presence=pres, count=cnt,
                        occ_pct=occ / 10, gap_s=gap / 10))
    return dict(schema=schema, device_class=dclass, channel=ch, src=src, sync=SYNCQ.get(syncq, syncq),
                seq=seq, cfg=cfgv, sample_ns=ts, cycle=cyc, readings=rds, mac=b[96:104].hex())


# ---- peer-network envelope between a controller and the emulated switch
ENV = "<4sBBBBH"
ENV_LEN = struct.calcsize(ENV)
ENV_CHAN, ENV_PTP_EVENT, ENV_PTP_GENERAL, ENV_SUBSCRIBE = 1, 2, 3, 4

# ---- CANopen (CiA 301) objects used by the detector cards
NMT_COB = 0x000
EMCY_BASE, TPDO1_BASE, HB_BASE = 0x080, 0x180, 0x700
NMT_START, NMT_STOP, NMT_PREOP, NMT_RESET = 0x01, 0x02, 0x80, 0x81
HB_BOOTUP, HB_STOPPED, HB_OPERATIONAL, HB_PREOP = 0x00, 0x04, 0x05, 0x7F
CARD_NODES = (0x11, 0x12)           # card A: D1-D4, card B: D5-D8


def can_frame(can_id: int, data: bytes) -> bytes:
    """Linux struct can_frame (16 bytes, host byte order) as sent on the virtual CAN bus."""
    return struct.pack("<IB3x8s", can_id, len(data), data.ljust(8, b"\0"))


def parse_can_frame(b: bytes):
    can_id, dlc, data = struct.unpack("<IB3x8s", b[:16])
    return can_id & 0x1FFFFFFF, data[:dlc]


# ---- pcap writers (no root needed: frames are written from the emulator's tap)
LINKTYPE_ETHERNET, LINKTYPE_CAN_SOCKETCAN = 1, 227


class PcapWriter:
    def __init__(self, path, linktype):
        self.f = open(path, "wb", buffering=0)
        self.f.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, linktype))

    def write(self, data: bytes, ts=None):
        ts = time.time() if ts is None else ts
        sec = int(ts)
        usec = int((ts - sec) * 1e6)
        self.f.write(struct.pack("<IIII", sec, usec, len(data), len(data)) + data)

    def close(self):
        self.f.close()


def can_pcap_record(can_id: int, data: bytes) -> bytes:
    # LINKTYPE_CAN_SOCKETCAN: CAN ID in network byte order, then len, pad, data
    return struct.pack(">IB3x", can_id, len(data)) + data.ljust(8, b"\0")


def _ip_checksum(h: bytes) -> int:
    s = sum(struct.unpack("!10H", h))
    s = (s >> 16) + (s & 0xFFFF)
    s += s >> 16
    return ~s & 0xFFFF


def mac_of(tsc: int) -> bytes:
    return bytes([0x02, 0x54, 0x53, 0x00, 0x00, tsc])


def ip_of(tsc: int) -> bytes:
    return bytes([10, 0, 0, tsc])


def eth_udp(src_mac, dst_mac, src_ip, dst_ip, sport, dport, payload, ttl=1):
    udp = struct.pack("!HHHH", sport, dport, 8 + len(payload), 0) + payload
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 0, 0x4000, ttl, 17, 0, src_ip, dst_ip)
    ip = ip[:10] + struct.pack("!H", _ip_checksum(ip)) + ip[12:]
    return dst_mac + src_mac + b"\x08\x00" + ip + udp


def mcast_mac(ip4: bytes) -> bytes:
    return bytes([0x01, 0x00, 0x5E, ip4[1] & 0x7F, ip4[2], ip4[3]])


PTP_MCAST = bytes([224, 0, 1, 129])
CHAN_UDP_PORT = 47100


def chan_group(ch: int) -> bytes:
    return bytes([239, 1, 0, ch])
