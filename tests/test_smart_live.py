"""Shadow and real execution: order translation, safeguards, arming, and the loop on a fake feed."""
import asyncio
import json
import time

import pytest

from smart.live.execution import Executor, Journal, LiveMM
from smart.live.guard import Guard, GuardConfig, geoblock, read_arming
from smart.live.orders import Order, Want, intents, plan, yes_side
from smart.live.runner import Publish, Runner
from smart.live.venue import DryVenue, OrderRefused
from smart.mm import MMConfig

Y, N = "111", "222"
SLUG = "m"


def meta(accepting=True):
    m = {"condition_id": "0xm", "slug": SLUG, "question": "?", "event_slug": "e", "neg_risk": False,
         "end_date": "2027-06-01T00:00:00+00:00", "tokens": [{"token_id": Y, "outcome": "Yes"}, {"token_id": N, "outcome": "No"}],
         "tick": 0.01, "min_size": 5.0, "accepting": accepting}
    return {Y: {**m, "token_id": Y, "complement": N}, N: {**m, "token_id": N, "complement": Y}}


def book(t, tok, bid, ask, size=1000):
    return {"_r": t, "_c": "live1", "event_type": "book", "asset_id": tok, "market": "0xm", "timestamp": str(t),
            "bids": [{"price": f"{bid - i / 100:.2f}", "size": str(size)} for i in range(3)],
            "asks": [{"price": f"{ask + i / 100:.2f}", "size": str(size)} for i in range(3)]}


def trade(t, tok, price, size, side="SELL"):
    return {"_r": t, "_c": "live1", "event_type": "last_trade_price", "asset_id": tok, "market": "0xm",
            "price": f"{price:.2f}", "size": str(size), "side": side, "timestamp": str(t)}


# ------------------------------------------------------------------ orders
def test_intents_flat_inventory_buys_yes_and_buys_no_for_the_ask():
    w = intents(Y, N, 0.0, 0.40, 10, 0.46, 10, 0.01, 5)
    assert [(x.token, x.side, x.price, x.size) for x in w] == [(Y, "BUY", 0.40, 10), (N, "BUY", 0.54, 10)]


def test_intents_sell_what_is_held_before_buying_the_other_side():
    long = intents(Y, N, 12.0, 0.40, 10, 0.46, 20, 0.01, 5)
    assert [(x.token, x.side, x.size) for x in long] == [(Y, "BUY", 10), (Y, "SELL", 12), (N, "BUY", 8)]
    short = intents(Y, N, -7.0, 0.40, 10, 0.46, 10, 0.01, 5)
    assert [(x.token, x.side, x.price, x.size) for x in short] == [(N, "SELL", 0.60, 7), (N, "BUY", 0.54, 10)]


def test_intents_drop_what_polymarket_would_refuse():
    assert intents(Y, N, 0.0, 0.40, 4.9, None, 0, 0.01, 5) == []          # under the minimum size
    assert intents(Y, N, 0.0, 0.405, 10, None, 0, 0.01, 5) == []          # off the tick
    assert intents(Y, N, 0.0, 0.05, 10, None, 0, 0.01, 5) == []           # a buy worth less than 1 USDC


def test_yes_side_of_an_order_on_no():
    assert yes_side(Order("a", N, "BUY", 0.54, 10, yes=False)) == ("SELL", 0.46)
    assert yes_side(Order("a", N, "SELL", 0.60, 10, yes=False)) == ("BUY", 0.40)


def test_plan_keeps_a_right_order_and_replaces_a_moved_or_eaten_one():
    rest = [Order("1", Y, "BUY", 0.40, 10), Order("2", N, "BUY", 0.54, 10, matched=7)]
    cancel, place = plan([Want(Y, "BUY", 0.40, 10), Want(N, "BUY", 0.54, 10)], rest)
    assert [o.id for o in cancel] == ["2"] and [(w.token, w.price) for w in place] == [(N, 0.54)]
    cancel, place = plan([Want(Y, "BUY", 0.41, 10)], rest)
    assert {o.id for o in cancel} == {"1", "2"} and len(place) == 1


# ------------------------------------------------------------------ guard
class Q:
    def __init__(self, inv=0.0, avg=0.0, cash=0.0, mid=0.5):
        self.inv, self.avg, self.cash, self.last_mid = inv, avg, cash, mid


def test_budget_and_order_cap():
    g = Guard(GuardConfig(budget_usdc=100, max_order_usdc=50))
    qs = [Q(inv=100, avg=0.5)]                                  # 50 committed
    rest = [Order("1", Y, "BUY", 0.5, 80)]                      # 40 more
    assert g.allow("BUY", 0.5, 18, qs, rest) is None            # 99
    assert g.allow("BUY", 0.5, 22, qs, rest) == "budget"
    assert "over the" in g.allow("BUY", 0.5, 120, [], [])
    assert g.allow("SELL", 0.5, 300, qs, rest) is None          # selling what is held commits nothing


def test_breakers_daily_loss_drawdown_floor(tmp_path):
    clock = [1795132800.0]
    g = Guard(GuardConfig(budget_usdc=100), tmp_path / "g.json", clock=lambda: clock[0])
    assert g.breakers([Q(cash=0)]) is None
    assert g.breakers([Q(cash=-4.9)]) is None
    assert g.breakers([Q(cash=-5.0)]).startswith("daily loss")
    clock[0] += 86400                                           # new day: the day starts at 95
    assert g.breakers([Q(cash=-5.0)]) is None
    assert g.breakers([Q(cash=-15.0)]).startswith("daily loss")
    g2 = Guard(GuardConfig(budget_usdc=100, daily_loss_pct=100), clock=lambda: clock[0])
    g2.breakers([Q(cash=20)])
    assert g2.breakers([Q(cash=1.9)]).startswith("drawdown")    # 101.9 < 120 * 0.85
    g3 = Guard(GuardConfig(budget_usdc=100, daily_loss_pct=100, drawdown_pct=100), clock=lambda: clock[0])
    assert "floor" in g3.breakers([Q(cash=-41)])


def test_halt_survives_a_restart_and_blocks_orders(tmp_path):
    g = Guard(GuardConfig(), tmp_path / "g.json")
    g.halt("test")
    g2 = Guard(GuardConfig(), tmp_path / "g.json")
    assert g2.halted["reason"] == "test" and g2.allow("BUY", 0.5, 1, [], []) == "halted"
    g2.clear()
    assert Guard(GuardConfig(), tmp_path / "g.json").halted is None


def test_arming_file(tmp_path):
    p = tmp_path / "ARMED"
    with pytest.raises(PermissionError):
        read_arming(p, check_owner=False)
    p.write_text("armed=no\nbudget_usdc=100\n")
    with pytest.raises(PermissionError):
        read_arming(p, check_owner=False)
    p.write_text("armed=yes  # the owner\nbudget_usdc=100\nuntil=2026-12-31\n")
    from datetime import date
    assert read_arming(p, today=date(2026, 12, 1), check_owner=False)["budget_usdc"] == 100.0
    with pytest.raises(PermissionError):
        read_arming(p, today=date(2027, 1, 1), check_owner=False)


def test_geoblock_and_positions():
    assert geoblock(lambda url: {"blocked": False, "country": "FI"}) is False
    assert geoblock(lambda url: {"blocked": True}) is True
    assert geoblock(lambda url: 1 / 0) is None
    clock = [0.0]
    g = Guard(GuardConfig(geoblock_every_s=10, geoblock_grace_s=100), clock=lambda: clock[0])
    assert g.check_geoblock(lambda url: 1 / 0) is None
    clock[0] = 200
    assert "too long" in g.check_geoblock(lambda url: 1 / 0)
    assert "blocked" in Guard(GuardConfig()).check_geoblock(lambda url: {"blocked": True})
    for i in range(2):
        assert g.check_positions({SLUG: 10}, {SLUG: 3}) is None
    assert "differ" in g.check_positions({SLUG: 10}, {SLUG: 3})
    assert g.check_positions({SLUG: 10}, {SLUG: 10.5}) is None


# ------------------------------------------------------------------ shadow loop on a fake feed
class FakeWS:
    def __init__(self, events):
        self.events, self.sent = list(events), []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, m):
        self.sent.append(m)

    async def recv(self):
        if not self.events:
            await asyncio.sleep(3600)
        await asyncio.sleep(0)
        return json.dumps(self.events.pop(0))


def test_shadow_mirrors_quotes_into_dry_orders_and_fills_them(tmp_path):
    now = int(time.time() * 1000)
    evs = [book(now, Y, 0.40, 0.46), book(now + 1, N, 0.54, 0.60)]
    for k in range(1, 6):
        evs.append(book(now + k * 1500, Y, 0.40, 0.46))
    evs.append(trade(now + 9000, Y, 0.40, 50))                  # an aggressive sale through our 0.41 bid
    evs.append(book(now + 9500, Y, 0.40, 0.46))
    sim = LiveMM([SLUG], meta(), MMConfig(latency_s=0.0, requote_s=0.0), mode="shadow")
    sim.load("2026-11-24")
    guard = Guard(GuardConfig(), tmp_path / "g.json")
    ex = Executor(sim, DryVenue(), guard, Journal(tmp_path / "journal"))
    pub = Publish(tmp_path, None, every_s=3600)
    ws = FakeWS(evs)
    r = Runner(sim, ex, guard, pub, get=lambda url: {"blocked": False}, connect=lambda url: ws,
               stop_file=tmp_path / "STOP")

    async def go():
        t = asyncio.ensure_future(r.main())
        for _ in range(200):
            await asyncio.sleep(0.01)
            if not ws.events:
                break
        await asyncio.sleep(0.05)
        r.stop("test")
        await asyncio.wait_for(t, 5)
    asyncio.run(go())
    q = sim.q[Y]
    lines = [json.loads(l) for f in (tmp_path / "journal").glob("*.jsonl") for l in f.read_text().splitlines()]
    places = [l for l in lines if l["k"] == "place"]
    assert {(l["side"], l["price"]) for l in places} >= {("BUY", 0.41), ("BUY", 0.55)}   # YES bid, NO bid for the YES ask
    assert q.inv > 0 and any(l["k"] == "fill" for l in lines)
    snap = json.loads((tmp_path / "data.json").read_text())
    assert snap["mode"] == "shadow" and snap["execution"]["placed"] >= 2
    path = snap["markets"][0]["ord"]
    assert path and any(row[1] == 0.41 and row[3] == 0.45 for row in path)
    assert json.loads(ws.sent[0]) == {"assets_ids": [Y, N], "type": "market"}


# ------------------------------------------------------------------ live executor with a fake venue
class FakeVenue:
    live = True

    def __init__(self):
        self.placed, self.cancelled, self.book = [], [], {}
        self.n = 0
        self.refuse = False

    def place(self, w, post_only=True, taker=False):
        if self.refuse:
            raise OrderRefused("no")
        self.n += 1
        oid = f"o{self.n}"
        self.placed.append((oid, w, post_only, taker))
        self.book[oid] = {"id": oid, "size_matched": "0"}
        return oid

    def cancel(self, ids):
        self.cancelled += ids
        for i in ids:
            self.book.pop(i, None)

    def cancel_all(self):
        self.cancel(list(self.book))

    def heartbeat(self):
        pass

    def orders(self):
        return dict(self.book)

    def order(self, oid):
        return self.gone.get(oid) if hasattr(self, "gone") else None


def live_setup(tmp_path, **gcfg):
    sim = LiveMM([SLUG], meta(), MMConfig(requote_s=0.0), mode="live")
    sim.load("2026-11-24")
    v = FakeVenue()
    guard = Guard(GuardConfig(**gcfg), tmp_path / "g.json")
    ex = Executor(sim, v, guard, Journal(tmp_path / "j"), clock=lambda: sim.now / 1000)
    ex.feed_ok = True
    return sim, v, ex


def test_live_quotes_rest_only_once_acknowledged_and_fills_come_from_the_venue(tmp_path):
    sim, v, ex = live_setup(tmp_path)
    sim.on_event(book(1_000, Y, 0.40, 0.46))
    sim.on_event(book(1_001, N, 0.54, 0.60))
    assert sim.q[Y].bid is None                                 # wanted, not resting yet
    ex.step()
    assert {(w.token, w.side, w.price) for _, w, po, _ in v.placed} == {(Y, "BUY", 0.41), (N, "BUY", 0.55)}
    assert all(po for _, _, po, _ in v.placed)                  # post-only
    assert sim.q[Y].bid == 0.41 and sim.q[Y].ask == 0.45
    sim.on_event(trade(2_000, Y, 0.39, 500))                    # the market trades through: no imagined fill
    assert sim.q[Y].inv == 0
    no_order = next(oid for oid, w, _, _ in v.placed if w.token == N)
    v.book[no_order]["size_matched"] = "4"                     # the venue reports 4 NO bought at 0.55
    ex.ingest(*ex.poll_io())
    assert sim.q[Y].inv == pytest.approx(-4) and sim.q[Y].fills[-1]["price"] == pytest.approx(0.45)


def test_live_halt_and_stale_feed_cancel_everything(tmp_path):
    sim, v, ex = live_setup(tmp_path)
    sim.on_event(book(1_000, Y, 0.40, 0.46))
    sim.on_event(book(1_001, N, 0.54, 0.60))
    ex.step()
    assert len(ex.orders) == 2
    sim.now = 1_001 + 31_000                                    # 31 s without a book update
    ex.step()
    assert ex.orders == {} and len(v.cancelled) == 2
    sim.on_event(book(sim.now, Y, 0.40, 0.46))
    ex.step()
    assert len(ex.orders) == 2
    ex.guard.halt("test")
    ex.step()
    assert ex.orders == {}
    sim.on_event(book(sim.now + 1, Y, 0.40, 0.46))
    ex.step()
    assert ex.orders == {}                                      # nothing new while halted


def test_live_budget_refuses_orders(tmp_path):
    sim, v, ex = live_setup(tmp_path, budget_usdc=2.0)
    sim.on_event(book(1_000, Y, 0.40, 0.46))
    sim.on_event(book(1_001, N, 0.54, 0.60))
    ex.step()
    assert len(v.placed) == 0 and ex.refused["budget"] == 2


def test_live_refused_order_is_journaled_and_nothing_breaks(tmp_path):
    sim, v, ex = live_setup(tmp_path)
    v.refuse = True
    sim.on_event(book(1_000, Y, 0.40, 0.46))
    sim.on_event(book(1_001, N, 0.54, 0.60))
    ex.step()
    assert ex.orders == {} and ex.refused["venue"] == 2


def test_live_exit_before_the_end_date_is_a_fill_and_kill(tmp_path):
    sim, v, ex = live_setup(tmp_path)
    q = sim.q[Y]
    q.inv, q.avg = 10.0, 0.40
    q.end_ms = 1_000 + 3_600_000                                # one hour left
    sim.on_event(book(1_000, Y, 0.40, 0.46))
    sim.on_event(book(1_001, N, 0.54, 0.60))
    ex.step()
    takers = [(w.token, w.side, w.price, w.size) for _, w, po, tk in v.placed if tk]
    assert takers == [(Y, "SELL", 0.40, 10.0)]


def test_clob_venue_signs_a_post_only_gtc_and_a_fill_and_kill_exit(monkeypatch):
    cc = pytest.importorskip("py_clob_client.client")
    from py_clob_client.clob_types import ApiCreds
    from smart.live.venue import ClobVenue
    sent = []

    def fake_get(url, *a, **k):
        return {"minimum_tick_size": 0.01, "neg_risk": False, "base_fee": 0}

    def fake_post(url, headers=None, data=None):
        sent.append(json.loads(data))
        return {"success": True, "orderID": f"0x{len(sent)}"}
    monkeypatch.setattr(cc, "get", fake_get)
    monkeypatch.setattr(cc, "post", fake_post)
    client = cc.ClobClient("https://clob.polymarket.com", key="0x" + "11" * 32, chain_id=137, signature_type=1, funder="0x" + "22" * 20)
    client.set_api_creds(ApiCreds("k", "c2VjcmV0", "p"))
    v = ClobVenue(None, "unused", 1, {"12345": {"tick": 0.01, "neg_risk": False}}, client=client)
    assert v.place(Want("12345", "BUY", 0.41, 7.0)) == "0x1"
    o = sent[-1]
    assert (o["orderType"], o["postOnly"], o["order"]["side"], o["order"]["makerAmount"], o["order"]["takerAmount"]) == \
        ("GTC", True, "BUY", "2870000", "7000000")
    v.place(Want("12345", "SELL", 0.40, 10.0), taker=True)
    assert (sent[-1]["orderType"], sent[-1]["postOnly"]) == ("FAK", False)


def test_guest_copy_is_sealed_with_its_own_password_then_revoked(tmp_path):
    from smart.dash import decrypt

    class S3:
        def __init__(self):
            self.objs = {}

        def put_object(self, Bucket, Key, Body, **k):
            self.objs[Key] = Body
    s3 = S3()
    raw = json.dumps({"mode": "shadow"}).encode()
    p = Publish(tmp_path, "owner-password-123456", s3, "b", "smart-dashboard/data-shadow.enc", guest_password="guest-password-1234567")
    p.write(raw, "{}")
    assert json.loads(decrypt(s3.objs["smart-dashboard/data-shadow-guest.enc"], "guest-password-1234567")) == {"mode": "shadow", "guest": True}
    assert json.loads(decrypt(s3.objs["smart-dashboard/data-shadow.enc"], "owner-password-123456")) == {"mode": "shadow"}
    p.guest = None
    p.write(raw, "{}")
    assert s3.objs["smart-dashboard/data-shadow-guest.enc"] == b"revoked"
