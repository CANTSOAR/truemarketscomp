"""
leadlag.py  —  cross-exchange lead/lag arbitrage for BTC/USDC.

Idea
────
A fast multi-exchange reference price leads; TrueMarkets' own REST quotes lag.
We stream TrueMarkets' own reference_prices feed to maintain a live **fair
value**, then poll the TrueMarkets REST quote endpoint. Whenever TrueMarkets
drifts far enough from fair value to cover taker fees plus a buffer, we take
the favourable side:

    TrueMarkets ASK  <  fair · (1 − edge)   →  BUY  on TrueMarkets (it's cheap)
    TrueMarkets BID  >  fair · (1 + edge)   →  SELL on TrueMarkets (it's rich)

The captured edge is the gap between the stale TrueMarkets price and the
leading fair value, which we expect to close as TrueMarkets catches up.

Lead market  : TrueMarkets' own market-data WebSocket (wss://api.truemarkets.co
               /v1/defi/market, confirmed via asyncapi.json + a live check).
               Public, no auth. The `reference_prices` channel is itself a
               weighted blend across multiple exchange BBOs (observed:
               Coinbase 50% / Kraken 50% for BTC|USD) — TrueMarkets' own
               cross-venue view, not a single external exchange, and push-based
               so it costs zero REST rate-limit budget. (Previously this used
               Coinbase's public ticker directly; swapped after confirming the
               official feed exists and covers BTC|USD.)
Lag market   : TrueMarkets REST quotes  (sell quote = bid, buy quote = ask)

Guards
──────
  • EDGE_THRESHOLD_BPS   minimum edge over fair value before we act
  • TAKER_FEE_BPS        per-fill fee assumption (folded into PnL + threshold)
  • MAX_INV_BTC          hard cap on net long/short inventory
  • FAIR_MAX_STALE_SECS  refuse to trade on a stale lead price
  • MAX_LOSS_USD         kill switch on mark-to-fair PnL

Run:  python3 leadlag.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from lib.execution import ExecutionClient
from lib.ingestion import MarketDataClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
load_dotenv(ROOT_DIR / "keys" / ".env")

# ── connectivity ──────────────────────────────────────────────────────────────
REST_URL    = os.getenv("BASE_REST_URL", "https://api.truemarkets.co")
KEY_FILE    = os.getenv("BOT_A_KEY_FILE", "./keys/truemarkets-api-key-edd1691b.json")
BASE_ASSET  = "BTC"
QUOTE_ASSET = "USDC"

# Lead market (TrueMarkets' own market-data feed — public, no auth required)
LEAD_WS_URL  = os.getenv("LEAD_WS_URL", "wss://api.truemarkets.co/v1/defi/market")
LEAD_PRODUCT = os.getenv("LEAD_PRODUCT", "BTC|USD")   # reference_prices pair notation; USD ≈ USDC for fair value

# ── strategy parameters ───────────────────────────────────────────────────────
TRADE_SIZE_BTC     = float(os.getenv("LL_SIZE_BTC",        "0.0001"))
POLL_INTERVAL      = float(os.getenv("LL_POLL_SECS",       "3.0"))
EDGE_THRESHOLD_BPS = float(os.getenv("LL_EDGE_BPS",        "15.0"))   # min edge to act
TAKER_FEE_BPS      = float(os.getenv("LL_TAKER_FEE_BPS",   "10.0"))   # per fill
MAX_INV_BTC        = float(os.getenv("LL_MAX_INV_BTC",     "0.001"))
FAIR_MAX_STALE     = float(os.getenv("LL_FAIR_MAX_STALE",  "5.0"))    # seconds
MAX_LOSS_USD       = float(os.getenv("LL_MAX_LOSS_USD",    "10.0"))

# ── REST rate limiting ────────────────────────────────────────────────────────
# Confirmed with TrueMarkets: 100 requests/min across the REST APIs. Pace at
# 95/min (5% margin — execution.py's proactive x-ratelimit-remaining throttle
# is the real backstop; this limiter just paces cycles) with a burst large
# enough to clear a full cycle's calls back-to-back, and keep the adaptive
# cool-down as a fallback in case 429s still happen for some other reason.
REQ_PER_SEC   = float(os.getenv("LL_REQ_PER_SEC",  str(95/60)))   # 95 req/min, under the 100/min limit
REQ_BURST     = float(os.getenv("LL_REQ_BURST",    "10"))    # let a full cycle burst through
COOLDOWN_BASE = float(os.getenv("LL_COOLDOWN_SECS","15.0"))  # fallback only — shouldn't trigger now
COOLDOWN_MAX  = float(os.getenv("LL_COOLDOWN_MAX", "60.0"))


# ── client-side rate limiter (token bucket) ───────────────────────────────────

class RateLimiter:
    """
    Async token bucket. `acquire()` blocks until a token is available, capping
    the sustained request rate while permitting a small burst. Serialised via a
    lock so the single trading loop self-paces against the server's limit.
    """

    def __init__(self, rate_per_sec: float, capacity: float):
        self.rate = rate_per_sec
        self.capacity = capacity
        self.tokens = capacity
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                await asyncio.sleep((1.0 - self.tokens) / self.rate)


# ── live fair value from the lead market ──────────────────────────────────────

class FairValueFeed:
    """
    Maintains a live mid price from TrueMarkets' own `reference_prices`
    channel (a weighted blend across multiple exchange BBOs — observed
    Coinbase 50% / Kraken 50% for BTC|USD — not a single external venue).
    Runs as a background task with auto-reconnect (via MarketDataClient);
    `.value` / `.age()` are read by the trading loop.
    """

    def __init__(self, ws_url: str, symbol: str):
        self._client = MarketDataClient(ws_url)
        self.symbol = symbol   # pair notation, e.g. "BTC|USD"
        self._mid: float | None = None
        self._ts: float = 0.0

    @property
    def value(self) -> float | None:
        return self._mid

    def age(self) -> float:
        """Seconds since the last update (inf if never updated)."""
        return float("inf") if self._ts == 0.0 else time.time() - self._ts

    async def run(self):
        await self._client.connect_and_listen(self._handle)

    async def _handle(self, msg: dict):
        if msg.get("type") != "reference_prices":
            return
        for item in msg.get("data", []):
            if item.get("symbol") != self.symbol:
                continue
            try:
                self._mid = float(item["mid_price"])
            except (KeyError, ValueError, TypeError):
                return
            self._ts = time.time()
            return


# ── position / PnL bookkeeping (marked to lead fair value) ────────────────────

class Book:
    """Tracks net BTC inventory and cash, marking PnL against the fair value."""

    def __init__(self):
        self.inventory_btc = 0.0   # net position vs. session start
        self.cash_usd = 0.0        # signed cash flow incl. fees
        self.trades = 0

    def record(self, side: str, qty: float, price: float, fee_usd: float):
        notional = qty * price
        if side == "buy":
            self.inventory_btc += qty
            self.cash_usd -= notional + fee_usd
        else:  # sell
            self.inventory_btc -= qty
            self.cash_usd += notional - fee_usd
        self.trades += 1

    def pnl(self, fair: float) -> float:
        """Mark-to-fair PnL: realised cash plus inventory valued at fair."""
        return self.cash_usd + self.inventory_btc * fair


# ── one decision/trade cycle ──────────────────────────────────────────────────

async def arb_cycle(
    bot: ExecutionClient,
    session: aiohttp.ClientSession,
    feed: FairValueFeed,
    book: Book,
    limiter: RateLimiter,
    size_str: str,
) -> bool:
    """Run one decision cycle. Returns False if a TM quote could not be fetched
    (likely rate-limited) so the caller can apply a cool-down."""
    fair = feed.value
    if fair is None:
        logging.info("Waiting for lead price…")
        return True
    if feed.age() > FAIR_MAX_STALE:
        logging.warning(f"Lead price stale ({feed.age():.1f}s) — skipping cycle.")
        return True

    # TrueMarkets effective book: sell quote → bid, buy quote → ask.
    # Each REST call passes through the rate limiter; sequential, not gathered.
    await limiter.acquire()
    sell_q = await bot.get_quote(session, BASE_ASSET, QUOTE_ASSET, side="sell", qty=size_str, qty_unit="base")
    await limiter.acquire()
    buy_q  = await bot.get_quote(session, BASE_ASSET, QUOTE_ASSET, side="buy",  qty=size_str, qty_unit="base")
    if not sell_q or not buy_q:
        logging.warning("TrueMarkets quote fetch failed (rate-limited?) — cooling down.")
        return False
    try:
        tm_bid = float(sell_q["price"])   # proceeds if we SELL on TrueMarkets
        tm_ask = float(buy_q["price"])    # cost     if we BUY  on TrueMarkets
    except (KeyError, ValueError, TypeError):
        logging.error(f"Unexpected quote shape: sell={sell_q} buy={buy_q}")
        return True

    # Edges in basis points relative to fair value.
    buy_edge_bps  = (fair - tm_ask) / fair * 1e4   # TM cheap → buy
    sell_edge_bps = (tm_bid - fair) / fair * 1e4   # TM rich  → sell
    # Require the gross edge to clear the fee on the leg plus the buffer.
    min_edge = EDGE_THRESHOLD_BPS + TAKER_FEE_BPS

    q = book.inventory_btc
    logging.info(
        f"fair={fair:,.2f}  TM bid={tm_bid:,.2f} ask={tm_ask:,.2f}  "
        f"buy_edge={buy_edge_bps:+.1f}bps  sell_edge={sell_edge_bps:+.1f}bps  "
        f"q={q:+.6f} BTC  pnl=${book.pnl(fair):+.4f}"
    )

    # Pick the better qualifying side, respecting inventory caps.
    side: str | None = None
    price = 0.0
    if buy_edge_bps >= sell_edge_bps:
        if buy_edge_bps >= min_edge and q + TRADE_SIZE_BTC <= MAX_INV_BTC:
            side, price = "buy", tm_ask
    if side is None:
        if sell_edge_bps >= min_edge and q - TRADE_SIZE_BTC >= -MAX_INV_BTC:
            side, price = "sell", tm_bid

    if side is None:
        return True

    # Selling requires BTC on hand beyond our synthetic short cap. Buying is
    # funded by a silent PYUSD->USDC conversion on the venue side (confirmed
    # with their engineering team) so no pre-trade USDC balance check needed.
    if side == "sell":
        await limiter.acquire()
        balances = await bot.get_balances(session)
        if balances.get(BASE_ASSET, 0.0) < TRADE_SIZE_BTC:
            logging.info("Sell signal but insufficient BTC balance — skipping.")
            return True

    edge_bps = buy_edge_bps if side == "buy" else sell_edge_bps
    logging.info(
        f"▶ TAKE {side.upper()} {size_str} {BASE_ASSET} @ {price:,.2f}  "
        f"(edge {edge_bps:.1f}bps vs fair {fair:,.2f})"
    )

    # qty_unit="base" for market orders on both sides, confirmed with
    # TrueMarkets engineering (the spec's "buy requires qty_unit=quote" is
    # one of the documented inconsistencies).
    await limiter.acquire()
    order = await bot.place_order(
        session,
        base_asset=BASE_ASSET,
        quote_asset=QUOTE_ASSET,
        side=side,
        qty=size_str,
        qty_unit="base",
        order_type="market",
    )
    if not order:
        logging.error("Order failed — no fill recorded.")
        return True

    fee_usd = TRADE_SIZE_BTC * price * (TAKER_FEE_BPS / 1e4)
    book.record(side, TRADE_SIZE_BTC, price, fee_usd)
    logging.info(
        f"Filled {side.upper()} {TRADE_SIZE_BTC} BTC  →  "
        f"q={book.inventory_btc:+.6f}  pnl=${book.pnl(fair):+.4f}  "
        f"trades={book.trades}"
    )
    return True


# ── entry point ───────────────────────────────────────────────────────────────

async def main():
    bot = ExecutionClient(key_file=KEY_FILE, base_url=REST_URL)
    feed = FairValueFeed(LEAD_WS_URL, LEAD_PRODUCT)
    book = Book()
    limiter = RateLimiter(REQ_PER_SEC, REQ_BURST)
    size_str = f"{TRADE_SIZE_BTC:.8f}".rstrip("0").rstrip(".")

    feed_task = asyncio.create_task(feed.run())

    async with aiohttp.ClientSession() as session:
        await bot.authenticate(session)
        logging.info(
            "Lead/lag arbitrage started\n"
            f"  lead         : {LEAD_PRODUCT} via {LEAD_WS_URL}\n"
            f"  lag          : {BASE_ASSET}/{QUOTE_ASSET} on {REST_URL}\n"
            f"  size         : {TRADE_SIZE_BTC} BTC   poll : {POLL_INTERVAL}s\n"
            f"  edge thresh  : {EDGE_THRESHOLD_BPS}bps + {TAKER_FEE_BPS}bps fee\n"
            f"  max inv      : ±{MAX_INV_BTC} BTC   loss limit : ${MAX_LOSS_USD}"
        )
        cooldown = COOLDOWN_BASE
        try:
            while True:
                quote_ok = True
                try:
                    quote_ok = await arb_cycle(bot, session, feed, book, limiter, size_str)
                    fair = feed.value
                    if fair is not None and book.pnl(fair) <= -MAX_LOSS_USD:
                        logging.error(
                            f"Kill switch: mark-to-fair PnL ${book.pnl(fair):.2f} "
                            f"≤ −${MAX_LOSS_USD:.2f}. Stopping."
                        )
                        break
                except Exception as e:
                    logging.error(f"Cycle error: {e}", exc_info=True)

                if quote_ok:
                    cooldown = COOLDOWN_BASE          # reset after a clean cycle
                    await asyncio.sleep(POLL_INTERVAL)
                else:
                    logging.warning(f"Backing off {cooldown:.0f}s after throttle.")
                    await asyncio.sleep(cooldown)
                    cooldown = min(cooldown * 2, COOLDOWN_MAX)
        finally:
            feed_task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Shutting down.")
