"""
Live quote viewer — polls bid/ask from the REST quote endpoint and streams
it to the terminal. No public WebSocket is available on truemarkets.co.

Run: python3 view.py
"""
import asyncio, json, os, sys, time, logging
from pathlib import Path
import aiohttp
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from lib.execution import ExecutionClient

load_dotenv(ROOT_DIR / "keys" / ".env")
logging.disable(logging.CRITICAL)   # suppress auth noise

REST_URL  = os.getenv("BASE_REST_URL", "https://api.truemarkets.co")
KEY_FILE  = os.getenv("BOT_A_KEY_FILE", "./keys/truemarkets-api-key-edd1691b.json")
SYMBOL    = os.getenv("TARGET_SYMBOL", "BTC-USDC")
BASE, QUOTE = SYMBOL.split("-")
INTERVAL  = 1.0          # seconds between polls
SIZE      = "0.001"      # BTC size used for the quote

# ── ANSI helpers ──────────────────────────────────────────────────────────────
CLEAR  = "\033[2J\033[H"
RED    = "\033[91m"
GREEN  = "\033[92m"
CYAN   = "\033[96m"
DIM    = "\033[2m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

history: list[tuple[float, float, float]] = []   # (ts, bid, ask)
MAX_HIST = 20

def render(bid: float, ask: float):
    now   = time.time()
    mid   = (bid + ask) / 2
    spread = ask - bid
    history.append((now, bid, ask))
    if len(history) > MAX_HIST:
        history.pop(0)

    lines = [
        CLEAR,
        f"{BOLD}  {BASE}/{QUOTE}  live quote{RESET}  {DIM}(polling REST){RESET}",
        "",
        f"  {RED}Ask   {ask:>14,.2f}  USDC{RESET}",
        f"  {DIM}Spread{'':5}{spread:>10,.2f}  USDC{RESET}",
        f"  {GREEN}Bid   {bid:>14,.2f}  USDC{RESET}",
        "",
        f"  {CYAN}Mid   {mid:>14,.2f}  USDC{RESET}",
        "",
        f"  {DIM}{'─'*36}{RESET}",
        f"  {DIM}{'TIME':^10}  {'BID':>12}  {'ASK':>12}{RESET}",
        f"  {DIM}{'─'*36}{RESET}",
    ]
    for ts, b, a in reversed(history[-10:]):
        t_str = time.strftime("%H:%M:%S", time.localtime(ts))
        lines.append(f"  {DIM}{t_str}  {b:>12,.2f}  {a:>12,.2f}{RESET}")

    lines.append(f"\n  {DIM}updated {time.strftime('%H:%M:%S')}  ctrl-c to quit{RESET}")
    print("\n".join(lines), end="", flush=True)


async def poll(bot: ExecutionClient, session: aiohttp.ClientSession):
    while True:
        try:
            sell_q, buy_q = await asyncio.gather(
                bot.get_quote(session, BASE, QUOTE, side="sell", qty=SIZE, qty_unit="base"),
                bot.get_quote(session, BASE, QUOTE, side="buy",  qty=SIZE, qty_unit="base"),
            )
            if sell_q and buy_q:
                bid = float(sell_q["price"])   # sell quote → effective bid
                ask = float(buy_q["price"])    # buy  quote → effective ask
                render(bid, ask)
        except Exception as e:
            print(f"\n  [error] {e}", flush=True)
        await asyncio.sleep(INTERVAL)


async def main():
    bot = ExecutionClient(key_file=KEY_FILE, base_url=REST_URL)
    async with aiohttp.ClientSession() as session:
        await bot.authenticate(session)
        await poll(bot, session)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nDone.")
