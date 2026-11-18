"""The extracted engine against the original bot.

Both read the same fake tournament API: `src/basket_bot.py` untouched (only its network and
order functions are replaced), and the engine through the Supermarket adapter. On every
scenario they must decide the same cancels and the same orders.
"""
import random
from datetime import datetime, timezone

import pytest

import basket_bot as legacy
from basket_exec import Cancel, Config, Runner
from basket_exec.adapters.supermarket import NO, SupermarketVenue

TITLES = list(legacy.BASKET)
EX = {t: f"ex{i}" for i, t in enumerate(TITLES)}
OUTSIDE = ["out0", "out1"]
BUILD = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
GUARD = datetime(2026, 11, 3, 23, 30, tzinfo=timezone.utc)
NIGHT = datetime(2026, 11, 4, 1, tzinfo=timezone.utc)
FROZEN = datetime(2026, 11, 4, 16, 30, tzinfo=timezone.utc)


class FakeAPI:
    """Answers the GET paths both implementations use, from one scenario dict."""

    def __init__(self, sc):
        self.sc = sc

    def get(self, path, **params):
        sc = self.sc
        if path.endswith("/markets"):
            data = [{"title": t, "exchanges": [{"id": ex}]} for t, ex in EX.items()]
            data += [{"title": f"Outside {ex}", "exchanges": [{"id": ex}]} for ex in OUTSIDE]
            return {"data": data, "pagination": {"hasMore": False}}
        if path == "/orders":
            return {"data": sc["orders"], "pagination": {"hasMore": False}}
        if path.endswith("/portfolio/positions"):
            return {"positions": sc["positions"]}
        if path.endswith("/portfolio/pnl"):
            return {"totalAccountValue": sc["equity"]}
        if path == "/exchanges/prices":
            return {"data": [{"exchangeId": ex, "bestBid": sc["quotes"][ex][0], "bestAsk": sc["quotes"][ex][1]}
                             for ex in params["ids"].split(",") if ex in sc["quotes"]]}
        if path.endswith("/orderbook"):
            return {"asks": sc["asks"].get(path.split("/")[2], [])}
        if path.endswith("/price-history"):
            ref = sc["refs"].get(path.split("/")[2])
            return {"candles": [] if ref is None else [
                {"time": "2026-11-03T21:00:00Z", "close": round(ref * 0.9, 3)},
                {"time": "2026-11-03T22:00:00Z", "close": ref}]}
        if path.startswith("/tournaments/"):
            return {"id": "tid"}
        raise AssertionError(path)


def run_legacy(sc, now, monkeypatch):
    api, out = FakeAPI(sc), []
    monkeypatch.setattr(legacy, "get", api.get)
    monkeypatch.setattr(legacy, "log", lambda m: None)
    monkeypatch.setattr(legacy, "now", lambda: now)
    monkeypatch.setattr(legacy, "basket_ids", {})
    monkeypatch.setattr(legacy, "ids_at", 0.0)
    monkeypatch.setattr(legacy, "precount_refs", {})
    monkeypatch.setattr(legacy, "place", lambda tid, ex, action, qty, price, side="yes":
                        out.append(("place", ex + (NO if side == "no" else ""), action, float(int(qty)), round(price, 3))))
    monkeypatch.setattr(legacy, "cancel", lambda o, reason: out.append(("cancel", o["id"])))
    legacy.one_round("tid")
    return sorted(out)


def run_engine(sc, now):
    venue = SupermarketVenue("key", dict(legacy.BASKET), sleep=lambda s: None)
    venue._get = FakeAPI(sc).get
    plan = Runner(venue, Config.sig_cup(), live=False, log=lambda m: None, clock=lambda: now).round()
    return sorted(("cancel", a.order.id) if isinstance(a, Cancel) else
                  ("place", a.instrument, a.action, float(a.quantity), round(a.price, 3)) for a in plan.actions)


def scenario(rng, invested=0.6, night=False):
    """A random but coherent account: some basket positions with their sell orders, stray
    positions and orders from earlier strategies, resting buys, live books."""
    tick = lambda p: round(max(0.005, min(0.995, round(p / 0.005) * 0.005)), 3)
    quotes, asks, refs, positions, orders, n = {}, {}, {}, [], [], 0

    def order(ex, action, qty, price, side="yes", filled=None):
        nonlocal n
        n += 1
        o = {"id": f"o{n}", "exchangeId": ex, "action": action, "side": side, "quantity": qty, "priceLimit": price}
        if filled is not None:
            o["quantityFilled"] = filled
        orders.append(o)

    for title, ex in EX.items():
        ref = legacy.BASKET[title]
        mid = tick(ref * rng.choice([0.25, 0.4, 0.8, 0.95, 1.0, 1.05, 1.2, 1.6, 2.5] if night else
                                    [0.3, 0.45, 0.9, 0.97, 1.0, 1.03, 1.08, 1.15, 1.5]))
        bid, ask = tick(mid - 0.005), tick(mid + 0.005)
        quotes[ex] = (None, ask) if rng.random() < 0.05 else (bid, ask)
        asks[ex] = [] if rng.random() < 0.1 else [
            {"price": tick(ask + 0.01), "quantity": rng.randint(50, 5000)},
            {"price": ask, "quantity": rng.randint(50, 40000)}]
        refs[ex] = None if rng.random() < 0.1 else tick(ref * rng.choice([0.7, 1.0, 1.3]))
        if rng.random() < invested:
            avg = round(ref * rng.uniform(0.5, 1.1), 4)
            q = rng.randint(2, 60000)
            positions.append({"exchangeId": ex, "quantity": q, "avgCost": avg, "costBasis": round(q * avg, 2),
                              "marketValue": round(q * (bid or 0), 2), "settled": False})
            kind = rng.random()
            lo = legacy.tick_up(min(0.95, 2 * avg))
            hi = legacy.tick_up(min(0.95, (5 if avg < 0.2 else 3) * avg))
            if kind < 0.35:      # ladder already in place
                order(ex, "sell", q // 2, lo)
                order(ex, "sell", q - q // 2, hi)
            elif kind < 0.55:    # ladder of an older, different position
                order(ex, "sell", max(q // 3, 1), lo)
                order(ex, "sell", max(q // 4, 1), tick(hi - 0.05))
            elif kind < 0.65:    # more on offer than held
                order(ex, "sell", q, lo)
                order(ex, "sell", q, hi)
            elif kind < 0.75 and bid:   # a rescue order from the previous round
                order(ex, "sell", q, legacy.tick_down(bid))
        elif rng.random() < 0.15:
            q = -rng.randint(10, 5000)   # short left by an earlier strategy
            positions.append({"exchangeId": ex, "quantity": q, "avgCost": 0.6, "costBasis": round(-q * 0.6, 2),
                              "marketValue": round(-q * (1 - ask), 2), "settled": False})
        elif rng.random() < 0.2:
            order(ex, "sell", 100, 0.9)  # orphan sell
        r = rng.random()
        if r < 0.25:
            order(ex, "buy", rng.randint(10, 3000), tick(ref * rng.choice([0.9, 1.0, 1.09, 1.2])), filled=rng.choice([None, 0, 5]))
        if r < 0.05:
            order(ex, "buy", 50, tick(ref))            # duplicate
        if rng.random() < 0.05:
            order(ex, "buy", 50, 0.5, side="no")
    for ex in OUTSIDE:
        quotes[ex] = (0.305, 0.315)
        if rng.random() < 0.6:
            q = rng.choice([1, -1]) * rng.randint(10, 9000)
            positions.append({"exchangeId": ex, "quantity": q, "avgCost": 0.3, "costBasis": abs(q) * 0.3,
                              "marketValue": abs(q) * 0.3, "settled": False})
            if rng.random() < 0.5:
                order(ex, "sell", abs(q), rng.choice([0.305, 0.6]), side="yes" if q > 0 else "no")
        if rng.random() < 0.3:
            order(ex, "buy", 100, 0.2)
    positions.append({"exchangeId": "settled", "quantity": 10, "avgCost": 0.1, "costBasis": 1.0,
                      "marketValue": 10.0, "settled": True})
    held = sum(p["marketValue"] for p in positions if not p["settled"])
    return {"orders": orders, "positions": positions, "quotes": quotes, "asks": asks, "refs": refs,
            "equity": round(held + rng.choice([0, 150, 5000, 60000, 250000]), 2)}


@pytest.mark.parametrize("now,night", [(BUILD, False), (GUARD, False), (NIGHT, True), (FROZEN, True)],
                         ids=["build", "guard", "night-rolling", "after-last-buy"])
def test_same_decisions_as_the_original_bot(now, night, monkeypatch):
    rng = random.Random(20261104)
    busy = off_by_one = 0
    for i in range(300):
        sc = scenario(rng, invested=rng.choice([0.0, 0.5, 0.9]), night=night)
        old, new = run_legacy(sc, now, monkeypatch), run_engine(sc, now)
        if new != old:
            # The only tolerated gap: a buy one unit larger, when the original truncated a float
            # that is an integer up to rounding (int(21425.999999999996) -> 21425).
            assert len(new) == len(old), f"scenario {i} at {now}"
            for a, b in zip(new, old):
                ok = a == b or (a[0] == "place" and a[:3] == b[:3] and a[4] == b[4] and a[2] == "buy" and a[3] - b[3] == 1)
                assert ok, f"scenario {i} at {now}: {a} != {b}"
            off_by_one += 1
        busy += bool(old)
    assert busy > 250        # the scenarios really exercise the logic
    assert off_by_one <= 3   # out of 300


def test_parity_scenarios_cover_every_rule(monkeypatch):
    rng, seen = random.Random(7), set()
    for now, night in [(BUILD, False), (GUARD, False), (NIGHT, True)]:
        for _ in range(150):
            sc = scenario(rng, night=night)
            basket_pos = {p["exchangeId"] for p in sc["positions"] if p["exchangeId"].startswith("ex") and p["quantity"] > 0}
            for kind, inst, *rest in (a for a in run_legacy(sc, now, monkeypatch) if a[0] == "place"):
                action, _qty, price = rest
                if action == "buy":
                    seen.add("day buy" if now == BUILD else "night buy")
                elif inst.endswith(NO):
                    seen.add("cover short")
                elif inst not in basket_pos:
                    seen.add("cleanup")
                else:
                    bid = sc["quotes"][inst][0]
                    seen.add("rescue" if bid and price <= bid else "ladder")
    assert seen == {"day buy", "night buy", "cover short", "cleanup", "rescue", "ladder"}


def test_reference_can_be_read_earlier_than_the_window_start():
    """Not in the original bot: `reference_at` reads the pre-window price before the market starts
    to move, so an early leg of a drop cannot lower the bar of the drop guard."""
    venue = SupermarketVenue("key", {}, sleep=lambda s: None)
    venue._tid = "tid"
    venue._get = lambda path, **kw: {"candles": [{"time": "2026-11-03T21:00:00Z", "close": 0.25},
                                                 {"time": "2026-11-03T22:00:00Z", "close": 0.21},
                                                 {"time": "2026-11-03T23:00:00Z", "close": 0.14}]}
    assert venue.reference("ex", datetime(2026, 11, 4, tzinfo=timezone.utc)) == 0.14      # original rule
    cfg = Config.sig_cup()
    cfg.rolling.reference_at = datetime(2026, 11, 3, 22, tzinfo=timezone.utc)
    runner = Runner(venue, cfg, log=lambda m: None)
    assert runner._reference("ex") == 0.25
    venue._get = lambda path, **kw: {"candles": []}
    assert runner._reference("ex") == 0.25                                                # frozen once read
