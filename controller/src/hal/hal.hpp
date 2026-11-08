// Hardware abstraction layer. Upper layers use only these interfaces, so new
// hardware (a different detector bus, a real PHC, a secure element) changes only
// this layer. In the demo each device is emulated by the testbench over UDP.
#pragma once
#include <atomic>
#include <cstdint>

#include "common/base.hpp"

namespace tsc::hal {

// ---- Local oscillator (TCXO). "True" time is the host clock; the controller
// never reads it directly, only through its own drifting crystal.
class Oscillator {
 public:
  void init(int64_t boot_offset_ns, double ppm);
  int64_t raw() const;                     // free-running local counter (ns)
  int64_t raw_at_true(int64_t true_ns) const;
  void set_ppm(double ppm);                // fault: crystal aging / temperature
  void step(int64_t ns);                   // fault: counter jump
  double ppm() const { return ppm_; }
  static int64_t true_now();               // simulation truth, used by HAL models only

 private:
  int64_t base_true_ = 0, base_raw_ = 0;
  double ppm_ = 0;
};

// ---- GNSS receiver with 1PPS. Produces one PPS edge per UTC second while
// locked; the edge is hardware-captured on the local counter.
class Gnss {
 public:
  void init(int id, bool has_sky, const char* nmea_log);
  // Returns true when a PPS edge occurred: *raw_edge = local counter at the edge,
  // *utc_ns = time the receiver claims for that edge (spoofable).
  bool poll(const Oscillator& osc, int64_t* raw_edge, int64_t* utc_ns);
  void set_sky(bool on);
  void set_spoof_ms(int64_t ms) { spoof_ns_ = ms * 1000000LL; }
  bool locked() const { return sky_; }

 private:
  void nmea(int64_t utc_ns, bool valid);
  bool sky_ = true;
  int64_t last_sec_ = 0, spoof_ns_ = 0;
  FILE* log_ = nullptr;
};

// ---- UDP socket (stands in for Ethernet / CAN / discrete I/O drivers)
class Udp {
 public:
  bool open(int bind_port);
  int send(int port, const void* buf, int len);
  int recv(void* buf, int cap);            // non-blocking, -1 if nothing
  int fd() const { return fd_; }

 private:
  int fd_ = -1;
};

// ---- Peer network interface. Carries channel messages and PTP over the reliable
// inter-intersection network; hardware timestamps are taken on the local counter.
enum EnvType : uint8_t { ENV_CHAN = 1, ENV_PTP_EVENT = 2, ENV_PTP_GENERAL = 3, ENV_SUBSCRIBE = 4 };

struct PeerFrame {
  uint8_t type, src, dst, aux;
  int len;
  uint8_t data[160];
  int64_t rx_raw;                          // hardware rx timestamp (local counter)
};

class PeerNet {
 public:
  bool open(int id);
  int64_t send(EnvType t, uint8_t dst, uint8_t aux, const void* p, int len, const Oscillator& osc);
  bool recv(PeerFrame* f, const Oscillator& osc);
  void subscribe(const uint8_t* ch, int n);
  void set_ptp_port(bool up) { ptp_up_ = up; }
  bool ptp_up() const { return ptp_up_; }
  int fd() const { return udp_.fd(); }

 private:
  Udp udp_;
  int id_ = 0;
  bool ptp_up_ = true;
};

// ---- Platform receive buffer: 10 messages, emptied at the end of every N-th
// cycle; when full, new messages are lost WITHOUT warning (brief, platform fact).
struct BufferedMsg {
  uint8_t data[kChanMsgLen];
  int64_t arrival_sys;
  bool processed;
};

class PlatformBuffer {
 public:
  void push(const uint8_t* m, int64_t arrival_sys);   // silent drop when full
  void clear() { n_ = 0; }
  int size() const { return n_; }
  BufferedMsg& at(int i) { return slots_[i]; }
  uint32_t platform_drops() const { return drops_; }  // not visible to the app in a real platform

 private:
  BufferedMsg slots_[kBufSlots];
  int n_ = 0;
  uint32_t drops_ = 0;
};

// ---- Virtual CAN bus to the detector cards (struct can_frame over UDP) + candump log
struct CanFrame {
  uint32_t id;
  uint8_t dlc;
  uint8_t data[8];
};

class CanBus {
 public:
  bool open(int id, const char* candump_path);
  bool recv(CanFrame* f);
  void send(const CanFrame& f);
  int fd() const { return rx_.fd(); }

 private:
  void dump(const CanFrame& f);
  Udp rx_, tx_;
  int id_ = 0;
  FILE* log_ = nullptr;
};

// ---- Load-switch drive (12 lamp outputs) to the cabinet + safety-unit status in
struct MmuStatus {
  uint8_t state = 0;          // 0 normal, 1 flashing (fault latched)
  char fault[64] = "";
  uint16_t field_lamps = 0;
  int64_t rx_mono = 0;
};

class LampIo {
 public:
  bool open(int id);
  void drive(uint16_t lamps, bool wd_toggle);
  bool recv_status(MmuStatus* s);
  int fd() const { return status_.fd(); }

 private:
  Udp cmd_, status_;
  int id_ = 0;
  uint32_t seq_ = 0;
};

// ---- Non-volatile storage (flash), file-backed
namespace flash {
bool read_file(const char* path, uint8_t* buf, int cap, int* len);
bool write_file_atomic(const char* path, const uint8_t* buf, int len);
bool append_line(const char* path, const char* line);
}  // namespace flash

// ---- Crypto engine (secure element on the real unit; OpenSSL here)
namespace crypto {
bool init(const char* peer_key_path, const char* sign_pub_path);
void cmac64(const uint8_t* msg, int len, uint8_t out[8]);          // AES-128-CMAC truncated to 64 bit
bool cmac_full(const uint8_t key[16], const uint8_t* msg, int len, uint8_t out[16]);
bool ed25519_verify(const uint8_t* msg, int len, const uint8_t sig[64]);
void sha256_hex(const char* data, size_t len, char out[65]);
}  // namespace crypto

// ---- Independent watchdog: resets the unit if the main loop stops kicking it
class Watchdog {
 public:
  void start(int timeout_ms);
  void kick();

 private:
  std::atomic<int64_t> last_{0};
};

}  // namespace tsc::hal
