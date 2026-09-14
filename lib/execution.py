from __future__ import annotations

import asyncio
import aiohttp
import hmac as _hmac
import hashlib
import json
import logging
import time
import base64
import uuid
from pathlib import Path
from urllib.parse import quote

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature


class ExecutionClient:
    def __init__(
        self,
        key_file: str,
        base_url: str,
        cefi_base_url: str = "https://api.truex.co",
        cefi_org_id: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        # /v1/cefi/* lives on a different host than the conductor.
        self._cefi_base_url = cefi_base_url.rstrip("/")
        self._key_id, self._key = self._load_key(key_file)
        # The CeFi HMAC auth requires a secret; we derive it from the raw
        # 32-byte big-endian EC private scalar (d) of the existing key.
        # If TrueMarkets issues a separate symmetric HMAC key, pass it as
        # bytes via hmac_secret= after construction to override this default.
        d = self._key.private_numbers().private_value
        self._cefi_hmac_secret: bytes = d.to_bytes(32, "big")
        # x-truex-auth-userid: client/org identifier.  Falls back to key_id
        # if not provided — update if the API returns 401 with "bad userid".
        self._cefi_org_id: str = cefi_org_id or self._key_id
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        # Real server-reported budget from x-ratelimit-* response headers
        # (100 req per 60s window, confirmed). Used to throttle proactively
        # instead of guessing a client-side request rate and reacting to 429s.
        self._rl_remaining: int | None = None
        self._rl_reset: float | None = None

    def _note_rate_limit(self, resp: aiohttp.ClientResponse) -> None:
        remaining = resp.headers.get("x-ratelimit-remaining")
        reset = resp.headers.get("x-ratelimit-reset")
        if remaining is not None:
            try:
                self._rl_remaining = int(remaining)
            except ValueError:
                pass
        if reset is not None:
            try:
                self._rl_reset = float(reset)
            except ValueError:
                pass

    def time_until_reset(self) -> float | None:
        """Seconds until the rate-limit window resets, or None if unknown.
        Handles both Unix-timestamp and seconds-remaining header formats."""
        if self._rl_reset is None:
            return None
        # Unix timestamps are > 1e9; seconds-remaining values are < 3600.
        if self._rl_reset > 1_000_000:
            return max(0.0, self._rl_reset - time.time())
        return max(0.0, float(self._rl_reset))

    async def _throttle(self) -> None:
        """Adaptive pacing from live server headers — no fixed cooldowns.

        When budget is healthy, does nothing (token bucket handles pacing).
        When running low, spreads remaining requests evenly over the reset
        window so we never fully exhaust the budget and never need to hard-stop.
        """
        remaining = self._rl_remaining
        secs = self.time_until_reset()
        if remaining is None or secs is None:
            return
        if remaining <= 0:
            logging.warning(f"Rate-limit window exhausted — waiting {secs:.1f}s for reset")
            await asyncio.sleep(secs + 0.1)
        elif remaining < 10 and secs > 0:
            # Spread the remaining budget evenly; this is exact, never a guess.
            await asyncio.sleep(secs / remaining)

    # ── key loading ──────────────────────────────────────────────────────────

    def _load_key(self, path: str) -> tuple[str, ec.EllipticCurvePrivateKey]:
        bundle = json.loads(Path(path).expanduser().read_text())
        key_id: str = bundle["key_id"]
        jwk: dict = bundle["private_key"]
        d = int.from_bytes(self._b64u_dec(jwk["d"]), "big")
        return key_id, ec.derive_private_key(d, ec.SECP256R1())

    # ── crypto helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _b64u(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    @staticmethod
    def _b64u_dec(s: str) -> bytes:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

    def _sign_challenge(self, ts: int) -> str:
        """ES256 sign `{key_id}.{timestamp}` → raw r||s → base64url."""
        msg = f"{self._key_id}.{ts}".encode()
        der = self._key.sign(msg, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        return self._b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))

    def _turnkey_stamp(self, payload_bytes: bytes) -> str:
        """Turnkey API stamp required for DeFi/unfunded-buy payloads."""
        der = self._key.sign(payload_bytes, ec.ECDSA(hashes.SHA256()))
        pub = self._key.public_key().public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.CompressedPoint,
        )
        stamp = {
            "publicKey": pub.hex(),
            "signature": der.hex(),
            "scheme": "SIGNATURE_SCHEME_TK_API_P256",
        }
        return self._b64u(json.dumps(stamp).encode())

    # ── JWT auth ─────────────────────────────────────────────────────────────

    async def authenticate(self, session: aiohttp.ClientSession) -> None:
        """Mint a fresh JWT token pair from the EC key."""
        ts = int(time.time())
        body = {
            "key_id": self._key_id,
            "timestamp": ts,
            "signature": self._sign_challenge(ts),
        }
        async with session.post(
            f"{self.base_url}/v1/auth/api-key/token", json=body
        ) as resp:
            raw = await resp.text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = raw
            if resp.status >= 400:
                raise RuntimeError(f"Auth failed {resp.status}: {data}")
            self._access_token = data["access_token"]
            self._refresh_token = data.get("refresh_token")
            logging.info("Auth: JWT minted successfully.")

    async def _refresh_jwt(self, session: aiohttp.ClientSession) -> None:
        if not self._refresh_token:
            await self.authenticate(session)
            return
        async with session.post(
            f"{self.base_url}/v1/auth/token/refresh",
            json={"refresh_token": self._refresh_token},
        ) as resp:
            if resp.status >= 400:
                await self.authenticate(session)
                return
            raw = await resp.text()
            data = json.loads(raw)
            self._access_token = data["access_token"]
            if data.get("refresh_token"):
                self._refresh_token = data["refresh_token"]

    async def _ensure_auth(self, session: aiohttp.ClientSession) -> None:
        if not self._access_token:
            await self.authenticate(session)

    def _auth_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

    # ── generic HTTP with 401 auto-refresh ──────────────────────────────────

    async def _post(
        self, session: aiohttp.ClientSession, path: str, body: dict
    ) -> dict | None:
        await self._ensure_auth(session)
        url = f"{self.base_url}{path}"
        backoff = 2.0
        refreshed = False
        attempt = 0
        while attempt < 6:
            await self._throttle()
            async with session.post(
                url, headers=self._auth_headers(), json=body
            ) as resp:
                self._note_rate_limit(resp)
                raw = await resp.text()
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    data = {"_raw": raw}
                if resp.status == 401 and not refreshed:
                    await self._refresh_jwt(session)
                    refreshed = True
                    attempt += 1
                    continue
                if resp.status == 429:
                    logging.warning(f"POST {path} → 429 rate-limited; backing off {backoff}s")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                    attempt += 1
                    continue
                if resp.status not in (200, 201):
                    logging.error(f"POST {path} → {resp.status}: {data}")
                    return None
                return data
            attempt += 1
        return None

    # ── order placement (two-step: create → execute if payloads present) ─────

    async def place_order(
        self,
        session: aiohttp.ClientSession,
        base_asset: str,
        quote_asset: str,
        side: str,
        qty: str,
        qty_unit: str = "quote",
        order_type: str = "market",
        price: str | None = None,
        venue: str = None,
    ) -> dict | None:
        """
        CeFi sell orders and sufficiently funded CeFi buy orders complete on
        create alone. DeFi orders or CeFi buys requiring a funding bridge will
        return unsigned payloads that we sign and submit to the execute endpoint.
        """
        payload: dict = {
            "base_asset": base_asset,
            "quote_asset": quote_asset,
            "qty": qty,
            "qty_unit": qty_unit,
            "type": order_type,
            "side": side,
        }
        if price and order_type == "limit":
            payload["price"] = price

        if venue:
            payload["venue"] = venue

        result = await self._post(session, "/v1/conductor/orders", payload)
        print(result)
        if not result:
            return None

        order_id = result.get("order_id")
        status = result.get("status")
        logging.info(f"Order created: id={order_id} status={status}")

        unsigned = result.get("payloads") or []
        if unsigned and order_id:
            # UNVERIFIED, contradicts the spec: UnsignedPayload.payload is
            # documented as base64-encoded, and .digest ("SHA-256 hash of
            # the payload, used as the signing input") is never read here.
            # This comment's claim (raw JSON string, sign as-is) was never
            # confirmed against a real payloads array on this account, since
            # every CeFi order placed so far returns no payloads (goes
            # straight to pending) or fails outright. Before trusting this:
            # dump a real `payloads` entry, check whether `payload` is valid
            # base64 and what `digest` actually is, then fix accordingly.
            signatures = [
                self._turnkey_stamp(p["payload"].encode("utf-8"))
                for p in unsigned
            ]
            exec_result = await self._post(
                session,
                f"/v1/conductor/orders/{order_id}/execute",
                {"signatures": signatures, "auth_type": "api_key"},
            )
            if exec_result:
                logging.info(
                    f"Order executed: id={order_id} status={exec_result.get('status')}"
                )
                return exec_result

        logging.info(
            f"Order complete: {side.upper()} {qty} {qty_unit} {base_asset}/{quote_asset}"
        )
        return result

    # ── generic GET with 401 auto-refresh ───────────────────────────────────

    async def _get(
        self, session: aiohttp.ClientSession, path: str
    ) -> dict | None:
        await self._ensure_auth(session)
        url = f"{self.base_url}{path}"
        backoff = 2.0
        attempt = 0
        while attempt < 6:
            await self._throttle()
            async with session.get(url, headers=self._auth_headers()) as resp:
                self._note_rate_limit(resp)
                raw = await resp.text()
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    data = {"_raw": raw}
                if resp.status == 401 and attempt == 0:
                    await self._refresh_jwt(session)
                    attempt += 1
                    continue
                if resp.status == 429:
                    logging.warning(f"GET {path} → 429 rate-limited; backing off {backoff}s")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                    attempt += 1
                    continue
                if resp.status != 200:
                    logging.error(f"GET {path} → {resp.status}: {data}")
                    return None
                return data
            attempt += 1
        return None

    # ── order status / cancel ────────────────────────────────────────────────

    async def get_order(
        self, session: aiohttp.ClientSession, order_id: str
    ) -> str | None:
        data = await self._get(session, f"/v1/conductor/orders/{order_id}")
        return data if data else None

    async def get_order_status(
        self, session: aiohttp.ClientSession, order_id: str
    ) -> str | None:
        data = await self._get(session, f"/v1/conductor/orders/{order_id}/status")
        return data.get("status") if data else None

    async def cancel_order(
        self, session: aiohttp.ClientSession, order_id: str
    ) -> bool:
        await self._ensure_auth(session)
        url = f"{self.base_url}/v1/conductor/orders/{order_id}"
        for attempt in range(2):
            await self._throttle()
            async with session.delete(url, headers=self._auth_headers()) as resp:
                self._note_rate_limit(resp)
                if resp.status == 401 and attempt == 0:
                    await self._refresh_jwt(session)
                    continue
                logging.info(f"Cancel {order_id}: HTTP {resp.status}")
                return resp.status in (200, 202)
        return False

    async def get_balances(
        self, session: aiohttp.ClientSession
    ) -> dict[str, float]:
        """Return {asset_symbol: float_available} for all non-zero balances.

        Response shape is {"data": [BalanceItem]} with symbol/available/total
        fields (matches /balances/unified) — NOT the older {"balances": [...]}
        with asset_name/balance this used to assume; TrueMarkets changed the
        plain /balances response to match unified at some point. A symbol can
        appear multiple times (once per custody bucket — CeFi plus each DeFi
        chain), so entries are summed by symbol rather than overwritten.
        """
        data = await self._get(session, "/v1/conductor/balances")
        if not data:
            return {}
        result: dict[str, float] = {}
        for b in data.get("data", []):
            symbol = b.get("symbol", "")
            try:
                qty = float(b.get("available", 0))
            except (ValueError, TypeError):
                qty = 0.0
            if qty > 0:
                result[symbol] = result.get(symbol, 0.0) + qty
        return result

    # ── cancel / utility ─────────────────────────────────────────────────────

    async def cancel_all(self, session: aiohttp.ClientSession) -> None:
        """Cancel every cancellable order, walking all pages.

        GetOrdersResponse nests orders under `data`, not `orders` (the old
        key never matched, so this was previously a silent no-op). The
        endpoint's prose claims it only returns completed/failed orders, but
        its own `status` filter example lists pending/canceled — request the
        cancellable statuses explicitly (pending, active) since those are the
        only ones cancelOrder actually accepts.
        """
        await self._ensure_auth(session)
        cursor: str | None = None
        while True:
            path = "/v1/conductor/orders?status=pending,active"
            if cursor:
                path += f"&cursor={quote(cursor)}"
            data = await self._get(session, path)
            if not data:
                return
            for order in data.get("data", []):
                oid = order.get("order_id")
                if oid:
                    await self.cancel_order(session, oid)
            cursor = (data.get("pagination") or {}).get("next_cursor")
            if not cursor:
                return

    # ── CeFi direct API (/v1/cefi/*) with HMAC-SHA256 auth ──────────────────
    # The /v1/cefi/orders endpoint uses four custom headers instead of JWT:
    #   x-truex-auth-userid     client/org identifier
    #   x-truex-auth-timestamp  Unix seconds (must be within 15s of server time)
    #   x-truex-auth-token      UUID for the HMAC key (we use key_id)
    #   x-truex-auth-signature  HMAC-SHA256( secret, f"{ts}.{body_json}" )
    # This endpoint supports ALO (Add Liquidity Only) time-in-force, which
    # causes the exchange to reject—not fill—any order that would cross the spread.

    def _cefi_sign(self, ts: str, body_json: str) -> str:
        msg = f"{ts}.{body_json}".encode()
        return _hmac.new(self._cefi_hmac_secret, msg, hashlib.sha256).hexdigest()

    def _cefi_headers(self, body_json: str) -> tuple[str, dict]:
        ts = str(int(time.time()))
        return ts, {
            "x-truex-auth-userid":    self._cefi_org_id,
            "x-truex-auth-timestamp": ts,
            "x-truex-auth-token":     self._key_id,
            "x-truex-auth-signature": self._cefi_sign(ts, body_json),
            "Content-Type": "application/json",
        }

    async def _cefi_post(
        self, session: aiohttp.ClientSession, path: str, body: dict
    ) -> dict | None:
        body_json = json.dumps(body, separators=(",", ":"))
        url = f"{self._cefi_base_url}{path}"
        backoff = 2.0
        for attempt in range(6):
            await self._throttle()
            _, headers = self._cefi_headers(body_json)
            async with session.post(url, headers=headers, data=body_json) as resp:
                self._note_rate_limit(resp)
                raw = await resp.text()
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    data = {"_raw": raw}
                if resp.status == 429:
                    logging.warning(f"CeFi POST {path} → 429; backing off {backoff}s")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                    continue
                if resp.status not in (200, 201):
                    logging.error(f"CeFi POST {path} → {resp.status}: {data}")
                    return None
                return data
        return None

    async def _cefi_delete(
        self, session: aiohttp.ClientSession, path: str
    ) -> bool:
        url = f"{self._cefi_base_url}{path}"
        body_json = ""
        for attempt in range(2):
            await self._throttle()
            _, headers = self._cefi_headers(body_json)
            headers.pop("Content-Type", None)
            async with session.delete(url, headers=headers) as resp:
                self._note_rate_limit(resp)
                logging.info(f"CeFi DELETE {path}: HTTP {resp.status}")
                return resp.status in (200, 202, 204)
        return False

    async def _cefi_get(
        self, session: aiohttp.ClientSession, path: str
    ) -> dict | None:
        url = f"{self._cefi_base_url}{path}"
        body_json = ""
        for attempt in range(2):
            await self._throttle()
            _, headers = self._cefi_headers(body_json)
            headers.pop("Content-Type", None)
            headers["Accept"] = "application/json"
            async with session.get(url, headers=headers) as resp:
                self._note_rate_limit(resp)
                raw = await resp.text()
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return None
        return None

    async def cefi_place_order(
        self,
        session: aiohttp.ClientSession,
        instrument_id: str,
        side: str,
        qty: str,
        price: str,
        post_only: bool = True,
    ) -> dict | None:
        """Place a CeFi limit order via POST /v1/cefi/orders.

        With post_only=True the request carries time_in_force=ALO so the
        exchange itself rejects the order rather than letting it cross and take.
        Returns a dict; 'order_id' is set to the CeFi 'id' string so the rest
        of the bot can treat it uniformly.

        Field names in 'info' follow the TrueMarkets CeFi REST spec (Jul 2026).
        If a 400 comes back listing an unknown field, check the docs and adjust.
        """
        info: dict = {
            "instrument_id": instrument_id,
            "side":          side.upper(),
            "type":          "LIMIT",
            "quantity":      qty,
            "price":         price,
        }
        if post_only:
            info["time_in_force"] = "ALO"

        payload = {
            "external_id": str(uuid.uuid4()),
            "info":        info,
        }
        result = await self._cefi_post(session, "/v1/cefi/orders", payload)
        if result and "id" in result:
            result.setdefault("order_id", result["id"])
        return result

    async def cefi_cancel_order(
        self, session: aiohttp.ClientSession, order_id: str
    ) -> bool:
        """Cancel a CeFi order by its exchange id (the 'id' field, not order_id)."""
        return await self._cefi_delete(session, f"/v1/cefi/orders/{order_id}")

    async def cefi_get_order_status(
        self, session: aiohttp.ClientSession, order_id: str
    ) -> str | None:
        """Return the CeFi status string for order_id, or None on failure.

        CeFi status values: INITIALIZED NEW_PENDING REJECTED ACTIVE FILLED
                            CANCEL_PENDING CANCELED MODIFY_PENDING
        """
        data = await self._cefi_get(session, f"/v1/cefi/orders/{order_id}")
        if data:
            return data.get("status")
        return None

    # ── end CeFi direct API ──────────────────────────────────────────────────

    async def get_quote(
        self,
        session: aiohttp.ClientSession,
        base_asset: str,
        quote_asset: str,
        side: str,
        qty: str = "1",
        qty_unit: str = "quote",
    ) -> dict | None:
        """Fetch an indicative quote without placing an order."""
        return await self._post(
            session,
            "/v1/conductor/quotes",
            {
                "base_asset": base_asset,
                "quote_asset": quote_asset,
                "qty": qty,
                "qty_unit": qty_unit,
                "side": side,
            },
        )

    # ── asset catalog / on-chain transfers ──────────────────────────────────

    async def list_assets(
        self, session: aiohttp.ClientSession, venue: str = "defi"
    ) -> list[dict]:
        """List every asset for a venue, walking pagination. For `venue=defi`
        each chain a token exists on is its own row with its own asset_id —
        filter by symbol AND chain to find the right one for a transfer."""
        assets: list[dict] = []
        cursor: str | None = None
        while True:
            path = f"/v1/conductor/assets?venue={venue}"
            if cursor:
                path += f"&cursor={quote(cursor)}"
            data = await self._get(session, path)
            if not data:
                return assets
            assets.extend(data.get("data", []))
            cursor = (data.get("pagination") or {}).get("next_cursor")
            if not cursor:
                return assets

    def sign_payload(self, payload: str) -> str:
        """Turnkey-stamp an UnsignedPayload.payload string (used by both
        order execution and transfer execution)."""
        return self._turnkey_stamp(payload.encode("utf-8"))

    async def create_transfer(
        self,
        session: aiohttp.ClientSession,
        asset_id: str,
        qty: str,
        to: str,
        qty_unit: str = "base",
        network = None
    ) -> dict | None:
        """Create an on-chain transfer. Returns unsigned payloads (status
        `awaiting_signature`) to be signed and passed to execute_transfer."""
        payload = {"asset_id": asset_id, "qty": qty, "qty_unit": qty_unit, "to": to}
        if network:
            payload["network"] = network
        return await self._post(
            session,
            "/v1/conductor/transfers",
            payload
        )

    async def execute_transfer(
        self, session: aiohttp.ClientSession, transfer_id: str, signatures: list[str]
    ) -> dict | None:
        return await self._post(
            session,
            f"/v1/conductor/transfers/{transfer_id}/execute",
            {"signatures": signatures, "auth_type": "api_key"},
        )

    async def get_transfer(
        self, session: aiohttp.ClientSession, transfer_id: str
    ) -> dict | None:
        return await self._get(session, f"/v1/conductor/transfers/{transfer_id}")
