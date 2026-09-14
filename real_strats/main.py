"""
main.py — hybrid market-maker / lead-lag taker for BTC/USDC on TrueMarkets.

Strategy
────────
Coinbase leads price discovery; TrueMarkets lags and has a wide resting
spread (~59bps observed). Each cycle:

  1. Check TrueMarkets' actual best bid/ask against the live Coinbase fair
     value. If it has drifted far enough to clear the spread + fees (the
     "lead signal" is significant), cancel any resting quotes and TAKE
     liquidity directly with a market order — this is leadlag.py's logic.
  2. Otherwise, rest passive limit quotes priced off the Coinbase fair value
     (not off TrueMarkets' own laggy mid — quoting off the leading reference
     means we're never the stale quote getting picked off by someone faster).
     Quotes are skewed by inventory, same spirit as maker.py's AS skew, just
     without the volatility-estimation machinery since the leading reference
     replaces the need for it.

Reuses FairValueFeed / RateLimiter / Book from leadlag.py rather than
duplicating them — same Coinbase feed, same rate-limit pacing tuned against
TrueMarkets' observed limits.

Known constraints baked in (confirmed this session):
  • qty_unit="base" for both market and limit orders (engineering-confirmed;
    the spec's "market buy requires qty_unit=quote" does not hold here).
  • Lot size 0.0001 BTC / tick $0.1 — current price bucket only
    ($10,000–$99,999.90). If BTC crosses a bucket boundary, LOT_SIZE/TICK_SIZE
    below need updating to match the new bucket.
  • Buys are funded by a silent PYUSD→USDC conversion (engineering-confirmed)
    — no pre-trade USDC balance gate needed. Sells still need BTC on hand.

Run:  python3 main.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections import deque
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from lib.execution import ExecutionClient
from lib.leadlag import FairValueFeed, RateLimiter, Book, LEAD_WS_URL, LEAD_PRODUCT

# WARNING+ only on the console — routine per-cycle status goes through the
# live terminal render below instead of the logger. basicConfig() only
# configures the root logger on its first call, and importing leadlag
# triggers its own basicConfig(INFO) first — so setLevel() directly, which
# always takes effect regardless of who called basicConfig first.
logging.basicConfig(format="%(asctime)s  %(levelname)-8s  %(message)s")
logging.getLogger().setLevel(logging.WARNING)
load_dotenv(ROOT_DIR / "keys" / ".env")

# Every place/cancel order action, to a file — independent of the console
# level above, so it's not lost to the live render taking over the screen.
trade_log = logging.getLogger("trades")
trade_log.setLevel(logging.INFO)
trade_log.addHandler(logging.FileHandler("trades.log"))
trade_log.propagate = False

# ── live terminal view ───────────────────────────────────────────────────────
CLEAR  = "\033[2J\033[H"
GREEN  = "\033[92m"
RED    = "\033[91m"
CYAN   = "\033[96m"
YELLOW = "\033[93m"
DIM    = "\033[2m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

HISTORY: deque = deque(maxlen=15)   # newest appended to the left

# Two independent win-rate metrics, since they can disagree:
#   *_wins/*_total      — does /quotes (a "non-binding price preview" per the
#                          spec, not necessarily a literal book read) happen
#                          to match our posted price.
#   *_active/*_active_total — did the order itself actually reach "active"
#                          status (exchange-confirmed acceptance into the
#                          book), checked via GET /orders/{id}/status. This
#                          is ground truth; the quote-match metric isn't.
STATS = {
    "bid_wins": 0, "bid_total": 0, "ask_wins": 0, "ask_total": 0,
    "bid_active": 0, "bid_active_total": 0, "ask_active": 0, "ask_active_total": 0,
}

# Latest real data from the (rate-limited, slower) fetch cycle. The display
# loop renders from this every second so the screen visibly ticks even while
# a cycle is still waiting on the rate limiter — refresh rate and fetch rate
# are deliberately decoupled.
latest: dict = {
    "fair": None, "tm_bid": None, "tm_ask": None,
    "status_line": None, "last_error": None, "ts": time.time(),
}


def update_latest(fair, tm_bid, tm_ask, status_line, last_error=None) -> None:
    latest.update(fair=fair, tm_bid=tm_bid, tm_ask=tm_ask,
                   status_line=status_line, last_error=last_error, ts=time.time())


def render(state, book) -> None:
    fair, tm_bid, tm_ask = latest["fair"], latest["tm_bid"], latest["tm_ask"]
    mine_bid = state.get("bid_px") is not None and tm_bid is not None and abs(tm_bid - state["bid_px"]) < 1e-6
    mine_ask = state.get("ask_px") is not None and tm_ask is not None and abs(tm_ask - state["ask_px"]) < 1e-6
    fair_str = f"{fair:>14,.2f}" if fair is not None else f"{'—':>14}"

    lines = [
        CLEAR,
        f"{BOLD}  BTC/USDC — hybrid market-maker / lead-lag{RESET}",
        "",
        f"  {CYAN}Coinbase fair{RESET}   {fair_str}",
    ]
    if tm_bid is not None and tm_ask is not None:
        lines += [
            f"  {GREEN}Best Bid{RESET}       {tm_bid:>14,.1f}  "
            f"{GREEN + '◀ MINE' + RESET if mine_bid else DIM + '(other)' + RESET}",
            f"  {RED}Best Ask{RESET}       {tm_ask:>14,.1f}  "
            f"{RED + '◀ MINE' + RESET if mine_ask else DIM + '(other)' + RESET}",
        ]
    else:
        lines += [f"  {DIM}Best Bid / Ask     waiting for quote…{RESET}"]

    pnl = book.pnl(fair) if fair is not None else 0.0
    inv_color = GREEN if book.inventory_btc > 0 else RED if book.inventory_btc < 0 else DIM
    lines += [
        "",
        f"  {DIM}inventory{RESET} {inv_color}{book.inventory_btc:+.6f} BTC{RESET}   "
        f"{DIM}pnl{RESET} {GREEN if pnl >= 0 else RED}${pnl:+.4f}{RESET}   "
        f"{DIM}trades={book.trades}{RESET}",
    ]
    bid_wr = f"{STATS['bid_wins']}/{STATS['bid_total']} ({100*STATS['bid_wins']/STATS['bid_total']:.0f}%)" if STATS["bid_total"] else "—"
    ask_wr = f"{STATS['ask_wins']}/{STATS['ask_total']} ({100*STATS['ask_wins']/STATS['ask_total']:.0f}%)" if STATS["ask_total"] else "—"
    bid_ar = f"{STATS['bid_active']}/{STATS['bid_active_total']} ({100*STATS['bid_active']/STATS['bid_active_total']:.0f}%)" if STATS["bid_active_total"] else "—"
    ask_ar = f"{STATS['ask_active']}/{STATS['ask_active_total']} ({100*STATS['ask_active']/STATS['ask_active_total']:.0f}%)" if STATS["ask_active_total"] else "—"
    lines.append(f"  {DIM}quote-match  {RESET}bid {GREEN}{bid_wr}{RESET}   ask {RED}{ask_wr}{RESET}  {DIM}(/quotes may not reflect the book){RESET}")
    lines.append(f"  {DIM}active-status{RESET}bid {GREEN}{bid_ar}{RESET}   ask {RED}{ask_ar}{RESET}  {DIM}(order actually accepted into book){RESET}")

    if latest["status_line"]:
        lines.append(f"  {YELLOW}{latest['status_line']}{RESET}")
    if latest["last_error"]:
        lines.append(f"  {RED}last error: {latest['last_error']}{RESET}")

    lines += [
        "",
        f"  {DIM}{'TIME':^10} {'FAIR':>12} {'TM BID':>12} {'TM ASK':>12} "
        f"{'OUR BID':>12} {'OUR ASK':>12}  ACTION{RESET}",
        f"  {DIM}{'─'*88}{RESET}",
    ]
    for h in HISTORY:
        t_str = time.strftime("%H:%M:%S", time.localtime(h["ts"]))
        fmt = lambda v: f"{v:>12,.1f}" if v is not None else f"{'—':>12}"
        lines.append(
            f"  {DIM}{t_str:^10}{RESET} {fmt(h['fair'])} {fmt(h['tm_bid'])} {fmt(h['tm_ask'])} "
            f"{fmt(h['bid_px'])} {fmt(h['ask_px'])}  {h['action']}"
        )

    age = time.time() - latest["ts"]
    lines.append(f"\n  {DIM}data refreshed {age:4.1f}s ago · screen ticks every 1s · ctrl-c to quit{RESET}")
    print("\n".join(lines), end="", flush=True)


async def display_loop(state: dict, book: Book) -> None:
    while True:
        render(state, book)
        await asyncio.sleep(1.0)

# ── connectivity ──────────────────────────────────────────────────────────────
REST_URL    = os.getenv("BASE_REST_URL", "https://api.truemarkets.co")
KEY_FILE    = os.getenv("BOT_A_KEY_FILE", "./keys/truemarkets-api-key-edd1691b.json")
BASE_ASSET  = "BTC"
QUOTE_ASSET = "USDC"

# ── current price-bucket constants (BTC $10,000–$99,999.90) ───────────────────
LOT_SIZE  = 0.0001   # base increment
TICK_SIZE = 0.1      # quote increment

# ── strategy parameters ───────────────────────────────────────────────────────
TRADE_SIZE_BTC     = float(os.getenv("MM_SIZE_BTC",        str(LOT_SIZE)))
QUOTE_INTERVAL      = float(os.getenv("MM_INTERVAL_SECS",   "1.0"))   # the rate limiter, not this, is the real pacing
HALF_SPREAD_BPS     = float(os.getenv("MM_HALF_SPREAD_BPS", "20.0"))   # passive quote half-spread off fair
SKEW_BPS_MAX        = float(os.getenv("MM_SKEW_BPS_MAX",    "10.0"))   # max inventory skew applied to reservation price
# Improvement over the current book when book-relative pricing wins (see
# cycle()). Was a flat 1 tick, which the live book (observed moving $30+/sec
# against a $225 spread) blows through almost immediately. 10% of the current
# spread scales with actual market movement instead of a fixed tiny amount.
BOOK_BUFFER_PCT     = float(os.getenv("MM_BOOK_BUFFER_PCT", "10.0"))
MAX_INV_BTC         = float(os.getenv("MM_MAX_INV_BTC",     str(3 * LOT_SIZE)))
EDGE_THRESHOLD_BPS  = float(os.getenv("MM_EDGE_BPS",        "15.0"))   # min edge over fair before taking
TAKER_FEE_BPS       = float(os.getenv("MM_TAKER_FEE_BPS",   "10.0"))
FAIR_MAX_STALE      = float(os.getenv("MM_FAIR_MAX_STALE",  "5.0"))
MAX_LOSS_USD        = float(os.getenv("MM_MAX_LOSS_USD",    "5.0"))    # kill switch — small account, small budget

TERMINAL = {"complete", "canceled", "failed"}


def round_to_tick(price: float) -> float:
    return round(round(price / TICK_SIZE) * TICK_SIZE, 1)


# ── order lifecycle helpers ────────────────────────────────────────────────────

async def _wait_for_status(
    bot: ExecutionClient, session: aiohttp.ClientSession, oid: str, timeout: float = 15.0,
) -> str:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        s = await bot.get_order_status(session, oid)
        if s in TERMINAL or s == "active":
            return s
        await asyncio.sleep(1.5)
    return "timeout"


async def cancel_resting(
    bot: ExecutionClient, session: aiohttp.ClientSession, limiter: RateLimiter, oids: list[str],
) -> None:
    for oid in filter(None, oids):
        await limiter.acquire()
        ok = await bot.cancel_order(session, oid)
        trade_log.info(f"CANCEL order_id={oid} ok={ok}")


# ── one decision cycle ──────────────────────────────────────────────────────────

async def cycle(
    bot: ExecutionClient,
    session: aiohttp.ClientSession,
    feed: FairValueFeed,
    book: Book,
    limiter: RateLimiter,
    size_str: str,
    state: dict,
) -> None:
    fair = feed.value
    if fair is None or feed.age() > FAIR_MAX_STALE:
        update_latest(fair, None, None, "waiting for lead price…")
        return

    await limiter.acquire()
    sell_q = await bot.get_quote(session, BASE_ASSET, QUOTE_ASSET, side="sell", qty=size_str, qty_unit="base")
    await limiter.acquire()
    buy_q = await bot.get_quote(session, BASE_ASSET, QUOTE_ASSET, side="buy", qty=size_str, qty_unit="base")
    if not sell_q or not buy_q:
        update_latest(fair, None, None, None, last_error="TrueMarkets quote fetch failed (rate-limited?)")
        return
    tm_bid = float(sell_q["price"])   # proceeds if we sell on TrueMarkets
    tm_ask = float(buy_q["price"])    # cost if we buy on TrueMarkets

    # win-rate: did the bid/ask we posted last cycle turn out to actually be
    # the best in the book this cycle? Measured before cancel_resting below
    # clears state["bid_px"]/["ask_px"]. NOTE: tm_bid/tm_ask come from
    # /quotes, which the spec calls a "non-binding price preview" — it may
    # not be a literal order-book top-of-book read. If win-rate looks wrong,
    # check trades.log: it logs every (tm_bid, our bid_px, match) triple, so
    # you can see whether quotes are actually tracking our resting order or
    # behaving like an independently-computed indicative price.
    if state.get("bid_px") is not None:
        STATS["bid_total"] += 1
        bid_win = abs(tm_bid - state["bid_px"]) < 1e-6
        STATS["bid_wins"] += int(bid_win)
        trade_log.info(f"WINCHECK bid our={state['bid_px']} tm_bid={tm_bid} win={bid_win}")
    if state.get("ask_px") is not None:
        STATS["ask_total"] += 1
        ask_win = abs(tm_ask - state["ask_px"]) < 1e-6
        STATS["ask_wins"] += int(ask_win)
        trade_log.info(f"WINCHECK ask our={state['ask_px']} tm_ask={tm_ask} win={ask_win}")

    # Captured before cancel_resting clears state["bid_px"]/["ask_px"] below —
    # needed downstream to detect when tm_bid/tm_ask is just an echo of our
    # own resting order rather than genuine external competition (see the
    # self-chase fix in the passive-quoting section).
    prev_bid_px = state.get("bid_px")
    prev_ask_px = state.get("ask_px")

    buy_edge_bps  = (fair - tm_ask) / fair * 1e4
    sell_edge_bps = (tm_bid - fair) / fair * 1e4
    min_edge = EDGE_THRESHOLD_BPS + TAKER_FEE_BPS

    q = book.inventory_btc
    take_side = None
    if buy_edge_bps >= sell_edge_bps:
        if buy_edge_bps >= min_edge and q + TRADE_SIZE_BTC <= MAX_INV_BTC:
            take_side = "buy"
    if take_side is None and sell_edge_bps >= min_edge and q - TRADE_SIZE_BTC >= -MAX_INV_BTC:
        take_side = "sell"

    # active-status: ground-truth check, independent of /quotes — did the
    # order actually get exchange-confirmed acceptance into the book before
    # we cancel it below. Checked right before cancellation so it's had the
    # full previous cycle's duration to settle into whatever state it reached.
    if state.get("bid_oid"):
        await limiter.acquire()
        s = await bot.get_order_status(session, state["bid_oid"])
        STATS["bid_active_total"] += 1
        STATS["bid_active"] += int(s == "active")
        trade_log.info(f"STATUSCHECK bid order_id={state['bid_oid']} status={s}")
    if state.get("ask_oid"):
        await limiter.acquire()
        s = await bot.get_order_status(session, state["ask_oid"])
        STATS["ask_active_total"] += 1
        STATS["ask_active"] += int(s == "active")
        trade_log.info(f"STATUSCHECK ask order_id={state['ask_oid']} status={s}")

    # always clear resting quotes before acting, so inventory accounting and
    # available balance reflect reality before we place anything new
    await cancel_resting(bot, session, limiter, [state.get("bid_oid"), state.get("ask_oid")])
    state["bid_oid"] = None
    state["ask_oid"] = None
    state["bid_px"] = None
    state["ask_px"] = None

    if take_side == "sell":
        await limiter.acquire()
        balances = await bot.get_balances(session)
        if balances.get(BASE_ASSET, 0.0) < TRADE_SIZE_BTC:
            take_side = None

    if take_side is not None:
        price = tm_ask if take_side == "buy" else tm_bid
        edge_bps = buy_edge_bps if take_side == "buy" else sell_edge_bps
        await limiter.acquire()
        order = await bot.place_order(
            session, base_asset=BASE_ASSET, quote_asset=QUOTE_ASSET,
            side=take_side, qty=size_str, qty_unit="base", order_type="market",
        )
        last_error = None
        if order:
            fee_usd = TRADE_SIZE_BTC * price * (TAKER_FEE_BPS / 1e4)
            book.record(take_side, TRADE_SIZE_BTC, price, fee_usd)
            action = f"TAKE {take_side.upper()} @ {price:,.1f} (edge {edge_bps:+.1f}bps)"
        else:
            action = f"TAKE {take_side.upper()} FAILED"
            last_error = "take order failed"
        trade_log.info(f"MAKE take side={take_side} price={price} qty={size_str} "
                        f"order_id={order.get('order_id') if order else None} "
                        f"status={order.get('status') if order else None}")
        HISTORY.appendleft({"ts": time.time(), "fair": fair, "tm_bid": tm_bid, "tm_ask": tm_ask,
                             "bid_px": None, "ask_px": None, "action": action})
        update_latest(fair, tm_bid, tm_ask, action, last_error=last_error)
        return

    # passive quoting: take whichever of (fair-value anchor, a buffer inside
    # the current TrueMarkets book) is more aggressive, so we track a tight
    # book instead of sitting on a stale fixed band — but clamp to the
    # reservation price so we can never quote through fair value chasing the
    # book (that's the adverse-selection guard against pure book-pegging).
    # The book buffer is a % of the live spread, not a flat tick — the book
    # has been observed moving $30+/sec, which a fixed $0.10 tick can't survive.
    #
    # Self-chase guard: trades.log showed tm_bid exactly echoing our own
    # last bid_px every cycle (confirmed: on this thin book, /quotes reflects
    # our own resting order once it's best). Without this guard,
    # "book_bid = tm_bid + buffer" reads back our own price and adds another
    # buffer on top of it every cycle, ratcheting our bid upward against no
    # real competition (observed climbing $0.60-$9.50/cycle in trades.log).
    # Only apply the book-chase term when tm_bid/tm_ask actually differs from
    # what we ourselves posted last cycle — otherwise hold steady.
    inv_frac = max(-1.0, min(1.0, q / MAX_INV_BTC)) if MAX_INV_BTC else 0.0
    reservation = fair * (1.0 - inv_frac * SKEW_BPS_MAX / 1e4)
    fair_bid = reservation * (1.0 - HALF_SPREAD_BPS / 1e4)
    fair_ask = reservation * (1.0 + HALF_SPREAD_BPS / 1e4)
    book_buffer = (tm_ask - tm_bid) * (BOOK_BUFFER_PCT / 100.0)
    bid_is_echo = prev_bid_px is not None and abs(tm_bid - prev_bid_px) < 1e-6
    ask_is_echo = prev_ask_px is not None and abs(tm_ask - prev_ask_px) < 1e-6
    book_bid = prev_bid_px if bid_is_echo else tm_bid + book_buffer
    book_ask = prev_ask_px if ask_is_echo else tm_ask - book_buffer
    bid_px = round_to_tick(min(max(fair_bid, book_bid), reservation))
    ask_px = round_to_tick(max(min(fair_ask, book_ask), reservation))

    post_bid = q + TRADE_SIZE_BTC <= MAX_INV_BTC
    post_ask = q - TRADE_SIZE_BTC >= -MAX_INV_BTC
    if post_ask:
        await limiter.acquire()
        balances = await bot.get_balances(session)
        if balances.get(BASE_ASSET, 0.0) < TRADE_SIZE_BTC:
            post_ask = False

    last_error = None
    if post_bid:
        await limiter.acquire()
        order = await bot.place_order(
            session, base_asset=BASE_ASSET, quote_asset=QUOTE_ASSET,
            side="buy", qty=size_str, qty_unit="base",
            order_type="limit", price=f"{bid_px:.1f}",
        )
        trade_log.info(f"MAKE bid price={bid_px} qty={size_str} "
                        f"order_id={order.get('order_id') if order else None} "
                        f"status={order.get('status') if order else None}")
        if order and order.get("order_id"):
            state["bid_oid"] = order["order_id"]
            state["bid_px"] = bid_px
        else:
            last_error = last_error or "bid order failed"

    if post_ask:
        await limiter.acquire()
        order = await bot.place_order(
            session, base_asset=BASE_ASSET, quote_asset=QUOTE_ASSET,
            side="sell", qty=size_str, qty_unit="base",
            order_type="limit", price=f"{ask_px:.1f}",
        )
        trade_log.info(f"MAKE ask price={ask_px} qty={size_str} "
                        f"order_id={order.get('order_id') if order else None} "
                        f"status={order.get('status') if order else None}")
        if order and order.get("order_id"):
            state["ask_oid"] = order["order_id"]
            state["ask_px"] = ask_px
        else:
            last_error = last_error or "ask order failed"

    action = f"PASSIVE bid={'Y' if post_bid else '-'} ask={'Y' if post_ask else '-'}"
    HISTORY.appendleft({"ts": time.time(), "fair": fair, "tm_bid": tm_bid, "tm_ask": tm_ask,
                         "bid_px": bid_px if post_bid else None, "ask_px": ask_px if post_ask else None,
                         "action": action})
    update_latest(fair, tm_bid, tm_ask, action, last_error=last_error)


# ── entry point ───────────────────────────────────────────────────────────────

async def main():
    bot = ExecutionClient(key_file=KEY_FILE, base_url=REST_URL)
    feed = FairValueFeed(LEAD_WS_URL, LEAD_PRODUCT)
    book = Book()
    # Confirmed with TrueMarkets: 100 req/min across the REST APIs. Pace at
    # 95/min (5% margin — live-checked via x-ratelimit-remaining headers, see
    # execution.py's proactive throttle, which is the real backstop; this
    # limiter just paces cycles). Capacity sized to a full cycle's worst-case
    # call count (2 quotes + up to 2 status checks + up to 2 cancels + up to
    # 1 balance check + up to 2 orders = 9) so a cycle bursts through
    # back-to-back instead of waiting ~1/rate between every single call.
    limiter = RateLimiter(rate_per_sec=95 / 60, capacity=12)
    size_str = f"{TRADE_SIZE_BTC:.8f}".rstrip("0").rstrip(".")
    state: dict = {"bid_oid": None, "ask_oid": None, "bid_px": None, "ask_px": None}

    feed_task = asyncio.create_task(feed.run())
    display_task = asyncio.create_task(display_loop(state, book))

    async with aiohttp.ClientSession() as session:
        await bot.authenticate(session)
        await bot.cancel_all(session)
        try:
            while True:
                try:
                    await cycle(bot, session, feed, book, limiter, size_str, state)
                    fair = feed.value
                    if fair is not None and book.pnl(fair) <= -MAX_LOSS_USD:
                        update_latest(fair, latest["tm_bid"], latest["tm_ask"],
                                      f"KILL SWITCH: PnL ${book.pnl(fair):.2f} ≤ −${MAX_LOSS_USD}. Stopped.")
                        render(state, book)
                        break
                except Exception as e:
                    update_latest(latest["fair"], latest["tm_bid"], latest["tm_ask"],
                                  latest["status_line"], last_error=f"cycle error: {e}")
                await asyncio.sleep(QUOTE_INTERVAL)
        finally:
            await cancel_resting(bot, session, limiter, [state.get("bid_oid"), state.get("ask_oid")])
            feed_task.cancel()
            display_task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Shutting down.")
