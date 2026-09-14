from __future__ import annotations

import asyncio
import json
import logging
import time

import websockets

# Coinbase Exchange public WebSocket — no auth required. The `ticker` channel
# pushes best_bid/best_ask on every book change, which is all Book A needs
# here (we don't reconstruct the full book, just its best quotes). This is a
# direct feed from a real second venue, distinct from TrueMarkets' own
# `reference_prices` channel (used elsewhere in this repo), which is already
# a blend across exchanges and therefore not a genuine independent Book A.


class CoinbaseBookA:
    def __init__(
        self,
        ws_url: str = "wss://ws-feed.exchange.coinbase.com",
        product_id: str = "BTC-USD",
    ):
        self.ws_url = ws_url
        self.product_id = product_id
        self._bid: float | None = None
        self._ask: float | None = None
        self._ts: float = 0.0

    @property
    def best_bid(self) -> float | None:
        return self._bid

    @property
    def best_ask(self) -> float | None:
        return self._ask

    @property
    def mid(self) -> float | None:
        if self._bid is None or self._ask is None:
            return None
        return (self._bid + self._ask) / 2.0

    def age(self) -> float:
        """Seconds since the last update (inf if never updated)."""
        return float("inf") if self._ts == 0.0 else time.time() - self._ts

    async def run(self) -> None:
        backoff = 1
        sub = {
            "type": "subscribe",
            "product_ids": [self.product_id],
            "channels": ["ticker"],
        }
        while True:
            try:
                async with websockets.connect(self.ws_url) as ws:
                    backoff = 1
                    await ws.send(json.dumps(sub))
                    logging.info(f"Coinbase WS: subscribed to ticker for {self.product_id}.")

                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if msg.get("type") != "ticker" or msg.get("product_id") != self.product_id:
                            continue
                        try:
                            self._bid = float(msg["best_bid"])
                            self._ask = float(msg["best_ask"])
                        except (KeyError, ValueError, TypeError):
                            continue
                        self._ts = time.time()

            except websockets.ConnectionClosed as e:
                logging.warning(f"Coinbase WS: connection closed (code={e.code}). Reconnecting in {backoff}s...")
            except OSError as e:
                logging.error(f"Coinbase WS: network error: {e}. Reconnecting in {backoff}s...")
            except Exception as e:
                logging.error(f"Coinbase WS: unexpected error: {e}. Reconnecting in {backoff}s...")

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
