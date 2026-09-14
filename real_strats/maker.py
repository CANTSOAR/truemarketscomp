"""
maker.py  —  Avellaneda-Stoikov market maker with rolling EWMA fair value.

Model
─────
Fair value  s  = EWMA of sampled mid prices (proxy for VWAP when trade-level
                 volume data is unavailable via REST).

Reservation price (inventory skew):
    r = s - f · γ · σ²·τ        f = q / MAX_INV ∈ [−1, 1],  τ = T / interval

Optimal half-spread (bounded to [MIN_SPREAD, MAX_SPREAD]):
    δ/2 = γ · σ²·τ / 2  +  (1/γ) · ln(1 + γ/k)

Bid = r − δ/2       Ask = r + δ/2

σ is realised vol per quote interval (USD); τ expresses the inventory horizon
in intervals so γ·σ²·τ stays dimensionally sane on a high-priced asset. Both
the half-spread and the skew are clamped so quotes always straddle the book
and stay close enough to the market to fill.

Parameters
──────────
  γ  (AS_GAMMA)      risk-aversion coefficient          default 0.1
  k  (AS_K)          order-arrival / depth proxy        default 1.5
  T  (AS_T_SECS)     inventory-risk horizon (seconds)   default 300
  σ                  realised vol, estimated from rolling log-returns

Cycle every AS_INTERVAL_SECS:
  1. poll REST for bid + ask quote
  2. update EWMA and vol estimate
  3. refresh inventory from balances (+ local fill-detection fallback)
  4. compute AS quotes; clamp to minimum spread
  5. cancel previous resting orders
  6. post fresh bid and ask (suppress the side that would breach MAX_INV)

Run:  python3 maker.py
"""

from __future__ import annotations

import asyncio
import math
import os
import sys
import time
import logging
from collections import deque
from pathlib import Path
from dotenv import load_dotenv
import aiohttp

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from lib.execution import ExecutionClient
from lib import dashboard

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

# ── execution ─────────────────────────────────────────────────────────────────
QUOTE_SIZE      = float(os.getenv("MAKER_SIZE_BTC",      "0.0001"))
QUOTE_INTERVAL  = float(os.getenv("MAKER_INTERVAL_SECS", "8"))    # requote faster
MIN_SPREAD_USD  = float(os.getenv("AS_MIN_SPREAD_USD",   "1.0"))
MAX_SPREAD_USD  = float(os.getenv("AS_MAX_SPREAD_USD",   "25.0"))  # tighter → more fills
MAX_INV_BTC     = float(os.getenv("AS_MAX_INV_BTC",      "0.001"))

# ── AS model parameters ───────────────────────────────────────────────────────
GAMMA       = float(os.getenv("AS_GAMMA",       "0.1"))
K           = float(os.getenv("AS_K",           "1.5"))
T_HORIZON   = float(os.getenv("AS_T_SECS",      "300"))
VWAP_ALPHA  = float(os.getenv("AS_VWAP_ALPHA",  "0.30"))  # EWMA decay — track market faster
VOL_WINDOW  = int(os.getenv("AS_VOL_WINDOW",    "12"))     # samples for σ estimate (warms up sooner)


# ── session state ─────────────────────────────────────────────────────────────
_ewma_mid: float | None = None
_price_hist: deque[float] = deque(maxlen=VOL_WINDOW + 1)

# running inventory tracked locally via fill-detection (supplement to REST balances)
_inventory_btc: float = 0.0
_base_btc: float | None = None   # BTC balance at session start

# currently resting orders
_bid_oid: str | None = None
_ask_oid: str | None = None


# ── EWMA fair value ───────────────────────────────────────────────────────────

def update_fair_value(mid: float) -> float:
    global _ewma_mid
    _ewma_mid = mid if _ewma_mid is None else VWAP_ALPHA * mid + (1 - VWAP_ALPHA) * _ewma_mid
    return _ewma_mid


# ── realised volatility ───────────────────────────────────────────────────────

def _sample_stdev(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mu = sum(values) / n
    return math.sqrt(sum((v - mu) ** 2 for v in values) / (n - 1))


def estimate_sigma() -> float | None:
    """
    Return realised volatility in USD *per quote interval*, or None until the
    rolling window is full.

    We estimate from log-returns (scale-free) and convert back to price units
    at the current level. Crucially this is the per-interval σ — it is NOT
    re-scaled to per-second, because as_quotes() expresses the inventory
    horizon in units of intervals, keeping the γ·σ²·T term dimensionally sane.
    """
    prices = list(_price_hist)
    if len(prices) < VOL_WINDOW:
        return None
    log_returns = [
        math.log(prices[i] / prices[i - 1])
        for i in range(1, len(prices))
        if prices[i - 1] > 0
    ]
    if len(log_returns) < 2:
        return None
    σ_log_per_interval = _sample_stdev(log_returns)
    # convert to price units: σ_price ≈ σ_log · S  (first-order approx)
    return σ_log_per_interval * prices[-1]


# ── Avellaneda-Stoikov quotes ─────────────────────────────────────────────────

def as_quotes(
    s: float,      # fair value (EWMA mid)
    q: float,      # net inventory in BTC (positive = long)
    sigma: float,  # realised σ  [USD per quote interval]
) -> tuple[float, float]:
    """
    Returns (bid_price, ask_price) per the AS closed-form solution, with two
    guardrails that keep quotes fillable:

      • the inventory horizon T is expressed in *intervals* (T/interval) so it
        matches σ's per-interval units — no runaway σ²·T blow-up;
      • inventory is normalised to a fraction of MAX_INV so the skew is in the
        same scale as the half-spread, and both are clamped to
        [MIN_SPREAD/2, MAX_SPREAD/2] / |half_spread|.

    Reservation price (skew):  r = s − f · γ · σ²·τ      with f = q/MAX_INV ∈ [−1,1]
    Half-spread:               δ = ½·γ·σ²·τ + (1/γ)·ln(1 + γ/k)
    """
    horizon = max(T_HORIZON / QUOTE_INTERVAL, 1.0)   # inventory horizon in intervals
    σ2τ = sigma ** 2 * horizon

    half_spread = 0.5 * σ2τ * GAMMA + (1.0 / GAMMA) * math.log(1.0 + GAMMA / K)
    half_spread = min(max(half_spread, MIN_SPREAD_USD / 2), MAX_SPREAD_USD / 2)

    inv_frac = max(-1.0, min(1.0, q / MAX_INV_BTC)) if MAX_INV_BTC else 0.0
    skew = inv_frac * GAMMA * σ2τ
    skew = max(-half_spread, min(half_spread, skew))   # never invert the book

    reservation_price = s - skew
    return reservation_price - half_spread, reservation_price + half_spread


# ── order lifecycle ───────────────────────────────────────────────────────────

TERMINAL = {"complete", "canceled", "failed"}


async def _wait_for_status(
    bot: ExecutionClient,
    session: aiohttp.ClientSession,
    oid: str,
    targets: set[str],
    timeout: float = 20.0,
) -> str:
    """Poll GET /orders/{id}/status until status ∈ targets or timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        s = await bot.get_order_status(session, oid)
        if s in targets:
            return s
        await asyncio.sleep(2.0)
    return "timeout"


async def detect_fills_and_cancel(
    bot: ExecutionClient, session: aiohttp.ClientSession
):
    """
    Check known resting orders for fills, cancel any still open, then
    sweep all remaining open orders and wait for them to be gone before
    returning — ensures nothing is in-flight when we place fresh quotes.
    """
    global _bid_oid, _ask_oid, _inventory_btc

    for oid, side in [(_bid_oid, "buy"), (_ask_oid, "sell")]:
        if not oid:
            continue
        status = await bot.get_order_status(session, oid)
        if status == "complete":
            delta = QUOTE_SIZE if side == "buy" else -QUOTE_SIZE
            _inventory_btc += delta
            logging.info(
                f"Fill detected: {side.upper()} {QUOTE_SIZE} BTC  "
                f"→  inventory now {_inventory_btc:+.6f} BTC"
            )
            # record fill in dashboard (price unknown at detection time — use EWMA)
            dashboard.record_fill(side, QUOTE_SIZE, _ewma_mid or 0.0)
        elif status not in TERMINAL:
            await bot.cancel_order(session, oid)
            await _wait_for_status(bot, session, oid, TERMINAL)

    _bid_oid = None
    _ask_oid = None

    # Sweep unknown orders (prior sessions, manual orders) and wait for clear
    await bot.cancel_all(session)
    await asyncio.sleep(1.0)


# ── main quote cycle ──────────────────────────────────────────────────────────

async def quote_cycle(bot: ExecutionClient, session: aiohttp.ClientSession):
    global _base_btc, _inventory_btc, _bid_oid, _ask_oid

    # 1. fetch market prices (buy-side and sell-side quote)
    size_str = f"{QUOTE_SIZE:.6f}".rstrip("0")
    sell_q, buy_q = await asyncio.gather(
        bot.get_quote(session, BASE_ASSET, QUOTE_ASSET,
                      side="sell", qty=size_str, qty_unit="base"),
        bot.get_quote(session, BASE_ASSET, QUOTE_ASSET,
                      side="buy",  qty=size_str, qty_unit="base"),
    )
    if not sell_q or not buy_q:
        logging.warning("Quote fetch failed — skipping cycle.")
        return

    market_bid = float(sell_q["price"])   # price we receive if we sell now
    market_ask = float(buy_q["price"])    # price we pay   if we buy  now
    mid        = (market_bid + market_ask) / 2

    # 2. update EWMA fair value and price history
    s = update_fair_value(mid)
    _price_hist.append(mid)

    # 3. inventory: prefer REST balances, fall back to local tracking
    balances = await bot.get_balances(session)
    if balances.get(BASE_ASSET) is not None:
        btc_bal = balances[BASE_ASSET]
        if _base_btc is None:
            _base_btc = btc_bal
        _inventory_btc = btc_bal - _base_btc
    q = _inventory_btc

    # 4. estimate volatility
    sigma = estimate_sigma()

    logging.info(
        f"mid={mid:,.2f}  s={s:,.2f}  q={q:+.6f} BTC  "
        f"σ={'building…' if sigma is None else f'{sigma:.4f} USD/√s'}"
    )

    # 5. compute AS quotes (or fallback while σ warms up)
    if sigma is not None and sigma > 0:
        bid_px, ask_px = as_quotes(s, q, sigma)
    else:
        # not enough history yet — quote a tight, fillable band around EWMA
        # (~1bp of price), bounded by the configured spread limits.
        fallback_half = min(max(0.0001 * s, MIN_SPREAD_USD / 2), MAX_SPREAD_USD / 2)
        bid_px = s - fallback_half
        ask_px = s + fallback_half

    # inventory guard: only quote the side that reduces exposure
    post_bid = q < MAX_INV_BTC
    post_ask = q > -MAX_INV_BTC

    logging.info(
        f"AS quotes  bid={bid_px:,.2f}  ask={ask_px:,.2f}  "
        f"spread={ask_px - bid_px:.2f}  "
        f"│  post_bid={post_bid}  post_ask={post_ask}"
    )

    # 6. detect fills on resting orders, then cancel stale quotes
    await detect_fills_and_cancel(bot, session)

    # 7. post fresh quotes — sequential: wait for each to be active before next
    if post_bid:
        order = await bot.place_order(
            session,
            base_asset=BASE_ASSET, quote_asset=QUOTE_ASSET,
            side="buy", qty=size_str, qty_unit="base",
            order_type="limit", price=f"{bid_px:.2f}",
        )
        _bid_oid = order.get("order_id") if order else None
        if _bid_oid:
            status = await _wait_for_status(
                bot, session, _bid_oid, {"active"} | TERMINAL
            )
            logging.info(f"Bid {_bid_oid[:8]}… settled → {status}")
            if status in ("canceled", "failed", "timeout"):
                _bid_oid = None

    if post_ask:
        order = await bot.place_order(
            session,
            base_asset=BASE_ASSET, quote_asset=QUOTE_ASSET,
            side="sell", qty=size_str, qty_unit="base",
            order_type="limit", price=f"{ask_px:.2f}",
        )
        _ask_oid = order.get("order_id") if order else None
        if _ask_oid:
            status = await _wait_for_status(
                bot, session, _ask_oid, {"active"} | TERMINAL
            )
            logging.info(f"Ask {_ask_oid[:8]}… settled → {status}")
            if status in ("canceled", "failed", "timeout"):
                _ask_oid = None

    # push state to dashboard
    dashboard.state.update({
        "mid":           mid,
        "fair_value":    s,
        "bid_quote":     bid_px,
        "ask_quote":     ask_px,
        "spread":        ask_px - bid_px,
        "sigma":         sigma,
        "inventory_btc": _inventory_btc,
        "bid_oid":       _bid_oid,
        "ask_oid":       _ask_oid,
        "bid_status":    "active" if _bid_oid else None,
        "ask_status":    "active" if _ask_oid else None,
        "balances":      balances,
    })
    dashboard.record_cycle({"mid": mid, "fair_value": s, "bid": bid_px, "ask": ask_px})


# ── entry point ───────────────────────────────────────────────────────────────

async def main():
    dashboard.start_background(port=8000)
    logging.info("Dashboard running at http://localhost:8000")

    bot = ExecutionClient(key_file=KEY_FILE, base_url=REST_URL)
    async with aiohttp.ClientSession() as session:
        await bot.authenticate(session)
        logging.info("Cancelling any pre-existing open orders...")
        await bot.cancel_all(session)
        await asyncio.sleep(0.5)

        logging.info(
            "Avellaneda-Stoikov market maker started\n"
            f"  γ={GAMMA}  k={K}  T={T_HORIZON}s\n"
            f"  size={QUOTE_SIZE} BTC  interval={QUOTE_INTERVAL}s\n"
            f"  EWMA α={VWAP_ALPHA}  vol window={VOL_WINDOW} samples\n"
            f"  spread=${MIN_SPREAD_USD}–${MAX_SPREAD_USD}  max inventory={MAX_INV_BTC} BTC"
        )
        while True:
            try:
                await quote_cycle(bot, session)
            except Exception as e:
                logging.error(f"Cycle error: {e}", exc_info=True)
                dashboard.record_error(str(e))
            await asyncio.sleep(QUOTE_INTERVAL)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Shutting down.")
