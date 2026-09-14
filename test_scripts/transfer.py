"""
transfer.py — move funds between your two accounts: TrueMarkets (DeFi
custody, on-chain) and a destination address you control (e.g. your
Coinbase deposit address), via TrueMarkets' /transfers endpoint.

⚠️  THIS SENDS A REAL ON-CHAIN TRANSACTION when you run `send`. There is no
    undo — sending the wrong asset to the wrong network, or to an address
    you don't actually control, can mean the funds are unrecoverable. This
    script never guesses or hardcodes a destination address: you must paste
    in your own, copied directly from the receiving account.

    To get a Coinbase deposit address: open Coinbase -> the asset (e.g.
    USDC) -> Receive -> copy BOTH the address and the network name. The
    network must exactly match the `chain` you pass here, or the deposit
    can be lost.

Usage:
    # 1. find the asset_id + supported chain(s) for what you want to send
    python3 transfer.py list-assets [SYMBOL]

    # 2. send — prints a summary and requires typing SEND to confirm
    python3 transfer.py send SYMBOL CHAIN QTY TO_ADDRESS

    # 3. check on a transfer afterwards
    python3 transfer.py status TRANSFER_ID
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from lib.execution import ExecutionClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
load_dotenv(ROOT_DIR / "keys" / ".env")

REST_URL = os.getenv("BASE_REST_URL", "https://api.truemarkets.co")
KEY_FILE = os.getenv("BOT_A_KEY_FILE", "./keys/truemarkets-api-key-edd1691b.json")

TERMINAL = {"completed", "failed"}


async def cmd_list_assets(bot: ExecutionClient, session: aiohttp.ClientSession, symbol_filter: str | None, venue = "defi") -> None:
    assets = await bot.list_assets(session, venue=venue)
    print(f"{'SYMBOL':<8}{'CHAIN':<14}{'ASSET_ID'}")
    print("-" * 70)
    for a in assets:
        if symbol_filter and a.get("symbol", "").upper() != symbol_filter.upper():
            continue
        print(f"{a.get('symbol', ''):<8}{str(a.get('chain') or '-'):<14}{a.get('id', '')}")


async def poll_status(bot: ExecutionClient, session: aiohttp.ClientSession, transfer_id: str,
                       attempts: int = 15, delay: float = 4.0) -> None:
    for _ in range(attempts):
        data = await bot.get_transfer(session, transfer_id)
        if not data:
            return
        status = data.get("status")
        print(data)
        logging.info(f"  status={status}  tx_hash={data.get('tx_hash') or '-'}   id={data.get('id') or '-'}")
        if status in TERMINAL:
            return
        await asyncio.sleep(delay)
    logging.warning("Stopped polling — check later with `python3 transfer.py status <id>`.")


async def cmd_send(bot: ExecutionClient, session: aiohttp.ClientSession,
                    symbol: str, chain: str, qty: str, to: str, venue="defi", network = None) -> None:
    assets = await bot.list_assets(session, venue=venue)
    match = [a for a in assets if a.get("symbol", "").upper() == symbol.upper() and a.get("chain") == chain]
    if not match:
        logging.error(
            f"No asset found for symbol={symbol} chain={chain}. "
            f"Run `python3 transfer.py list-assets {symbol}` to see what's available."
        )
        return
    asset = match[0]

    print("\n" + "=" * 64)
    print("  CONFIRM ON-CHAIN TRANSFER — THIS CANNOT BE UNDONE")
    print("=" * 64)
    print(f"  Asset       : {asset['symbol']}  ({asset['id']})")
    print(f"  Chain       : {chain}")
    print(f"  Quantity    : {qty} {asset['symbol']}")
    print(f"  Destination : {to}")
    print("=" * 64)
    print("  Double-check the destination address AND network match the")
    print("  receiving account exactly before continuing.")
    typed = input("\n  Type SEND to confirm, anything else to abort: ")
    if typed.strip() != "SEND":
        print("Aborted — nothing was sent.")
        return

    created = await bot.create_transfer(session, asset_id=asset["id"], qty=qty, to=to, qty_unit="base", network = network)
    if not created:
        logging.error("Transfer creation failed.")
        return
    transfer_id = created.get("id")
    logging.info(f"Transfer created: id={transfer_id} status={created.get('status')}")

    payloads = created.get("payloads") or []
    if payloads and transfer_id:
        signatures = [bot.sign_payload(p["payload"]) for p in payloads]
        executed = await bot.execute_transfer(session, transfer_id, signatures)
        if not executed:
            logging.error("Transfer execution failed.")
            return
        logging.info(f"Transfer executed: id={transfer_id} status={executed.get('status')}")

    if transfer_id:
        await poll_status(bot, session, transfer_id)


async def cmd_status(bot: ExecutionClient, session: aiohttp.ClientSession, transfer_id: str) -> None:
    data = await bot.get_transfer(session, transfer_id)
    if not data:
        logging.error("Could not fetch transfer.")
        return
    for k, v in data.items():
        if k != "payloads":
            print(f"  {k:<14}{v}")


async def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]
    bot = ExecutionClient(key_file=KEY_FILE, base_url=REST_URL)
    async with aiohttp.ClientSession() as session:
        await bot.authenticate(session)

        if cmd == "list-assets":
            symbol = sys.argv[2] if len(sys.argv) > 2 else None
            await cmd_list_assets(bot, session, symbol)
        elif cmd == "send":
            if len(sys.argv) != 6:
                print("Usage: python3 transfer.py send SYMBOL CHAIN QTY TO_ADDRESS")
                return
            _, _, symbol, chain, qty, to = sys.argv
            await cmd_send(bot, session, symbol, chain, qty, to)
        elif cmd == "status":
            if len(sys.argv) != 3:
                print("Usage: python3 transfer.py status TRANSFER_ID")
                return
            await cmd_status(bot, session, sys.argv[2])
        else:
            print(__doc__)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Shutting down.")
