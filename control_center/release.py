"""Rules repository (Git), validation, compilation, signing and releases.

The signing key lives in runtime/cc/hsm/ and stands in for a hardware security
module: only this module can produce configurations the controllers accept.
"""
import json
import os
import struct
import subprocess
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from common import city as C
from common import rules as R

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
RT = os.path.join(ROOT, "runtime")
REPO = os.path.join(RT, "rules_repo")
RELEASES = os.path.join(RT, "cc", "releases")
HSM_KEY = os.path.join(RT, "cc", "hsm", "config_signing.key")

ENGINEERS = {
    "a.joshi": "Aashutosh Joshi <a.joshi@traffic.city.example>",
    "r.vermeer": "Rosa Vermeer <r.vermeer@traffic.city.example>",
    "k.smit": "Koen Smit <k.smit@traffic.city.example>",
}


def git(*args, author=None, check=True):
    env = dict(os.environ)
    if author:
        name, mail = ENGINEERS.get(author, f"{author} <{author}@traffic.city.example>").rsplit(" <", 1)
        env.update(GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=mail[:-1], GIT_COMMITTER_NAME=name,
                   GIT_COMMITTER_EMAIL=mail[:-1])
    r = subprocess.run(["git", "-C", REPO, *args], capture_output=True, text=True, env=env)
    if check and r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return r.stdout


def rules_file(tsc):
    return os.path.join(REPO, f"I{tsc:02d}.rules")


def load_all(at=None):
    """{tsc: [Rule]} or {tsc: RuleError}. `at` = git revision (None = working copy)."""
    out = {}
    for tsc in C.CITY:
        try:
            text = git("show", f"{at}:I{tsc:02d}.rules") if at else open(rules_file(tsc)).read()
            out[tsc] = R.parse_rules(text, tsc)
        except R.RuleError as e:
            out[tsc] = e
        except (OSError, RuntimeError):
            out[tsc] = []
    return out


def save_rules(tsc, text, author, message):
    R.parse_rules(text, tsc)                      # refuse to commit unreadable rules
    with open(rules_file(tsc), "w") as f:
        f.write(text)
    git("add", f"I{tsc:02d}.rules")
    if not git("status", "--porcelain"):
        return git("rev-parse", "--short=12", "HEAD").strip(), False
    git("commit", "-q", "-m", message, author=author)
    return git("rev-parse", "--short=12", "HEAD").strip(), True


def history(tsc=None, n=40):
    args = ["log", f"-n{n}", "--date=iso-strict", "--pretty=format:%h%x1f%an%x1f%ad%x1f%s%x1f%D"]
    if tsc:
        args += ["--", f"I{tsc:02d}.rules"]
    rows = []
    for line in git(*args).splitlines():
        h, a, d, s, refs = line.split("\x1f")
        rows.append(dict(commit=h, author=a, date=d, message=s, refs=refs))
    return rows


def diff(a, b="HEAD", tsc=None):
    args = ["diff", a, b] if b else ["diff", a]
    if tsc:
        args += ["--", f"I{tsc:02d}.rules"]
    return git(*args)


def signing_key():
    with open(HSM_KEY, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def releases():
    out = []
    if not os.path.isdir(RELEASES):
        return out
    for d in sorted(os.listdir(RELEASES), key=lambda s: int(s[1:]) if s[1:].isdigit() else 0):
        p = os.path.join(RELEASES, d, "manifest.json")
        if os.path.exists(p):
            m = json.load(open(p))
            m.pop("rules", None)
            out.append(m)
    return out


def manifest(version):
    p = os.path.join(RELEASES, f"v{version}", "manifest.json")
    return json.load(open(p)) if os.path.exists(p) else None


def config_blob(version, tsc):
    return open(os.path.join(RELEASES, f"v{version}", f"tsc{tsc:02d}.cfg"), "rb").read()


def make_release(author, approver, note=""):
    if not approver or approver == author:
        raise ValueError("two-person rule: the approver must be a different engineer than the author")
    all_rules = load_all()
    ok, report = R.validate(all_rules)
    if not ok:
        raise ValueError("validation failed: fix the rules before releasing")
    if git("status", "--porcelain"):
        raise ValueError("working copy has uncommitted changes")
    commit = git("rev-parse", "--short=12", "HEAD").strip()
    prev = releases()
    version = (prev[-1]["version"] + 1) if prev else 1
    tag = f"rel-v{version}"
    key = signing_key()
    d = os.path.join(RELEASES, f"v{version}")
    os.makedirs(d, exist_ok=True)
    created = time.time_ns()
    files, prev_hashes = {}, {}
    if prev:
        pm = manifest(prev[-1]["version"])
        prev_hashes = {int(k): v["rules_sha"] for k, v in pm["files"].items()}
    import hashlib
    for tsc, rules in all_rules.items():
        blob = R.compile_config(tsc, rules, version, tag, commit, created)
        blob += key.sign(blob)                          # Ed25519, 64 bytes
        with open(os.path.join(d, f"tsc{tsc:02d}.cfg"), "wb") as f:
            f.write(blob)
        rules_sha = hashlib.sha256(R.format_rules(rules, tsc).encode()).hexdigest()[:16]
        files[tsc] = dict(bytes=len(blob), sha256=hashlib.sha256(blob).hexdigest()[:16], rules_sha=rules_sha,
                          changed=prev_hashes.get(tsc) != rules_sha, n_rules=len([r for r in rules if r.enabled]))
    m = dict(version=version, tag=tag, git_commit=commit, author=author, approver=approver, note=note,
             created_ns=created, created_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created / 1e9)),
             validation_ok=True, files=files,
             changed=[t for t, f in files.items() if f["changed"]],
             rules={t: [r.to_dict() for r in rs] for t, rs in all_rules.items()})
    json.dump(m, open(os.path.join(d, "manifest.json"), "w"), indent=1)
    git("tag", "-a", tag, "-m", f"Release v{version} approved by {approver}: {note}", author=approver)
    return m


def activation_command(tsc, op, version, at_ns):
    """op 1 = activate staged slot, 2 = rollback to the other slot. Signed like configurations."""
    body = struct.pack(R.ACT_FMT, b"TSCA", tsc, op, 0, version, at_ns)
    return body + signing_key().sign(body)


def new_signing_key(pub_raw_path):
    k = Ed25519PrivateKey.generate()
    os.makedirs(os.path.dirname(HSM_KEY), exist_ok=True)
    with open(HSM_KEY, "wb") as f:
        f.write(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()))
    os.chmod(HSM_KEY, 0o600)
    with open(pub_raw_path, "wb") as f:
        f.write(k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
