#!/usr/bin/env python3
"""One-time: enroll a Duo software token and save its HOTP secret.

Usage:
    duo_enroll.py "<activation_code>-<base64-host>"

The argument is the raw payload encoded in a Duo "add device" activation QR
(do NOT scan it with the phone — feed it here instead). We POST to Duo's
activation endpoint and store the returned hotp_secret (base32) so auto_login.py
can generate Duo passcodes unattended.

Writes $CANVAS_CHECK_HOME/.duo_totp (chmod 600) = {"secret": "<base32>", "counter": 0}.
"""
import base64
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

OUT = Path(os.environ.get("CANVAS_CHECK_HOME", Path.home() / "canvas-check")) / ".duo_totp"


def b64pad(s):
    return s + "=" * (-len(s) % 4)


def parse_payload(payload):
    """Return (code, api_host) from either the raw QR content '<code>-<b64host>' or a
    Duo activation link like https://m-XXXX.duosecurity.com/activate/<code>."""
    payload = payload.strip()
    if payload.startswith("http"):
        u = urllib.parse.urlparse(payload)
        code = u.path.rstrip("/").split("/")[-1]
        host = u.netloc
        if host.startswith("m-"):
            host = "api-" + host[2:]  # activation link host → API host
        return code, host
    if "-" not in payload:
        raise SystemExit("BAD_PAYLOAD expected '<code>-<base64host>' or an activation URL")
    code, b64host = payload.split("-", 1)
    host = base64.b64decode(b64pad(b64host)).decode()
    return code, host


def activate(payload):
    code, host = parse_payload(payload)
    if not host.endswith("duosecurity.com"):
        raise SystemExit(f"BAD_HOST decoded host = {host!r}")

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()

    params = {
        "pkpush": "rsa-sha512", "pubkey": pub,
        "jailbroken": "false", "architecture": "arm64", "region": "US",
        "app_id": "com.duosecurity.duomobile", "full_disk_encryption": "true",
        "passcode_status": "true", "platform": "Android", "app_version": "4.59.0",
        "app_build_number": "459010", "version": "13", "manufacturer": "Google",
        "model": "Pixel", "security_patch_level": "2024-01-01",
    }
    url = f"https://{host}/push/v2/activation/{code}?customer_protocol=1"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "okhttp/2.7.5"})
    resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
    if resp.get("stat") != "OK":
        raise SystemExit(f"ACTIVATION_FAILED {resp}")
    secret = resp["response"]["hotp_secret"]
    b32 = base64.b32encode(secret.encode("utf-8")).decode("utf-8")
    OUT.write_text(json.dumps({"secret": b32, "counter": 0}))
    os.chmod(OUT, 0o600)
    print("OK enrolled →", OUT)
    print("pkey:", resp["response"].get("pkey", "")[:12], "...")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit('usage: duo_enroll.py "<code>-<base64host>"')
    activate(sys.argv[1])
