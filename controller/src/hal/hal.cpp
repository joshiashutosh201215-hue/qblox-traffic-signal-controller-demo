#include "hal/hal.hpp"

#include <arpa/inet.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <openssl/core_names.h>
#include <openssl/evp.h>
#include <sys/socket.h>
#include <unistd.h>

#include <chrono>
#include <ctime>
#include <thread>

namespace tsc::hal {

// ---------------------------------------------------------------- oscillator
int64_t Oscillator::true_now() {
  timespec ts;
  clock_gettime(CLOCK_REALTIME, &ts);
  return (int64_t)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

void Oscillator::init(int64_t boot_offset_ns, double ppm) {
  base_true_ = true_now();
  base_raw_ = base_true_ + boot_offset_ns;
  ppm_ = ppm;
}

int64_t Oscillator::raw_at_true(int64_t t) const {
  int64_t d = t - base_true_;
  return base_raw_ + d + (int64_t)((double)d * ppm_ * 1e-6);
}

int64_t Oscillator::raw() const { return raw_at_true(true_now()); }

void Oscillator::set_ppm(double ppm) {
  int64_t t = true_now();
  base_raw_ = raw_at_true(t);
  base_true_ = t;
  ppm_ = ppm;
}

void Oscillator::step(int64_t ns) { base_raw_ += ns; }

// ---------------------------------------------------------------- GNSS
static void nmea_checksum(char* s, size_t cap) {
  uint8_t cs = 0;
  for (const char* p = s + 1; *p; ++p) cs ^= (uint8_t)*p;
  size_t n = strlen(s);
  snprintf(s + n, cap - n, "*%02X", cs);
}

void Gnss::init(int id, bool has_sky, const char* nmea_log) {
  (void)id;
  sky_ = has_sky;
  log_ = fopen(nmea_log, "a");
  if (log_) setvbuf(log_, nullptr, _IOLBF, 0);
}

void Gnss::set_sky(bool on) {
  if (on != sky_) logf("GNSS", "%s", on ? "antenna sky view restored, acquiring" : "LOST: no satellites in view");
  sky_ = on;
}

void Gnss::nmea(int64_t utc_ns, bool valid) {
  if (!log_) return;
  char zda[96], rmc[128];
  time_t s = (time_t)(utc_ns / 1000000000LL);
  tm t;
  gmtime_r(&s, &t);
  if (valid) {
    snprintf(zda, sizeof zda, "$GPZDA,%02d%02d%02d.00,%02d,%02d,%04d,00,00", t.tm_hour, t.tm_min,
             t.tm_sec, t.tm_mday, t.tm_mon + 1, t.tm_year + 1900);
    snprintf(rmc, sizeof rmc, "$GPRMC,%02d%02d%02d.00,A,5200.6960,N,00421.4260,E,0.0,0.0,%02d%02d%02d,,,A",
             t.tm_hour, t.tm_min, t.tm_sec, t.tm_mday, t.tm_mon + 1, t.tm_year % 100);
  } else {
    snprintf(zda, sizeof zda, "$GPZDA,,,,,,");
    snprintf(rmc, sizeof rmc, "$GPRMC,,V,,,,,,,,,,N");
  }
  nmea_checksum(zda, sizeof zda);
  nmea_checksum(rmc, sizeof rmc);
  fprintf(log_, "%s\n%s\n", rmc, zda);
}

bool Gnss::poll(const Oscillator& osc, int64_t* raw_edge, int64_t* utc_ns) {
  int64_t t = Oscillator::true_now();
  int64_t sec = t / 1000000000LL;
  if (sec == last_sec_) return false;
  last_sec_ = sec;
  if (!sky_) {
    nmea(0, false);
    return false;
  }
  int64_t edge = sec * 1000000000LL;
  *raw_edge = osc.raw_at_true(edge);
  *utc_ns = edge + spoof_ns_;
  nmea(*utc_ns, true);
  return true;
}

// ---------------------------------------------------------------- UDP
bool Udp::open(int bind_port) {
  fd_ = socket(AF_INET, SOCK_DGRAM | SOCK_NONBLOCK, 0);
  if (fd_ < 0) return false;
  int one = 1;
  setsockopt(fd_, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
  sockaddr_in a{};
  a.sin_family = AF_INET;
  a.sin_port = htons((uint16_t)bind_port);
  a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  return bind(fd_, (sockaddr*)&a, sizeof a) == 0;
}

int Udp::send(int port, const void* buf, int len) {
  sockaddr_in a{};
  a.sin_family = AF_INET;
  a.sin_port = htons((uint16_t)port);
  a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  return (int)sendto(fd_, buf, (size_t)len, 0, (sockaddr*)&a, sizeof a);
}

int Udp::recv(void* buf, int cap) { return (int)::recv(fd_, buf, (size_t)cap, 0); }

// ---------------------------------------------------------------- peer network
bool PeerNet::open(int id) {
  id_ = id;
  return udp_.open(port::peer(id));
}

int64_t PeerNet::send(EnvType t, uint8_t dst, uint8_t aux, const void* p, int len, const Oscillator& osc) {
  if ((t == ENV_PTP_EVENT || t == ENV_PTP_GENERAL) && !ptp_up_) return 0;
  uint8_t buf[200];
  memcpy(buf, "PNET", 4);
  buf[4] = t;
  buf[5] = (uint8_t)id_;
  buf[6] = dst;
  buf[7] = aux;
  buf[8] = (uint8_t)(len & 0xff);
  buf[9] = (uint8_t)(len >> 8);
  memcpy(buf + 10, p, (size_t)len);
  int64_t ts = osc.raw();             // tx hardware timestamp
  udp_.send(port::kSwitch, buf, 10 + len);
  return ts;
}

bool PeerNet::recv(PeerFrame* f, const Oscillator& osc) {
  uint8_t buf[200];
  for (;;) {
    int n = udp_.recv(buf, sizeof buf);
    if (n < 10) return false;
    int64_t ts = osc.raw();           // rx hardware timestamp
    if (memcmp(buf, "PNET", 4) != 0) continue;
    f->type = buf[4];
    f->src = buf[5];
    f->dst = buf[6];
    f->aux = buf[7];
    f->len = buf[8] | (buf[9] << 8);
    if (f->len > (int)sizeof f->data || f->len > n - 10) continue;
    if ((f->type == ENV_PTP_EVENT || f->type == ENV_PTP_GENERAL) && !ptp_up_) continue;
    memcpy(f->data, buf + 10, (size_t)f->len);
    f->rx_raw = ts;
    return true;
  }
}

void PeerNet::subscribe(const uint8_t* ch, int n) {
  uint8_t buf[64];
  memcpy(buf, "PNET", 4);
  buf[4] = ENV_SUBSCRIBE;
  buf[5] = (uint8_t)id_;
  buf[6] = 0;
  buf[7] = 0;
  buf[8] = (uint8_t)n;
  buf[9] = 0;
  memcpy(buf + 10, ch, (size_t)n);
  udp_.send(port::kSwitch, buf, 10 + n);
}

// ---------------------------------------------------------------- platform buffer
void PlatformBuffer::push(const uint8_t* m, int64_t arrival_sys) {
  if (n_ >= kBufSlots) {
    ++drops_;                          // lost without warning
    return;
  }
  memcpy(slots_[n_].data, m, kChanMsgLen);
  slots_[n_].arrival_sys = arrival_sys;
  slots_[n_].processed = false;
  ++n_;
}

// ---------------------------------------------------------------- CAN
bool CanBus::open(int id, const char* candump_path) {
  id_ = id;
  log_ = fopen(candump_path, "a");
  if (log_) setvbuf(log_, nullptr, _IOLBF, 0);
  return rx_.open(port::can_rx(id)) && tx_.open(0);
}

void CanBus::dump(const CanFrame& f) {
  if (!log_) return;
  int64_t t = Oscillator::true_now();
  char hex[20];
  for (int i = 0; i < f.dlc; ++i) snprintf(hex + 2 * i, 3, "%02X", f.data[i]);
  hex[2 * f.dlc] = 0;
  fprintf(log_, "(%lld.%06lld) can0 %03X#%s\n", (long long)(t / 1000000000LL),
          (long long)((t % 1000000000LL) / 1000), f.id, hex);
}

bool CanBus::recv(CanFrame* f) {
  uint8_t b[16];
  if (rx_.recv(b, sizeof b) != 16) return false;
  memcpy(&f->id, b, 4);                // struct can_frame, host byte order
  f->id &= 0x1FFFFFFF;
  f->dlc = b[4] > 8 ? 8 : b[4];
  memcpy(f->data, b + 8, 8);
  dump(*f);
  return true;
}

void CanBus::send(const CanFrame& f) {
  uint8_t b[16] = {0};
  memcpy(b, &f.id, 4);
  b[4] = f.dlc;
  memcpy(b + 8, f.data, 8);
  tx_.send(port::can_tx(id_), b, 16);
  dump(f);
}

// ---------------------------------------------------------------- lamps / MMU
bool LampIo::open(int id) {
  id_ = id;
  return cmd_.open(0) && status_.open(port::mmu_status(id));
}

void LampIo::drive(uint16_t lamps, bool wd) {
  uint8_t b[12];
  memcpy(b, "LAMP", 4);
  ++seq_;
  memcpy(b + 4, &seq_, 4);
  memcpy(b + 8, &lamps, 2);
  b[10] = wd ? 1 : 0;
  b[11] = 0;
  cmd_.send(port::mmu_cmd(id_), b, sizeof b);
}

bool LampIo::recv_status(MmuStatus* s) {
  uint8_t b[80];
  int n = status_.recv(b, sizeof b);
  if (n < 72 || memcmp(b, "MMUS", 4) != 0) return false;
  s->state = b[4];
  memcpy(&s->field_lamps, b + 6, 2);
  memcpy(s->fault, b + 8, 63);
  s->fault[63] = 0;
  s->rx_mono = mono_ns();
  return true;
}

// ---------------------------------------------------------------- flash
namespace flash {
bool read_file(const char* path, uint8_t* buf, int cap, int* len) {
  FILE* f = fopen(path, "rb");
  if (!f) return false;
  *len = (int)fread(buf, 1, (size_t)cap, f);
  fclose(f);
  return *len > 0;
}

bool write_file_atomic(const char* path, const uint8_t* buf, int len) {
  char tmp[512];
  snprintf(tmp, sizeof tmp, "%s.tmp", path);
  FILE* f = fopen(tmp, "wb");
  if (!f) return false;
  bool ok = fwrite(buf, 1, (size_t)len, f) == (size_t)len;
  fflush(f);
  fsync(fileno(f));
  fclose(f);
  return ok && rename(tmp, path) == 0;
}

bool append_line(const char* path, const char* line) {
  FILE* f = fopen(path, "a");
  if (!f) return false;
  fputs(line, f);
  fputc('\n', f);
  fclose(f);
  return true;
}
}  // namespace flash

// ---------------------------------------------------------------- crypto
namespace crypto {
static uint8_t g_peer_key[16];
static EVP_PKEY* g_sign_pub = nullptr;

bool init(const char* peer_key_path, const char* sign_pub_path) {
  int n = 0;
  uint8_t pub[64];
  if (!flash::read_file(peer_key_path, g_peer_key, 16, &n) || n != 16) return false;
  if (!flash::read_file(sign_pub_path, pub, 64, &n) || n != 32) return false;
  g_sign_pub = EVP_PKEY_new_raw_public_key(EVP_PKEY_ED25519, nullptr, pub, 32);
  return g_sign_pub != nullptr;
}

bool cmac_full(const uint8_t key[16], const uint8_t* msg, int len, uint8_t out[16]) {
  EVP_MAC* mac = EVP_MAC_fetch(nullptr, "CMAC", nullptr);
  if (!mac) return false;
  EVP_MAC_CTX* ctx = EVP_MAC_CTX_new(mac);
  char cipher[] = "AES-128-CBC";
  OSSL_PARAM params[] = {OSSL_PARAM_construct_utf8_string(OSSL_MAC_PARAM_CIPHER, cipher, 0),
                         OSSL_PARAM_construct_end()};
  size_t olen = 0;
  bool ok = EVP_MAC_init(ctx, key, 16, params) && EVP_MAC_update(ctx, msg, (size_t)len) &&
            EVP_MAC_final(ctx, out, &olen, 16) && olen == 16;
  EVP_MAC_CTX_free(ctx);
  EVP_MAC_free(mac);
  return ok;
}

void cmac64(const uint8_t* msg, int len, uint8_t out[8]) {
  uint8_t full[16] = {0};
  cmac_full(g_peer_key, msg, len, full);
  memcpy(out, full, 8);                // truncated MAC, as in bandwidth-limited buses
}

bool ed25519_verify(const uint8_t* msg, int len, const uint8_t sig[64]) {
  if (!g_sign_pub) return false;
  EVP_MD_CTX* ctx = EVP_MD_CTX_new();
  bool ok = EVP_DigestVerifyInit(ctx, nullptr, nullptr, nullptr, g_sign_pub) == 1 &&
            EVP_DigestVerify(ctx, sig, 64, msg, (size_t)len) == 1;
  EVP_MD_CTX_free(ctx);
  return ok;
}

void sha256_hex(const char* data, size_t len, char out[65]) {
  unsigned char md[32];
  unsigned int n = 0;
  EVP_Digest(data, len, md, &n, EVP_sha256(), nullptr);
  for (int i = 0; i < 32; ++i) snprintf(out + 2 * i, 3, "%02x", md[i]);
}
}  // namespace crypto

// ---------------------------------------------------------------- watchdog
void Watchdog::start(int timeout_ms) {
  kick();
  std::thread([this, timeout_ms] {
    for (;;) {
      std::this_thread::sleep_for(std::chrono::milliseconds(100));
      if (mono_ns() - last_.load() > (int64_t)timeout_ms * 1000000LL) {
        logf("WDOG", "main loop did not kick for %d ms -> HARDWARE RESET", timeout_ms);
        _exit(70);
      }
    }
  }).detach();
}

void Watchdog::kick() { last_.store(mono_ns()); }

}  // namespace tsc::hal
