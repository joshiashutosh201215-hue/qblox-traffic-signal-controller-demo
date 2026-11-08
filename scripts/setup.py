"""One-time setup of the demo runtime (idempotent; --reset wipes it).

  * PKI: demo CA, broker certificate, one client certificate per controller and
    one for the control center (mutual TLS on the center link)
  * peer-network MAC key (AES-128) and the configuration signing key (Ed25519,
    private half in runtime/cc/hsm = stand-in for the HSM)
  * Mosquitto configuration: mTLS listener 8883 with per-device ACLs, read-only
    viewer listener 1883 on localhost
  * rules repository (Git) with the initial rule sets, release v1 validated,
    compiled, signed and factory-provisioned into every controller's flash slot A
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from common import city as C  # noqa: E402
from common import rules as R  # noqa: E402
from control_center import release as REL  # noqa: E402

RT = os.path.join(ROOT, "runtime")
PKI = os.path.join(RT, "pki")


def sh(*cmd, cwd=None):
    subprocess.run(cmd, check=True, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def pki():
    if os.path.exists(os.path.join(PKI, "ca.crt")):
        print("  PKI exists")
        return
    os.makedirs(PKI, exist_ok=True)
    sh("openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", "ca.key", cwd=PKI)
    sh("openssl", "req", "-x509", "-new", "-key", "ca.key", "-sha256", "-days", "3650",
       "-subj", "/O=City Traffic Demo/CN=City Traffic Demo CA", "-out", "ca.crt", cwd=PKI)

    def leaf(name, cn, ext):
        with open(os.path.join(PKI, f"{name}.ext"), "w") as f:
            f.write(ext)
        sh("openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", f"{name}.key", cwd=PKI)
        sh("openssl", "req", "-new", "-key", f"{name}.key", "-subj", f"/O=City Traffic Demo/CN={cn}",
           "-out", f"{name}.csr", cwd=PKI)
        sh("openssl", "x509", "-req", "-in", f"{name}.csr", "-CA", "ca.crt", "-CAkey", "ca.key",
           "-CAcreateserial", "-days", "825", "-sha256", "-extfile", f"{name}.ext", "-out", f"{name}.crt", cwd=PKI)
        os.remove(os.path.join(PKI, f"{name}.csr"))
        os.remove(os.path.join(PKI, f"{name}.ext"))

    leaf("server", "localhost", "subjectAltName=DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth\n")
    leaf("control-center", "control-center", "extendedKeyUsage=clientAuth\n")
    for t in C.CITY:
        leaf(f"tsc{t:02d}", f"tsc{t:02d}", "extendedKeyUsage=clientAuth\n")
    with open(os.path.join(PKI, "peer_mac.key"), "wb") as f:
        f.write(os.urandom(16))
    REL.new_signing_key(os.path.join(PKI, "config_signing.pub"))
    print("  PKI: CA, broker cert, 20 controller certs, control-center cert, peer MAC key, Ed25519 signing key")


def mosquitto_conf():
    d = os.path.join(RT, "broker")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "acl_tls"), "w") as f:
        f.write("# control center: full access\nuser control-center\ntopic readwrite #\n\n"
                "# each controller (username = certificate CN) may only publish its own topics\n"
                "pattern write field/%u/#\npattern read center/%u/#\npattern read center/time\n")
    with open(os.path.join(d, "acl_view"), "w") as f:
        f.write("# localhost viewer listener: read-only (mosquitto_sub for the demo)\ntopic read #\n")
    with open(os.path.join(d, "mosquitto.conf"), "w") as f:
        f.write(f"""# Control-center MQTT broker (demo)
per_listener_settings true
persistence true
persistence_location {RT}/broker/
max_queued_messages 100000
log_dest file {RT}/logs/mosquitto.log
log_type all
log_timestamp true
log_timestamp_format %Y-%m-%dT%H:%M:%S
connection_messages true

# Field devices and the control center: mutual TLS, identity from the client certificate
listener 8883 127.0.0.1
cafile {PKI}/ca.crt
certfile {PKI}/server.crt
keyfile {PKI}/server.key
require_certificate true
use_identity_as_username true
allow_anonymous false
acl_file {d}/acl_tls

# Read-only viewer for demonstrations: mosquitto_sub -h 127.0.0.1 -p 1883 -t 'field/#' -v
listener 1883 127.0.0.1
allow_anonymous true
acl_file {d}/acl_view
""")
    print("  Mosquitto config: mTLS :8883 (ACL per device), read-only viewer :1883")


def rules_repo():
    if os.path.isdir(os.path.join(REL.REPO, ".git")):
        print("  rules repository exists")
        return
    os.makedirs(REL.REPO, exist_ok=True)
    sh("git", "init", "-q", "-b", "main", cwd=REL.REPO)
    with open(os.path.join(REL.REPO, "README.md"), "w") as f:
        f.write("# Traffic rules\n\nOne file per intersection, written by the rule table editor. "
                "Every change is a reviewed commit; every release is a signed tag.\n")
    for t in C.CITY:
        with open(REL.rules_file(t), "w") as f:
            f.write(R.format_rules(R.default_rules(t), t))
    REL.git("add", "-A")
    REL.git("commit", "-q", "-m", "Initial rule sets for 20 intersections", author="a.joshi")
    print("  rules repository: 20 rule files, initial commit by a.joshi")


def release_and_provision():
    if REL.releases():
        print(f"  releases exist (latest v{REL.releases()[-1]['version']})")
        return
    m = REL.make_release("a.joshi", "r.vermeer", "factory baseline")
    for t in C.CITY:
        d = os.path.join(RT, "flash", f"tsc{t:02d}")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "slot_A.cfg"), "wb") as f:
            f.write(REL.config_blob(m["version"], t))
        with open(os.path.join(d, "active_slot"), "w") as f:
            f.write("A")
    print(f"  release v{m['version']} ({m['tag']}, commit {m['git_commit']}) signed and provisioned to 20 flash slots A")


def main():
    if "--reset" in sys.argv and os.path.isdir(RT):
        shutil.rmtree(RT)
        print("runtime wiped")
    for d in ("logs/can", "logs/nmea", "logs/pcap", "logs/procs", "flash", "cc/releases", "cc/hsm"):
        os.makedirs(os.path.join(RT, d), exist_ok=True)
    print("Setting up demo runtime in", RT)
    pki()
    mosquitto_conf()
    rules_repo()
    release_and_provision()
    print("done")


if __name__ == "__main__":
    main()
