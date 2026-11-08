// Time service: three-tier hierarchy from the design.
//   Tier 1 GNSS + 1PPS     -> absolute UTC
//   Tier 2 PTP from peers   -> IEEE 1588-2008 two-step delay request-response over
//                              the reliable peer network (boundary-clock style, max 2 hops)
//   Tier 3 TCXO holdover    -> 2 ppm bound, valid while the error bound < 100 ms
// Rules: never step after start-up (slew <= 10 %), reject GNSS jumps that peers
// contradict (spoofing), and the control center is never a time source.
#include <cmath>
#include <cstdlib>

#include "services/services.hpp"

namespace tsc::svc {

namespace {
constexpr int64_t kSec = 1000000000LL;
constexpr int64_t kTaiUtc = 37;          // TAI - UTC (s); PTP timescale is TAI
constexpr double kTcxoPpm = 2.0;         // holdover oscillator specification
constexpr double kMaxSlew = 0.10;        // 100 ms per second
constexpr int64_t kJump = 50 * 1000000LL;

void put16(uint8_t* b, uint16_t v) { b[0] = (uint8_t)(v >> 8); b[1] = (uint8_t)v; }
uint16_t get16(const uint8_t* b) { return (uint16_t)((b[0] << 8) | b[1]); }

void put_ts(uint8_t* b, int64_t utc_ns) {
  int64_t tai = utc_ns + kTaiUtc * kSec;
  uint64_t s = (uint64_t)(tai / kSec);
  uint32_t ns = (uint32_t)(tai % kSec);
  for (int i = 0; i < 6; ++i) b[i] = (uint8_t)(s >> (8 * (5 - i)));
  for (int i = 0; i < 4; ++i) b[6 + i] = (uint8_t)(ns >> (8 * (3 - i)));
}

int64_t get_ts(const uint8_t* b) {
  uint64_t s = 0;
  uint32_t ns = 0;
  for (int i = 0; i < 6; ++i) s = (s << 8) | b[i];
  for (int i = 0; i < 4; ++i) ns = (ns << 8) | b[6 + i];
  return (int64_t)s * kSec + ns - kTaiUtc * kSec;
}

void identity(int id, uint8_t out[8]) {
  const uint8_t v[8] = {0x02, 0x54, 0x53, 0xFF, 0xFE, 0x00, 0x00, (uint8_t)id};
  memcpy(out, v, 8);
}
}  // namespace

void TimeService::init(int id, hal::Oscillator* osc, hal::Gnss* gnss, hal::PeerNet* net, const uint8_t* nb, int nnb) {
  id_ = id;
  osc_ = osc;
  gnss_ = gnss;
  net_ = net;
  nnb_ = nnb > 4 ? 4 : nnb;
  for (int i = 0; i < nnb_; ++i) {
    nb_[i] = nb[i];
    peers_[i].id = nb[i];
    peers_[i].gm_class = 255;
  }
  ref_ = osc_->raw();
  last_tick_mono_ = mono_ns();
  last_sync_tx_mono_ = last_tick_mono_;
  logf("TIME", "boot: free-running TCXO, no time source yet -> local-only mode");
}

int64_t TimeService::sys_from_raw(int64_t raw) const {
  return raw + off_ + (int64_t)((double)(raw - ref_) * freq_);
}

void TimeService::rebase() {
  int64_t r = osc_->raw();
  int64_t s = sys_from_raw(r);
  ref_ = r;
  off_ = s - r;
}

uint8_t TimeService::clock_class() const {
  if (q_ == SyncQ::GNSS) return 6;
  if (q_ == SyncQ::PTP && master_steps_ + 1 < 3) return 6;
  if (q_ == SyncQ::HOLDOVER) return 7;
  return 248;
}

uint16_t TimeService::steps() const {
  return q_ == SyncQ::PTP ? (uint16_t)(master_steps_ + 1) : 0;
}

double TimeService::holdover_left_h() const {
  if (q_ != SyncQ::HOLDOVER) return 0;
  return (tol_ms_ - bound_ms_) / (kTcxoPpm * 1e-6 * 3600e3);
}

void TimeService::tick() {
  int64_t m = mono_ns();
  double dt = (double)(m - last_tick_mono_) / 1e9;
  last_tick_mono_ = m;
  if (pending_) {                                   // slew, never jump
    int64_t mx = (int64_t)(dt * kMaxSlew * 1e9);
    if (mx < 1) mx = 1;
    int64_t a = pending_ > 0 ? (pending_ < mx ? pending_ : mx) : (pending_ > -mx ? pending_ : -mx);
    off_ += a;
    pending_ -= a;
  }
  int64_t raw_edge, utc;
  if (gnss_->poll(*osc_, &raw_edge, &utc)) {
    gnss_theta_ = sys_from_raw(raw_edge) - utc;
    gnss_meas_mono_ = m;
    measurement(gnss_theta_, true);
  }
  if (m - last_sync_tx_mono_ >= kSec) {
    last_sync_tx_mono_ = m;
    bmca();
    send_announce();
    if (clock_class() <= 6) ptp_master_tx();
  }
  update_quality();
}

void TimeService::measurement(int64_t theta, bool from_gnss) {
  int64_t m = mono_ns();
  if (from_gnss && gnss_rejected_) {
    if (llabs(theta) < 20 * 1000000LL) {
      gnss_rejected_ = false;
      gnss_jump_count_ = 0;
      logf("GNSS", "receiver agrees with the local clock again (%+.3f ms): accepted", theta / 1e6);
    } else {
      return;
    }
  }
  bool gnss_usable = gnss_->locked() && !gnss_rejected_ && (m - gnss_meas_mono_) < 2500000000LL;
  bool steer = from_gnss || !gnss_usable;
  if (!steer) return;                               // PTP kept as cross-check only
  if (!ever_synced_) {
    off_ -= theta;
    pending_ = 0;
    ever_synced_ = true;
    (from_gnss ? gnss_theta_ : ptp_theta_) = 0;
    logf("TIME", "first lock via %s: STEP %+.3f ms (allowed only at start-up, before actuated control)",
         from_gnss ? "GNSS" : "PTP", -theta / 1e6);
    return;
  }
  if (from_gnss && llabs(theta) > kJump) {
    bool peer_ok = master_ && (m - ptp_meas_mono_) < 3 * kSec && llabs(ptp_theta_) < 20 * 1000000LL;
    if (peer_ok) {
      if (!gnss_rejected_)
        alarm("GNSS_SPOOF", "major", "GNSS time jumped %+.0f ms but PTP peer I%d agrees with the local clock "
              "(%+.3f ms): GNSS rejected, spoofing suspected", theta / 1e6, master_, ptp_theta_ / 1e6);
      gnss_rejected_ = true;
      return;
    }
    if (++gnss_jump_count_ < 10) {
      logf("GNSS", "jump of %+.0f ms not confirmed by any peer (%d/10): holding", theta / 1e6, gnss_jump_count_);
      return;
    }
  } else {
    gnss_jump_count_ = 0;
  }
  // PI servo. GNSS 1PPS is clean; PTP over a software-timestamped path is noisy,
  // so its gains are lower (a hardware-timestamped PHC would allow tighter loops).
  double kp = from_gnss ? 0.7 : 0.25, ki = from_gnss ? 0.3 : 0.02;
  rebase();
  freq_ -= ki * (double)theta / 1e9;                // integral term, 1 s interval
  if (freq_ > 0.02) freq_ = 0.02;
  if (freq_ < -0.02) freq_ = -0.02;
  int64_t corr = -(int64_t)(kp * (double)theta);    // proportional term
  if (llabs(corr) < 1000000) {
    off_ += corr;
    pending_ = 0;
  } else {
    pending_ = corr;
  }
  if (++meas_log_ % 15 == 0) {
    if (from_gnss)
      logf("GNSS", "PPS offset %+.4f ms, frequency correction %+.3f ppm", theta / 1e6, freq_ * 1e6);
    else
      logf("PTP", "offset from master I%d %+.4f ms, path delay %.4f ms, freq %+.3f ppm", master_, theta / 1e6,
           ptp_delay_ / 1e6, freq_ * 1e6);
  }
}

void TimeService::update_quality() {
  int64_t m = mono_ns();
  bool g = ever_synced_ && gnss_->locked() && !gnss_rejected_ && (m - gnss_meas_mono_) < 2500000000LL;
  bool p = ever_synced_ && master_ && master_class_ <= 6 && (m - ptp_meas_mono_) < 3500000000LL;
  SyncQ nq;
  if (g) {
    nq = SyncQ::GNSS;
    bound_ms_ = 0.001 + fabs((double)gnss_theta_) / 1e6;
  } else if (p) {
    nq = SyncQ::PTP;
    bound_ms_ = fabs((double)ptp_theta_) / 1e6 + (double)ptp_delay_ / 2e6 + 0.001;
  } else if (ever_synced_) {
    if (q_ == SyncQ::GNSS || q_ == SyncQ::PTP) {
      loss_mono_ = m;
      bound_at_loss_ = bound_ms_;
    }
    bound_ms_ = bound_at_loss_ + kTcxoPpm * 1e-6 * (double)(m - loss_mono_) / 1e6;
    nq = bound_ms_ <= tol_ms_ ? SyncQ::HOLDOVER : SyncQ::UNSYNC;
  } else {
    nq = SyncQ::UNSYNC;
    bound_ms_ = 1e9;
  }
  if (nq != q_) {
    if (nq == SyncQ::HOLDOVER)
      alarm("SYNC_HOLDOVER", "warn", "GNSS and PTP lost: holdover on TCXO (%.0f ppm), stays within %.0f ms for %.1f h",
            kTcxoPpm, tol_ms_, (tol_ms_ - bound_ms_) / (kTcxoPpm * 1e-6 * 3600e3));
    else if (nq == SyncQ::UNSYNC && ever_synced_)
      alarm("SYNC_LOST", "major", "clock error bound %.1f ms exceeds %.0f ms: upstream rules fall back, records flagged",
            bound_ms_, tol_ms_);
    else if (nq == SyncQ::PTP)
      alarm("SYNC_PTP", "info", "time from PTP master I%d (%u hop%s from GNSS)", master_, master_steps_ + 1,
            master_steps_ ? "s" : "");
    else if (nq == SyncQ::GNSS)
      alarm("SYNC_GNSS", "info", "time from GNSS (1PPS), error bound %.3f ms", bound_ms_);
  }
  q_ = nq;
  switch (q_) {
    case SyncQ::GNSS: snprintf(state_txt_, sizeof state_txt_, "GNSS 1PPS locked"); break;
    case SyncQ::PTP: snprintf(state_txt_, sizeof state_txt_, "PTP slave of I%d (%u hop%s)", master_, master_steps_ + 1, master_steps_ ? "s" : ""); break;
    case SyncQ::HOLDOVER: snprintf(state_txt_, sizeof state_txt_, "holdover TCXO, %.1f h left", holdover_left_h()); break;
    default: snprintf(state_txt_, sizeof state_txt_, ever_synced_ ? "UNSYNCHRONIZED (bound exceeded)" : "not yet synchronized (start-up)");
  }
}

int TimeService::build_header(uint8_t* b, uint8_t type, uint16_t len, uint16_t seq, uint8_t ctrl, bool two_step) const {
  memset(b, 0, len);
  b[0] = type;                  // transportSpecific 0 | messageType
  b[1] = 0x02;                  // versionPTP 2
  put16(b + 2, len);
  b[4] = 0;                     // domainNumber
  b[6] = two_step ? 0x02 : 0;   // flagField: twoStepFlag
  identity(id_, b + 20);        // sourcePortIdentity.clockIdentity
  put16(b + 28, 1);             // portNumber
  put16(b + 30, seq);
  b[32] = ctrl;
  b[33] = type == 0x1 ? 0x7F : 0x00;
  return len;
}

void TimeService::send_announce() {
  uint8_t b[64];
  build_header(b, 0xB, 64, ++seq_ann_, 5, false);
  b[7] = 0x0C;                                    // ptpTimescale | currentUtcOffsetValid
  put_ts(b + 34, now());
  put16(b + 44, (uint16_t)kTaiUtc);               // currentUtcOffset
  b[47] = 128;                                    // grandmasterPriority1
  uint8_t cls = clock_class();
  b[48] = cls;
  b[49] = cls == 6 ? 0x21 : 0xFE;                 // clockAccuracy: 100 ns / unknown
  b[50] = 0xFF;
  b[51] = 0xFF;
  b[52] = 128;                                    // grandmasterPriority2
  if (q_ == SyncQ::PTP) memcpy(b + 53, master_gm_, 8);
  else identity(id_, b + 53);
  put16(b + 61, steps());
  b[63] = (q_ == SyncQ::GNSS || q_ == SyncQ::PTP) ? 0x20 : 0xA0;   // GPS / internal oscillator
  net_->send(hal::ENV_PTP_GENERAL, 0, 0x0B, b, 64, *osc_);
}

void TimeService::ptp_master_tx() {
  uint8_t s[44], f[44];
  uint16_t seq = ++seq_sync_;
  build_header(s, 0x0, 44, seq, 0, true);
  put_ts(s + 34, now());                          // approximate; precise time follows
  int64_t tx = net_->send(hal::ENV_PTP_EVENT, 0, 0x00, s, 44, *osc_);
  build_header(f, 0x8, 44, seq, 2, false);
  put_ts(f + 34, sys_from_raw(tx));               // preciseOriginTimestamp (hardware tx stamp)
  net_->send(hal::ENV_PTP_GENERAL, 0, 0x08, f, 44, *osc_);
}

void TimeService::bmca() {
  int64_t m = mono_ns();
  uint8_t self[8];
  identity(id_, self);
  int best = -1;
  for (int i = 0; i < nnb_; ++i) {
    const Peer& p = peers_[i];
    if (m - p.seen_mono > 3500000000LL || p.gm_class != 6 || p.steps >= 2) continue;
    if (memcmp(p.gm_id, self, 8) == 0) continue;
    if (best < 0 || p.steps < peers_[best].steps ||
        (p.steps == peers_[best].steps && p.id < peers_[best].id))
      best = i;
  }
  uint8_t nm = best >= 0 ? peers_[best].id : 0;
  if (nm != master_) {
    if (nm)
      logf("PTP", "BMCA: selected master I%d (GM %02X..%02X, class %u, stepsRemoved %u)", nm,
           peers_[best].gm_id[0], peers_[best].gm_id[7], peers_[best].gm_class, peers_[best].steps);
    else
      logf("PTP", "BMCA: no usable master on the peer network");
    have_t2_ = dreq_out_ = false;
  }
  master_ = nm;
  if (best >= 0) {
    memcpy(master_gm_, peers_[best].gm_id, 8);
    master_class_ = peers_[best].gm_class;
    master_steps_ = peers_[best].steps;
  } else {
    master_class_ = 255;
  }
}

void TimeService::on_ptp(const hal::PeerFrame& f) {
  const uint8_t* b = f.data;
  if (f.len < 34 || (b[1] & 0x0F) != 2) return;
  uint8_t type = b[0] & 0x0F;
  uint16_t seq = get16(b + 30);
  uint8_t src = f.src;
  switch (type) {
    case 0xB: {                                   // Announce
      if (f.len < 64) return;
      for (int i = 0; i < nnb_; ++i) {
        if (peers_[i].id != src) continue;
        peers_[i].seen_mono = mono_ns();
        peers_[i].gm_class = b[48];
        memcpy(peers_[i].gm_id, b + 53, 8);
        peers_[i].steps = get16(b + 61);
        peers_[i].time_source = b[63];
      }
      break;
    }
    case 0x0:                                     // Sync (two-step)
      if (src == master_) {
        rx_sync_seq_ = seq;
        t2_ = sys_from_raw(f.rx_raw);
        have_t2_ = true;
      }
      break;
    case 0x8:                                     // Follow_Up
      if (src == master_ && have_t2_ && seq == rx_sync_seq_ && f.len >= 44) {
        t1_ = get_ts(b + 34);
        uint8_t d[44];
        build_header(d, 0x1, 44, ++seq_dreq_, 1, false);
        put_ts(d + 34, now());
        int64_t tx = net_->send(hal::ENV_PTP_EVENT, master_, 0x01, d, 44, *osc_);
        t3_ = sys_from_raw(tx);
        dreq_out_ = true;
      }
      break;
    case 0x1: {                                   // Delay_Req -> answer as master
      if (clock_class() > 6) return;
      int64_t t4 = sys_from_raw(f.rx_raw);
      uint8_t r[54];
      build_header(r, 0x9, 54, seq, 3, false);
      put_ts(r + 34, t4);
      memcpy(r + 44, b + 20, 10);                 // requestingPortIdentity
      net_->send(hal::ENV_PTP_GENERAL, src, 0x09, r, 54, *osc_);
      break;
    }
    case 0x9: {                                   // Delay_Resp
      uint8_t self[8];
      identity(id_, self);
      if (src != master_ || !dreq_out_ || seq != seq_dreq_ || f.len < 54 || memcmp(b + 44, self, 8) != 0) return;
      int64_t t4 = get_ts(b + 34);
      int64_t ms = t2_ - t1_, sm = t4 - t3_;
      ptp_theta_ = (ms - sm) / 2;
      ptp_delay_ = (ms + sm) / 2;
      ptp_meas_mono_ = mono_ns();
      dreq_out_ = have_t2_ = false;
      measurement(ptp_theta_, false);
      break;
    }
    default:
      break;
  }
}

}  // namespace tsc::svc
