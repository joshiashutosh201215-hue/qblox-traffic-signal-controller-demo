// Application layer: rule interpreter, decision arbiter, mode manager and the
// signal phase sequencer. Rules arrive as signed data; a rule change never needs
// new firmware.
#pragma once
#include "services/services.hpp"

namespace tsc::app {

enum RStatus : uint8_t { FIRED, NOT_FIRED, MISSING };
enum Act : uint8_t { ACT_NONE = 0, ACT_EXTEND = 1, ACT_HOLD = 2 };

struct Input {
  uint8_t src, det;
  svc::Look look;
  svc::Reading r;
  int64_t sample_ns;
  uint32_t seq;
  char why[160];
};

struct RuleResult {
  RStatus st;
  uint8_t act;
  uint32_t ms;
  int missing_input;      // index into inputs, -1 if none
};

struct Decision {
  uint8_t act;
  uint32_t ext_ms;
  int winner;             // rule index, -1 if none
};

int32_t measure_milli(const svc::Reading& r, uint8_t measure);
int find_input(const Input* in, int n, uint8_t src, uint8_t det);
// Bounded evaluation: <= 16 rules x 3 conditions, no loops over data, no allocation.
void evaluate(const svc::Config& c, const Input* in, int n_in, RuleResult* res, Decision* d);
const char* act_text(uint8_t act);

enum Phase : uint8_t { STARTUP, MAIN_G, MAIN_A, MAIN_R, SIDE_G, SIDE_A, SIDE_R, FLASHED };
enum Mode : uint8_t { M_STARTUP, M_ACTUATED, M_DEGRADED, M_FIXED, M_FLASH };

class PhaseController {
 public:
  using ActivatedFn = void (*)(uint32_t ver, int64_t at, int64_t now, uint32_t cycle);
  void init(int id, svc::Config* cfg, svc::TimeService* ts, svc::DetectorService* det, svc::MessageService* msg,
            svc::Recorder* rec, svc::ConfigStore* store, ActivatedFn on_act);
  void tick(const hal::MmuStatus& mmu);
  uint16_t lamps() const;
  const char* phase_name() const;
  const char* mode_name() const;
  uint32_t cycle() const { return (uint32_t)cur_cyc_; }
  double cycle_t() const { return ct_s_; }
  const char* last_decision() const { return last_dec_; }
  const char* last_record_id() const { return last_rec_id_; }
  Mode mode() const { return mode_; }
  bool side_skipped() const { return side_skipped_; }
  bool bug_conflict = false;   // sim fault injection: software defect commands conflicting greens

 private:
  void set_phase(Phase p);
  void cycle_begin(int64_t cyc, int64_t now);
  void enter_cycle(int64_t ct);
  void try_decide(int64_t ct_eff, int64_t now);
  bool side_call() const;
  bool fixed_time_needed(char* why, size_t n) const;
  void write_record(int64_t now, const Input* in, int n_in, const RuleResult* res, const Decision& d,
                    int64_t waited_ns, bool fixed, const char* fixed_why);

  int id_ = 0;
  svc::Config* cfg_ = nullptr;
  svc::TimeService* ts_ = nullptr;
  svc::DetectorService* det_ = nullptr;
  svc::MessageService* msg_ = nullptr;
  svc::Recorder* rec_ = nullptr;
  svc::ConfigStore* store_ = nullptr;
  ActivatedFn on_act_ = nullptr;
  Phase ph_ = STARTUP;
  Mode mode_ = M_STARTUP;
  int64_t ph_start_m_ = 0;
  int64_t cur_cyc_ = -1, cycle_of_main_ = 0, side_cyc_ = 0;
  bool sampled_ = false, decided_ = false, side_skipped_ = false;
  int64_t main_end_ = 0;       // cycle-relative ns
  double ct_s_ = 0;
  char last_dec_[200] = "no decision yet";
  char last_rec_id_[24] = "";
};

}  // namespace tsc::app
