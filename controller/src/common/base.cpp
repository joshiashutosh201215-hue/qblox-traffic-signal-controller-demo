#include "common/base.hpp"

#include <ctime>
#include <cstdlib>

namespace tsc {

const char* to_str(SyncQ q) {
  switch (q) {
    case SyncQ::GNSS: return "GNSS";
    case SyncQ::PTP: return "PTP";
    case SyncQ::HOLDOVER: return "HOLDOVER";
    default: return "UNSYNC";
  }
}

const char* to_str(DetQ q) {
  switch (q) {
    case DetQ::OK: return "OK";
    case DetQ::STUCK_ON: return "STUCK_ON";
    case DetQ::LOOP_FAULT: return "LOOP_FAULT";
    case DetQ::SILENT: return "SILENT";
    default: return "CHATTER";
  }
}

int64_t mono_ns() {
  timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (int64_t)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

void fmt_utc(int64_t ns, char* out, size_t n) {
  time_t s = (time_t)(ns / 1000000000LL);
  int ms = (int)((ns % 1000000000LL) / 1000000LL);
  if (ms < 0) { ms += 1000; s -= 1; }
  tm t;
  gmtime_r(&s, &t);
  snprintf(out, n, "%04d-%02d-%02dT%02d:%02d:%02d.%03dZ", t.tm_year + 1900, t.tm_mon + 1,
           t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec, ms);
}

static FILE* g_log = nullptr;
static int g_id = 0;
static TimeFn g_time = nullptr;

void log_init(int id, const char* path, TimeFn fn) {
  g_id = id;
  g_time = fn;
  g_log = fopen(path, "a");
  if (g_log) setvbuf(g_log, nullptr, _IOLBF, 0);
}

void logf(const char* mod, const char* fmt, ...) {
  char ts[40], msg[768];
  fmt_utc(g_time ? g_time() : 0, ts, sizeof ts);
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(msg, sizeof msg, fmt, ap);
  va_end(ap);
  FILE* f = g_log ? g_log : stderr;
  fprintf(f, "%s I%02d [%-5s] %s\n", ts, g_id, mod, msg);
}

static const char* find_key(const char* js, const char* key) {
  char pat[64];
  snprintf(pat, sizeof pat, "\"%s\"", key);
  const char* p = strstr(js, pat);
  if (!p) return nullptr;
  p += strlen(pat);
  while (*p == ' ' || *p == ':') ++p;
  return p;
}

bool json_str(const char* js, const char* key, char* out, size_t n) {
  const char* p = find_key(js, key);
  if (!p || *p != '"') return false;
  ++p;
  size_t i = 0;
  while (*p && *p != '"' && i + 1 < n) out[i++] = *p++;
  out[i] = 0;
  return true;
}

bool json_num(const char* js, const char* key, double* v) {
  const char* p = find_key(js, key);
  if (!p) return false;
  if (!strncmp(p, "true", 4)) { *v = 1; return true; }
  if (!strncmp(p, "false", 5)) { *v = 0; return true; }
  char* end;
  *v = strtod(p, &end);
  return end != p;
}

void JsonW::put(const char* fmt, ...) {
  if (ovf_) return;
  va_list ap;
  va_start(ap, fmt);
  int w = vsnprintf(b_ + n_, cap_ - n_, fmt, ap);
  va_end(ap);
  if (w < 0 || (size_t)w >= cap_ - n_) { ovf_ = true; b_[n_] = 0; return; }
  n_ += (size_t)w;
}

void JsonW::sep() {
  if (!first_[depth_]) put(",");
  first_[depth_] = false;
}

void JsonW::key(const char* k) {
  sep();
  if (k) put("\"%s\":", k);
}

JsonW& JsonW::open(const char* k, char c) {
  if (depth_ > 0) key(k);
  put("%c", c);
  if (depth_ < 23) first_[++depth_] = true;
  return *this;
}

JsonW& JsonW::close(char c) {
  put("%c", c);
  if (depth_ > 0) --depth_;
  return *this;
}

JsonW& JsonW::kv(const char* k, int64_t v) { key(k); put("%lld", (long long)v); return *this; }
JsonW& JsonW::kvf(const char* k, double v, int prec) { key(k); put("%.*f", prec, v); return *this; }
JsonW& JsonW::kvb(const char* k, bool v) { key(k); put("%s", v ? "true" : "false"); return *this; }
JsonW& JsonW::kvraw(const char* k, const char* raw) { key(k); put("%s", raw); return *this; }

JsonW& JsonW::kv(const char* k, const char* v) {
  key(k);
  put("\"");
  for (const char* p = v; *p && !ovf_; ++p) {
    if (*p == '"' || *p == '\\') put("\\%c", *p);
    else if ((unsigned char)*p < 0x20) put(" ");
    else put("%c", *p);
  }
  put("\"");
  return *this;
}

}  // namespace tsc
