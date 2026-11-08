/* Signal safety unit: conflict monitor (MMU-style), one per cabinet.
 *
 * Deliberately independent of the controller: a separate program, written in C,
 * with its own fixed compatibility table. Rules and configuration can never
 * change it. It sits between the load-switch drive and the field: it watches the
 * ACTUAL lamp outputs and, on any fault, transfers the intersection to flashing
 * (main amber / side red) and latches until a technician resets it.
 *
 * Monitored faults (thresholds are this demo's values):
 *   CONFLICT        main (W/E) and side (N/S) green-or-amber at the same time >= 150 ms
 *   MULTI-IND       two lamps on one head >= 150 ms
 *   RED FAIL        a head completely dark >= 700 ms
 *   CLEARANCE       green followed by red without >= 2.7 s of amber
 *   WATCHDOG        no load-switch update from the controller for 1 s
 *
 * Lamp word: bit = head*3 + lamp, heads W=0 E=1 N=2 S=3, lamps R=0 A=1 G=2.
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <netinet/in.h>
#include <poll.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define FIELD_PORT 33500
#define MS 1000000LL

static const char *HEAD[4] = {"W", "E", "N", "S"};
static int id;
static FILE *logf_;

static int64_t mono(void) {
  struct timespec t;
  clock_gettime(CLOCK_MONOTONIC, &t);
  return (int64_t)t.tv_sec * 1000000000LL + t.tv_nsec;
}

static void lg(const char *fmt, ...) {
  struct timespec t;
  clock_gettime(CLOCK_REALTIME, &t);
  struct tm tm;
  gmtime_r(&t.tv_sec, &tm);
  char msg[300];
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(msg, sizeof msg, fmt, ap);
  va_end(ap);
  fprintf(logf_ ? logf_ : stderr, "%04d-%02d-%02dT%02d:%02d:%02d.%03ldZ MMU%02d %s\n", tm.tm_year + 1900,
          tm.tm_mon + 1, tm.tm_mday, tm.tm_hour, tm.tm_min, tm.tm_sec, t.tv_nsec / 1000000, id, msg);
}

static int udp(int port) {
  int fd = socket(AF_INET, SOCK_DGRAM | SOCK_NONBLOCK, 0);
  struct sockaddr_in a = {0};
  a.sin_family = AF_INET;
  a.sin_port = htons((uint16_t)port);
  a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  int one = 1;
  setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
  if (bind(fd, (struct sockaddr *)&a, sizeof a) != 0) {
    perror("bind");
    exit(1);
  }
  return fd;
}

static void send_to(int fd, int port, const void *b, int n) {
  struct sockaddr_in a = {0};
  a.sin_family = AF_INET;
  a.sin_port = htons((uint16_t)port);
  a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  sendto(fd, b, (size_t)n, 0, (struct sockaddr *)&a, sizeof a);
}

static int lamp(uint16_t L, int head, int l) { return (L >> (head * 3 + l)) & 1; }
static int go(uint16_t L, int head) { return lamp(L, head, 1) || lamp(L, head, 2); }

int main(int argc, char **argv) {
  char rt[256] = "runtime";
  for (int i = 1; i + 1 < argc; i += 2) {
    if (!strcmp(argv[i], "--id")) id = atoi(argv[i + 1]);
    if (!strcmp(argv[i], "--runtime")) snprintf(rt, sizeof rt, "%s", argv[i + 1]);
  }
  char p[400];
  snprintf(p, sizeof p, "%s/logs/mmu%02d.log", rt, id);
  logf_ = fopen(p, "a");
  if (logf_) setvbuf(logf_, NULL, _IOLBF, 0);
  int cmd_fd = udp(33000 + id), bp_fd = udp(34100 + id), out_fd = udp(0);
  lg("power-up: start-up flash until the controller drives the load switches");

  uint16_t cmd = 0, welded = 0, field = 0, last_field = 0xFFFF;
  int latched = 0, have_cmd = 0;
  char fault[64] = "";
  int64_t last_cmd = 0, last_status = 0;
  int64_t conflict_since = 0, multi_since[4] = {0}, dark_since[4] = {0};
  int64_t amber_since[4] = {0};
  int prev_state[4] = {0};   /* 0 red, 1 amber, 2 green, 3 other */
  uint32_t trips = 0;

  for (;;) {
    struct pollfd fds[2] = {{cmd_fd, POLLIN, 0}, {bp_fd, POLLIN, 0}};
    poll(fds, 2, 10);
    int64_t now = mono();
    uint8_t b[256];
    int n;
    while ((n = (int)recv(cmd_fd, b, sizeof b, 0)) >= 12) {
      if (memcmp(b, "LAMP", 4) != 0) continue;
      memcpy(&cmd, b + 8, 2);
      if (!have_cmd) lg("controller is driving the load switches: normal operation");
      have_cmd = 1;
      last_cmd = now;
    }
    while ((n = (int)recv(bp_fd, b, sizeof b - 1, 0)) > 0) {   /* technician / bench fault injection */
      b[n] = 0;
      if (strstr((char *)b, "\"reset\"")) {
        if (latched) lg("technician RESET: fault '%s' cleared, field handed back to the controller", fault);
        latched = 0;
        fault[0] = 0;
        conflict_since = 0;
        memset(multi_since, 0, sizeof multi_since);
        memset(dark_since, 0, sizeof dark_since);
        memset(prev_state, 0, sizeof prev_state);
        last_cmd = now;
      } else if (strstr((char *)b, "\"weld\"")) {
        char *v = strstr((char *)b, "\"value\"");
        int bit = v ? atoi(v + 8) : -1;
        welded = bit >= 0 && bit < 12 ? (uint16_t)(1u << bit) : 0;
        lg("bench fault injection: load switch %s", welded ? "WELDED ON" : "repaired");
        if (welded) lg("  welded output: head %s %s", HEAD[bit / 3], bit % 3 == 0 ? "red" : bit % 3 == 1 ? "amber" : "green");
      }
    }
    if (have_cmd && !latched && now - last_cmd > 1000 * MS) {
      latched = 1;
      ++trips;
      snprintf(fault, sizeof fault, "WATCHDOG: no load-switch update for %.1f s", (now - last_cmd) / 1e9);
      lg("FAULT %s -> FLASH (latched)", fault);
    }
    if (latched || !have_cmd) {
      /* flash transfer relays: load switches (and any welded output) are disconnected */
      int on = (now / (500 * MS)) % 2;
      field = on ? (uint16_t)((1u << (0 * 3 + 1)) | (1u << (1 * 3 + 1)) | (1u << (2 * 3 + 0)) | (1u << (3 * 3 + 0))) : 0;
    } else {
      field = (uint16_t)(cmd | welded);
      int c = (go(field, 0) || go(field, 1)) && (go(field, 2) || go(field, 3));
      if (c) {
        if (!conflict_since) conflict_since = now;
        if (now - conflict_since >= 150 * MS) {
          snprintf(fault, sizeof fault, "CONFLICT: W/E and N/S both green/amber");
        }
      } else {
        conflict_since = 0;
      }
      for (int h = 0; h < 4 && !fault[0]; ++h) {
        int cnt = lamp(field, h, 0) + lamp(field, h, 1) + lamp(field, h, 2);
        if (cnt > 1) {
          if (!multi_since[h]) multi_since[h] = now;
          if (now - multi_since[h] >= 150 * MS) snprintf(fault, sizeof fault, "MULTIPLE INDICATION on head %s", HEAD[h]);
        } else {
          multi_since[h] = 0;
        }
        if (cnt == 0) {
          if (!dark_since[h]) dark_since[h] = now;
          if (now - dark_since[h] >= 700 * MS) snprintf(fault, sizeof fault, "RED FAIL: head %s dark", HEAD[h]);
        } else {
          dark_since[h] = 0;
        }
        int st = cnt != 1 ? 3 : lamp(field, h, 0) ? 0 : lamp(field, h, 1) ? 1 : 2;
        if (st == 1 && prev_state[h] != 1) amber_since[h] = now;
        if (st == 0 && prev_state[h] == 2)
          snprintf(fault, sizeof fault, "CLEARANCE: head %s green->red without amber", HEAD[h]);
        if (st == 0 && prev_state[h] == 1 && now - amber_since[h] < 2700 * MS)
          snprintf(fault, sizeof fault, "CLEARANCE: head %s amber only %.1f s", HEAD[h], (now - amber_since[h]) / 1e9);
        if (st != 3) prev_state[h] = st;
      }
      if (fault[0]) {
        latched = 1;
        ++trips;
        lg("FAULT %s -> FLASH (latched until technician reset); field outputs were 0x%03X", fault, field);
      }
    }
    if (field != last_field || now - last_status >= 100 * MS) {
      uint8_t s[72] = {0};
      memcpy(s, "MMUS", 4);
      s[4] = (uint8_t)latched;
      memcpy(s + 6, &field, 2);
      memcpy(s + 8, fault, 63);
      send_to(out_fd, 33100 + id, s, 72);     /* to the controller */
      uint8_t f[80] = {0};
      memcpy(f, "FLD1", 4);
      f[4] = (uint8_t)id;
      f[5] = (uint8_t)(latched ? 2 : have_cmd ? 0 : 1);   /* 0 normal, 1 start-up flash, 2 fault flash */
      memcpy(f + 6, &field, 2);
      memcpy(f + 8, &welded, 2);
      memcpy(f + 10, &trips, 4);
      memcpy(f + 14, fault, 63);
      send_to(out_fd, FIELD_PORT, f, 80);     /* to the field (testbench plant) */
      last_field = field;
      last_status = now;
    }
  }
}
