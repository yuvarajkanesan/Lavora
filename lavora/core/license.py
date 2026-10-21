"""Offline-verifiable licence keys (the hook; there is no payment system yet).

A key is  LAVORA-<base32 payload>.<base32 Ed25519 signature>  where the payload is JSON such as
{"v":1,"tier":"pro","name":"Jane","exp":"2027-12-31"}. The vendor signs with a private key that never ships; the app
only holds the public key, so a key can be checked with no network. tools/make_license.py creates keys.
"""
from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from datetime import date

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .paths import data_dir

# Public half of the Lavati signing key. TODO: replace with the production key before a public release.
PUBLIC_KEY_B64 = "R/C4zAF4wN/DoWs/oqHJVZJa9zq9FP3b91MYwb1FnY4="

FREE_RECOVERY_LIMIT = 256 * 1024 * 1024  # bytes per recovery run on the Free tier
PRO_FEATURES = {"imaging", "backup", "doctor", "unlimited-recovery"}


class LicenseRequired(Exception):
    def __init__(self, feature: str):
        super().__init__(f"'{feature}' is a Lavora Pro feature. Enter a licence key (lavora license install <key>).")
        self.feature = feature


@dataclass
class License:
    tier: str = "free"
    name: str = ""
    expires: date | None = None
    valid: bool = False
    reason: str = ""

    @property
    def is_pro(self) -> bool:
        return self.valid and self.tier == "pro" and (self.expires is None or self.expires >= date.today())

    def require(self, feature: str) -> None:
        if feature in PRO_FEATURES and not self.is_pro:
            raise LicenseRequired(feature)

    def recovery_limit(self) -> int | None:
        return None if self.is_pro else FREE_RECOVERY_LIMIT


def _b32d(s: str) -> bytes:
    return base64.b32decode(s + "=" * (-len(s) % 8))


def _b32e(b: bytes) -> str:
    return base64.b32encode(b).decode().rstrip("=")


def verify_key(key: str, public_key_b64: str = PUBLIC_KEY_B64) -> License:
    try:
        body = key.strip()
        if not body.startswith("LAVORA-") or "." not in body:
            return License(reason="not a Lavora key")
        payload_b32, sig_b32 = body[len("LAVORA-"):].split(".", 1)
        payload, sig = _b32d(payload_b32), _b32d(sig_b32)
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64)).verify(sig, payload)
        data = json.loads(payload)
    except (InvalidSignature, ValueError, KeyError, TypeError):
        return License(reason="signature check failed")
    exp = date.fromisoformat(data["exp"]) if data.get("exp") else None
    lic = License(data.get("tier", "free"), data.get("name", ""), exp, True)
    if exp and exp < date.today():
        lic.reason = "expired"
    return lic


def make_key(private_key, payload: dict) -> str:
    """Vendor side: sign `payload` with an Ed25519 private key object."""
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return f"LAVORA-{_b32e(raw)}.{_b32e(private_key.sign(raw))}"


def key_path() -> str:
    return os.path.join(data_dir(), "license.key")


def install_key(key: str) -> License:
    lic = verify_key(key)
    if not lic.valid:
        raise ValueError(f"licence rejected: {lic.reason}")
    with open(key_path(), "w", encoding="utf-8") as f:
        f.write(key.strip())
    return lic


def current() -> License:
    try:
        with open(key_path(), encoding="utf-8") as f:
            return verify_key(f.read())
    except OSError:
        return License(reason="no licence installed")
