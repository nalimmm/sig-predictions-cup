"""book_recorder against message shapes from Polymarket's documentation (no network).

The websocket payloads below follow the documented market channel (book, both price_change
layouts, last_trade_price, tick_size_change). They were not captured live: the first run on the
server is the check, and `python -m book_recorder stats` on its first day file shows what came in.
"""
import asyncio
import gzip
import json
from datetime import datetime, timezone

import pytest

from book_recorder import markets as mk
from book_recorder.replay import Books, iter_events, iter_lines, stats
from book_recorder.store import DailyWriter
from book_recorder.trades import TradeDownloader, recorded_markets
from book_recorder.ws import Recorder

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
T0 = NOW.timestamp()


# ------------------------------------------------------------------ Gamma market selection
def gm(i, vol, liq=5000, end="2026-12-31T00:00:00Z", event="ev", **kw):
    m = {"conditionId": f"0xc{i}", "slug": f"m{i}", "question": f"Q{i}?", "volume24hr": vol,
         "liquidityNum": liq, "endDate": end, "clobTokenIds": json.dumps([f"{i}1", f"{i}2"]),
         "outcomes": json.dumps(["Yes", "No"]), "negRisk": False, "active": True, "closed": False,
         "enableOrderBook": True, "acceptingOrders": True, "events": [{"slug": event}]}
    m.update(kw)
    return m


class Gamma:
    def __init__(self, rows, extra=None):
        self.rows, self.extra, self.calls = rows, extra or {}, []

    def __call__(self, url, **params):
        self.calls.append(params)
        if "slug" in params or "condition_ids" in params:
            v = params.get("slug") or params.get("condition_ids")
            return [self.extra[v]] if v in self.extra else []
        off, lim = params["offset"], params["limit"]
        return self.rows[off:off + lim]


def test_select_normalises_and_filters():
    rows = [gm(1, 900), gm(2, 800, closed=True), gm(3, 700, end="2026-11-19T13:00:00Z"),
            gm(4, 600, liq=10), gm(5, 500, acceptingOrders=False), gm(6, 400), gm(7, 300)]
    got = mk.select(Gamma(rows), n=2, now=NOW, page=3)
    assert [m["slug"] for m in got] == ["m1", "m6"]
    assert got[0]["tokens"] == [{"token_id": "11", "outcome": "Yes"}, {"token_id": "12", "outcome": "No"}]
    assert got[0]["event_slug"] == "ev" and got[0]["volume_24h"] == 900
    assert mk.asset_ids(got) == ["11", "12", "61", "62"]


def test_select_pins_and_caps_per_event():
    rows = [gm(1, 900, event="a"), gm(2, 800, event="a"), gm(3, 700, event="b"), gm(4, 600, event="c")]
    extra = {"m9": gm(9, 1, liq=0)}
    got = mk.select(Gamma(rows, extra), n=3, now=NOW, max_per_event=1, pinned=["m9", "m3"])
    assert [m["slug"] for m in got] == ["m3", "m9", "m1"]


def test_select_stops_paging_when_enough():
    g = Gamma([gm(i, 1000 - i) for i in range(1, 400)])
    assert len(mk.select(g, n=150, now=NOW, page=100)) == 150
    assert len(g.calls) == 2


# ------------------------------------------------------------------ storage
def test_daily_writer_rolls_at_midnight_and_appends(tmp_path):
    w = DailyWriter(tmp_path)
    w.write('{"a":1}', now=T0)
    assert w.write('{"a":2}', now=T0 + 12 * 3600) is True       # 00:00 UTC on the 20th
    w.close()
    w2 = DailyWriter(tmp_path)                                    # a restart appends a gzip member
    w2.write('{"a":3}', now=T0 + 13 * 3600)
    w2.close()
    assert [l["a"] for l in iter_lines(tmp_path / "book" / "2026-11-19.jsonl.gz")] == [1]
    assert [l["a"] for l in iter_lines(tmp_path / "book" / "2026-11-20.jsonl.gz")] == [2, 3]
    with gzip.open(tmp_path / "book" / "2026-11-20.jsonl.gz") as f:   # plain gzip reads it too
        assert len(f.read().splitlines()) == 2


def test_crash_keeps_flushed_lines(tmp_path):
    w = DailyWriter(tmp_path, flush_seconds=0)
    for i in range(50):
        w.write(json.dumps({"i": i}), now=T0)
    path = w.path("2026-11-19")
    data = path.read_bytes()                        # never closed: no gzip trailer, like a kill -9
    path.write_bytes(data + b"\x00garbage")
    assert [l["i"] for l in iter_lines(path)] == list(range(50))


# ------------------------------------------------------------------ replay
BOOK = {"event_type": "book", "asset_id": "11", "market": "0xc1", "timestamp": "1763553600000", "hash": "h",
        "bids": [{"price": "0.48", "size": "100"}, {"price": "0.47", "size": "50"}],
        "asks": [{"price": "0.52", "size": "80"}, {"price": "0.55", "size": "10"}]}
PC_NEW = {"event_type": "price_change", "market": "0xc1", "timestamp": "1763553601000",
          "price_changes": [{"asset_id": "11", "price": "0.49", "size": "20", "side": "BUY", "hash": "x",
                             "best_bid": "0.49", "best_ask": "0.52"},
                            {"asset_id": "11", "price": "0.52", "size": "0", "side": "SELL", "hash": "y",
                             "best_bid": "0.49", "best_ask": "0.55"}]}
PC_OLD = {"event_type": "price_change", "asset_id": "11", "market": "0xc1", "timestamp": "1763553602000",
          "changes": [{"price": "0.47", "side": "BUY", "size": "0"}]}
TRADE = {"event_type": "last_trade_price", "asset_id": "11", "market": "0xc1", "price": "0.52",
         "side": "BUY", "size": "80", "fee_rate_bps": "0", "timestamp": "1763553601500"}
TICK = {"event_type": "tick_size_change", "asset_id": "11", "market": "0xc1", "old_tick_size": "0.01",
        "new_tick_size": "0.001", "timestamp": "1763553603000"}


def test_books_apply_both_price_change_layouts_and_dedupe_trades():
    b = Books()
    for i, ev in enumerate([BOOK, PC_NEW, PC_OLD, TICK]):
        assert b.apply(dict(ev, _r=i)) is None
    l2 = b["11"]
    assert (l2.best_bid(), l2.best_ask()) == (0.49, 0.55)
    assert l2.levels("bid") == [(0.49, 20.0), (0.48, 100.0)]
    assert l2.depth("ask", 0.05) == 10 and l2.tick == 0.001
    assert l2.mid() == pytest.approx(0.52) and l2.spread() == pytest.approx(0.06)
    t = b.apply(dict(TRADE, _r=1000))
    assert (t.price, t.size, t.side, t.ts) == (0.52, 80.0, "BUY", 1763553601500)
    assert b.apply(dict(TRADE, _r=1500)) is None                 # same trade from an overlapping connection
    assert b.apply(dict(TRADE, _r=60_000)) is not None          # long after: a genuine repeat


# ------------------------------------------------------------------ websocket recorder
class FakeWS:
    def __init__(self, script):
        self.script, self.sent = list(script), []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, msg):
        self.sent.append(msg)

    async def recv(self):
        await asyncio.sleep(0)
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        await asyncio.sleep(3600)                   # idle until cancelled


class FakeServer:
    """Each connection gets: the book snapshot of every subscribed token, then `then`."""

    def __init__(self, then=()):
        self.then, self.conns = list(then), []

    def __call__(self, url):
        ws = FakeWS([])
        server = self

        class Ctx:
            async def __aenter__(self_):
                server.conns.append(ws)
                return ws

            async def __aexit__(self_, *exc):
                return False

        orig_send = ws.send

        async def send(msg):
            await orig_send(msg)
            if msg.startswith("{"):
                ids = json.loads(msg)["assets_ids"]
                ws.script += [json.dumps([dict(BOOK, asset_id=i) for i in ids])]
                if len(server.conns) == 1:
                    ws.script += list(server.then)
        ws.send = send
        return Ctx()


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def run_recorder(rec, until, timeout=5.0):
    async def go():
        stop = asyncio.Event()
        task = asyncio.ensure_future(rec.run(stop, tick=0.01))
        deadline = asyncio.get_running_loop().time() + timeout
        while not until() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        stop.set()
        await task
    asyncio.run(go())


def selection(n):
    return lambda: [mk.normalise(gm(i, 1000 - i)) for i in range(1, n + 1)]


async def _nosleep(_):
    await asyncio.sleep(0)


def test_recorder_shards_subscribes_and_records(tmp_path):
    server = FakeServer(then=[json.dumps(PC_NEW), json.dumps(TRADE), "PONG", "INVALID OPERATION"])
    clock = Clock(T0)
    rec = Recorder(tmp_path, selection(3), connect=server, shard_size=4, clock=clock, sleep=_nosleep)
    run_recorder(rec, lambda: rec.writer.lines >= 10)
    subs = [json.loads(ws.sent[0]) for ws in server.conns]
    assert [s["assets_ids"] for s in subs] == [["11", "12", "21", "22"], ["31", "32"]]
    assert all(s["type"] == "market" for s in subs)
    day = tmp_path / "book" / "2026-11-19.jsonl.gz"
    evs = list(iter_events(day, with_meta=True))
    kinds = [e.get("event_type") or e["_e"] for e in evs]
    assert kinds.count("book") == 6 and "price_change" in kinds and "last_trade_price" in kinds
    assert {"markets", "open", "subscribe", "close"} <= set(kinds)
    assert any(l.get("x") == "INVALID OPERATION" for l in iter_lines(day))
    assert not any(l.get("x") == "PONG" for l in iter_lines(day))
    saved = json.loads((tmp_path / "markets" / "2026-11-19.json").read_text())
    assert saved[0]["generation"] == 1 and len(saved[0]["markets"]) == 3
    s = stats(day)
    assert s["trades"] == 1 and s["tokens_seen"] == 6 and s["books_two_sided"] == 6


def test_recorder_reconnects_after_a_drop(tmp_path):
    server = FakeServer(then=[ConnectionResetError("peer reset")])
    rec = Recorder(tmp_path, selection(1), connect=server, clock=Clock(T0), sleep=_nosleep)
    run_recorder(rec, lambda: len(server.conns) >= 2 and rec.shards[0].messages >= 2)
    evs = list(iter_events(tmp_path / "book" / "2026-11-19.jsonl.gz", with_meta=True))
    closes = [e for e in evs if e.get("_e") == "close"]
    assert closes and "ConnectionResetError" in closes[0]["why"]
    assert sum(1 for e in evs if e.get("event_type") == "book") >= 4       # fresh snapshot after reconnect


def test_recorder_rotates_at_midnight_without_gap(tmp_path):
    clock = Clock(T0 + 11.9 * 3600)
    server = FakeServer()
    rec = Recorder(tmp_path, selection(1), connect=server, clock=clock, sleep=_nosleep)

    def until():
        if rec.generation == 1 and rec.shards and rec.shards[0].ready.is_set():
            clock.t = T0 + 12.1 * 3600                                  # past midnight UTC
        return rec.generation == 2 and rec.shards[0].ready.is_set()
    run_recorder(rec, until)
    new_day = list(iter_events(tmp_path / "book" / "2026-11-20.jsonl.gz", with_meta=True))
    order = [(e.get("_e") or e.get("event_type"), e["_c"]) for e in new_day]
    first_book = order.index(("book", "s0g2"))
    old_close = order.index(("close", "s0g1"))
    assert first_book < old_close            # the new connection had data before the old one closed
    assert (tmp_path / "markets" / "2026-11-20.json").exists()


def test_recorder_pauses_when_disk_is_low(tmp_path, monkeypatch):
    import book_recorder.ws as ws_mod
    free = {"gb": 1.0}
    monkeypatch.setattr(ws_mod, "free_gb", lambda p: free["gb"])
    rec = Recorder(tmp_path, selection(1), connect=FakeServer(), clock=Clock(T0), sleep=_nosleep)
    run_recorder(rec, lambda: rec.dropped >= 1)
    free["gb"] = 10.0
    rec._check_disk()
    rec.writer.close()
    evs = [e.get("_e") for e in iter_events(tmp_path / "book" / "2026-11-19.jsonl.gz", with_meta=True)]
    assert evs[0] == "disk_low" and evs[-1] == "disk_ok"


def test_selection_failure_is_retried_not_fatal(tmp_path):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        raise RuntimeError("gamma down")
    rec = Recorder(tmp_path, flaky, connect=FakeServer(), clock=Clock(T0), sleep=_nosleep)
    run_recorder(rec, lambda: calls["n"] >= 1, timeout=0.3)
    assert calls["n"] == 1 and rec.generation == 0     # waits 60 s before retrying, no crash


# ------------------------------------------------------------------ Data API v2 trades
def trow(i, ts):
    return {"token_id": "11", "condition_id": "0xc1", "proxy_wallet": f"0xw{i % 3}", "side": "BUY",
            "price": 0.5, "size": 10 + i, "timestamp": ts, "transaction_hash": f"0xt{i}", "outcome": "Yes",
            "bio": "long text", "profile_image": "https://example.invalid/p.png", "pseudonym": "Some-Name"}


class DataAPI:
    """Newest first, cursor pagination like /v2/positions."""

    def __init__(self, rows, page=2):
        self.rows, self.page, self.calls = rows, page, []

    def __call__(self, url, **params):
        self.calls.append(params)
        start = int(params.get("cursor") or 0)
        data = sorted(self.rows, key=lambda r: -r["timestamp"])[start:start + self.page]
        more = start + self.page < len(self.rows)
        return {"data": data, "pagination": {"has_more": more, "next_cursor": str(start + self.page) if more else None}}


def test_trades_download_is_incremental(tmp_path):
    api = DataAPI([trow(i, 1000 + i) for i in range(5)])
    d = TradeDownloader(tmp_path, api, clock=lambda: T0)
    assert d.download("0xc1") == 5
    assert all(c["condition"] == "0xc1" for c in api.calls) and api.calls[1]["cursor"] == "2"
    api.rows += [trow(5, 1005), trow(6, 1005)]
    api.calls.clear()
    d2 = TradeDownloader(tmp_path, api, clock=lambda: T0)        # a later run, state from disk
    assert d2.download("0xc1") == 2
    assert len(api.calls) == 2                                   # stopped at the first known page
    lines = list(iter_lines(tmp_path / "trades" / "0xc1.jsonl.gz"))
    assert [l["t"]["tx"] for l in lines] == [f"0xt{i}" for i in range(7)]
    assert lines[0]["t"]["wallet"] == "0xw0" and lines[0]["raw"]["transaction_hash"] == "0xt0"
    assert "bio" not in lines[0]["raw"] and lines[0]["raw"]["pseudonym"] == "Some-Name"


def test_trades_since_and_errors(tmp_path):
    api = DataAPI([trow(i, 1000 + i) for i in range(5)])
    d = TradeDownloader(tmp_path, api)
    assert d.download("0xc1", since=1003) == 2
    assert len(api.calls) == 2              # newest first: stops at the first page reaching the target
    bad = TradeDownloader(tmp_path, lambda url, **p: {"error": "gone"})
    res = bad.download_all(["0xc2"])
    assert res["0xc2"].startswith("error: RuntimeError")


def test_trades_reject_unknown_rows_and_stuck_cursor(tmp_path):
    d = TradeDownloader(tmp_path, lambda url, **p: {"data": [{"foo": 1}], "pagination": {}})
    with pytest.raises(RuntimeError, match="not understood"):
        d.download("0xc1")
    stuck = lambda url, **p: {"data": [trow(1, 5)], "pagination": {"has_more": True, "next_cursor": "same"}}
    with pytest.raises(RuntimeError, match="not advancing"):
        TradeDownloader(tmp_path, stuck).download("0xc9")


def test_recorded_markets_reads_market_files(tmp_path):
    (tmp_path / "markets").mkdir()
    for day, ids in [("2026-11-18", ["0xa"]), ("2026-11-19", ["0xb", "0xa"]), ("2026-11-20", ["0xc"])]:
        (tmp_path / "markets" / f"{day}.json").write_text(json.dumps(
            [{"from_ms": 0, "generation": 1, "markets": [{"condition_id": i} for i in ids]}]))
    assert recorded_markets(tmp_path, days=2) == ["0xb", "0xa", "0xc"]
    assert recorded_markets(tmp_path, days=2, today="2026-11-19") == ["0xa", "0xb"]


def test_recorder_with_the_real_websockets_library(tmp_path):
    """The default connection code against a local server speaking the same protocol."""
    websockets = pytest.importorskip("websockets")
    from book_recorder.ws import default_connect
    pings = []

    async def handler(ws, *_):
        sub = json.loads(await ws.recv())
        await ws.send(json.dumps([dict(BOOK, asset_id=i) for i in sub["assets_ids"]]))
        await ws.send(json.dumps(TRADE))
        async for msg in ws:
            if msg == "PING":
                pings.append(1)
                await ws.send("PONG")

    async def go():
        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = list(server.sockets)[0].getsockname()[1]
            rec = Recorder(tmp_path, selection(2), connect=default_connect, url=f"ws://127.0.0.1:{port}",
                           ping_seconds=0.05, clock=Clock(T0))
            stop = asyncio.Event()
            task = asyncio.ensure_future(rec.run(stop, tick=0.01))
            for _ in range(300):
                if pings and rec.shards and rec.shards[0].messages >= 2:
                    break
                await asyncio.sleep(0.01)
            stop.set()
            await task
    asyncio.run(go())
    assert pings
    s = stats(tmp_path / "book" / "2026-11-19.jsonl.gz")
    assert s["events"] == {"book": 4, "last_trade_price": 1} and s["connection_events"]["open"] == 1


# ------------------------------------------------------------------ book compaction and disk budget
def big_book(asset, bump=0, n=40, ts=1763553600000):
    bids = [{"price": f"{0.40 - i / 100:.2f}", "size": str(100 + i)} for i in range(n // 2)]
    asks = [{"price": f"{0.41 + i / 100:.2f}", "size": str(200 + i)} for i in range(n // 2)]
    asks[0]["size"] = str(200 - bump)              # a trade ate part of the best ask
    if bump >= 50:
        asks.pop(0)                                 # ... or all of it
    return {"event_type": "book", "asset_id": asset, "market": "0xc1", "timestamp": str(ts + bump),
            "hash": f"h{bump}", "bids": bids, "asks": asks}


def test_repeated_books_are_stored_as_diffs_and_rebuilt_exactly(tmp_path):
    clock = Clock(T0)
    rec = Recorder(tmp_path, selection(1), clock=clock, full_book_seconds=600)
    sent = [big_book("11", 0), big_book("11", 10), big_book("11", 20), big_book("11", 60)]
    rec.record("s0g1", json.dumps([sent[0], dict(TRADE)]))          # snapshot frame: a list
    for b in sent[1:]:
        clock.t += 1
        rec.record("s0g1", json.dumps(b))
    clock.t += 601                                                   # time for a full book again
    rec.record("s0g1", json.dumps(big_book("11", 70)))
    rec.writer.close()
    path = tmp_path / "book" / "2026-11-19.jsonl.gz"
    lines = list(iter_lines(path))
    assert [("bd" in l, "m" in l) for l in lines] == [(False, True)] + [(True, False)] * 3 + [(False, True)]
    assert lines[1]["bd"]["asks"] == [["0.41", "190"]]
    assert lines[3]["bd"]["asks"] == [["0.41", "0"]]
    books = [e for e in iter_events(path) if e["event_type"] == "book"]
    norm = lambda side: sorted((l["price"], l["size"]) for l in side)
    for got, want in zip(books, sent + [big_book("11", 70)]):
        assert norm(got["bids"]) == norm(want["bids"]) and norm(got["asks"]) == norm(want["asks"])
        assert got["hash"] == want["hash"] and got["timestamp"] == want["timestamp"]
    assert len(lines[1]["bd"]["bids"]) == 0 and len(json.dumps(lines[1])) < len(json.dumps(sent[1])) / 5
    b = Books()
    for e in books:
        b.apply(e)
    assert b["11"].best_ask() == 0.42


def test_compaction_restarts_each_day_so_day_files_stand_alone(tmp_path):
    clock = Clock(T0 + 11.99 * 3600)
    rec = Recorder(tmp_path, selection(1), clock=clock)
    rec.record("s0g1", json.dumps(big_book("11", 0)))
    clock.t = T0 + 12.01 * 3600
    rec.record("s0g1", json.dumps(big_book("11", 10)))               # old connection after midnight
    rec.writer.close()
    day2 = list(iter_lines(tmp_path / "book" / "2026-11-20.jsonl.gz"))
    assert "m" in day2[0]
    assert [e["hash"] for e in iter_events(tmp_path / "book" / "2026-11-20.jsonl.gz")] == ["h10"]


def test_book_dir_is_kept_under_budget(tmp_path):
    rec = Recorder(tmp_path, selection(1), clock=Clock(T0), max_gb=2e-9 * 1000)   # 2 kB budget
    for day in ("2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19"):
        (tmp_path / "book" / f"{day}.jsonl.gz").write_bytes(b"x" * 900)
    rec._prune()
    left = sorted(f.name for f in (tmp_path / "book").glob("*.gz"))
    assert left == ["2026-11-18.jsonl.gz", "2026-11-19.jsonl.gz"]
    rec.writer.close()
