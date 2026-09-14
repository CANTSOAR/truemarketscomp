"""
Two-bot maker/sniper coordinator for BTC/USDC.

Strategy
--------
One bot places a passive LIMIT SELL above the real market (so no external
participant ever fills it). The other bot immediately crosses it with a LIMIT
BUY at the same price, filling only against the maker. Roles alternate each
round so BTC and USDC ping-pong between the two accounts indefinitely.

Round even  →  Bot A is MAKER (limit sell), Bot B is SNIPER (limit buy)
Round odd   →  Bot B is MAKER (limit sell), Bot A is SNIPER (limit buy)

Price anchor
------------
  artificial_price = mid × (1 + PRICE_OFFSET_PCT)

At 10% above mid, no real buyer in the book will be at this price, so the
maker order sits untouched until our own sniper crosses it.

Bootstrap
---------
The sell leg always needs BTC. Before the first round the coordinator checks
Bot A's BTC balance; if it is below TRADE_SIZE_BTC it executes a small market
buy to seed the loop.
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.execution import ExecutionClient

TERMINAL_STATUSES = {"complete", "canceled", "failed"}


# ── price helpers ──────────────────────────────────────────────────────────────

async def fetch_mid(
    bot: ExecutionClient,
    session,
    base: str,
    quote: str,
    size: str,
) -> float | None:
    """Compute mid as average of buy-side and sell-side quote prices."""
    buy_q = await bot.get_quote(session, base, quote, side="buy",  qty=size, qty_unit="base")
    sell_q = await bot.get_quote(session, base, quote, side="sell", qty=size, qty_unit="base")
    if not buy_q or not sell_q:
        return None
    try:
        buy_price  = float(buy_q["price"])
        sell_price = float(sell_q["price"])
        return (buy_price + sell_price) / 2.0
    except (KeyError, ValueError, TypeError):
        logging.error(f"Unexpected quote response: buy={buy_q} sell={sell_q}")
        return None


# ── order polling ──────────────────────────────────────────────────────────────

async def wait_for_fill(
    bot: ExecutionClient,
    session,
    order_id: str,
    timeout: float,
    poll_interval: float = 0.75,
) -> str:
    """
    Poll GET /orders/{id}/status until a terminal status or timeout.
    Returns the final status string (or 'timeout' if it never settled).
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        status = await bot.get_order_status(session, order_id)
        if status in TERMINAL_STATUSES:
            return status
        await asyncio.sleep(poll_interval)
    return "timeout"


# ── bootstrap ─────────────────────────────────────────────────────────────────

async def ensure_btc(
    bot: ExecutionClient,
    session,
    base: str,
    quote: str,
    required_btc: float,
    label: str,
) -> bool:
    """
    If `bot` holds less than `required_btc`, do a market buy to top it up.
    Returns True if the bot now has enough BTC to proceed.
    """
    balances = await bot.get_balances(session)
    held = balances.get(base, 0.0)
    logging.info(f"[{label}] Balances: {balances}")

    if held >= required_btc:
        return True

    need_btc = required_btc - held
    # Market buy: qty_unit=base, smallest tradeable increment
    need_str = f"{need_btc:.8f}".rstrip("0").rstrip(".")
    logging.info(f"[{label}] Bootstrap: market-buying {need_str} {base}...")
    result = await bot.place_order(
        session,
        base_asset=base,
        quote_asset=quote,
        side="buy",
        qty=need_str,
        qty_unit="base",
        order_type="market",
    )
    if not result:
        logging.error(f"[{label}] Bootstrap buy failed.")
        return False

    logging.info(f"[{label}] Bootstrap buy placed: {result.get('order_id')} status={result.get('status')}")
    return True


# ── main loop ──────────────────────────────────────────────────────────────────

async def run(
    bot_a: ExecutionClient,
    bot_b: ExecutionClient,
    session_a,
    session_b,
    base_asset: str,
    quote_asset: str,
    trade_size_btc: float,
    price_offset_pct: float,
    round_delay: float,
    fill_timeout: float,
    max_loss_usd: float,
):
    size_str = f"{trade_size_btc:.8f}".rstrip("0").rstrip(".")

    # ── bootstrap: seed Bot A with BTC for round 0 ──────────────────────────
    ok = await ensure_btc(bot_a, session_a, base_asset, quote_asset, trade_size_btc, "A")
    if not ok:
        logging.error("Bootstrap failed — cannot start coordinator.")
        return

    cumulative_loss = 0.0
    round_num = 0

    while True:
        # ── kill-switch ──────────────────────────────────────────────────────
        if cumulative_loss >= max_loss_usd:
            logging.error(
                f"Cumulative loss ${cumulative_loss:.4f} ≥ limit ${max_loss_usd:.2f}. Stopping."
            )
            break

        # ── assign roles ─────────────────────────────────────────────────────
        if round_num % 2 == 0:
            maker,  maker_sess,  maker_label  = bot_a, session_a, "A"
            sniper, sniper_sess, sniper_label = bot_b, session_b, "B"
        else:
            maker,  maker_sess,  maker_label  = bot_b, session_b, "B"
            sniper, sniper_sess, sniper_label = bot_a, session_a, "A"

        logging.info(
            f"\n{'─'*60}\n"
            f"  Round {round_num + 1}  |  "
            f"MAKER=Bot {maker_label}  |  SNIPER=Bot {sniper_label}\n"
            f"{'─'*60}"
        )

        # ── fetch current mid and compute artificial limit price ─────────────
        mid = await fetch_mid(bot_a, session_a, base_asset, quote_asset, size_str)
        if mid is None:
            logging.error("Could not fetch mid price — retrying in 5 s...")
            await asyncio.sleep(5)
            continue

        limit_price = round(mid * (1.0 + price_offset_pct), 2)
        price_str   = f"{limit_price:.2f}"
        logging.info(
            f"  Mid=${mid:,.2f}  |  "
            f"Limit price=${limit_price:,.2f}  "
            f"({price_offset_pct*100:.0f}% above mid)"
        )

        # ── step 1: maker places LIMIT SELL ──────────────────────────────────
        logging.info(
            f"  [Bot {maker_label}] LIMIT SELL {size_str} {base_asset} @ {price_str}..."
        )
        maker_order = await maker.place_order(
            maker_sess,
            base_asset=base_asset,
            quote_asset=quote_asset,
            side="sell",
            qty=size_str,
            qty_unit="base",
            order_type="limit",
            price=price_str,
        )
        if not maker_order:
            logging.error(f"  [Bot {maker_label}] LIMIT SELL failed. Skipping round.")
            await asyncio.sleep(round_delay)
            round_num += 1
            continue

        maker_oid = maker_order.get("order_id")
        await asyncio.sleep(0.3)   # let the order land in the book

        # ── step 2: sniper crosses with LIMIT BUY at the same price ──────────
        logging.info(
            f"  [Bot {sniper_label}] LIMIT BUY  {size_str} {base_asset} @ {price_str}  (crossing)..."
        )
        sniper_order = await sniper.place_order(
            sniper_sess,
            base_asset=base_asset,
            quote_asset=quote_asset,
            side="buy",
            qty=size_str,
            qty_unit="base",
            order_type="limit",
            price=price_str,
        )
        if not sniper_order:
            logging.error(
                f"  [Bot {sniper_label}] LIMIT BUY failed. Cancelling maker order..."
            )
            if maker_oid:
                await maker.cancel_order(maker_sess, maker_oid)
            await asyncio.sleep(round_delay)
            round_num += 1
            continue

        sniper_oid = sniper_order.get("order_id")

        # ── step 3: wait for both legs to reach a terminal state ─────────────
        logging.info("  Waiting for fills...")
        maker_status, sniper_status = await asyncio.gather(
            wait_for_fill(maker,  maker_sess,  maker_oid,  fill_timeout),
            wait_for_fill(sniper, sniper_sess, sniper_oid, fill_timeout),
        )
        logging.info(
            f"  Maker  (Bot {maker_label})  → {maker_status}\n"
            f"  Sniper (Bot {sniper_label}) → {sniper_status}"
        )

        # ── step 4: clean up any stale orders ────────────────────────────────
        if maker_status == "timeout":
            logging.warning(f"  Maker order timed out — cancelling {maker_oid}...")
            await maker.cancel_order(maker_sess, maker_oid)

        if sniper_status == "timeout":
            logging.warning(f"  Sniper order timed out — cancelling {sniper_oid}...")
            await sniper.cancel_order(sniper_sess, sniper_oid)

        # Rough fee cost per round (2 fills × ~0.1% taker fee × notional)
        notional    = trade_size_btc * limit_price
        round_cost  = notional * 0.002   # conservative 0.2% round-trip
        cumulative_loss += round_cost
        logging.info(
            f"  Notional=${notional:.2f}  |  "
            f"Est. round cost=${round_cost:.4f}  |  "
            f"Cumulative cost=${cumulative_loss:.4f}"
        )

        await asyncio.sleep(round_delay)
        round_num += 1
