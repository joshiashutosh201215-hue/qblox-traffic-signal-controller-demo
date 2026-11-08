// Device manager for the detector cards: a minimal CANopen (CiA 301) master.
// Card A (node 0x11) carries D1-D4, card B (node 0x12) carries D5-D8.
//   NMT 0x000        start remote node after boot-up / pre-operational heartbeat
//   Heartbeat 0x700+n consumer, 1.5 s timeout -> card SILENT
//   TPDO1 0x180+n     byte0 = presence bitmap, sent on change + every 100 ms
//   EMCY 0x080+n      loop open/short per channel (error code 0x5000), 0x0000 = reset
// From the edges it derives occupancy, count and gap over a 10 s window, and
// runs plausibility checks (stuck-on, chatter).
#include "services/services.hpp"

namespace tsc::svc {

namespace {
constexpr int64_t kWindow = 10000000000LL;     // 10 s measurement window
constexpr int64_t kHbTimeout = 1500000000LL;
constexpr int64_t kStuckOn = 45000000000LL;    // longer than the longest red
constexpr uint8_t kNodes[2] = {0x11, 0x12};
}  // namespace

// ---------------------------------------------------------------- alarms
static Alarm g_alarms[32];
static int g_ah = 0, g_an = 0;
extern TimeService* g_time;   // set in main, used to timestamp alarms

void alarm(const char* code, const char* sev, const char* fmt, ...) {
  Alarm a{};
  snprintf(a.code, sizeof a.code, "%s", code);
  snprintf(a.sev, sizeof a.sev, "%s", sev);
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(a.text, sizeof a.text, fmt, ap);
  va_end(ap);
  a.t = g_time ? g_time->now() : 0;
  logf("ALARM", "%s %s: %s", a.sev, a.code, a.text);
  if (g_an == 32) {                              // drop oldest
    g_ah = (g_ah + 1) % 32;
    --g_an;
  }
  g_alarms[(g_ah + g_an) % 32] = a;
  ++g_an;
}

bool alarm_peek(Alarm* a) {
  if (!g_an) return false;
  *a = g_alarms[g_ah];
  return true;
}

void alarm_pop() {
  if (!g_an) return;
  g_ah = (g_ah + 1) % 32;
  --g_an;
}

int alarm_pending() { return g_an; }

// ---------------------------------------------------------------- detectors
void DetectorService::init(hal::CanBus* bus) {
  bus_ = bus;
  card_[0].node = kNodes[0];
  card_[1].node = kNodes[1];
}

void DetectorService::nmt_start(uint8_t node) {
  hal::CanFrame f{0x000, 2, {0x01, node}};
  bus_->send(f);
  logf("CAN", "NMT start remote node 0x%02X", node);
}

void DetectorService::start() {
  int64_t now = mono_ns();
  for (auto& c : card_) {
    c.last_hb = now;
    nmt_start(c.node);
  }
}

void DetectorService::edge(int det, bool on, int64_t t) {
  Det& d = d_[det];
  if (on == d.occ) return;
  d.occ = on;
  if (on) {
    d.on_since = t;
    d.iv[d.head] = {t, 0};
    d.head = (d.head + 1) % 32;
    if (d.n < 32) ++d.n;
  } else {
    d.last_off = t;
    int last = (d.head + 31) % 32;
    if (d.n && d.iv[last].off == 0) d.iv[last].off = t;
  }
}

void DetectorService::on_frame(const hal::CanFrame& f) {
  int64_t now = mono_ns();
  uint32_t fn = f.id & 0x780, node = f.id & 0x7F;
  int ci = node == kNodes[0] ? 0 : node == kNodes[1] ? 1 : -1;
  if (ci < 0) return;
  Card& c = card_[ci];
  if (fn == 0x700 && f.dlc >= 1) {               // heartbeat / boot-up
    uint8_t st = f.data[0] & 0x7F;
    if (c.silent && c.seen) {
      alarm("DET_CARD_OK", "info", "detector card 0x%02X heartbeat restored (D%d-D%d)", c.node, ci * 4 + 1, ci * 4 + 4);
    }
    if (!c.seen || st != c.nmt) {
      logf("CAN", "node 0x%02X heartbeat state %s", c.node,
           st == 0x00 ? "BOOT-UP" : st == 0x05 ? "OPERATIONAL" : st == 0x7F ? "PRE-OPERATIONAL" : st == 0x04 ? "STOPPED" : "?");
    }
    c.seen = true;
    c.silent = false;
    c.last_hb = now;
    c.nmt = st;
    if (st == 0x00 || st == 0x7F) nmt_start(c.node);
  } else if (fn == 0x180 && f.dlc >= 1) {        // TPDO1
    for (int k = 0; k < 4; ++k) edge(ci * 4 + k, (f.data[0] >> k) & 1, now);
  } else if (fn == 0x080 && f.dlc >= 8) {        // EMCY
    uint16_t code = (uint16_t)(f.data[0] | (f.data[1] << 8));
    int ch = f.data[3];
    if (code == 0x0000) {
      for (int k = 0; k < 4; ++k) {
        if ((ch == 0 || ch == k + 1) && d_[ci * 4 + k].loop_fault) {
          d_[ci * 4 + k].loop_fault = false;
          alarm("DET_OK", "info", "D%d loop fault cleared (EMCY reset)", ci * 4 + k + 1);
        }
      }
    } else if (ch >= 1 && ch <= 4) {
      Det& d = d_[ci * 4 + ch - 1];
      if (!d.loop_fault)
        alarm("DET_FAULT", "major", "D%d loop %s (EMCY 0x%04X from node 0x%02X): readings invalid, "
              "rules using D%d take their fallback", ci * 4 + ch, f.data[4] == 2 ? "short circuit" : "open circuit",
              code, c.node, ci * 4 + ch);
      d.loop_fault = true;
      edge(ci * 4 + ch - 1, false, now);
    }
  }
}

void DetectorService::tick() {
  int64_t now = mono_ns();
  for (int i = 0; i < 2; ++i) {
    Card& c = card_[i];
    if (!c.silent && now - c.last_hb > kHbTimeout) {
      c.silent = true;
      alarm("DET_CARD_SILENT", "major", "detector card 0x%02X heartbeat lost: D%d-D%d SILENT", c.node, i * 4 + 1, i * 4 + 4);
      for (int k = 0; k < 4; ++k) edge(i * 4 + k, false, now);
    }
  }
  static DetQ last[kDet];
  for (int k = 0; k < kDet; ++k) {
    DetQ q = quality(k + 1);
    if (q != last[k] && (q == DetQ::STUCK_ON || q == DetQ::CHATTER))
      alarm("DET_FAULT", "major", "D%d %s (plausibility check)", k + 1, to_str(q));
    if (q == DetQ::OK && (last[k] == DetQ::STUCK_ON || last[k] == DetQ::CHATTER))
      alarm("DET_OK", "info", "D%d plausible again", k + 1);
    last[k] = q;
  }
}

DetQ DetectorService::quality(int det) const {
  const Det& d = d_[det - 1];
  int64_t now = mono_ns();
  if (card_[(det - 1) / 4].silent) return DetQ::SILENT;
  if (d.loop_fault) return DetQ::LOOP_FAULT;
  if (d.occ && now - d.on_since > kStuckOn) return DetQ::STUCK_ON;
  int fast = 0;
  for (int i = 0; i < d.n; ++i)
    if (d.iv[i].on > now - 2000000000LL) ++fast;
  if (fast >= 10) return DetQ::CHATTER;
  return DetQ::OK;
}

int DetectorService::count_window(int det, int64_t window_ns) const {
  const Det& d = d_[det - 1];
  int64_t from = mono_ns() - window_ns;
  int c = 0;
  for (int i = 0; i < d.n; ++i)
    if (d.iv[i].on >= from) ++c;
  return c;
}

void DetectorService::sample(Reading out[kDet]) const {
  int64_t now = mono_ns(), from = now - kWindow;
  for (int k = 0; k < kDet; ++k) {
    const Det& d = d_[k];
    int64_t occ = 0;
    int cnt = 0;
    for (int i = 0; i < d.n; ++i) {
      int64_t on = d.iv[i].on, off = d.iv[i].off ? d.iv[i].off : now;
      if (on >= from) ++cnt;
      int64_t a = on > from ? on : from;
      if (off > a) occ += off - a;
    }
    double gap = d.occ ? 0.0 : (d.last_off ? (double)(now - d.last_off) / 1e9 : 99.9);
    if (gap > 99.9) gap = 99.9;
    out[k].det = (uint8_t)(k + 1);
    out[k].q = quality(k + 1);
    out[k].presence = d.occ ? 1 : 0;
    out[k].count = (uint8_t)(cnt > 255 ? 255 : cnt);
    out[k].occ_permille = (uint16_t)(occ * 1000 / kWindow);
    out[k].gap_ds = (uint16_t)(gap * 10 + 0.5);
  }
}

}  // namespace tsc::svc
