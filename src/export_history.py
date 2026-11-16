"""Dump the hourly account history as CSV on stdout: time,totalValue.

This is how data/account_history_hourly.csv was produced during the tournament.
Usage: python3 export_history.py > history.csv   (requires SUPERMARKET_API_KEY)
"""
import os
import sys

import requests

API_KEY = os.environ.get("SUPERMARKET_API_KEY")
if not API_KEY:
    sys.exit("Set the SUPERMARKET_API_KEY environment variable first.")

s = requests.Session()
s.headers["Authorization"] = f"Bearer {API_KEY}"
r = s.get(os.environ.get("SUPERMARKET_BASE", "https://sig.thesuper.market/api/v1")
          + "/tournaments/midterm-elections/portfolio/history",
          params={"interval": "1h"}, timeout=30)
r.raise_for_status()
print("time,totalValue")
for h in r.json()["data"]:
    if h.get("totalValue") is not None:
        print(f"{h['time']},{h['totalValue']}")
