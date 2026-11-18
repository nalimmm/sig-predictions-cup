"""The decision rules, on hand-built snapshots. No network, no venue."""
from datetime import datetime, timezone

import pytest

from basket_exec import (Book, Cancel, Config, Level, OpenOrder, Place, Position, Rolling, Snapshot, Spec,
                         ladder_targets, plan_round, tick_down, tick_up)

T = lambda s: datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
BUILD, HOLD, ROLL = T("2026-06-01"), T("2026-07-01T12:00"), T("2026-07-02T01:00")
CENT = Spec(tick=0.01, size_step=0.01, min_size=5, min_notional=1)


def cfg(**kw):
    base = dict(build_until="2026-07-01T00:00:00Z",
                rolling=Rolling(start="2026-07-02T00:00:00Z", buys_end="2026-07-02T16:00:00Z"))
    return Config(**{**base, **kw})


def snap(equity=1000.0, cash=1000.0, positions=(), orders=(), quotes=None, spec=CENT, blocked=()):
    quotes = quotes or {}
    return Snapshot(equity=equity, cash=cash, positions={p.instrument: p for p in positions},
                    orders=list(orders), quotes=quotes, blocked=set(blocked),
                    specs={i: spec for i in list(quotes) + ["A", "B", "X"]})


def books(**asks):
    return lambda inst: Book(bids=[], asks=[Level(p, s) for p, s in asks.get(inst, [])])


def places(plan, action=None):
    return [a for a in plan.actions if isinstance(a, Place) and action in (None, a.action)]


def cancels(plan):
    return [a.order.id for a in plan.actions if isinstance(a, Cancel)]


# ------------------------------------------------------------------ ticks
def test_ticks_round_the_safe_way_and_stay_inside_the_book():
    assert tick_down(0.4399, 0.01) == 0.43 and tick_up(0.4301, 0.01) == 0.44
    assert tick_down(0.44, 0.01) == 0.44 and tick_up(0.44, 0.01) == 0.44      # no float drift on exact ticks
    assert tick_up(0.999, 0.01) == 0.99 and tick_down(0.001, 0.01) == 0.01
    assert tick_up(0.4381, 0.005) == 0.44 and tick_down(0.4381, 0.001) == 0.438


# ------------------------------------------------------------------- buys
def test_tranche_takes_only_the_best_offer():
    plan = plan_round(snap(), {"A": 0.30, "B": 0.40}, cfg(), BUILD,
                      books(A=[(0.31, 40), (0.30, 25)], B=[(0.41, 10_000)]))
    a, b = places(plan, "buy")
    assert (a.instrument, a.quantity, a.price) == ("A", 25, 0.30)       # the size shown at the best offer, no more
    assert b.instrument == "B" and b.price == 0.41
    # budget = 98% of 1000 / 2 = 490, but only 1000 - 7.50 of cash is left: B is capped by its budget
    assert b.quantity == pytest.approx(1195.12)


def test_never_buys_above_reference_plus_ten_percent():
    plan = plan_round(snap(), {"A": 0.30}, cfg(), BUILD, books(A=[(0.34, 100)]))
    assert places(plan) == []                                            # cap is 0.33: wait, do not chase
    plan = plan_round(snap(), {"A": 0.30}, cfg(), BUILD, books(A=[(0.33, 100)]))
    assert places(plan, "buy")[0].price == 0.33


def test_budget_counts_what_is_already_invested():
    held = Position("A", 1000, 0.30, 300.0)
    plan = plan_round(snap(equity=1000, cash=700, positions=[held]), {"A": 0.30, "B": 0.40}, cfg(), BUILD,
                      books(A=[(0.30, 10_000)], B=[(0.40, 10_000)]))
    buys = {b.instrument: b for b in places(plan, "buy")}
    assert buys["A"].quantity == pytest.approx((490 - 300) / 0.30, abs=0.01)
    assert buys["B"].quantity == pytest.approx(490 / 0.40, abs=0.01)


def test_cash_is_the_binding_constraint_not_equity():
    """The bug that hid in the tournament bot: unrealized gains are not spendable."""
    held = Position("A", 1000, 0.10, 100.0)                              # now worth 900
    s = snap(equity=1000, cash=100, positions=[held], quotes={"A": (0.90, 0.91), "B": (0.39, 0.40)})
    plan = plan_round(s, {"A": 0.10, "B": 0.40}, cfg(min_cash_reserve=10), BUILD, books(B=[(0.40, 10_000)]))
    (b,) = places(plan, "buy")
    assert b.instrument == "B" and b.quantity * b.price <= 90 + 1e-6     # 100 cash - 10 reserve, not 490


def test_resting_buys_are_committed_cash_and_block_a_second_order():
    resting = OpenOrder("o1", "A", "buy", 0.30, 1000)                    # 300 already committed
    plan = plan_round(snap(cash=320, orders=[resting]), {"A": 0.30, "B": 0.40}, cfg(), BUILD,
                      books(A=[(0.30, 500)], B=[(0.40, 10_000)]))
    assert cancels(plan) == []
    (b,) = places(plan, "buy")
    assert b.instrument == "B" and b.quantity == pytest.approx(20 / 0.40)


def test_stale_duplicate_and_out_of_window_buys_are_cancelled():
    orders = [OpenOrder("ok", "A", "buy", 0.33, 10), OpenOrder("dup", "A", "buy", 0.32, 10),
              OpenOrder("high", "B", "buy", 0.50, 10)]
    plan = plan_round(snap(orders=orders), {"A": 0.30, "B": 0.40}, cfg(), BUILD, books())
    assert cancels(plan) == ["dup", "high"]
    plan = plan_round(snap(orders=orders), {"A": 0.30, "B": 0.40}, cfg(), HOLD, books())
    assert cancels(plan) == ["ok", "dup", "high"] and places(plan) == []  # window closed: no buys at all


def test_venue_minimums_are_respected():
    plan = plan_round(snap(), {"A": 0.30}, cfg(), BUILD, books(A=[(0.30, 3)]))
    assert places(plan) == []                                            # 3 shares < min size 5
    plan = plan_round(snap(), {"A": 0.10}, cfg(), BUILD, books(A=[(0.10, 8)]))
    assert places(plan) == []                                            # 0.80 < 1 USDC minimum
    plan = plan_round(snap(blocked=["A"]), {"A": 0.30}, cfg(), BUILD, books(A=[(0.30, 100)]))
    assert places(plan) == []


def test_max_capital_caps_the_sizing_base():
    plan = plan_round(snap(equity=10_000, cash=10_000), {"A": 0.30}, cfg(max_capital=500), BUILD,
                      books(A=[(0.30, 100_000)]))
    assert places(plan)[0].quantity * 0.30 == pytest.approx(490, abs=0.01)


# ------------------------------------------------------------------ sells
def test_ladder_half_at_2x_half_at_3x():
    plan = plan_round(snap(positions=[Position("A", 101, 0.30, 30.3)]), {"A": 0.30}, cfg(), HOLD, books())
    assert [(s.price, s.quantity) for s in places(plan, "sell")] == [(0.60, 50.5), (0.90, 50.5)]


def test_ladder_5x_below_twenty_cents_and_cap_at_95():
    c = cfg()
    assert ladder_targets(100, 0.15, c, CENT) == [(0.30, 50), (0.75, 50)]
    assert ladder_targets(100, 0.40, c, CENT) == [(0.80, 50), (0.95, 50)]
    assert ladder_targets(100, 0.50, c, CENT) == [(0.95, 100)]           # both legs capped: one order
    assert ladder_targets(100, 0.223, c, CENT) == [(0.45, 50), (0.67, 50)]  # targets round UP to the tick


def test_sell_floor_applies_to_both_legs_once_the_guard_is_armed():
    c = cfg(guard_sell_floor=0.70)
    assert ladder_targets(100, 0.30, c, CENT, floor=0.70) == [(0.70, 50), (0.90, 50)]
    assert ladder_targets(100, 0.20, c, CENT, floor=0.70) == [(0.70, 100)]      # 0.40 and 0.60 both raised: one order
    assert ladder_targets(100, 0.15, c, CENT, floor=0.70) == [(0.70, 50), (0.75, 50)]
    assert ladder_targets(100, 0.40, c, CENT, floor=0.70) == [(0.80, 50), (0.95, 50)]   # already above: unchanged
    pos = Position("A", 100, 0.30, 30)
    day = plan_round(snap(positions=[pos]), {"A": 0.30}, c, BUILD, books())
    assert [s.price for s in places(day, "sell")] == [0.60, 0.90]               # guard not armed: no floor
    night = plan_round(snap(positions=[pos]), {"A": 0.30}, c, HOLD, books())
    assert [s.price for s in places(night, "sell")] == [0.70, 0.90]
    # the day ladder left in the book is replaced when the floor comes into force
    old = [OpenOrder("lo", "A", "sell", 0.60, 50), OpenOrder("hi", "A", "sell", 0.90, 50)]
    swap = plan_round(snap(positions=[pos], orders=old), {"A": 0.30}, c, HOLD, books())
    assert cancels(swap) == ["lo"] and [(s.price, s.quantity) for s in places(swap)] == [(0.70, 50)]


def test_ladder_with_small_positions():
    assert ladder_targets(8, 0.30, cfg(), CENT) == [(0.60, 8)]            # two legs of 4 would be rejected
    assert ladder_targets(3, 0.30, cfg(), CENT) == []                     # dust: nothing can be posted
    assert ladder_targets(10.526, 0.30, cfg(), CENT) == [(0.60, 5.26), (0.90, 5.26)]


def test_ladder_is_idempotent_and_recomputed_when_the_position_changes():
    pos = Position("A", 100, 0.30, 30)
    good = [OpenOrder("lo", "A", "sell", 0.60, 50), OpenOrder("hi", "A", "sell", 0.90, 50)]
    assert plan_round(snap(positions=[pos], orders=good), {"A": 0.30}, cfg(), HOLD, books()).actions == []
    # bought more at a higher price: targets move, old orders go, new ones are posted
    pos2 = Position("A", 200, 0.32, 64)
    plan = plan_round(snap(positions=[pos2], orders=good), {"A": 0.30}, cfg(), HOLD, books())
    assert cancels(plan) == ["lo", "hi"]
    assert [(s.price, s.quantity) for s in places(plan, "sell")] == [(0.64, 100), (0.95, 100)]
    # a leg partly filled: position 75, sells 25 + 50 -> low leg target is now 37.5, both stay within targets
    pos3 = Position("A", 75, 0.30, 22.5)
    part = [OpenOrder("lo", "A", "sell", 0.60, 25), OpenOrder("hi", "A", "sell", 0.90, 50)]
    plan = plan_round(snap(positions=[pos3], orders=part), {"A": 0.30}, cfg(), HOLD, books())
    assert cancels(plan) == ["hi"]
    assert [(s.price, s.quantity) for s in places(plan, "sell")] == [(0.60, 12.5), (0.90, 37.5)]


def test_sells_never_exceed_the_position():
    pos = Position("A", 60, 0.30, 18)
    over = [OpenOrder("lo", "A", "sell", 0.60, 50), OpenOrder("hi", "A", "sell", 0.90, 50)]
    plan = plan_round(snap(positions=[pos], orders=over), {"A": 0.30}, cfg(), HOLD, books())
    assert cancels(plan) == ["lo", "hi"]
    assert sum(s.quantity for s in places(plan, "sell")) == 60
    orphan = [OpenOrder("x", "A", "sell", 0.60, 50)]                      # position gone, order left behind
    assert cancels(plan_round(snap(orders=orphan), {"A": 0.30}, cfg(), HOLD, books())) == ["x"]


# ----------------------------------------------------------------- rescue
@pytest.mark.parametrize("avg,bid,rescued", [
    (0.30, 0.16, False),      # threshold 0.15 (half the cost)
    (0.30, 0.15, True),
    (0.30, 0.12, True),
    (0.15, 0.11, False),      # half the cost is 0.075: the absolute floor 0.10 takes over
    (0.15, 0.10, True),
    (0.60, 0.31, False),
    (0.60, 0.30, True),
])
def test_rescue_threshold_is_the_greater_of_half_cost_and_floor(avg, bid, rescued):
    pos = Position("A", 100, avg, 100 * avg)
    plan = plan_round(snap(positions=[pos], quotes={"A": (bid, bid + 0.02)}), {"A": 0.30}, cfg(), HOLD, books())
    sells = places(plan, "sell")
    if rescued:
        assert [(s.price, s.quantity, s.reason) for s in sells] == [(bid, 100, "rescue")]
    else:
        assert sells and all(s.reason == "ladder" for s in sells)


def test_rescue_replaces_the_ladder_and_is_idempotent():
    pos = Position("A", 100, 0.30, 30)
    ladder = [OpenOrder("lo", "A", "sell", 0.60, 50), OpenOrder("hi", "A", "sell", 0.90, 50)]
    s = snap(positions=[pos], orders=ladder, quotes={"A": (0.12, 0.14)})
    plan = plan_round(s, {"A": 0.30}, cfg(), HOLD, books())
    assert cancels(plan) == ["lo", "hi"] and [(x.price, x.quantity) for x in places(plan)] == [(0.12, 100)]
    resting = [OpenOrder("r", "A", "sell", 0.12, 100)]                   # next round, not filled yet
    s = snap(positions=[pos], orders=resting, quotes={"A": (0.12, 0.14)})
    assert plan_round(s, {"A": 0.30}, cfg(), HOLD, books()).actions == []
    s = snap(positions=[pos], orders=resting, quotes={"A": (0.11, 0.14)})  # bid stepped down: follow it
    plan = plan_round(s, {"A": 0.30}, cfg(), HOLD, books())
    assert cancels(plan) == ["r"] and places(plan)[0].price == 0.11


def test_rescue_is_not_armed_before_the_guard_and_can_be_armed_early():
    pos = Position("A", 100, 0.30, 30)
    s = snap(positions=[pos], quotes={"A": (0.05, 0.07)})
    assert all(x.reason != "rescue" for x in places(plan_round(s, {"A": 0.30}, cfg(), BUILD, books())))
    early = cfg(guard_from="2026-01-01T00:00:00Z")
    assert [x.reason for x in places(plan_round(s, {"A": 0.30}, early, BUILD, books()), "sell")] == ["rescue"]


def test_guard_armed_during_the_build_never_buys_back_a_collapse():
    early = cfg(guard_from="2026-01-01T00:00:00Z")
    pos = Position("A", 100, 0.30, 30)
    s = snap(positions=[pos], quotes={"A": (0.14, 0.16)})
    plan = plan_round(s, {"A": 0.30}, early, BUILD, books(A=[(0.16, 500)]))
    assert [x.reason for x in places(plan)] == ["rescue"]                # sold, and not bought back at 0.16
    s = snap(quotes={"A": (0.14, 0.16)})                                 # next round, position gone
    assert places(plan_round(s, {"A": 0.30}, early, BUILD, books(A=[(0.16, 500)]))) == []
    s = snap(quotes={"A": (0.16, 0.17)})                                 # back above half the reference
    assert len(places(plan_round(s, {"A": 0.30}, early, BUILD, books(A=[(0.17, 500)])), "buy")) == 1
    s = snap(quotes={"A": (0.14, 0.16)})                                 # guard not armed: the tournament behaviour
    assert len(places(plan_round(s, {"A": 0.30}, cfg(), BUILD, books(A=[(0.16, 500)])), "buy")) == 1


def test_no_rescue_without_a_bid():
    pos = Position("A", 100, 0.30, 30)
    plan = plan_round(snap(positions=[pos], quotes={"A": (None, 0.07)}), {"A": 0.30}, cfg(), HOLD, books())
    assert [x.reason for x in places(plan)] == ["ladder", "ladder"]       # nobody to sell to: targets stay


# ---------------------------------------------------------------- rolling
def test_rolling_buys_what_holds_and_skips_what_drops():
    refs = {"A": 0.40, "B": 0.30, "X": 0.20}
    q = {"A": (0.44, 0.45), "B": (0.20, 0.22), "X": (0.50, 0.52)}
    plan = plan_round(snap(quotes=q), {"A": 0.3, "B": 0.3, "X": 0.3}, cfg(), ROLL,
                      books(A=[(0.45, 100)], B=[(0.22, 100)], X=[(0.52, 100)]), refs.get)
    # B: bid 0.20 < 80% of 0.30 -> losing race.  X: 0.52 > 1.5 x 0.20 -> ran away.  A: fine.
    assert [(b.instrument, b.price, b.quantity) for b in places(plan, "buy")] == [("A", 0.45, 100)]


def test_rolling_caps_price_and_share_and_needs_a_reference():
    q = {"A": (0.80, 0.81), "B": (0.50, 0.51)}
    plan = plan_round(snap(quotes=q), {"A": 0.3, "B": 0.3}, cfg(), ROLL,
                      books(A=[(0.81, 100)], B=[(0.51, 100_000)]), {"A": 0.70, "B": 0.45}.get)
    (b,) = places(plan, "buy")                                           # A is above 0.80
    assert b.instrument == "B" and b.quantity == pytest.approx(350 / 0.51, abs=0.01)   # 35% of the account
    plan = plan_round(snap(quotes=q), {"A": 0.3, "B": 0.3}, cfg(), ROLL, books(B=[(0.51, 100)]))
    assert places(plan) == []                                            # no reference, no buy


def test_no_rolling_window_means_no_buys_after_the_build():
    plan = plan_round(snap(), {"A": 0.30}, cfg(rolling=None), ROLL, books(A=[(0.30, 100)]), {"A": 0.3}.get)
    assert places(plan) == []


# ---------------------------------------------------------------- cleanup
def test_positions_and_orders_outside_the_basket_are_left_alone_by_default():
    other = Position("Z", 500, 0.50, 250)
    orders = [OpenOrder("zb", "Z", "buy", 0.40, 10), OpenOrder("zs", "Z", "sell", 0.99, 500)]
    s = snap(positions=[other], orders=orders, quotes={"Z": (0.45, 0.47)})
    assert plan_round(s, {"A": 0.30}, cfg(), HOLD, books()).actions == []
    plan = plan_round(s, {"A": 0.30}, cfg(cleanup_outside=True), HOLD, books())
    assert cancels(plan) == ["zs", "zb"]
    assert [(x.instrument, x.price, x.quantity, x.reason) for x in places(plan)] == [("Z", 0.45, 500, "cleanup")]


def test_config_rejects_unknown_parameters():
    with pytest.raises(ValueError):
        Config.from_dict({"build_until": "2026-07-01T00:00:00Z", "rescue_flor": 0.2})


def test_example_config_loads():
    import yaml
    from pathlib import Path
    raw = yaml.safe_load((Path(__file__).resolve().parents[1] / "examples" / "polymarket_basket.yaml").read_text())
    c = Config.from_dict(raw["strategy"])
    assert c.cleanup_outside is False and c.max_capital == 500 and c.guard_from < c.build_until
