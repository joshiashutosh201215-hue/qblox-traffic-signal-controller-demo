"""Generate shared golden vectors for the rule interpreter (Python reference -> C++ test)."""
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common import rules as R  # noqa: E402

rng = random.Random(26)
MEAS = list(R.MEASURE_CODES)
OPS = list(R.OP_CODES)
out = []
for v in range(400):
    tsc, ups = 17, [16, 12]
    rules = []
    for i in range(rng.randint(1, 6)):
        cl = []
        for _ in range(rng.randint(1, 3)):
            src = rng.choice([tsc, tsc] + ups)
            m = rng.choice(MEAS)
            val = {"occupancy": rng.randint(0, 1000) / 10, "gap": rng.randint(0, 600) / 10,
                   "count": rng.randint(0, 12), "presence": rng.randint(0, 1)}[m]
            cl.append(R.Clause("own" if src == tsc else "upstream", src, rng.randint(1, 8), m, rng.choice(OPS), val))
        rules.append(R.Rule(id=f"R{v % 100}-{i}", title="", tsc=tsc, clauses=cl,
                            action=rng.choice([1, 1, 2]), action_s=rng.randint(1, 12),
                            if_missing=rng.choice([0, 0, 1, 2]), missing_s=rng.randint(1, 5),
                            priority=rng.randint(1, 99)))
    keys = {(c.src_tsc, c.det) for r in rules for c in r.clauses}
    inputs, lines = {}, []
    for k in sorted(keys):
        ok = rng.random() > 0.2
        rd = dict(presence=rng.randint(0, 1), count=rng.randint(0, 12), occ_permille=rng.randint(0, 1000),
                  gap_ds=rng.randint(0, 600))
        inputs[k] = rd if ok else None
        lines.append(f"I {k[0]} {k[1]} {int(ok)} {rd['presence']} {rd['count']} {rd['occ_permille']} {rd['gap_ds']}")
    res, dec = R.evaluate(rules, inputs, 8.0)
    out.append(f"V {v}")
    for r in sorted(rules, key=lambda r: -r.priority):
        cs = " ".join(f"{c.src_tsc} {c.det} {R.MEASURE_CODES[c.measure]} {R.OP_CODES[c.op]} {int(round(c.value * 1000))}"
                      for c in r.clauses)
        out.append(f"R {r.id} {r.priority} {r.action} {int(r.action_s * 1000)} {r.if_missing} "
                   f"{int(r.missing_s * 1000)} {len(r.clauses)} {cs}")
    out.extend(lines)
    act = {"NONE": 0, "EXTEND": 1, "HOLD": 2}[dec["action"]]
    st = "".join({"FIRED": "F", "NOT_FIRED": "N", "MISSING": "M"}[x["status"]] for x in res)
    out.append(f"E {act} {dec['ext_ms']} {dec['by'] or '-'} {st}")
path = os.path.join(os.path.dirname(__file__), "..", "shared", "rule_vectors.txt")
open(path, "w").write("\n".join(out) + "\n")
print(f"wrote {len(out)} lines, 400 vectors -> {os.path.normpath(path)}")
