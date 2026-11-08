// Service layer: messaging, freshness/liveness, time, configuration, records,
// health/alarms, detectors (device manager) and the center agent.
#pragma once
#include <cstdint>

#include "common/base.hpp"
#include "hal/hal.hpp"

struct mosquitto;
struct mosquitto_message;

namespace tsc::svc {

// ------------------------------------------------------------------ alarms
struct Alarm {
  char code[24];
  char sev[8];
  char text[200];
  int64_t t;
};
void alarm(const char* code, const char* sev, const char* fmt, ...) __attribute__((format(printf, 3, 4)));
bool alarm_peek(Alarm* a);
void alarm_pop();
int alarm_pending();

// ------------------------------------------------------------------ time
class TimeService {
 public:
  void init(int id, hal::Oscillator* osc, hal::Gnss* gnss, hal::PeerNet* net, const uint8_t* nb, int nnb);
  int64_t now() const { return osc_ ? sys_from_raw(osc_->raw()) : 0; }
  int64_t sys_from_raw(int64_t raw) const;
  void tick();
  void on_ptp(const hal::PeerFrame& f);
  void set_tolerance_ms(double ms) { tol_ms_ = ms; }
  SyncQ quality() const { return q_; }
  bool synced() const { return q_ != SyncQ::UNSYNC; }
  double err_bound_ms() const { return bound_ms_; }
  double freq_ppm() const { return freq_ * 1e6; }
  uint8_t master() const { return master_; }
  double ptp_offset_ms() const { return ptp_theta_ / 1e6; }
  double ptp_delay_ms() const { return ptp_delay_ / 1e6; }
  double gnss_offset_ms() const { return gnss_theta_ / 1e6; }
  uint8_t clock_class() const;
  uint16_t steps() const;
  bool gnss_rejected() const { return gnss_rejected_; }
  double holdover_left_h() const;
  const char* state_text() const { return state_txt_; }

 private:
  void measurement(int64_t theta, bool from_gnss);
  void rebase();
  void update_quality();
  void bmca();
  void ptp_master_tx();
  void send_announce();
  int build_header(uint8_t* b, uint8_t type, uint16_t len, uint16_t seq, uint8_t ctrl, bool two_step) const;

  int id_ = 0;
  hal::Oscillator* osc_ = nullptr;
  hal::Gnss* gnss_ = nullptr;
  hal::PeerNet* net_ = nullptr;
  uint8_t nb_[4] = {0};
  int nnb_ = 0;
  // correction model: sys = raw + off_ + (raw - ref_) * freq_
  int64_t off_ = 0, ref_ = 0, pending_ = 0;
  double freq_ = 0;
  int64_t last_tick_mono_ = 0;
  bool ever_synced_ = false;
  SyncQ q_ = SyncQ::UNSYNC;
  double bound_ms_ = 1e9, tol_ms_ = 100;
  double bound_at_loss_ = 0;
  int64_t loss_mono_ = 0;
  int64_t gnss_theta_ = 0, ptp_theta_ = 0, ptp_delay_ = 0;
  int64_t gnss_meas_mono_ = 0, ptp_meas_mono_ = 0;
  bool gnss_rejected_ = false;
  int gnss_jump_count_ = 0;
  int meas_log_ = 0;
  char state_txt_[96] = "free-running, not synchronized";
  // PTP
  struct Peer {
    uint8_t id;
    int64_t seen_mono;
    uint8_t gm_class;
    uint16_t steps;
    uint8_t gm_id[8];
    uint8_t time_source;
  } peers_[4] = {};
  uint8_t master_ = 0;
  uint8_t master_gm_[8] = {0};
  uint8_t master_class_ = 255;
  uint16_t master_steps_ = 0;
  uint16_t seq_sync_ = 0, seq_ann_ = 0, seq_dreq_ = 0;
  int64_t last_sync_tx_mono_ = 0;
  // slave exchange state
  uint16_t rx_sync_seq_ = 0;
  int64_t t1_ = 0, t2_ = 0, t3_ = 0;
  bool have_t2_ = false, dreq_out_ = false;
};

// ------------------------------------------------------------------ detectors (CANopen master)
struct Reading {          // wire units, identical to the channel message
  uint8_t det;
  DetQ q;
  uint8_t presence, count;
  uint16_t occ_permille, gap_ds;
};

class DetectorService {
 public:
  void init(hal::CanBus* bus);
  void start();                       // NMT start remote node, both cards
  void on_frame(const hal::CanFrame& f);
  void tick();
  void sample(Reading out[kDet]) const;
  DetQ quality(int det) const;        // det 1..8
  bool present(int det) const { return d_[det - 1].occ; }
  int count_window(int det, int64_t window_ns) const;
  bool card_silent(int i) const { return card_[i].silent; }
  uint8_t card_state(int i) const { return card_[i].nmt; }

 private:
  struct Iv {
    int64_t on, off;
  };
  struct Det {
    bool occ = false;
    int64_t on_since = 0, last_off = 0;
    Iv iv[32] = {};
    int head = 0, n = 0;
    bool loop_fault = false;
  } d_[kDet];
  struct Card {
    uint8_t node;
    uint8_t nmt = 0xFF;
    int64_t last_hb = 0;
    bool silent = true;
    bool seen = false;
  } card_[2];
  hal::CanBus* bus_ = nullptr;
  void edge(int det, bool on, int64_t t);
  void nmt_start(uint8_t node);
};

// ------------------------------------------------------------------ configuration
struct Clause {
  uint8_t src, det, measure, op;
  int32_t value;
};
struct Rule {
  char id[9];
  uint8_t n, action, if_missing, prio;
  uint32_t action_ms, missing_ms;
  Clause c[kMaxClauses];
};
struct Config {
  bool valid = false;
  uint8_t tsc = 0;
  uint32_t version = 0, flags = 0;
  int64_t created = 0;
  char tag[17] = "", git[13] = "", hash[17] = "";
  uint8_t own_ch = 0, buffer_n = 1, n_subs = 0, n_up = 0;
  uint8_t subs[kMaxSubs] = {0}, up[kMaxUp] = {0};
  uint32_t cycle_ms = 40000, offset_ms = 0, main_min_ms = 14000, main_max_ext_ms = 8000;
  uint32_t side_min_ms = 8000, amber_ms = 3000, allred_ms = 2000, sample_at_ms = 13000;
  uint32_t up_wait_ms = 2000, sync_tol_ms = 100, dl_local_ms = 2000, dl_up_ms = 5000;
  uint32_t del_intra_ms = 1000, del_inter_ms = 3000, del_tol_ms = 1000;
  int n_rules = 0;
  Rule rules[kMaxRules];
  bool legacy_prev_cycle() const { return flags & 1; }
  bool side_skip() const { return flags & 2; }
};
bool parse_config(const uint8_t* buf, int len, int expect_tsc, Config* out, char* err, size_t n);

class ConfigStore {
 public:
  void init(const char* dir, int id);
  bool boot(Config* active);
  bool stage(const uint8_t* buf, int len, char* result, size_t n);
  bool command(const uint8_t* buf, int len, uint32_t active_ver, char* result, size_t n);
  bool due(int64_t now) const { return pending_ && now >= pending_at_; }
  bool activate(Config* active, char* result, size_t n);
  char active_slot() const { return active_; }
  uint32_t slot_version(char s) const { return s == 'A' ? ver_a_ : ver_b_; }
  bool pending(uint32_t* ver, int64_t* at) const {
    *ver = pending_ver_;
    *at = pending_at_;
    return pending_;
  }

 private:
  void path(char slot, char* out, size_t n) const;
  void refresh_versions();
  char dir_[256] = "";
  int id_ = 0;
  char active_ = 'A';
  uint32_t ver_a_ = 0, ver_b_ = 0;
  bool pending_ = false;
  char pending_slot_ = 'B';
  uint32_t pending_ver_ = 0;
  int64_t pending_at_ = 0;
};

// ------------------------------------------------------------------ channel messaging
enum class Look { OK, WAIT, MISSING };

struct ChanState {
  uint8_t ch = 0, src = 0;
  bool have = false, have_prev = false;
  uint8_t msg[kChanMsgLen], prev[kChanMsgLen];
  int64_t arrival_sys = 0, sample_ns = 0, prev_arrival = 0, prev_sample = 0;
  uint32_t seq = 0, cycle = 0, prev_seq = 0;
  SyncQ syncq = SyncQ::UNSYNC;
  bool rx_this_cycle = false;
  int silent_cycles = 0, good_cycles = 0;
  enum Live : uint8_t { UNKNOWN, OK, SILENT, RECOVERING } live = UNKNOWN;
  uint32_t lost = 0;
};

class MessageService {
 public:
  void init(int id, hal::PeerNet* net, hal::PlatformBuffer* buf, TimeService* ts, hal::Oscillator* osc,
            const char* flash_dir);
  void configure(const Config& cfg);
  void resubscribe();
  void send_own(const Reading r[kDet], uint32_t cycle, uint32_t cfg_ver);
  void on_frame(const hal::PeerFrame& f);
  void process();
  void end_of_cycle(uint32_t cycle_no, int buffer_n);
  Look lookup(uint8_t src_tsc, uint8_t det, int64_t cycle_start, bool legacy, Reading* out,
              int64_t* sample_ns, uint32_t* seq, char* why, size_t n);
  const ChanState* state_for(uint8_t tsc) const;
  int n_chans() const { return n_; }
  const ChanState& chan(int i) const { return st_[i]; }
  uint32_t mac_fail = 0, replays = 0, lost_total = 0, sent = 0;
  // fault injection (sim): emulate the proposal's buffer N on this controller
  int buffer_n_override = 0;

 private:
  ChanState* find_src(uint8_t tsc);
  int id_ = 0;
  hal::PeerNet* net_ = nullptr;
  hal::PlatformBuffer* buf_ = nullptr;
  TimeService* ts_ = nullptr;
  hal::Oscillator* osc_ = nullptr;
  ChanState st_[kMaxSubs];
  int n_ = 0;
  uint32_t seq_ = 0;
  uint8_t own_ch_ = 0;
  uint8_t subs_[kMaxSubs] = {0};
  char seq_file_[300] = "";
  uint32_t del_intra_ms_ = 1000, del_inter_ms_ = 3000, del_tol_ms_ = 1000;
};

// ------------------------------------------------------------------ decision records
class Recorder {
 public:
  void init(const char* dir, int id);
  uint32_t next_seq() const { return seq_ + 1; }
  const char* prev_hash() const { return prev_; }
  void append(char* body, size_t len, size_t cap);   // body: complete JSON object w/o "hash"
  int batch(char* out, size_t cap, long* end_off, int max_records);
  void commit(long end_off);
  int backlog() const { return (int)(seq_ - uploaded_); }
  uint32_t seq() const { return seq_; }

 private:
  char file_[300] = "", cursor_file_[300] = "";
  char prev_[65] = "0000000000000000000000000000000000000000000000000000000000000000";
  uint32_t seq_ = 0, uploaded_ = 0;
  long cursor_ = 0;
  int inflight_n_ = 0;
};

// ------------------------------------------------------------------ center agent (MQTT over mTLS)
class CenterAgent {
 public:
  using HealthFn = int (*)(char* buf, size_t cap);
  using CfgFn = void (*)(const char* sub, const uint8_t* p, int len);
  void init(int id, const char* pki_dir, HealthFn health, CfgFn cfg, Recorder* rec);
  void tick();
  void publish(const char* sub, const char* payload, int qos, bool retain = false);
  bool connected() const { return connected_; }
  int sock() const;

  // libmosquitto callbacks
  void on_connect(int rc);
  void on_disconnect(int rc);
  void on_message(const mosquitto_message* m);
  void on_publish(int mid);

 private:
  int id_ = 0;
  char name_[16] = "";
  mosquitto* m_ = nullptr;
  bool connected_ = false;
  int64_t last_try_ = 0, last_health_ = 0, last_rec_ = 0;
  HealthFn health_ = nullptr;
  CfgFn cfg_ = nullptr;
  Recorder* rec_ = nullptr;
  int rec_mid_ = -1;
  long rec_end_ = 0;
  int64_t rec_sent_mono_ = 0;
  int alarm_mid_ = -1;
};

}  // namespace tsc::svc
