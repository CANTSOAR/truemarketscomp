"""
Smoke test: market buy $1 of BTC at best available price.
Run: python3 test.py
"""
import asyncio, os, sys, logging, aiohttp
from pathlib import Path
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from lib.execution import ExecutionClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
load_dotenv(ROOT_DIR / "keys" / ".env")

REST_URL = os.getenv("BASE_REST_URL", "https://api.truemarkets.co")
KEY_FILE = os.getenv("BOT_A_KEY_FILE", "./keys/truemarkets-api-key-edd1691b.json")

async def main():
    bot = ExecutionClient(key_file=KEY_FILE, base_url=REST_URL)
    async with aiohttp.ClientSession() as session:
        await bot.authenticate(session)
        logging.info("Placing MARKET BUY $1 of BTC...")
        order = await bot.place_order(
            session,
            base_asset="BTC",
            quote_asset="USDC",
            side="buy",
            qty="1",
            qty_unit="quote",
            order_type="market",
        )
        logging.info(f"Result: {order}")

if __name__ == "__main__":
    asyncio.run(main())
