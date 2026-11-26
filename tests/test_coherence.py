"""Coherence study: question parsing, families, executable value of a violation, replay episodes."""
import csv
import json

import pytest

from smart.__main__ import main as smart_main
from smart.coherence import (Coherence, eval_basket, eval_pair, families, gamma_info, parse_question)
from tests.test_smart import DAY, T0, write_day


def mk(cid, q, yes, no, end="2026-12-31T23:59:00+00:00", event="ev", neg=False, slug=None):
    return {"condition_id": cid, "slug": slug or cid, "question": q, "event_slug": event, "neg_risk": neg,
            "end_date": end, "volume_24h": 1.0, "liquidity": 1.0,
            "tokens": [{"token_id": yes, "outcome": "Yes"}, {"token_id": no, "outcome": "No"}]}


def lv(t, tok, bids, asks, c="s0g1"):
    return {"r": t, "c": c, "m": {"event_type": "book", "asset_id": tok, "market": "x", "timestamp": str(t),
                                  "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
                                  "asks": [{"price": str(p), "size": str(s)} for p, s in asks]}}


# ---------------------------------------------------------------- parsing
def test_parse_thresholds_and_templates():
    a = parse_question("Will Bitcoin reach $120,000 by December 31, 2026?")
    b = parse_question("Will Bitcoin reach $110k by November 30, 2026?")
    assert a["dir"] == "up" and a["strike"] == 120000 and a["cumulative"]
    assert b["strike"] == 110000 and a["template"] == b["template"]
    d = parse_question("Will Bitcoin dip to $55,000 by December 31, 2026?")
    assert d["dir"] == "down" and d["template"] != a["template"]
    assert parse_question("Anthropic IPO closing market cap above $1.75T?")["strike"] == 1.75e12
    assert parse_question("Will BTC be between $90k and $100k?") is None
    assert parse_question("Will the Fed cut rates in December?") is None
    assert not parse_question("Will Bitcoin reach $120,000 in November?")["cumulative"]


def test_families_order_strikes_and_deadlines():
    ms = [mk("hi", "Will Bitcoin reach $120,000 by December 31, 2026?", "h", "hn"),
          mk("lo", "Will Bitcoin reach $110,000 by December 31, 2026?", "l", "ln"),
          mk("nov", "Will Bitcoin reach $120,000 by November 30, 2026?", "n", "nn", end="2026-11-30T23:59:00+00:00"),
          mk("dip", "Will Bitcoin dip to $80,000 by December 31, 2026?", "d1", "d1n"),
          mk("dip2", "Will Bitcoin dip to $70,000 by December 31, 2026?", "d2", "d2n"),
          mk("inN", "Will Bitcoin reach $120,000 in November?", "i1", "i1n", end="2026-11-30T23:59:00+00:00"),
          mk("inD", "Will Bitcoin reach $120,000 in December?", "i2", "i2n")]
    pairs = {tuple(c.legs) for c in families(ms) if c.kind == "pair"}
    assert ("h", "l") in pairs                   # higher strike is harder
    assert ("n", "h") in pairs                   # earlier deadline is harder
    assert ("n", "l") in pairs                   # both
    assert ("d2", "d1") in pairs                 # lower dip is harder
    assert not any({"i1", "i2"} <= set(p) for p in pairs)    # "in November" vs "in December": not nested
    assert len(pairs) == 4


def test_baskets_need_gamma_for_the_buy_side():
    ms = [mk(f"c{i}", f"Will candidate {i} win?", f"y{i}", f"n{i}", event="elec", neg=True) for i in range(3)]
    kinds = {c.kind for c in families(ms)}
    assert kinds == {"basket_sell"}
    kinds = {c.kind for c in families(ms, {"elec": True})}
    assert kinds == {"basket_sell", "basket_buy"}


def test_gamma_completeness_and_fees():
    ms = [mk(f"c{i}", f"Will candidate {i} win?", f"y{i}", f"n{i}", event="elec", neg=True) for i in range(2)]

    def get(url):
        if "/events" in url:
            return [{"markets": [{"conditionId": "c0"}, {"conditionId": "c1"}, {"conditionId": "c2", "closed": True}]}]
        cid = url.split("condition_ids=")[1]
        return [{"feeSchedule": {"rate": 0.04}}] if cid == "c0" else [{"feesEnabled": False}]
    complete, rates, notes = gamma_info(ms, get)
    assert complete == {"elec": True} and rates == {"y0": 0.04, "y1": 0.0}

    def bad(url):
        raise OSError("down")
    complete, rates, _ = gamma_info(ms, bad)
    assert complete == {"elec": False} and rates == {}


# ---------------------------------------------------------------- executable value
def test_eval_pair_walks_both_books_after_fees():
    g, net, sh, usd = eval_pair([(0.40, 100), (0.38, 100)], [(0.35, 50), (0.37, 500)], 0.0, 0.0)
    assert g == pytest.approx(0.05)
    # 50 at 0.40-0.35, 50 at 0.40-0.37, 100 at 0.38-0.37
    assert sh == pytest.approx(200) and usd == pytest.approx(50 * .05 + 50 * .03 + 100 * .01)
    g, net, sh, usd = eval_pair([(0.40, 100)], [(0.39, 100)], 0.07, 0.07)
    assert g > 0 and net < 0 and sh == 0 and usd == 0      # one cent does not pay two taker fees


def test_eval_basket_sell_and_buy():
    g, net, sh, usd = eval_basket([[(0.6, 10)], [(0.5, 20)]], [0, 0], sell=True)
    assert g == pytest.approx(0.1) and sh == 10 and usd == pytest.approx(1.0)
    g, net, sh, usd = eval_basket([[(0.3, 10), (0.31, 10)], [(0.6, 30)]], [0, 0], sell=False)
    assert g == pytest.approx(0.1) and sh == 20 and usd == pytest.approx(10 * .1 + 10 * .09)


# ---------------------------------------------------------------- replay
def two_strikes():
    return [mk("hi", "Will Bitcoin reach $120,000 by December 31, 2026?", "h", "hn"),
            mk("lo", "Will Bitcoin reach $110,000 by December 31, 2026?", "l", "ln")]


def test_episode_measured_after_latency_and_closed():
    co = Coherence(families(two_strikes()), latency_ms=2000, default_fee=0.0)
    for line in [lv(T0, "h", [(0.30, 100)], [(0.32, 100)]), lv(T0 + 1, "l", [(0.33, 100)], [(0.35, 100)]),
                 lv(T0 + 10_000, "h", [(0.38, 40)], [(0.40, 100)]),        # violation: 0.38 > 0.35
                 lv(T0 + 11_000, "h", [(0.37, 40)], [(0.40, 100)]),        # narrower, still there 2 s on
                 lv(T0 + 20_000, "h", [(0.30, 40)], [(0.32, 100)])]:       # gone
        ev = dict(line["m"], _r=line["r"], _c=line["c"])
        co.on_event(ev)
    co.finish()
    assert len(co.episodes) == 1
    e = co.episodes[0]
    assert e.start == T0 + 10_000 and e.end == T0 + 20_000
    assert e.max_gross == pytest.approx(0.03) and e.max_money == pytest.approx(40 * 0.03)
    assert e.lat_net_top == pytest.approx(0.02) and e.lat_money == pytest.approx(40 * 0.02)


def test_closed_connection_cannot_fake_a_violation():
    co = Coherence(families(two_strikes()), latency_ms=2000, default_fee=0.0)
    seq = [dict(lv(T0, "h", [(0.30, 100)], [(0.32, 100)], c="a")["m"], _r=T0, _c="a"),
           dict(lv(T0 + 1, "l", [(0.33, 100)], [(0.35, 100)], c="b")["m"], _r=T0 + 1, _c="b"),
           {"_r": T0 + 5, "_c": "b", "_e": "close", "why": "x"},
           # the h book moves above l's last (now stale) ask: no episode while l is untrusted
           dict(lv(T0 + 10, "h", [(0.40, 100)], [(0.42, 100)], c="a")["m"], _r=T0 + 10, _c="a")]
    for ev in seq:
        co.on_event(ev)
    co.finish()
    assert co.episodes == []


def test_cli_end_to_end(tmp_path):
    lines = [lv(T0, "h", [(0.30, 100)], [(0.32, 100)]), lv(T0 + 1, "l", [(0.33, 100)], [(0.35, 100)]),
             lv(T0 + 10_000, "h", [(0.40, 100)], [(0.42, 100)]),
             lv(T0 + 60_000, "h", [(0.30, 100)], [(0.32, 100)])]
    write_day(tmp_path / "data", lines, two_strikes())
    smart_main(["coherence", "--data", str(tmp_path / "data"), "--days", DAY, "--out", str(tmp_path / "o"),
                "--fee-rate", "0"])
    s = json.loads((tmp_path / "o" / "coherence_summary.json").read_text())
    assert s["by_kind"]["pair"]["episodes"] == 1
    assert s["by_kind"]["pair"]["usd_after_latency"] == pytest.approx(5.0)     # 100 shares x 5 c
    top = s["top_constraints"][0]
    assert top["share_of_time_violated"] == pytest.approx(50 / 59.999, rel=1e-3)
    rows = list(csv.DictReader(open(tmp_path / "o" / "families.csv")))
    assert rows[0]["legs"] == "hi | lo"
