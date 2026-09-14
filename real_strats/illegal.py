import asyncio
import os
import sys
from pathlib import Path
import aiohttp
import logging
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from lib.execution import ExecutionClient
from lib import coordinator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)

load_dotenv(ROOT_DIR / "keys" / ".env")

# ── config ────────────────────────────────────────────────────────────────────
REST_URL        = os.getenv("BASE_REST_URL",    "https://api.truemarkets.co")
BOT_A_KEY_FILE  = os.getenv("BOT_A_KEY_FILE",   "./keys/truemarkets-api-key-edd1691b.json")
BOT_B_KEY_FILE  = os.getenv("BOT_B_KEY_FILE",   "./keys/truemarkets-api-key-bot-b.json")
SYMBOL          = os.getenv("TARGET_SYMBOL",    "BTC-USDC")
TRADE_SIZE_BTC  = float(os.getenv("TRADE_SIZE_BTC",  "0.0001"))
PRICE_OFFSET    = float(os.getenv("PRICE_OFFSET_PCT", "0.10"))
ROUND_DELAY     = float(os.getenv("ROUND_DELAY_SECS", "5"))
FILL_TIMEOUT    = float(os.getenv("FILL_TIMEOUT_SECS","30"))
MAX_LOSS_USD    = float(os.getenv("MAX_LOSS_USD",     "10.0"))

BASE_ASSET, QUOTE_ASSET = SYMBOL.split("-")


async def main():
    # ── validate both key files exist before opening any session ────────────
    for label, path in [("Bot A", BOT_A_KEY_FILE), ("Bot B", BOT_B_KEY_FILE)]:
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"{label} key file not found: {path}\n"
                f"Set BOT_{'A' if label == 'Bot A' else 'B'}_KEY_FILE in .env "
                f"to the JSON bundle downloaded from truemarkets.co → Settings → API Keys."
            )

    bot_a = ExecutionClient(key_file=BOT_A_KEY_FILE, base_url=REST_URL)
    bot_b = ExecutionClient(key_file=BOT_B_KEY_FILE, base_url=REST_URL)

    async with (
        aiohttp.ClientSession() as session_a,
        aiohttp.ClientSession() as session_b,
    ):
        logging.info("Authenticating Bot A...")
        await bot_a.authenticate(session_a)
        logging.info("Authenticating Bot B...")
        await bot_b.authenticate(session_b)

        logging.info(
            f"Starting ping-pong coordinator\n"
            f"  Symbol      : {SYMBOL}\n"
            f"  Trade size  : {TRADE_SIZE_BTC} BTC per leg\n"
            f"  Limit price : mid × {1 + PRICE_OFFSET:.2f}  ({PRICE_OFFSET*100:.0f}% above market)\n"
            f"  Round delay : {ROUND_DELAY}s\n"
            f"  Fill timeout: {FILL_TIMEOUT}s\n"
            f"  Loss limit  : ${MAX_LOSS_USD}"
        )

        await coordinator.run(
            bot_a=bot_a,
            bot_b=bot_b,
            session_a=session_a,
            session_b=session_b,
            base_asset=BASE_ASSET,
            quote_asset=QUOTE_ASSET,
            trade_size_btc=TRADE_SIZE_BTC,
            price_offset_pct=PRICE_OFFSET,
            round_delay=ROUND_DELAY,
            fill_timeout=FILL_TIMEOUT,
            max_loss_usd=MAX_LOSS_USD,
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Shutting down.")
    except FileNotFoundError as e:
        logging.error(str(e))
