"""
diag_order.py  —  place ONE test limit order and print the full venue response
                  to discover WHY the maker's orders are failing.

⚠️  THIS PLACES A REAL ORDER on your live account. It uses a resting price
    (far from mid) so it should NOT immediately execute, and it cancels the
    order at the end. Run it deliberately.

Usage:
    python3 diag_order.py            # default: sell 0.0002 BTC (tests min-size)
    python3 diag_order.py buy 0.0002
"""

import os
import sys
import json
import time
import base64
import urllib.request
import urllib.error
from pathlib import Path

from dotenv import load_dotenv
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / "keys" / ".env")
UA = "Mozilla/5.0"
BASE = os.getenv("BASE_REST_URL", "https://api.truemarkets.co")
KEY_FILE = os.getenv("BOT_A_KEY_FILE", "./keys/truemarkets-api-key-edd1691b.json")

SIDE = sys.argv[1] if len(sys.argv) > 1 else "sell"
QTY = sys.argv[2] if len(sys.argv) > 2 else "0.0002"


def req(method, path, body=None, tok=None):
    h = {"Content-Type": "application/json", "User-Agent": UA}
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    data = json.dumps(body).encode() if body is not None else None
    rq = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(rq) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"_raw": raw.decode(errors="replace")}


def main():
    bundle = json.loads(open(os.path.expanduser(KEY_FILE)).read())
    kid = bundle["key_id"]
    d = int.from_bytes(base64.urlsafe_b64decode(bundle["private_key"]["d"] + "=="), "big")
    key = ec.derive_private_key(d, ec.SECP256R1())
    ts = int(time.time())
    der = key.sign(f"{kid}.{ts}".encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    sig = base64.urlsafe_b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).rstrip(b"=").decode()
    tok = req("POST", "/v1/auth/api-key/token",
              {"key_id": kid, "timestamp": ts, "signature": sig})[1]["access_token"]

    # current mid, then a resting price 1% away so it shouldn't fill.
    # side="buy" returns the price we'd pay (effective ask), not mid — average
    # both sides to get an actual mid, per QuoteResponse semantics.
    _, sell_q = req("POST", "/v1/conductor/quotes",
                    {"base_asset": "BTC", "quote_asset": "USDC", "qty": QTY,
                     "qty_unit": "base", "side": "sell"}, tok)
    _, buy_q = req("POST", "/v1/conductor/quotes",
                   {"base_asset": "BTC", "quote_asset": "USDC", "qty": QTY,
                    "qty_unit": "base", "side": "buy"}, tok)
    mid = (float(sell_q["price"]) + float(buy_q["price"])) / 2
    px = round(mid * (1.01 if SIDE == "sell" else 0.99), 2)
    print(f"mid≈{mid}  →  resting {SIDE.upper()} {QTY} BTC @ {px}\n")

    code, res = req("POST", "/v1/conductor/orders",
                    {"base_asset": "BTC", "quote_asset": "USDC", "qty": QTY,
                     "qty_unit": "base", "type": "limit", "side": SIDE,
                     "price": str(px)}, tok)
    print(f"CREATE  HTTP {code}\n{json.dumps(res, indent=2)}\n")

    oid = res.get("order_id")
    if oid:
        # GET /orders/{id} (no suffix) doesn't exist in the spec — only
        # /orders/{id}/status (GetOrderStatusResponse = {"status": ...}).
        for _ in range(5):
            time.sleep(2)
            _, detail = req("GET", f"/v1/conductor/orders/{oid}/status", tok=tok)
            print(f"  status={detail.get('status')}  detail={json.dumps(detail)}")
            if detail.get("status") in ("complete", "canceled", "failed"):
                break
        print("\nCancelling test order...")
        print("DELETE", req("DELETE", f"/v1/conductor/orders/{oid}", tok=tok))


if __name__ == "__main__":
    main()
