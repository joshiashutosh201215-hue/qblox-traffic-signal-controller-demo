// Operator console for the traffic-control demo. Two live sources:
//   control center (same origin): REST + /ws  -> what the city's back office knows (via MQTT, 10 s RTT)
//   HIL testbench  (:8091)                  -> the street, cabinet probes, fault injection (bench only)
"use strict";
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const api = async (method, url, body) => {
  const r = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || r.statusText);
  return j;
};
const hhmmss = (t) => new Date(t * 1000).toISOString().substr(11, 8);
const lamp = (L, h, l) => (L >> (h * 3 + l)) & 1;
const MODE_COL = { ACTUATED: "#3ddc84", DEGRADED: "#ffb648", FIXED_TIME: "#d27cff", FLASH: "#ff5d5d", STARTUP: "#8796a8" };

const S = {
  city: null, world: null, probes: {}, faults: {}, procs: {}, wan: {}, peer: {}, health: {}, status: {},
  alarms: [], deployments: [], releases: [], sel: 17, tab: "demo", anims: [], lastMsgT: 0, events: [],
  timeHist: {}, worldRx: 0, rules: null, engineers: [], benchWs: null, logStream: null, records: [],
};

// ------------------------------------------------------------------ connections
function connectCC() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen = () => $("#pill-cc").className = "pill ok";
  ws.onclose = () => { $("#pill-cc").className = "pill bad"; setTimeout(connectCC, 1500); };
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type === "health") { S.health[m.tsc] = m.h; S.status[m.tsc] = "online"; }
    else if (m.type === "status") S.status[m.tsc] = m.status;
    else if (m.type === "alarm") { S.alarms.push(m.a); S.alarms = S.alarms.slice(-300); tick(`I${m.a.tsc} ${m.a.sev.toUpperCase()} ${m.a.code}: ${m.a.text}`, m.a.sev); if (S.tab === "monitor") renderAlarms(); }
    else if (m.type === "record") { if (S.tab === "audit") onRecord(m); }
    else if (m.type === "deploy") { const i = S.deployments.findIndex((d) => d.id === m.dep.id); if (i >= 0) S.deployments[i] = m.dep; else S.deployments.push(m.dep); if (S.tab === "deploy") renderDeploy(); }
    else if (m.type === "releases") { S.releases = m.releases; if (S.tab === "deploy") renderDeploy(); }
    else if (m.type === "cc_time") $("#utc").textContent = new Date(m.t * 1000).toISOString().substr(11, 8) + " UTC";
  };
}

function connectBench() {
  const ws = new WebSocket(`ws://${location.hostname}:8091`);
  S.benchWs = ws;
  ws.onopen = () => { $("#pill-bench").className = "pill ok"; if (S.logStream) subLog(S.logStream); };
  ws.onclose = () => { $("#pill-bench").className = "pill bad"; setTimeout(connectBench, 1500); };
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type === "world") onWorld(m);
    else if (m.type === "probes") onProbes(m);
    else if (m.type === "bench_event") tick(m.text, "bench");
    else if (m.type === "hello") { fillStreams(m.streams); m.events.forEach((x) => S.events.push(x)); }
    else if (m.type === "log") onLog(m);
  };
}

function fault(kind, tsc, value) {
  if (S.benchWs && S.benchWs.readyState === 1) S.benchWs.send(JSON.stringify({ cmd: "fault", kind, tsc, value }));
}

function tick(text, sev) {
  const col = sev === "major" ? "bad-t" : sev === "warn" ? "warn-t" : sev === "bench" ? "info-t" : "ok-t";
  S.events.push({ t: Date.now() / 1000, text, col });
  S.events = S.events.slice(-60);
  $("#ticker").innerHTML = S.events.slice(-4).reverse().map((e) => `<span class="${e.col || ""}">${hhmmss(e.t)} ${esc(e.text)}</span>`).join(" &nbsp;·&nbsp; ");
}

// ------------------------------------------------------------------ tabs
document.querySelectorAll("#tabs button[data-tab]").forEach((b) => b.onclick = () => showTab(b.dataset.tab));
function showTab(t) {
  S.tab = t;
  document.querySelectorAll("#tabs button[data-tab]").forEach((b) => b.classList.toggle("on", b.dataset.tab === t));
  document.querySelectorAll(".tab").forEach((d) => d.classList.toggle("on", d.id === "tab-" + t));
  if (t === "rules") loadRules();
  if (t === "deploy") refreshDeploy();
  if (t === "monitor") { renderMonitor(); renderAlarms(); }
  if (t === "audit") loadRecords();
  if (t === "logs" && !S.logStream) subLog(`tsc${String(S.sel).padStart(2, "0")}.log`);
  if (t === "city") sizeCanvas();
  document.body.classList.toggle("wide", t === "help");
  if (t === "help") { loadGuide(); renderProcs(); }
}

// ------------------------------------------------------------------ city view
const cv = $("#world"), mctx = cv.getContext("2d"), zv = $("#zoom"), zctx = zv.getContext("2d");
let cx = mctx, scale = 1, ox = 0, oy = 0, mscale = 1, zsize = 300, ZOOM = false;
function sizeCanvas() {
  if (!S.world) return;
  const avail = (cv.parentElement.clientWidth || 900);
  const hmax = window.innerHeight - cv.getBoundingClientRect().top - 60;
  scale = Math.max(0.8, Math.min(avail / S.world.w, hmax / S.world.h));
  const w = S.world.w * scale;
  cv.style.width = w + "px";
  cv.width = w * devicePixelRatio; cv.height = S.world.h * scale * devicePixelRatio;
  cv.style.height = S.world.h * scale + "px";
  mctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
  mscale = scale;
  zsize = Math.max(240, Math.min(avail - w - 14, S.world.h * scale - 30, 560));
  zv.width = zv.height = zsize * devicePixelRatio; zv.style.width = zv.style.height = zsize + "px";
  zctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
}
window.addEventListener("resize", sizeCanvas);
const ctr = (t) => { const r = Math.floor((t - 1) / 5), c = (t - 1) % 5; return [S.world.margin + c * S.world.spacing, S.world.margin + r * S.world.spacing]; };
const P = (x, y) => [x * scale + ox, y * scale + oy];

function onWorld(m) {
  const first = !S.world;
  S.world = m; S.worldRx = performance.now();
  if (first) sizeCanvas();
  const now = Date.now() / 1000;
  for (const e of m.msgs || []) {
    if (e.t <= S.lastMsgT) continue;
    S.lastMsgT = Math.max(S.lastMsgT, e.t);
    for (const d of e.dsts) S.anims.push({ src: e.src, dst: d, kind: e.kind, start: e.t, dur: d === e.src ? 1 : 3 });
  }
  S.anims = S.anims.filter((a) => now - a.start < a.dur + 0.3);
  S.feed = S.feed || [];
  for (const e of m.msgs || []) {
    const t = S.sel;
    if (e.src === t || e.dsts.includes(t)) S.feed.unshift(e);
  }
  S.feed = S.feed.filter((e) => e.src === S.sel || e.dsts.includes(S.sel)).slice(0, 14);
  $("#ztitle").innerHTML = `Close-up <b>I${S.sel}</b> — loops D1–D8, heads W/E/N/S · channel traffic of I${S.sel}:`;
  $("#zfeed").innerHTML = S.feed.map((e) => {
    const out = e.src === S.sel, cls = e.kind !== "msg" ? "bad" : out ? "out" : "in";
    const d = e.dsts.filter((x) => x !== e.src).map((x) => "I" + x).join(",");
    return `<div class="${cls}">${hhmmss(e.t)} ${e.kind !== "msg" ? e.kind.toUpperCase() + " " : ""}ch${e.ch} seq ${e.seq} ${out ? `I${e.src} → self (1 s)${d ? ", " + d + " (3 s)" : ""}` : `I${e.src} → I${S.sel} (3 s)`} · clock ${e.sync}</div>`;
  }).join("");
  const st = m.stats;
  $("#kpis").innerHTML = `<span>vehicles <b>${st.vehicles}</b></span><span>waiting <b>${st.waiting}</b></span><span>avg speed <b>${st.avg_speed} km/h</b></span><span>avg stops/trip <b>${st.avg_stops}</b></span><span>msgs <b>${m.counts.chan}</b> · PTP <b>${m.counts.ptp}</b></span>`;
}

function headState(t, h) {
  const f = S.world && S.world.field[t];
  if (!f || Date.now() / 1000 - f.t > 1.5) return null;
  return { r: lamp(f.lamps, h, 0), a: lamp(f.lamps, h, 1), g: lamp(f.lamps, h, 2), flash: f.state !== 0 };
}

function draw() {
  requestAnimationFrame(draw);
  if (!S.world || S.tab !== "city") return;
  const W = S.world;
  cx = mctx; scale = mscale; ox = 0; oy = 0; ZOOM = false;
  render(W.w * scale, W.h * scale);
  const [xc, yc] = ctr(S.sel), zs = zsize / 72;
  cx = zctx; scale = zs; ox = zsize / 2 - xc * zs; oy = zsize / 2 - yc * zs; ZOOM = true;
  render(zsize, zsize);
  scale = mscale;
}

function render(cw, ch) {
  const W = S.world, now = Date.now() / 1000;
  cx.fillStyle = "#0d1a14"; cx.fillRect(0, 0, cw, ch);
  cx.fillStyle = "#13221a";
  for (let r = -1; r < 4; r++) for (let c = -1; c < 5; c++) {
    const [x, y] = P(W.margin + c * W.spacing + 9, W.margin + r * W.spacing + 9);
    cx.fillRect(x, y, (W.spacing - 18) * scale, (W.spacing - 18) * scale);
  }
  cx.fillStyle = "#2b3038";
  for (let r = 0; r < 4; r++) { const [, y] = P(0, W.margin + r * W.spacing - 7); cx.fillRect(0, y, cw, 14 * scale); }
  for (let c = 0; c < 5; c++) { const [x] = P(W.margin + c * W.spacing - 7, 0); cx.fillRect(x, 0, 14 * scale, ch); }
  cx.strokeStyle = "#6b6f55"; cx.setLineDash([6, 8]); cx.lineWidth = 1;
  for (let r = 0; r < 4; r++) { const [, y] = P(0, W.margin + r * W.spacing); cx.beginPath(); cx.moveTo(0, y); cx.lineTo(cw, y); cx.stroke(); }
  for (let c = 0; c < 5; c++) { const [x] = P(W.margin + c * W.spacing, 0); cx.beginPath(); cx.moveTo(x, 0); cx.lineTo(x, ch); cx.stroke(); }
  cx.setLineDash([]);
  for (let t = 1; t <= 20; t++) drawIntersection(t, now);
  cx.setLineDash([4, 4]); cx.strokeStyle = "#b78cff"; cx.lineWidth = 1.5;
  for (const [t, p] of Object.entries(S.probes)) {
    if (p.sync && p.sync.q === "PTP" && p.sync.master) {
      const [x1, y1] = P(...ctr(p.sync.master)), [x2, y2] = P(...ctr(+t));
      cx.beginPath(); cx.moveTo(x1, y1); cx.lineTo(x2, y2); cx.stroke();
    }
  }
  cx.setLineDash([]);
  const dt = Math.min((performance.now() - S.worldRx) / 1000, 0.25);
  const cols = ["#e8eef5", "#6ec8ff", "#ffcf5c", "#ff8a7a", "#9cf0b0", "#c9a7ff", "#f5f5a0"];
  for (const v of W.vehicles) {
    let [x, y, d, sp, c] = v;
    x += [1, -1, 0, 0][d] * sp * dt; y += [0, 0, -1, 1][d] * sp * dt;
    const [px, py] = P(x, y);
    if (px < -10 || py < -10 || px > cw + 10 || py > ch + 10) continue;
    cx.fillStyle = cols[c];
    const L = 4.5 * scale, Wd = 2.0 * scale;
    if (d < 2) cx.fillRect(px - L / 2, py - Wd / 2, L, Wd); else cx.fillRect(px - Wd / 2, py - L / 2, Wd, L);
  }
  for (const a of S.anims) {
    const f = (now - a.start) / a.dur;
    if (f < 0 || f > 1) continue;
    const [x1, y1] = P(...ctr(a.src)), [x2, y2] = P(...ctr(a.dst));
    const col = a.kind === "msg" ? "#4cc2ff" : "#ff4f8b";
    if (a.src === a.dst) {
      cx.strokeStyle = col; cx.globalAlpha = 1 - f; cx.lineWidth = 2;
      cx.beginPath(); cx.arc(x1, y1, 10 + f * 22, 0, 7); cx.stroke(); cx.globalAlpha = 1;
    } else {
      const x = x1 + (x2 - x1) * f, y = y1 + (y2 - y1) * f;
      cx.fillStyle = col; cx.beginPath(); cx.arc(x, y, a.kind === "msg" ? 3.5 : 5, 0, 7); cx.fill();
    }
  }
}

function drawIntersection(t, now) {
  const W = S.world, [xc, yc] = ctr(t), b = W.box, st = W.stop;
  const pr = S.probes[t], alive = pr && now - pr.rx < 2;
  // detector loops
  const pres = (W.presence[t] || []);
  const df = (W.det_faults || {})[t] || {};
  const loops = [[1, xc - st - 6, yc + 1.2, 5, 3.6], [2, xc + st + 1, yc - 4.8, 5, 3.6], [3, xc + 1.2, yc + st + 1, 3.6, 5], [4, xc - 4.8, yc - st - 6, 3.6, 5],
    [5, xc + b + 6, yc + 1.2, 5, 3.6], [6, xc - b - 11, yc - 4.8, 5, 3.6], [7, xc + 1.2, yc - b - 11, 3.6, 5], [8, xc - 4.8, yc + b + 6, 3.6, 5]];
  for (const [d, x, y, w, h] of loops) {
    const [px, py] = P(x, y);
    cx.strokeStyle = df[d] ? "#ff5d5d" : pres[d - 1] ? "#ffd84c" : "#555c66";
    cx.lineWidth = df[d] ? 2 : 1;
    if (pres[d - 1]) { cx.fillStyle = "rgba(255,216,76,.35)"; cx.fillRect(px, py, w * scale, h * scale); }
    cx.strokeRect(px, py, w * scale, h * scale);
    if (ZOOM && t === S.sel) { cx.fillStyle = df[d] ? "#ff7a7a" : "#c9d3de"; cx.font = "11px system-ui"; cx.fillText("D" + d, px + (w > h ? 0 : w * scale + 2), py + (w > h ? -3 : h * scale)); }
  }
  // box
  const [bx, by] = P(xc - b, yc - b);
  cx.fillStyle = "#30353e"; cx.fillRect(bx, by, 2 * b * scale, 2 * b * scale);
  // stop lines
  cx.strokeStyle = "#cfd4da"; cx.lineWidth = 2;
  const sl = [[xc - st, yc, xc - st, yc + 6.5], [xc + st, yc - 6.5, xc + st, yc], [xc, yc + st, xc + 6.5, yc + st], [xc - 6.5, yc - st, xc, yc - st]];
  for (const [a1, b1, a2, b2] of sl) { const [p1, q1] = P(a1, b1), [p2, q2] = P(a2, b2); cx.beginPath(); cx.moveTo(p1, q1); cx.lineTo(p2, q2); cx.stroke(); }
  // signal heads at the near-side stop line
  const heads = [[0, xc - st - 2, yc + 10], [1, xc + st + 2, yc - 10], [2, xc - 10, yc - st - 2], [3, xc + 10, yc + st + 2]];
  for (const [h, x, y] of heads) {
    const s = headState(t, h);
    let col = "#222";
    if (s) {
      if (s.flash) col = (s.a || s.r) ? (s.a ? "#ffb000" : "#ff3b3b") : "#3a2a10";
      else col = s.g ? "#2bff88" : s.a ? "#ffb000" : s.r ? "#ff3b3b" : "#222";
    }
    const [px, py] = P(x, y);
    cx.fillStyle = "#0a0d11"; cx.beginPath(); cx.arc(px, py, 5.5, 0, 7); cx.fill();
    cx.fillStyle = col; cx.shadowColor = col; cx.shadowBlur = col === "#222" ? 0 : 8;
    cx.beginPath(); cx.arc(px, py, 3.8, 0, 7); cx.fill(); cx.shadowBlur = 0;
    if (ZOOM && t === S.sel) { cx.fillStyle = "#8796a8"; cx.font = "10px system-ui"; cx.fillText("WENS"[h], px + 7, py + 3); }
  }
  // badge
  const [px, py] = P(xc + b + 10, yc - b - 10);
  const mode = alive ? pr.mode : "OFFLINE";
  cx.lineWidth = t === S.sel ? 3 : 2;
  cx.strokeStyle = alive ? (MODE_COL[mode] || "#888") : "#555";
  cx.fillStyle = t === S.sel ? "#203348" : "#121a24";
  cx.beginPath(); cx.roundRect(px - 16, py - 11, 46, 22, 6); cx.fill(); cx.stroke();
  cx.fillStyle = "#e6eef7"; cx.font = "bold 11px system-ui"; cx.fillText("I" + t, px - 12, py + 4);
  if (alive) {
    const q = pr.sync.q;
    cx.fillStyle = q === "GNSS" ? "#3ddc84" : q === "PTP" ? "#b78cff" : q === "HOLDOVER" ? "#ffb648" : "#ff5d5d";
    cx.font = "bold 9px system-ui"; cx.fillText(q[0], px + 9, py + 4);
    cx.fillStyle = pr.mqtt ? "#4cc2ff" : "#555"; cx.fillText("⇅", px + 18, py + 4);
  } else {
    cx.fillStyle = "#ff5d5d"; cx.font = "bold 9px system-ui"; cx.fillText("✕", px + 12, py + 4);
  }
}

cv.addEventListener("click", (e) => {
  if (!S.world) return;
  const r = cv.getBoundingClientRect(), x = (e.clientX - r.left) / mscale, y = (e.clientY - r.top) / mscale;
  let best = 0, bd = 1e9;
  for (let t = 1; t <= 20; t++) { const [a, b] = ctr(t); const d = Math.hypot(a - x, b - y); if (d < bd) { bd = d; best = t; } }
  if (bd < 45) selectTsc(best);
});

function selectTsc(t) { S.sel = t; renderCabinet(); }

// ------------------------------------------------------------------ probes + cabinet
function onProbes(m) {
  S.probes = m.probes; S.faults = m.faults; S.procs = m.procs; S.wan = m.wan; S.peer = m.peer;
  const now = Date.now() / 1000;
  for (const [t, p] of Object.entries(m.probes)) {
    const h = (S.timeHist[t] = S.timeHist[t] || []);
    h.push([now, p.truth_offset_ms, p.sync.bound_ms, p.sync.q]);
    if (h.length > 300) h.shift();
  }
  const online = Object.values(S.status).filter((s) => s === "online").length;
  $("#pill-mqtt").textContent = `MQTT ${online}/20`;
  $("#pill-mqtt").className = "pill " + (online === 20 ? "ok" : online ? "" : "bad");
  renderCabinet();
  if (S.tab === "time") renderTime();
  if (S.tab === "monitor") renderMonitor();
}

const btn = (label, kind, value, cls = "") => `<button class="${cls}" data-f="${kind}" data-v='${JSON.stringify(value ?? null)}'>${label}</button>`;

function renderCabinet() {
  const t = S.sel, p = S.probes[t], now = Date.now() / 1000;
  const x = S.city ? S.city.intersections.find((i) => i.id === t) : null;
  const f = (S.world && S.world.field[t]) || {};
  const fl = S.faults[t] || {};
  const proc = S.procs[`tsc${String(t).padStart(2, "0")}`] || {};
  const hd = (h, n) => { const s = headState(t, h) || {}; const on = (k) => (s.flash ? (s[k] ? "on" : "") : s[k] ? "on" : ""); return `<div class="head"><span class="lamp r ${on("r")}"></span><span class="lamp a ${on("a")}"></span><span class="lamp g ${on("g")}"></span><b>${n}</b></div>`; };
  let h = `<div class="cab-title"><h2>Cabinet I${t}</h2><span>${x ? `row ${x.row + 1} · col ${x.col + 1} · ` : ""}${x && x.upstream.length ? "upstream " + x.upstream.map((u) => "I" + u).join(", ") : "corridor head (no upstream)"}</span></div>`;
  if (!p || now - p.rx > 2) {
    h += `<div class="box"><b class="bad-t">Controller not running</b> <span class="muted">(${esc(proc.state || "?")}, boots: ${proc.starts || 0})</span></div>`;
  }
  const mmuFlash = f.state === 2, startFlash = f.state === 1;
  h += `<div class="box"><h4>Signal heads — field outputs <span>${mmuFlash ? '<span class="chip bad">SAFETY UNIT: FLASH</span>' : startFlash ? '<span class="chip warn">start-up flash</span>' : '<span class="chip ok">safety unit normal</span>'}</span></h4>
    <div class="row"><div class="heads">${hd(0, "W")}${hd(1, "E")}${hd(2, "N")}${hd(3, "S")}</div>
    <div style="flex:1;font-size:12px">${mmuFlash ? `<span class="bad-t">${esc(f.fault)}</span><br>` : ""}<span class="muted">Independent C conflict monitor: conflicts, multiple indications, dark heads, amber ≥ 3 s, controller watchdog. Trips: ${f.trips ?? "-"}</span></div></div>
    <div class="row">${btn("Technician RESET", "mmu_reset", null, mmuFlash ? "ok" : "")}</div></div>`;
  if (p) {
    const c = p.cfg, sy = p.sync;
    const cyc = S.city ? S.city.timing.cycle_s : 40;
    const pct = (v) => (100 * v / cyc).toFixed(2) + "%";
    h += `<div class="box"><h4>Intersection controller — C++ on RTOS <span><span class="chip" style="color:${MODE_COL[p.mode]};border-color:${MODE_COL[p.mode]}">${p.mode}</span></span></h4>
      <div class="kv"><span>Phase</span><span><b>${esc(p.phase)}</b>${p.side_skipped ? ' <span class="chip info">side skipped: no call</span>' : ""}</span>
      <span>Cycle</span><span>#${p.cycle % 100000} · t = ${p.cycle_t.toFixed(1)} s / ${cyc} s</span></div>
      <div class="cyclebar" title="sample 13 s · decide 14 s · main ≤ 22 s · side ≥ 8 s"><div style="left:0;width:${pct(14)};background:#1f6b3f"></div><div style="left:${pct(14)};width:${pct(8)};background:#1f6b3f88"></div>
      <div style="left:${pct(30)};width:${pct(5)};background:#7a5a10"></div><div style="left:${pct(35)};width:${pct(5)};background:#7a5a10"></div>
      <div style="left:${pct(13)};width:2px;background:#4cc2ff"></div><div class="cur" style="left:${pct(p.cycle_t)}"></div></div>
      <div class="kv" style="margin-top:6px"><span>Last decision</span><span style="white-space:normal">${esc(p.records.last)}</span>
      <span>Config</span><span>v${c.ver} <span class="muted">${esc(c.tag)} · ${esc(c.hash)}</span> · slot ${String.fromCharCode(c.slot)} (A: v${c.ver_a}, B: v${c.ver_b})${c.pending ? ` <span class="chip warn">v${c.pending_ver} scheduled ${hhmmss(c.pending_at_ns / 1e9)}</span>` : ""}${c.legacy_prev_cycle ? ' <span class="chip bad">PROPOSAL MODE</span>' : ""}</span>
      <span>Firmware</span><span>${esc(p.fw)} · up ${Math.round(p.uptime_s)} s · boots ${proc.starts || 1}</span>
      <span>Heap after init</span><span class="${p.heap_allocs_after_init ? "bad-t" : "ok-t"}">${p.heap_allocs_after_init} C++ allocations</span></div>
      <div class="layers" style="margin-top:6px"><div class="layer"><b>Application</b> rule interpreter · decision arbiter · mode manager</div>
      <div class="layer"><b>Services</b> messaging · freshness/liveness · time · config A/B · recorder · health · device mgr · center agent</div>
      <div class="layer"><b>HAL</b> CANopen detector I/O · peer NIC (HW timestamps) · 1PPS/TCXO · flash · watchdog · crypto</div></div></div>`;
    const cards = p.cards.map((k) => `0x${k.node.toString(16).toUpperCase()} ${k.silent ? '<span class="bad-t">SILENT</span>' : k.nmt === 5 ? '<span class="ok-t">OPERATIONAL</span>' : "state " + k.nmt}`).join(" · ");
    const det = S.city ? S.city.detectors : {};
    h += `<div class="box"><h4>Detector cards — CANopen (CiA 301) <span class="muted" style="text-transform:none">${cards}</span></h4><div class="leds">
      ${p.det.map((d) => `<div class="led ${d.p ? "on" : ""} ${d.q !== "OK" ? "bad" : ""}" title="${esc(det[d.d])}"><i></i>D${d.d} ${d.q !== "OK" ? d.q : d.occ.toFixed(0) + "%"}</div>`).join("")}</div>
      <div class="muted" style="font-size:11px;margin-top:4px">TPDO1 0x19${1}/0x192 on change + 100 ms · heartbeat 0x711/0x712 1 s · EMCY 0x091/0x092 on loop faults</div></div>`;
    const truth = p.truth_offset_ms;
    h += `<div class="box"><h4>Time hardware <span>${sy.q === "GNSS" ? '<span class="chip ok">GNSS</span>' : sy.q === "PTP" ? '<span class="chip info">PTP</span>' : sy.q === "HOLDOVER" ? '<span class="chip warn">HOLDOVER</span>' : '<span class="chip bad">UNSYNC</span>'}</span></h4>
      <div class="kv"><span>State</span><span>${esc(sy.state)}</span>
      <span>Error bound</span><span>${sy.bound_ms < 0 ? "∞" : sy.bound_ms.toFixed(3) + " ms"} (tolerance 100 ms)</span>
      <span>Actual error</span><span class="${Math.abs(truth) > 100 ? "bad-t" : "ok-t"}">${truth.toFixed(3)} ms <span class="muted">(bench truth)</span></span>
      <span>GNSS receiver</span><span>${sy.gnss_sky ? "satellites in view" : '<span class="warn-t">no sky view</span>'}${sy.gnss_rejected ? ' <span class="chip bad">REJECTED: spoof suspected</span>' : ""}</span>
      <span>PTP</span><span>${sy.ptp_port ? "" : '<span class="bad-t">port DOWN</span> '}${sy.master ? `master I${sy.master}, offset ${sy.ptp_offset_ms.toFixed(3)} ms, delay ${sy.ptp_delay_ms.toFixed(3)} ms` : "no master"} · class ${sy.clock_class} · steps ${sy.steps}</span>
      <span>TCXO</span><span>${sy.freq_ppm.toFixed(2)} ppm correction${p.osc_ppm && Math.abs(p.osc_ppm) > 50 ? ` <span class="chip bad">crystal at ${p.osc_ppm} ppm</span>` : ""}</span></div></div>`;
    const b = p.buffer;
    h += `<div class="box"><h4>Peer network — channels <span class="muted" style="text-transform:none">MAC fail ${p.security.mac_fail} · replays ${p.security.replay}</span></h4>
      <table><tr><th>ch</th><th>from</th><th>liveness</th><th>age</th><th>seq</th><th>lost</th><th>sender clock</th></tr>
      ${p.neighbours.map((n) => `<tr><td>${n.ch}</td><td>I${n.tsc}${n.tsc === t ? " (own)" : ""}</td><td class="${n.live === "OK" ? "ok-t" : n.live === "SILENT" ? "bad-t" : "warn-t"}">${n.live}</td><td>${n.age_s < 0 ? "-" : n.age_s.toFixed(1) + " s"}</td><td>${n.seq}</td><td>${n.lost}</td><td>${n.sync}</td></tr>`).join("")}</table>
      <div class="row" style="margin:6px 0 0"><span class="muted">Platform buffer ${b.used}/${b.slots}, emptied every ${b.n} cycle(s)</span><div class="slots" style="flex:1">${Array.from({ length: 10 }, (_, i) => `<i class="${i < b.used ? "on" : ""}"></i>`).join("")}</div></div>
      ${b.lost_by_seq ? `<div class="bad-t">${b.lost_by_seq} message(s) lost (detected from sequence gaps — the platform drops silently)</div>` : ""}</div>`;
    const hc = S.health[t];
    const age = hc ? (now - hc.cc.rx_ns / 1e9) : null;
    h += `<div class="box"><h4>Center link — MQTT 3.1.1 over mutual TLS <span>${p.mqtt ? '<span class="chip ok">connected</span>' : '<span class="chip bad">down</span>'}</span></h4>
      <div class="kv"><span>Store &amp; forward</span><span>${p.records.backlog} record(s), ${p.alarms_queued} alarm(s) waiting · ${p.records.seq} records on flash</span>
      <span>CC last heard</span><span>${hc ? age.toFixed(1) + " s ago" : "never"}${hc ? ` · CC clock estimate ${hc.cc.est_offset_s >= 0 ? "+" : ""}${hc.cc.est_offset_s.toFixed(2)} s ± ${hc.cc.uncertainty_s} s` : ""}</span></div>
      <div class="muted" style="font-size:11px">The center only observes: its offset estimate is ±5 s (half the 10 s round trip), so it can never set controller clocks.</div></div>`;
  }
  h += `<div class="box"><h4>Fault injection — bench <span class="muted" style="text-transform:none">${Object.keys(fl).length ? Object.entries(fl).map(([k, v]) => `<span class="chip bad">${esc(k)}: ${esc(v)}</span>`).join("") : "none active"}</span></h4>
    <div class="sect">Detectors</div><div class="faults">${btn("D1 loop cut", "det_open", 1)}${btn("D3 loop cut", "det_open", 3)}${btn("D1 stuck-on", "det_stuck", 1)}${btn("Card A off", "card_off", 0)}${btn("Card B off", "card_off", 1)}${btn("Repair", "det_repair", null, "ok")}</div>
    <div class="sect">Controller</div><div class="faults">${btn("Crash (reboots)", "tsc_kill", null, "danger")}${btn("Power OFF", "tsc_power", 0, "danger")}${btn("Power ON", "tsc_power", 1, "ok")}${btn("Software hang", "tsc_hang", null, "danger")}${btn("Bug: conflicting greens", "bug_conflict", 1, "danger")}${btn("Bug fixed", "bug_conflict", 0, "ok")}</div>
    <div class="sect">Safety unit</div><div class="faults">${btn("Weld N-green load switch", "weld", 1, "danger")}${btn("Replace switch", "weld", 0, "ok")}${btn("Technician reset", "mmu_reset")}</div>
    <div class="sect">Time</div><div class="faults">${btn("GNSS off", "gnss", 0, "warn")}${btn("GNSS on", "gnss", 1, "ok")}${btn("PTP port off", "ptp", 0, "warn")}${btn("PTP on", "ptp", 1, "ok")}${btn("TCXO fault 3000 ppm", "tcxo", 3000, "danger")}${btn("TCXO ok", "tcxo", 0, "ok")}${btn("GNSS spoof +800 ms", "spoof", 800, "danger")}${btn("Spoof off", "spoof", 0, "ok")}</div>
    <div class="sect">Networks</div><div class="faults">${btn("Peer link down", "peer_link", 0, "warn")}${btn("Peer link up", "peer_link", 1, "ok")}${btn("Center link down", "center_link", 0, "warn")}${btn("Center link up", "center_link", 1, "ok")}</div>
    <div class="sect">Security (channel ${t})</div><div class="faults">${btn("Forge message", "forge", null, "danger")}${btn("Replay old message", "replay", null, "danger")}</div>
    <div class="sect">Proposal behaviour (for comparison)</div><div class="faults">${btn("Previous-cycle upstream ON", "legacy", 1, "warn")}${btn("OFF", "legacy", 0, "ok")}${btn("Buffer N=4", "buffer_n", 4, "warn")}${btn("N=1", "buffer_n", 1, "ok")}</div></div>`;
  $("#cab").innerHTML = h;
}
$("#cab").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-f]");
  if (b) fault(b.dataset.f, S.sel, JSON.parse(b.dataset.v));
});

// ------------------------------------------------------------------ demo script
const SCEN = [
  ["Normal operation", "Coordinated-actuated control on a shared clock: 40 s background cycle, 8 s offsets per block = green wave eastbound. Each controller samples its 8 detectors at t = 13 s, sends one authenticated message, and decides at t = 14 s.",
    "Watch blue dots: one message per intersection per cycle, 3 s to downstream neighbours, 1 s back to itself. Open Audit → any record → trace.", [["Open city", () => showTab("city")], ["Cabinet I17", () => { selectTsc(17); showTab("city"); }]]],
  ["Detector loop cut", "I17 D1 cable cut. The CANopen card sends an EMCY frame; the controller marks D1 LOOP_FAULT immediately. Rule R17-01 (uses D1) takes its declared fallback; R17-02 and R17-09 keep working.",
    "Cabinet I17 → D1 red; next record shows R17-01 MISSING with the reason. Logs → can/tsc17.candump shows 091#0050...", [["Cut I17 D1", () => { selectTsc(17); fault("det_open", 17, 1); }], ["Repair", () => fault("det_repair", 17)]]],
  ["Detector stuck-on", "I17 D1 reports presence permanently. Plausibility check flags STUCK_ON after 45 s (longer than any red), so it cannot be a real queue.",
    "Alarm DET_FAULT after ~45 s, R17-01 falls back. Side-street detectors failing count as permanent demand (side still served).", [["Stick I17 D1", () => { selectTsc(17); fault("det_stuck", 17, 1); }], ["Repair", () => fault("det_repair", 17)]]],
  ["Upstream controller fails", "I16 loses power and stays down. Its safety unit trips to flash on the missing watchdog; I17 notices the missing sequence number within one cycle — no center involved — and runs R17-02 on its fallback.",
    "I16 badge ✕ then flash; I17 alarm NEIGHBOR_SILENT; after reboot + technician reset, I17 re-enables after 2 good cycles (no flapping).", [["Power off I16", () => { selectTsc(17); fault("tsc_power", 16, 0); }], ["Power on I16", () => fault("tsc_power", 16, 1)], ["Reset I16 safety unit", () => fault("mmu_reset", 16)]]],
  ["Peer link cut", "I16 is fine but its cabinet loses the peer network: no channel messages, no PTP. I16 holds time on GNSS; I17 falls back for R17-02.",
    "Same detection path as a crash, without the flash.", [["Cut I16 peer link", () => { selectTsc(17); fault("peer_link", 16, 0); }], ["Restore", () => fault("peer_link", 16, 1)]]],
  ["GNSS lost → PTP → holdover", "Remove I12's sky view: it selects a GNSS-locked neighbour as PTP master (purple dashed line). Then fail its PTP port: holdover on the TCXO (2 ppm → ~14 h within 100 ms).",
    "Time sync tab: I12 stays within the band; state GNSS → PTP → HOLDOVER.", [["I12 GNSS off", () => { selectTsc(12); fault("gnss", 12, 0); }], ["I12 PTP off", () => fault("ptp", 12, 0)], ["Restore", () => { fault("gnss", 12, 1); fault("ptp", 12, 1); }]]],
  ["Clock fault while isolated", "With GNSS and PTP gone, I12's crystal fails (3000 ppm ≈ 3 ms/s). I12 believes it is in holdover; downstream I13 and I17 see its readings arrive with implausible sample timestamps and reject them.",
    "After ~5 min the error passes 1 s and receivers log 'timestamp implausible'; Time tab shows I12 drifting off the band.", [["Isolate I12 + TCXO fault", () => { selectTsc(12); fault("gnss", 12, 0); fault("ptp", 12, 0); fault("tcxo", 12, 3000); }], ["Restore I12", () => { fault("tcxo", 12, 0); fault("gnss", 12, 1); fault("ptp", 12, 1); }]]],
  ["GNSS spoofing", "An attacker shifts I7's GNSS time by +800 ms. The jump is cross-checked against the PTP peer, which agrees with the local clock → GNSS rejected, time from PTP.",
    "Alarm GNSS_SPOOF; I7 becomes a PTP slave; actual error stays near 0.", [["Spoof I7", () => { selectTsc(7); fault("spoof", 7, 800); }], ["Stop", () => fault("spoof", 7, 0)]]],
  ["Center link down (hours)", "The WAN to the control center drops for all intersections. Nothing changes on the street: decisions, failure handling and time are all local. Records and alarms are kept in flash.",
    "MQTT pill → 0/20; cabinet shows a growing backlog. Restore: records upload with their original timestamps (Audit shows upload delay).", [["All links down", () => fault("center_link", 0, 0)], ["Restore", () => fault("center_link", 0, 1)]]],
  ["Software bug: conflicting greens", "A defect in I9's controller commands N/S green during main green. The independent safety unit sees it on the field outputs and transfers to flash within 150 ms, latched.",
    "I9 flashes (amber main / red side), vehicles treat it as a stop. Fix the bug, then a technician must reset.", [["Inject bug at I9", () => { selectTsc(9); fault("bug_conflict", 9, 1); }], ["Fix bug", () => fault("bug_conflict", 9, 0)], ["Technician reset", () => fault("mmu_reset", 9)]]],
  ["Welded load switch", "Hardware fault: I9's N-green output welded on. Controller logic is correct, but the field shows a conflict or a double indication → flash.",
    "mmu09.log shows which check tripped and the field lamp word.", [["Weld I9 N-green", () => { selectTsc(9); fault("weld", 9, 1); }], ["Replace", () => fault("weld", 9, 0)], ["Technician reset", () => fault("mmu_reset", 9)]]],
  ["Forged & replayed messages", "A device on the peer network forges an I16 message announcing a platoon, then replays an old genuine one. AES-CMAC rejects the forgery; the sequence number rejects the replay.",
    "Red dots on the map; I17 alarms SEC_MAC and SEC_REPLAY; decisions unaffected.", [["Forge on ch 16", () => { selectTsc(17); fault("forge", 16); }], ["Replay on ch 16", () => fault("replay", 16)]]],
  ["The proposal, side by side", "Turn on the proposal's behaviour at I17: upstream rules on the previous cycle's readings. Records show readings ~40 s old used as if current. At I13 (two upstream), buffer emptied every 4 cycles: 3 × 4 = 12 > 10 → silent loss, detected only through sequence gaps.",
    "Audit → I17 trace prints the PROPOSAL-mode warning; I13 raises MSG_LOSS.", [["I17 previous-cycle ON", () => { selectTsc(17); fault("legacy", 17, 1); }], ["I13 buffer N=4", () => fault("buffer_n", 13, 4)], ["Back to the design", () => { fault("legacy", 17, 0); fault("buffer_n", 13, 1); }]]],
  ["Change → validate → release → deploy → trace", "Edit a rule in the table editor (e.g. I17 R17-02 extend 6 → 7 s), commit with your name, validate all 20 controllers, replay recorded cycles, release with a second engineer, deploy: staged, confirmed, then activated together at the next cycle boundary.",
    "Deploy tab shows staged → scheduled → active per controller; Audit trace of the next I17 record names the new release, commit and approver.", [["Open rule editor", () => { selectTsc(17); showTab("rules"); }], ["Deploy tab", () => showTab("deploy")]]],
];

function renderDemo() {
  $("#tab-demo").innerHTML = `<div class="intro"><h2>What runs here</h2>
    <div>Every box below is a real process on this Linux host, talking real protocols. Only the street, the cabinet hardware and the networks are emulated by the HIL testbench.</div>
    <div class="arch"><div><b>Control center</b> (Python, Linux)<br>rule editor · validator/compiler · Ed25519 signing (HSM stand-in) · replay · staged deployment · monitoring · audit · Mosquitto MQTT broker (mTLS)</div>
    <div><b>20 × intersection controller</b> (C++17, no heap after start-up)<br>bounded rule interpreter on signed tables · GNSS/PTP/TCXO time · CANopen master · AES-CMAC channel messages · A/B config slots · hash-chained decision records</div>
    <div><b>20 × safety unit</b> (C, independent)<br>conflict monitor on the field outputs: conflicting greens, multiple indications, dark heads, amber ≥ 3 s, controller watchdog → latched flash</div>
    <div><b>Peer network</b> (emulated switch)<br>channel = multicast group; 1 s / 3 s delivery from the brief; PTP IEEE 1588 on UDP 319/320; SPAN capture → pcap</div>
    <div><b>Center link</b> (emulated WAN)<br>TCP proxy adding 5 s each way (10 s round trip); can be cut for hours</div>
    <div><b>Plant</b> (emulated street)<br>IDM vehicles obey field lamps; inductive loops → CANopen cards (TPDO, heartbeat, EMCY, NMT)</div></div></div>
    <div class="scen" style="margin-top:10px">${SCEN.map((s, i) => `<div class="sc"><h3><span>${i + 1}</span>${esc(s[0])}</h3><p>${esc(s[1])}</p><p class="exp">Expect: ${esc(s[2])}</p><div class="btns">${s[3].map((b, j) => `<button data-s="${i}" data-b="${j}" class="${j === 0 ? "primary" : ""}">${esc(b[0])}</button>`).join("")}</div></div>`).join("")}</div>`;
}
$("#tab-demo").addEventListener("click", (e) => { const b = e.target.closest("button[data-s]"); if (b) SCEN[+b.dataset.s][3][+b.dataset.b][1](); });

// ------------------------------------------------------------------ rules
function fillSelect(sel, items, val) { sel.innerHTML = items.map(([v, l]) => `<option value="${v}" ${v == val ? "selected" : ""}>${esc(l)}</option>`).join(""); }

async function loadRules() {
  const t = +($("#r-tsc").value || S.sel);
  $("#r-tsc").value = t;
  S.rules = await api("GET", `/api/rules/${t}`);
  S.rules.rules.forEach((r) => r.clauses.forEach((c) => c.value = +c.value));
  $("#r-up").textContent = S.rules.upstream.length ? `upstream: ${S.rules.upstream.map((u) => "I" + u).join(", ")}` : "corridor head: no upstream neighbour";
  $("#r-text").textContent = S.rules.text;
  renderRuleTable();
  renderHistory(S.rules.history, `I${t}`);
}

function renderRuleTable() {
  const t = S.rules.tsc, R = S.rules.rules;
  const srcs = [[t, `own (I${t})`], ...S.rules.upstream.map((u) => [u, `upstream I${u}`])];
  const dets = Object.entries(S.city.detectors).map(([d, desc]) => [d, `D${d} – ${desc}`]);
  const ms = S.city.measures.map((m) => [m, m]), ops = S.city.ops.map((o) => [o, o]);
  const unit = { occupancy: "%", gap: "s", count: "veh", presence: "" };
  const actSel = (v, withNone) => (withNone ? [[0, "do not extend"]] : []).concat([[1, "extend green by"], [2, "hold, do not extend"]]).map(([a, l]) => `<option value="${a}" ${a == v ? "selected" : ""}>${l}</option>`).join("");
  $("#r-table").innerHTML = R.map((r, i) => `<div class="rule" data-i="${i}">
    <div class="rh"><input data-k="id" value="${esc(r.id)}" style="width:80px"><input class="title" data-k="title" value="${esc(r.title)}">
      <label class="muted">priority <input type="number" data-k="priority" value="${r.priority}"></label>
      <label class="muted"><input type="checkbox" data-k="enabled" ${r.enabled ? "checked" : ""}> enabled</label><button data-del="${i}" class="danger">✕</button></div>
    <div class="rgrid"><label>Where</label><div>intersection ${t}, phase main</div>
      <label>When</label><div>${r.clauses.map((c, j) => `<div class="cond" data-j="${j}">${j ? "<b class='muted'>and</b>" : ""}
        <select data-c="src_tsc">${srcs.map(([v, l]) => `<option value="${v}" ${v == c.src_tsc ? "selected" : ""}>${l}</option>`).join("")}</select>
        <select data-c="det">${dets.map(([v, l]) => `<option value="${v}" ${v == c.det ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>
        <select data-c="measure">${ms.map(([v, l]) => `<option ${v == c.measure ? "selected" : ""}>${v}</option>`).join("")}</select>
        <select data-c="op">${ops.map(([v]) => `<option ${v == c.op ? "selected" : ""}>${esc(v)}</option>`).join("")}</select>
        <input type="number" step="0.1" data-c="value" value="${c.value}"><span class="muted">${unit[c.measure]}</span>
        ${r.clauses.length > 1 ? `<button data-dc="${j}">−</button>` : ""}</div>`).join("")}
        ${r.clauses.length < 3 ? `<button data-ac="1">+ condition</button>` : ""}</div>
      <label>Then</label><div class="cond"><select data-k="action">${actSel(r.action, false)}</select>${r.action == 1 ? `<input type="number" step="0.5" data-k="action_s" value="${r.action_s}"> s` : ""}</div>
      <label>If missing</label><div class="cond"><select data-k="if_missing">${actSel(r.if_missing, true)}</select>${r.if_missing == 1 ? `<input type="number" step="0.5" data-k="missing_s" value="${r.missing_s}"> s` : ""}
        <span class="muted">— applied when an input is absent, stale, unsynchronised or faulty</span></div></div></div>`).join("");
}

$("#r-table").addEventListener("change", (e) => {
  const rd = e.target.closest(".rule"); if (!rd) return;
  const r = S.rules.rules[+rd.dataset.i];
  if (e.target.dataset.k) {
    const k = e.target.dataset.k;
    r[k] = e.target.type === "checkbox" ? e.target.checked : ["priority", "action", "if_missing"].includes(k) ? +e.target.value : ["action_s", "missing_s"].includes(k) ? +e.target.value : e.target.value;
    if (["action", "if_missing"].includes(k)) renderRuleTable();
  } else if (e.target.dataset.c) {
    const c = r.clauses[+e.target.closest(".cond").dataset.j];
    const k = e.target.dataset.c;
    c[k] = ["src_tsc", "det", "value"].includes(k) ? +e.target.value : e.target.value;
    if (k === "measure") renderRuleTable();
  }
});
$("#r-table").addEventListener("click", (e) => {
  const rd = e.target.closest(".rule"); if (!rd) return;
  const r = S.rules.rules[+rd.dataset.i];
  if (e.target.dataset.del !== undefined) S.rules.rules.splice(+e.target.dataset.del, 1);
  else if (e.target.dataset.dc !== undefined) r.clauses.splice(+e.target.dataset.dc, 1);
  else if (e.target.dataset.ac) r.clauses.push({ src_tsc: S.rules.tsc, det: 2, measure: "occupancy", op: ">", value: 20 });
  else return;
  renderRuleTable();
});
$("#r-add").onclick = () => {
  const t = S.rules.tsc;
  S.rules.rules.push({ id: `R${t}-${String(S.rules.rules.length + 3).padStart(2, "0")}`, title: "New rule", clauses: [{ src_tsc: t, det: 2, measure: "occupancy", op: ">", value: 25 }], action: 1, action_s: 3, if_missing: 0, missing_s: 0, priority: 30, enabled: true });
  renderRuleTable();
};
$("#r-text-toggle").onclick = () => $("#r-text").classList.toggle("hidden");
$("#r-save").onclick = async () => {
  const t = S.rules.tsc;
  try {
    const r = await api("PUT", `/api/rules/${t}`, { author: $("#r-author").value, message: $("#r-msg").value || `I${t}: rule table edited`, rules: S.rules.rules });
    $("#r-result").innerHTML = r.changed ? `<div class="card"><b class="ok-t">Committed ${r.commit}</b> by ${esc($("#r-author").value)}<pre class="code">${colorDiff(r.diff)}</pre></div>` : `<div class="card muted">No change to commit.</div>`;
    $("#r-msg").value = "";
    loadRules();
  } catch (e) { $("#r-result").innerHTML = `<div class="card bad-t">Rejected: ${esc(e.message)}</div>`; }
};
const colorDiff = (d) => esc(d).split("\n").map((l) => l.startsWith("+") && !l.startsWith("+++") ? `<span class="ok-t">${l}</span>` : l.startsWith("-") && !l.startsWith("---") ? `<span class="bad-t">${l}</span>` : `<span class="muted">${l}</span>`).join("\n");

function renderHistory(rows, scope) {
  $("#h-scope").textContent = scope;
  $("#r-hist").innerHTML = `<table><tr><th>commit</th><th>author</th><th>date</th><th>message</th><th>refs</th></tr>${rows.map((r) => `<tr><td>${r.commit}</td><td>${esc(r.author)}</td><td>${r.date.substr(0, 19)}</td><td>${esc(r.message)}</td><td>${esc(r.refs)}</td></tr>`).join("")}</table>`;
}

$("#v-run").onclick = async () => {
  $("#v-out").innerHTML = "validating…";
  const rep = await api("POST", "/api/validate");
  const t = S.rules ? S.rules.tsc : S.sel;
  let n = 0, f = 0;
  Object.values(rep.intersections).forEach((cs) => cs.forEach((c) => { n++; if (c.status !== "ok") f++; }));
  const fails = Object.entries(rep.intersections).flatMap(([k, cs]) => cs.filter((c) => c.status !== "ok").map((c) => ({ ...c, t: k })));
  const line = (c, pre = "") => `<div class="check"><span class="s ${c.status === "ok" ? "ok-t" : "bad-t"}">${c.status === "ok" ? "PASS" : "FAIL"}</span>${pre}<b>${esc(c.check)}</b> — <span class="muted">${esc(c.detail)}</span></div>`;
  $("#v-out").innerHTML = `<div class="row"><span class="chip ${rep.ok ? "ok" : "bad"}">${rep.ok ? "VALIDATION PASSED" : "VALIDATION FAILED"}</span><span class="muted">${n} checks on 20 controllers, ${f} failed</span></div>
    ${rep.city.map((c) => line(c)).join("")}${fails.map((c) => line(c, `I${c.t} `)).join("")}
    <div class="sect">I${t} in detail</div>${(rep.intersections[t] || []).map((c) => line(c)).join("")}`;
};
$("#rp-run").onclick = async () => {
  const t = S.rules ? S.rules.tsc : S.sel;
  $("#rp-out").innerHTML = "replaying…";
  try {
    const r = await api("POST", `/api/replay/${t}`);
    $("#rp-out").innerHTML = `<div class="row"><span class="chip info">${r.cycles} recorded cycles (v${r.active_version})</span><span class="chip ${r.reference_matches_recorded === r.cycles ? "ok" : "bad"}">Python reference = controller: ${r.reference_matches_recorded}/${r.cycles}</span><span class="chip ${r.decisions_changed ? "warn" : "ok"}">${r.decisions_changed} decision(s) would change</span></div>
      <div class="list" style="max-height:220px"><table><tr><th>record</th><th>recorded</th><th>with edited rules</th></tr>${r.rows.slice(0, 40).map((x) => `<tr class="${x.changed ? "sel" : ""}"><td>${x.rec}</td><td>${esc(x.recorded)}</td><td class="${x.changed ? "warn-t" : ""}">${esc(x.proposed)}</td></tr>`).join("")}</table></div>`;
  } catch (e) { $("#rp-out").innerHTML = `<span class="bad-t">${esc(e.message)}</span>`; }
};
$("#rel-run").onclick = async () => {
  $("#rel-out").innerHTML = "compiling and signing…";
  try {
    const m = await api("POST", "/api/release", { author: $("#rel-author").value, approver: $("#rel-approver").value, note: $("#rel-note").value });
    $("#rel-out").innerHTML = `<div class="ok-t"><b>Release v${m.version}</b> (${m.tag}) from commit ${m.git_commit}: ${m.changed.length} controller(s) changed (${m.changed.map((x) => "I" + x).join(", ") || "none"}). Signed with Ed25519. <button id="go-deploy" class="primary">Deploy…</button></div>`;
    $("#go-deploy").onclick = () => showTab("deploy");
    loadRules();
  } catch (e) { $("#rel-out").innerHTML = `<span class="bad-t">Refused: ${esc(e.message)}</span>`; }
};

// ------------------------------------------------------------------ deploy
async function refreshDeploy() {
  const st = await api("GET", "/api/state");
  S.releases = st.releases; S.deployments = st.deployments;
  Object.assign(S.health, st.health); Object.assign(S.status, st.status);
  renderDeploy();
}
function renderDeploy() {
  $("#d-rel").innerHTML = `<table><tr><th>version</th><th>tag</th><th>commit</th><th>author</th><th>approver</th><th>created (UTC)</th><th>changed</th><th></th></tr>
    ${S.releases.slice().reverse().map((r) => `<tr><td><b>v${r.version}</b></td><td>${r.tag}</td><td>${r.git_commit}</td><td>${esc(r.author)}</td><td>${esc(r.approver)}</td><td>${r.created_utc}</td><td>${r.changed.map((x) => "I" + x).join(", ") || "-"}</td>
    <td><button class="primary" data-dep="${r.version}" data-tg="changed" title="Only intersections whose rules changed in this release">Deploy to changed</button> <button data-dep="${r.version}" data-tg="all" title="Whole fleet, one coordinated activation time">all 20</button></td></tr>`).join("")}</table>`;
  $("#d-grid").innerHTML = Array.from({ length: 20 }, (_, i) => i + 1).map((t) => {
    const h = S.health[t], on = S.status[t] === "online";
    const c = h ? h.cfg : null;
    return `<div class="tile"><b>I${t}</b> <span class="${on ? "ok-t" : "bad-t"}">${on ? "online" : S.status[t] || "?"}</span><br>${c ? `v${c.ver} · slot ${String.fromCharCode(c.slot)} (A v${c.ver_a} / B v${c.ver_b})${c.pending ? `<br><span class="warn-t">v${c.pending_ver} at ${hhmmss(c.pending_at_ns / 1e9)}</span>` : ""}` : "-"}</div>`;
  }).join("");
  const stCol = { active: "ok", staged: "info", scheduled: "info", scheduling: "warn", staging: "warn", rejected: "bad", timeout: "bad", offline: "bad" };
  $("#d-deps").innerHTML = S.deployments.slice().reverse().map((d) => `<div class="box"><h4>#${d.id} release v${d.version} ${d.late ? "(late join)" : ""}<span class="chip ${d.phase === "complete" ? "ok" : d.phase === "aborted" ? "bad" : "warn"}">${d.phase}</span></h4>
    ${d.at_ns ? `<div class="muted">Activation time T = ${hhmmss(d.at_ns / 1e9)} UTC — each controller switches at its first cycle boundary after T, on its synchronized clock.</div>` : ""}
    <div>${Object.entries(d.targets).map(([t, x]) => `<span class="chip ${stCol[x.state] || ""}" title="${esc(x.detail)}">I${t} ${x.state}</span>`).join("")}</div>
    <pre class="code" style="max-height:150px">${d.log.map((l) => `${hhmmss(l.t)}  ${esc(l.text)}`).join("\n")}</pre></div>`).join("") || `<span class="muted">No deployments yet.</span>`;
}
$("#d-rel").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-dep]"); if (!b) return;
  try { const r = await api("POST", "/api/deploy", { version: +b.dataset.dep, targets: b.dataset.tg }); tick(`deploying v${r.version} to ${r.targets.map((x) => "I" + x).join(", ")}`, "bench"); }
  catch (er) { alert(er.message); }
});
$("#rb-run").onclick = async () => {
  const tg = $("#rb-targets").value.split(",").map((x) => +x.trim()).filter((x) => x);
  const r = await api("POST", "/api/rollback", { targets: tg });
  $("#rb-out").textContent = Object.entries(r).map(([t, v]) => `I${t}: ${v}`).join(" · ");
};

// ------------------------------------------------------------------ monitor
function renderMonitor() {
  const now = Date.now() / 1000;
  const rows = Array.from({ length: 20 }, (_, i) => i + 1).map((t) => {
    const h = S.health[t], st = S.status[t];
    if (!h) return `<tr><td>I${t}</td><td class="bad-t">${st || "no data"}</td></tr>`;
    const age = now - h.cc.rx_ns / 1e9, bad = h.det.filter((d) => d.q !== "OK").map((d) => `D${d.d} ${d.q}`);
    const nb = h.neighbours.filter((n) => n.tsc !== t).map((n) => `<span class="${n.live === "OK" ? "ok-t" : n.live === "SILENT" ? "bad-t" : "warn-t"}">I${n.tsc} ${n.live}</span>`).join(" ");
    return `<tr class="${t === S.sel ? "sel" : ""}"><td><b>I${t}</b></td><td class="${st === "online" ? "ok-t" : "bad-t"}">${st}</td><td>${age.toFixed(1)} s</td>
      <td style="color:${MODE_COL[h.mode]}">${h.mode}</td><td>${esc(h.sync.q)} ${h.sync.bound_ms >= 0 ? "±" + h.sync.bound_ms.toFixed(3) + " ms" : ""}</td>
      <td>${h.cc.est_offset_s >= 0 ? "+" : ""}${h.cc.est_offset_s.toFixed(2)} ± ${h.cc.uncertainty_s} s</td>
      <td class="${bad.length ? "bad-t" : ""}">${bad.join(", ") || "8 OK"}</td><td>${nb || "-"}</td><td>${h.buffer.used}/${h.buffer.slots} N=${h.buffer.n}${h.buffer.lost_by_seq ? ` <span class="bad-t">lost ${h.buffer.lost_by_seq}</span>` : ""}</td>
      <td>${h.security.mac_fail + h.security.replay ? `<span class="bad-t">MAC ${h.security.mac_fail} / replay ${h.security.replay}</span>` : "0"}</td>
      <td>v${h.cfg.ver} ${String.fromCharCode(h.cfg.slot)}</td><td class="${h.mmu.state !== "NORMAL" ? "bad-t" : ""}">${h.mmu.state}</td><td>${h.records.backlog}</td></tr>`;
  });
  $("#m-table").innerHTML = `<table><tr><th>TSC</th><th>link</th><th>telemetry age</th><th>mode</th><th>own sync</th><th>CC clock estimate</th><th>detectors</th><th>upstream</th><th>buffer</th><th>security</th><th>cfg</th><th>safety</th><th>backlog</th></tr>${rows.join("")}</table>`;
}
function renderAlarms() {
  $("#m-alarms").innerHTML = S.alarms.slice().reverse().map((a) => `<div class="alarm"><span class="muted">${new Date(a.t_ns / 1e6).toISOString().substr(11, 12)}</span> <b>I${a.tsc}</b> <span class="${a.sev === "major" ? "bad-t" : a.sev === "warn" ? "warn-t" : "ok-t"}">${a.sev.toUpperCase()} ${esc(a.code)}</span> ${esc(a.text)} <span class="muted">(received ${((a.rx_ns - a.t_ns) / 1e9).toFixed(1)} s later)</span></div>`).join("");
}

// ------------------------------------------------------------------ audit
async function loadRecords() {
  const t = +$("#a-tsc").value;
  S.records = await api("GET", `/api/records?tsc=${t}&limit=120`);
  renderRecords();
}
function recLine(r) {
  const d = r.decision;
  const col = d.action === "EXTEND" ? "ok-t" : d.action === "HOLD" ? "warn-t" : "muted";
  return `<div class="rec" data-rec="${r.rec}"><b>${r.rec}</b> <span class="muted">${r.t_utc.substr(11, 12)}</span> <span class="${col}">${d.action}${d.action === "EXTEND" ? " " + d.ext_s + " s" : ""}</span> by ${esc(d.by)} <span class="chip" style="color:${MODE_COL[r.mode] || "#aaa"}">${r.mode}</span>${r.cfg && r.cfg.legacy_prev_cycle ? ' <span class="chip bad">proposal</span>' : ""}${r._hash_ok === false ? ' <span class="chip bad">hash!</span>' : ""}</div>`;
}
function renderRecords() { $("#a-list").innerHTML = S.records.map(recLine).join("") || `<span class="muted">No records uploaded yet (first decisions arrive ~1 cycle after start).</span>`; }
function onRecord(m) {
  const t = +$("#a-tsc").value;
  if (t && m.tsc !== t) return;
  S.records.unshift({ rec: m.rec, t_utc: m.t_utc, decision: m.decision, mode: m.mode, _hash_ok: m.hash_ok });
  S.records = S.records.slice(0, 150);
  renderRecords();
}
$("#a-list").addEventListener("click", async (e) => {
  const d = e.target.closest(".rec"); if (!d) return;
  document.querySelectorAll(".rec").forEach((x) => x.classList.toggle("sel", x === d));
  const tr = await api("GET", `/api/trace/${d.dataset.rec}`);
  const r = tr.record;
  $("#a-trace").innerHTML = `<div class="row"><span class="chip ${tr.hash_ok ? "ok" : "bad"}">hash ${tr.hash_ok ? "verified" : "MISMATCH"}</span><span class="chip ${tr.chain_ok ? "ok" : tr.chain_ok === null ? "" : "bad"}">chain ${tr.chain_ok ? "intact" : tr.chain_ok === null ? "n/a" : "BROKEN"}</span><span class="chip info">uploaded ${tr.rx_delay_s} s after the decision</span><span class="chip">release v${tr.release.version} · ${esc(tr.release.git_commit)}</span></div>
    <div class="expl">${tr.explanation.map((l) => l.startsWith("    ") ? `<div class="sub">${esc(l.trim())}</div>` : `<div>${esc(l)}</div>`).join("")}</div>
    <details><summary class="muted">Inputs as recorded (${r.inputs.length})</summary><table><tr><th>src</th><th>det</th><th>status</th><th>value</th><th>sampled</th><th>age</th><th>seq</th><th>why</th></tr>
    ${r.inputs.map((i) => `<tr><td>I${i.src}</td><td>D${i.det}</td><td class="${i.status === "OK" ? "ok-t" : "bad-t"}">${i.status}</td><td>${i.q ? `occ ${i.occ_pct}% gap ${i.gap_s}s n ${i.count}` : "-"}</td><td>${i.sample_utc ? i.sample_utc.substr(11) : "-"}</td><td>${i.age_s ?? "-"}</td><td>${i.seq ?? "-"}</td><td style="white-space:normal">${esc(i.why || "")}</td></tr>`).join("")}</table></details>
    <details><summary class="muted">Raw record (as stored on the controller's flash)</summary><pre class="code">${esc(JSON.stringify(r, null, 1))}</pre></details>`;
});
$("#a-refresh").onclick = loadRecords;
$("#a-tsc").onchange = loadRecords;

// ------------------------------------------------------------------ time sync
const tc = $("#t-chart"), tcx = tc.getContext("2d");
function renderTime() {
  const w = tc.clientWidth, h = 260;
  tc.width = w * devicePixelRatio; tc.height = h * devicePixelRatio; tcx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
  const sc = +$("#t-scale").value, now = Date.now() / 1000, span = 150;
  tcx.fillStyle = "#0b1016"; tcx.fillRect(0, 0, w, h);
  const Y = (v) => h / 2 - (Math.max(-sc, Math.min(sc, v)) / sc) * (h / 2 - 12), X = (t) => 50 + (w - 60) * (1 - (now - t) / span);
  tcx.strokeStyle = "#2a3546"; tcx.fillStyle = "#8796a8"; tcx.font = "10px system-ui";
  for (const v of [-sc, -sc / 2, 0, sc / 2, sc]) { tcx.beginPath(); tcx.moveTo(50, Y(v)); tcx.lineTo(w, Y(v)); tcx.stroke(); tcx.fillText((v >= 1000 || v <= -1000 ? v / 1000 + " s" : v + " ms"), 4, Y(v) + 3); }
  if (sc >= 100) { tcx.strokeStyle = "#ff5d5d55"; tcx.setLineDash([5, 5]); for (const v of [-100, 100]) { tcx.beginPath(); tcx.moveTo(50, Y(v)); tcx.lineTo(w, Y(v)); tcx.stroke(); } tcx.setLineDash([]); }
  const hue = (t) => `hsl(${(t * 47) % 360} 75% 62%)`;
  for (const [t, pts] of Object.entries(S.timeHist)) {
    tcx.strokeStyle = hue(+t); tcx.lineWidth = +t === S.sel ? 2.5 : 1; tcx.beginPath();
    pts.filter((p) => now - p[0] < span).forEach((p, i) => (i ? tcx.lineTo(X(p[0]), Y(p[1])) : tcx.moveTo(X(p[0]), Y(p[1]))));
    tcx.stroke();
    const last = pts[pts.length - 1];
    if (last && Math.abs(last[1]) > sc * 0.05) { tcx.fillStyle = hue(+t); tcx.fillText("I" + t, w - 30, Y(last[1]) - 3); }
  }
  $("#t-table").innerHTML = `<table><tr><th>TSC</th><th>source</th><th>state</th><th>error bound</th><th>actual error</th><th>PTP master</th><th>PTP offset</th><th>path delay</th><th>class</th><th>freq corr</th><th>GNSS</th></tr>
    ${Object.entries(S.probes).map(([t, p]) => `<tr class="${+t === S.sel ? "sel" : ""}"><td>I${t}</td><td>${p.sync.q}</td><td>${esc(p.sync.state)}</td><td>${p.sync.bound_ms < 0 ? "∞" : p.sync.bound_ms.toFixed(3) + " ms"}</td>
    <td class="${Math.abs(p.truth_offset_ms) > 100 ? "bad-t" : ""}">${p.truth_offset_ms.toFixed(3)} ms</td><td>${p.sync.master ? "I" + p.sync.master : "-"}</td><td>${p.sync.master ? p.sync.ptp_offset_ms.toFixed(3) + " ms" : "-"}</td><td>${p.sync.master ? p.sync.ptp_delay_ms.toFixed(3) + " ms" : "-"}</td>
    <td>${p.sync.clock_class}</td><td>${p.sync.freq_ppm.toFixed(2)} ppm</td><td>${p.sync.gnss_sky ? "sky" : "none"}${p.sync.gnss_rejected ? " (rejected)" : ""}</td></tr>`).join("")}</table>`;
}
$("#t-scale").onchange = renderTime;

// ------------------------------------------------------------------ logs
function fillStreams(streams) {
  $("#l-stream").innerHTML = streams.map((s) => `<option>${s}</option>`).join("");
  if (S.logStream) $("#l-stream").value = S.logStream;
  $("#l-pcaps").innerHTML = `<div class="muted">Written live by the emulated switch's SPAN port and the cabinet CAN taps. Wireshark decodes PTP (UDP 319/320) and SocketCAN natively; add <a href="/gui/tsc_channel.lua">tsc_channel.lua</a> to decode the channel messages.</div>
    <div class="row"><a href="/pcap/peer_network.pcap"><button>peer_network.pcap</button></a>${[12, 13, 16, 17].map((t) => `<a href="/pcap/can_tsc${t}.pcap"><button>can_tsc${t}.pcap</button></a>`).join("")}</div>
    <div class="muted">Live MQTT on the terminal: <code>./demo.sh sub</code> (read-only listener 1883). Broker log: stream mosquitto.log.</div>`;
}
function subLog(s) {
  if (!s) return;
  S.logStream = s; $("#l-out").textContent = "";
  $("#l-stream").value = s;
  if (S.benchWs && S.benchWs.readyState === 1) S.benchWs.send(JSON.stringify({ cmd: "log_sub", stream: s }));
}
function onLog(m) {
  if (m.stream !== S.logStream) return;
  const f = $("#l-filter").value.trim();
  let text = m.text;
  if (f) text = text.split("\n").filter((l) => l.includes(f)).join("\n") + "\n";
  const o = $("#l-out");
  o.textContent = (o.textContent + text).slice(-150000);
  if ($("#l-follow").checked) o.scrollTop = o.scrollHeight;
}
$("#l-stream").onchange = (e) => subLog(e.target.value);
$("#l-clear").onclick = () => $("#l-out").textContent = "";

// ------------------------------------------------------------------ help & guide
let guideLoaded = false;
async function loadGuide() {
  if (guideLoaded) return;
  const g = await api("GET", "/api/guide");
  $("#h-doc").innerHTML = g.html;
  $("#h-toc").innerHTML = [...$("#h-doc").querySelectorAll("h2[id], h3[id]")]
    .map((h) => `<a href="#" data-to="${h.id}" class="${h.tagName.toLowerCase()}">${esc(h.textContent)}</a>`).join("");
  guideLoaded = true;
}
$("#h-toc").addEventListener("click", (e) => {
  const a = e.target.closest("a[data-to]"); if (!a) return;
  e.preventDefault(); document.getElementById(a.dataset.to).scrollIntoView({ behavior: "smooth", block: "start" });
});
$("#h-doc").addEventListener("click", (e) => {           // in-page links of the guide (#anchor)
  const a = e.target.closest('a[href^="#"]'); if (!a || a.dataset.to) return;
  const el = document.getElementById(a.getAttribute("href").slice(1));
  if (el) { e.preventDefault(); el.scrollIntoView({ behavior: "smooth", block: "start" }); }
});
async function renderProcs() {
  if (S.tab !== "help") return;
  let s;
  try { s = await api("GET", "/api/system"); } catch (e) { $("#h-procs").textContent = e.message; return; }
  const mb = (p) => (p.rss_kb / 1024).toFixed(1) + " MB";
  const up = (x) => x >= 3600 ? (x / 3600).toFixed(1) + " h" : x >= 60 ? Math.round(x / 60) + " min" : x + " s";
  const one = (name, list, what) => list.length ? list.map((p) => `<tr><td><b>${name}</b></td><td>${p.pid}</td><td>${mb(p)}</td><td>${p.threads}</td><td>${up(p.uptime_s)}</td><td class="muted" style="white-space:normal">${esc(p.argv.join(" ").replaceAll(s.root + "/", ""))}</td></tr>`).join("")
    : `<tr><td><b>${name}</b></td><td colspan="5" class="bad-t">not running — ${what}</td></tr>`;
  const grid = (list, label) => {
    const by = Object.fromEntries(list.map((p) => [p.tsc, p]));
    return `<div class="procgrid">${Array.from({ length: 20 }, (_, i) => i + 1).map((t) => by[t]
      ? `<div><b>${label}${String(t).padStart(2, "0")}</b> pid ${by[t].pid}<br>${mb(by[t])} · up ${up(by[t].uptime_s)}</div>`
      : `<div class="down"><b>${label}${String(t).padStart(2, "0")}</b><br>not running</div>`).join("")}</div>`;
  };
  $("#h-procs").innerHTML = `<div class="row"><span class="chip ${s.controllers.length === 20 ? "ok" : "bad"}">${s.controllers.length}/20 controllers (C++)</span>
      <span class="chip ${s.safety_units.length === 20 ? "ok" : "bad"}">${s.safety_units.length}/20 safety units (C)</span>
      <span class="chip ${s.broker.length ? "ok" : "bad"}">broker ${s.broker.length ? "up" : "down"}</span>
      <span class="chip ${s.control_center.length ? "ok" : "bad"}">control center ${s.control_center.length ? "up" : "down"}</span>
      <span class="chip ${s.testbench.length ? "ok" : "bad"}">testbench ${s.testbench.length ? "up" : "down"}</span>
      <span class="muted">Same check in a terminal: <code>./demo.sh status</code> · <code>pgrep -af controller/build/tsc</code></span></div>
    <table><tr><th>process</th><th>pid</th><th>memory</th><th>threads</th><th>up</th><th>command line</th></tr>
      ${one("Mosquitto broker", s.broker, "./demo.sh start")}${one("Control center", s.control_center, "./demo.sh start")}${one("Testbench", s.testbench, "./demo.sh start")}</table>
    <div class="sect">Intersection controllers <span class="muted">controller/build/tsc --id N</span></div>${grid(s.controllers, "tsc")}
    <div class="sect">Safety units <span class="muted">controller/build/mmu --id N</span></div>${grid(s.safety_units, "mmu")}`;
}
setInterval(renderProcs, 2000);

// ------------------------------------------------------------------ controls + boot
$("#demand").oninput = (e) => { $("#demand-v").textContent = "×" + (+e.target.value).toFixed(1); };
$("#demand").onchange = (e) => fault("demand", 0, +e.target.value);
$("#rtt").onchange = (e) => fault("wan_latency", 0, +e.target.value);
$("#clear-all").onclick = () => fault("clear_all", 0);

(async function boot() {
  S.city = await api("GET", "/api/city");
  const tscs = S.city.intersections.map((i) => [i.id, `I${i.id}`]);
  fillSelect($("#r-tsc"), tscs, 17);
  $("#a-tsc").innerHTML += tscs.map(([v, l]) => `<option value="${v}">${l}</option>`).join("");
  const eng = S.city.engineers.map((e) => [e, e]);
  fillSelect($("#r-author"), eng, "a.joshi"); fillSelect($("#rel-author"), eng, "a.joshi"); fillSelect($("#rel-approver"), eng, "r.vermeer");
  $("#r-tsc").onchange = () => loadRules();
  const st = await api("GET", "/api/state");
  Object.assign(S.health, st.health); Object.assign(S.status, st.status);
  S.alarms = st.alarms; S.deployments = st.deployments; S.releases = st.releases;
  renderDemo();
  connectCC(); connectBench();
  requestAnimationFrame(draw);
  const q = new URLSearchParams(location.search);
  if (q.get("sel")) S.sel = +q.get("sel");
  if (location.hash.length > 1) showTab(location.hash.slice(1));
  setInterval(() => { if (S.tab === "time") renderTime(); }, 1000);
})();
