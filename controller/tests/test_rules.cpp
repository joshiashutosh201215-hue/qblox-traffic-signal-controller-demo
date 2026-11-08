// Golden vectors: the C++ interpreter must produce exactly the decisions of the
// Python reference evaluator (common/rules.py) used by the control center replay.
#include <cstdio>
#include <cstdlib>
#include <cstring>

#include "app/app.hpp"

using namespace tsc;

int main(int argc, char** argv) {
  FILE* f = fopen(argc > 1 ? argv[1] : "shared/rule_vectors.txt", "r");
  if (!f) { perror("vectors"); return 2; }
  static svc::Config c;
  static app::Input in[24];
  static app::RuleResult res[kMaxRules];
  int n_in = 0, vectors = 0, fails = 0;
  char line[512];
  while (fgets(line, sizeof line, f)) {
    char* t = strtok(line, " \n");
    if (!t) continue;
    if (!strcmp(t, "V")) {
      c = svc::Config{};
      c.main_max_ext_ms = 8000;
      n_in = 0;
    } else if (!strcmp(t, "R")) {
      svc::Rule& r = c.rules[c.n_rules++];
      snprintf(r.id, sizeof r.id, "%s", strtok(nullptr, " "));
      r.prio = (uint8_t)atoi(strtok(nullptr, " "));
      r.action = (uint8_t)atoi(strtok(nullptr, " "));
      r.action_ms = (uint32_t)atoi(strtok(nullptr, " "));
      r.if_missing = (uint8_t)atoi(strtok(nullptr, " "));
      r.missing_ms = (uint32_t)atoi(strtok(nullptr, " "));
      r.n = (uint8_t)atoi(strtok(nullptr, " "));
      for (int k = 0; k < r.n; ++k) {
        r.c[k].src = (uint8_t)atoi(strtok(nullptr, " "));
        r.c[k].det = (uint8_t)atoi(strtok(nullptr, " "));
        r.c[k].measure = (uint8_t)atoi(strtok(nullptr, " "));
        r.c[k].op = (uint8_t)atoi(strtok(nullptr, " "));
        r.c[k].value = atoi(strtok(nullptr, " \n"));
      }
    } else if (!strcmp(t, "I")) {
      app::Input& x = in[n_in++];
      x.src = (uint8_t)atoi(strtok(nullptr, " "));
      x.det = (uint8_t)atoi(strtok(nullptr, " "));
      x.look = atoi(strtok(nullptr, " ")) ? svc::Look::OK : svc::Look::MISSING;
      x.r.presence = (uint8_t)atoi(strtok(nullptr, " "));
      x.r.count = (uint8_t)atoi(strtok(nullptr, " "));
      x.r.occ_permille = (uint16_t)atoi(strtok(nullptr, " "));
      x.r.gap_ds = (uint16_t)atoi(strtok(nullptr, " \n"));
    } else if (!strcmp(t, "E")) {
      int act = atoi(strtok(nullptr, " "));
      unsigned ext = (unsigned)atoi(strtok(nullptr, " "));
      const char* win = strtok(nullptr, " ");
      const char* st = strtok(nullptr, " \n");
      app::Decision d;
      app::evaluate(c, in, n_in, res, &d);
      char got[32] = "";
      for (int i = 0; i < c.n_rules; ++i) got[i] = "FNM"[res[i].st];
      const char* gw = d.winner >= 0 ? c.rules[d.winner].id : "-";
      bool ok = d.act == act && d.ext_ms == ext && !strcmp(gw, win) && !strcmp(got, st);
      ++vectors;
      if (!ok) {
        ++fails;
        printf("vector %d MISMATCH: expected act=%d ext=%u by=%s [%s], got act=%d ext=%u by=%s [%s]\n", vectors, act,
               ext, win, st, d.act, d.ext_ms, gw, got);
      }
    }
  }
  printf("rule vectors: %d checked, %d mismatches -> %s\n", vectors, fails, fails ? "FAIL" : "PASS");
  return fails ? 1 : 0;
}
