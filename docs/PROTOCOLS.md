# Protocols, formats and ports

All multi-byte fields little-endian unless stated (PTP is big-endian per IEEE 1588).

## Channel message (peer network) — 104 bytes

| Offset | Size | Field |
|---|---|---|
| 0 | 2 | magic `TS` |
| 2 | 1 | schema version (1) — older controllers ignore newer schemas |
| 3 | 1 | device class (1 = vehicle detectors, 2 = pedestrian, planned) |
| 4 | 1 | channel |
| 5 | 1 | source intersection |
| 6 | 1 | sender clock quality: 0 GNSS, 1 PTP, 2 HOLDOVER, 3 UNSYNC |
| 7 | 1 | number of readings (8) |
| 8 | 4 | sequence number (persisted across reboots) |
| 12 | 4 | sender configuration version |
| 16 | 8 | sample time, UTC ns on the synchronized clock |
| 24 | 4 | sender cycle number |
| 28 | 4 | reserved |
| 32 | 8×8 | readings: det, quality (0 OK, 1 STUCK_ON, 2 LOOP_FAULT, 3 SILENT, 4 CHATTER), presence, count (10 s), occupancy ‰ (u16), gap 0.1 s (u16) |
| 96 | 8 | AES-128-CMAC over bytes 0..95, truncated to 64 bits |

On the field network: UDP 47100 to multicast 239.1.0.<channel> (the channel decides who receives).
Receiver checks, in order: schema → MAC → sequence newer than last (replay) → sequence gap (loss) →
sampled within the receiver's current cycle → sender clock synchronized → arrival − sample within the
expected delivery ±1 s (clock plausibility) → per-reading quality.

## Peer-network envelope (controller ↔ emulated switch, UDP 30000 / 31000+id)

`"PNET"`, type (1 channel, 2 PTP event, 3 PTP general, 4 subscribe), src, dst (0 = neighbours / channel),
aux (channel or PTP message type), u16 length, payload.

## PTP (IEEE 1588-2008, UDP 319 event / 320 general on the field network)

Announce (64 B) every 1 s: grandmasterClockQuality.clockClass 6 = traceable to GNSS (own receiver, or
via a PTP master with stepsRemoved < 2), 7 = holdover, 248 = free-running; timeSource 0x20 GPS / 0xA0
internal oscillator. Sync (two-step) + Follow_Up every 1 s from usable masters; Delay_Req/Delay_Resp
unicast. offset = ((t2 − t1) − (t4 − t3)) / 2. Timescale TAI, currentUtcOffset 37 s.
BMCA (simplified): best clockClass 6 neighbour, fewest steps, lowest id; GNSS preferred when locked.

## CANopen detector cards (CiA 301 subset), virtual bus per cabinet

Linux `struct can_frame` (16 bytes) over UDP: cards → controller 32000+id, controller → cards 32100+id.

| COB-ID | Object | Data |
|---|---|---|
| 0x000 | NMT from controller | `01 <node>` start remote node |
| 0x700 + node | heartbeat 1 s | `00` boot-up, `7F` pre-operational, `05` operational |
| 0x180 + node | TPDO1 on change + 100 ms | presence bitmap (4 ch), counter, card ms (u32) |
| 0x080 + node | EMCY | error code u16 (0x5000 device hardware / 0x0000 reset), error register, channel, kind (1 open, 2 short) |

Nodes: 0x11 = D1–D4 (stop bars), 0x12 = D5–D8 (exits). Heartbeat consumer timeout 1.5 s.

## Signed configuration (control center → controller)

128-byte header (`TSCF`, format 1, tsc id, rule count, version, flags, created ns, release tag,
own channel, buffer N, subscriptions, upstream ids, 15 timing fields in ms, Git commit) + 64 bytes per
rule (id, phase, n conditions, action, if-missing, ms values, priority, 3 × condition {src, det, measure,
op, value ×1000}) + 64-byte Ed25519 signature. Layout: `common/rules.py` `HEADER_FMT`/`RULE_FMT`.

Activation / rollback command: `TSCA`, tsc, op (1 activate staged, 2 roll back), version, activate-at
UTC ns + Ed25519 signature. The controller switches at its first cycle boundary after that time.

## MQTT topics (broker mTLS 8883; username = certificate CN)

| Topic | Dir | QoS | Payload |
|---|---|---|---|
| `field/tscNN/status` | up, retained, LWT | 1 | `online` / `offline` |
| `field/tscNN/health` | up, every 2 s | 0 | JSON: mode, phase, sync, detectors, neighbours, buffer, security, cfg, safety unit, backlog |
| `field/tscNN/alarm` | up | 1 | `{code, sev, text, t_ns}` |
| `field/tscNN/records` | up, store-and-forward | 1 | JSON array of hash-chained decision records |
| `field/tscNN/cfg/ack`, `cfg/activated` | up | 1 | deployment results |
| `center/tscNN/cfg/stage` | down | 1 | signed configuration (binary) |
| `center/tscNN/cfg/cmd` | down | 1 | signed activation / rollback command |
| `center/time` | down | 0 | center UTC (logged only, never used to set clocks) |

ACL: devices may write `field/%u/#` and read `center/%u/#`, `center/time`. The control center
holds a persistent session so the broker queues QoS 1 traffic while it is down.

## Decision record (controller flash, JSON lines)

`rec, seq, tsc, cycle, t_utc, t_ns, sync, sync_bound_ms, mode, cfg{ver, tag, hash, git, legacy_prev_cycle},
inputs[{src, det, ch, status, q, presence, count, occ_pct, gap_s, sample_utc, age_s, seq, why}],
rules[{id, prio, status, result, missing}], decision{action, ext_s, by, main_end_s, waited_s, latency_s,
deadline_s, deadline_met, deadline_basis}, prev, hash` — `hash` = SHA-256 of the record without it;
`prev` = previous record's hash.

## Ports (all 127.0.0.1)

| Port | Use |
|---|---|
| 8080 | control center REST/WS + GUI |
| 8091 | testbench WebSocket (bench view, fault injection) |
| 8883 / 1883 | Mosquitto mTLS / read-only viewer |
| 18801–18820 | WAN emulator in front of 8883, one per controller |
| 30000 / 31001–31020 | peer switch / controller peer interfaces |
| 32001–32020 / 32101–32120 | CAN bus cards → controller / controller → cards |
| 33001–33020 / 33101–33120 / 33500 | lamp drive → safety unit / safety unit → controller / field outputs → plant |
| 34001–34020 / 34101–34120 | bench fault injection → controller HAL / → safety unit |
| 35000 | bench probe (controllers → bench) |
