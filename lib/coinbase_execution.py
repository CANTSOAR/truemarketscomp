from __future__ import annotations

import base64
import json
import logging
import secrets
import time
import uuid
from pathlib import Path

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.asymmetric.ec import ECDSA
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature


class CoinbaseExecutionClient:
    """
    Taker-only client for Coinbase's Advanced Trade API (the regular
    coinbase.com retail product, via a Coinbase Developer Platform / CDP API
    key) — the hedge leg for Book A.

    Auth mirrors execution.py's pattern for TrueMarkets: a per-request JWT
    signed by the CDP key, rather than HMAC. Get a key file at
    https://portal.cdp.coinbase.com/projects/api-keys -> "Create API key" ->
    grant View + Trade only (never Transfer) -> download the JSON the moment
    it's shown (Coinbase displays the private key exactly once). Two file
    shapes are supported, since the portal's default changed over time:
      * current default — Ed25519: {"id": "<uuid>", "privateKey":
        "<base64, 64 bytes: 32-byte seed + 32-byte pubkey>"}, signed EdDSA.
      * legacy — EC: {"name": "organizations/.../apiKeys/...", "privateKey":
        "-----BEGIN EC PRIVATE KEY-----..."}, signed ES256.

    Runs in DRY-RUN mode (no network call, simulated immediate fill at the
    Book A reference price) whenever no key_file is given/found — this lets
    the cross-market engine run end-to-end against real Book A market data
    and real TrueMarkets orders while never risking an order on an account
    this process has no credentials for.
    """

    def __init__(
        self,
        key_file: str | None = None,
        base_url: str = "https://api.coinbase.com",
    ):
        self.base_url = base_url.rstrip("/")
        self._host = self.base_url.split("//", 1)[-1]
        self._key_name: str | None = None
        self._key = None
        self._alg: str | None = None
        if key_file and Path(key_file).expanduser().exists():
            self._key_name, self._key, self._alg = self._load_key(key_file)
        self.dry_run = self._key is None
        if self.dry_run:
            logging.warning(
                "CoinbaseExecutionClient: no key file found — running in "
                "DRY-RUN mode (hedge orders are simulated, not sent)."
            )

    @staticmethod
    def _load_key(path: str):
        bundle = json.loads(Path(path).expanduser().read_text())
        raw_key = bundle["privateKey"]
        if "name" in bundle:
            key_id = bundle["name"]
            private_key = serialization.load_pem_private_key(raw_key.encode(), password=None)
            return key_id, private_key, "ES256"
        # current CDP default: id + base64(32-byte seed || 32-byte pubkey)
        key_id = bundle["id"]
        seed = base64.b64decode(raw_key)[:32]
        private_key = ed25519.Ed25519PrivateKey.from_private_bytes(seed)
        return key_id, private_key, "EdDSA"

    @staticmethod
    def _b64u(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    def _build_jwt(self, method: str, path: str) -> str:
        """CDP per-request JWT, bound to method+host+path, 120s expiry."""
        now = int(time.time())
        header = {"alg": self._alg, "kid": self._key_name, "nonce": secrets.token_hex(16), "typ": "JWT"}
        payload = {
            "sub": self._key_name,
            "iss": "cdp",
            "nbf": now,
            "exp": now + 120,
            "uri": f"{method} {self._host}{path}",
        }
        signing_input = (
            self._b64u(json.dumps(header, separators=(",", ":")).encode())
            + "."
            + self._b64u(json.dumps(payload, separators=(",", ":")).encode())
        )
        if self._alg == "EdDSA":
            sig = self._key.sign(signing_input.encode())
        else:
            der = self._key.sign(signing_input.encode(), ECDSA(hashes.SHA256()))
            r, s = decode_dss_signature(der)
            sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return f"{signing_input}.{self._b64u(sig)}"

    def _headers(self, method: str, path: str) -> dict:
        return {
            "Authorization": f"Bearer {self._build_jwt(method, path)}",
            "Content-Type": "application/json",
        }

    async def place_order(
        self,
        session: aiohttp.ClientSession,
        product_id: str,
        side: str,
        size: str,
        ref_price: float | None = None,
    ) -> dict | None:
        """Place a market taker order sized in base currency (e.g. BTC). In
        dry-run, simulate an immediate fill at `ref_price` — the Book A
        quote observed at decision time."""
        if self.dry_run:
            logging.info(
                f"[DRY-RUN] Coinbase {side.upper()} {size} {product_id} @ ~{ref_price} (simulated)"
            )
            return {
                "order_id": f"dryrun-{int(time.time() * 1000)}",
                "status": "done",
                "side": side,
                "size": size,
                "product_id": product_id,
                "price": ref_price,
                "dry_run": True,
            }

        path = "/api/v3/brokerage/orders"
        body = json.dumps({
            "client_order_id": str(uuid.uuid4()),
            "product_id": product_id,
            "side": side.upper(),
            "order_configuration": {"market_market_ioc": {"base_size": size}},
        })
        async with session.post(
            f"{self.base_url}{path}", data=body, headers=self._headers("POST", path)
        ) as resp:
            raw = await resp.text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {"_raw": raw}
            if resp.status != 200 or data.get("success") is False:
                logging.error(f"Coinbase POST {path} -> {resp.status}: {data}")
                return None
            return data

    async def get_balances(self, session: aiohttp.ClientSession) -> dict[str, float]:
        if self.dry_run:
            return {}
        path = "/api/v3/brokerage/accounts"
        async with session.get(
            f"{self.base_url}{path}", headers=self._headers("GET", path)
        ) as resp:
            raw = await resp.text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                return {}
            if resp.status != 200:
                logging.error(f"Coinbase GET {path} -> {resp.status}: {data}")
                return {}
            return {
                a["currency"]: float(a["available_balance"]["value"])
                for a in data.get("accounts", [])
            }
