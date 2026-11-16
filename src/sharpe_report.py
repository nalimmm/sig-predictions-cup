"""Risk-adjusted performance of the account over the full tournament, straight from the API.

Method: account value hour by hour (gaps forward filled), hourly returns r_t = V_t / V_{t-1} - 1,
risk free rate set to zero (virtual currency).
  Annualized hourly Sharpe  = mean(r) / stdev(r, ddof=1) x sqrt(24 x 365)
  Annualized daily Sharpe   = the same on close-to-close daily returns x sqrt(365)
The figure returned by the tournament API (methodology undocumented) is printed for comparison.

Usage: python3 sharpe_report.py   (requires SUPERMARKET_API_KEY)
"""
import math
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

API_KEY = os.environ.get("SUPERMARKET_API_KEY")
BASE = os.environ.get("SUPERMARKET_BASE", "https://sig.thesuper.market/api/v1")
SLUG = "midterm-elections"
s = requests.Session()
s.headers["Authorization"] = f"Bearer {API_KEY}"


def get(path, **p):
    r = s.get(BASE + path, params=p, timeout=30)
    time.sleep(0.8)
    r.raise_for_status()
    return r.json()


def parse(x):
    return datetime.fromisoformat(x.replace("Z", "+00:00"))


if not API_KEY:
    sys.exit("Set the SUPERMARKET_API_KEY environment variable first.")

hist = get(f"/tournaments/{SLUG}/portfolio/history", interval="1h")["data"]
raw = {parse(h["time"]).replace(minute=0, second=0, microsecond=0): h["totalValue"]
       for h in hist if h.get("totalValue") is not None}
pnl = get(f"/tournaments/{SLUG}/portfolio/pnl", period="all")

t0, t1 = min(raw), max(raw)
series, v, t = [], None, t0
while t <= t1:
    v = raw.get(t, v)
    series.append((t, v))
    t += timedelta(hours=1)

hourly = [b / a - 1 for (_, a), (_, b) in zip(series, series[1:]) if a and a > 0]
by_day = {}
for t, v in series:
    by_day[t.date()] = v  # last value of each day
closes = list(by_day.values())
daily = [b / a - 1 for a, b in zip(closes, closes[1:]) if a and a > 0]


def sharpe(r, per_year):
    if len(r) < 3:
        return None, None, None
    m, sd = statistics.fmean(r), statistics.stdev(r)
    return (m / sd * math.sqrt(per_year) if sd > 1e-15 else None), m, sd


sh_h, mh, sdh = sharpe(hourly, 24 * 365)
sh_d, md, sdd = sharpe(daily, 365)
v0, v1 = series[0][1], series[-1][1]
days = (t1 - t0).total_seconds() / 86400

print(f"Period: {t0:%b %d %H:%M} UTC -> {t1:%b %d %H:%M} UTC ({days:.1f} days, {len(hourly)} hourly returns)")
print(f"Value: {v0:,.0f} -> {v1:,.0f} ({(v1 / v0 - 1) * 100:+.1f}%)")
print(f"Mean hourly return {mh * 100:+.4f}%, hourly volatility {sdh * 100:.4f}%")
print(f"Annualized Sharpe (hourly returns)           : {sh_h:.2f}")
print(f"Annualized Sharpe (daily closes, {len(daily)} days)    : {sh_d:.2f}" if sh_d else
      f"Daily Sharpe: not enough days ({len(daily)})")
print(f"Sharpe reported by the API (period=all)      : {pnl.get('sharpe')}")
