#!/usr/bin/env bash
# Fetch the broker and MQTT libraries into third_party/ without root:
#   Mosquitto 2.0.18 + libmosquitto + its libraries (Ubuntu 24.04 packages, unpacked with dpkg -x)
#   paho-mqtt 2.1.0 (Python wheel, unpacked)
# Usage: scripts/fetch_third_party.sh [target-dir]      (default: third_party)
set -euo pipefail
cd "$(dirname "$0")/.."
TP=$(realpath -m "${1:-third_party}")
mkdir -p "$TP/debs" "$TP/mosquitto" "$TP/py"
cd "$TP/debs"
MQ=2.0.18-1build3
for p in "mosquitto=$MQ" "mosquitto-clients=$MQ" "libmosquitto1=$MQ" "libmosquitto-dev=$MQ" libcjson1 libdlt2 libwebsockets19t64; do
  apt-get download "$p" 2>/dev/null || apt-get download "${p%%=*}"     # pinned version, else the archive's current one
done
for d in *.deb; do dpkg -x "$d" "$TP/mosquitto"; done
python3 -m pip download --quiet --no-deps --only-binary=:all: -d . paho-mqtt==2.1.0
python3 -m zipfile -e paho_mqtt-2.1.0-py3-none-any.whl "$TP/py"
echo "third_party ready: $("$TP/mosquitto/usr/sbin/mosquitto" -h 2>/dev/null | head -1 || echo mosquitto)"
