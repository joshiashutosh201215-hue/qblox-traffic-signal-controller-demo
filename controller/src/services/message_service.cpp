// Message service + freshness and liveness.
// One authenticated message per cycle per channel: sample time on the shared
// clock, sync quality, sequence, configuration version, 8 readings, AES-CMAC-64.
// Receive path: platform buffer (10 slots, silent drop) -> verify MAC -> reject
// replays -> detect losses from sequence gaps (the platform itself never warns).
#include <cstdlib>

#include "services/services.hpp"

namespace tsc::svc {

namespace {
uint32_t rd32(const uint8_t* b) { uint32_t v; memcpy(&v, b, 4); return v; }
int64_t rd64(const uint8_t* b) { int64_t v; memcpy(&v, b, 8); return v; }
uint16_t rd16(const uint8_t* b) { uint16_t v; memcpy(&v, b, 2); return v; }
}  // namespace

void MessageService::init(int id, hal::PeerNet* net, hal::PlatformBuffer* buf, TimeService* ts,
                          hal::Oscillator* osc, const char* flash_dir) {
  id_ = id;
  net_ = net;
  buf_ = buf;
  ts_ = ts;
  osc_ = osc;
  snprintf(seq_file_, sizeof seq_file_, "%s/msg_seq", flash_dir);
  uint8_t b[16];
  int n = 0;
  if (hal::flash::read_file(seq_file_, b, sizeof b - 1, &n)) {
    b[n] = 0;
    seq_ = (uint32_t)strtoul((const char*)b, nullptr, 10) + 16;   // never reuse a sequence number
  }
}

void MessageService::configure(const Config& cfg) {
  own_ch_ = cfg.own_ch;
  del_intra_ms_ = cfg.del_intra_ms;
  del_inter_ms_ = cfg.del_inter_ms;
  del_tol_ms_ = cfg.del_tol_ms;
  ChanState old[kMaxSubs];
  int old_n = n_;
  for (int i = 0; i < n_; ++i) old[i] = st_[i];
  n_ = cfg.n_subs;
  for (int i = 0; i < n_; ++i) {
    uint8_t ch = cfg.subs[i];
    st_[i] = ChanState{};
    for (int j = 0; j < old_n; ++j)
      if (old[j].ch == ch) st_[i] = old[j];      // keep sequence and liveness state
    st_[i].ch = ch;
    st_[i].src = i == 0 ? (uint8_t)id_ : cfg.up[i - 1];
    subs_[i] = ch;
  }
  resubscribe();
  char list[64] = "";
  for (int i = 0; i < n_; ++i) {
    size_t l = strlen(list);
    snprintf(list + l, sizeof list - l, "%s%u", i ? "," : "", subs_[i]);
  }
  logf("CHAN", "subscribed to channels [%s] (own %u); buffer emptied every %u cycle(s)", list, own_ch_, cfg.buffer_n);
}

void MessageService::resubscribe() { net_->subscribe(subs_, n_); }

void MessageService::send_own(const Reading r[kDet], uint32_t cycle, uint32_t cfg_ver) {
  uint8_t m[kChanMsgLen] = {0};
  m[0] = 'T';
  m[1] = 'S';
  m[2] = 1;                        // schema version
  m[3] = 1;                        // device class: vehicle detectors
  m[4] = own_ch_;
  m[5] = (uint8_t)id_;
  m[6] = (uint8_t)ts_->quality();
  m[7] = kDet;
  ++seq_;
  memcpy(m + 8, &seq_, 4);
  memcpy(m + 12, &cfg_ver, 4);
  int64_t now = ts_->now();
  memcpy(m + 16, &now, 8);
  memcpy(m + 24, &cycle, 4);
  for (int i = 0; i < kDet; ++i) {
    uint8_t* p = m + 32 + 8 * i;
    p[0] = r[i].det;
    p[1] = (uint8_t)r[i].q;
    p[2] = r[i].presence;
    p[3] = r[i].count;
    memcpy(p + 4, &r[i].occ_permille, 2);
    memcpy(p + 6, &r[i].gap_ds, 2);
  }
  hal::crypto::cmac64(m, 96, m + 96);
  net_->send(hal::ENV_CHAN, 0, own_ch_, m, kChanMsgLen, *osc_);
  ++sent;
  char s[16];
  int l = snprintf(s, sizeof s, "%u", seq_);
  hal::flash::write_file_atomic(seq_file_, (const uint8_t*)s, l);
}

void MessageService::on_frame(const hal::PeerFrame& f) {
  if (f.type != hal::ENV_CHAN || f.len != kChanMsgLen) return;
  buf_->push(f.data, ts_->sys_from_raw(f.rx_raw));
}

ChanState* MessageService::find_src(uint8_t tsc) {
  for (int i = 0; i < n_; ++i)
    if (st_[i].src == tsc) return &st_[i];
  return nullptr;
}

const ChanState* MessageService::state_for(uint8_t tsc) const {
  for (int i = 0; i < n_; ++i)
    if (st_[i].src == tsc) return &st_[i];
  return nullptr;
}

void MessageService::process() {
  for (int i = 0; i < buf_->size(); ++i) {
    hal::BufferedMsg& b = buf_->at(i);
    if (b.processed) continue;
    b.processed = true;
    const uint8_t* m = b.data;
    uint8_t ch = m[4], src = m[5];
    if (m[0] != 'T' || m[1] != 'S' || m[2] != 1) {
      logf("CHAN", "channel %u: unknown schema %u ignored (newer device type?)", ch, m[2]);
      continue;
    }
    uint8_t mac[8];
    hal::crypto::cmac64(m, 96, mac);
    if (memcmp(mac, m + 96, 8) != 0) {
      ++mac_fail;
      alarm("SEC_MAC", "major", "channel %u: AES-CMAC check failed, message claiming to be from I%u dropped "
            "(forged or corrupted)", ch, src);
      continue;
    }
    ChanState* s = nullptr;
    for (int k = 0; k < n_; ++k)
      if (st_[k].ch == ch) s = &st_[k];
    if (!s) continue;
    uint32_t seq = rd32(m + 8);
    if (s->have && seq <= s->seq) {
      ++replays;
      alarm("SEC_REPLAY", "major", "channel %u: sequence %u not newer than %u, replayed message dropped", ch, seq, s->seq);
      continue;
    }
    if (s->have && seq > s->seq + 1) {
      uint32_t gap = seq - s->seq - 1;
      if (gap < 16) {
        s->lost += gap;
        lost_total += gap;
        alarm("MSG_LOSS", "major", "channel %u (I%u): %u message(s) missing, sequence %u -> %u: platform buffer "
              "overflow or link loss", ch, src, gap, s->seq, seq);
      } else {
        logf("CHAN", "channel %u: I%u restarted (sequence epoch %u -> %u)", ch, src, s->seq, seq);
      }
    }
    if (s->have) {
      memcpy(s->prev, s->msg, kChanMsgLen);
      s->prev_arrival = s->arrival_sys;
      s->prev_sample = s->sample_ns;
      s->prev_seq = s->seq;
      s->have_prev = true;
    }
    memcpy(s->msg, m, kChanMsgLen);
    s->arrival_sys = b.arrival_sys;
    s->sample_ns = rd64(m + 16);
    s->seq = seq;
    s->cycle = rd32(m + 24);
    s->syncq = (SyncQ)m[6];
    s->have = true;
    s->rx_this_cycle = true;
  }
}

void MessageService::end_of_cycle(uint32_t cycle_no, int buffer_n) {
  for (int i = 0; i < n_; ++i) {
    ChanState& s = st_[i];
    if (s.rx_this_cycle) {
      s.silent_cycles = 0;
      if (s.live == ChanState::SILENT || s.live == ChanState::RECOVERING) {
        ++s.good_cycles;
        s.live = ChanState::RECOVERING;
        if (s.good_cycles >= 2) {
          s.live = ChanState::OK;
          alarm("NEIGHBOR_OK", "info", "I%u healthy for %d cycles: its rules re-enabled", s.src, s.good_cycles);
        }
      } else {
        s.live = ChanState::OK;
      }
    } else {
      ++s.silent_cycles;
      s.good_cycles = 0;
      if (s.live != ChanState::SILENT && (s.live == ChanState::OK || s.silent_cycles >= 2)) {
        s.live = ChanState::SILENT;
        alarm("NEIGHBOR_SILENT", "major", "no message from I%u on channel %u in a whole cycle (sequence stalled at %u): "
              "rules using I%u take their fallback", s.src, s.ch, s.seq, s.src);
      }
    }
    s.rx_this_cycle = false;
  }
  int n = buffer_n_override ? buffer_n_override : buffer_n;
  if (n <= 1 || cycle_no % (uint32_t)n == 0) buf_->clear();
}

Look MessageService::lookup(uint8_t src, uint8_t det, int64_t cycle_start, bool legacy, Reading* out,
                            int64_t* sample_ns, uint32_t* seq, char* why, size_t n) {
  ChanState* s = find_src(src);
  bool own = src == id_;
  if (!s) { snprintf(why, n, "not subscribed to I%u", src); return Look::MISSING; }
  if (!own && !ts_->synced()) { snprintf(why, n, "own clock not synchronized (local-only mode)"); return Look::MISSING; }
  if (s->live == ChanState::SILENT) {
    snprintf(why, n, "I%u silent for %d cycle(s) (sequence stalled at %u)", src, s->silent_cycles, s->seq);
    return Look::MISSING;
  }
  if (s->live == ChanState::RECOVERING) {
    snprintf(why, n, "I%u back, re-enabled after 2 good cycles (%d/2)", src, s->good_cycles);
    return Look::MISSING;
  }
  if (!s->have) { snprintf(why, n, "no message from I%u yet", src); return Look::WAIT; }
  const uint8_t* m = s->msg;
  int64_t smp = s->sample_ns, arr = s->arrival_sys;
  uint32_t sq = s->seq;
  if (legacy && !own && smp >= cycle_start && s->have_prev) {   // the proposal: previous cycle's reading
    m = s->prev;
    smp = s->prev_sample;
    arr = s->prev_arrival;
    sq = s->prev_seq;
  }
  if (!legacy && smp < cycle_start) {
    snprintf(why, n, "latest I%u reading was sampled %.1f s before this cycle started (previous cycle)", src,
             (double)(cycle_start - smp) / 1e9);
    return Look::WAIT;
  }
  if ((SyncQ)m[6] == SyncQ::UNSYNC && !own) {
    snprintf(why, n, "I%u clock not synchronized: its timestamp cannot prove the reading is current", src);
    return Look::MISSING;
  }
  double d = (double)(arr - smp) / 1e9;
  double expect = (own ? del_intra_ms_ : del_inter_ms_) / 1000.0, tol = del_tol_ms_ / 1000.0;
  if (!legacy && (d < -tol || d > expect + tol)) {
    snprintf(why, n, "timestamp implausible: arrived %.2f s after sampling (expected %.1f s): I%u or own clock suspect",
             d, expect, src);
    return Look::MISSING;
  }
  const uint8_t* p = m + 32 + 8 * (det - 1);
  out->det = p[0];
  out->q = (DetQ)p[1];
  out->presence = p[2];
  out->count = p[3];
  out->occ_permille = rd16(p + 4);
  out->gap_ds = rd16(p + 6);
  *sample_ns = smp;
  *seq = sq;
  if (out->q != DetQ::OK) {
    snprintf(why, n, "I%u D%u reported %s", src, det, to_str(out->q));
    return Look::MISSING;
  }
  return Look::OK;
}

}  // namespace tsc::svc
