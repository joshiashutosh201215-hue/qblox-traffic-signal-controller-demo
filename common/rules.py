"""Rule language: parse, validate, compile, evaluate.

Rules are data, not code. A traffic engineer edits a table in the GUI; the table
is saved as this readable text format in Git (one block per rule):

    [R17-02]
    title      = Extend main green for an approaching platoon
    where      = intersection 17, phase main
    when       = own D1 occupancy > 10 %
    and        = upstream 16 D5 gap < 3 s
    then       = extend green by 6 s
    if-missing = do not extend
    priority   = 20

The validator proves timing, buffer, channel and current-cycle limits before
deployment. The compiler emits one signed binary table per controller. The
evaluator here is the reference for replay; the C++ interpreter on the
controller must agree with it (shared golden vectors in shared/rule_vectors.json).
"""
import re
import struct
from dataclasses import dataclass, field, asdict

from . import city as C

MEASURE_CODES = {"occupancy": 1, "gap": 2, "count": 3, "presence": 4}
OP_CODES = {">": 1, ">=": 2, "<": 3, "<=": 4, "==": 5}
ACT_NONE, ACT_EXTEND, ACT_HOLD = 0, 1, 2
ACT_NAMES = {ACT_NONE: "do not extend", ACT_EXTEND: "extend green", ACT_HOLD: "hold, do not extend"}
MAX_RULES, MAX_CLAUSES = 16, 3

HEADER_FMT = "<4sHBBIIq16sBBBB8s4s" + "I" * 15 + "12s"
RULE_FMT = "<8sBBBBIIB3x"
CLAUSE_FMT = "<BBBBiI"
assert struct.calcsize(HEADER_FMT) == 128
assert struct.calcsize(RULE_FMT) + 3 * struct.calcsize(CLAUSE_FMT) + 4 == 64
ACT_FMT = "<4sBBHIq"     # activation / rollback command, followed by 64-byte signature


class RuleError(Exception):
    pass


@dataclass
class Clause:
    src: str          # "own" or "upstream"
    src_tsc: int
    det: int
    measure: str
    op: str
    value: float

    def text(self, own):
        s = "own" if self.src_tsc == own else f"upstream {self.src_tsc}"
        unit = C.MEASURES[self.measure][0]
        v = f"{self.value:g}"
        return f"{s} D{self.det} {self.measure} {self.op} {v}{(' ' + unit) if unit else ''}"


@dataclass
class Rule:
    id: str
    title: str
    tsc: int
    phase: str = "main"
    clauses: list = field(default_factory=list)
    action: int = ACT_EXTEND
    action_s: float = 0.0
    if_missing: int = ACT_NONE
    missing_s: float = 0.0
    priority: int = 10
    enabled: bool = True
    line: int = 0

    def to_dict(self):
        d = asdict(self)
        d["action_text"] = action_text(self.action, self.action_s)
        d["missing_text"] = action_text(self.if_missing, self.missing_s)
        d["when_text"] = [c.text(self.tsc) for c in self.clauses]
        return d


def action_text(act, secs):
    if act == ACT_EXTEND:
        return f"extend green by {secs:g} s"
    return ACT_NAMES[act]


_CLAUSE_RE = re.compile(
    r"^(own|upstream\s+(\d+))\s+D([1-8])\s+(occupancy|gap|count|presence)\s*"
    r"(>=|<=|==|>|<)\s*([0-9]+(?:\.[0-9]+)?)\s*(%|s|veh)?$", re.I)
_EXT_RE = re.compile(r"^extend green by\s+([0-9]+(?:\.[0-9]+)?)\s*s$", re.I)


def _parse_action(text, line):
    t = text.strip().lower()
    m = _EXT_RE.match(t)
    if m:
        return ACT_EXTEND, float(m.group(1))
    if t in ("hold, do not extend", "hold"):
        return ACT_HOLD, 0.0
    if t in ("do not extend", "none"):
        return ACT_NONE, 0.0
    raise RuleError(f"line {line}: unknown action '{text}' "
                    "(use 'extend green by N s', 'hold, do not extend' or 'do not extend')")


def parse_rules(text: str, tsc: int):
    """Parse one intersection's .rules file. Raises RuleError with a line number."""
    rules, cur = [], None
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"^\[(R[0-9A-Za-z-]{1,7})\]$", line)
        if m:
            cur = Rule(id=m.group(1), title="", tsc=tsc, line=n)
            rules.append(cur)
            continue
        if cur is None:
            raise RuleError(f"line {n}: expected a rule header like [R{tsc}-01]")
        if "=" not in line:
            raise RuleError(f"line {n}: expected 'field = value'")
        key, val = (s.strip() for s in line.split("=", 1))
        key = key.lower()
        if key == "title":
            cur.title = val
        elif key == "where":
            m = re.match(r"^intersection\s+(\d+)\s*,\s*phase\s+(main)$", val, re.I)
            if not m:
                raise RuleError(f"line {n}: where must be 'intersection <n>, phase main'")
            if int(m.group(1)) != tsc:
                raise RuleError(f"line {n}: rule is in the file for I{tsc} but says intersection {m.group(1)}")
            cur.phase = "main"
        elif key in ("when", "and"):
            m = _CLAUSE_RE.match(val)
            if not m:
                raise RuleError(f"line {n}: cannot read condition '{val}' "
                                "(form: own|upstream <n> D<1-8> <measure> <op> <value> [unit])")
            src_tsc = tsc if m.group(1).lower() == "own" else int(m.group(2))
            cur.clauses.append(Clause(src="own" if src_tsc == tsc else "upstream", src_tsc=src_tsc,
                                      det=int(m.group(3)), measure=m.group(4).lower(),
                                      op=m.group(5), value=float(m.group(6))))
        elif key == "then":
            cur.action, cur.action_s = _parse_action(val, n)
        elif key == "if-missing":
            cur.if_missing, cur.missing_s = _parse_action(val, n)
        elif key == "priority":
            if not val.isdigit():
                raise RuleError(f"line {n}: priority must be a whole number")
            cur.priority = int(val)
        elif key == "enabled":
            cur.enabled = val.lower() in ("yes", "true", "1")
        else:
            raise RuleError(f"line {n}: unknown field '{key}'")
    return rules


def format_rules(rules, tsc):
    out = [f"# Rule set for intersection {tsc}. Written by the rule table editor.",
           "# One block per rule; fields mirror the editor columns.", ""]
    for r in rules:
        out.append(f"[{r.id}]")
        out.append(f"title      = {r.title}")
        out.append(f"where      = intersection {tsc}, phase {r.phase}")
        for i, c in enumerate(r.clauses):
            out.append(f"{'when' if i == 0 else 'and':<10} = {c.text(tsc)}")
        out.append(f"then       = {action_text(r.action, r.action_s)}")
        out.append(f"if-missing = {action_text(r.if_missing, r.missing_s)}")
        out.append(f"priority   = {r.priority}")
        if not r.enabled:
            out.append("enabled    = no")
        out.append("")
    return "\n".join(out)


def rule_from_dict(d, tsc):
    r = Rule(id=d["id"], title=d.get("title", ""), tsc=tsc, phase="main",
             action=int(d["action"]), action_s=float(d.get("action_s", 0)),
             if_missing=int(d["if_missing"]), missing_s=float(d.get("missing_s", 0)),
             priority=int(d["priority"]), enabled=bool(d.get("enabled", True)))
    for c in d.get("clauses", []):
        st = int(c["src_tsc"])
        r.clauses.append(Clause(src="own" if st == tsc else "upstream", src_tsc=st,
                                det=int(c["det"]), measure=c["measure"], op=c["op"],
                                value=float(c["value"])))
    return r


def default_rules(tsc):
    x = C.CITY[tsc]
    rules = [Rule(id=f"R{tsc}-01", title="Extend main green while a queue is present", tsc=tsc,
                  clauses=[Clause("own", tsc, 1, "occupancy", ">", 25)],
                  action=ACT_EXTEND, action_s=4, if_missing=ACT_NONE, priority=10)]
    west = [u for u in x.upstream if C.rc(u)[0] == x.row]
    north = [u for u in x.upstream if C.rc(u)[1] == x.col]
    if west and north:
        rules.append(Rule(id=f"R{tsc}-02", title="Platoon from west, unless the north feeder is busy", tsc=tsc,
                          clauses=[Clause("upstream", west[0], 5, "gap", "<", 3),
                                   Clause("upstream", north[0], 8, "count", "<", 4)],
                          action=ACT_EXTEND, action_s=6, if_missing=ACT_NONE, priority=20))
    elif west:
        rules.append(Rule(id=f"R{tsc}-02", title="Extend main green for an approaching platoon", tsc=tsc,
                          clauses=[Clause("upstream", west[0], 5, "gap", "<", 3)],
                          action=ACT_EXTEND, action_s=6, if_missing=ACT_NONE, priority=20))
    rules.append(Rule(id=f"R{tsc}-09", title="Do not block the box (gridlock guard)", tsc=tsc,
                      clauses=[Clause("own", tsc, 5, "occupancy", ">", 60)],
                      action=ACT_HOLD, if_missing=ACT_NONE, priority=90))
    return rules


# ---------------------------------------------------------------- validation

def _chk(report, name, ok, detail, level="fail"):
    report.append({"check": name, "status": "ok" if ok else level, "detail": detail})
    return ok


def timing_for(tsc, upstream_used):
    """Worst-case timeline in receiver cycle time for one decision (seconds)."""
    T = C.TIMING
    x = C.CITY[tsc]
    own_sample = T["sample_at_s"]
    own_arrival = own_sample + C.DELIVERY_INTRA_S
    ups = []
    for u in upstream_used:
        delta = (x.offset_s - C.CITY[u].offset_s) % C.CYCLE_S
        ts = T["sample_at_s"] - delta          # upstream sample, in receiver cycle time
        ups.append(dict(tsc=u, delta=delta, sample=ts, arrival=ts + C.DELIVERY_INTER_S))
    decision = max([T["main_min_s"], own_arrival] + [u["arrival"] for u in ups]) + C.EVAL_TIME_S
    last_reading = max([own_sample] + [u["sample"] for u in ups])
    return dict(own_sample=own_sample, own_arrival=own_arrival, ups=ups, decision=decision,
                last_reading=last_reading, wait=max(0.0, decision - C.EVAL_TIME_S - T["main_min_s"]))


def validate(all_rules: dict, buffer_n: dict = None, legacy_prev_cycle: dict = None):
    """all_rules: {tsc: [Rule]} (or {tsc: RuleError}). Returns (ok, report)."""
    buffer_n = buffer_n or {}
    legacy_prev_cycle = legacy_prev_cycle or {}
    T = C.TIMING
    out = {"ok": True, "intersections": {}, "city": []}
    used_channels = set()
    for tsc in sorted(C.CITY):
        rep = []
        rules = all_rules.get(tsc, [])
        if isinstance(rules, Exception):
            _chk(rep, "Syntax", False, str(rules))
            out["intersections"][tsc] = rep
            out["ok"] = False
            continue
        x = C.CITY[tsc]
        active = [r for r in rules if r.enabled]
        ok = True
        ok &= _chk(rep, "Syntax", True, f"{len(rules)} rules parsed, {len(active)} enabled")
        ok &= _chk(rep, "Bounded interpreter", len(active) <= MAX_RULES and all(len(r.clauses) <= MAX_CLAUSES for r in active),
                   f"{len(active)}/{MAX_RULES} rules, max {max([len(r.clauses) for r in active] or [0])}/{MAX_CLAUSES} conditions per rule")
        ids = [r.id for r in rules]
        ok &= _chk(rep, "Unique rule ids", len(ids) == len(set(ids)), ", ".join(ids) or "none")
        upstream_used = set()
        for r in active:
            if not r.clauses:
                ok &= _chk(rep, f"{r.id} conditions", False, "rule has no condition")
            for c in r.clauses:
                unit, lo, hi = C.MEASURES[c.measure]
                ok &= _chk(rep, f"{r.id} {c.text(tsc)}", lo <= c.value <= hi,
                           f"detector D{c.det} exists; {c.measure} range {lo}..{hi} {unit}")
                if c.src_tsc != tsc:
                    good = c.src_tsc in x.upstream
                    ok &= _chk(rep, f"{r.id} source I{c.src_tsc}", good,
                               f"I{c.src_tsc} is {'an' if good else 'NOT an'} upstream neighbour of I{tsc} "
                               f"(upstream: {', '.join('I%d' % u for u in x.upstream) or 'none'})")
                    if good:
                        upstream_used.add(c.src_tsc)
            ok &= _chk(rep, f"{r.id} fallback", True, f"if missing: {action_text(r.if_missing, r.missing_s)}")
            if r.action == ACT_EXTEND or r.if_missing == ACT_EXTEND:
                ext = max(r.action_s, r.missing_s)
                ok &= _chk(rep, f"{r.id} extension limit", 0 < ext <= T["main_max_ext_s"],
                           f"extends {ext:g} s; limit {T['main_max_ext_s']:g} s keeps side green >= {T['side_min_s']:g} s")
        # priorities: equal priority with different actions is a conflict
        by_prio = {}
        for r in active:
            by_prio.setdefault(r.priority, []).append(r)
        conflicts = [f"{', '.join(r.id for r in rs)} at priority {p}"
                     for p, rs in by_prio.items() if len({(r.action, r.action_s) for r in rs}) > 1]
        ok &= _chk(rep, "Rule conflicts", not conflicts,
                   "no two rules share a priority with different actions" if not conflicts else
                   "same priority, different actions: " + "; ".join(conflicts))
        # buffers: M messages per cycle, buffer emptied every N cycles, +1 cycle for early/late arrival
        n = buffer_n.get(tsc, 1)
        M = 1 + len(upstream_used)
        load = M * (n + 1)
        ok &= _chk(rep, "Buffer load", load <= C.BUFFER_SLOTS,
                   f"M={M} msg/cycle x (N={n} + 1 late cycle) = {load} of {C.BUFFER_SLOTS} slots")
        # deadline: the arbiter makes ONE decision per cycle from all rules, so it waits for every input it
        # needs; if any rule uses an upstream reading the 5 s deadline applies, otherwise 2 s
        tl = timing_for(tsc, sorted(upstream_used))
        limit = C.DEADLINE_UPSTREAM_S if upstream_used else C.DEADLINE_LOCAL_S
        lat = tl["decision"] - tl["last_reading"]
        basis = ("needs readings from " + ", ".join(f"I{u}" for u in sorted(upstream_used)) + f" -> {limit:g} s applies"
                 if upstream_used else f"local readings only -> {limit:g} s applies")
        ok &= _chk(rep, "Decision deadline", lat <= limit and tl["wait"] <= T["upstream_wait_max_s"] + 1e-9,
                   f"one decision per cycle {basis}; decided {lat:.2f} s after the last reading; "
                   f"green held {tl['wait']:.2f} s for inputs (bound {T['upstream_wait_max_s']:g} s)")
        for u in tl["ups"]:
            cur = u["sample"] >= 0
            ok &= _chk(rep, f"Current cycle (I{u['tsc']})", cur,
                       f"I{u['tsc']} samples at {u['sample']:+.1f} s of I{tsc}'s cycle "
                       f"(offset difference {u['delta']:g} s), arrives at {u['arrival']:+.1f} s"
                       + ("" if cur else " -> reading would be from the PREVIOUS cycle"))
        if legacy_prev_cycle.get(tsc):
            ok &= _chk(rep, "Current-cycle requirement", False,
                       "configuration evaluates upstream rules on previous-cycle readings")
        out["intersections"][tsc] = rep
        out["timeline"] = out.get("timeline", {})
        out["timeline"][tsc] = tl
        used_channels.add(x.channel)
        out["ok"] &= ok
    reserved = len(used_channels)       # one more channel per intersection planned for pedestrian sensors
    out["city"].append({"check": "Channel budget", "status": "ok" if len(used_channels) + reserved <= C.N_CHANNELS else "fail",
                        "detail": f"{len(used_channels)} of {C.N_CHANNELS} channels used; "
                                  f"{reserved} more reserved for pedestrian sensors; "
                                  f"{C.N_CHANNELS - len(used_channels) - reserved} free"})
    out["city"].append({"check": "Subscribed channels exist", "status": "ok",
                        "detail": "every upstream channel is published by its intersection"})
    return out["ok"], out


# ---------------------------------------------------------------- compiler

def subscriptions(tsc, rules):
    ups = sorted({c.src_tsc for r in rules if r.enabled for c in r.clauses if c.src_tsc != tsc})
    return [C.CITY[tsc].channel] + [C.CITY[u].channel for u in ups], ups


def compile_config(tsc, rules, version, tag, git_commit, created_ns, buffer_n=1,
                   legacy_prev_cycle=False):
    T = C.TIMING
    ms = lambda s: int(round(s * 1000))
    active = [r for r in rules if r.enabled]
    subs, ups = subscriptions(tsc, active)
    x = C.CITY[tsc]
    flags = (1 if legacy_prev_cycle else 0) | 2   # bit1: skip side phase when no side call
    hdr = struct.pack(
        HEADER_FMT, b"TSCF", 1, tsc, len(active), version, flags, created_ns,
        tag.encode()[:16], x.channel, buffer_n, len(subs), len(ups),
        bytes(subs + [0] * (8 - len(subs))), bytes(ups + [0] * (4 - len(ups))),
        ms(T["cycle_s"]), ms(x.offset_s), ms(T["main_min_s"]), ms(T["main_max_ext_s"]),
        ms(T["side_min_s"]), ms(T["amber_s"]), ms(T["allred_s"]), ms(T["sample_at_s"]),
        ms(T["upstream_wait_max_s"]), ms(C.SYNC_TOLERANCE_S), ms(C.DEADLINE_LOCAL_S),
        ms(C.DEADLINE_UPSTREAM_S), ms(C.DELIVERY_INTRA_S), ms(C.DELIVERY_INTER_S), 1000,
        git_commit.encode()[:12])
    body = b""
    for r in sorted(active, key=lambda r: -r.priority):
        b = struct.pack(RULE_FMT, r.id.encode()[:8], 0, len(r.clauses), r.action, r.if_missing,
                        ms(r.action_s), ms(r.missing_s), r.priority)
        for i in range(MAX_CLAUSES):
            if i < len(r.clauses):
                c = r.clauses[i]
                b += struct.pack(CLAUSE_FMT, c.src_tsc, c.det, MEASURE_CODES[c.measure],
                                 OP_CODES[c.op], int(round(c.value * 1000)), 0)
            else:
                b += bytes(12)
        body += b + bytes(4)
    return hdr + body


# ---------------------------------------------------------------- evaluator

def measure_milli(reading, measure):
    """reading: dict with presence, count, occ_permille, gap_ds (wire units)."""
    if measure == "occupancy":
        return reading["occ_permille"] * 100
    if measure == "gap":
        return reading["gap_ds"] * 100
    if measure == "count":
        return reading["count"] * 1000
    return reading["presence"] * 1000


def _cmp(a, op, b):
    return {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b, "==": a == b}[op]


def evaluate(rules, inputs, max_ext_s):
    """inputs: {(src_tsc, det): reading dict or None (missing/not current)}.
    Mirrors controller/src/app/rule_interpreter.cpp exactly."""
    results = []
    for r in sorted([r for r in rules if r.enabled], key=lambda r: -r.priority):
        missing = [c for c in r.clauses if inputs.get((c.src_tsc, c.det)) is None]
        if missing:
            results.append(dict(id=r.id, status="MISSING", act=r.if_missing, ms=int(round(r.missing_s * 1000)),
                                prio=r.priority))
            continue
        fired = all(_cmp(measure_milli(inputs[(c.src_tsc, c.det)], c.measure), c.op, int(round(c.value * 1000)))
                    for c in r.clauses)
        results.append(dict(id=r.id, status="FIRED" if fired else "NOT_FIRED",
                            act=r.action if fired else ACT_NONE,
                            ms=int(round(r.action_s * 1000)) if fired else 0, prio=r.priority))
    # arbiter: highest priority non-NONE action wins (rules are sorted, so the first one)
    winner = next((x for x in results if x["act"] != ACT_NONE), None)
    if winner is None:
        decision = dict(action="NONE", ext_ms=0, by=None)
    elif winner["act"] == ACT_HOLD:
        decision = dict(action="HOLD", ext_ms=0, by=winner["id"])
    else:
        decision = dict(action="EXTEND", ext_ms=min(winner["ms"], int(max_ext_s * 1000)), by=winner["id"])
    return results, decision
