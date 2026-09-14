from __future__ import annotations

import websockets
import json
import logging
import asyncio
from typing import Callable, Awaitable

# Confirmed against asyncapi.json: wss://api.truemarkets.co/v1/defi/market is
# real and public (no auth — the JWT/Authorization logic this file had
# before was never part of the spec and has been removed). Subscribing with
# {"type": "subscribe", "topics": ["all"]} streams price_candles,
# trending_assets, surging_assets, and reference_prices messages.
#
# reference_prices is the interesting one for fair-value purposes: it's
# TrueMarkets' own weighted blend across multiple exchange BBOs (observed:
# Coinbase 50% / Kraken 50% for BTC|USD) — note the pair-notation symbol
# format ("BTC|USD"), which differs from price_candles/trending_assets'
# bare symbol format ("BTC").


class MarketDataClient:
    def __init__(self, ws_url: str = "wss://api.truemarkets.co/v1/defi/market"):
        self.ws_url = ws_url

    async def connect_and_listen(
        self, on_message_callback: Callable[[dict], Awaitable[None]]
    ):
        backoff = 1
        while True:
            try:
                async with websockets.connect(self.ws_url) as ws:
                    backoff = 1
                    await ws.send(json.dumps({"type": "subscribe", "topics": ["all"]}))
                    logging.info("WS: subscribed to topic 'all'.")

                    async for raw in ws:
                        try:
                            data = json.loads(raw)
                            await on_message_callback(data)
                        except json.JSONDecodeError:
                            logging.warning(f"WS: non-JSON message: {raw!r}")

            except websockets.ConnectionClosed as e:
                logging.warning(f"WS: connection closed (code={e.code}). Reconnecting in {backoff}s...")
            except OSError as e:
                logging.error(f"WS: network error: {e}. Reconnecting in {backoff}s...")
            except Exception as e:
                logging.error(f"WS: unexpected error: {e}. Reconnecting in {backoff}s...")

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
