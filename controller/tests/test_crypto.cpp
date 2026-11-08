// AES-CMAC against RFC 4493 section 4 examples, Ed25519 against RFC 8032 test 1.
#include <cstdio>
#include <cstring>

#include "hal/hal.hpp"

using namespace tsc;

static int hex(const char* s, uint8_t* out) {
  int n = 0;
  for (; s[0] && s[1]; s += 2) {
    unsigned v;
    sscanf(s, "%2x", &v);
    out[n++] = (uint8_t)v;
  }
  return n;
}

int main() {
  int fails = 0;
  uint8_t key[16], msg[64], exp[16], out[16];
  hex("2b7e151628aed2a6abf7158809cf4f3c", key);
  const char* m = "6bc1bee22e409f96e93d7e117393172aae2d8a571e03ac9c9eb76fac45af8e5130c81c46a35ce411"
                  "e5fbc1191a0a52eff69f2445df4f9b17ad2b417be66c3710";
  hex(m, msg);
  struct { int len; const char* tag; } v[] = {{0, "bb1d6929e95937287fa37d129b756746"},
                                               {16, "070a16b46b4d4144f79bdd9dd04a287c"},
                                               {40, "dfa66747de9ae63030ca32611497c827"},
                                               {64, "51f0bebf7e3b9d92fc49741779363cfe"}};
  for (auto& t : v) {
    hex(t.tag, exp);
    hal::crypto::cmac_full(key, msg, t.len, out);
    bool ok = !memcmp(out, exp, 16);
    printf("AES-CMAC RFC 4493 Mlen=%2d: %s\n", t.len, ok ? "PASS" : "FAIL");
    fails += !ok;
  }
  // Ed25519 RFC 8032 section 7.1 TEST 1 (empty message)
  uint8_t pub[32], sig[64];
  hex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", pub);
  hex("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46b"
      "d25bf5f0595bbe24655141438e7a100b", sig);
  FILE* f = fopen("/tmp/tsc_test_pub.bin", "wb");
  fwrite(pub, 1, 32, f);
  fclose(f);
  f = fopen("/tmp/tsc_test_key.bin", "wb");
  fwrite(key, 1, 16, f);
  fclose(f);
  hal::crypto::init("/tmp/tsc_test_key.bin", "/tmp/tsc_test_pub.bin");
  bool ok = hal::crypto::ed25519_verify(msg, 0, sig);
  printf("Ed25519 RFC 8032 test 1 verify: %s\n", ok ? "PASS" : "FAIL");
  fails += !ok;
  sig[5] ^= 1;
  ok = !hal::crypto::ed25519_verify(msg, 0, sig);
  printf("Ed25519 tampered signature rejected: %s\n", ok ? "PASS" : "FAIL");
  fails += !ok;
  return fails ? 1 : 0;
}
