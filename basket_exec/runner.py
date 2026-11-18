"""One stateless round: read the venue, plan, execute. The only place where orders are sent."""
import json
import os
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
                 log: Callable[[str], None] = _print, clock: Callable[[], datetime] = None,
                 refs_file: Optional[str] = None):
        """refs_file: where the frozen pre-window references are saved, so a restart cannot move them.
        Use one file per campaign: a file left over from another one would be trusted."""
        self.venue, self.cfg, self.live, self.log = venue, cfg, live, log
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.refs_file = refs_file
        self._refs: Dict[str, float] = {}   # instrument -> price just before the rolling window
        if refs_file and os.path.exists(refs_file):
            try:
                with open(refs_file, encoding="utf-8") as f:
                    self._refs = {k: float(v) for k, v in json.load(f).items() if v}
                self.log(f"{len(self._refs)} frozen pre-window references loaded from {refs_file}")
            except (OSError, ValueError) as e:
                self.log(f"WARNING: {refs_file} unreadable ({e}), pre-window references start empty")

    def _save_refs(self) -> None:
        if not self.refs_file:
            return
        tmp = self.refs_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._refs, f, indent=1, sort_keys=True)
        os.replace(tmp, self.refs_file)  # atomic: a crash never leaves a half written file

    def _snapshot_refs(self, basket, snap: Snapshot, now: datetime) -> None:
        """Between the guard and the rolling window, record the mid quote of every basket instrument
        each round. At the start of the window the snapshot stops: the reference is frozen, and no
        price seen after that can move it."""
        r = self.cfg.rolling
        if r is None or not self.cfg.guard_from <= now < r.start:
            return
        changed = False
        for inst in basket:
            bid, ask = snap.quotes.get(inst, (None, None))
            mid = (bid + ask) / 2 if bid and ask else (bid or None)
            if mid and abs(self._refs.get(inst, 0.0) - round(mid, 4)) > 1e-9:
                self._refs[inst] = round(mid, 4)
                changed = True
        if changed:
            self._save_refs()

    def _reference(self, inst: str) -> Optional[float]:
        """The frozen pre-window price. Fallback when no snapshot was taken (bot down just before the
        window): the venue's history up to the start of the window, frozen the first time it
        succeeds. A failure is not cached."""
        if self._refs.get(inst):
            return self._refs[inst]
        try:
            ref = self.venue.reference(inst, self.cfg.rolling.start)
        except Exception as e:
            self.log(f"Pre-window reference for {self.venue.label(inst)} unreadable: {e}")
            return None
        if not ref:
            return None
        self._refs[inst] = ref
        self.log(f"WARNING: no pre-window snapshot for {self.venue.label(inst)}, frozen from history at {ref:.3f}")
        self._save_refs()
        return ref

    def describe(self, a) -> str:
        if isinstance(a, Cancel):
            o = a.order
            return (f"cancel {o.action} {o.remaining:,g} @ {o.price:g}  "
                    f"{self.venue.label(o.instrument)} ({a.reason})")
        return f"{a.action.upper():4} {a.quantity:>12,g} @ {a.price:.3f}  {self.venue.label(a.instrument)} ({a.reason})"

    def round(self) -> Plan:
        basket = self.venue.basket()
        snap = self.venue.snapshot(basket, self.cfg.cleanup_outside)
        now = self.clock()
        self._snapshot_refs(basket, snap, now)
        plan = plan_round(snap, basket, self.cfg, now, self.venue.book, self._reference)
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
