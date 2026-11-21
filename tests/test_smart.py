"""Smart pod, S1 paper engine: feed reader, detector, simulated fills, risk and breakers."""
import gzip
import json
import zlib

import pytest

from smart.__main__ import main as smart_main
from smart.config import Config
from smart.engine import Engine, Journal
from smart.feed import DayFile, follow, replay
from smart.report import read, summary

DAY = "2026-11-20"
T0 = 1795132800000          # 2026-11-20 00:00 UTC, ms
A, B = "111", "222"         # YES and NO tokens of market M


def market(end="2027-01-01T00:00:00+00:00", event="ev1", cid="0xm", tokens=(A, B), vol=100000.0, slug="m"):
    return {"condition_id": cid, "slug": slug, "question": "?", "event_slug": event, "neg_risk": False,
            "end_date": end, "volume_24h": vol, "liquidity": 5000.0,
            "tokens": [{"token_id": t, "outcome": o} for t, o in zip(tokens, ("Yes", "No"))]}


def book(t, tok, bid, ask, size=1000):
    return {"r": t, "c": "s0g1", "m": {"event_type": "book", "asset_id": tok, "market": "0xm", "timestamp": str(t),
                                       "bids": [{"price": f"{bid - i / 100:.2f}", "size": str(size)} for i in range(3)],
                                       "asks": [{"price": f"{ask + i / 100:.2f}", "size": str(size)} for i in range(3)]}}


def trade(t, tok, price, size, side="SELL"):
    return {"r": t, "c": "s0g1", "m": {"event_type": "last_trade_price", "asset_id": tok, "market": "0xm",
                                       "price": f"{price:.2f}", "size": str(size), "side": side,
                                       "timestamp": str(t), "fee_rate_bps": "0"}}


def calm(minutes=30, start=T0):
    """A quiet half hour: the mid wanders by one tick, so the local sigma is about a tick."""
    lines = []
    for m in range(minutes):
        bid = 0.50 if m % 2 == 0 else 0.51
        lines.append(book(start + m * 60000, A, bid, bid + 0.01))
        lines.append(book(start + m * 60000 + 1, B, round(1 - bid - 0.01, 2), round(1 - bid, 2)))
    return lines


def sweep(t):
    """Aggressive selling at four prices, 200 USDC, the book of A falls to 0.45 / 0.47."""
    lines = [trade(t + i * 1000, A, p, 100) for i, p in enumerate((0.50, 0.49, 0.48, 0.47))]
    lines.append(book(t + 5000, A, 0.45, 0.47))
    return lines


def write_day(root, lines, markets=None, day=DAY):
    (root / "book").mkdir(parents=True, exist_ok=True)
    (root / "markets").mkdir(parents=True, exist_ok=True)
    with gzip.open(root / "book" / f"{day}.jsonl.gz", "wb") as f:
        for l in lines:
            f.write((json.dumps(l) + "\n").encode())
    (root / "markets" / f"{day}.json").write_text(json.dumps([{"from_ms": T0, "markets": markets or [market()]}]))


def run(tmp_path, lines, cfg=None, markets=None):
    write_day(tmp_path / "data", lines, markets)
    eng = Engine(cfg or Config(), Journal(tmp_path / "j"), data_dir=tmp_path / "data")
    for ev in replay(tmp_path / "data" / "book", [DAY]):
        eng.on_event(ev)
    eng.journal.close()
    return eng, read(tmp_path / "j")


def kinds(recs, k):
    return [r for r in recs if r["k"] == k]


# ---------------------------------------------------------------- feed
def test_dayfile_reads_members_skips_a_crashed_tail_and_follows_appends(tmp_path):
    p = tmp_path / "d.jsonl.gz"
    l1 = json.dumps({"r": 1, "c": "x", "m": {"event_type": "a"}}).encode() + b"\n"
    l2 = json.dumps({"r": 2, "c": "x", "m": {"event_type": "b"}}).encode() + b"\n"
    l3 = json.dumps({"r": 3, "c": "x", "m": {"event_type": "c"}}).encode() + b"\n"
    member1 = gzip.compress(l1)
    z = zlib.compressobj(6, zlib.DEFLATED, zlib.MAX_WBITS | 16)
    crashed = z.compress(l2 * 50) + z.flush(zlib.Z_SYNC_FLUSH)     # flushed but never closed
    p.write_bytes(member1 + crashed[:-7])                           # and cut mid-flush
    d = DayFile(p, chunk=64, stall_bytes=32)
    got = [e["event_type"] for e in d.events()]
    assert got[0] == "a" and set(got[1:]) <= {"b"}
    with open(p, "ab") as f:                                        # the recorder restarts
        f.write(gzip.compress(l3))
    assert [e["event_type"] for e in d.events()] == ["c"]
    assert d.skipped == 1


def test_dayfile_reads_a_member_still_being_written(tmp_path):
    p = tmp_path / "d.jsonl.gz"
    z = zlib.compressobj(6, zlib.DEFLATED, zlib.MAX_WBITS | 16)
    line = lambda i: json.dumps({"r": i, "c": "x", "m": {"event_type": "t", "i": i}}).encode() + b"\n"
    p.write_bytes(z.compress(line(1)) + z.flush(zlib.Z_SYNC_FLUSH))
    d = DayFile(p)
    assert [e["i"] for e in d.events()] == [1]
    assert list(d.events()) == []
    with open(p, "ab") as f:
        f.write(z.compress(line(2) + line(3)) + z.flush(zlib.Z_SYNC_FLUSH))
    assert [e["i"] for e in d.events()] == [2, 3]


def test_follow_yields_idle_then_moves_to_the_next_day(tmp_path):
    root = tmp_path / "book"
    root.mkdir()
    with gzip.open(root / "2026-11-20.jsonl.gz", "wb") as f:
        f.write(b'{"r":1,"c":"x","m":{"event_type":"a"}}\n')
    clock = {"t": T0 / 1000 + 3600}
    seen, polls = [], []

    def sleep(_):
        polls.append(1)
        if len(polls) == 1:
            with gzip.open(root / "2026-11-21.jsonl.gz", "wb") as f:
                f.write(b'{"r":2,"c":"x","m":{"event_type":"b"}}\n')
            clock["t"] += 86400

    for ev in follow(root, clock=lambda: clock["t"], sleep=sleep, stop=lambda: len(polls) >= 2):
        seen.append(ev.get("event_type") or ev["_e"])
    assert seen == ["day", "a", "idle", "day", "b", "idle"]


# ---------------------------------------------------------------- strategy
def happy_path():
    t = T0 + 30 * 60000
    lines = calm() + sweep(t)
    lines.append(trade(t + 10000, A, 0.45, 50))                   # through our 0.46 bid: filled
    lines.append(book(t + 120000, A, 0.50, 0.51))                 # bid 0.50 > target 0.49: sold
    lines.append(book(t + 40 * 60000, A, 0.50, 0.51))             # lets the follow-ups run
    return lines


def test_sweep_fires_s1_and_the_paper_trade_reverts(tmp_path):
    eng, recs = run(tmp_path, happy_path())
    sig = kinds(recs, "signal")
    assert len(sig) == 1 and sig[0]["skip"] is None and sig[0]["token"] == A
    assert sig[0]["levels"] == 4 and sig[0]["drop"] == pytest.approx(0.055)
    order = kinds(recs, "order")[0]
    assert order["price"] == 0.46 and order["target"] == 0.49
    assert order["t"] - sig[0]["t"] >= 2000                       # placed after the latency
    assert order["shares"] * order["price"] == pytest.approx(3.0, abs=0.01)     # 3 % of 100
    exit_ = kinds(recs, "exit")[0]
    assert exit_["how"] == "bid_through" and exit_["price"] == 0.49
    assert exit_["pnl"] == pytest.approx(order["shares"] * 0.03)
    assert eng.positions == {} and eng.nav() == pytest.approx(100 + exit_["pnl"])
    s = summary(recs)
    assert s["closed_trades"] == 1 and s["study"]["5 min"]["n"] == 1
    assert s["study"]["30 min"]["mean_reverted_frac"] == pytest.approx(0.818, abs=0.001)


def test_touching_our_price_is_not_a_fill(tmp_path):
    lines = [l for l in happy_path() if not (l["m"].get("event_type") == "last_trade_price" and l["m"]["price"] == "0.45")]
    t = T0 + 30 * 60000
    lines.insert(len(calm()) + 5, trade(t + 10000, A, 0.46, 50))
    _, recs = run(tmp_path, lines)
    assert kinds(recs, "fill") == []
    assert [r["why"] for r in kinds(recs, "cancel")] == ["ttl"]


def test_time_exit_sells_into_the_bids_after_the_latency(tmp_path):
    t = T0 + 30 * 60000
    lines = calm() + sweep(t) + [trade(t + 10000, A, 0.45, 50), book(t + 20 * 60000, A, 0.46, 0.48),
                                 book(t + 31 * 60000, A, 0.44, 0.48), book(t + 32 * 60000, A, 0.44, 0.48)]
    _, recs = run(tmp_path, lines)
    ex = kinds(recs, "exit")
    assert ex and ex[0]["how"] == "time" and ex[0]["price"] == 0.44 and ex[0]["pnl"] < 0


def test_no_entry_near_the_end_date_or_when_a_sibling_moved(tmp_path):
    _, recs = run(tmp_path, happy_path(), markets=[market(end="2026-11-20T12:00:00+00:00")])
    assert kinds(recs, "signal")[0]["skip"] == "ends_soon" and kinds(recs, "order") == []

    C = "333"
    t = T0 + 30 * 60000
    lines = calm() + [book(T0 + m * 60000 + 2, C, 0.30, 0.31) for m in range(30)]
    lines += [book(t - 20000, C, 0.40, 0.41)] + sweep(t)
    lines.sort(key=lambda l: l["r"])
    mk = [market(), market(cid="0xn", tokens=(C, "444"), slug="n")]
    _, recs = run(tmp_path / "b", lines, markets=mk)
    assert kinds(recs, "signal")[0]["skip"] == "siblings_moved"


def test_size_respects_market_cap_and_minimum(tmp_path):
    cfg = Config(max_order=0.5, max_market=0.04)
    _, recs = run(tmp_path, happy_path(), cfg=cfg)
    o = kinds(recs, "order")[0]
    assert o["shares"] * o["price"] == pytest.approx(4.0, abs=0.01)
    _, recs = run(tmp_path / "b", happy_path(), markets=[market(vol=50.0)])
    assert kinds(recs, "skip")[0]["why"] == "too_small:volume"


def test_drawdown_halts_the_pod_and_cancels_entries(tmp_path):
    eng = Engine(Config(), Journal(tmp_path / "j"))
    eng.now = T0
    eng.on_clock(T0)
    eng.cash = 80.0                                               # a 20 % loss appears
    eng.orders[1] = __import__("smart.engine", fromlist=["Order"]).Order(1, A, "BUY", 0.4, 5, T0, 1)
    eng.on_clock(T0 + 2000)
    assert eng.halted == "drawdown" and eng.orders == {}
    assert eng.blocked_day == DAY                                 # the daily breaker fired too
    eng.journal.close()
    whats = [r["what"] for r in kinds(read(tmp_path / "j"), "breaker")]
    assert whats == ["halt", "day_loss"]


def test_stale_feed_blocks_entries_in_live_mode(tmp_path):
    write_day(tmp_path / "data", happy_path())
    eng = Engine(Config(), Journal(tmp_path / "j"), data_dir=tmp_path / "data",
                 wall=lambda: T0 / 1000 + 10 * 86400)
    for ev in replay(tmp_path / "data" / "book", [DAY]):
        eng.on_event(ev)
    eng.journal.close()
    recs = read(tmp_path / "j")
    assert kinds(recs, "signal")[0]["skip"] == "stale_feed" and kinds(recs, "order") == []


def test_state_survives_a_restart_without_replaying_old_fills(tmp_path):
    t = T0 + 30 * 60000
    lines = calm() + sweep(t) + [trade(t + 10000, A, 0.45, 50), book(t + 60000, A, 0.46, 0.48)]
    write_day(tmp_path / "data", lines)
    state = tmp_path / "state.json"
    wall = lambda: (t + 60000) / 1000
    cfg = Config(stale_s=1e9)
    eng = Engine(cfg, Journal(tmp_path / "j"), data_dir=tmp_path / "data", wall=wall, state_path=state)
    for ev in replay(tmp_path / "data" / "book", [DAY]):
        eng.on_event(ev)
    eng.save()
    assert A in eng.positions
    eng2 = Engine(cfg, Journal(tmp_path / "j2"), data_dir=tmp_path / "data", wall=wall, state_path=state)
    for ev in replay(tmp_path / "data" / "book", [DAY]):
        eng2.on_event(ev)
    eng2.journal.close()
    assert eng2.positions[A].shares == eng.positions[A].shares
    assert kinds(read(tmp_path / "j2"), "fill") == [] and kinds(read(tmp_path / "j2"), "signal") == []


def test_cli_backtest_and_report(tmp_path, capsys):
    write_day(tmp_path / "data", happy_path())
    smart_main(["backtest", "--data", str(tmp_path / "data"), "--days", f"{DAY}..{DAY}", "--out", str(tmp_path / "bt")])
    out = capsys.readouterr().out
    assert "Signaux S1 : 1" in out and "1 trades fermés" in out
    smart_main(["report", str(tmp_path / "bt"), "--json"])
    assert json.loads(capsys.readouterr().out)["closed_trades"] == 1
