"""
portfolio.py  —  minimal standalone portfolio printer for True Markets.

No project imports. Signs an ES256 auth challenge with the API key, mints a
JWT, then fetches /v1/conductor/balances.

Usage:  python3 portfolio.py
"""

from __future__ import annotations

import os
import json
import time
import base64
import urllib.request

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

UA = "Mozilla/5.0"  # default urllib UA gets 403'd by the edge/WAF
BASE_URL = os.getenv("BASE_REST_URL", "https://api.truemarkets.co")
KEY_FILE = os.getenv("BOT_A_KEY_FILE", "./keys/truemarkets-api-key-edd1691b.json")


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def post(url: str, body: dict, token: str | None = None) -> dict:
    headers = {"Content-Type": "application/json", "User-Agent": UA}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def get(url: str, token: str) -> dict:
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", "User-Agent": UA}
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def main():
    bundle = json.loads(open(os.path.expanduser(KEY_FILE)).read())
    key_id = bundle["key_id"]
    d = int.from_bytes(b64u_decode(bundle["private_key"]["d"]), "big")
    key = ec.derive_private_key(d, ec.SECP256R1())

    # 1. sign "{key_id}.{timestamp}" → raw r||s → base64url
    ts = int(time.time())
    der = key.sign(f"{key_id}.{ts}".encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    signature = b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))

    # 2. mint JWT
    auth = post(
        f"{BASE_URL}/v1/auth/api-key/token",
        {"key_id": key_id, "timestamp": ts, "signature": signature},
    )
    token = auth["access_token"]

    # 3. fetch balances
    # ListBalancesResponse = {"data": [BalanceItem]}, not {"balances": [...]}.
    # BalanceItem has symbol/name/total/available/held, not asset_name/balance.
    data = get(f"{BASE_URL}/v1/conductor/balances/unified", token)

    print(f"{'ASSET':<8}{'AVAILABLE':>20}{'HELD':>20}{'TOTAL':>20}")
    print("-" * 68)
    unified = {}
    for b in data.get("data", []):
        asset = b.get("symbol") or b.get("name", "?")
        if asset not in unified:
            unified[asset] = 0
        unified[asset] += float(b.get('available', 0))

    for asset in unified:
        print(f"{asset:<8}{float(unified[asset]):>20.8f}")


if __name__ == "__main__":
    main()
