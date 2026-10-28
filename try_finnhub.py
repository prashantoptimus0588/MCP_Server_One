import os
import json
import urllib.request
from dotenv import load_dotenv

# 1. Force Python to look for and read your .env file
load_dotenv()

# 2. Check if the key was found safely
if 'FINNHUB_KEY' not in os.environ:
    print("\n[ERROR] 'FINNHUB_KEY' not found in your .env file!")
    print("Please make sure your .env file contains: FINNHUB_KEY=your_actual_key\n")
    exit(1)

# 3. Build the URL using the loaded key
u = f"https://finnhub.io/api/v1/stock/metric?symbol=AAPL&metric=all&token={os.environ['FINNHUB_KEY']}"

# 4. Make the request using standard libraries
try:
    with urllib.request.urlopen(u) as response:
        data = json.loads(response.read().decode())
        print(json.dumps(data.get("metric", {}), indent=1))
except Exception as e:
    print(f"\n[API ERROR] Call failed: {e}")
