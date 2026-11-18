"""Unit tests for the price and sizing rules of src/basket_bot.py.

Each test pins one rule from the module docstring (and the README) to numbers, so a future port of the
strategy (for example to Polymarket) can be checked against the behaviour that actually ran.
"""
import pytest

import basket_bot as bb
from conftest import basket

DAY = "2026-10-28T12:00:00Z"          # build phase: day buys on, guard off
GUARD = "2026-11-03T23:30:00Z"        # day buys over, rescue armed, count not started
NIGHT = "2026-11-04T01:00:00Z"        # vote count: rolling buys on
LATE_NIGHT = "2026-11-04T16:30:00Z"   # after NIGHT_BUYS_END, before the freeze

# exchange ids given by conftest.basket(): b0 Iowa Sen 0.438, b1 Kansas Sen 0.417, b2 South Carolina
# Sen 0.232, b3 Texas Gov 0.297, b4 Florida Gov 0.203, b5 Kansas Gov 0.253, b6 Vermont Gov 0.263,
# b7 Nebraska Sen 0.268


# ---------------------------------------------------------------- tick rounding

@pytest.mark.parametrize("p, down, up", [
    (0.2552, 0.255, 0.26),
    (0.255, 0.255, 0.255),
    (0.6, 0.6, 0.6),
    (3 * 0.3, 0.9, 0.9),        # 0.8999999999 must not fall to 0.895
    (0.0001, 0.005, 0.005),     # never below one tick
    (1.2, 0.995, 0.995),        # never above 0.995
])
def test_tick_rounding(p, down, up):
    assert bb.tick_down(p) == down
    assert bb.tick_up(p) == up


# ---------------------------------------------------------------- laddered sells

def ladder(monkeypatch, q, avg, existing=()):
    placed, cancelled = [], []
    monkeypatch.setattr(bb, "place", lambda tid, ex, a, qty, price, side="yes": placed.append((qty, price)))
    monkeypatch.setattr(bb, "cancel", lambda o, reason: cancelled.append(o["id"]))
    sells = [{"id": f"s{i}", "quantity": qty, "priceLimit": price} for i, (qty, price) in enumerate(existing)]
    bb.laddered_sells("tid", "ex", sells, q, avg)
    return placed, cancelled


@pytest.mark.parametrize("avg, low, high", [
    (0.30, 0.60, 0.90),     # 2x and 3x
    (0.25, 0.50, 0.75),     # 3x leg, avg exactly at or above 0.20
    (0.20, 0.40, 0.60),     # boundary: 0.20 is NOT below HIGH_LEG_BELOW, so 3x
    (0.15, 0.30, 0.75),     # below 0.20: second half at 5x
    (0.40, 0.80, 0.95),     # 3x capped at 0.95
])
def test_ladder_targets(monkeypatch, avg, low, high):
    placed, cancelled = ladder(monkeypatch, 1000, avg)
    assert placed == [(500, low), (500, high)]
    assert cancelled == []


def test_ladder_odd_quantity_puts_extra_share_on_high_leg(monkeypatch):
    placed, _ = ladder(monkeypatch, 1001, 0.30)
    assert placed == [(500, 0.60), (501, 0.90)]


def test_ladder_collapses_to_one_leg_when_both_capped(monkeypatch):
    placed, _ = ladder(monkeypatch, 1000, 0.48)   # 2x = 0.96 -> 0.95, 3x -> 0.95
    assert placed == [(1000, 0.95)]


def test_ladder_reuses_orders_already_at_target(monkeypatch):
    placed, cancelled = ladder(monkeypatch, 1000, 0.30, existing=[(500, 0.60), (500, 0.90)])
    assert placed == [] and cancelled == []


def test_ladder_tops_up_a_partial_leg(monkeypatch):
    placed, cancelled = ladder(monkeypatch, 1000, 0.30, existing=[(500, 0.60), (200, 0.90)])
    assert cancelled == []
    assert placed == [(300, 0.90)]


def test_ladder_replaces_orders_after_average_cost_moves(monkeypatch):
    placed, cancelled = ladder(monkeypatch, 1000, 0.25, existing=[(500, 0.60), (500, 0.90)])
    assert cancelled == ["s0", "s1"]
    assert placed == [(500, 0.50), (500, 0.75)]


def test_ladder_cancels_excess_at_the_right_price(monkeypatch):
    placed, cancelled = ladder(monkeypatch, 1000, 0.30, existing=[(800, 0.60), (500, 0.90)])
    assert cancelled == ["s0"]
    assert placed == [(500, 0.60)]


# ---------------------------------------------------------------- rescue and floor

def test_no_rescue_before_the_guard_is_armed(api, bot):
    basket(api, b3=(0.05, 0.06, 0))
    api.position("b3", 1000, 0.30)
    bot.run(DAY)
    assert bot.sells("b3") == [(500, 0.60), (500, 0.90)]


def test_rescue_below_half_the_average_cost(api, bot):
    basket(api, b3=(0.147, 0.16, 0))          # threshold = max(0.5 x 0.30, 0.10) = 0.15
    api.position("b3", 1000, 0.30)
    api.order("b3", "sell", 500, 0.60, oid="ladder_low")
    bot.run(GUARD)
    assert bot.sells("b3") == [(1000, 0.145)]  # whole position at tick_down(best bid)
    assert ("ladder_low", "selling to the best bidder") in bot.cancelled


def test_no_rescue_just_above_threshold(api, bot):
    basket(api, b3=(0.155, 0.16, 0))
    api.position("b3", 1000, 0.30)
    bot.run(GUARD)
    assert bot.sells("b3") == [(500, 0.60), (500, 0.90)]


def test_absolute_floor_rescues_cheap_positions(api, bot):
    # The Florida case on election night: avg 0.15, so 50 percent of cost (0.075) would never fire
    # before the contract is nearly worthless; the 0.10 floor does.
    basket(api, b4=(0.10, 0.11, 0))
    api.position("b4", 55_720, 0.15)
    bot.run(NIGHT)
    assert bot.sells("b4") == [(55_720, 0.10)]


def test_floor_does_not_fire_above_it(api, bot):
    basket(api, b4=(0.105, 0.11, 0))
    api.position("b4", 1000, 0.15)
    bot.run(NIGHT)
    assert bot.sells("b4") == [(500, 0.30), (500, 0.75)]


# ---------------------------------------------------------------- cleanup outside the basket

def test_positions_outside_the_basket_are_dumped(api, bot):
    basket(api)
    api.market("x1", "Will the Republican Party win the Ohio Senate?", 0.412, 0.42)
    api.market("x2", "Will the Democratic Party win the Ohio Senate?", 0.57, 0.583)
    api.position("x1", 300, 0.30)
    api.position("x2", -200, 0.40)
    api.order("x9", "buy", 100, 0.10, oid="stray_buy")
    bot.run(DAY)
    assert ("x1", "sell", "yes", 300, 0.41) in bot.placed
    assert ("x2", "sell", "no", 200, round(1 - 0.585, 3)) in bot.placed
    assert ("stray_buy", "buy outside the basket") in bot.cancelled


def test_sells_exceeding_position_are_cancelled(api, bot):
    basket(api, b3=(0.30, 0.31, 0))
    api.position("b3", 100, 0.30)
    api.order("b3", "sell", 500, 0.60, oid="too_big")
    bot.run(DAY)
    assert ("too_big", "YES sells exceed the position") in bot.cancelled
    assert bot.sells("b3") == [(50, 0.60), (50, 0.90)]


# ---------------------------------------------------------------- day buys: cap and sizing

def test_day_buy_takes_only_the_best_offer_up_to_budget(api, bot):
    basket(api, b2=(0.25, 0.255, 100_000))     # cap = tick_down(0.232 x 1.10) = 0.255
    bot.run(DAY)
    budget = (1 - bb.CASH_BUFFER) * 100_000 / 8
    assert bot.buys("b2") == [(int(budget / 0.255), 0.255)]


def test_day_buy_takes_only_the_quantity_shown(api, bot):
    basket(api, b2=(0.25, 0.25, 7_000))
    bot.run(DAY)
    assert bot.buys("b2") == [(7_000, 0.25)]


def test_day_buy_never_above_reference_plus_ten_percent(api, bot):
    basket(api, b2=(0.25, 0.26, 100_000))      # one tick above the 0.255 cap
    bot.run(DAY)
    assert bot.buys() == []


def test_day_buys_cheapest_reference_first_until_cash_runs_out(api, bot):
    api.equity = 10_000                         # budget 1,225 per contract, cash 10,000
    basket(api, **{ex: (0.19, 0.20, 1_000_000) for ex in ("b2", "b4", "b5")})
    bot.run(DAY)
    assert [ex for ex, a, *_ in bot.placed if a == "buy"] == ["b4", "b2", "b5"]
    assert all(q * p <= 1_225 for q, p in bot.buys())


def test_spendable_cash_uses_market_value_not_cost_basis(api, bot):
    # Regression for the cash bug in the README: unrealized gains are not cash.
    basket(api, b4=(0.19, 0.20, 1_000_000))
    api.position("b0", 100_000, 0.50, market_value=95_000)   # cost 50,000, worth 95,000
    bot.run(DAY)
    cash = 100_000 - 95_000
    assert bot.buys("b4") == [(int((cash - 200) / 0.20), 0.20)]


def test_no_buy_once_budget_is_reached(api, bot):
    basket(api, b4=(0.19, 0.20, 1_000_000))
    api.position("b4", 61_200, 0.20)          # cost 12,240: room 10 below the 100 minimum
    bot.run(DAY)
    assert bot.buys() == []


def test_one_resting_buy_per_contract(api, bot):
    basket(api, b4=(0.19, 0.20, 1_000_000))
    api.order("b4", "buy", 1000, 0.20, oid="keep")
    api.order("b4", "buy", 1000, 0.21, oid="dup")
    api.order("b2", "buy", 1000, 0.26, oid="stale")    # above the 0.255 cap
    bot.run(DAY)
    assert ("dup", "stale level or duplicate") in bot.cancelled
    assert ("stale", "stale level or duplicate") in bot.cancelled
    assert "keep" not in [oid for oid, _ in bot.cancelled]
    assert bot.buys("b4") == []


def test_no_day_buys_once_guard_is_armed(api, bot):
    basket(api, b4=(0.19, 0.20, 1_000_000))
    api.order("b4", "buy", 1000, 0.20, oid="day_buy")
    bot.run(GUARD)
    assert bot.buys() == []
    assert ("day_buy", "buy window closed") in bot.cancelled


# ---------------------------------------------------------------- night rolling

def precount(api, ex, ref, after=None):
    api.candles[ex] = [{"time": "2026-11-03T22:00:00Z", "close": ref - 0.01},
                       {"time": "2026-11-03T23:00:00Z", "close": ref}]
    if after is not None:   # candles after COUNT_START must never move the reference
        api.candles[ex].append({"time": "2026-11-04T00:00:00Z", "close": after})


def test_night_cap_is_min_of_080_and_15x_precount(api, bot):
    basket(api, b3=(0.47, 0.48, 1_000_000), b6=(0.70, 0.80, 1_000_000))
    precount(api, "b3", 0.32, after=0.90)      # cap = min(0.80, 0.48) = 0.48
    precount(api, "b6", 0.60)                  # cap = min(0.80, 0.90) = 0.80
    bot.run(NIGHT)
    budget = bb.NIGHT_MAX_SHARE * 100_000
    assert bot.buys("b3") == [(int(budget / 0.48), 0.48)]
    assert bot.buys("b6") == [(int(budget / 0.80), 0.80)]   # each contract capped at 35 percent


def test_night_never_buys_above_cap(api, bot):
    basket(api, b3=(0.48, 0.485, 1_000_000))
    precount(api, "b3", 0.32)
    bot.run(NIGHT)
    assert bot.buys() == []


def test_night_never_rolls_into_a_dropping_race(api, bot):
    basket(api, b4=(0.159, 0.17, 1_000_000))   # bid below 0.80 x 0.20 = 0.16
    precount(api, "b4", 0.20)
    bot.run(NIGHT)
    assert bot.buys() == []


def test_night_rolls_cheapest_ask_first(api, bot):
    api.equity = 1_000
    basket(api, b0=(0.39, 0.40, 1_000_000), b5=(0.29, 0.30, 1_000_000))
    precount(api, "b0", 0.40)
    precount(api, "b5", 0.30)
    bot.run(NIGHT)
    assert [ex for ex, a, *_ in bot.placed if a == "buy"] == ["b5", "b0"]
    assert bot.buys("b5") == [(int(350 / 0.30), 0.30)]


def test_no_night_buys_after_night_buys_end(api, bot):
    basket(api, b3=(0.40, 0.41, 1_000_000))
    precount(api, "b3", 0.32)
    api.order("b3", "buy", 1000, 0.40, oid="night_buy")
    bot.run(LATE_NIGHT)
    assert bot.buys() == []
    assert ("night_buy", "buy window closed") in bot.cancelled


# ---------------------------------------------------------------- simulation sends nothing

def test_simulation_mode_never_writes(monkeypatch):
    def no_writes(method, path, **kw):
        raise AssertionError(f"simulation sent {method} {path}")
    monkeypatch.setattr(bb, "LIVE", False)
    monkeypatch.setattr(bb, "call", no_writes)
    monkeypatch.setattr(bb, "log", lambda msg: None)
    bb.place("tid", "ex", "buy", 100, 0.2)
    bb.cancel({"id": "o1", "action": "buy", "side": "yes", "quantity": 100, "priceLimit": 0.2,
               "exchangeId": "ex"}, "test")
