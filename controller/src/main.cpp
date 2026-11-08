// Intersection controller (TSC). One process per cabinet; in the field this is
// firmware on an RTOS. Here a 10 ms cyclic executive stands in for the RTOS tasks.
//
//   tsc --id 17 --runtime <dir> --nb 12,16,18,22 --gnss 1 --boot-offset-ms 1800 --ppm 1.3
#include <poll.h>
#include <signal.h>
#include <unistd.h>

#include <atomic>
#include <cstdlib>
#include <new>

#include "app/app.hpp"

// ---- heap instrumentation: proves "no C++ heap allocation after start-up"
static std::atomic<bool> g_after_init{false};
static std::atomic<uint64_t> g_new_after{0};
void* operator new(size_t n) {
  if (g_after_init) ++g_new_after;
  void* p = malloc(n ? n : 1);
  if (!p) abort();
  return p;
}
void* operator new[](size_t n) { return operator new(n); }
void operator delete(void* p) noexcept { free(p); }
void operator delete[](void* p) noexcept { free(p); }
void operator delete(void* p, size_t) noexcept { free(p); }
void operator delete[](void* p, size_t) noexcept { free(p); }

namespace tsc::svc {
TimeService* g_time = nullptr;
}

using namespace tsc;

namespace {
int g_id = 0;
char g_rt[256] = "runtime";
hal::Oscillator g_osc;
hal::Gnss g_gnss;
hal::PeerNet g_net;
hal::PlatformBuffer g_buf;
hal::CanBus g_can;
hal::LampIo g_lamp;
hal::Udp g_bp, g_probe;
hal::Watchdog g_wd;
hal::MmuStatus g_mmu;
svc::TimeService g_ts;
svc::DetectorService g_det;
svc::MessageService g_msg;
svc::ConfigStore g_store;
svc::Recorder g_rec;
svc::CenterAgent g_center;
svc::Config g_cfg;
app::PhaseController g_pc;
bool g_hang = false;
int64_t g_boot_mono = 0;
char g_json[16384];

int64_t now_fn() { return g_ts.now(); }

const char* live_str(uint8_t l) {
  static const char* s[] = {"UNKNOWN", "OK", "SILENT", "RECOVERING"};
  return s[l];
}

// Shared by the MQTT health message and the bench probe.
void common_fields(JsonW& w) {
  uint32_t pv;
  int64_t pat;
  bool pend = g_store.pending(&pv, &pat);
  w.kv("id", g_id).kv("fw", kFwVersion).kvf("uptime_s", (mono_ns() - g_boot_mono) / 1e9, 0);
  w.kv("mode", g_pc.mode_name()).kv("phase", g_pc.phase_name()).kv("cycle", g_pc.cycle()).kvf("cycle_t", g_pc.cycle_t(), 1);
  w.kv("lamps", g_pc.lamps()).kvb("side_skipped", g_pc.side_skipped());
  w.obj("sync").kv("q", to_str(g_ts.quality())).kv("state", g_ts.state_text());
  w.kvf("bound_ms", g_ts.err_bound_ms() > 1e6 ? -1.0 : g_ts.err_bound_ms(), 4).kv("master", g_ts.master());
  w.kvf("ptp_offset_ms", g_ts.ptp_offset_ms(), 4).kvf("ptp_delay_ms", g_ts.ptp_delay_ms(), 4);
  w.kvf("gnss_offset_ms", g_ts.gnss_offset_ms(), 4).kvb("gnss_sky", g_gnss.locked()).kvb("gnss_rejected", g_ts.gnss_rejected());
  w.kv("clock_class", g_ts.clock_class()).kv("steps", g_ts.steps()).kvf("freq_ppm", g_ts.freq_ppm(), 3);
  w.kvf("holdover_h", g_ts.holdover_left_h(), 2).kv("local_utc_ns", g_ts.now()).kvb("ptp_port", g_net.ptp_up()).end_obj();
  w.arr("det");
  svc::Reading r[kDet];
  g_det.sample(r);
  for (int i = 0; i < kDet; ++i)
    w.obj().kv("d", i + 1).kv("q", to_str(r[i].q)).kv("p", r[i].presence).kvf("occ", r[i].occ_permille / 10.0, 1).kvf("gap", r[i].gap_ds / 10.0, 1).end_obj();
  w.end_arr();
  w.arr("cards");
  for (int i = 0; i < 2; ++i) w.obj().kv("node", 0x11 + i).kvb("silent", g_det.card_silent(i)).kv("nmt", g_det.card_state(i)).end_obj();
  w.end_arr();
  w.arr("neighbours");
  for (int i = 0; i < g_msg.n_chans(); ++i) {
    const svc::ChanState& s = g_msg.chan(i);
    w.obj().kv("tsc", s.src).kv("ch", s.ch).kv("live", live_str(s.live)).kv("seq", s.seq).kv("lost", s.lost);
    w.kv("sync", to_str(s.syncq)).kvf("age_s", s.have ? (g_ts.now() - s.sample_ns) / 1e9 : -1.0, 1).end_obj();
  }
  w.end_arr();
  w.obj("buffer").kv("used", g_buf.size()).kv("slots", kBufSlots).kv("n", g_msg.buffer_n_override ? g_msg.buffer_n_override : g_cfg.buffer_n);
  w.kv("lost_by_seq", g_msg.lost_total).end_obj();
  w.obj("security").kv("mac_fail", g_msg.mac_fail).kv("replay", g_msg.replays).end_obj();
  w.obj("cfg").kv("ver", g_cfg.version).kv("tag", g_cfg.tag).kv("hash", g_cfg.hash).kv("slot", (int)g_store.active_slot());
  w.kv("ver_a", g_store.slot_version('A')).kv("ver_b", g_store.slot_version('B')).kvb("pending", pend);
  w.kv("pending_ver", pend ? pv : 0).kv("pending_at_ns", pend ? pat : 0).kvb("legacy_prev_cycle", g_cfg.legacy_prev_cycle()).end_obj();
  w.obj("mmu").kv("state", g_mmu.state ? "FLASH" : "NORMAL").kv("fault", g_mmu.fault).kv("field_lamps", g_mmu.field_lamps).end_obj();
  w.obj("records").kv("seq", g_rec.seq()).kv("backlog", g_rec.backlog()).kv("last", g_pc.last_decision()).end_obj();
  w.kv("heap_allocs_after_init", (int64_t)g_new_after.load()).kvb("mqtt", g_center.connected());
  w.kv("alarms_queued", svc::alarm_pending());
}

int health_fn(char* buf, size_t cap) {
  JsonW w(buf, cap);
  w.obj();
  common_fields(w);
  w.end_obj();
  return w.overflow() ? 0 : (int)w.len();
}

void send_probe() {   // bench instrumentation only (not part of the product)
  JsonW w(g_json, sizeof g_json);
  w.obj().kv("type", "tsc");
  common_fields(w);
  w.kvf("truth_offset_ms", (g_ts.now() - hal::Oscillator::true_now()) / 1e6, 4).kvf("osc_ppm", g_osc.ppm(), 1);
  w.end_obj();
  if (!w.overflow()) g_probe.send(port::kProbe, g_json, (int)w.len());
}

void cfg_fn(const char* sub, const uint8_t* p, int len) {
  char res[240], ack[400];
  bool ok;
  const char* op;
  if (!strcmp(sub, "stage")) {
    op = "stage";
    ok = g_store.stage(p, len, res, sizeof res);
  } else if (!strcmp(sub, "cmd")) {
    op = "cmd";
    ok = g_store.command(p, len, g_cfg.version, res, sizeof res);
  } else {
    return;
  }
  JsonW w(ack, sizeof ack);
  w.obj().kv("op", op).kvb("ok", ok).kv("result", res).kv("active", g_cfg.version).kv("slot_a", g_store.slot_version('A'));
  w.kv("slot_b", g_store.slot_version('B')).end_obj();
  g_center.publish("cfg/ack", ack, 1);
}

void activated_fn(uint32_t ver, int64_t at, int64_t now, uint32_t cycle) {
  char b[300], ts[40];
  fmt_utc(now, ts, sizeof ts);
  JsonW w(b, sizeof b);
  w.obj().kv("ver", ver).kv("at_ns", now).kv("scheduled_ns", at).kvf("after_schedule_s", (now - at) / 1e9, 3).kv("cycle", cycle).kv("t_utc", ts).end_obj();
  g_center.publish("cfg/activated", b, 1);
  svc::alarm("CFG_ACTIVE", "info", "configuration v%u active from cycle %u (%.1f s after the scheduled time, at the cycle boundary)",
             ver, cycle, (now - at) / 1e9);
}

void backplane() {   // simulated hardware faults injected by the testbench (not part of the product)
  char b[256];
  int n = g_bp.recv(b, sizeof b - 1);
  if (n <= 0) return;
  b[n] = 0;
  char cmd[32] = "";
  double v = 0;
  json_str(b, "cmd", cmd, sizeof cmd);
  json_num(b, "value", &v);
  logf("SIM", "bench fault injection: %s", b);
  if (!strcmp(cmd, "gnss")) g_gnss.set_sky(v != 0);
  else if (!strcmp(cmd, "spoof")) g_gnss.set_spoof_ms((int64_t)v);
  else if (!strcmp(cmd, "ppm")) g_osc.set_ppm(v);
  else if (!strcmp(cmd, "clock_step")) g_osc.step((int64_t)(v * 1e6));
  else if (!strcmp(cmd, "ptp")) {
    g_net.set_ptp_port(v != 0);
    logf("PTP", "peer PTP port %s", v != 0 ? "UP" : "DOWN (hardware fault)");
  } else if (!strcmp(cmd, "hang")) g_hang = true;
  else if (!strcmp(cmd, "bug_conflict")) g_pc.bug_conflict = v != 0;
  else if (!strcmp(cmd, "legacy")) {
    if (v != 0) g_cfg.flags |= 1u; else g_cfg.flags &= ~1u;
    logf("RULE", "PROPOSAL MODE %s: upstream rules use previous-cycle readings", v != 0 ? "ON" : "OFF");
  } else if (!strcmp(cmd, "buffer_n")) {
    g_msg.buffer_n_override = (int)v;
    logf("CHAN", "buffer emptied every %d cycle(s) (proposal value injected, bypassing the validator)", (int)v);
  }
}

void parse_list(const char* s, uint8_t* out, int* n) {
  *n = 0;
  while (*s && *n < 4) {
    out[(*n)++] = (uint8_t)atoi(s);
    while (*s && *s != ',') ++s;
    if (*s == ',') ++s;
  }
}
}  // namespace

int main(int argc, char** argv) {
  uint8_t nb[4] = {0};
  int nnb = 0;
  bool gnss = true;
  double boot_off_ms = 0, ppm = 0;
  for (int i = 1; i + 1 < argc; i += 2) {
    if (!strcmp(argv[i], "--id")) g_id = atoi(argv[i + 1]);
    else if (!strcmp(argv[i], "--runtime")) snprintf(g_rt, sizeof g_rt, "%s", argv[i + 1]);
    else if (!strcmp(argv[i], "--nb")) parse_list(argv[i + 1], nb, &nnb);
    else if (!strcmp(argv[i], "--gnss")) gnss = atoi(argv[i + 1]) != 0;
    else if (!strcmp(argv[i], "--boot-offset-ms")) boot_off_ms = atof(argv[i + 1]);
    else if (!strcmp(argv[i], "--ppm")) ppm = atof(argv[i + 1]);
  }
  if (g_id < 1 || g_id > 99) {
    fprintf(stderr, "usage: tsc --id N --runtime DIR [--nb a,b,c] [--gnss 0|1] [--boot-offset-ms X] [--ppm Y]\n");
    return 2;
  }
  g_boot_mono = mono_ns();
  signal(SIGPIPE, SIG_IGN);   // a dropped center link must never kill the controller; errors come back as return codes
  char p[400], flash_dir[300], pki[300], p2[400];
  snprintf(flash_dir, sizeof flash_dir, "%s/flash/tsc%02d", g_rt, g_id);
  snprintf(pki, sizeof pki, "%s/pki", g_rt);
  g_osc.init((int64_t)(boot_off_ms * 1e6), ppm);
  svc::g_time = &g_ts;
  snprintf(p, sizeof p, "%s/logs/tsc%02d.log", g_rt, g_id);
  log_init(g_id, p, now_fn);
  g_ts.init(g_id, &g_osc, &g_gnss, &g_net, nb, nnb);
  logf("BOOT", "%s, intersection I%d; TCXO %+.2f ppm, boot clock error %+.0f ms; GNSS antenna %s", kFwVersion, g_id, ppm,
       boot_off_ms, gnss ? "with sky view" : "in urban canyon (no sky view)");
  snprintf(p, sizeof p, "%s/peer_mac.key", pki);
  snprintf(p2, sizeof p2, "%s/config_signing.pub", pki);
  if (!hal::crypto::init(p, p2)) logf("BOOT", "crypto keys missing: configurations cannot be verified");
  snprintf(p, sizeof p, "%s/logs/nmea/tsc%02d.nmea", g_rt, g_id);
  g_gnss.init(g_id, gnss, p);
  g_net.open(g_id);
  g_bp.open(port::backplane(g_id));
  g_probe.open(0);
  snprintf(p, sizeof p, "%s/logs/can/tsc%02d.candump", g_rt, g_id);
  g_can.open(g_id, p);
  g_lamp.open(g_id);
  g_store.init(flash_dir, g_id);
  if (!g_store.boot(&g_cfg)) {
    logf("CFG", "NO VALID CONFIGURATION: built-in fixed-time plan, no rules");
    g_cfg = svc::Config{};
    g_cfg.tsc = (uint8_t)g_id;
    g_cfg.own_ch = (uint8_t)g_id;
    g_cfg.n_subs = 1;
    g_cfg.subs[0] = (uint8_t)g_id;
    snprintf(g_cfg.tag, sizeof g_cfg.tag, "factory");
  }
  g_ts.set_tolerance_ms(g_cfg.sync_tol_ms);
  g_rec.init(flash_dir, g_id);
  g_msg.init(g_id, &g_net, &g_buf, &g_ts, &g_osc, flash_dir);
  g_msg.configure(g_cfg);
  g_det.init(&g_can);
  g_det.start();
  g_pc.init(g_id, &g_cfg, &g_ts, &g_det, &g_msg, &g_rec, &g_store, activated_fn);
  g_center.init(g_id, pki, health_fn, cfg_fn, &g_rec);
  g_wd.start(2000);
  g_after_init = true;
  logf("BOOT", "start-up complete in %.1f ms; C++ heap frozen from here", (mono_ns() - g_boot_mono) / 1e6);

  int64_t last_lamp = 0, last_probe = 0, last_sub = mono_ns();
  uint16_t last_lamps = 0xFFFF;
  bool wd_toggle = false;
  for (;;) {
    if (g_hang) {                         // injected software hang: stop kicking the watchdog
      logf("SIM", "main loop hung (injected)");
      for (;;) pause();
    }
    pollfd fds[6] = {{g_net.fd(), POLLIN, 0}, {g_can.fd(), POLLIN, 0}, {g_lamp.fd(), POLLIN, 0},
                     {g_bp.fd(), POLLIN, 0}, {g_center.sock(), POLLIN, 0}, {-1, 0, 0}};
    poll(fds, 5, 10);
    hal::PeerFrame f;
    while (g_net.recv(&f, g_osc)) {
      if (f.type == hal::ENV_CHAN) g_msg.on_frame(f);
      else g_ts.on_ptp(f);
    }
    hal::CanFrame cf;
    while (g_can.recv(&cf)) g_det.on_frame(cf);
    while (g_lamp.recv_status(&g_mmu)) {
    }
    backplane();
    g_ts.tick();
    g_det.tick();
    g_msg.process();
    g_pc.tick(g_mmu);
    int64_t m = mono_ns();
    uint16_t L = g_pc.lamps();
    if (L != last_lamps || m - last_lamp >= 100000000LL) {    // drive load switches + watchdog toggle to the MMU
      wd_toggle = !wd_toggle;
      g_lamp.drive(L, wd_toggle);
      last_lamps = L;
      last_lamp = m;
    }
    g_center.tick();
    if (m - last_probe >= 200000000LL) {
      last_probe = m;
      send_probe();
    }
    if (m - last_sub >= 10000000000LL) {
      last_sub = m;
      g_msg.resubscribe();
    }
    g_wd.kick();
  }
}
