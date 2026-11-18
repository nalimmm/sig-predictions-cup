"""The execution logic of the basket bot, as one pure function.

`plan_round` takes a snapshot of the account and returns the orders to cancel and to place. It
does no I/O and keeps no state: every round starts from what the venue reports, so the bot can be
stopped and restarted at any time without losing or doubling a position.

  1. SELLS. Every basket position carries a ladder: half at 2x the average cost, half at 3x (5x
     when the average cost is below 0.20), capped at 0.95. Once the guard is armed, a position
     whose best bid falls to max(50% of the average cost, 0.10) is sold whole to the best bidder
     instead (RESCUE). With `cleanup_outside`, positions outside the basket are sold to the best
     bidder and orders outside the basket are cancelled.
  2. BUYS, in tranches and slippage free: for each basket instrument below its budget, take ONLY
     the quantity shown at the best offer, with a limit at that price, and never above the cap
     (reference x 1.10 while building; during the rolling window min(0.80, 1.5 x the pre-window
     price), and only if the bid holds at 80% of that price).
     If the guard is armed while the basket is still being built (never the case in the
     tournament, a choice on other venues), an instrument being rescued, or whose bid sits at
     max(50% of its reference, 0.10) or lower, is not bought.
  3. CASH. Budgets are a share of equity; what can actually be spent is the venue's cash minus
     the buy orders already resting minus the reserve. Unrealized gains are never spendable.
"""
import math
from datetime import datetime
from typing import Callable, Dict, Optional

from .config import Config
from .types import Book, Cancel, OpenOrder, Place, Plan, Snapshot, Spec

EPS = 1e-9


def tick_down(p: float, tick: float) -> float:
    return round(max(tick, min(1 - tick, math.floor(round(p / tick, 6)) * tick)), 6)


def tick_up(p: float, tick: float) -> float:
    return round(max(tick, min(1 - tick, math.ceil(round(p / tick, 6)) * tick)), 6)


def floor_step(q: float, step: float) -> float:
    return round(math.floor(q / step + 1e-9) * step, 6)


def _same(a: float, b: float) -> bool:
    return abs(a - b) < EPS


def _total(orders) -> float:
    return sum(o.remaining for o in orders)


def ladder_targets(quantity: float, avg_cost: float, cfg: Config, spec: Spec):
    """[(price, quantity)] of the sell ladder for a position. Legs the venue would reject as too
    small are merged into the low leg; a position below the minimum order size gets no ladder."""
    low = min(cfg.sell_cap, cfg.leg_low_mult * avg_cost)
    if cfg.low_leg_floor is not None:
        low = min(cfg.sell_cap, max(low, cfg.low_leg_floor))
    mult = cfg.leg_high_mult_cheap if avg_cost < cfg.high_leg_below else cfg.leg_high_mult
    leg_low, leg_high = tick_up(low, spec.tick), tick_up(min(cfg.sell_cap, mult * avg_cost), spec.tick)
    q = floor_step(quantity, spec.size_step)
    smallest = max(spec.min_size, spec.size_step)
    if q < smallest - EPS:
        return []
    q_low = floor_step(q / 2, spec.size_step)
    q_high = round(q - q_low, 6)
    if leg_high > leg_low and q_low >= smallest - EPS and q_high >= smallest - EPS:
        return [(leg_low, q_low), (leg_high, q_high)]
    return [(leg_low, q)]


def plan_round(snap: Snapshot, basket: Dict[str, float], cfg: Config, now: datetime,
               book: Callable[[str], Optional[Book]],
               reference: Callable[[str], Optional[float]] = lambda inst: None) -> Plan:
    """basket: instrument -> reference price. book(inst): live order book, asked only for the
    instruments about to be bought. reference(inst): price frozen just before the rolling window."""
    plan = Plan()
    act, note = plan.actions.append, plan.notes.append
    spec = lambda inst: snap.specs.get(inst, Spec())
    build = now < cfg.build_until
    guard = now >= cfg.guard_from
    roll = cfg.rolling if cfg.rolling and now >= cfg.rolling.start else None
    roll_buys = roll is not None and now < roll.buys_end
    equity = snap.equity if cfg.max_capital is None else min(snap.equity, cfg.max_capital)

    def dump(inst, sells, quantity, price, why):
        """Sell everything to the best bidder, reusing orders already resting at that price."""
        keep = [o for o in sells if _same(o.price, price)]
        for o in sells:
            if o not in keep:
                act(Cancel(o, "selling to the best bidder"))
        s = spec(inst)
        left = floor_step(quantity - _total(keep), s.size_step)
        if left >= max(s.min_size, s.size_step) - EPS:
            act(Place(inst, "sell", left, price, why))
        elif left > 0:
            note(f"{inst}: {left:g} left to sell is below the venue minimum")

    def ladder(inst, sells, quantity, avg_cost):
        wanted = ladder_targets(quantity, avg_cost, cfg, spec(inst))
        if not wanted:
            note(f"{inst}: position {quantity:g} is below the minimum order size, no ladder")
        keep = []
        for o in sells:
            target = next((w for w in wanted if _same(o.price, w[0])), None)
            already = sum(k.remaining for k in keep if _same(k.price, o.price))
            if target and already + o.remaining <= target[1] + EPS:
                keep.append(o)
            else:
                act(Cancel(o, "sell target recomputed"))
        for price, qty in wanted:
            missing = round(qty - sum(k.remaining for k in keep if _same(k.price, price)), 6)
            if missing >= max(spec(inst).min_size, spec(inst).size_step) - EPS:
                act(Place(inst, "sell", missing, price, "ladder"))

    rescued = set()
    # 1. Positions: basket -> ladder (or rescue once the guard is armed); outside -> dump (opt-in)
    managed = list(basket) + [i for i in sorted(snap.positions) if i not in basket and cfg.cleanup_outside]
    for inst in managed:
        p = snap.positions.get(inst)
        q = p.quantity if p else 0.0
        bid, _ask = snap.quotes.get(inst, (None, None))
        sells = [o for o in snap.orders if o.instrument == inst and o.action == "sell"]
        if _total(sells) > q + EPS:
            for o in sells:
                act(Cancel(o, "sells exceed the position"))
            sells = []
        if q <= 0:
            continue
        if inst in basket:
            threshold = max(cfg.rescue_fraction * p.avg_cost, cfg.rescue_floor)
            if guard and bid is not None and bid <= threshold + EPS:
                note(f"RESCUE {inst}: bid {bid:.3f} at or below {threshold:.3f} (max of "
                     f"{cfg.rescue_fraction:.0%} of avg cost {p.avg_cost:.3f} and floor {cfg.rescue_floor})")
                dump(inst, sells, q, tick_down(bid, spec(inst).tick), "rescue")
                rescued.add(inst)
            else:
                ladder(inst, sells, q, p.avg_cost)
        elif bid:
            dump(inst, sells, q, tick_down(bid, spec(inst).tick), "cleanup")

    # 2. Buys
    def price_cap(inst):
        if build:
            return tick_down(basket[inst] * cfg.price_cap_mult, spec(inst).tick)
        if roll_buys:
            ref = reference(inst)
            if ref:
                return tick_down(min(roll.price_cap, roll.momentum_max * ref), spec(inst).tick)
        return None

    budget = (1 - cfg.cash_buffer) * equity / max(len(basket), 1)
    committed: Dict[str, float] = {}
    for o in snap.orders:
        if o.action != "buy" or (o.instrument not in basket and not cfg.cleanup_outside):
            continue
        cap = price_cap(o.instrument) if o.instrument in basket else None
        if cap is not None and o.price <= cap + EPS and o.instrument not in committed:
            committed[o.instrument] = o.remaining * o.price
        else:
            act(Cancel(o, "buy outside the basket" if o.instrument not in basket else
                       "buy window closed" if cap is None else "stale level or duplicate"))

    if build or roll_buys:
        spendable = snap.cash - sum(committed.values()) - cfg.min_cash_reserve
        if build:
            order_of_play = sorted(basket, key=lambda i: basket[i])
        else:  # cheapest first
            order_of_play = sorted(basket, key=lambda i: snap.quotes.get(i, (None, None))[1] or 1.0)
        for inst in order_of_play:
            if inst in committed or inst in snap.blocked or inst in rescued:
                continue
            bid = snap.quotes.get(inst, (None, None))[0]
            if build and guard and bid is not None and \
                    bid <= max(cfg.rescue_fraction * basket[inst], cfg.rescue_floor) + EPS:
                continue  # guard armed while building: never buy what the rescue would sell
            cap = price_cap(inst)
            if cap is None:
                continue
            if build:
                limit = budget
            else:
                bid, ref = snap.quotes.get(inst, (None, None))[0], reference(inst)
                if ref is None or bid is None or bid < roll.drop_guard * ref - EPS:
                    continue  # dropping: never roll into it, the rescue handles the exit
                limit = roll.max_share * equity
            p = snap.positions.get(inst)
            room = limit - (abs(p.cost_basis) if p else 0.0)
            if room <= 0 or room < cfg.min_tranche_notional:
                continue
            b = book(inst)
            if not b or not b.asks:
                continue
            best = min(b.asks, key=lambda l: l.price)
            if best.price > cap + EPS:
                continue  # above the cap: wait for the market to come back, never chase
            s = spec(inst)
            qty = floor_step(min(best.size, room / best.price, max(spendable, 0) / best.price), s.size_step)
            if qty < max(s.min_size, s.size_step) - EPS or qty * best.price < s.min_notional - EPS:
                continue
            act(Place(inst, "buy", qty, tick_up(best.price, s.tick), "tranche"))
            spendable -= qty * best.price

    held = {i: snap.positions[i] for i in basket if i in snap.positions}
    plan.stats = {
        "equity": snap.equity, "sizing_equity": equity, "cash": snap.cash, "budget": budget,
        "phase": "build" if build else "rolling" if roll_buys else "hold",
        "guard": guard,
        "filled": sum(min(abs(p.cost_basis) / budget, 1) for p in held.values()) / max(len(basket), 1) if budget > 0 else 0.0,
        "outside": sum(1 for i in snap.positions if i not in basket),
    }
    return plan
