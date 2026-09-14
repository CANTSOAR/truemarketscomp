import requests
import time

# 1. Define the endpoints you want to fuzz
ENDPOINTS = [
    "https://api.truemarkets.co/v1/gateway/trades",
    "https://api.truemarkets.co/v1/gateway/orderbook",
    "https://api.truemarkets.co/v1/cefi/trades"
]

TARGET_ASSET_ID = "b2538174-aae3-4b55-b79f-ac177c99f816"
TARGET_LOT_SIZE = 0.01

def scan_public_tape():
    for url in ENDPOINTS:
        print(f"\n[*] Hitting endpoint: {url}")
        
        # Pass the Asset ID as a URL query parameter
        params = {"asset_id": TARGET_ASSET_ID}
        
        try:
            # Execute the fetch
            response = requests.get(url, params=params, timeout=5)
            
            # 200 OK means the public endpoint exists and returned data
            if response.status_code == 200:
                print("[+] Success! Parsing JSON payload...")
                data = response.json()
                
                # APIs usually return trades in a list. 
                # This checks if the data is wrapped in a 'trades' or 'data' key.
                trades = data.get("trades", data.get("data", [])) if isinstance(data, dict) else data
                
                if not isinstance(trades, list):
                    print("[-] Unexpected JSON structure. You may need to adjust the parser.")
                    continue
                    
                found_leak = False
                
                # 2. Filter the JSON for the smoking gun
                for trade in trades:
                    # APIs sometimes return numbers as strings, so we cast to float
                    size = float(trade.get("size", trade.get("amount", 0)))
                    
                    if size == TARGET_LOT_SIZE:
                        # Check for common internal ID keys
                        maker_id = trade.get("maker_uid") or trade.get("maker_id") or trade.get("provider")
                        
                        if maker_id:
                            print(f"[!!!] GOT THEM: 0.01 Lot -> Maker ID: {maker_id}")
                            found_leak = True
                            
                if not found_leak:
                    print("[-] Endpoint valid, but no 0.01 lots with exposed Maker IDs found.")
                    
            elif response.status_code == 404:
                print("[-] 404: Endpoint doesn't exist.")
            elif response.status_code in [401, 403]:
                print("[-] 401/403: Endpoint requires authentication.")
            else:
                print(f"[-] Failed with status code: {response.status_code}")
                
        except requests.exceptions.RequestException as e:
            print(f"[-] Connection error: {e}")
            
        # Pause briefly so you don't trigger rate limits or WAF blocks
        time.sleep(1)

if __name__ == "__main__":
    scan_public_tape()