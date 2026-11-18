"""Shared fixtures: import the bot without touching the network, and drive one_round() on a fake API.

Nothing in src/ is modified for the tests: the bot is a record of what ran, so the tests monkeypatch
its module level helpers (get, now, log, place, cancel) instead of refactoring it.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))            # basket_exec
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "deploy"))

import basket_bot as bb  # noqa: E402

SLUG = bb.SLUG


def utc(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


class FakeAPI:
    """Answers the GET paths one_round() reads, from plain Python state set by each test."""

    def __init__(self, equity=100_000.0):
        self.equity = equity
        self.markets = {}      # exchange id -> market title
        self.quotes = {}       # exchange id -> (bestBid, bestAsk)
        self.books = {}        # exchange id -> list of {"price", "quantity"} asks
        self.positions = []    # API position dicts
        self.orders = []       # API open order dicts
        self.candles = {}      # exchange id -> list of {"time", "close"}
        self.calls = []

    # helpers to build state
    def market(self, ex, title, bid=None, ask=None, ask_qty=0):
        self.markets[ex] = title
        self.quotes[ex] = (bid, ask)
        self.books[ex] = [{"price": ask, "quantity": ask_qty}] if ask is not None and ask_qty else []

    def position(self, ex, qty, avg, market_value=None):
        self.positions.append({
            "exchangeId": ex, "quantity": qty, "avgCost": avg, "settled": False,
            "costBasis": qty * avg, "marketValue": qty * avg if market_value is None else market_value,
        })

    def order(self, ex, action, qty, price, side="yes", filled=0, oid=None):
        self.orders.append({"id": oid or f"o{len(self.orders)}", "exchangeId": ex, "action": action,
                            "side": side, "quantity": qty, "priceLimit": price, "quantityFilled": filled})

    # the fake endpoint
    def get(self, path, **params):
        self.calls.append((path, params))
        page = {"hasMore": False, "nextCursor": None}
        if path == f"/tournaments/{SLUG}/markets":
            return {"data": [{"title": t, "exchanges": [{"id": ex}]} for ex, t in self.markets.items()],
                    "pagination": page}
        if path == "/orders":
            return {"data": list(self.orders), "pagination": page}
        if path == f"/tournaments/{SLUG}/portfolio/positions":
            return {"positions": list(self.positions)}
        if path == f"/tournaments/{SLUG}/portfolio/pnl":
            return {"totalAccountValue": self.equity}
        if path == "/exchanges/prices":
            return {"data": [{"exchangeId": ex, "bestBid": self.quotes.get(ex, (None, None))[0],
                              "bestAsk": self.quotes.get(ex, (None, None))[1]}
                             for ex in params["ids"].split(",")]}
        if path.endswith("/orderbook"):
            return {"asks": self.books.get(path.split("/")[2], [])}
        if path.endswith("/price-history"):
            return {"candles": self.candles.get(path.split("/")[2], [])}
        raise AssertionError(f"unexpected GET {path}")


class Bot:
    """The bot module wired to a FakeAPI, recording every order it would place or cancel."""

    def __init__(self, api):
        self.api = api
        self.placed = []     # (exchangeId, action, side, qty, price)
        self.cancelled = []  # (order id, reason)
        self.logs = []

    def run(self, at):
        bb.now = lambda: utc(at)
        bb.one_round("tid")
        return self

    def sells(self, ex=None):
        return [(q, p) for e, a, s, q, p in self.placed if a == "sell" and (ex is None or e == ex)]

    def buys(self, ex=None):
        return [(q, p) for e, a, s, q, p in self.placed if a == "buy" and (ex is None or e == ex)]


@pytest.fixture
def api():
    return FakeAPI()


@pytest.fixture
def bot(api, monkeypatch):
    b = Bot(api)
    monkeypatch.setattr(bb, "get", api.get)
    monkeypatch.setattr(bb, "log", b.logs.append)
    monkeypatch.setattr(bb, "place", lambda tid, ex, action, qty, price, side="yes":
                        b.placed.append((ex, action, side, qty, price)))
    monkeypatch.setattr(bb, "cancel", lambda o, reason: b.cancelled.append((o["id"], reason)))
    monkeypatch.setattr(bb, "now", bb.now)
    # module level caches: start every test from a cold bot, like a fresh restart
    monkeypatch.setattr(bb, "names", {})
    monkeypatch.setattr(bb, "basket_ids", {})
    monkeypatch.setattr(bb, "precount_refs", {})
    monkeypatch.setattr(bb, "ids_at", 0.0)
    return b


def basket(api, **quotes):
    """Register every basket contract as exchange 'b0'..'b7'; quotes override (bid, ask, ask_qty)."""
    ids = {}
    for i, title in enumerate(bb.BASKET):
        ex = f"b{i}"
        bid, ask, qty = quotes.get(ex, (None, None, 0))
        api.market(ex, title, bid, ask, qty)
        ids[ex] = title
    return ids
