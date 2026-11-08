// Common types, ports, logging and bounded JSON helpers for the intersection controller.
#pragma once
#include <cstdarg>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstring>

namespace tsc {

constexpr int kDet = 8;           // vehicle detectors per intersection
constexpr int kBufSlots = 10;     // platform receive buffer (fact from the brief)
constexpr int kMaxRules = 16;
constexpr int kMaxClauses = 3;
constexpr int kMaxSubs = 8;
constexpr int kMaxUp = 4;
constexpr int kChanMsgLen = 104;
constexpr const char* kFwVersion = "tsc-fw 1.0.0";

namespace port {
constexpr int kSwitch = 30000;                         // emulated peer-network switch
inline int peer(int id) { return 31000 + id; }         // peer network interface
inline int can_rx(int id) { return 32000 + id; }       // virtual CAN bus, cards -> controller
inline int can_tx(int id) { return 32100 + id; }       // virtual CAN bus, controller -> cards
inline int mmu_cmd(int id) { return 33000 + id; }      // load-switch drive -> safety unit
inline int mmu_status(int id) { return 33100 + id; }   // safety unit -> controller
inline int backplane(int id) { return 34000 + id; }    // testbench fault injection (sim only)
constexpr int kProbe = 35000;                          // bench probe (sim only)
inline int wan(int id) { return 18800 + id; }          // center link via WAN emulator
}  // namespace port

enum class SyncQ : uint8_t { GNSS = 0, PTP = 1, HOLDOVER = 2, UNSYNC = 3 };
enum class DetQ : uint8_t { OK = 0, STUCK_ON = 1, LOOP_FAULT = 2, SILENT = 3, CHATTER = 4 };
const char* to_str(SyncQ q);
const char* to_str(DetQ q);

int64_t mono_ns();
void fmt_utc(int64_t utc_ns, char* out, size_t n);

using TimeFn = int64_t (*)();
void log_init(int id, const char* path, TimeFn fn);
void logf(const char* mod, const char* fmt, ...) __attribute__((format(printf, 2, 3)));

// Tiny parsers for the flat JSON commands from the testbench backplane.
bool json_str(const char* js, const char* key, char* out, size_t n);
bool json_num(const char* js, const char* key, double* v);

// Bounded JSON writer onto a caller-owned buffer (no heap).
class JsonW {
 public:
  JsonW(char* buf, size_t cap) : b_(buf), cap_(cap) { b_[0] = 0; }
  JsonW& obj(const char* key = nullptr) { return open(key, '{'); }
  JsonW& arr(const char* key = nullptr) { return open(key, '['); }
  JsonW& end_obj() { return close('}'); }
  JsonW& end_arr() { return close(']'); }
  JsonW& kv(const char* key, int64_t v);
  JsonW& kv(const char* key, int v) { return kv(key, (int64_t)v); }
  JsonW& kv(const char* key, unsigned v) { return kv(key, (int64_t)v); }
  JsonW& kvf(const char* key, double v, int prec = 3);
  JsonW& kv(const char* key, const char* v);
  JsonW& kvb(const char* key, bool v);
  JsonW& kvraw(const char* key, const char* raw);
  size_t len() const { return n_; }
  bool overflow() const { return ovf_; }
  const char* c_str() const { return b_; }

 private:
  JsonW& open(const char* key, char c);
  JsonW& close(char c);
  void sep();
  void key(const char* k);
  void put(const char* fmt, ...) __attribute__((format(printf, 2, 3)));
  char* b_;
  size_t cap_, n_ = 0;
  bool ovf_ = false;
  bool first_[24] = {true};
  int depth_ = 0;
};

}  // namespace tsc
