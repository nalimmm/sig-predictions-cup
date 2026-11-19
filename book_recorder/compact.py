"""Lossless compaction of repeated book snapshots.

The probe on the server showed "book" messages dominate the bytes: Polymarket resends the whole
book of a token after every trade, although a trade usually changes one or two levels. The
recorder therefore stores, per connection and token:

  - the first book of the day, and then one every `full_every` seconds, as received ("m" lines);
  - every other book as the levels that changed since the previous book of that token on that
    connection ("bd" lines): [[price, size], ...] per side, size "0" for a level that disappeared.

Prices and sizes are kept as the exact strings Polymarket sent, so `Expander` rebuilds each book
exactly (level order aside: bids best first, asks best first). The book's own hash is stored too,
for anyone who wants to verify. State resets at each UTC day, so every day file stands alone.
"""
from typing import Dict, Optional, Tuple

from .store import utc_day


def _levels(levels) -> Dict[str, str]:
    return {str(l["price"]): str(l["size"]) for l in levels or []}


def _diff(old: Dict[str, str], new: Dict[str, str]):
    out = [[p, s] for p, s in new.items() if old.get(p) != s]
    out += [[p, "0"] for p in old if p not in new]
    return out


class Compactor:
    def __init__(self, full_every: float = 600.0):
        self.full_every = full_every
        self.state: Dict[Tuple[str, str], tuple] = {}   # (cid, asset) -> (bids, asks, last full ms)
        self.day: Optional[str] = None
        self.full = self.diffs = 0

    def encode(self, cid: str, ev: dict, now_ms: int) -> Optional[dict]:
        """None: store `ev` as received. Otherwise the diff to store instead."""
        day = utc_day(now_ms / 1000)
        if day != self.day:
            self.state.clear()
            self.day = day
        asset = str(ev.get("asset_id"))
        bids, asks = _levels(ev.get("bids", ev.get("buys"))), _levels(ev.get("asks", ev.get("sells")))
        prev = self.state.get((cid, asset))
        if prev is None or now_ms - prev[2] >= self.full_every * 1000:
            self.state[(cid, asset)] = (bids, asks, now_ms)
            self.full += 1
            return None
        self.state[(cid, asset)] = (bids, asks, prev[2])
        self.diffs += 1
        return {"asset_id": asset, "market": ev.get("market"), "timestamp": ev.get("timestamp"),
                "hash": ev.get("hash"), "bids": _diff(prev[0], bids), "asks": _diff(prev[1], asks)}


class Expander:
    """Turns "bd" lines back into full book events, in file order."""

    def __init__(self):
        self.state: Dict[Tuple[str, str], tuple] = {}
        self.orphans = 0

    def full(self, cid: str, ev: dict):
        self.state[(cid, str(ev.get("asset_id")))] = (
            _levels(ev.get("bids", ev.get("buys"))), _levels(ev.get("asks", ev.get("sells"))))

    def expand(self, cid: str, d: dict) -> Optional[dict]:
        prev = self.state.get((cid, str(d["asset_id"])))
        if prev is None:
            self.orphans += 1
            return None
        bids, asks = dict(prev[0]), dict(prev[1])
        for side, change in ((bids, d["bids"]), (asks, d["asks"])):
            for p, s in change:
                if float(s) == 0:
                    side.pop(p, None)
                else:
                    side[p] = s
        self.state[(cid, str(d["asset_id"]))] = (bids, asks)
        lv = lambda side, rev: [{"price": p, "size": s}
                                for p, s in sorted(side.items(), key=lambda x: float(x[0]), reverse=rev)]
        return {"event_type": "book", "asset_id": d["asset_id"], "market": d.get("market"),
                "timestamp": d.get("timestamp"), "hash": d.get("hash"),
                "bids": lv(bids, True), "asks": lv(asks, False), "_diff": True}
