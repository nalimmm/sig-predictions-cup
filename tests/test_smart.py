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


def test_study_measures_reversion_fills_and_trade_signs(tmp_path, capsys):
    t = T0 + 30 * 60000
    lines = happy_path() + [book(t + 70 * 60000, A, 0.50, 0.51)]
    write_day(tmp_path / "data", lines)
    smart_main(["study", "--data", str(tmp_path / "data"), "--days", DAY, "--out", str(tmp_path / "st")])
    s = json.loads(capsys.readouterr().out)
    blk = s["sweeps_s1_with_filters"]
    assert blk["n"] == 1 and blk["reverted"]["300s"]["mean"] == pytest.approx(0.818, abs=0.001)
    paper = blk["paper"]["latence_2s"]
    assert paper["filled"] == 1 and paper["states"] == {"closed_target": 1} and paper["mean_pnl_cents"] == 3.0
    assert blk["paper"]["latence_7s"]["states"] == {"unfilled": 1}      # placed after the trade through
    assert s["by_flow_after_(s1_with_filters)"]["suivi"]["n"] == 0
    assert s["side_check"]["sell_at_or_below_bid"] >= 1 and s["side_check"]["trades"] == 5
    ev = [json.loads(l) for l in (tmp_path / "st" / "sweeps.jsonl").read_text().splitlines()]
    assert len(ev) == 1 and ev[0]["mids"]["3600"] == pytest.approx(0.505)


def test_study_stylised_facts_fold_both_tokens(tmp_path):
    from smart.study import Study
    lines = [book(T0, A, 0.50, 0.51), book(T0 + 1, B, 0.49, 0.50)]
    for i in range(300):                       # persistent flow: runs of 10 buys of YES, 10 buys of NO
        yes = (i // 10) % 2 == 0
        lines.append(trade(T0 + 1000 + i * 100, A if yes else B, 0.51 if yes else 0.50, 10, "BUY"))
    write_day(tmp_path / "data", lines)
    st = Study(tmp_path / "data")
    for ev in replay(tmp_path / "data" / "book", [DAY]):
        st.on_event(ev)
    f = st.stylised()
    assert f["markets"] == 1 and f["trades"] == 300 and f["share_buys"] == 0.5
    assert f["C"][1] == pytest.approx(0.8, abs=0.02) and f["C"][10] < -0.9


def test_mm_markouts_split_spread_earned_and_adverse_move(tmp_path):
    from smart.mm_study import Markouts
    lines = [book(T0, A, 0.50, 0.52), book(T0 + 1, B, 0.48, 0.50)]
    for i in range(60):                        # buys of YES at the ask; the mid then rises one cent
        t = T0 + 1000 + i * 400000
        lines += [book(t - 10, A, 0.50, 0.52), trade(t, A, 0.52, 10, "BUY"), book(t + 30000, A, 0.51, 0.53)]
    write_day(tmp_path / "data", lines)
    mk = Markouts(tmp_path / "data")
    for ev in replay(tmp_path / "data" / "book", [DAY]):
        mk.on_event(ev)
    row = mk.rows()[0]
    assert row["half_spread_earned_c"] == pytest.approx(1.0) and row["edge_10s_c"] == pytest.approx(1.0)
    assert row["edge_60s_c"] == pytest.approx(0.0, abs=1e-6)       # the cent earned is lost to the move
    assert row["share_buys"] == 1.0 and row["trades_per_day"] == 60


def test_mm_markouts_mirror_trades_of_the_other_token(tmp_path):
    from smart.mm_study import Markouts
    lines = [book(T0, A, 0.50, 0.52), book(T0 + 1, B, 0.48, 0.50)]
    lines += [trade(T0 + 1000 + i * 1000, B, 0.50, 10, "BUY") for i in range(50)]      # = sells of YES at 0.50
    lines.append(book(T0 + 3600000, A, 0.50, 0.52))
    write_day(tmp_path / "data", lines)
    mk = Markouts(tmp_path / "data")
    for ev in replay(tmp_path / "data" / "book", [DAY]):
        mk.on_event(ev)
    row = mk.rows()[0]
    assert row["token"] == A and row["share_buys"] == 0.0 and row["half_spread_earned_c"] == pytest.approx(1.0)


def test_sig_cycle_replay_uses_past_hours_only_and_marks_open_cycles():
    from smart.mm_study import scan_token
    prints = [(h * 3600.0, 0.20, 100.0) for h in range(100)]       # anchor 0.20 once 72 hours exist
    prints += [(100 * 3600.0, 0.11, 50.0),                         # through the 0.12 bid (x0.60): filled
               (101 * 3600.0, 0.19, 50.0),                         # through the 0.18 target (x1.50): won
               (102 * 3600.0, 0.10, 50.0),                         # filled again
               (103 * 3600.0, 0.06, 50.0)]                         # never comes back: marked at 0.06
    r = scan_token(prints)["x0.60_x1.50"]
    assert r["cycles"] == 2 and r["wins"] == 1 and r["open"] == 1
    assert r["ret"][0] == pytest.approx(0.5) and r["open_mark_ret"][0] == pytest.approx(-0.5)
    assert scan_token(prints[:60])["x0.60_x1.50"]["cycles"] == 0   # not enough history: no bid


def wide(t, bid, ask):
    """Books of YES and NO around a wide spread."""
    return [book(t, A, bid, ask), book(t + 1, B, round(1 - ask, 2), round(1 - bid, 2))]


def mm_run(tmp_path, lines, **cfg):
    from smart.mm import MMConfig, MMSim
    write_day(tmp_path / "data", lines)
    sim = MMSim(tmp_path / "data", ["m"], MMConfig(**cfg))
    for ev in replay(tmp_path / "data" / "book", [DAY]):
        sim.on_event(ev)
    q = sim.q[A]
    return sim, q, sim.result(q, 1.0)


def test_mm_quotes_inside_a_wide_spread_and_earns_it_on_a_round_trip(tmp_path):
    lines = wide(T0, 0.40, 0.50) + wide(T0 + 5000, 0.40, 0.50)
    lines += [trade(T0 + 10000, A, 0.40, 100, "SELL")]              # prints under our 0.41 bid: we buy
    lines += wide(T0 + 20000, 0.40, 0.50) + wide(T0 + 25000, 0.40, 0.50)
    lines += [trade(T0 + 30000, B, 0.50, 100, "BUY")]               # buys NO at 0.50 = sells YES at 0.50: not through our ask
    lines += [trade(T0 + 40000, A, 0.50, 100, "BUY")]               # prints above our ask: we sell
    lines += wide(T0 + 50000, 0.40, 0.50)
    sim, q, r = mm_run(tmp_path, lines, sweep_usdc=1e9)
    assert [(f["side"], f["price"]) for f in q.fills][0] == ("BUY", 0.41)
    assert q.fills[1]["side"] == "SELL" and q.fills[1]["price"] in (0.48, 0.49)     # skewed down while long
    assert r["round_trip_shares"] > 0 and r["spread_captured_usdc"] > 0 and r["pnl_if_liquidated_usdc"] > 0


def test_mm_stays_behind_a_one_tick_spread_and_is_not_filled_by_prints_at_the_best(tmp_path):
    lines = wide(T0, 0.40, 0.41) + wide(T0 + 5000, 0.40, 0.41)       # one tick spread: no room inside
    lines += [trade(T0 + 10000, A, 0.40, 100, "SELL"), trade(T0 + 11000, A, 0.41, 100, "BUY")]
    sim, q, r = mm_run(tmp_path, lines, sweep_usdc=1e9)
    assert (q.bid, q.ask) == (0.39, 0.42) and q.fills == []


def test_mm_pulls_the_bid_during_a_sweep_and_respects_the_inventory_cap(tmp_path):
    lines = wide(T0, 0.40, 0.50) + wide(T0 + 5000, 0.40, 0.50)
    lines += [trade(T0 + 10000 + i, A, 0.40, 400, "SELL") for i in range(3)]       # 480 USDC of selling
    lines += wide(T0 + 12000, 0.40, 0.50) + wide(T0 + 15000, 0.40, 0.50)
    sim, q, r = mm_run(tmp_path, lines)
    assert len(q.fills) == 1 and q.bid is None and q.pulls == 1      # hit once, then out of the way
    lines = wide(T0, 0.40, 0.50)
    for i in range(1, 40):                                           # sellers keep coming, slowly
        lines += wide(T0 + i * 100000, 0.40, 0.50) + [trade(T0 + i * 100000 + 50000, A, 0.39, 100, "SELL")]
    sim, q, r = mm_run(tmp_path / "b", lines, sweep_usdc=1e9)
    assert 0 < q.inv <= 10.0 / 0.45 + 1e-6 and q.inv * 0.45 <= 10.5  # never more than 10 USDC long


def test_mm_stops_opening_near_the_end_date(tmp_path):
    from smart.mm import MMConfig, MMSim
    lines = wide(T0, 0.40, 0.50) + wide(T0 + 5000, 0.40, 0.50) + [trade(T0 + 10000, A, 0.39, 100, "SELL")]
    write_day(tmp_path / "data", lines, markets=[market(end="2026-11-20T12:00:00+00:00")])
    sim = MMSim(tmp_path / "data", ["m"], MMConfig())
    for ev in replay(tmp_path / "data" / "book", [DAY]):
        sim.on_event(ev)
    assert sim.q[A].fills == [] and sim.q[A].bid is None


def test_mm_does_not_add_to_a_short_while_the_price_runs_against_it_then_stops(tmp_path):
    # the market climbs from 0.80 to 0.95 and lifts our ask at every step
    lines, t = [], T0
    for i in range(16):
        b = round(0.80 + 0.01 * i, 2)
        lines += wide(t, b, round(b + 0.05, 2)) + wide(t + 5000, b, round(b + 0.05, 2))
        lines += [trade(t + 10000, A, round(b + 0.07, 2), 500, "BUY")]
        t += 200000
    lines += wide(t, 0.95, 0.99)
    sim, q, r = mm_run(tmp_path, lines, sweep_usdc=1e9, sweep_ticks=99, max_price=0.99)
    assert q.inv < 0
    assert -q.inv * (1 - q.avg) <= 10.0 + 1e-6          # the worst case of the short never passes the cap
    assert r["stopped"] and q.cash + q.inv * q.last_mid > -10.0


def test_mm_state_survives_a_restart_and_nothing_trades_while_down(tmp_path):
    import json as _json
    from smart.dash import snapshot
    from smart.mm import MMConfig, MMSim
    first = wide(T0, 0.40, 0.50) + wide(T0 + 5000, 0.40, 0.50) + [trade(T0 + 10000, A, 0.40, 100, "SELL")]
    first += wide(T0 + 400000, 0.40, 0.50)
    down = [trade(T0 + 500000, A, 0.60, 100, "BUY")]                # while stopped: must not fill our old ask
    after = wide(T0 + 900000, 0.40, 0.50) + wide(T0 + 905000, 0.40, 0.50) + [trade(T0 + 910000, A, 0.60, 100, "BUY")]
    sim, q, _ = mm_run(tmp_path / "a", first, sweep_usdc=1e9)
    assert len(q.fills) == 1 and q.ask is not None
    st = _json.loads(_json.dumps(sim.state()))
    legacy = MMSim.state_from_snapshot(_json.loads(_json.dumps(snapshot(sim))))     # what an older version left behind
    for k in ("inv", "cash", "avg", "bought", "buy_cost"):
        assert abs(legacy["quoters"][0][k] - st["quoters"][0][k]) < 1e-6
    assert [list(x) for x in legacy["quoters"][0]["nav_path"]] == [list(x) for x in q.nav_path] and len(legacy["quoters"][0]["fills"]) == 1

    for state in (st, legacy):
        d = tmp_path / ("b" + str(id(state)))
        write_day(d, first + down + after)
        new = MMSim(d, ["m"], MMConfig(sweep_usdc=1e9))
        new.restore(state, T0 + 800000)
        for ev in replay(d / "book", [DAY]):
            new.on_event(ev)
        r = new.q[A]
        assert list(new.q) == [A] and r.end_ms is not None
        assert [f["side"] for f in r.fills] == ["BUY", "SELL"] and r.fills[1]["t"] == T0 + 910000
        assert abs(r.bought - q.bought) < 1e-9 and r.nav_path[0] == tuple(q.nav_path[0])
    assert q.quotes[0][1:] == (None, None) and (0.41, 0.49) in [x[1:] for x in q.quotes]    # where the quotes rested
    assert snapshot(sim)["markets"][0]["qt"][-1][1:] == [q.bid, q.ask] and st["quoters"][0]["quotes"]
    back = MMSim(tmp_path / "a" / "data", ["m"], MMConfig())
    back.restore(st, T0)
    assert list(back.q["saved:m"].quotes) == list(q.quotes)                         # kept across the restart


def test_mm_spread_is_measured_against_the_mid_of_each_fill():
    from smart.mm import MMSim, Quoter
    q = Quoter(token="t", slug="m", end_ms=None)
    q.nav_path = [(0, 0.0, 0.50, 0.0), (300000, 0.0, 0.60, 0.0)]
    q.fills = [{"t": 1000, "side": "BUY", "price": 0.49, "shares": 10, "mid": 0.50},      # one cent inside the mid
               {"t": 400000, "side": "SELL", "price": 0.61, "shares": 10, "mid": None}]    # restored: mid 0.60 from the path
    assert abs(MMSim.edge(q) - 0.20) < 1e-9          # 2 cents of spread; the 10 cents of price move are not spread
    q.fees = 0.05
    assert abs(MMSim.edge(q) - 0.15) < 1e-9


C, D = "333", "444"
BTC1, BTC2 = "will-bitcoin-dip-to-60k-in-x", "will-bitcoin-dip-to-55k-in-x"


def two_btc(tmp_path, lines, spot=None, **cfg):
    from smart.mm import MMConfig, MMSim
    write_day(tmp_path / "data", lines, markets=[market(slug=BTC1), market(slug=BTC2, cid="0xn", tokens=(C, D))])
    sim = MMSim(tmp_path / "data", [BTC1, BTC2], MMConfig(sweep_usdc=1e9, **cfg))
    sim.spot = spot
    for ev in replay(tmp_path / "data" / "book", [DAY]):
        sim.on_event(ev)
    return sim, sim.q[A], sim.q[C]


def wide2(t, bid, ask):
    return [book(t, C, bid, ask), book(t + 1, D, round(1 - ask, 2), round(1 - bid, 2))]


def test_mm_markets_on_one_underlying_share_a_cap(tmp_path):
    lines = wide(T0, 0.40, 0.50) + wide2(T0 + 10, 0.40, 0.50) + wide(T0 + 5000, 0.40, 0.50)
    lines += [trade(T0 + 10000, A, 0.40, 100, "SELL")]              # we buy on the first strike
    lines += wide2(T0 + 20000, 0.40, 0.50) + wide2(T0 + 25000, 0.40, 0.50)
    lines += [trade(T0 + 30000, C, 0.40, 100, "SELL")] + wide2(T0 + 40000, 0.40, 0.50)
    _, q1, q2 = two_btc(tmp_path / "a", lines)
    assert q1.under == q2.under == "Bitcoin" and len(q1.fills) == 1 and len(q2.fills) == 1
    _, q1, q2 = two_btc(tmp_path / "b", lines, max_group_usdc=4.0)  # 2.73 at risk on the first: no room for 5 shares more
    assert len(q1.fills) == 1 and q2.fills == []


def test_mm_stops_adding_to_the_net_delta_at_its_limit(tmp_path):
    lines = wide(T0, 0.40, 0.50) + wide(T0 + 5000, 0.40, 0.50) + [trade(T0 + 10000, A, 0.40, 100, "SELL")]
    lines += wide(T0 + 60000, 0.40, 0.50) + wide(T0 + 65000, 0.40, 0.50) + [trade(T0 + 70000, A, 0.38, 100, "SELL")]
    lines += wide(T0 + 80000, 0.40, 0.50)
    _, q, _ = two_btc(tmp_path / "a", lines)                                        # no BTC price: no delta limit
    assert [f["side"] for f in q.fills] == ["BUY", "BUY"]
    sim, q, _ = two_btc(tmp_path / "b", lines, spot=80000.0, max_net_delta_usdc=0.05)
    assert q.dps < 0 and q.inv * q.dps < -0.05                                      # long the dip = short BTC, past the limit
    assert [f["side"] for f in q.fills] == ["BUY"] and q.bid is None and q.ask is not None


def test_mm_sells_what_is_left_into_the_book_before_the_end_date(tmp_path):
    from smart.mm import MMConfig, MMSim
    lines = wide(T0, 0.40, 0.50) + wide(T0 + 5000, 0.40, 0.50) + [trade(T0 + 10000, A, 0.40, 100, "SELL")]
    lines += wide(T0 + 2 * 3600000, 0.40, 0.50) + wide(T0 + 2 * 3600000 + 5000, 0.40, 0.50)     # the old ask is cancelled
    write_day(tmp_path / "data", lines, markets=[market(end="2026-11-20T10:30:00+00:00")])
    sim = MMSim(tmp_path / "data", ["m"], MMConfig(sweep_usdc=1e9, exit_hours=10, min_hours_left=9))
    for ev in replay(tmp_path / "data" / "book", [DAY]):
        sim.on_event(ev)
    q = sim.q[A]
    assert [(f["side"], f["price"], f.get("taker", False)) for f in q.fills] == [("BUY", 0.41, False), ("SELL", 0.40, True)]
    assert abs(q.inv) < 1e-9 and q.fees > 0 and q.bid is None and q.ask is None
    assert abs(q.cash - (-q.fills[0]["shares"] * 0.01 - q.fees)) < 1e-6             # lost the tick and the taker fee


def test_dashboard_snapshot_is_sealed_with_the_password_and_never_uploaded_in_clear(tmp_path):
    import json as _json
    import pytest
    from smart.dash import Publisher, decrypt, encrypt
    lines = wide(T0, 0.40, 0.50) + wide(T0 + 5000, 0.40, 0.50) + [trade(T0 + 10000, A, 0.40, 100, "SELL")]
    lines += wide(T0 + 400000, 0.40, 0.50)
    sim, q, r = mm_run(tmp_path, lines, sweep_usdc=1e9)
    sent = []

    class Client:
        def put_object(self, **kw):
            sent.append(kw)

    pw = "une phrase assez longue"
    pub = Publisher(tmp_path / "dash", pw, Client(), "bucket", clock=lambda: 1000.0)
    assert pub.maybe(sim) and not pub.maybe(sim)                    # once per period
    d = _json.loads((tmp_path / "dash" / "data.json").read_text())
    assert d["mode"] == "paper" and d["markets"][0]["fills"] == 1 and d["markets"][0]["fl"][0][1] == 1
    assert len(sent) == 2 and sent[0]["Key"].endswith("data.enc") and b"slug" not in sent[0]["Body"]
    assert sent[1]["Key"].endswith("data-guest.enc") and sent[1]["Body"] == b"revoked"     # no guest password: no guest access
    m = d["markets"][0]                                             # what was paid for the position is what can be lost
    assert abs(m["risk"] - m["inv"] * m["fl"][0][2]) < 0.01 and abs(d["invested"] - m["risk"]) < 1e-6
    assert d["greeks"] is None and d["capital"] == 100.0 and d["live_from"] == 1000000 and d["missing"] == []
    assert d["exposure"] == {m["under"]: m["risk"]} and d["hourly"]["n"] < 2 and d["hourly"]["sharpe"] is None
    assert _json.loads(decrypt(sent[0]["Body"], pw)) == d
    with pytest.raises(Exception):
        decrypt(sent[0]["Body"], "mauvais mot de passe !")
    with pytest.raises(ValueError):
        encrypt(b"x", "court")
    # a visitor's copy under another password; taking the password away overwrites it
    sent.clear()
    pub = Publisher(tmp_path / "g", pw, Client(), "bucket", clock=lambda: 1000.0, guest_password="une autre phrase longue")
    pub.maybe(sim)
    assert [x["Key"] for x in sent] == ["smart-dashboard/data.enc", "smart-dashboard/data-guest.enc"]
    g = _json.loads(decrypt(sent[1]["Body"], "une autre phrase longue"))
    assert g["guest"] is True and g["total_mid"] == d["total_mid"]
    with pytest.raises(Exception):
        decrypt(sent[1]["Body"], pw)                                # the owner's password does not open the guest copy
    with pytest.raises(Exception):
        decrypt(sent[0]["Body"], "une autre phrase longue")         # nor the guest's the owner's
    sent.clear()
    t = [1000.0]
    pub = Publisher(tmp_path / "g", pw, Client(), "bucket", clock=lambda: t[0])
    pub.maybe(sim)
    t[0] += 100
    pub.maybe(sim)
    assert [x["Key"] for x in sent].count("smart-dashboard/data-guest.enc") == 1 and sent[1]["Body"] == b"revoked"
    assert not (tmp_path / "g" / "data-guest.enc").exists()


def test_dashboard_hourly_statistics_and_underlyings():
    from smart.dash import hourly, underlying
    h = hourly([[0, 0.0], [3600, 1.0], [7200, 3.0], [10800, 2.0]], 0)      # hourly changes +1, +2, -1
    assert h["n"] == 3 and abs(h["mean"] - 2 / 3) < 1e-3 and abs(h["var"] - 7 / 3) < 1e-3
    assert abs(h["sharpe"] - (2 / 3) / (7 / 3) ** 0.5) < 1e-3
    assert underlying("will-bitcoin-dip-to-50k-in-november-2026") == underlying("btc-above-90k") == "Bitcoin"
    assert underlying("some-election") == "some-election"


def test_greeks_of_a_bitcoin_barrier_market():
    from smart.greeks import barrier, greeks, implied_vol, portfolio, touch
    assert barrier("will-bitcoin-dip-to-52pt5k-in-november-2026") == (52500.0, -1)
    assert barrier("will-bitcoin-dip-to-40000-by-december-31-2026-593-522") == (40000.0, -1)
    assert barrier("will-bitcoin-reach-150k-in-2026") == (150000.0, 1) and barrier("some-election") is None
    p = touch(80000, 60000, -1, 0.5, 0.25)
    assert 0 < p < 1 and abs(implied_vol(p, 80000, 60000, -1, 0.25) - 0.5) < 1e-9          # round trip
    assert touch(59000, 60000, -1, 0.5, 0.25) == 1.0 and implied_vol(0.995, 80000, 60000, -1, 0.25) is None
    g = greeks(p, 80000, 60000, -1, 0.25)
    assert g["delta"] < 0 < g["vega"] and g["theta"] < 0            # a dip is likelier when BTC falls, vol rises, time remains
    now = 1_700_000_000_000
    end = now + int(0.25 * 365 * 86400 * 1000)
    pf = portfolio([("will-bitcoin-dip-to-60k-x", -10.0, p, end), ("some-election", 5.0, 0.5, end)], 80000.0, now)
    assert pf["priced"] == 1 and abs(pf["delta"] + 10 * g["delta"]) < 1e-3 and pf["delta"] > 0     # short YES is long BTC
    assert portfolio([], None, now) is None


def test_scan_keeps_steady_edges_one_per_event_and_says_why_it_rejects():
    from smart.mm_study import scan
    base = {"event": "e", "end_date": "2026-12-31", "tick": 0.01, "mid": 0.5, "trades_per_day": 100, "usdc_per_day": 5000,
            "spread_c": 3.0, "depth_best_usdc": 200, "half_spread_earned_c": 1.2, "edge_300s_c": 0.6, "edge_1800s_c": 0.4,
            "edge_300s_t": 4.0}
    rows = [{**base, "slug": "steady"}, {**base, "slug": "sibling", "edge_300s_c": 0.5},
            {**base, "slug": "noisy", "event": "n", "edge_300s_c": 0.9, "edge_300s_t": 1.2},
            {**base, "slug": "tight", "event": "t", "spread_c": 1.0},
            {**base, "slug": "toxic", "event": "x", "edge_300s_c": -0.3},
            {**base, "slug": "other", "event": "o", "edge_300s_c": 0.4, "edge_300s_t": 3.0}]
    r = {x["slug"]: x for x in scan(rows, "2026-11-21")}
    assert r["steady"]["rank"] == 1 and r["other"]["rank"] == 2
    assert r["steady"]["adverse_selection_c"] == 0.6 and r["steady"]["score"] == 0.45
    assert "same event" in r["sibling"]["rejected"] and "significant" in r["noisy"]["rejected"]
    assert "tight" in r["tight"]["rejected"] and "no edge" in r["toxic"]["rejected"]
