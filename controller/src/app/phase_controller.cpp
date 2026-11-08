// Signal sequencer, mode manager and decision recording.
// Coordinated-actuated control on the shared clock: background cycle C with a
// per-intersection offset (green wave); detectors and rules adjust the split.
//   cycle t:  0 ........ 13 (sample) 14 (decide) ... main end | amber | all-red | side ... | amber | all-red | C
// The independent safety unit (MMU) enforces conflicts and clearances; this
// code only ever requests safe sequences.
#include <cmath>

#include "app/app.hpp"

namespace tsc::app {

namespace {
constexpr int64_t kMs = 1000000LL;
int64_t floor_div(int64_t a, int64_t b) {
  int64_t q = a / b;
  if ((a % b != 0) && ((a < 0) != (b < 0))) --q;
  return q;
}
char g_rec[8192];
Input g_in[24];
RuleResult g_res[kMaxRules];
const char* kPhase[] = {"STARTUP all-red", "MAIN green", "MAIN amber", "all-red", "SIDE green", "SIDE amber",
                        "all-red", "FLASH (safety unit)"};
const char* kMode[] = {"STARTUP", "ACTUATED", "DEGRADED", "FIXED_TIME", "FLASH"};
}  // namespace

void PhaseController::init(int id, svc::Config* cfg, svc::TimeService* ts, svc::DetectorService* det,
                           svc::MessageService* msg, svc::Recorder* rec, svc::ConfigStore* store, ActivatedFn on_act) {
  id_ = id;
  cfg_ = cfg;
  ts_ = ts;
  det_ = det;
  msg_ = msg;
  rec_ = rec;
  store_ = store;
  on_act_ = on_act;
  ph_start_m_ = mono_ns();
  logf("MODE", "start-up: all signal heads RED for 5 s");
}

const char* PhaseController::phase_name() const { return kPhase[ph_]; }
const char* PhaseController::mode_name() const { return kMode[mode_]; }

void PhaseController::set_phase(Phase p) {
  if (p != ph_) logf("PHASE", "%-15s -> %-15s (cycle t=%.1f s)", kPhase[ph_], kPhase[p], ct_s_);
  ph_ = p;
  ph_start_m_ = mono_ns();
}

uint16_t PhaseController::lamps() const {
  uint16_t L = 0;
  auto on = [&L](int head, int lamp) { L |= (uint16_t)(1u << (head * 3 + lamp)); };   // heads W,E,N,S; lamps R,A,G
  switch (ph_) {
    case MAIN_G: on(0, 2); on(1, 2); on(2, 0); on(3, 0); break;
    case MAIN_A: on(0, 1); on(1, 1); on(2, 0); on(3, 0); break;
    case SIDE_G: on(0, 0); on(1, 0); on(2, 2); on(3, 2); break;
    case SIDE_A: on(0, 0); on(1, 0); on(2, 1); on(3, 1); break;
    default: on(0, 0); on(1, 0); on(2, 0); on(3, 0); break;
  }
  if (bug_conflict && ph_ == MAIN_G) {             // injected software defect
    L &= (uint16_t)~(1u << 6);
    on(2, 2);
  }
  return L;
}

bool PhaseController::side_call() const {
  // a failed side detector counts as permanent demand, so the side street is still served
  return det_->present(3) || det_->present(4) || det_->quality(3) != DetQ::OK || det_->quality(4) != DetQ::OK;
}

bool PhaseController::fixed_time_needed(char* why, size_t n) const {
  if (!cfg_->valid) { snprintf(why, n, "no valid signed configuration in flash"); return true; }
  if (det_->card_silent(0) && det_->card_silent(1)) { snprintf(why, n, "both detector cards silent"); return true; }
  int bad = 0;
  for (int d = 1; d <= 4; ++d) bad += det_->quality(d) != DetQ::OK;
  if (bad >= 3) { snprintf(why, n, "%d of 4 stop-bar detectors failed", bad); return true; }
  return false;
}

void PhaseController::cycle_begin(int64_t cyc, int64_t now) {
  if (cur_cyc_ >= 0) msg_->end_of_cycle((uint32_t)cur_cyc_, cfg_->buffer_n);
  if (store_->due(now)) {
    uint32_t v;
    int64_t at;
    store_->pending(&v, &at);
    char res[200];
    if (store_->activate(cfg_, res, sizeof res)) {
      msg_->configure(*cfg_);
      ts_->set_tolerance_ms(cfg_->sync_tol_ms);
      if (on_act_) on_act_(cfg_->version, at, now, (uint32_t)cyc);
    }
  }
  cur_cyc_ = cyc;
  sampled_ = false;
  decided_ = false;
  main_end_ = (int64_t)cfg_->main_min_ms * kMs;
  if (ph_ == MAIN_G) {
    cycle_of_main_ = cyc;          // side was skipped: main green continues into this cycle
    side_skipped_ = false;
  }
  if (ph_ == STARTUP && mono_ns() - ph_start_m_ >= 5000 * kMs) {
    set_phase(MAIN_G);
    cycle_of_main_ = cyc;
    mode_ = M_ACTUATED;
    logf("MODE", "start-up complete: cycle %lld begins with MAIN green", (long long)cyc);
  }
}

void PhaseController::enter_cycle(int64_t ct) {
  const svc::Config& c = *cfg_;
  int64_t C = (int64_t)c.cycle_ms * kMs, amber = c.amber_ms * kMs, allred = c.allred_ms * kMs;
  if (ct < (int64_t)(c.main_min_ms - 2000) * kMs) {
    set_phase(MAIN_G);
    cycle_of_main_ = cur_cyc_;
  } else if (ct < C - amber - allred - (int64_t)c.side_min_ms * kMs) {
    set_phase(SIDE_G);
    side_cyc_ = cur_cyc_;
  } else {
    return;                        // stay all red until the next cycle begins
  }
  mode_ = M_ACTUATED;
  logf("MODE", "start-up complete: entering cycle at t=%.1f s with %s", ct / 1e9, kPhase[ph_]);
}

void PhaseController::tick(const hal::MmuStatus& mmu) {
  const svc::Config& c = *cfg_;
  int64_t now = ts_->now(), m = mono_ns();
  int64_t C = (int64_t)c.cycle_ms * kMs;
  int64_t rel = now - (int64_t)c.offset_ms * kMs;
  int64_t cyc = floor_div(rel, C), ct = rel - cyc * C;
  ct_s_ = ct / 1e9;
  if (mmu.state == 1) {
    if (ph_ != FLASHED) {
      logf("MODE", "safety unit forced FLASH (%s): lamp commands are ignored, controller holds all red", mmu.fault);
      set_phase(FLASHED);
      mode_ = M_FLASH;
    }
  } else if (ph_ == FLASHED) {
    logf("MODE", "safety unit released by technician: start-up sequence (all red 5 s, then cycle)");
    set_phase(STARTUP);
    mode_ = M_STARTUP;
  }
  if (cyc != cur_cyc_) cycle_begin(cyc, now);
  if (!sampled_ && ct >= (int64_t)c.sample_at_ms * kMs) {   // all 8 detectors at one defined instant
    svc::Reading r[kDet];
    det_->sample(r);
    msg_->send_own(r, (uint32_t)cyc, c.version);
    sampled_ = true;
  }
  int64_t amber = c.amber_ms * kMs, allred = c.allred_ms * kMs, el = m - ph_start_m_;
  switch (ph_) {
    case FLASHED:
      break;
    case STARTUP:
      if (el >= 5000 * kMs) enter_cycle(ct);
      break;
    case MAIN_G: {
      int64_t ct_eff = ct + (cyc - cycle_of_main_) * C;
      if (!decided_ && ct_eff >= (int64_t)c.main_min_ms * kMs) try_decide(ct_eff, now);
      if (decided_ && ct_eff >= main_end_ && el >= 6000 * kMs) {
        bool call = side_call();
        if (c.side_skip() && !call) {
          if (!side_skipped_) {
            side_skipped_ = true;
            logf("PHASE", "no side-street call on D3/D4: side phase skipped, main stays green");
          }
        } else if (!side_skipped_ || ct + 2 * (amber + allred) + (int64_t)c.side_min_ms * kMs <= C) {
          if (side_skipped_) logf("PHASE", "late side-street call: serving the side phase");
          side_skipped_ = false;
          set_phase(MAIN_A);
        }
      }
      break;
    }
    case MAIN_A:
      if (el >= amber) set_phase(MAIN_R);
      break;
    case MAIN_R:
      if (el >= allred) {
        set_phase(SIDE_G);
        side_cyc_ = cyc;
      }
      break;
    case SIDE_G:
      if (el >= (int64_t)c.side_min_ms * kMs && (ct >= C - amber - allred || cyc != side_cyc_)) set_phase(SIDE_A);
      break;
    case SIDE_A:
      if (el >= amber) set_phase(SIDE_R);
      break;
    case SIDE_R:
      if (el >= allred) {
        set_phase(MAIN_G);
        cycle_of_main_ = ct > C / 2 ? cyc + 1 : cyc;
      }
      break;
  }
}

void PhaseController::try_decide(int64_t ct_eff, int64_t now) {
  const svc::Config& c = *cfg_;
  int64_t C = (int64_t)c.cycle_ms * kMs;
  int64_t cap = C - 2 * (int64_t)(c.amber_ms + c.allred_ms) * kMs - (int64_t)c.side_min_ms * kMs;
  char why[160];
  if (fixed_time_needed(why, sizeof why)) {
    if (mode_ != M_FIXED) svc::alarm("MODE_FIXED", "major", "local fixed-time plan: %s", why);
    mode_ = M_FIXED;
    Decision d{ACT_EXTEND, 4000, -1};
    main_end_ = ct_eff + 4000 * kMs;
    if (main_end_ > cap) main_end_ = cap;
    decided_ = true;
    write_record(now, nullptr, 0, nullptr, d, 0, true, why);
    return;
  }
  if (mode_ == M_FIXED) svc::alarm("MODE_ACTUATED", "info", "detectors healthy again: actuated control resumed");
  int n = 0;
  for (int i = 0; i < c.n_rules; ++i)
    for (int k = 0; k < c.rules[i].n; ++k)
      if (find_input(g_in, n, c.rules[i].c[k].src, c.rules[i].c[k].det) < 0 && n < 24) {
        g_in[n].src = c.rules[i].c[k].src;
        g_in[n].det = c.rules[i].c[k].det;
        ++n;
      }
  int64_t cycle_start = (int64_t)c.offset_ms * kMs + cur_cyc_ * C;
  bool wait = false;
  for (int i = 0; i < n; ++i) {
    g_in[i].why[0] = 0;
    g_in[i].sample_ns = 0;
    g_in[i].seq = 0;
    memset(&g_in[i].r, 0, sizeof g_in[i].r);
    g_in[i].look = msg_->lookup(g_in[i].src, g_in[i].det, cycle_start, c.legacy_prev_cycle(), &g_in[i].r,
                                &g_in[i].sample_ns, &g_in[i].seq, g_in[i].why, sizeof g_in[i].why);
    if (g_in[i].look == svc::Look::WAIT) wait = true;
  }
  int64_t waited = ct_eff - (int64_t)c.main_min_ms * kMs;
  if (wait && waited < (int64_t)c.up_wait_ms * kMs) return;   // inputs in flight: hold green, bounded
  for (int i = 0; i < n; ++i)
    if (g_in[i].look == svc::Look::WAIT) g_in[i].look = svc::Look::MISSING;
  Decision d;
  evaluate(c, g_in, n, g_res, &d);
  main_end_ = ct_eff + (d.act == ACT_EXTEND ? (int64_t)d.ext_ms * kMs : 0);
  if (main_end_ > cap) main_end_ = cap;
  decided_ = true;
  bool missing = false;
  for (int i = 0; i < c.n_rules; ++i) missing |= g_res[i].st == MISSING;
  Mode nm = missing ? M_DEGRADED : M_ACTUATED;
  if (nm != mode_) logf("MODE", "%s -> %s", kMode[mode_], kMode[nm]);
  mode_ = nm;
  write_record(now, g_in, n, g_res, d, waited > 0 ? waited : 0, false, nullptr);
}

void PhaseController::write_record(int64_t now, const Input* in, int n_in, const RuleResult* res, const Decision& d,
                                   int64_t waited, bool fixed, const char* fixed_why) {
  const svc::Config& c = *cfg_;
  char ts[40], id[24], buf[64];
  fmt_utc(now, ts, sizeof ts);
  snprintf(id, sizeof id, "I%02d-%06u", id_, rec_->next_seq());
  JsonW w(g_rec, sizeof g_rec);
  double bound = ts_->err_bound_ms();
  w.obj().kv("rec", id).kv("seq", rec_->next_seq()).kv("tsc", id_).kv("cycle", cur_cyc_).kv("t_utc", ts).kv("t_ns", now);
  w.kv("sync", to_str(ts_->quality())).kvf("sync_bound_ms", bound > 1e6 ? -1.0 : bound, 3);
  w.kv("mode", fixed ? "FIXED_TIME" : kMode[mode_]);
  w.obj("cfg").kv("ver", c.version).kv("tag", c.tag).kv("hash", c.hash).kv("git", c.git).kvb("legacy_prev_cycle", c.legacy_prev_cycle())
      .kv("buffer_n", msg_->buffer_n_override ? msg_->buffer_n_override : (int)c.buffer_n).end_obj();
  if (fixed) w.kv("fixed_reason", fixed_why);
  int64_t last = 0;
  bool up = false;
  w.arr("inputs");
  for (int i = 0; i < n_in; ++i) {
    const Input& x = in[i];
    w.obj().kv("src", x.src).kv("det", x.det).kv("ch", x.src).kv("status", x.look == svc::Look::OK ? "OK" : "MISSING");
    if (x.sample_ns) {
      w.kv("q", to_str(x.r.q)).kv("presence", x.r.presence).kv("count", x.r.count);
      w.kvf("occ_pct", x.r.occ_permille / 10.0, 1).kvf("gap_s", x.r.gap_ds / 10.0, 1);
      fmt_utc(x.sample_ns, buf, sizeof buf);
      w.kv("sample_utc", buf).kvf("age_s", (now - x.sample_ns) / 1e9, 3).kv("seq", x.seq);
    }
    if (x.look != svc::Look::OK) w.kv("why", x.why);
    w.end_obj();
    if (x.look == svc::Look::OK && x.sample_ns > last) last = x.sample_ns;
    up |= x.src != id_;   // one decision per cycle: if it needs an upstream reading, the 5 s deadline applies
  }
  w.end_arr();
  w.arr("rules");
  char sum[120] = "";
  for (int i = 0; res && i < c.n_rules; ++i) {
    const svc::Rule& r = c.rules[i];
    const RuleResult& x = res[i];
    const char* st = x.st == FIRED ? "FIRED" : x.st == NOT_FIRED ? "NOT_FIRED" : "MISSING";
    w.obj().kv("id", r.id).kv("prio", r.prio).kv("status", st);
    snprintf(buf, sizeof buf, "%s%s", act_text(x.act), x.act == ACT_EXTEND ? "" : "");
    if (x.act == ACT_EXTEND) snprintf(buf, sizeof buf, "extend green by %.1f s", x.ms / 1000.0);
    w.kv("result", buf);
    if (x.st == MISSING)
      w.kv("missing", x.missing_input >= 0 ? in[x.missing_input].why : "input not available");
    w.end_obj();
    size_t l = strlen(sum);
    snprintf(sum + l, sizeof sum - l, "%s%s %s", i ? ", " : "", r.id, st);
  }
  w.end_arr();
  const char* an = d.act == ACT_EXTEND ? "EXTEND" : d.act == ACT_HOLD ? "HOLD" : "NONE";
  const char* by = fixed ? "fixed-time plan" : d.winner >= 0 ? c.rules[d.winner].id : "none";
  double lat = last ? (now - last) / 1e9 : -1, dl = (up ? c.dl_up_ms : c.dl_local_ms) / 1000.0;
  w.obj("decision").kv("action", an).kvf("ext_s", d.ext_ms / 1000.0, 1).kv("by", by);
  w.kvf("main_end_s", main_end_ / 1e9, 1).kvf("waited_s", waited / 1e9, 2);
  if (last) w.kvf("latency_s", lat, 3).kvf("deadline_s", dl, 1).kvb("deadline_met", lat <= dl);
  w.kv("deadline_basis", up ? "decision needs upstream readings (5 s)" : "local readings only (2 s)");
  w.end_obj();
  w.kv("prev", rec_->prev_hash()).end_obj();
  if (w.overflow()) {
    logf("REC", "record too large, dropped");
    return;
  }
  rec_->append(g_rec, w.len(), sizeof g_rec);
  snprintf(last_rec_id_, sizeof last_rec_id_, "%s", id);
  if (fixed)
    snprintf(last_dec_, sizeof last_dec_, "%s FIXED-TIME +4.0 s (%s)", id, fixed_why);
  else if (last)
    snprintf(last_dec_, sizeof last_dec_, "%s %s %.1f s by %s, %.2f s after last reading (limit %.0f s)", id, an,
             d.ext_ms / 1000.0, by, lat, dl);
  else
    snprintf(last_dec_, sizeof last_dec_, "%s %s by %s (no valid inputs)", id, an, by);
  logf("RULE", "cycle %lld %s | rules: %s%s", (long long)cur_cyc_, last_dec_, sum, waited > 100 * kMs ? " | green held for in-flight upstream reading" : "");
}

}  // namespace tsc::app
