"""Greeks of the Bitcoin barrier markets ("will Bitcoin dip to X by D", "will Bitcoin reach X").

Each market is read as a one-touch option on BTC under a driftless geometric Brownian motion:
    price = P(touch before expiry) = 2 N(-|ln(S/H)| / (sigma sqrt(T)))
The market's own price gives sigma (implied volatility); the Greeks are finite differences of the
same formula, per YES share, then multiplied by the position. They are model figures: the delta is
the sturdy one, vega and theta lean on the model, and nothing is computed when the price sits near
0 or 1, where the implied volatility is not identified. Markets on anything else have no Greeks.
"""
import json
import math
import re
import urllib.request
from statistics import NormalDist
from typing import Optional

N = NormalDist()
YEAR_S = 365 * 86400.0
BARRIER = re.compile(r"bitcoin-(dip-to|reach|hit)-(\d+(?:pt\d+)?)(k?)(?:-|$)")
SPOT_URL = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"


def barrier(slug: str) -> Optional[tuple]:
    """(level in dollars, +1 for a barrier above spot or -1 below), or None when the slug is not a BTC barrier."""
    m = BARRIER.search(slug)
    if not m:
        return None
    level = float(m.group(2).replace("pt", ".")) * (1000 if m.group(3) else 1)
    return level, (-1 if m.group(1) == "dip-to" else 1)


def touch(spot: float, level: float, side: int, vol: float, years: float) -> float:
    if (spot - level) * side >= 0:                  # already through the barrier
        return 1.0
    if years <= 0 or vol <= 0:
        return 0.0
    return 2 * N.cdf(-abs(math.log(spot / level)) / (vol * math.sqrt(years)))


def implied_vol(price: float, spot: float, level: float, side: int, years: float) -> Optional[float]:
    if not 0.02 <= price <= 0.98 or years <= 0 or (spot - level) * side >= 0:
        return None
    vol = abs(math.log(spot / level)) / (N.inv_cdf(1 - price / 2) * math.sqrt(years))
    return vol if vol <= 3.0 else None              # above 300 % the price is telling something the model cannot


def greeks(price: float, spot: float, level: float, side: int, years: float) -> Optional[dict]:
    """Per YES share, in dollars: delta and gamma for BTC +1 %, vega for +1 volatility point, theta for one day."""
    vol = implied_vol(price, spot, level, side, years)
    if vol is None:
        return None
    p = lambda s=spot, v=vol, t=years: touch(s, level, side, v, t)
    up, dn = p(s=spot * 1.01), p(s=spot * 0.99)
    return {"iv": vol, "delta": (up - dn) / 2, "gamma": up + dn - 2 * p(),
            "vega": p(v=vol + 0.01) - p(), "theta": p(t=max(years - 86400 / YEAR_S, 0.0)) - p()}


def binance_spot(timeout: float = 3.0) -> Optional[float]:
    """Last BTC/USDT trade on Binance: public, read-only, no key. None on any failure."""
    try:
        with urllib.request.urlopen(SPOT_URL, timeout=timeout) as r:
            return float(json.loads(r.read())["price"])
    except Exception:
        return None


def portfolio(positions, spot: Optional[float], now_ms: int) -> Optional[dict]:
    """positions: (slug, shares, mid, end_ms). Returns the totals and the figures per market."""
    if not spot:
        return None
    tot = {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0}
    per = {}
    for slug, shares, mid, end_ms in positions:
        b = barrier(slug)
        if not b or not end_ms or not mid:
            continue
        g = greeks(mid, spot, b[0], b[1], (end_ms - now_ms) / 1000 / YEAR_S)
        if g is None:
            continue
        per[slug] = {"iv": round(g["iv"], 4), "delta": round(shares * g["delta"], 4)}
        for k in tot:
            tot[k] += shares * g[k]
    return {"spot": spot, "source": "Binance BTC/USDT", "priced": len(per), **{k: round(v, 4) for k, v in tot.items()}, "markets": per}
