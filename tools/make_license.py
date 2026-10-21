"""Vendor-side licence tool (never shipped).

  python tools/make_license.py init                      create .keys/lavora_ed25519.pem and print the public key
  python tools/make_license.py issue --name "Jane" [--exp 2027-12-31] [--tier pro]
"""
import argparse
import base64
import os
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from lavora.core.license import make_key, verify_key  # noqa: E402

KEY_FILE = os.path.join(ROOT, ".keys", "lavora_ed25519.pem")


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    iss = sub.add_parser("issue")
    iss.add_argument("--name", required=True)
    iss.add_argument("--exp")
    iss.add_argument("--tier", default="pro")
    a = ap.parse_args()
    if a.cmd == "init":
        if os.path.exists(KEY_FILE):
            print("key already exists:", KEY_FILE)
            return 1
        os.makedirs(os.path.dirname(KEY_FILE), exist_ok=True)
        k = Ed25519PrivateKey.generate()
        with open(KEY_FILE, "wb") as f:
            f.write(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
        pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        print("PUBLIC_KEY_B64 =", base64.b64encode(pub).decode())
        return 0
    with open(KEY_FILE, "rb") as f:
        k = serialization.load_pem_private_key(f.read(), None)
    payload = {"v": 1, "tier": a.tier, "name": a.name}
    if a.exp:
        payload["exp"] = a.exp
    key = make_key(k, payload)
    assert verify_key(key).valid, "generated key does not verify against the embedded public key"
    print(key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
