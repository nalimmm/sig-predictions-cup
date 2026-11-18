"""Adapter for the SIG Predictions Cup tournament API (sig.thesuper.market), the venue the
strategy was written for. Kept as the reference adapter: tests/test_parity.py checks that the
engine, fed through this adapter, takes the same decisions as the original src/basket_bot.py.

Instruments: the exchange id for the YES side, "<exchange id>:no" for the NO side. A negative
tournament position is a long position in the NO instrument.
"""
import time
import uuid
from datetime import datetime, timedelta
from typing import Dict, Optional

import requests

from ..config import parse_time
from ..types import Book, Level, OpenOrder, Place, Position, Snapshot, Spec

SPEC = Spec(tick=0.005, size_step=1.0)
NO = ":no"


class SupermarketVenue:
    def __init__(self, api_key: str, titles: Dict[str, float], slug: str = "midterm-elections",
                 base: str = "https://sig.thesuper.market/api/v1", session=None, sleep=time.sleep,
                 ids_refresh: float = 3600):
        """titles: exact market title -> reference price."""
        self.titles, self.slug, self.base, self.sleep, self.ids_refresh = titles, slug, base, sleep, ids_refresh
        self.session = session or requests.Session()
        self.session.headers["Authorization"] = f"Bearer {api_key}"
        self.names: Dict[str, str] = {}
        self._basket: Dict[str, float] = {}
        self._ids_at = 0.0
        self._tid = None

    # ------------------------------------------------------------------ HTTP
    def _call(self, method, path, **kw):
        for _ in range(3):
            try:
                r = self.session.request(method, self.base + path, timeout=30, **kw)
            except requests.RequestException:
                self.sleep(30)
                continue
            self.sleep(0.7 if method == "GET" else 2.1)  # API limits: 100 reads and 30 writes per minute
            if r.status_code == 429:
                self.sleep(int(r.headers.get("Retry-After", 60)))
                continue
            return r
        return None

    def _get(self, path, **params):
        r = self._call("GET", path, params={k: v for k, v in params.items() if v is not None})
        if r is None or not r.ok:
            raise RuntimeError(f"GET {path}: {'no response' if r is None else f'{r.status_code} {r.text[:300]}'}")
        return r.json()

    @property
    def tid(self):
        if self._tid is None:
            self._tid = self._get(f"/tournaments/{self.slug}")["id"]
        return self._tid

    # ----------------------------------------------------------------- Venue
    def basket(self) -> Dict[str, float]:
        if self._basket and time.time() - self._ids_at < self.ids_refresh:
            return self._basket
        found, cursor = {}, None
        while True:
            page = self._get(f"/tournaments/{self.slug}/markets", status="open", limit=100, cursor=cursor)
            for m in page["data"]:
                for ex in m["exchanges"]:
                    self.names[ex["id"]] = m["title"]
                    if m["title"] in self.titles:
                        found[ex["id"]] = m["title"]
            if not page["pagination"]["hasMore"]:
                break
            cursor = page["pagination"]["nextCursor"]
        by_title = {t: ex for ex, t in found.items()}
        self._basket = {by_title[t]: ref for t, ref in self.titles.items() if t in by_title}
        self._ids_at = time.time()
        return self._basket

    def snapshot(self, basket, with_outside=True) -> Snapshot:
        raw, cursor = [], None
        while True:
            page = self._get("/orders", tournamentId=self.tid, status="open", limit=200, cursor=cursor)
            raw += page["data"]
            if not page["pagination"]["hasMore"]:
                break
            cursor = page["pagination"]["nextCursor"]
        orders = [OpenOrder(o["id"], o["exchangeId"] + (NO if o["side"] == "no" else ""), o["action"],
                            o["priceLimit"], o["quantity"] - (o.get("quantityFilled") or 0)) for o in raw]
        live = [p for p in self._get(f"/tournaments/{self.slug}/portfolio/positions")["positions"]
                if not p["settled"] and p["quantity"]]
        equity = self._get(f"/tournaments/{self.slug}/portfolio/pnl", period="all")["totalAccountValue"]
        snap = Snapshot(equity=equity, orders=orders,
                        # Spendable cash is equity minus the MARKET value of positions.
                        cash=equity - sum(p["marketValue"] for p in live))
        for p in live:
            ex, q = p["exchangeId"], p["quantity"]
            inst = ex if q > 0 else ex + NO
            snap.positions[inst] = Position(inst, abs(q), p["avgCost"], abs(p["costBasis"]))
            if q < 0:
                snap.blocked.add(ex)  # never buy YES against a NO position
        ids = list(dict.fromkeys(list(basket) + [p["exchangeId"] for p in live]))
        for i in range(0, len(ids), 100):
            data = self._get("/exchanges/prices", ids=",".join(ids[i:i + 100]), tournamentId=self.tid)["data"]
            for q in data:
                bid, ask = q.get("bestBid"), q.get("bestAsk")
                snap.quotes[q["exchangeId"]] = (bid, ask)
                snap.quotes[q["exchangeId"] + NO] = (round(1 - ask, 6) if ask else None, None)
        snap.specs = {i: SPEC for i in list(snap.quotes) + list(snap.positions) + list(basket)}
        return snap

    def book(self, instrument) -> Optional[Book]:
        ob = self._get(f"/exchanges/{instrument}/orderbook", tournamentId=self.tid, depth=5) or {}
        side = lambda k, rev: sorted((Level(l["price"], l["quantity"]) for l in ob.get(k, [])),
                                     key=lambda l: l.price, reverse=rev)
        return Book(bids=side("bids", True), asks=side("asks", False))

    def reference(self, instrument, before: datetime) -> Optional[float]:
        """Close of the last hourly candle that ENDED by `before`: a candle still open at that time
        would mix in later prices. Fallback only; the runner normally freezes its own snapshot."""
        start = (before - timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
        h = self._get(f"/exchanges/{instrument}/price-history", tournamentId=self.tid, resolution="1h",
                      **{"from": start}, limit=12)
        closes = [(parse_time(c["time"]), c["close"]) for c in h["candles"]
                  if c["close"] is not None and parse_time(c["time"]) + timedelta(hours=1) <= before]
        return max(closes)[1] if closes else None

    def place(self, order: Place) -> str:
        is_no = order.instrument.endswith(NO)
        ex = order.instrument[:-len(NO)] if is_no else order.instrument
        body = {"exchangeId": ex, "side": "no" if is_no else "yes",
                "action": order.action, "quantity": int(order.quantity), "price": round(order.price, 3),
                "tournamentId": self.tid, "idempotencyKey": str(uuid.uuid4())}
        r = self._call("POST", "/orders", json=body)
        if r is None or not r.ok:
            raise RuntimeError("no response" if r is None else f"{r.status_code} {r.text[:300]}")
        return f"filled immediately: {r.json().get('quantityTraded', 0):,.0f}"

    def cancel(self, order: OpenOrder) -> None:
        r = self._call("DELETE", f"/orders/{order.id}")  # order by order: never a blanket cancel-all
        if r is None or not r.ok:
            raise RuntimeError("no response" if r is None else f"{r.status_code} {r.text[:300]}")

    def label(self, instrument) -> str:
        ex = instrument[:-len(NO)] if instrument.endswith(NO) else instrument
        short = self.names.get(ex, ex).replace("Will the ", "").replace(" win the", "").rstrip("?")[:55]
        return short + (" [NO]" if instrument.endswith(NO) else "")
