"""
maker_v2.py — Simple market maker: bid 5% above best bid, ask 5% below best ask.
Cancels and replaces every 15 seconds.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from dotenv import load_dotenv
import aiohttp

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from lib.execution import ExecutionClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
load_dotenv(ROOT_DIR / "keys" / ".env")

REST_URL    = os.getenv("BASE_REST_URL", "https://api.truemarkets.co")
KEY_FILE    = os.getenv("BOT_A_KEY_FILE", "./keys/truemarkets-api-key-edd1691b.json")
BASE_ASSET  = "BTC"
QUOTE_ASSET = "USDC"

QUOTE_SIZE = float(os.getenv("MAKER_SIZE_BTC", "0.0001"))
INTERVAL   = 15.0   # seconds between cancel-replace cycles
OFFSET_PCT = 0.05   # 5% offset from best bid/ask

TERMINAL = {"complete", "canceled", "failed"}

_bid_oid: str | None = None
_ask_oid: str | None = None


async def cancel_resting(bot: ExecutionClient, session: aiohttp.ClientSession):
    global _bid_oid, _ask_oid
    for oid in filter(None, [_bid_oid, _ask_oid]):
        await bot.cancel_order(session, oid)
    _bid_oid = None
    _ask_oid = None
    await bot.cancel_all(session)
    await asyncio.sleep(1.0)


async def quote_cycle(bot: ExecutionClient, session: aiohttp.ClientSession):
    global _bid_oid, _ask_oid

    size_str = f"{QUOTE_SIZE:.6f}".rstrip("0")

    sell_q, buy_q = await asyncio.gather(
        bot.get_quote(session, BASE_ASSET, QUOTE_ASSET, side="sell", qty=size_str, qty_unit="base"),
        bot.get_quote(session, BASE_ASSET, QUOTE_ASSET, side="buy",  qty=size_str, qty_unit="base"),
    )
    if not sell_q or not buy_q:
        logging.warning("Quote fetch failed — skipping cycle.")
        return

    best_bid = float(sell_q["price"])
    best_ask = float(buy_q["price"])
    spread   = best_ask - best_bid
    bid_px   = best_bid + OFFSET_PCT * spread
    ask_px   = best_ask - OFFSET_PCT * spread

    logging.info(
        f"best_bid={best_bid:,.2f}  best_ask={best_ask:,.2f}  spread={spread:.2f}  "
        f"→  posting bid={bid_px:,.2f}  ask={ask_px:,.2f}"
    )

    await cancel_resting(bot, session)

    order = await bot.place_order(
        session,
        base_asset=BASE_ASSET, quote_asset=QUOTE_ASSET,
        side="buy", qty=size_str, qty_unit="base",
        order_type="limit", price=f"{bid_px:.2f}",
    )
    _bid_oid = order.get("order_id") if order else None
    if _bid_oid:
        logging.info(f"Bid  {_bid_oid[:8]}… @ {bid_px:,.2f}")

    order = await bot.place_order(
        session,
        base_asset=BASE_ASSET, quote_asset=QUOTE_ASSET,
        side="sell", qty=size_str, qty_unit="base",
        order_type="limit", price=f"{ask_px:.2f}",
    )
    _ask_oid = order.get("order_id") if order else None
    if _ask_oid:
        logging.info(f"Ask  {_ask_oid[:8]}… @ {ask_px:,.2f}")


async def main():
    bot = ExecutionClient(key_file=KEY_FILE, base_url=REST_URL)
    async with aiohttp.ClientSession() as session:
        await bot.authenticate(session)
        logging.info("Cancelling pre-existing open orders...")
        await bot.cancel_all(session)
        await asyncio.sleep(0.5)

        logging.info(
            f"maker_v2 started  size={QUOTE_SIZE} BTC  interval={INTERVAL}s  "
            f"improving spread by {OFFSET_PCT*100:.0f}% on each side"
        )
        while True:
            try:
                await quote_cycle(bot, session)
            except Exception as e:
                logging.error(f"Cycle error: {e}", exc_info=True)
            await asyncio.sleep(INTERVAL)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Shutting down.")
