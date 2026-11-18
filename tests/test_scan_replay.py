"""Replay of data/turbo_scan_results.csv.

Three layers:
  1. every figure the README quotes from the scan is recomputed from the CSV, so the write-up and the
     data cannot drift apart;
  2. the CSV's internal invariants hold (no more wins than cycles, quotes ordered, capacity coherent);
  3. the basket bot is replayed, in simulation, on the order books the scan recorded for the basket
     contracts it covers: every buy must respect the cap, the quantity shown and the budget.
"""
import csv
import statistics
from pathlib import Path

import pytest

import basket_bot as bb

CSV = Path(__file__).resolve().parents[1] / "data" / "turbo_scan_results.csv"
STRATEGIES = ["entry06_target15", "entry06_target20", "entry075_target15", "entry075_target20"]


def num(x):
    return float(x) if x != "" else None


@pytest.fixture(scope="module")
def rows():
    with open(CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------- 1. README figures

def test_universe_is_contracts_between_2_and_35_percent(rows):
    assert len(rows) == 104
    assert len({r["exchange_id"] for r in rows}) == len(rows)
    assert all(0.02 <= num(r["mid_price"]) <= 0.35 for r in rows)


def test_readme_83_wins_out_of_84_cycles(rows):
    p = "entry06_target15"   # bid at 0.6x the 7 day median, sell at 1.5x the entry
    assert sum(int(r[p + "_cycles"]) for r in rows) == 84
    assert sum(int(r[p + "_wins"]) for r in rows) == 83


def test_readme_median_holding_time_10_5_hours(rows):
    hours = [num(r["entry06_target15_median_hours"]) for r in rows if r["entry06_target15_median_hours"]]
    assert statistics.median(hours) == 10.5


def test_readme_only_9_contracts_above_5000_exit_capacity(rows):
    caps = [num(r["entry06_target15_capacity_median_susqies"]) for r in rows]
    assert sum(1 for c in caps if c is not None and c > 5000) == 9


def test_chosen_cycle_has_the_best_win_rate(rows):
    rate = {p: sum(int(r[p + "_wins"]) for r in rows) / sum(int(r[p + "_cycles"]) for r in rows)
            for p in STRATEGIES}
    assert max(rate, key=rate.get) == "entry06_target15"


# ---------------------------------------------------------------- 2. invariants

def test_quotes_are_ordered(rows):
    for r in rows:
        assert num(r["best_bid"]) <= num(r["mid_price"]) <= num(r["best_ask"]), r["exchange_id"]


def test_book_depth_is_coherent_with_best_quotes(rows):
    # depth is summed over several levels: asks sit at or above the best ask, bids at or below the best bid
    for r in rows:
        assert num(r["ask_susqies"]) >= num(r["ask_shares"]) * num(r["best_ask"]) - 1, r["exchange_id"]
        assert num(r["bid_susqies"]) <= num(r["bid_shares"]) * num(r["best_bid"]) + 1, r["exchange_id"]


@pytest.mark.parametrize("p", STRATEGIES)
def test_cycle_statistics_are_coherent(rows, p):
    for r in rows:
        cycles, wins = int(r[p + "_cycles"]), int(r[p + "_wins"])
        multiple = num(r[p + "_compound_multiple"])
        assert 0 <= wins <= cycles, r["exchange_id"]
        assert (r[p + "_median_hours"] == "") == (wins == 0), r["exchange_id"]
        if cycles == 0:
            assert multiple == 1.0, r["exchange_id"]
        if wins == cycles and wins > 0:
            assert multiple > 1.0, r["exchange_id"]


@pytest.mark.parametrize("p", ["entry06_target15", "entry06_target20"])
def test_capacity_columns_come_in_pairs(rows, p):
    for r in rows:
        shares, susqies = r[p + "_capacity_median_shares"], r[p + "_capacity_median_susqies"]
        assert (shares == "") == (susqies == ""), r["exchange_id"]
        if shares:
            assert 0 < num(susqies) <= num(shares), r["exchange_id"]   # every price is below 1


# ---------------------------------------------------------------- 3. bot replay on the scan's books

def test_basket_bot_replayed_on_scan_books(rows, bot, api):
    scanned = {r["title"]: r for r in rows if r["title"] in bb.BASKET}
    assert len(scanned) == 6   # Iowa and Kansas Senate were above 0.35 at scan time
    for r in scanned.values():
        api.market(r["exchange_id"], r["title"], num(r["best_bid"]), num(r["best_ask"]),
                   int(num(r["ask_shares"])))
    bot.run("2026-10-24T12:00:00Z")

    budget = (1 - bb.CASH_BUFFER) * api.equity / len(scanned)
    expected = {r["exchange_id"] for t, r in scanned.items()
                if num(r["best_ask"]) <= bb.tick_down(bb.BASKET[t] * bb.PRICE_CAP) + 1e-9}
    bought = {ex for ex, action, *_ in bot.placed if action == "buy"}
    assert bought == expected
    assert "884" not in bought   # Texas Governor was quoted above its cap: never chased

    spent = 0.0
    for ex, action, side, qty, price in bot.placed:
        r = next(r for r in scanned.values() if r["exchange_id"] == ex)
        assert action == "buy" and side == "yes"
        assert price <= bb.tick_down(bb.BASKET[r["title"]] * bb.PRICE_CAP) + 1e-9
        assert qty <= num(r["ask_shares"])              # only the quantity shown at the best offer
        assert qty * price <= budget + 1e-6
        spent += qty * price
    assert spent <= api.equity - 200
