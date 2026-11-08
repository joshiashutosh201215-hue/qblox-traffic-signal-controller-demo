// Configuration store: two flash slots (A/B), Ed25519-signed binary rule tables,
// anti-rollback version counter, activation at an agreed time on the next cycle
// boundary. The controller starts from local flash and never needs the center.
#include <cstdlib>
#include <initializer_list>

#include "services/services.hpp"

namespace tsc::svc {

namespace {
uint32_t rd32(const uint8_t* b) { uint32_t v; memcpy(&v, b, 4); return v; }
int32_t rdi32(const uint8_t* b) { int32_t v; memcpy(&v, b, 4); return v; }
int64_t rd64(const uint8_t* b) { int64_t v; memcpy(&v, b, 8); return v; }
}  // namespace

bool parse_config(const uint8_t* b, int len, int expect_tsc, Config* c, char* err, size_t n) {
  if (len < 128 + 64 || memcmp(b, "TSCF", 4) != 0) { snprintf(err, n, "not a TSCF configuration"); return false; }
  if (b[4] != 1 || b[5] != 0) { snprintf(err, n, "unsupported format %u", b[4]); return false; }
  int nr = b[7];
  if (nr > kMaxRules || len != 128 + 64 * nr + 64) { snprintf(err, n, "length %d does not match %d rules", len, nr); return false; }
  if (!hal::crypto::ed25519_verify(b, len - 64, b + len - 64)) { snprintf(err, n, "Ed25519 signature INVALID"); return false; }
  if (b[6] != expect_tsc) { snprintf(err, n, "configuration is for I%u, not I%d", b[6], expect_tsc); return false; }
  Config x;
  x.tsc = b[6];
  x.n_rules = nr;
  x.version = rd32(b + 8);
  x.flags = rd32(b + 12);
  x.created = rd64(b + 16);
  memcpy(x.tag, b + 24, 16);
  x.tag[16] = 0;
  x.own_ch = b[40];
  x.buffer_n = b[41] ? b[41] : 1;
  x.n_subs = b[42] > kMaxSubs ? kMaxSubs : b[42];
  x.n_up = b[43] > kMaxUp ? kMaxUp : b[43];
  memcpy(x.subs, b + 44, kMaxSubs);
  memcpy(x.up, b + 52, kMaxUp);
  uint32_t* t[] = {&x.cycle_ms, &x.offset_ms, &x.main_min_ms, &x.main_max_ext_ms, &x.side_min_ms,
                   &x.amber_ms, &x.allred_ms, &x.sample_at_ms, &x.up_wait_ms, &x.sync_tol_ms,
                   &x.dl_local_ms, &x.dl_up_ms, &x.del_intra_ms, &x.del_inter_ms, &x.del_tol_ms};
  for (int i = 0; i < 15; ++i) *t[i] = rd32(b + 56 + 4 * i);
  memcpy(x.git, b + 116, 12);
  x.git[12] = 0;
  for (int r = 0; r < nr; ++r) {
    const uint8_t* p = b + 128 + 64 * r;
    Rule& R = x.rules[r];
    memcpy(R.id, p, 8);
    R.id[8] = 0;
    R.n = p[9] > kMaxClauses ? kMaxClauses : p[9];
    R.action = p[10];
    R.if_missing = p[11];
    R.action_ms = rd32(p + 12);
    R.missing_ms = rd32(p + 16);
    R.prio = p[20];
    for (int k = 0; k < kMaxClauses; ++k) {
      const uint8_t* q = p + 24 + 12 * k;
      R.c[k] = Clause{q[0], q[1], q[2], q[3], rdi32(q + 4)};
    }
  }
  char h[65];
  hal::crypto::sha256_hex((const char*)b, (size_t)len, h);
  memcpy(x.hash, h, 16);
  x.hash[16] = 0;
  x.valid = true;
  *c = x;
  return true;
}

void ConfigStore::init(const char* dir, int id) {
  snprintf(dir_, sizeof dir_, "%s", dir);
  id_ = id;
  char p[300];
  snprintf(p, sizeof p, "%s/active_slot", dir_);
  uint8_t b[4];
  int n = 0;
  if (hal::flash::read_file(p, b, sizeof b, &n) && (b[0] == 'A' || b[0] == 'B')) active_ = (char)b[0];
  refresh_versions();
}

void ConfigStore::path(char slot, char* out, size_t n) const { snprintf(out, n, "%s/slot_%c.cfg", dir_, slot); }

void ConfigStore::refresh_versions() {
  static uint8_t buf[4096];
  for (char s : {'A', 'B'}) {
    char p[300], err[96];
    int n = 0;
    Config c;
    path(s, p, sizeof p);
    uint32_t v = 0;
    if (hal::flash::read_file(p, buf, sizeof buf, &n) && parse_config(buf, n, id_, &c, err, sizeof err)) v = c.version;
    (s == 'A' ? ver_a_ : ver_b_) = v;
  }
}

bool ConfigStore::boot(Config* active) {
  static uint8_t buf[4096];
  for (int attempt = 0; attempt < 2; ++attempt) {
    char p[300], err[96];
    int n = 0;
    path(active_, p, sizeof p);
    if (hal::flash::read_file(p, buf, sizeof buf, &n) && parse_config(buf, n, id_, active, err, sizeof err)) {
      logf("CFG", "boot from flash slot %c: v%u (%s), %d rules, signature OK, hash %s", active_, active->version,
           active->tag, active->n_rules, active->hash);
      return true;
    }
    logf("CFG", "slot %c unusable (%s), trying the other slot", active_, n ? err : "empty");
    active_ = active_ == 'A' ? 'B' : 'A';
  }
  return false;
}

bool ConfigStore::stage(const uint8_t* b, int len, char* res, size_t n) {
  Config c;
  char err[96];
  if (!parse_config(b, len, id_, &c, err, sizeof err)) {
    snprintf(res, n, "REJECTED: %s", err);
    alarm("CFG_REJECTED", "major", "staged configuration rejected: %s", err);
    return false;
  }
  uint32_t act = slot_version(active_);
  if (c.version == act) { snprintf(res, n, "ALREADY_ACTIVE v%u", c.version); return true; }
  if (c.version < act) {
    snprintf(res, n, "REJECTED: v%u older than active v%u (anti-rollback)", c.version, act);
    alarm("CFG_REJECTED", "major", "%s", res);
    return false;
  }
  char slot = active_ == 'A' ? 'B' : 'A', p[300];
  path(slot, p, sizeof p);
  if (!hal::flash::write_file_atomic(p, b, len)) { snprintf(res, n, "REJECTED: flash write failed"); return false; }
  refresh_versions();
  snprintf(res, n, "STAGED v%u in slot %c (signature OK, hash %s)", c.version, slot, c.hash);
  logf("CFG", "%s", res);
  return true;
}

bool ConfigStore::command(const uint8_t* b, int len, uint32_t active_ver, char* res, size_t n) {
  if (len != 20 + 64 || memcmp(b, "TSCA", 4) != 0) { snprintf(res, n, "REJECTED: malformed command"); return false; }
  if (!hal::crypto::ed25519_verify(b, 20, b + 20)) {
    snprintf(res, n, "REJECTED: command signature INVALID");
    alarm("CFG_REJECTED", "major", "%s", res);
    return false;
  }
  if (b[4] != id_) { snprintf(res, n, "REJECTED: command for I%u", b[4]); return false; }
  uint8_t op = b[5];
  uint32_t ver = rd32(b + 8);
  int64_t at = rd64(b + 12);
  char other = active_ == 'A' ? 'B' : 'A';
  if (ver == active_ver) { snprintf(res, n, "ALREADY_ACTIVE v%u", ver); return true; }
  if (slot_version(other) != ver) {
    snprintf(res, n, "REJECTED: slot %c holds v%u, not v%u", other, slot_version(other), ver);
    return false;
  }
  pending_ = true;
  pending_slot_ = other;
  pending_ver_ = ver;
  pending_at_ = at;
  char ts[40];
  fmt_utc(at, ts, sizeof ts);
  snprintf(res, n, "%s v%u scheduled at %s (first cycle boundary after)", op == 2 ? "ROLLBACK to" : "ACTIVATION of", ver, ts);
  logf("CFG", "%s", res);
  return true;
}

bool ConfigStore::activate(Config* active, char* res, size_t n) {
  static uint8_t buf[4096];
  char p[300], err[96];
  int len = 0;
  pending_ = false;
  path(pending_slot_, p, sizeof p);
  Config c;
  if (!hal::flash::read_file(p, buf, sizeof buf, &len) || !parse_config(buf, len, id_, &c, err, sizeof err)) {
    snprintf(res, n, "ACTIVATION FAILED: %s", err);
    return false;
  }
  active_ = pending_slot_;
  char ap[300];
  snprintf(ap, sizeof ap, "%s/active_slot", dir_);
  hal::flash::write_file_atomic(ap, (const uint8_t*)&active_, 1);
  *active = c;
  snprintf(res, n, "ACTIVE v%u (%s) from slot %c, hash %s", c.version, c.tag, active_, c.hash);
  logf("CFG", "%s", res);
  return true;
}

// ---------------------------------------------------------------- recorder
static char g_line[8192];

void Recorder::init(const char* dir, int id) {
  (void)id;
  snprintf(file_, sizeof file_, "%s/records.jsonl", dir);
  snprintf(cursor_file_, sizeof cursor_file_, "%s/upload_cursor", dir);
  uint8_t b[32];
  int n = 0;
  if (hal::flash::read_file(cursor_file_, b, sizeof b - 1, &n)) {
    b[n] = 0;
    cursor_ = atol((const char*)b);
  }
  FILE* f = fopen(file_, "r");
  if (!f) return;
  long off = 0;
  while (fgets(g_line, sizeof g_line, f)) {
    ++seq_;
    off += (long)strlen(g_line);
    if (off <= cursor_) ++uploaded_;
    const char* h = nullptr;                      // the record hash is the LAST "hash" field (cfg has one too)
    for (const char* q = strstr(g_line, "\"hash\":\""); q; q = strstr(q + 1, "\"hash\":\"")) h = q;
    if (h && strlen(h) >= 72) {
      memcpy(prev_, h + 8, 64);
      prev_[64] = 0;
    }
  }
  fclose(f);
  logf("REC", "decision log recovered: %u records, %u uploaded, chain head %.12s", seq_, uploaded_, prev_);
}

void Recorder::append(char* body, size_t len, size_t cap) {
  char h[65];
  hal::crypto::sha256_hex(body, len, h);
  snprintf(body + len - 1, cap - len + 1, ",\"hash\":\"%s\"}", h);
  hal::flash::append_line(file_, body);
  memcpy(prev_, h, 65);
  ++seq_;
}

int Recorder::batch(char* out, size_t cap, long* end_off, int max_records) {
  FILE* f = fopen(file_, "r");
  if (!f) return 0;
  fseek(f, cursor_, SEEK_SET);
  size_t n = 0;
  int k = 0;
  out[n++] = '[';
  long pos = cursor_;
  while (k < max_records && fgets(g_line, sizeof g_line, f)) {
    size_t l = strlen(g_line);
    if (l && g_line[l - 1] == '\n') g_line[--l] = 0;
    if (n + l + 3 >= cap) break;
    if (k) out[n++] = ',';
    memcpy(out + n, g_line, l);
    n += l;
    pos = ftell(f);
    ++k;
  }
  fclose(f);
  out[n++] = ']';
  out[n] = 0;
  *end_off = pos;
  inflight_n_ = k;
  return k;
}

void Recorder::commit(long end_off) {
  cursor_ = end_off;
  uploaded_ += (uint32_t)inflight_n_;
  inflight_n_ = 0;
  char s[32];
  int l = snprintf(s, sizeof s, "%ld", cursor_);
  hal::flash::write_file_atomic(cursor_file_, (const uint8_t*)s, l);
}

}  // namespace tsc::svc
