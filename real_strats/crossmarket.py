"""
crossmarket.py — genuine two-venue arbitrage between Coinbase (Book A) and
TrueMarkets (Book B) for BTC/USD(C).

Book A (Coinbase) is fast and liquid — its BBO is the price-discovery oracle.
Book B (TrueMarkets) is slower and wider-spread. Every cycle we run two
frameworks against the same pair of books, taking whichever fires:

  1. Cross-market making (maker-taker)
     Rest a passive limit order on TrueMarkets *inside its own spread* but
     anchored to Coinbase's mid (never bid above it, never ask below it), so
     uninformed TrueMarkets flow gets filled at a price that's still good for
     us relative to the real market. The instant a resting order fills, fire
     an opposite-side market hedge on Coinbase.

  2. Latency arbitrage (taker-taker)
     If TrueMarkets' own best bid/ask has drifted far enough from Coinbase's
     BBO to clear both venues' taker fees plus a buffer, take liquidity on
     both sides at once: lift/hit the stale TrueMarkets quote and hedge
     immediately on Coinbase.

Both legs are tracked in a single inventory/PnL ledger (CrossBook), marked to
Coinbase's mid (the oracle). Coinbase execution defaults to DRY-RUN (see
coinbase_execution.py) — real Book A market data drives every decision, but
no live Coinbase order is sent until COINBASE_KEY_FILE points at a real CDP
API key. TrueMarkets execution is unmodified execution.py, the same client
validated in truemarkets_notebook.ipynb.

Risk:
  • Per-venue inventory cap (XM_MAX_INV_BTC)
  • Kill switch on combined mark-to-Coinbase PnL (XM_MAX_LOSS_USD)
  • Imbalance warning (XM_IMBALANCE_WARN_BTC) — this process never moves
    capital between venues itself (deposits/withdrawals are a separate,
    deliberate action); it only warns loudly when net inventory drifts far
    enough that a manual rebalance is needed before margin runs out.

Run:  python3 crossmarket.py
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

from lib.coinbase_execution import CoinbaseExecutionClient
from lib.coinbase_feed import CoinbaseBookA
from lib.execution import ExecutionClient
from lib.leadlag import RateLimiter

load_dotenv(ROOT_DIR / "keys" / ".env")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    force=True,
)

# ── connectivity ──────────────────────────────────────────────────────────────
TM_REST_URL   = os.getenv("BASE_REST_URL", "https://api.truemarkets.co")
TM_KEY_FILE   = os.getenv("BOT_A_KEY_FILE", "./keys/truemarkets-api-key-edd1691b.json")
BASE_ASSET    = "BTC"
QUOTE_ASSET   = "USDC"

CB_WS_URL     = os.getenv("COINBASE_WS_URL", "wss://ws-feed.exchange.coinbase.com")
CB_REST_URL   = os.getenv("COINBASE_REST_URL", "https://api.coinbase.com")
CB_KEY_FILE   = os.getenv("COINBASE_KEY_FILE")   # unset -> DRY-RUN
CB_PRODUCT_ID = os.getenv("COINBASE_PRODUCT_ID", "BTC-USD")

# ── current price-bucket constants (BTC $10,000–$99,999.90 on TrueMarkets) ────
LOT_SIZE  = float(os.getenv("XM_LOT_SIZE",  "0.0001"))
TICK_SIZE = float(os.getenv("XM_TICK_SIZE", "0.1"))

# ── strategy parameters ───────────────────────────────────────────────────────
TRADE_SIZE_BTC      = float(os.getenv("XM_SIZE_BTC",          str(LOT_SIZE)))
POLL_INTERVAL        = float(os.getenv("XM_POLL_SECS",         "2.0"))   # the rate limiter, not this, is the real pacing
FAIR_MAX_STALE        = float(os.getenv("XM_FAIR_MAX_STALE",    "5.0"))   # max age (s) of Coinbase BBO before we stand down

TM_TAKER_FEE_BPS      = float(os.getenv("XM_TM_TAKER_FEE_BPS",  "10.0"))
CB_TAKER_FEE_BPS      = float(os.getenv("XM_CB_TAKER_FEE_BPS",  "60.0"))  # Coinbase Advanced Trade low-tier taker; check your account's actual tier at coinbase.com/advanced-trade/fees
ARB_FEE_BUFFER_BPS    = float(os.getenv("XM_ARB_BUFFER_BPS",    "10.0"))
ARB_MIN_EDGE_BPS       = TM_TAKER_FEE_BPS + CB_TAKER_FEE_BPS + ARB_FEE_BUFFER_BPS

MAKER_IMPROVE_BPS     = float(os.getenv("XM_MAKER_IMPROVE_BPS", "5.0"))   # how far inside fair value our resting quote sits
SKEW_BPS_MAX          = float(os.getenv("XM_SKEW_BPS_MAX",      "8.0"))   # inventory skew applied to the maker reservation price
REPRICE_THRESHOLD_BPS = float(os.getenv("XM_REPRICE_THRESHOLD_BPS", "3.0"))  # only cancel+repost a resting maker quote once the target moves this far; otherwise leave it resting so flow has a real chance to hit it

MAX_INV_BTC           = float(os.getenv("XM_MAX_INV_BTC",       str(3 * LOT_SIZE)))   # cap per venue leg
MAX_LOSS_USD          = float(os.getenv("XM_MAX_LOSS_USD",      "5.0"))
IMBALANCE_WARN_BTC    = float(os.getenv("XM_IMBALANCE_WARN_BTC", str(2 * LOT_SIZE)))

TERMINAL = {"complete", "canceled", "failed"}


def round_to_tick(price: float) -> float:
    return round(round(price / TICK_SIZE) * TICK_SIZE, 1)


# ── combined two-venue inventory / PnL ledger ─────────────────────────────────

class CrossBook:
    """Tracks net BTC inventory and cash on each venue separately, plus a
    combined mark-to-market PnL against Coinbase's mid (the oracle price)."""

    def __init__(self):
        self.tm_inventory_btc = 0.0
        self.cb_inventory_btc = 0.0
        self.cash_usd = 0.0
        self.trades = 0

    def record(self, venue: str, side: str, qty: float, price: float, fee_usd: float) -> None:
        notional = qty * price
        sign = 1.0 if side == "buy" else -1.0
        if venue == "tm":
            self.tm_inventory_btc += sign * qty
        else:
            self.cb_inventory_btc += sign * qty
        self.cash_usd -= sign * notional + fee_usd
        self.trades += 1

    @property
    def net_inventory_btc(self) -> float:
        return self.tm_inventory_btc + self.cb_inventory_btc

    def pnl(self, mark_price: float) -> float:
        return self.cash_usd + self.net_inventory_btc * mark_price


# ── fill detection + immediate hedge (maker-taker leg) ────────────────────────

async def detect_fills_and_hedge(
    bot: ExecutionClient,
    tm_session: aiohttp.ClientSession,
    cb: CoinbaseExecutionClient,
    cb_session: aiohttp.ClientSession,
    book_a: CoinbaseBookA,
    book: CrossBook,
    limiter: RateLimiter,
    state: dict,
    size_str: str,
    size_btc: float,
) -> None:
    for key, side in [("bid", "buy"), ("ask", "sell")]:
        oid = state.get(f"{key}_oid")
        if not oid:
            continue
        await limiter.acquire()
        status = await bot.get_order_status(tm_session, oid)
        if status == "complete":
            fill_price = state[f"{key}_px"]
            tm_fee = size_btc * fill_price * (TM_TAKER_FEE_BPS / 1e4)
            book.record("tm", side, size_btc, fill_price, tm_fee)

            hedge_side = "sell" if side == "buy" else "buy"
            ref_price = book_a.best_bid if hedge_side == "sell" else book_a.best_ask
            hedge = await cb.place_order(
                cb_session, product_id=CB_PRODUCT_ID, side=hedge_side,
                size=size_str, ref_price=ref_price,
            )
            if hedge and ref_price is not None:
                cb_fee = size_btc * ref_price * (CB_TAKER_FEE_BPS / 1e4)
                book.record("cb", hedge_side, size_btc, ref_price, cb_fee)
                logging.info(
                    f"FILL  TM {side.upper()} {size_btc} BTC @ {fill_price:,.1f}  →  "
                    f"hedged CB {hedge_side.upper()} @ ~{ref_price:,.1f}  "
                    f"net_inv={book.net_inventory_btc:+.6f} BTC"
                )
            else:
                logging.error("TM fill detected but Coinbase hedge failed — inventory is now unhedged!")
            state[f"{key}_oid"] = None
        elif status in TERMINAL:
            # canceled/failed by something other than us (e.g. manually) — clear so we repost
            state[f"{key}_oid"] = None
        # else: still resting (pending/active) — leave it. Repricing, if any, is
        # handled by the maker section below, not by an unconditional cancel here;
        # cancelling every cycle regardless of fills gives flow no time to ever hit it.


async def place_or_hold(
    bot: ExecutionClient,
    tm_session: aiohttp.ClientSession,
    limiter: RateLimiter,
    state: dict,
    key: str,
    side: str,
    should_post: bool,
    target_px: float,
    size_str: str,
) -> None:
    """Post a fresh resting quote, leave the current one alone, or cancel it
    outright — whichever keeps the order resting as long as possible while
    still tracking the market. Cancelling and reposting an unchanged price
    every cycle gives real flow zero time to ever hit it."""
    oid = state.get(f"{key}_oid")
    existing_px = state.get(f"{key}_px")

    if not should_post:
        if oid:
            await limiter.acquire()
            await bot.cancel_order(tm_session, oid)
            state[f"{key}_oid"] = None
        return

    if oid and existing_px:
        drift_bps = abs(target_px - existing_px) / existing_px * 1e4
        if drift_bps <= REPRICE_THRESHOLD_BPS:
            return   # close enough — leave it resting, preserve queue priority

    if oid:
        await limiter.acquire()
        await bot.cancel_order(tm_session, oid)
        state[f"{key}_oid"] = None

    await limiter.acquire()
    order = await bot.place_order(
        tm_session, base_asset=BASE_ASSET, quote_asset=QUOTE_ASSET,
        side=side, qty=size_str, qty_unit="base",
        order_type="limit", price=f"{target_px:.1f}",
    )
    if order and order.get("order_id"):
        state[f"{key}_oid"], state[f"{key}_px"] = order["order_id"], target_px
        logging.info(f"  resting {key.upper()}  {order['order_id'][:8]}… @ {target_px:,.1f}")


# ── one decision cycle ────────────────────────────────────────────────────────

async def cycle(
    bot: ExecutionClient,
    tm_session: aiohttp.ClientSession,
    cb: CoinbaseExecutionClient,
    cb_session: aiohttp.ClientSession,
    book_a: CoinbaseBookA,
    book: CrossBook,
    limiter: RateLimiter,
    state: dict,
    size_str: str,
) -> None:
    if book_a.mid is None or book_a.age() > FAIR_MAX_STALE:
        logging.info("Waiting for Coinbase BBO…")
        return

    await detect_fills_and_hedge(
        bot, tm_session, cb, cb_session, book_a, book, limiter, state, size_str, TRADE_SIZE_BTC
    )

    # kill switch — mark combined position to Coinbase mid
    pnl = book.pnl(book_a.mid)
    if pnl <= -MAX_LOSS_USD:
        logging.error(f"Kill switch: combined PnL ${pnl:.2f} ≤ −${MAX_LOSS_USD:.2f}. Stopping.")
        state["killed"] = True
        return

    if abs(book.net_inventory_btc) >= IMBALANCE_WARN_BTC:
        logging.warning(
            f"Inventory imbalance: TM={book.tm_inventory_btc:+.6f}  CB={book.cb_inventory_btc:+.6f}  "
            f"net={book.net_inventory_btc:+.6f} BTC — manual rebalance between venues recommended; "
            "this process does not move capital itself."
        )

    q = book.net_inventory_btc
    fair = book_a.mid

    # ── optional: TM indicative prices ──────────────────────────────────────────
    # /v1/conductor/quotes gives the TM bid/ask for latency-arb detection and
    # TM-book clamping on the maker leg. If the endpoint is down (503) or returns
    # bad data, degrade gracefully: arb is disabled and maker prices anchor to
    # CB fair value alone (no TM book clamping). Both strategies still run.
    tm_bid: float | None = None
    tm_ask: float | None = None
    await limiter.acquire()
    sell_q = await bot.get_quote(tm_session, BASE_ASSET, QUOTE_ASSET, side="sell", qty=size_str, qty_unit="base")
    await limiter.acquire()
    buy_q  = await bot.get_quote(tm_session, BASE_ASSET, QUOTE_ASSET, side="buy",  qty=size_str, qty_unit="base")
    if sell_q and buy_q:
        raw_sell = float(sell_q.get("price") or 0)
        raw_buy  = float(buy_q.get("price")  or 0)
        if raw_sell > 0 and raw_buy > 0 and raw_sell != raw_buy:
            lo, hi = (raw_sell, raw_buy) if raw_sell < raw_buy else (raw_buy, raw_sell)
            if 0.95 * fair <= lo and hi <= 1.05 * fair:
                tm_bid, tm_ask = lo, hi
                if raw_sell > raw_buy:
                    logging.debug("TM quotes market-maker perspective (sell>buy); bid/ask swapped.")
            else:
                logging.warning(
                    f"TM quotes far from CB fair ({fair:,.1f}): lo={lo:,.1f} hi={hi:,.1f} — ignoring."
                )

    # ── real balances on both venues ────────────────────────────────────────────
    # A "buy" leg (TM buy, hedged by a CB sell) needs: TM quote currency to fund
    # the buy, AND Coinbase BTC on hand to actually hedge-sell afterward.
    # A "sell" leg (TM sell, hedged by a CB buy) needs: TM BTC to sell, AND
    # Coinbase USD on hand to hedge-buy afterward. Checking both sides up front
    # means we never take/post an action we already know we can't hedge, rather
    # than discovering it only when the hedge call itself fails.
    await limiter.acquire()
    tm_balances = await bot.get_balances(tm_session)
    cb_balances = {} if cb.dry_run else await cb.get_balances(cb_session)

    tm_quote_balance = tm_balances.get("PYUSD", 0.0) + tm_balances.get("USDC", 0.0)
    tm_btc_balance = tm_balances.get(BASE_ASSET, 0.0)
    cb_btc_balance = cb_balances.get("BTC", 0.0)
    cb_usd_balance = cb_balances.get("USD", 0.0)
    required_quote = TRADE_SIZE_BTC * fair * 1.01   # 1% buffer for slippage/fees

    can_buy_tm        = tm_quote_balance >= required_quote
    can_sell_tm       = tm_btc_balance >= TRADE_SIZE_BTC
    can_hedge_sell_cb = cb.dry_run or cb_btc_balance >= TRADE_SIZE_BTC   # needed to hedge a TM buy
    can_hedge_buy_cb  = cb.dry_run or cb_usd_balance >= required_quote   # needed to hedge a TM sell
    can_go_long  = can_buy_tm and can_hedge_sell_cb    # TM buy + CB sell hedge
    can_go_short = can_sell_tm and can_hedge_buy_cb    # TM sell + CB buy hedge

    tm_bid_str = f"{tm_bid:,.1f}" if tm_bid is not None else "n/a"
    tm_ask_str = f"{tm_ask:,.1f}" if tm_ask is not None else "n/a"
    logging.info(
        f"CB bid={book_a.best_bid:,.1f} ask={book_a.best_ask:,.1f}  |  "
        f"TM bid={tm_bid_str} ask={tm_ask_str}  |  "
        f"net_inv={q:+.6f} BTC  pnl=${pnl:+.4f}  |  "
        f"TM quote={tm_quote_balance:.2f} BTC={tm_btc_balance:.6f}  "
        f"CB BTC={cb_btc_balance:.6f} USD={cb_usd_balance:.2f}"
    )

    # ── 1. latency arbitrage (taker-taker) ────────────────────────────────────
    # Requires reliable TM bid/ask; skip when quotes endpoint is unavailable.
    if tm_bid is not None and tm_ask is not None:
        buy_edge_bps  = (book_a.best_bid - tm_ask) / book_a.best_bid * 1e4   # TM ask cheap vs CB bid
        sell_edge_bps = (tm_bid - book_a.best_ask) / book_a.best_ask * 1e4   # TM bid rich vs CB ask

        arb_side = None
        if buy_edge_bps >= sell_edge_bps:
            if buy_edge_bps >= ARB_MIN_EDGE_BPS and q + TRADE_SIZE_BTC <= MAX_INV_BTC:
                arb_side = "buy"
        if arb_side is None and sell_edge_bps >= ARB_MIN_EDGE_BPS and q - TRADE_SIZE_BTC >= -MAX_INV_BTC:
            arb_side = "sell"

        if arb_side == "buy" and not can_go_long:
            logging.info(
                f"Arb buy signal but can't fund+hedge it (TM quote ok={can_buy_tm}, "
                f"CB BTC ok={can_hedge_sell_cb}) — skipping."
            )
            arb_side = None
        if arb_side == "sell" and not can_go_short:
            logging.info(
                f"Arb sell signal but can't fund+hedge it (TM BTC ok={can_sell_tm}, "
                f"CB USD ok={can_hedge_buy_cb}) — skipping."
            )
            arb_side = None

        if arb_side is not None:
            for key in ("bid", "ask"):
                oid = state.get(f"{key}_oid")
                if oid:
                    await limiter.acquire()
                    await bot.cancel_order(tm_session, oid)
                    state[f"{key}_oid"] = None

        if arb_side is not None:
            tm_price = tm_ask if arb_side == "buy" else tm_bid
            edge_bps = buy_edge_bps if arb_side == "buy" else sell_edge_bps
            logging.info(f"▶ LATENCY ARB: TM {arb_side.upper()} @ {tm_price:,.1f}  (edge {edge_bps:.1f}bps)")

            await limiter.acquire()
            order = await bot.place_order(
                tm_session, base_asset=BASE_ASSET, quote_asset=QUOTE_ASSET,
                side=arb_side, qty=size_str, qty_unit="base", order_type="market",
            )
            if order:
                tm_fee = TRADE_SIZE_BTC * tm_price * (TM_TAKER_FEE_BPS / 1e4)
                book.record("tm", arb_side, TRADE_SIZE_BTC, tm_price, tm_fee)

                hedge_side = "sell" if arb_side == "buy" else "buy"
                ref_price = book_a.best_bid if hedge_side == "sell" else book_a.best_ask
                hedge = await cb.place_order(
                    cb_session, product_id=CB_PRODUCT_ID, side=hedge_side,
                    size=size_str, ref_price=ref_price,
                )
                if hedge:
                    cb_fee = TRADE_SIZE_BTC * ref_price * (CB_TAKER_FEE_BPS / 1e4)
                    book.record("cb", hedge_side, TRADE_SIZE_BTC, ref_price, cb_fee)
                    logging.info(
                        f"Arb filled: TM {arb_side.upper()} @ {tm_price:,.1f}  +  "
                        f"CB {hedge_side.upper()} @ ~{ref_price:,.1f}  "
                        f"pnl=${book.pnl(book_a.mid):+.4f}  trades={book.trades}"
                    )
                else:
                    logging.error("TM arb leg filled but Coinbase hedge failed — inventory is now unhedged!")
            else:
                logging.error("Latency-arb TM order failed.")
            return

    # ── 2. cross-market making (maker-taker) ──────────────────────────────────
    inv_frac = max(-1.0, min(1.0, q / MAX_INV_BTC)) if MAX_INV_BTC else 0.0
    reservation = fair * (1.0 - inv_frac * SKEW_BPS_MAX / 1e4)

    candidate_bid = reservation * (1.0 - MAKER_IMPROVE_BPS / 1e4)
    candidate_ask = reservation * (1.0 + MAKER_IMPROVE_BPS / 1e4)
    # When TM book prices are available, clamp inside TM's spread so we never
    # post worse than one tick inside TM's BBO. When quotes endpoint is down,
    # anchor purely to CB fair value (no clamping — profitability floor still applies).
    if tm_bid is not None:
        bid_px = round_to_tick(min(max(candidate_bid, tm_bid + TICK_SIZE), reservation))
        ask_px = round_to_tick(max(min(candidate_ask, tm_ask - TICK_SIZE), reservation))
    else:
        bid_px = round_to_tick(candidate_bid)
        ask_px = round_to_tick(candidate_ask)

    # profitability floor: winning queue priority is worthless if the price needed
    # to get there doesn't clear the round-trip cost of a fill + Coinbase hedge
    # (same fee+buffer math as the arb leg's ARB_MIN_EDGE_BPS). Without this, the
    # tick-above/below-book clamp above will happily chase a tightening TM book
    # down past the point where a fill can ever be profitable.
    bid_ceiling = reservation * (1.0 - ARB_MIN_EDGE_BPS / 1e4)   # most aggressive bid that's still profitable
    ask_floor   = reservation * (1.0 + ARB_MIN_EDGE_BPS / 1e4)   # least aggressive ask that's still profitable

    post_bid = bid_px < reservation and bid_px <= bid_ceiling and q + TRADE_SIZE_BTC <= MAX_INV_BTC and can_go_long
    post_ask = ask_px > reservation and ask_px >= ask_floor and q - TRADE_SIZE_BTC >= -MAX_INV_BTC and can_go_short

    await place_or_hold(bot, tm_session, limiter, state, "bid", "buy", post_bid, bid_px, size_str)
    await place_or_hold(bot, tm_session, limiter, state, "ask", "sell", post_ask, ask_px, size_str)


# ── entry point ───────────────────────────────────────────────────────────────

async def main():
    bot = ExecutionClient(key_file=TM_KEY_FILE, base_url=TM_REST_URL)
    cb = CoinbaseExecutionClient(key_file=CB_KEY_FILE, base_url=CB_REST_URL)
    book_a = CoinbaseBookA(ws_url=CB_WS_URL, product_id=CB_PRODUCT_ID)
    book = CrossBook()
    # TrueMarkets: 100 req/min confirmed. Worst case per cycle here: 2 fill
    # checks + 1 balance fetch + 2 quotes + up to 2 cancels + up to 2 orders
    # = 9. Pace at 90/min (10% margin) with capacity sized to clear a full
    # cycle back-to-back rather than trickling one call at a time.
    limiter = RateLimiter(rate_per_sec=1.5, capacity=10)
    size_str = f"{TRADE_SIZE_BTC:.8f}".rstrip("0").rstrip(".")
    state: dict = {"bid_oid": None, "ask_oid": None, "bid_px": None, "ask_px": None, "killed": False}

    feed_task = asyncio.create_task(book_a.run())

    async with aiohttp.ClientSession() as tm_session, aiohttp.ClientSession() as cb_session:
        await bot.authenticate(tm_session)
        logging.info("Cancelling any pre-existing open TrueMarkets orders...")
        await bot.cancel_all(tm_session)

        logging.info(
            "Cross-market engine started\n"
            f"  Book A (oracle) : Coinbase {CB_PRODUCT_ID}  {'[DRY-RUN]' if cb.dry_run else '[LIVE]'}\n"
            f"  Book B (slow)   : TrueMarkets {BASE_ASSET}/{QUOTE_ASSET}\n"
            f"  size            : {TRADE_SIZE_BTC} BTC   poll : {POLL_INTERVAL}s\n"
            f"  arb min edge    : {ARB_MIN_EDGE_BPS:.1f}bps (fees {TM_TAKER_FEE_BPS}+{CB_TAKER_FEE_BPS} + {ARB_FEE_BUFFER_BPS} buffer)\n"
            f"  maker improve   : {MAKER_IMPROVE_BPS}bps inside fair value\n"
            f"  max inv/venue   : ±{MAX_INV_BTC} BTC   loss limit : ${MAX_LOSS_USD}\n"
            f"  imbalance warn  : ±{IMBALANCE_WARN_BTC} BTC"
        )
        try:
            while not state["killed"]:
                try:
                    await cycle(bot, tm_session, cb, cb_session, book_a, book, limiter, state, size_str)
                except Exception as e:
                    logging.error(f"Cycle error: {e}", exc_info=True)
                await asyncio.sleep(POLL_INTERVAL)
        finally:
            for key in ("bid_oid", "ask_oid"):
                oid = state.get(key)
                if oid:
                    await bot.cancel_order(tm_session, oid)
            feed_task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Shutting down.")
