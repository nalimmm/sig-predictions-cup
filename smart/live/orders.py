"""From the quotes the market maker wants on YES to the orders Polymarket accepts.

The simulator thinks in YES only: a bid and an ask on YES, and an inventory that may be short.
Polymarket has no short sale: selling YES needs YES in the wallet. So

  bid on YES at p, s shares   sell NO at 1 - p for what NO is held, buy YES at p for the rest
  ask on YES at p, s shares   sell YES at p for what YES is held, buy NO at 1 - p for the rest

Buying NO at 1 - p is economically the same as selling YES at p (a YES and a NO always pay
exactly 1 together), and costs 1 - p of capital per share, which is what the simulator's risk
cap already counts.

At most one resting order per (token, side). An order is kept while its price is right and at
least half of the wanted size is still resting; otherwise it is cancelled and replaced. Every
order is post-only: it rests in the book or it is rejected, it never takes liquidity. The only
taker orders are the exits before the end date (`take`), sent fill-and-kill.
"""
import math
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

SIZE_STEP = 0.01
MIN_BUY_USDC = 1.0          # Polymarket refuses a buy worth less than 1 USDC


@dataclass
class Want:
    token: str
    side: str               # BUY / SELL on that token
    price: float
    size: float


@dataclass
class Order:
    id: str
    token: str
    side: str
    price: float
    size: float
    slug: str = ""
    yes: bool = True        # placed on the market's YES token (False: on NO)
    matched: float = 0.0
    placed_ms: int = 0
    taker: bool = False

    @property
    def left(self) -> float:
        return max(self.size - self.matched, 0.0)

    def notional(self) -> float:
        return self.left * self.price if self.side == "BUY" else 0.0


def floor_size(x: float) -> float:
    return math.floor(x / SIZE_STEP + 1e-9) * SIZE_STEP


def on_tick(price: float, tick: float) -> bool:
    return abs(price / tick - round(price / tick)) < 1e-6 and tick - 1e-9 <= price <= 1 - tick + 1e-9


def intents(yes: str, no: str, inv: float, bid: Optional[float], bid_sz: float, ask: Optional[float], ask_sz: float,
            tick: float, min_size: float) -> List[Want]:
    """The orders that carry a YES bid/ask given the YES inventory `inv` (negative = NO held)."""
    out: List[Want] = []
    held_yes, held_no = max(inv, 0.0), max(-inv, 0.0)

    def add(token, side, price, size):
        size = floor_size(size)
        price = round(price, 6)
        if size < min_size or not on_tick(price, tick):
            return
        if side == "BUY" and size * price < MIN_BUY_USDC:
            return
        out.append(Want(token, side, price, round(size, 2)))

    if bid is not None and bid_sz > 0:
        a = min(bid_sz, held_no)
        if a > 0:
            add(no, "SELL", 1 - bid, a)
        if bid_sz - a > 0:
            add(yes, "BUY", bid, bid_sz - a)
    if ask is not None and ask_sz > 0:
        a = min(ask_sz, held_yes)
        if a > 0:
            add(yes, "SELL", ask, a)
        if ask_sz - a > 0:
            add(no, "BUY", 1 - ask, ask_sz - a)
    return out


def yes_side(order: Order) -> Tuple[str, float]:
    """What a fill of this order does to the YES inventory: (BUY or SELL of YES, YES price)."""
    if order.yes:
        return order.side, order.price
    return ("SELL" if order.side == "BUY" else "BUY"), round(1 - order.price, 6)


def plan(wanted: List[Want], resting: List[Order], keep_frac: float = 0.5) -> Tuple[List[Order], List[Want]]:
    """(orders to cancel, orders to place) so that what rests matches what is wanted."""
    by_key: Dict[tuple, Want] = {(w.token, w.side): w for w in wanted}
    cancel, place, kept = [], [], set()
    for o in resting:
        if o.taker:
            continue
        w = by_key.get((o.token, o.side))
        key = (o.token, o.side)
        if w is not None and key not in kept and abs(o.price - w.price) < 1e-9 \
                and o.left >= keep_frac * w.size - 1e-9 and o.left <= w.size + 1e-6:
            kept.add(key)
        else:
            cancel.append(o)
    for key, w in by_key.items():
        if key not in kept:
            place.append(w)
    return cancel, place
