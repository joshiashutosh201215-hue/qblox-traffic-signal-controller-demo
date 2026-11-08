"""Decode a compiled, signed controller configuration (.cfg) and check its Ed25519 signature.

    python3 tools/cfg_dump.py runtime/cc/releases/v1/tsc17.cfg
    python3 tools/cfg_dump.py runtime/flash/tsc17/slot_A.cfg

This is the exact byte layout the C++ controller parses (services/config_store.cpp, parse_config).
"""
import os, struct, sys, time
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from common import rules as R

MEAS = {v: k for k, v in R.MEASURE_CODES.items()}
OPS = {v: k for k, v in R.OP_CODES.items()}
TIMING = ["cycle", "offset", "main_min", "main_max_ext", "side_min", "amber", "allred", "sample_at", "upstream_wait_max",
          "sync_tolerance", "deadline_local", "deadline_upstream", "delivery_intra", "delivery_inter", "delivery_tolerance"]


def main(path):
    b = open(path, "rb").read()
    body, sig = b[:-64], b[-64:]
    h = struct.unpack(R.HEADER_FMT, body[:128])
    magic, fmt, tsc, n_rules, ver, flags, created, tag, ch, buf_n, n_subs, n_ups, subs, ups = h[:14]
    tm, git = h[14:29], h[29]
    z = lambda x: x.rstrip(b"\0").decode()
    print(f"{path}: {len(b)} bytes = 128 header + {n_rules} x 64 rule + 64 signature")
    print(f"  magic {magic.decode()}  format {fmt}  intersection I{tsc}  version {ver}  tag {z(tag)}"
          f"  git {z(git)}  created {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(created / 1e9))} UTC")
    print(f"  own channel {ch}  buffer emptied every {buf_n} cycle(s)  subscribes {list(subs[:n_subs])}  upstream {list(ups[:n_ups])}"
          f"  flags 0x{flags:x} (bit1 = skip side phase without a call)")
    print("  timing (ms): " + ", ".join(f"{k} {v}" for k, v in zip(TIMING, tm)))
    off = 128
    for _ in range(n_rules):
        rid, phase, n, act, miss, act_ms, miss_ms, prio = struct.unpack(R.RULE_FMT, body[off:off + struct.calcsize(R.RULE_FMT)])
        p = off + struct.calcsize(R.RULE_FMT)
        conds = []
        for k in range(n):
            src, det, m, op, val, _ = struct.unpack(R.CLAUSE_FMT, body[p + 12 * k:p + 12 * k + 12])
            conds.append(f"I{src} D{det} {MEAS[m]} {OPS[op]} {val / 1000:g}")
        print(f"  rule {z(rid):8s} priority {prio:3d}  when {' and '.join(conds)}"
              f"  then {R.action_text(act, act_ms / 1000)}  if missing {R.action_text(miss, miss_ms / 1000)}")
        off += 64
    pub = os.path.join(R.C.__file__.rsplit("/common/", 1)[0], "runtime", "pki", "config_signing.pub")
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(open(pub, "rb").read()).verify(sig, body)
        print(f"  Ed25519 signature: VALID ({os.path.relpath(pub)})")
    except FileNotFoundError:
        print("  Ed25519 signature: not checked (run ./demo.sh setup first)")
    except Exception:
        print("  Ed25519 signature: INVALID — a controller would reject this file")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "runtime/cc/releases/v1/tsc13.cfg")
