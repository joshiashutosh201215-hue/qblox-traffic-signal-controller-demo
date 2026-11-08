// Rule interpreter + decision arbiter. Must agree with common/rules.py::evaluate
// (checked by tests/test_rules.cpp against shared/rule_vectors.json).
#include "app/app.hpp"

namespace tsc::app {

int32_t measure_milli(const svc::Reading& r, uint8_t measure) {
  switch (measure) {
    case 1: return (int32_t)r.occ_permille * 100;   // occupancy, milli-percent
    case 2: return (int32_t)r.gap_ds * 100;         // gap, ms
    case 3: return (int32_t)r.count * 1000;
    default: return (int32_t)r.presence * 1000;
  }
}

static bool cmp(int32_t a, uint8_t op, int32_t b) {
  switch (op) {
    case 1: return a > b;
    case 2: return a >= b;
    case 3: return a < b;
    case 4: return a <= b;
    default: return a == b;
  }
}

int find_input(const Input* in, int n, uint8_t src, uint8_t det) {
  for (int i = 0; i < n; ++i)
    if (in[i].src == src && in[i].det == det) return i;
  return -1;
}

const char* act_text(uint8_t act) {
  return act == ACT_EXTEND ? "extend green" : act == ACT_HOLD ? "hold, do not extend" : "do not extend";
}

void evaluate(const svc::Config& c, const Input* in, int n_in, RuleResult* res, Decision* d) {
  d->act = ACT_NONE;
  d->ext_ms = 0;
  d->winner = -1;
  for (int i = 0; i < c.n_rules; ++i) {        // compiled in descending priority
    const svc::Rule& r = c.rules[i];
    RuleResult& x = res[i];
    x.missing_input = -1;
    bool fired = true, missing = false;
    for (int k = 0; k < r.n; ++k) {
      int idx = find_input(in, n_in, r.c[k].src, r.c[k].det);
      if (idx < 0 || in[idx].look != svc::Look::OK) {
        missing = true;
        x.missing_input = idx;
        break;
      }
      if (!cmp(measure_milli(in[idx].r, r.c[k].measure), r.c[k].op, r.c[k].value)) fired = false;
    }
    if (missing) {
      x.st = MISSING;
      x.act = r.if_missing;
      x.ms = r.missing_ms;
    } else if (fired) {
      x.st = FIRED;
      x.act = r.action;
      x.ms = r.action_ms;
    } else {
      x.st = NOT_FIRED;
      x.act = ACT_NONE;
      x.ms = 0;
    }
    if (d->winner < 0 && x.act != ACT_NONE) {  // arbiter: highest priority action wins
      d->winner = i;
      d->act = x.act;
      d->ext_ms = x.act == ACT_EXTEND ? (x.ms < c.main_max_ext_ms ? x.ms : c.main_max_ext_ms) : 0;
    }
  }
}

}  // namespace tsc::app
