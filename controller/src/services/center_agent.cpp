// Center agent: outbound-only MQTT 3.1.1 over mutual TLS (libmosquitto).
// The link is unreliable (down for hours); nothing in the decision path uses it.
//   up:   field/<id>/status (retained, LWT "offline"), health (QoS0), alarm (QoS1),
//         records (QoS1, store-and-forward from flash), cfg/ack, cfg/activated
//   down: center/<id>/cfg/stage (signed table), center/<id>/cfg/cmd (signed command),
//         center/time (beacon, logged for comparison only - never used to set the clock)
#include <mosquitto.h>

#include <cstdlib>

#include "services/services.hpp"

namespace tsc::svc {

extern TimeService* g_time;

namespace {
CenterAgent* g_agent = nullptr;
void cb_connect(mosquitto*, void*, int rc) { g_agent->on_connect(rc); }
void cb_disconnect(mosquitto*, void*, int rc) { g_agent->on_disconnect(rc); }
void cb_message(mosquitto*, void*, const mosquitto_message* m) { g_agent->on_message(m); }
void cb_publish(mosquitto*, void*, int mid) { g_agent->on_publish(mid); }
char g_payload[16384];
}  // namespace

void CenterAgent::init(int id, const char* pki, HealthFn health, CfgFn cfg, Recorder* rec) {
  g_agent = this;
  id_ = id;
  health_ = health;
  cfg_ = cfg;
  rec_ = rec;
  snprintf(name_, sizeof name_, "tsc%02d", id);
  mosquitto_lib_init();
  m_ = mosquitto_new(name_, true, this);
  char ca[300], crt[300], key[300], will[64];
  snprintf(ca, sizeof ca, "%s/ca.crt", pki);
  snprintf(crt, sizeof crt, "%s/%s.crt", pki, name_);
  snprintf(key, sizeof key, "%s/%s.key", pki, name_);
  mosquitto_tls_set(m_, ca, nullptr, crt, key, nullptr);
  mosquitto_tls_opts_set(m_, 1, "tlsv1.2", nullptr);
  snprintf(will, sizeof will, "field/%s/status", name_);
  mosquitto_will_set(m_, will, 7, "offline", 1, true);
  mosquitto_connect_callback_set(m_, cb_connect);
  mosquitto_disconnect_callback_set(m_, cb_disconnect);
  mosquitto_message_callback_set(m_, cb_message);
  mosquitto_publish_callback_set(m_, cb_publish);
  mosquitto_connect_async(m_, "127.0.0.1", port::wan(id), 60);
  last_try_ = mono_ns();
  logf("MQTT", "connecting to control center via WAN (127.0.0.1:%d), mutual TLS, client cert CN=%s", port::wan(id), name_);
}

int CenterAgent::sock() const { return m_ ? mosquitto_socket(m_) : -1; }

void CenterAgent::publish(const char* sub, const char* payload, int qos, bool retain) {
  char t[96];
  snprintf(t, sizeof t, "field/%s/%s", name_, sub);
  mosquitto_publish(m_, nullptr, t, (int)strlen(payload), payload, qos, retain);
}

void CenterAgent::on_connect(int rc) {
  if (rc != 0) {
    logf("MQTT", "CONNACK refused: %s", mosquitto_connack_string(rc));
    return;
  }
  connected_ = true;
  char t[96];
  snprintf(t, sizeof t, "center/%s/cfg/#", name_);
  mosquitto_subscribe(m_, nullptr, t, 1);
  mosquitto_subscribe(m_, nullptr, "center/time", 0);
  publish("status", "online", 1, true);
  logf("MQTT", "connected (TLS session up); %d records and %d alarms waiting for upload", rec_->backlog(), alarm_pending());
  rec_mid_ = alarm_mid_ = -1;
}

void CenterAgent::on_disconnect(int rc) {
  if (connected_) logf("MQTT", "center link DOWN (%s): continuing autonomously, buffering records and alarms",
                       rc ? mosquitto_strerror(rc) : "closed");
  connected_ = false;
  rec_mid_ = alarm_mid_ = -1;
}

void CenterAgent::on_message(const mosquitto_message* m) {
  const char* t = m->topic;
  char pre[64];
  snprintf(pre, sizeof pre, "center/%s/cfg/", name_);
  if (!strncmp(t, pre, strlen(pre))) {
    cfg_(t + strlen(pre), (const uint8_t*)m->payload, m->payloadlen);
  } else if (!strcmp(t, "center/time") && m->payloadlen < 64) {
    static int n = 0;
    if (n++ % 30 == 0) {
      char b[64];
      memcpy(b, m->payload, (size_t)m->payloadlen);
      b[m->payloadlen] = 0;
      double cc = atof(b);
      logf("TIME", "center beacon says %.3f, local clock %.3f: difference %+.2f s is mostly WAN latency "
           "(±5 s) -> logged only, never used to set the clock", cc, g_time->now() / 1e9, g_time->now() / 1e9 - cc);
    }
  }
}

void CenterAgent::on_publish(int mid) {
  if (mid == rec_mid_) {
    rec_->commit(rec_end_);
    rec_mid_ = -1;
  }
  if (mid == alarm_mid_) {
    alarm_pop();
    alarm_mid_ = -1;
  }
}

void CenterAgent::tick() {
  int64_t now = mono_ns();
  int rc = mosquitto_loop(m_, 0, 1);
  if (rc != MOSQ_ERR_SUCCESS) {
    if (connected_) on_disconnect(rc);
    if (now - last_try_ > 5000000000LL) {
      last_try_ = now;
      mosquitto_reconnect_async(m_);
    }
    return;
  }
  if (!connected_) return;
  if (now - last_health_ > 2000000000LL) {
    last_health_ = now;
    int n = health_(g_payload, sizeof g_payload);
    if (n > 0) publish("health", g_payload, 0);
  }
  if (alarm_mid_ < 0) {
    Alarm a;
    if (alarm_peek(&a)) {
      char b[400];
      JsonW w(b, sizeof b);
      w.obj().kv("code", a.code).kv("sev", a.sev).kv("text", a.text).kv("t_ns", a.t).end_obj();
      char t[96];
      snprintf(t, sizeof t, "field/%s/alarm", name_);
      mosquitto_publish(m_, &alarm_mid_, t, (int)w.len(), b, 1, false);
    }
  }
  if (rec_mid_ < 0 && rec_->backlog() > 0 && now - last_rec_ > 300000000LL) {
    last_rec_ = now;
    int k = rec_->batch(g_payload, sizeof g_payload, &rec_end_, 8);
    if (k > 0) {
      char t[96];
      snprintf(t, sizeof t, "field/%s/records", name_);
      mosquitto_publish(m_, &rec_mid_, t, (int)strlen(g_payload), g_payload, 1, false);
    }
  }
}

}  // namespace tsc::svc
