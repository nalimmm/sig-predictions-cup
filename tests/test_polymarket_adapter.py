"""The Polymarket adapter against canned API payloads (no network, no signing)."""
import json
from datetime import datetime, timezone

import pytest

from basket_exec import Cancel, Config, OpenOrder, Place, Runner
from basket_exec.adapters.polymarket import CLOB, DATA, GAMMA, PolymarketVenue

pytest.importorskip("py_clob_client")
YES, NO, OTHER = "111", "222", "999"


class Resp:
    def __init__(self, payload, status=200):
        self.payload, self.status = payload, status

    def raise_for_status(self):
        if self.status >= 400:
            import requests
            raise requests.HTTPError(str(self.status))

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, books, positions, history=None):
        self.books, self.positions, self.history, self.calls = books, positions, history or [], []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        if url == f"{GAMMA}/markets":
            return Resp([{"question": "Will it happen?", "outcomes": json.dumps(["Yes", "No"]),
                          "clobTokenIds": json.dumps([YES, NO])}] if params["slug"] == "will-it-happen" else [])
        if url == f"{CLOB}/book":
            b = self.books.get(params["token_id"])
            return Resp(b, 200) if b else Resp({"error": "No orderbook exists"}, 404)
        if url == f"{DATA}/positions":
            assert params["user"] == "0xfunder"
            return Resp(self.positions)
        if url == f"{CLOB}/prices-history":
            return Resp({"history": self.history})
        raise AssertionError(url)


class FakeClient:
    def __init__(self, usdc, tokens, orders=()):
        self.usdc, self.tokens, self.orders, self.sent, self.cancelled = usdc, tokens, list(orders), [], []
        self.refuse = False

    def get_balance_allowance(self, params):
        if params.asset_type == "COLLATERAL":
            return {"balance": str(int(self.usdc * 1e6))}
        return {"balance": str(int(self.tokens.get(params.token_id, 0) * 1e6))}

    def get_orders(self):
        return self.orders

    def create_order(self, args, options):
        return (args, options)

    def post_order(self, signed, order_type):
        self.sent.append((signed, order_type))
        return {"success": False, "errorMsg": "not enough balance"} if self.refuse else \
               {"success": True, "errorMsg": "", "orderID": "0xabc", "status": "live"}

    def cancel(self, order_id):
        self.cancelled.append(order_id)
        return {"canceled": [order_id], "not_canceled": {}}


def book(bids, asks, tick="0.01", neg_risk=False):
    lv = lambda rows: [{"price": str(p), "size": str(s)} for p, s in rows]
    return {"bids": lv(bids), "asks": lv(asks), "tick_size": tick, "min_order_size": "5", "neg_risk": neg_risk}


def row(asset, size, avg, cur, **kw):
    return {"asset": asset, "size": size, "avgPrice": avg, "curPrice": cur, "currentValue": size * cur,
            "initialValue": size * avg, "title": "Will it happen?", "outcome": "Yes", "redeemable": False, **kw}


ENTRIES = [{"slug": "will-it-happen", "outcome": "Yes", "reference": 0.30}]


def venue(books, positions, client, **kw):
    return PolymarketVenue(ENTRIES, "0xfunder", client, session=FakeSession(books, positions, kw.pop("history", None)),
                           sleep=lambda s: None, **kw)


def test_basket_resolves_slug_and_outcome_to_a_token():
    v = venue({}, [], None)
    assert v.basket() == {YES: 0.30} and v.label(YES) == "Will it happen? [Yes]"
    v = PolymarketVenue([{"slug": "will-it-happen", "outcome": "no", "reference": 0.7}], "0xfunder",
                        session=FakeSession({}, []), sleep=lambda s: None)
    assert v.basket() == {NO: 0.7}
    for bad in ([{"slug": "nope", "reference": 0.3}], [{"slug": "will-it-happen", "outcome": "Maybe", "reference": 0.3}],
                [{"token_id": YES, "reference": 30}], [{"token_id": YES, "reference": 0.3}] * 2):
        with pytest.raises(ValueError):
            PolymarketVenue(bad, "0xfunder", session=FakeSession({}, []), sleep=lambda s: None).basket()


def test_snapshot_reads_cash_orders_positions_and_sorts_the_book():
    # Polymarket returns bids ascending and asks descending: the adapter must not trust the order
    books = {YES: book(bids=[(0.01, 900), (0.27, 50), (0.28, 120)], asks=[(0.99, 10), (0.31, 400), (0.30, 75)])}
    client = FakeClient(usdc=412.5, tokens={YES: 200.0}, orders=[
        {"id": "s1", "asset_id": YES, "side": "SELL", "original_size": "100", "size_matched": "40", "price": "0.6"},
        {"id": "b1", "asset_id": OTHER, "side": "BUY", "original_size": "10", "size_matched": "0", "price": "0.2"},
        {"id": "done", "asset_id": YES, "side": "SELL", "original_size": "10", "size_matched": "10", "price": "0.9"}])
    v = venue(books, [row(YES, 200.0, 0.25, 0.29), row(OTHER, 50.0, 0.5, 0.4),
                      row("777", 30.0, 0.1, 1.0, redeemable=True)], client)
    s = v.snapshot(v.basket())
    assert s.cash == 412.5                                         # the USDC balance, read directly
    assert s.equity == pytest.approx(412.5 + 200 * 0.28 + 50 * 0.4)  # basket at the bid, the rest at the venue's mark
    assert s.quotes[YES] == (0.28, 0.30) and v.book(YES).asks[0].size == 75
    assert [(o.id, o.instrument, o.action, o.price, o.remaining) for o in s.orders] == \
           [("s1", YES, "sell", 0.6, 60.0), ("b1", OTHER, "buy", 0.2, 10.0)]
    assert s.positions[YES].quantity == 200 and s.positions[YES].avg_cost == 0.25 and s.positions[YES].cost_basis == 50
    assert s.positions[OTHER].quantity == 50 and "777" not in s.positions   # resolved markets are ignored
    assert (s.specs[YES].tick, s.specs[YES].min_size, s.specs[YES].size_step, s.specs[YES].min_notional) == (0.01, 5, 0.01, 1)
    assert not any(u == f"{CLOB}/book" and p["token_id"] == OTHER for u, p in v.session.calls)  # outside: not even quoted


def test_token_balance_overrides_a_lagging_data_api():
    books = {YES: book(bids=[(0.28, 100)], asks=[(0.30, 100)])}
    # just sold everything: the Data API still shows 200 shares, the CLOB says 0
    v = venue(books, [row(YES, 200.0, 0.25, 0.29)], FakeClient(100, {YES: 0.0}))
    s = v.snapshot(v.basket())
    assert YES not in s.positions and s.equity == 100
    # just bought: the CLOB shows the shares, the Data API has no average cost yet -> hands off for one round
    v = venue(books, [], FakeClient(100, {YES: 50.0}))
    s = v.snapshot(v.basket())
    assert YES not in s.positions and YES in s.blocked and s.equity == pytest.approx(100 + 50 * 0.28)
    assert "average cost not published yet" in s.notes[0]
    # partial lag: quantity from the CLOB, average cost from the Data API
    v = venue(books, [row(YES, 200.0, 0.25, 0.29)], FakeClient(100, {YES: 260.5}))
    assert v.snapshot(v.basket()).positions[YES].quantity == 260.5


def test_market_without_a_book_is_skipped_not_fatal():
    v = venue({}, [row(YES, 10.0, 0.25, 0.29)], FakeClient(100, {YES: 10.0}))
    s = v.snapshot(v.basket())
    assert s.quotes[YES] == (None, None) and v.book(YES) is None
    assert s.equity == pytest.approx(100 + 10 * 0.29)


def test_read_only_mode_needs_no_key_and_cannot_trade():
    books = {YES: book(bids=[(0.28, 100)], asks=[(0.30, 100)])}
    v = venue(books, [row(YES, 20.0, 0.25, 0.29)], None, paper_cash=300)
    s = v.snapshot(v.basket())
    assert s.cash == 300 and s.orders == [] and s.positions[YES].quantity == 20
    with pytest.raises(RuntimeError):
        v.place(Place(YES, "buy", 10, 0.3))
    with pytest.raises(RuntimeError):
        v.cancel(OpenOrder("x", YES, "buy", 0.3, 10))


def test_orders_carry_tick_size_and_neg_risk_and_refusals_raise():
    client = FakeClient(100, {})
    v = venue({YES: book(bids=[(0.281, 100)], asks=[(0.300, 100)], tick="0.001", neg_risk=True)}, [], client)
    v.snapshot(v.basket())
    assert v.place(Place(YES, "sell", 12.34, 0.562)) == "live"
    (args, options), order_type = client.sent[0]
    assert (args.token_id, args.side, args.size, args.price) == (YES, "SELL", 12.34, 0.562)
    assert (options.tick_size, options.neg_risk, order_type) == ("0.001", True, "GTC")
    client.refuse = True
    with pytest.raises(RuntimeError, match="not enough balance"):
        v.place(Place(YES, "buy", 10, 0.3))
    with pytest.raises(RuntimeError):
        v.place(Place(OTHER, "buy", 10, 0.3))      # never quoted this round: no blind order
    v.cancel(OpenOrder("s1", YES, "sell", 0.6, 10))
    assert client.cancelled == ["s1"]


def test_reference_is_the_last_print_at_or_before_the_window():
    t0 = int(datetime(2026, 7, 2, tzinfo=timezone.utc).timestamp())
    hist = [{"t": t0 - 7200, "p": 0.40}, {"t": t0 - 60, "p": 0.42}, {"t": t0 + 600, "p": 0.20}]
    v = venue({}, [], None, history=hist)
    assert v.reference(YES, datetime(2026, 7, 2, tzinfo=timezone.utc)) == 0.42


def test_full_round_on_polymarket_build_ladder_then_rescue():
    cfg = Config(build_until="2026-07-01T00:00:00Z", guard_from="2026-01-01T00:00:00Z", max_capital=100,
                 min_cash_reserve=5)
    now = lambda: datetime(2026, 6, 1, tzinfo=timezone.utc)
    books = {YES: book(bids=[(0.29, 100)], asks=[(0.31, 80), (0.30, 60)])}
    client = FakeClient(usdc=1000, tokens={YES: 100.0})
    v = venue(books, [row(YES, 100.0, 0.30, 0.29)], client)
    log = []
    plan = Runner(v, cfg, live=True, log=log.append, clock=now).round()
    got = [(a.action, a.quantity, a.price, a.reason) for a in plan.actions]
    # ladder on the 100 shares held; then one tranche: only the 60 shown at 0.30 (budget 98 - 30 invested = 68)
    assert got == [("sell", 50, 0.60, "ladder"), ("sell", 50, 0.90, "ladder"), ("buy", 60, 0.30, "tranche")]
    assert len(client.sent) == 3 and all(l.startswith(("OK", "Round OK")) for l in log)

    # the market collapses: bid 0.14 <= max(50% of 0.30, 0.10) -> everything out at the bid, ladder cancelled
    v.session.books[YES] = book(bids=[(0.14, 500)], asks=[(0.16, 80)])
    client.orders = [{"id": "lo", "asset_id": YES, "side": "SELL", "original_size": "50", "size_matched": "0", "price": "0.6"},
                     {"id": "hi", "asset_id": YES, "side": "SELL", "original_size": "50", "size_matched": "0", "price": "0.9"}]
    plan = Runner(v, cfg, live=True, log=log.append, clock=now).round()
    assert [a.order.id for a in plan.actions if isinstance(a, Cancel)] == ["lo", "hi"]
    assert [(a.action, a.quantity, a.price) for a in plan.actions if isinstance(a, Place)] == [("sell", 100, 0.14)]
    assert client.cancelled == ["lo", "hi"] and any(l.startswith("RESCUE") for l in log)


def test_simulation_sends_nothing_and_stop_only_touches_the_basket():
    books = {YES: book(bids=[(0.29, 100)], asks=[(0.30, 60)])}
    client = FakeClient(1000, {}, orders=[
        {"id": "mine", "asset_id": YES, "side": "BUY", "original_size": "10", "size_matched": "0", "price": "0.3"},
        {"id": "other", "asset_id": OTHER, "side": "BUY", "original_size": "10", "size_matched": "0", "price": "0.2"}])
    v = venue(books, [], client)
    cfg = Config(build_until="2027-01-01T00:00:00Z")
    log = []
    Runner(v, cfg, live=False, log=log.append).round()
    assert client.sent == [] and client.cancelled == []
    Runner(v, cfg, live=True, log=log.append).stop()
    assert client.cancelled == ["mine"]
