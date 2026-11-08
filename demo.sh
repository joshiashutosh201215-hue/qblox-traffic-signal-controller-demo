#!/usr/bin/env bash
# Qblox traffic-signal assignment demo launcher.
#   ./demo.sh setup     build C/C++, create PKI, rules repo, release v1, provision flash
#   ./demo.sh start     broker + control center + testbench (20 controllers, 20 safety units)
#   ./demo.sh stop | restart | status | test | reset
#   ./demo.sh sub       live MQTT traffic (read-only viewer listener)
#   ./demo.sh tail 17   follow controller I17's log
set -euo pipefail
cd "$(dirname "$0")"
ROOT=$(pwd)
RT=$ROOT/runtime
MOSQ=$ROOT/third_party/mosquitto/usr
export LD_LIBRARY_PATH=$MOSQ/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
export PYTHONPATH=$ROOT:$ROOT/third_party/py
PIDS=$RT/pids

build() {
  cmake -S controller -B controller/build -DCMAKE_BUILD_TYPE=RelWithDebInfo >/dev/null
  cmake --build controller/build -j"$(nproc)" 2>&1 | grep -E "error|warning" || true
  echo "built: controller/build/tsc controller/build/mmu"
}

up() {  # name command...
  local name=$1; shift
  nohup "$@" >"$RT/logs/procs/$name.out" 2>&1 &
  echo $! >"$PIDS/$name.pid"
}

running() { [[ -f $PIDS/$1.pid ]] && kill -0 "$(cat "$PIDS/$1.pid")" 2>/dev/null; }

start() {
  [[ -x $MOSQ/sbin/mosquitto ]] || { echo "run ./demo.sh setup first"; exit 1; }
  [[ -x controller/build/tsc ]] || build
  [[ -f $RT/pki/ca.crt ]] || python3 scripts/setup.py
  mkdir -p "$PIDS" "$RT/logs/procs"
  running broker || TZ=UTC up broker "$MOSQ/sbin/mosquitto" -c "$RT/broker/mosquitto.conf"
  sleep 0.5
  running cc || up cc python3 -m uvicorn control_center.server:app --host 127.0.0.1 --port 8080 --log-level warning
  sleep 1
  running bench || up bench python3 testbench/bench.py
  sleep 2
  status
  echo
  echo "GUI:  http://127.0.0.1:8080"
}

stop() {
  for n in bench cc broker; do
    if running $n; then kill "$(cat "$PIDS/$n.pid")" 2>/dev/null || true; fi
    rm -f "$PIDS/$n.pid"
  done
  sleep 1
  pkill -f "$ROOT/controller/build/tsc" 2>/dev/null || true
  pkill -f "$ROOT/controller/build/mmu" 2>/dev/null || true
  echo "stopped"
}

status() {
  for n in broker cc bench; do
    if running $n; then echo "  $n: running (pid $(cat "$PIDS/$n.pid"))"; else echo "  $n: stopped"; fi
  done
  echo "  controllers: $(pgrep -fc "$ROOT/controller/build/tsc" || true)/20   safety units: $(pgrep -fc "$ROOT/controller/build/mmu" || true)/20"
}

case "${1:-}" in
  setup) [[ -x $MOSQ/sbin/mosquitto ]] || scripts/fetch_third_party.sh; build; python3 scripts/setup.py ;;
  build) build ;;
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  status) status ;;
  reset) stop; python3 scripts/setup.py --reset ;;
  test)
    python3 tools/gen_rule_vectors.py
    (cd controller/build && ctest --output-on-failure)
    python3 tools/selftest.py ;;
  sub) exec "$MOSQ/bin/mosquitto_sub" -h 127.0.0.1 -p 1883 -t 'field/#' -t 'center/#' -v ;;
  tail) exec tail -f "$RT/logs/tsc$(printf %02d "${2:-17}").log" ;;
  *) sed -n '2,9p' "$0" ;;
esac
