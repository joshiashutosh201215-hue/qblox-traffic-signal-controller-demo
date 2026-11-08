"""Control center (standard Linux server, Python).

Plans, validates, deploys, monitors and audits. It is never in a live path:
not for decisions, not for failure handling, not for time.

  REST + WebSocket for the GUI on :8080
  MQTT client 'control-center' over mutual TLS to the broker (:8883)
"""
import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import ssl
import time
from collections import deque

import paho.mqtt.client as mqtt
from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from common import city as C
from common import rules as R
from control_center import release as REL

ROOT = REL.ROOT
RT = REL.RT
GUI = os.path.join(ROOT, "gui")
logging.Formatter.converter = time.gmtime
logging.basicConfig(filename=os.path.join(RT, "logs", "cc.log"), level=logging.INFO,
                    format="%(asctime)s.%(msecs)03dZ CC %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
log = logging.getLogger("cc")
NOMINAL_ONE_WAY_S = C.CENTER_RTT_S / 2


class Center:
    def __init__(self):
        self.health, self.status = {}, {t: "unknown" for t in C.CITY}
        self.alarms = deque(maxlen=500)
        self.deployments = []
        self.pending = {}               # tsc -> version to stage when it comes back online
        self.clients = set()
        self.loop = None
        self.db = sqlite3.connect(os.path.join(RT, "cc", "records.db"), check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS records (rec TEXT PRIMARY KEY, tsc INT, seq INT, t_ns INT,
                           hash TEXT, prev TEXT, hash_ok INT, chain_ok INT, raw TEXT, rx_ns INT)""")
        self.db.execute("CREATE INDEX IF NOT EXISTS rec_tsc ON records(tsc, seq)")
        self.db.commit()

    # ---------------------------------------------------------------- MQTT
    def start_mqtt(self):
        pki = os.path.join(RT, "pki")
        # persistent session: the broker queues QoS 1 records/alarms while the control center itself is down
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="control-center", clean_session=False)
        c.tls_set(ca_certs=f"{pki}/ca.crt", certfile=f"{pki}/control-center.crt", keyfile=f"{pki}/control-center.key",
                  tls_version=ssl.PROTOCOL_TLS_CLIENT)
        c.on_connect = lambda cl, u, f, rc, p=None: (cl.subscribe("field/#", qos=1),
                                                     log.info("MQTT connected to broker (mTLS, CN=control-center)"))
        c.on_message = lambda cl, u, m: self.loop.call_soon_threadsafe(self.on_mqtt, m.topic, m.payload)
        c.connect_async("127.0.0.1", 8883, keepalive=30)
        c.loop_start()
        self.mqtt = c

    def publish(self, topic, payload, qos=1):
        self.mqtt.publish(topic, payload, qos=qos)

    def push(self, msg):
        data = json.dumps(msg)
        for ws in list(self.clients):   # callable from sync endpoints too (they run in a worker thread)
            asyncio.run_coroutine_threadsafe(self._send(ws, data), self.loop)

    async def _send(self, ws, data):
        try:
            await ws.send_text(data)
        except Exception:
            self.clients.discard(ws)

    def on_mqtt(self, topic, payload):
        parts = topic.split("/")
        if parts[0] != "field" or len(parts) < 3 or not parts[1].startswith("tsc"):
            return
        t = int(parts[1][3:])
        sub = "/".join(parts[2:])
        now = time.time_ns()
        if sub == "status":
            st = payload.decode()
            if st != self.status.get(t):
                log.info("I%d %s", t, st.upper())
                self.push(dict(type="status", tsc=t, status=st))
            self.status[t] = st
            if st == "online" and t in self.pending:
                asyncio.ensure_future(self.deploy(self.pending.pop(t), [t], late=True))
        elif sub == "health":
            h = json.loads(payload)
            raw = (h["sync"]["local_utc_ns"] - now) / 1e9
            h["cc"] = dict(rx_ns=now, raw_diff_s=round(raw, 3), est_offset_s=round(raw + NOMINAL_ONE_WAY_S, 3),
                           uncertainty_s=NOMINAL_ONE_WAY_S)
            self.health[t] = h
            self.status[t] = "online"
            self.push(dict(type="health", tsc=t, h=h))
        elif sub == "alarm":
            a = json.loads(payload)
            a.update(tsc=t, rx_ns=now)
            self.alarms.append(a)
            log.info("ALARM I%d %s %s: %s", t, a["sev"], a["code"], a["text"])
            self.push(dict(type="alarm", a=a))
        elif sub == "records":
            self.store_records(t, payload.decode())
        elif sub == "cfg/ack":
            self.on_ack(t, json.loads(payload))
        elif sub == "cfg/activated":
            self.on_activated(t, json.loads(payload))

    # ---------------------------------------------------------------- records + audit
    def store_records(self, t, text):
        dec = json.JSONDecoder()
        i, n = 1, 0
        while i < len(text):
            while i < len(text) and text[i] in " ,\n]":
                i += 1
            if i >= len(text):
                break
            obj, end = dec.raw_decode(text, i)
            raw = text[i:end]
            i = end
            cut = raw.rfind(',"hash":"')
            hash_ok = hashlib.sha256((raw[:cut] + "}").encode()).hexdigest() == obj.get("hash")
            row = self.db.execute("SELECT hash FROM records WHERE tsc=? AND seq=?", (t, obj["seq"] - 1)).fetchone()
            chain_ok = None if row is None else int(row[0] == obj.get("prev"))
            self.db.execute("INSERT OR IGNORE INTO records VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (obj["rec"], t, obj["seq"], obj["t_ns"], obj.get("hash"), obj.get("prev"),
                             int(hash_ok), chain_ok, raw, time.time_ns()))
            n += 1
            self.push(dict(type="record", tsc=t, rec=obj["rec"], t_utc=obj["t_utc"], decision=obj["decision"],
                           mode=obj["mode"], sync=obj["sync"], hash_ok=hash_ok, chain_ok=chain_ok,
                           delay_s=round((time.time_ns() - obj["t_ns"]) / 1e9, 1)))
        self.db.commit()
        if n:
            log.info("I%d uploaded %d decision record(s)", t, n)

    def records(self, tsc=None, limit=50, before=None):
        q, a = "SELECT raw, hash_ok, chain_ok, rx_ns FROM records", []
        w = []
        if tsc:
            w.append("tsc=?"); a.append(tsc)
        if before:
            w.append("t_ns<?"); a.append(before)
        if w:
            q += " WHERE " + " AND ".join(w)
        q += " ORDER BY t_ns DESC LIMIT ?"
        a.append(limit)
        out = []
        for raw, h, c, rx in self.db.execute(q, a):
            o = json.loads(raw)
            o["_hash_ok"], o["_chain_ok"], o["_rx_delay_s"] = bool(h), c, round((rx - o["t_ns"]) / 1e9, 1)
            out.append(o)
        return out

    def trace(self, rec_id):
        row = self.db.execute("SELECT raw, hash_ok, chain_ok, rx_ns FROM records WHERE rec=?", (rec_id,)).fetchone()
        if not row:
            raise HTTPException(404, "record not found (not uploaded yet?)")
        rec = json.loads(row[0])
        m = REL.manifest(rec["cfg"]["ver"]) or {}
        rules = {r["id"]: r for r in m.get("rules", {}).get(str(rec["tsc"]), [])}
        last_change = {}
        if m.get("git_commit"):
            out = REL.git("log", "-1", "--date=iso-strict", "--format=%h|%an|%ad|%s", m["git_commit"], "--",
                          f"I{rec['tsc']:02d}.rules", check=False).strip()
            if out:
                h, a, d, s = out.split("|", 3)
                last_change = dict(commit=h, author=a, date=d, message=s)
        return dict(record=rec, hash_ok=bool(row[1]), chain_ok=row[2], rx_delay_s=round((row[3] - rec["t_ns"]) / 1e9, 1),
                    release={k: m.get(k) for k in ("version", "tag", "git_commit", "author", "approver", "created_utc", "note")},
                    rules=rules, last_change=last_change,
                    explanation=self.explain(rec, rules, m, last_change, bool(row[1]), row[2]))

    @staticmethod
    def explain(rec, rules, m, lc, hash_ok, chain_ok):
        d = rec["decision"]
        inputs = {(i["src"], i["det"]): i for i in rec["inputs"]}
        act = {"EXTEND": f"extend the main green by {d['ext_s']} s", "HOLD": "not extend the main green (hold)",
               "NONE": "not extend the main green"}[d["action"]]
        L = [f"At {rec['t_utc']} intersection I{rec['tsc']} decided to {act} (mode {rec['mode']}; clock {rec['sync']}, "
             f"error bound {rec['sync_bound_ms']} ms)."]
        if rec.get("fixed_reason"):
            L.append(f"Local fixed-time plan in force because: {rec['fixed_reason']}.")

        def clause_facts(r):
            facts = []
            for c, txt in zip(r["clauses"], r["when_text"]):
                i = inputs.get((c["src_tsc"], c["det"]))
                if i and i["status"] == "OK":
                    val = {"occupancy": f"{i['occ_pct']} %", "gap": f"{i['gap_s']} s", "count": f"{i['count']} veh",
                           "presence": str(i["presence"])}[c["measure"]]
                    facts.append(f"{txt}: measured {val} (I{i['src']} D{i['det']}, sampled {i['sample_utc']}, "
                                 f"{i['age_s']} s before the decision, seq {i['seq']})")
                elif i:
                    facts.append(f"{txt}: NOT AVAILABLE - {i.get('why', 'missing')}")
            return facts

        for rr in rec["rules"]:
            r = rules.get(rr["id"])
            title = f"{rr['id']} '{r['title']}'" if r else rr["id"]
            if rr["id"] == d["by"]:
                verb = "fired" if rr["status"] == "FIRED" else "had missing inputs and applied its fallback"
                L.append(f"Deciding rule: {title} (priority {rr['prio']}) {verb} -> {rr['result']}.")
                for f in clause_facts(r) if r else []:
                    L.append(f"    - {f}")
            elif rr["status"] == "MISSING":
                L.append(f"{title} (priority {rr['prio']}) took its fallback ({rr['result']}): {rr.get('missing')}.")
            else:
                L.append(f"{title} (priority {rr['prio']}) {rr['status'].replace('_', ' ').lower()}.")
                if r and rr["status"] == "NOT_FIRED":
                    for f in clause_facts(r):
                        L.append(f"    - {f}")
        if d["by"] == "none":
            L.append("No rule requested an action, so the base split applied.")
        if "latency_s" in d:
            L.append(f"Timing: decided {d['latency_s']} s after the last reading it used; deadline {d['deadline_s']} s "
                     f"({d.get('deadline_basis', '')}) "
                     f"-> {'MET' if d['deadline_met'] else 'MISSED'}."
                     + (f" Green was held {d['waited_s']} s for an in-flight upstream reading." if d["waited_s"] > 0.1 else ""))
        if m:
            L.append(f"Configuration v{m['version']} ({m['tag']}, hash {rec['cfg']['hash']}) compiled from Git commit "
                     f"{m['git_commit']}, released {m['created_utc']} by {m['author']}, approved by {m['approver']}.")
        if lc:
            L.append(f"This intersection's rules were last changed in {lc['commit']} by {lc['author']} on {lc['date']}: "
                     f"\"{lc['message']}\".")
        if rec["cfg"].get("buffer_n", 1) > 1:
            L.append(f"WARNING: the receive buffer was emptied only every {rec['cfg']['buffer_n']} cycles (proposal "
                     "value) - messages can be lost silently and readings can be stale.")
        if rec["cfg"].get("legacy_prev_cycle"):
            L.append("WARNING: the controller ran in PROPOSAL mode (previous-cycle upstream readings) - this decision "
                     "violates the current-cycle requirement.")
        L.append(f"Integrity: record hash {'verified' if hash_ok else 'MISMATCH'}; chain to the previous record "
                 f"{'intact' if chain_ok else 'not checked (previous record not received)' if chain_ok is None else 'BROKEN'}.")
        return L

    # ---------------------------------------------------------------- deployment
    def _dep(self, version, targets, late=False):
        dep = dict(id=len(self.deployments) + 1, version=version, late=late, created=time.time(), phase="staging",
                   at_ns=None, targets={t: dict(state="pending", detail="") for t in targets}, log=[])
        self.deployments.append(dep)
        return dep

    def dlog(self, dep, text):
        dep["log"].append(dict(t=time.time(), text=text))
        log.info("DEPLOY #%d v%d: %s", dep["id"], dep["version"], text)
        self.push(dict(type="deploy", dep=dep))

    def on_ack(self, t, a):
        for dep in reversed(self.deployments):
            x = dep["targets"].get(t)
            if not x or dep["phase"] in ("complete", "aborted"):
                continue
            ok = a["ok"]
            if a["op"] == "stage" and x["state"] == "staging":
                x["state"] = "staged" if ok else "rejected"
                x["detail"] = a["result"]
                self.dlog(dep, f"I{t} {a['result']}")
            elif a["op"] == "cmd" and x["state"] in ("scheduling",):
                x["state"] = "scheduled" if ok else "rejected"
                x["detail"] = a["result"]
                if "ALREADY_ACTIVE" in a["result"]:
                    x["state"] = "active"
                self.dlog(dep, f"I{t} {a['result']}")
            else:
                continue
            return

    def on_activated(self, t, a):
        for dep in reversed(self.deployments):
            x = dep["targets"].get(t)
            if x and x["state"] in ("scheduled", "scheduling") and dep["version"] == a["ver"]:
                x["state"] = "active"
                x["detail"] = f"active from cycle {a['cycle']} at {a['t_utc']} ({a['after_schedule_s']:+.1f} s after T: next cycle boundary)"
                self.dlog(dep, f"I{t} {x['detail']}")
                return

    async def wait_states(self, dep, from_state, timeout):
        end = time.time() + timeout
        while time.time() < end:
            if not any(x["state"] == from_state for x in dep["targets"].values()):
                return True
            await asyncio.sleep(0.25)
        return False

    async def deploy(self, version, targets, lead_s=20.0, late=False):
        blob = {t: REL.config_blob(version, t) for t in targets}
        dep = self._dep(version, targets, late)
        self.dlog(dep, f"{'late join: ' if late else ''}staging signed configuration v{version} on {len(targets)} controller(s)")
        for t in targets:
            if self.status.get(t) != "online":
                dep["targets"][t] = dict(state="offline", detail="center link down: queued, will be staged on reconnect")
                self.pending[t] = version
                continue
            dep["targets"][t]["state"] = "staging"
            self.publish(f"center/tsc{t:02d}/cfg/stage", blob[t])
        await self.wait_states(dep, "staging", 45)
        for t, x in dep["targets"].items():
            if x["state"] == "staging":
                x.update(state="timeout", detail="no acknowledgement within 45 s")
        bad = [t for t, x in dep["targets"].items() if x["state"] in ("rejected", "timeout")]
        if bad:
            dep["phase"] = "aborted"
            self.dlog(dep, "ABORTED: " + ", ".join(f"I{t}" for t in bad) + " did not stage; nothing is activated")
            return dep
        go = [t for t, x in dep["targets"].items() if x["state"] == "staged"]
        if not go:
            dep["phase"] = "complete"
            self.dlog(dep, "nothing to activate")
            return dep
        at = time.time_ns() + int(lead_s * 1e9)
        dep["at_ns"] = at
        dep["phase"] = "scheduling"
        self.dlog(dep, f"all {len(go)} connected controller(s) confirmed; activation together at "
                       f"{time.strftime('%H:%M:%S', time.gmtime(at / 1e9))} UTC (each at its next cycle boundary)")
        for t in go:
            dep["targets"][t]["state"] = "scheduling"
            self.publish(f"center/tsc{t:02d}/cfg/cmd", REL.activation_command(t, 1, version, at))
        await self.wait_states(dep, "scheduling", 45)
        dep["phase"] = "activating"
        self.push(dict(type="deploy", dep=dep))
        await self.wait_states(dep, "scheduled", lead_s + C.CYCLE_S + C.CENTER_RTT_S + 15)
        n_ok = sum(x["state"] == "active" for x in dep["targets"].values())
        dep["phase"] = "complete" if n_ok == len(dep["targets"]) else "partial"
        self.dlog(dep, f"{n_ok}/{len(dep['targets'])} active")
        return dep

    async def rollback(self, targets, lead_s=20.0):
        at = time.time_ns() + int(lead_s * 1e9)
        out = {}
        for t in targets:
            h = self.health.get(t)
            if not h:
                out[t] = "no health data"
                continue
            cfg = h["cfg"]
            other = cfg["ver_b"] if chr(cfg["slot"]) == "A" else cfg["ver_a"]
            if not other:
                out[t] = "no previous configuration in the other slot"
                continue
            self.publish(f"center/tsc{t:02d}/cfg/cmd", REL.activation_command(t, 2, other, at))
            out[t] = f"rollback to v{other} scheduled"
        log.info("ROLLBACK %s", out)
        return out


cc = Center()
app = FastAPI(title="Traffic Control Center")


@app.on_event("startup")
async def startup():
    cc.loop = asyncio.get_running_loop()
    cc.start_mqtt()
    log.info("control center up: REST/WS :8080, MQTT mTLS :8883")

    async def beacon():
        while True:
            await asyncio.sleep(1.0)
            cc.publish("center/time", f"{time.time():.3f}", qos=0)
            cc.push(dict(type="cc_time", t=time.time()))
    asyncio.ensure_future(beacon())


@app.middleware("http")
async def revalidate_console(request, call_next):
    # Without Cache-Control, browsers guess a freshness lifetime from Last-Modified and keep serving an
    # old index.html / app.js after the GUI is updated (e.g. the Help tab never appears). "no-cache"
    # makes the browser re-check every load; the ETag keeps that check cheap (304 when unchanged).
    resp = await call_next(request)
    p = request.url.path
    if p == "/" or p.startswith(("/gui/", "/docs/")):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/")
def index():
    return FileResponse(os.path.join(GUI, "index.html"))


app.mount("/gui", StaticFiles(directory=GUI), name="gui")
app.mount("/docs", StaticFiles(directory=os.path.join(ROOT, "docs")), name="docs")
os.makedirs(os.path.join(ROOT, "media"), exist_ok=True)
app.mount("/media", StaticFiles(directory=os.path.join(ROOT, "media")), name="media")


def _github_lists(text):
    """Python-Markdown needs a blank line before a list and 4-space nesting; GitHub needs neither."""
    import re
    out, fence, prev = [], False, ""
    item = re.compile(r"^(\s*)([-*]|\d+\.)\s")
    for line in text.split("\n"):
        if line.startswith("```"):
            fence = not fence
        m = None if fence else item.match(line)
        if m:
            if 0 < len(m.group(1)) < 4:
                line = "    " + line.lstrip()
            if prev.strip() and not item.match(prev) and not prev.startswith((" ", "\t")):
                out.append("")
        out.append(line)
        prev = line
    return "\n".join(out)


@app.get("/api/guide")
def guide():
    """docs/GUIDE.md rendered for the GUI's Help tab (the same file GitHub renders)."""
    text = _github_lists(open(os.path.join(ROOT, "docs", "GUIDE.md"), encoding="utf-8").read())
    try:
        import markdown
        html = markdown.markdown(text, extensions=["tables", "fenced_code", "toc", "attr_list"])
    except ImportError:                 # python3-markdown missing: show the source instead
        import html as H
        html = f"<pre>{H.escape(text)}</pre>"
    return {"html": html.replace('src="img/', 'src="/docs/img/').replace('href="img/', 'href="/docs/img/')}


def _proc_info(pid):
    try:
        argv = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")[:-1]
        cwd = os.readlink(f"/proc/{pid}/cwd")
        st = dict(l.split(":", 1) for l in open(f"/proc/{pid}/status").read().splitlines() if ":" in l)
        start_ticks = int(open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[19])
        boot = float(open("/proc/uptime").read().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    return dict(pid=pid, argv=[a.decode(errors="replace") for a in argv], cwd=cwd,
                rss_kb=int(st.get("VmRSS", "0 kB").split()[0]), threads=int(st.get("Threads", "0")),
                state=st.get("State", "?").strip(), uptime_s=round(boot - start_ticks / os.sysconf("SC_CLK_TCK")))


@app.get("/api/system")
def system():
    """Every process of the demo, found in /proc (Linux), grouped by role."""
    out = dict(root=ROOT, broker=[], control_center=[], testbench=[], controllers=[], safety_units=[])
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        p = _proc_info(int(d))
        if not p or not p["argv"]:
            continue
        a0, cmd = p["argv"][0], " ".join(p["argv"])
        idx = p["argv"].index("--id") + 1 if "--id" in p["argv"] else 0
        if a0.endswith("controller/build/tsc") and a0.startswith(ROOT):
            out["controllers"].append(dict(p, tsc=int(p["argv"][idx])))
        elif a0.endswith("controller/build/mmu") and a0.startswith(ROOT):
            out["safety_units"].append(dict(p, tsc=int(p["argv"][idx])))
        elif a0.endswith("sbin/mosquitto") and ROOT in cmd:
            out["broker"].append(p)
        elif "control_center.server:app" in cmd and p["cwd"] == ROOT:
            out["control_center"].append(p)
        elif "testbench/bench.py" in cmd and p["cwd"] == ROOT:
            out["testbench"].append(p)
    for k in ("controllers", "safety_units"):
        out[k].sort(key=lambda x: x["tsc"])
    return out
os.makedirs(os.path.join(RT, "logs", "pcap"), exist_ok=True)
app.mount("/pcap", StaticFiles(directory=os.path.join(RT, "logs", "pcap")), name="pcap")


@app.get("/api/city")
def city():
    return C.city_json() | dict(engineers=list(REL.ENGINEERS), measures=list(R.MEASURE_CODES),
                                ops=list(R.OP_CODES), facts=dict(delivery_intra=C.DELIVERY_INTRA_S,
                                delivery_inter=C.DELIVERY_INTER_S, deadline_local=C.DEADLINE_LOCAL_S,
                                deadline_upstream=C.DEADLINE_UPSTREAM_S, rtt=C.CENTER_RTT_S, buffer=C.BUFFER_SLOTS))


@app.get("/api/state")
def state():
    return dict(health=cc.health, status=cc.status, alarms=list(cc.alarms)[-150:], deployments=cc.deployments[-10:],
                releases=REL.releases(), pending=cc.pending)


@app.get("/api/rules/{tsc}")
def get_rules(tsc: int):
    text = open(REL.rules_file(tsc)).read()
    try:
        rules = [r.to_dict() for r in R.parse_rules(text, tsc)]
        err = None
    except R.RuleError as e:
        rules, err = [], str(e)
    return dict(tsc=tsc, text=text, rules=rules, error=err, upstream=C.CITY[tsc].upstream,
                history=REL.history(tsc, 15))


@app.put("/api/rules/{tsc}")
def put_rules(tsc: int, body: dict = Body(...)):
    author = body.get("author") or "a.joshi"
    try:
        if "text" in body:
            text = body["text"]
        else:
            text = R.format_rules([R.rule_from_dict(d, tsc) for d in body["rules"]], tsc)
        commit, changed = REL.save_rules(tsc, text, author, body.get("message") or f"I{tsc}: rule table edited")
    except (R.RuleError, KeyError, ValueError) as e:
        raise HTTPException(400, str(e))
    log.info("RULES I%d saved by %s -> commit %s%s", tsc, author, commit, "" if changed else " (no change)")
    return dict(commit=commit, changed=changed, text=text, diff=REL.diff("HEAD~1", "HEAD", tsc) if changed else "")


@app.get("/api/history")
def history(tsc: int = 0):
    return REL.history(tsc or None, 40)


@app.post("/api/validate")
def validate():
    ok, rep = R.validate(REL.load_all())
    rep["timeline"] = {k: v for k, v in rep.get("timeline", {}).items()}
    log.info("VALIDATION %s", "PASSED" if ok else "FAILED")
    return rep


@app.post("/api/replay/{tsc}")
def replay(tsc: int):
    rels = REL.releases()
    h = cc.health.get(tsc)
    ver = h["cfg"]["ver"] if h else rels[-1]["version"]
    m = REL.manifest(ver)
    old = [R.rule_from_dict(d, tsc) for d in m["rules"][str(tsc)]]
    try:
        new = R.parse_rules(open(REL.rules_file(tsc)).read(), tsc)
    except R.RuleError as e:
        raise HTTPException(400, str(e))
    rows, same_as_recorded, changed = [], 0, 0
    for rec in cc.records(tsc, 200):
        if rec.get("fixed_reason") or rec["cfg"]["ver"] != ver:
            continue
        inputs = {}
        for i in rec["inputs"]:
            inputs[(i["src"], i["det"])] = None if i["status"] != "OK" else dict(
                presence=i["presence"], count=i["count"], occ_permille=round(i["occ_pct"] * 10), gap_ds=round(i["gap_s"] * 10))
        _, d_old = R.evaluate(old, inputs, C.TIMING["main_max_ext_s"])
        _, d_new = R.evaluate(new, inputs, C.TIMING["main_max_ext_s"])
        rd = rec["decision"]
        match = d_old["action"] == rd["action"] and d_old["ext_ms"] == round(rd["ext_s"] * 1000)
        same_as_recorded += match
        diff = (d_old["action"], d_old["ext_ms"]) != (d_new["action"], d_new["ext_ms"])
        changed += diff
        rows.append(dict(rec=rec["rec"], t_utc=rec["t_utc"], recorded=f"{rd['action']} {rd['ext_s']} s by {rd['by']}",
                         reference=f"{d_old['action']} {d_old['ext_ms'] / 1000} s by {d_old['by']}",
                         proposed=f"{d_new['action']} {d_new['ext_ms'] / 1000} s by {d_new['by']}",
                         changed=diff, match=match,
                         unrecorded=[f"I{c.src_tsc} D{c.det}" for r in new for c in r.clauses
                                     if (c.src_tsc, c.det) not in inputs]))
    return dict(tsc=tsc, active_version=ver, cycles=len(rows), decisions_changed=changed,
                reference_matches_recorded=same_as_recorded, rows=rows)


@app.post("/api/release")
def release(body: dict = Body(...)):
    try:
        m = REL.make_release(body.get("author"), body.get("approver"), body.get("note", ""))
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))
    log.info("RELEASE v%d %s commit %s author %s approver %s changed %s", m["version"], m["tag"], m["git_commit"],
             m["author"], m["approver"], m["changed"])
    m.pop("rules", None)
    cc.push(dict(type="releases", releases=REL.releases()))
    return m


@app.get("/api/releases")
def releases():
    return REL.releases()


@app.post("/api/deploy")
async def deploy(body: dict = Body(...)):
    v = int(body["version"])
    m = REL.manifest(v)
    if not m:
        raise HTTPException(404, "unknown release")
    tg = body.get("targets", "changed")
    if tg == "all":
        targets = list(C.CITY)
    elif tg == "changed":
        # only intersections whose rule set changed in this release (rule content hash), not yet running it
        targets = [t for t in C.CITY if m["files"][str(t)]["changed"]
                   and cc.health.get(t, {}).get("cfg", {}).get("ver") != v]
    else:
        targets = [int(x) for x in tg]
    if not targets:
        raise HTTPException(400, "no controller needs this release")
    asyncio.ensure_future(cc.deploy(v, targets, float(body.get("lead_s", 20))))
    return dict(started=True, version=v, targets=targets)


@app.post("/api/rollback")
async def rollback(body: dict = Body(...)):
    return await cc.rollback([int(x) for x in body["targets"]], float(body.get("lead_s", 20)))


@app.get("/api/records")
def records(tsc: int = 0, limit: int = 50):
    return cc.records(tsc or None, min(limit, 500))


@app.get("/api/trace/{rec_id}")
def trace(rec_id: str):
    return cc.trace(rec_id)


@app.get("/api/diff")
def diff(a: str, b: str = "HEAD", tsc: int = 0):
    return JSONResponse(dict(diff=REL.diff(a, b, tsc or None)))


@app.websocket("/ws")
async def ws(sock: WebSocket):
    await sock.accept()
    cc.clients.add(sock)
    try:
        while True:
            await sock.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        cc.clients.discard(sock)
