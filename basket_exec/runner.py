"""One stateless round: read the venue, plan, execute. The only place where orders are sent."""
import time
from datetime import datetime, timezone
from typing import Callable, Dict, Optional, Protocol

from .config import Config
from .engine import plan_round
from .types import Book, Cancel, OpenOrder, Place, Plan, Snapshot


class Venue(Protocol):
    """What an exchange adapter has to provide. Instruments are opaque strings to the engine."""

    def basket(self) -> Dict[str, float]:
        """instrument -> reference price, in the order the basket was declared."""

    def snapshot(self, basket: Dict[str, float], with_outside: bool) -> Snapshot:
        """Account state, with quotes and specs for the basket (and outside positions if asked)."""

    def book(self, instrument: str) -> Optional[Book]: ...

    def reference(self, instrument: str, before: datetime) -> Optional[float]:
        """Last price printed at or before `before`. Must not move once `before` has passed."""

    def place(self, order: Place) -> str:
        """Send a limit order (good till cancelled). Returns a short status for the log; raises on failure."""

    def cancel(self, order: OpenOrder) -> None: ...

    def label(self, instrument: str) -> str: ...


def _print(msg: str) -> None:
    print(f"{datetime.now():%d/%m %H:%M:%S}  {msg}", flush=True)


class Runner:
    def __init__(self, venue: Venue, cfg: Config, live: bool = False,
                 log: Callable[[str], None] = _print, clock: Callable[[], datetime] = None):
        self.venue, self.cfg, self.live, self.log = venue, cfg, live, log
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._refs: Dict[str, float] = {}   # pre-window references: frozen once read

    def _reference(self, inst: str) -> Optional[float]:
        if inst not in self._refs:
            try:
                ref = self.venue.reference(inst, self.cfg.rolling.reference_at)
            except Exception as e:
                self.log(f"Reference for {self.venue.label(inst)} unreadable: {e}")
                return None
            if ref is None:
                return None
            self._refs[inst] = ref
        return self._refs[inst]

    def describe(self, a) -> str:
        if isinstance(a, Cancel):
            o = a.order
            return (f"cancel {o.action} {o.remaining:,g} @ {o.price:g}  "
                    f"{self.venue.label(o.instrument)} ({a.reason})")
        return f"{a.action.upper():4} {a.quantity:>12,g} @ {a.price:.3f}  {self.venue.label(a.instrument)} ({a.reason})"

    def round(self) -> Plan:
        basket = self.venue.basket()
        snap = self.venue.snapshot(basket, self.cfg.cleanup_outside)
        plan = plan_round(snap, basket, self.cfg, self.clock(), self.venue.book, self._reference)
        for n in snap.notes + plan.notes:
            self.log(n if n.startswith("RESCUE") else f"note: {n}")
        sent = 0
        for a in plan.actions:
            desc = self.describe(a)
            if not self.live:
                self.log(f"[simulation] {desc}")
                continue
            try:  # one failed order never stops the round: the next round re-reads true state
                status = self.venue.cancel(a.order) if isinstance(a, Cancel) else self.venue.place(a)
                sent += 1
                self.log(f"OK   {desc}" + (f"  ({status})" if status else ""))
            except Exception as e:
                self.log(f"FAIL {desc}  {e}")
        s = plan.stats
        held = "  ".join(f"{self.venue.label(i)[:28]}:{p.quantity:,g}@{p.avg_cost:.3f}"
                         for i, p in snap.positions.items() if i in basket) or "none"
        self.log(f"Round OK ({s['phase']}{', guard armed' if s['guard'] else ''}): account {s['equity']:,.2f}, "
                 f"cash {s['cash']:,.2f}, basket {s['filled']:.0%} filled ({s['budget']:,.2f}/instrument), "
                 f"positions {held}, {s['outside']} outside the basket, {sent} orders sent")
        return plan

    def stop(self) -> int:
        """Cancel the open orders this strategy owns (every order of the account with cleanup_outside)."""
        basket = self.venue.basket()
        snap = self.venue.snapshot(basket, self.cfg.cleanup_outside)
        mine = [o for o in snap.orders if o.instrument in basket or self.cfg.cleanup_outside]
        for o in mine:
            desc = self.describe(Cancel(o, "stop"))
            if not self.live:
                self.log(f"[simulation] {desc}")
                continue
            try:
                self.venue.cancel(o)
                self.log(f"OK   {desc}")
            except Exception as e:
                self.log(f"FAIL {desc}  {e}")
        return len(mine)

    def loop(self, every: float = 60.0) -> None:
        self.log(f"Starting in {'LIVE' if self.live else 'SIMULATION'} mode")
        while True:
            try:
                self.round()
            except Exception as e:  # log and carry on, the next round re-reads true state
                self.log(f"Error during the round: {e}")
            if not self.live:
                self.log("Simulation finished: no order was sent. Run again with --live to trade.")
                return
            time.sleep(every)
