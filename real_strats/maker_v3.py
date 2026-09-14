"""
maker_v3.py — make TrueMarkets around Coinbase without crossing external fair.

The strategy does one narrow thing:
  * stream Coinbase BTC-USD best bid / best ask;
  * poll TrueMarkets' current bid / ask;
  * quote a thinner TrueMarkets spread, but never bid above Coinbase's bid and
    never ask below Coinbase's ask.

It does not hedge. Coinbase is the adverse-selection guardrail; TrueMarkets'
own spread decides how far inside the local book we try to quote.

Run:  python3 real_strats/maker_v3.py
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import sys
import time
from collections import deque
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from lib.coinbase_feed import CoinbaseBookA
from lib.execution import ExecutionClient
from lib.leadlag import RateLimiter

load_dotenv(ROOT_DIR / "keys" / ".env")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[logging.FileHandler("maker_v3.log")],
    force=True,
)

# ── connectivity ──────────────────────────────────────────────────────────────
TM_REST_URL = os.getenv("BASE_REST_URL", "https://api.truemarkets.co")
TM_KEY_FILE = os.getenv("BOT_A_KEY_FILE", "./keys/truemarkets-api-key-edd1691b.json")
BASE_ASSET = os.getenv("MAKER_V3_BASE_ASSET", "BTC")
QUOTE_ASSET = os.getenv("MAKER_V3_QUOTE_ASSET", "USDC")

CB_WS_URL = os.getenv("COINBASE_WS_URL", "wss://ws-feed.exchange.coinbase.com")
CB_PRODUCT_ID = os.getenv("COINBASE_PRODUCT_ID", "BTC-USD")

# ── quote parameters ──────────────────────────────────────────────────────────
QUOTE_SIZE_BTC = float(os.getenv("MAKER_V3_SIZE_BTC", os.getenv("MAKER_SIZE_BTC", "0.0001")))
POLL_INTERVAL = float(os.getenv("MAKER_V3_POLL_SECS", "1.0"))
FAIR_MAX_STALE = float(os.getenv("MAKER_V3_CB_MAX_STALE", "3.0"))

# How often to refresh the TrueMarkets BBO snapshot (seconds). CB WebSocket
# drives pricing; TM BBO is fetched on this interval only as a cross-prevention
# clamp — we never bid above TM's ask or ask below TM's bid. At 10s this costs
# 12 req/min (2 req per refresh) vs 120 req/min if fetched every poll cycle.
TM_BBO_REFRESH_SECS = float(os.getenv("MAKER_V3_TM_BBO_REFRESH_SECS", "10.0"))

# TrueMarkets' BTC price bucket is currently $0.10 ticks in this repo. Override
# if the venue changes its increment or you quote a different asset bucket.
TICK_SIZE = float(os.getenv("MAKER_V3_TICK_SIZE", "0.1"))
PRICE_DECIMALS = int(os.getenv("MAKER_V3_PRICE_DECIMALS", "1"))

# Price movement needed before cancel/repost. Default is exactly one TM tick,
# which avoids replacing the same rounded price while still tracking CB BBO.
REPRICE_TICKS = float(os.getenv("MAKER_V3_REPRICE_TICKS", "5"))

# How aggressively to thin the current TrueMarkets spread. 0.25 means bid 25%
# of the spread above TM bid and ask 25% below TM ask, then clamp to Coinbase.
SPREAD_THIN_PCT = min(max(float(os.getenv("MAKER_V3_SPREAD_THIN_PCT", "0.25")), 0.0), 0.49)
MAX_TM_DISLOCATION_BPS = float(os.getenv("MAKER_V3_MAX_TM_DISLOCATION_BPS", "100.0"))

# Balance/risk gates. Set MAKER_V3_POST_BID/ASK=false to run one-sided.
POST_BID_ENABLED = os.getenv("MAKER_V3_POST_BID", "true").lower() == "true"
POST_ASK_ENABLED = os.getenv("MAKER_V3_POST_ASK", "true").lower() == "true"
CHECK_BALANCES = os.getenv("MAKER_V3_CHECK_BALANCES", "true").lower() == "true"
MAX_SESSION_POSITION_BTC = float(os.getenv("MAKER_V3_MAX_SESSION_POSITION_BTC", "0.001"))
MAX_SESSION_LOSS_USD = float(os.getenv("MAKER_V3_MAX_SESSION_LOSS_USD", "5.0"))
FEE_BPS = float(os.getenv("MAKER_V3_FEE_BPS", "4.0"))
BALANCE_CHECK_SECS = float(os.getenv("MAKER_V3_BALANCE_CHECK_SECS", "15.0"))
STATUS_CHECK_SECS = float(os.getenv("MAKER_V3_STATUS_CHECK_SECS", "5.0"))
ERROR_COOLDOWN_SECS = float(os.getenv("MAKER_V3_ERROR_COOLDOWN_SECS", "2.0"))

# TrueMarkets REST limit is 100 req/min. Pace at 95/min (5% margin);
# execution.py's proactive x-ratelimit-remaining check is the real backstop.
# Burst capacity covers worst-case cycle: 2 status + 1 balance + 2 cancel +
# 2 place = 7, so 10 lets a full cycle burst through without intra-cycle stalls.
REQ_PER_SEC = float(os.getenv("MAKER_V3_REQ_PER_SEC", str(95 / 60)))
REQ_BURST   = float(os.getenv("MAKER_V3_REQ_BURST",   "10"))

TERMINAL = {"complete", "canceled", "failed"}


class QuoteWinStats:
    def __init__(self) -> None:
        self.bid: deque[bool] = deque(maxlen=100)
        self.ask: deque[bool] = deque(maxlen=100)
        self.all: deque[bool] = deque(maxlen=100)

    def record(self, bid_mine: bool | None, ask_mine: bool | None) -> None:
        if bid_mine is not None:
            self.bid.append(bid_mine)
            self.all.append(bid_mine)
        if ask_mine is not None:
            self.ask.append(ask_mine)
            self.all.append(ask_mine)

    @staticmethod
    def _pct(values: deque[bool]) -> str:
        if not values:
            return "--"
        return f"{100.0 * sum(values) / len(values):4.0f}%"

    def bid_pct(self) -> str:
        return self._pct(self.bid)

    def ask_pct(self) -> str:
        return self._pct(self.ask)

    def all_pct(self) -> str:
        return self._pct(self.all)


def format_size(size: float) -> str:
    return f"{size:.8f}".rstrip("0").rstrip(".")


def floor_to_tick(price: float) -> float:
    return round(math.floor(price / TICK_SIZE) * TICK_SIZE, PRICE_DECIMALS)


def ceil_to_tick(price: float) -> float:
    return round(math.ceil(price / TICK_SIZE) * TICK_SIZE, PRICE_DECIMALS)


def format_price(price: float) -> str:
    return f"{price:.{PRICE_DECIMALS}f}"


def should_reprice(old_px: float | None, new_px: float) -> bool:
    if old_px is None:
        return True
    return abs(new_px - old_px) >= TICK_SIZE * REPRICE_TICKS - 1e-12


def is_same_price(a: float | None, b: float | None) -> bool:
    return a is not None and b is not None and abs(a - b) < max(1e-9, TICK_SIZE / 10)


def fmt_optional(price: float | None) -> str:
    return "n/a" if price is None else format_price(price)


def render_line(
    *,
    cb_bid: float | None,
    cb_ask: float | None,
    tm_bid: float | None,
    tm_ask: float | None,
    quote_bid: float | None,
    quote_ask: float | None,
    bid_mine: bool | None,
    ask_mine: bool | None,
    source: str,
    state: dict,
    book: "SessionBook",
    stats: QuoteWinStats,
    status: str,
) -> None:
    bid_flag = "Y" if bid_mine else "N" if bid_mine is not None else "-"
    ask_flag = "Y" if ask_mine else "N" if ask_mine is not None else "-"
    line = (
        f"{time.strftime('%H:%M:%S')} "
        f"CB {fmt_optional(cb_bid)}/{fmt_optional(cb_ask)} "
        f"TM {fmt_optional(tm_bid)}/{fmt_optional(tm_ask)} "
        f"ours {fmt_optional(state.get('bid_px') or quote_bid)}/{fmt_optional(state.get('ask_px') or quote_ask)} "
        f"mine B:{bid_flag} A:{ask_flag} "
        f"last100 mine:{stats.all_pct()} B:{stats.bid_pct()} A:{stats.ask_pct()} "
        f"pos {book.inventory_btc:+.6f} pnl {book.pnl(None if cb_bid is None or cb_ask is None else (cb_bid + cb_ask) / 2):+.2f} "
        f"fills {book.fills} "
        f"{source} {status}"
    )
    print("\r" + line[:240].ljust(240), end="", flush=True)


class SessionBook:
    def __init__(self) -> None:
        self.inventory_btc = 0.0
        self.cash_usdc = 0.0
        self.fills = 0

    def record_fill(self, side: str, qty: float, price: float, fee_bps: float) -> None:
        fee = qty * price * fee_bps / 10_000
        if side == "buy":
            self.inventory_btc += qty
            self.cash_usdc -= qty * price + fee
        else:
            self.inventory_btc -= qty
            self.cash_usdc += qty * price - fee
        self.fills += 1

    def pnl(self, mark_price: float | None) -> float:
        if mark_price is None:
            return self.cash_usdc
        return self.cash_usdc + self.inventory_btc * mark_price


async def cancel_tracked(
    bot: ExecutionClient,
    session: aiohttp.ClientSession,
    limiter: RateLimiter,
    state: dict,
    key: str,
) -> bool:
    oid = state.get(f"{key}_oid")
    if not oid:
        return True
    await limiter.acquire()
    ok = await bot.cancel_order(session, oid)
    logging.info("Cancel %s %s ok=%s", key, oid[:8], ok)
    if ok:
        state[f"{key}_oid"] = None
        state[f"{key}_px"] = None
        state[f"{key}_status_ts"] = 0.0
    return ok


async def detect_fills(
    bot: ExecutionClient,
    session: aiohttp.ClientSession,
    limiter: RateLimiter,
    state: dict,
    book: SessionBook,
) -> None:
    for key, side in (("bid", "buy"), ("ask", "sell")):
        oid = state.get(f"{key}_oid")
        if not oid:
            continue
        now = time.monotonic()
        last_check = state.get(f"{key}_status_ts", 0.0)
        if now - last_check < STATUS_CHECK_SECS:
            continue
        await limiter.acquire()
        status = await bot.get_order_status(session, oid)
        state[f"{key}_status_ts"] = now
        if status == "complete":
            price = state[f"{key}_px"]
            book.record_fill(side, QUOTE_SIZE_BTC, price, FEE_BPS)
            state["balances"] = None
            state["balances_ts"] = 0.0
            logging.info(
                "Fill %s %s BTC @ %s  session_pos=%+.8f BTC fills=%d",
                side.upper(),
                format_size(QUOTE_SIZE_BTC),
                format_price(price),
                book.inventory_btc,
                book.fills,
            )
            state[f"{key}_oid"] = None
            state[f"{key}_px"] = None
            state[f"{key}_status_ts"] = 0.0
        elif status in TERMINAL:
            logging.info("%s %s terminal status=%s", key.capitalize(), oid[:8], status)
            state[f"{key}_oid"] = None
            state[f"{key}_px"] = None
            state[f"{key}_status_ts"] = 0.0
        elif status not in {"pending", "active", None}:
            logging.warning("%s %s unexpected status=%s", key.capitalize(), oid[:8], status)


async def balances_allow(
    bot: ExecutionClient,
    session: aiohttp.ClientSession,
    limiter: RateLimiter,
    state: dict,
    bid_px: float,
    post_bid: bool,
    post_ask: bool,
) -> tuple[bool, bool]:
    if not CHECK_BALANCES or (not post_bid and not post_ask):
        return post_bid, post_ask

    now = time.monotonic()
    balances = state.get("balances")
    if balances is None or now - state.get("balances_ts", 0.0) >= BALANCE_CHECK_SECS:
        await limiter.acquire()
        balances = await bot.get_balances(session)
        state["balances"] = balances
        state["balances_ts"] = now
    quote_balance = balances.get(QUOTE_ASSET, 0.0) + balances.get("PYUSD", 0.0)
    base_balance = balances.get(BASE_ASSET, 0.0)
    required_quote = QUOTE_SIZE_BTC * bid_px * 1.01

    if post_bid and quote_balance < required_quote:
        logging.warning(
            "Bid suppressed: quote balance %.2f < required %.2f %s",
            quote_balance,
            required_quote,
            QUOTE_ASSET,
        )
        post_bid = False
    if post_ask and base_balance < QUOTE_SIZE_BTC:
        logging.warning(
            "Ask suppressed: %s balance %.8f < size %.8f",
            BASE_ASSET,
            base_balance,
            QUOTE_SIZE_BTC,
        )
        post_ask = False

    return post_bid, post_ask


async def get_truemarkets_bbo(
    bot: ExecutionClient,
    session: aiohttp.ClientSession,
    limiter: RateLimiter,
    size_str: str,
) -> tuple[float | None, float | None]:
    await limiter.acquire()
    sell_q = await bot.get_quote(
        session, BASE_ASSET, QUOTE_ASSET, side="sell", qty=size_str, qty_unit="base"
    )
    await limiter.acquire()
    buy_q = await bot.get_quote(
        session, BASE_ASSET, QUOTE_ASSET, side="buy", qty=size_str, qty_unit="base"
    )
    if not sell_q or not buy_q:
        logging.warning("TrueMarkets quote fetch failed; standing down.")
        return None, None

    try:
        tm_bid = float(sell_q["price"])
        tm_ask = float(buy_q["price"])
    except (KeyError, ValueError, TypeError):
        logging.warning("Unexpected TrueMarkets quote shape: sell=%s buy=%s", sell_q, buy_q)
        return None, None

    if tm_bid <= 0 or tm_ask <= 0:
        return None, None
    if tm_bid >= tm_ask:
        lo, hi = sorted((tm_bid, tm_ask))
        logging.warning("TrueMarkets quote sides inverted/locked: sell=%s buy=%s; using lo/hi.", tm_bid, tm_ask)
        return lo, hi
    return tm_bid, tm_ask


def choose_prices(
    cb_bid: float,
    cb_ask: float,
    tm_bid: float | None,
    tm_ask: float | None,
    state: dict,
) -> tuple[float, float, str]:
    cb_bid_px = floor_to_tick(cb_bid)
    cb_ask_px = ceil_to_tick(cb_ask)
    source = "coinbase-only"

    use_tm_book = tm_bid is not None and tm_ask is not None and tm_bid < tm_ask
    if use_tm_book:
        bid_echo = is_same_price(tm_bid, state.get("bid_px"))
        ask_echo = is_same_price(tm_ask, state.get("ask_px"))
        if bid_echo or ask_echo:
            logging.info(
                "TrueMarkets quote echoes our order (bid_echo=%s ask_echo=%s); not chasing our own quote.",
                bid_echo,
                ask_echo,
            )
            use_tm_book = False

    if use_tm_book:
        spread = tm_ask - tm_bid
        thin_bid = tm_bid + spread * SPREAD_THIN_PCT
        thin_ask = tm_ask - spread * SPREAD_THIN_PCT
        bid_px = floor_to_tick(min(cb_bid, thin_bid))
        ask_px = ceil_to_tick(max(cb_ask, thin_ask))
        source = "tm-thin+cb-clamp"
    else:
        bid_px = cb_bid_px
        ask_px = cb_ask_px

    return bid_px, ask_px, source


async def place_or_hold(
    bot: ExecutionClient,
    session: aiohttp.ClientSession,
    limiter: RateLimiter,
    state: dict,
    key: str,
    side: str,
    target_px: float,
    should_post: bool,
    size_str: str,
) -> None:
    existing_oid = state.get(f"{key}_oid")
    existing_px = state.get(f"{key}_px")

    if not should_post:
        ok = await cancel_tracked(bot, session, limiter, state, key)
        if not ok:
            state["cooldown_until"] = time.monotonic() + (bot.time_until_reset() or ERROR_COOLDOWN_SECS)
        return

    if existing_oid and not should_reprice(existing_px, target_px):
        return

    if existing_oid:
        ok = await cancel_tracked(bot, session, limiter, state, key)
        if not ok:
            logging.warning("Cancel failed for %s; not placing replacement.", key)
            state["cooldown_until"] = time.monotonic() + (bot.time_until_reset() or ERROR_COOLDOWN_SECS)
            return

    await limiter.acquire()
    order = await bot.place_order(
        session,
        base_asset=BASE_ASSET,
        quote_asset=QUOTE_ASSET,
        side=side,
        qty=size_str,
        qty_unit="base",
        order_type="limit",
        price=format_price(target_px),
    )
    if order and order.get("order_id"):
        state[f"{key}_oid"] = order["order_id"]
        state[f"{key}_px"] = target_px
        state[f"{key}_status_ts"] = time.monotonic()
        logging.info(
            "Post %s %s @ %s  order_id=%s status=%s",
            key.upper(),
            size_str,
            format_price(target_px),
            order["order_id"][:8],
            order.get("status"),
        )
    else:
        logging.error("Failed to post %s @ %s", key, format_price(target_px))


async def quote_cycle(
    bot: ExecutionClient,
    session: aiohttp.ClientSession,
    book_a: CoinbaseBookA,
    limiter: RateLimiter,
    state: dict,
    book: SessionBook,
    stats: QuoteWinStats,
    size_str: str,
) -> None:
    if book_a.best_bid is None or book_a.best_ask is None:
        render_line(
            cb_bid=None,
            cb_ask=None,
            tm_bid=None,
            tm_ask=None,
            quote_bid=None,
            quote_ask=None,
            bid_mine=None,
            ask_mine=None,
            source="waiting",
            state=state,
            book=book,
            stats=stats,
            status="waiting for Coinbase BBO",
        )
        return

    cooldown_remaining = state.get("cooldown_until", 0.0) - time.monotonic()
    if cooldown_remaining > 0:
        render_line(
            cb_bid=book_a.best_bid,
            cb_ask=book_a.best_ask,
            tm_bid=state.get("last_tm_bid"),
            tm_ask=state.get("last_tm_ask"),
            quote_bid=None,
            quote_ask=None,
            bid_mine=None,
            ask_mine=None,
            source="cooldown",
            state=state,
            book=book,
            stats=stats,
            status=f"REST cooldown {cooldown_remaining:.0f}s",
        )
        return

    if book_a.age() > FAIR_MAX_STALE:
        await cancel_tracked(bot, session, limiter, state, "bid")
        await cancel_tracked(bot, session, limiter, state, "ask")
        render_line(
            cb_bid=book_a.best_bid,
            cb_ask=book_a.best_ask,
            tm_bid=None,
            tm_ask=None,
            quote_bid=None,
            quote_ask=None,
            bid_mine=None,
            ask_mine=None,
            source="standdown",
            state=state,
            book=book,
            stats=stats,
            status=f"stale Coinbase BBO {book_a.age():.1f}s",
        )
        return

    await detect_fills(bot, session, limiter, state, book)

    mark = (book_a.best_bid + book_a.best_ask) / 2
    pnl = book.pnl(mark)
    if pnl <= -MAX_SESSION_LOSS_USD:
        state["killed"] = True
        await cancel_tracked(bot, session, limiter, state, "bid")
        await cancel_tracked(bot, session, limiter, state, "ask")
        render_line(
            cb_bid=book_a.best_bid,
            cb_ask=book_a.best_ask,
            tm_bid=state.get("last_tm_bid"),
            tm_ask=state.get("last_tm_ask"),
            quote_bid=None,
            quote_ask=None,
            bid_mine=None,
            ask_mine=None,
            source="killed",
            state=state,
            book=book,
            stats=stats,
            status=f"loss cap hit {pnl:+.2f}",
        )
        return

    # Refresh TM BBO on interval — CB WebSocket drives pricing, TM BBO is used
    # only to clamp quotes so we never cross TM's spread and become a taker.
    # On fetch failure keep the last known values; don't cooldown or stand down.
    now_mono = time.monotonic()
    if now_mono - state.get("tm_bbo_ts", 0.0) >= TM_BBO_REFRESH_SECS:
        fresh_bid, fresh_ask = await get_truemarkets_bbo(bot, session, limiter, size_str)
        if fresh_bid is not None and fresh_ask is not None:
            state["last_tm_bid"] = fresh_bid
            state["last_tm_ask"] = fresh_ask
        state["tm_bbo_ts"] = now_mono  # always advance timer, even on failure

    tm_bid = state.get("last_tm_bid")
    tm_ask = state.get("last_tm_ask")

    bid_mine = is_same_price(tm_bid, state.get("bid_px")) if tm_bid is not None else None
    ask_mine = is_same_price(tm_ask, state.get("ask_px")) if tm_ask is not None else None
    stats.record(bid_mine, ask_mine)

    bid_px, ask_px, price_source = choose_prices(
        book_a.best_bid,
        book_a.best_ask,
        tm_bid,
        tm_ask,
        state,
    )
    if bid_px >= ask_px:
        logging.warning(
            "Chosen quote crossed/locked: CB bid=%.2f ask=%.2f TM bid=%s ask=%s chosen bid=%s ask=%s; standing down.",
            book_a.best_bid,
            book_a.best_ask,
            "n/a" if tm_bid is None else format_price(tm_bid),
            "n/a" if tm_ask is None else format_price(tm_ask),
            format_price(bid_px),
            format_price(ask_px),
        )
        await cancel_tracked(bot, session, limiter, state, "bid")
        await cancel_tracked(bot, session, limiter, state, "ask")
        render_line(
            cb_bid=book_a.best_bid,
            cb_ask=book_a.best_ask,
            tm_bid=tm_bid,
            tm_ask=tm_ask,
            quote_bid=bid_px,
            quote_ask=ask_px,
            bid_mine=bid_mine,
            ask_mine=ask_mine,
            source=price_source,
            state=state,
            book=book,
            stats=stats,
            status="crossed; standing down",
        )
        return

    post_bid = POST_BID_ENABLED and book.inventory_btc + QUOTE_SIZE_BTC <= MAX_SESSION_POSITION_BTC
    post_ask = POST_ASK_ENABLED and book.inventory_btc - QUOTE_SIZE_BTC >= -MAX_SESSION_POSITION_BTC
    post_bid, post_ask = await balances_allow(bot, session, limiter, state, bid_px, post_bid, post_ask)

    await place_or_hold(bot, session, limiter, state, "bid", "buy", bid_px, post_bid, size_str)
    await place_or_hold(bot, session, limiter, state, "ask", "sell", ask_px, post_ask, size_str)

    status_parts = []
    if not post_bid:
        status_parts.append("bid off")
    if not post_ask:
        status_parts.append("ask off")
    render_line(
        cb_bid=book_a.best_bid,
        cb_ask=book_a.best_ask,
        tm_bid=tm_bid,
        tm_ask=tm_ask,
        quote_bid=bid_px,
        quote_ask=ask_px,
        bid_mine=bid_mine,
        ask_mine=ask_mine,
        source=price_source,
        state=state,
        book=book,
        stats=stats,
        status=", ".join(status_parts) if status_parts else "live",
    )


async def main() -> None:
    bot = ExecutionClient(key_file=TM_KEY_FILE, base_url=TM_REST_URL)
    book_a = CoinbaseBookA(ws_url=CB_WS_URL, product_id=CB_PRODUCT_ID)
    limiter = RateLimiter(rate_per_sec=REQ_PER_SEC, capacity=REQ_BURST)
    size_str = format_size(QUOTE_SIZE_BTC)
    state = {
        "bid_oid": None,
        "ask_oid": None,
        "bid_px": None,
        "ask_px": None,
        "bid_status_ts": 0.0,
        "ask_status_ts": 0.0,
        "balances": None,
        "balances_ts": 0.0,
        "cooldown_until": 0.0,
        "last_tm_bid": None,
        "last_tm_ask": None,
        "tm_bbo_ts": 0.0,
        "killed": False,
    }
    book = SessionBook()
    stats = QuoteWinStats()

    feed_task = asyncio.create_task(book_a.run())

    async with aiohttp.ClientSession() as session:
        await bot.authenticate(session)
        logging.info("Cancelling pre-existing TrueMarkets open orders...")
        await bot.cancel_all(session)
        logging.info(
            "maker_v3 started: TrueMarkets %s/%s size=%s, Coinbase %s guardrail, tick=%s, thin_pct=%.2f, poll=%.1fs, req=%.1f/min, max_loss=$%.2f",
            BASE_ASSET,
            QUOTE_ASSET,
            size_str,
            CB_PRODUCT_ID,
            TICK_SIZE,
            SPREAD_THIN_PCT,
            POLL_INTERVAL,
            REQ_PER_SEC * 60,
            MAX_SESSION_LOSS_USD,
        )
        try:
            while not state["killed"]:
                try:
                    await quote_cycle(bot, session, book_a, limiter, state, book, stats, size_str)
                except Exception as exc:
                    logging.error("Cycle error: %s", exc, exc_info=True)
                    render_line(
                        cb_bid=book_a.best_bid,
                        cb_ask=book_a.best_ask,
                        tm_bid=None,
                        tm_ask=None,
                        quote_bid=None,
                        quote_ask=None,
                        bid_mine=None,
                        ask_mine=None,
                        source="error",
                        state=state,
                        book=book,
                        stats=stats,
                        status=str(exc),
                    )
                await asyncio.sleep(POLL_INTERVAL)
        finally:
            await cancel_tracked(bot, session, limiter, state, "bid")
            await cancel_tracked(bot, session, limiter, state, "ask")
            print()
            feed_task.cancel()
            try:
                await feed_task
            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Shutting down.")
