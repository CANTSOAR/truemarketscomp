import asyncio
import time
import websockets

WS_URL = "wss://api.truex.co/api/v1"

async def monitor_trade_only():
    headers = {
        "Origin": "https://truemarkets.co",
        "Cache-Control": "no-cache",
        "Accept-Language": "en-US,en;q=0.9",
        "Pragma": "no-cache",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
    }
    
    print(f"[*] Connecting to {WS_URL}...")
    try:
        async with websockets.connect(WS_URL, additional_headers=headers) as ws:
            print("[+] Connected successfully!")
            
            # Subscribe ONLY to the TRADE channel
            timestamp = str(int(time.time()))
            payload = f'{{"type":"SUBSCRIBE_NO_AUTH","item_names":["BTC-PYUSD"],"channels":["TRADE"],"timestamp":"{timestamp}"}}'
            
            print(f"[*] Sending payload: {payload}")
            await ws.send(payload)
            
            print("[*] Socket is open and listening. Go make your 0.001 BTC trade now!\n")
            print("=" * 60)
            
            while True:
                response = await ws.recv()
                print(f"[RAW INCOMING DATA]: {response}")
                print("-" * 60)
                        
    except Exception as e:
        print(f"[-] WebSocket Error: {e}")

if __name__ == "__main__":
    try:
        asyncio.run(monitor_trade_only())
    except KeyboardInterrupt:
        print("\n[*] Disconnected by user.")