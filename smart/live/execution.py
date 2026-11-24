"""The market maker driven by live books, and the order layer between it and a venue.

LiveMM is the paper market maker (smart.mm.MMSim) with three differences:
  - market metadata comes from Gamma by slug (see markets.py), not from the recorder's list;
  - in shadow mode it fills its quotes exactly as the paper market maker does, and the order
    layer mirrors the quotes into the orders it would send, on a DryVenue;
  - in live mode it never imagines a fill: its quotes are the orders the venue acknowledged,
    and its inventory moves only when the venue reports a match.

Executor works in three steps so the real venue can be called from a worker thread while the
event loop keeps reading the books:
  decide()   (event loop)  what to cancel and place, after the safeguards
  execute()  (worker)      the venue calls, nothing else
  commit()   (event loop)  record what the venue answered
"""
import json
import time
from collections import Counter
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from ..mm import MMConfig, MMSim, Quoter
from .guard import Guard
from .orders import Order, Want, intents, plan, yes_side
from .venue import OrderRefused


class LiveMM(MMSim):
    def __init__(self, slugs: List[str], meta: Dict[str, dict], cfg: Optional[MMConfig] = None, mode: str = "shadow"):
        super().__init__("/nonexistent", slugs, cfg)
        self.meta, self.mode = meta, mode
        self.executor: Optional["Executor"] = None
        self.last_book_ms = 0

    def load(self, day: str):
        self.markets.update(self.meta)
        super().load(day)

    def on_event(self, ev: dict):
        if ev.get("event_type") in ("book", "price_change") and ev.get("_r"):
            self.last_book_ms = ev["_r"]
        super().on_event(ev)

    # ------------------------------------------------------------ live overrides
    def apply_pending(self, q: Quoter):
        if self.mode != "live":
            return super().apply_pending(q)
        if q.pending:                               # the wanted quotes; what rests is the executor's business
            _, bid, bsz, ask, asz = q.pending
            q.pending = None
            q.target = (bid, bsz, ask, asz)
            q.quote_updates += 1

    def on_book(self, q: Quoter):
        super().on_book(q)
        if self.mode == "live":
            self.apply_pending(q)                   # no simulated latency: the venue's is real

    def on_trade(self, t):
        if self.mode != "live":
            return super().on_trade(t)
        side = (t.side or "").upper()
        if side not in ("BUY", "SELL") or t.size <= 0:
            return
        if t.token in self.q:
            q, eps = self.q[t.token], (1 if side == "BUY" else -1)
        elif t.token in self.mirror:
            q, eps = self.q[self.mirror[t.token]], (-1 if side == "BUY" else 1)
        else:
            return
        self.apply_pending(q)
        q.flow.append((self.now, eps * t.price * t.size))
        self.protect(q)

    def take_out(self, q: Quoter, b):
        if self.mode != "live":
            return super().take_out(q, b)
        if self.executor is not None:
            self.executor.want_exit(q, b)
        q.next_take = self.now + int(self.cfg.take_every_s * 1000)

    def fill(self, q: Quoter, side: str, price: float, shares: float, taker: bool = False):
        if self.mode != "live" and not taker and self.executor is not None:
            # shadow: a quote fills only if a dry order carried it (the safeguards may have refused it)
            shares = self.executor.on_sim_fill(q, side, price, shares)
            if shares <= 0:
                return
        super().fill(q, side, price, shares, taker)


class Journal:
    def __init__(self, out: Path, clock: Callable[[], float] = time.time):
        self.out, self.clock = Path(out), clock
        self.out.mkdir(parents=True, exist_ok=True)

    def write(self, kind: str, **d):
        t = self.clock()
        day = time.strftime("%Y-%m-%d", time.gmtime(t))
        with open(self.out / f"orders-{day}.jsonl", "a") as f:
            f.write(json.dumps({"t": int(t * 1000), "k": kind, **d}, separators=(",", ":")) + "\n")


class Executor:
    def __init__(self, sim: LiveMM, venue, guard: Guard, journal: Journal, clock: Callable[[], float] = time.time,
                 stale_feed_s: float = 30.0):
        self.sim, self.venue, self.guard, self.journal, self.clock = sim, venue, guard, journal, clock
        self.stale_feed_s = stale_feed_s
        self.orders: Dict[str, Order] = {}
        self.exits: List[Tuple[Quoter, Want]] = []
        self.feed_ok = False                        # set by the runner once books are current
        self.stats = Counter()
        self.refused = Counter()
        self.call_ms: List[float] = []
        self.quote_path: Dict[str, list] = {}       # slug -> [[t ms, bid, bid size, ask, ask size]] of what rests, YES terms
        sim.executor = self

    # ------------------------------------------------------------ helpers
    def tokens(self, q: Quoter) -> Tuple[str, str]:
        no = next((s for s, f in self.sim.mirror.items() if f == q.token), None)
        return q.token, no

    def meta(self, token: str) -> dict:
        return self.sim.meta.get(token) or {}

    def quotes(self, q: Quoter):
        """The YES quotes this market should show now."""
        if self.sim.mode == "live":
            bid, bsz, ask, asz = getattr(q, "target", None) or (None, 0.0, None, 0.0)
            now = self.sim.now
            if bid is not None and now < q.pull_bid_until:
                bid, bsz = None, 0.0
            if ask is not None and now < q.pull_ask_until:
                ask, asz = None, 0.0
            return bid, bsz, ask, asz
        return q.bid, q.bid_sz, q.ask, q.ask_sz

    def crosses(self, w: Want) -> bool:
        b = self.sim.books.get(w.token)
        if b is None:
            return True
        best = b.best_ask() if w.side == "BUY" else b.best_bid()
        if best is None:
            return False
        return w.price >= best - 1e-9 if w.side == "BUY" else w.price <= best + 1e-9

    def fresh(self) -> bool:
        return self.feed_ok and (self.clock() * 1000 - self.sim.last_book_ms) <= self.stale_feed_s * 1000

    def want_exit(self, q: Quoter, b):
        """Close what is left before the end date: a fill-and-kill at the best price shown."""
        yes, no = self.tokens(q)
        if q.inv > 0:
            px = b.best_bid()
            w = Want(yes, "SELL", px, round(q.inv, 2)) if px else None
        else:
            nb = self.sim.books.get(no)
            px = nb.best_bid() if nb else None
            w = Want(no, "SELL", px, round(-q.inv, 2)) if px else None
        if w and w.size > 0:
            self.exits.append((q, w))

    # ------------------------------------------------------------ the three steps
    def decide(self) -> dict:
        """{"cancel": [orders], "place": [(quoter, want, taker)], "cancel_all": bool}"""
        resting = [o for o in self.orders.values()]
        if self.guard.halted or not self.fresh():
            exits, self.exits = self.exits, []
            return {"cancel": [o for o in resting if not o.taker], "place": [], "cancel_all": False}
        cancel, place = [], []
        working = [o for o in resting]
        quoters = list(self.sim.q.values())
        for q in quoters:
            yes, no = self.tokens(q)
            if no is None:
                continue
            m = self.meta(yes)
            if not m.get("accepting", True):
                wants = []
            else:
                bid, bsz, ask, asz = self.quotes(q)
                wants = intents(yes, no, q.inv, bid, bsz, ask, asz, m.get("tick", 0.01), m.get("min_size", 5.0))
            mine = [o for o in resting if o.token in (yes, no)]
            c, p = plan(wants, mine)
            cancel += c
            working = [o for o in working if o not in c]
            for w in p:
                if self.crosses(w):
                    self.refused["would_cross"] += 1
                    continue
                why = self.guard.allow(w.side, w.price, w.size, quoters, working)
                if why:
                    self.refused[why.split(":")[0].split(" of ")[0]] += 1
                    continue
                o = Order("pending", w.token, w.side, w.price, w.size)
                working.append(o)
                place.append((q, w, False))
        exits, self.exits = self.exits, []
        for q, w in exits:
            place.append((q, w, True))
        return {"cancel": cancel, "place": place, "cancel_all": False}

    def execute(self, d: dict) -> dict:
        """Venue calls only. Cancels first, so a replacement never doubles the exposure."""
        out = {"cancelled": [], "placed": [], "refused": [], "errors": []}
        ids = [o.id for o in d["cancel"]]
        if ids:
            t0 = time.time()
            try:
                self.venue.cancel(ids)
                out["cancelled"] = ids
            except Exception as e:
                out["errors"].append(f"cancel: {type(e).__name__}: {e}"[:300])
            self.call_ms.append((time.time() - t0) * 1000)
        if out["errors"]:
            return out                              # nothing new while old orders may still rest
        for q, w, taker in d["place"]:
            t0 = time.time()
            try:
                oid = self.venue.place(w, post_only=not taker, taker=taker)
                out["placed"].append((q, w, taker, oid))
            except OrderRefused as e:
                out["refused"].append((q, w, str(e)))
            except Exception as e:
                out["errors"].append(f"place: {type(e).__name__}: {e}"[:300])
                break
            self.call_ms.append((time.time() - t0) * 1000)
        self.call_ms = self.call_ms[-200:]
        return out

    def commit(self, r: dict):
        now = int(self.clock() * 1000)
        for oid in r["cancelled"]:
            o = self.orders.pop(oid, None)
            if o:
                self.stats["cancelled"] += 1
                self.journal.write("cancel", id=oid, slug=o.slug, token=o.token[-6:], side=o.side, price=o.price, left=round(o.left, 2))
        for q, w, taker, oid in r["placed"]:
            self.orders[oid] = Order(oid, w.token, w.side, w.price, w.size, slug=q.slug, yes=(w.token == q.token),
                                     placed_ms=now, taker=taker)
            self.stats["taker" if taker else "placed"] += 1
            self.journal.write("place", id=oid, slug=q.slug, token=w.token[-6:], side=w.side, price=w.price, size=w.size, taker=taker)
        for q, w, why in r["refused"]:
            self.refused["venue"] += 1
            self.journal.write("refused", slug=q.slug, side=w.side, price=w.price, size=w.size, why=why)
        for e in r["errors"]:
            self.stats["errors"] += 1
            self.journal.write("error", why=e)
        self.refresh_quotes()
        self.track()

    def refresh_quotes(self):
        """In live mode the simulator's quotes are what rests: YES-equivalent best of each side."""
        if self.sim.mode != "live":
            return
        for q in self.sim.q.values():
            bids = [(yes_side(o)[1], o.left) for o in self.orders.values() if not o.taker and o.slug == q.slug and yes_side(o)[0] == "BUY"]
            asks = [(yes_side(o)[1], o.left) for o in self.orders.values() if not o.taker and o.slug == q.slug and yes_side(o)[0] == "SELL"]
            q.bid, q.bid_sz = (bids[0] if bids else (None, 0.0))
            q.ask, q.ask_sz = (asks[0] if asks else (None, 0.0))

    def track(self, cap: int = 4000):
        """Record each change of the resting quotes (YES terms, best of each side) for the price charts."""
        t = int(self.clock() * 1000)
        for q in self.sim.q.values():
            bids = [(yes_side(o)[1], o.left) for o in self.orders.values() if not o.taker and o.slug == q.slug and yes_side(o)[0] == "BUY"]
            asks = [(yes_side(o)[1], o.left) for o in self.orders.values() if not o.taker and o.slug == q.slug and yes_side(o)[0] == "SELL"]
            b = max(bids) if bids else (None, 0.0)
            a = min(asks) if asks else (None, 0.0)
            cur = [b[0], round(b[1], 2), a[0], round(a[1], 2)]
            path = self.quote_path.setdefault(q.slug, [])
            if not path or path[-1][1:] != cur:
                path.append([t] + cur)
                del path[:-cap]

    def step(self):
        """decide, execute and commit in one go (shadow mode: the dry venue does not block)."""
        d = self.decide()
        if d["cancel"] or d["place"]:
            self.commit(self.execute(d))

    # ------------------------------------------------------------ fills
    def on_sim_fill(self, q: Quoter, side: str, price: float, shares: float) -> float:
        """Shadow mode: the simulator wants to fill a quote. Returns the shares a dry order carried
        (0 when no order rested there: the fill is dropped, which is what real mode would see)."""
        for o in list(self.orders.values()):
            if o.slug == q.slug and not o.taker and yes_side(o) == (side, price):
                got = min(shares, o.left)
                o.matched += got
                self.stats["fills"] += 1
                self.journal.write("fill", id=o.id, slug=q.slug, side=side, price=price, shares=round(got, 4), sim=True)
                if o.left < 1e-6:
                    self.orders.pop(o.id, None)
                    self.track()
                return got
        self.stats["unbacked"] += 1
        return 0.0

    def poll_io(self) -> Tuple[Dict[str, dict], Dict[str, Optional[dict]]]:
        """Live mode, worker thread: the open orders, and the final state of ours that left the book."""
        open_ = self.venue.orders()
        gone = {oid: self.venue.order(oid) for oid in list(self.orders) if oid not in open_}
        return open_, gone

    def ingest(self, open_: Dict[str, dict], gone: Dict[str, Optional[dict]]):
        """Live mode, event loop: matched size that grew since the last poll is a fill."""
        for oid, o in list(self.orders.items()):
            info = open_.get(oid) or gone.get(oid)
            if info is None:
                continue
            matched = float(info.get("size_matched") or 0)
            if matched > o.matched + 1e-9:
                delta = matched - o.matched
                o.matched = matched
                q = next((x for x in self.sim.q.values() if x.slug == o.slug), None)
                if q is not None:
                    side, px = yes_side(o)
                    if o.taker:
                        fee = delta * self.sim.cfg.taker_fee_rate * px * (1 - px)
                        q.cash -= fee
                        q.fees += fee
                        q.taken += delta
                    self.sim.fill(q, side, px, delta, taker=o.taker)
                self.stats["fills"] += 1
                self.journal.write("fill", id=oid, slug=o.slug, side=o.side, token=o.token[-6:], price=o.price,
                                   shares=round(delta, 4), taker=o.taker)
            if oid not in open_:
                self.orders.pop(oid, None)
        self.refresh_quotes()
        self.track()

    # ------------------------------------------------------------ dashboard
    def summary(self) -> dict:
        ms = sorted(self.call_ms)
        return {"mode": self.sim.mode, "resting": len([o for o in self.orders.values() if not o.taker]),
                "placed": self.stats["placed"], "cancelled": self.stats["cancelled"], "taker": self.stats["taker"],
                "fills": self.stats["fills"], "errors": self.stats["errors"], "refused": dict(self.refused),
                "call_ms_median": round(ms[len(ms) // 2], 1) if ms else None,
                "halt": self.guard.halted, "committed": round(Guard.committed(self.sim.q.values(), self.orders.values()), 4),
                "budget": self.guard.cfg.budget_usdc}
