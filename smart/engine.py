"""The Smart pod's paper engine: S1 signals, simulated orders, virtual NAV and circuit breakers.

The same object runs the backtest (events from closed day files, clock = event time) and the
paper trader on the server (events from the file the recorder is writing, plus the wall clock for
the stale-data check). It never sends an order: there is no live mode in this module.

Simulation rules (design note, "Méthodologie de backtest"):
  - passive orders fill only when the market trades *through* them: a trade strictly below our
    bid, or a best ask strictly below it; a take-profit ask fills on a trade or a best bid strictly
    above it. Touching is not enough, so the queue position never flatters us;
  - every decision is executed `latency_s` later on the book of that moment: an entry is priced,
    and the signal checked again, only then; an aggressive exit walks the bid levels then;
  - takers pay the market's fee (fee_rate_bps from the tape, times min(p, 1 - p) per share),
    makers pay nothing; the tick and a 5 share minimum are respected.

Risk (design note, "Risk management à 100 $"), all in fractions of the virtual NAV:
  per order 3 %, per market 10 %, per event 20 %, gross 50 %, S1 budget 25 %, 20 % of the bid
  depth within two ticks, 1 % of the 24 h volume. Circuit breakers: a 5 % daily loss stops new
  entries until 00:00 UTC, a 15 % drawdown or a NAV under 60 USDC halts the pod (open bids
  cancelled, exits keep running) until the user restarts it, more than 30 orders a minute halts
  it too, and in live mode a feed older than `stale_s` blocks new entries.
"""
import json
import math
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from book_recorder.replay import Books

from .config import Config
from .feed import load_markets, utc_day
from .s1 import S1Detector, Signal, infer_tick


def _ms_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _floor(x: float, step: float) -> float:
    return math.floor(x / step + 1e-9) * step


def _ceil(x: float, step: float) -> float:
    return math.ceil(x / step - 1e-9) * step


@dataclass
class Order:
    id: int
    token: str
    side: str                   # "BUY" (entry bid) or "SELL" (take-profit ask)
    price: float
    shares: float
    t: int
    signal: int
    target: float = 0.0         # entry bids: the take-profit price the position will get


@dataclass
class Position:
    token: str
    shares: float
    cost: float                 # USDC paid, fees included
    entry_px: float
    entry_t: int
    signal: int
    market: str
    event: str
    target_px: float
    target_order: Optional[int] = None
    exiting: bool = False


class Journal:
    """One JSON line per record in <dir>/<UTC day>.jsonl, by the record's own time."""

    def __init__(self, directory):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._f, self._day = None, None

    def write(self, rec: dict):
        day = utc_day(rec["t"]) if rec.get("t") else (self._day or "undated")
        if day != self._day:
            if self._f:
                self._f.close()
            self._f, self._day = open(self.dir / f"{day}.jsonl", "a", encoding="utf-8"), day
        self._f.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")

    def flush(self):
        if self._f:
            self._f.flush()

    def close(self):
        if self._f:
            self._f.close()
            self._f = None


class Engine:
    def __init__(self, cfg: Config, journal: Journal, data_dir=None,
                 wall: Optional[Callable[[], float]] = None, state_path=None):
        self.cfg, self.journal = cfg, journal
        self.data_dir = Path(data_dir) if data_dir else None
        self.wall = wall                    # live mode only: wall clock in seconds
        self.state_path = Path(state_path) if state_path else None
        self.books = Books()
        self.det = S1Detector(cfg)
        self.markets: Dict[str, dict] = {}
        self.event_tokens: Dict[str, List[str]] = {}
        self.fee_bps: Dict[str, float] = {}
        self.last_bid: Dict[str, float] = {}
        # money
        self.cash = cfg.start_nav
        self.positions: Dict[str, Position] = {}
        self.orders: Dict[int, Order] = {}
        self.pending: List[dict] = []       # actions waiting for the latency: entries and exits
        self.followups: List[tuple] = []    # (due ms, signal id, token, minutes)
        self.hwm = cfg.start_nav
        self.day: Optional[str] = None
        self.day_start_nav = cfg.start_nav
        self.blocked_day: Optional[str] = None
        self.halted: Optional[str] = None
        self.placed: deque = deque()        # times of orders placed, for the rate breaker
        self.seq = 0
        self.now = 0
        self.last_r = 0
        self.resume_after = 0               # events up to this time only rebuild the books
        self._next_timer = 0
        self._next_nav = 0
        self._next_save = 0
        self.closed = 0
        self.realised = 0.0
        if self.state_path and self.state_path.exists():
            self._load()

    # ================================================================== state
    def _load(self):
        s = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.cash, self.hwm = s["cash"], s["hwm"]
        self.day, self.day_start_nav = s.get("day"), s.get("day_start_nav", self.cfg.start_nav)
        self.blocked_day, self.halted = s.get("blocked_day"), s.get("halted")
        self.seq, self.closed, self.realised = s.get("seq", 0), s.get("closed", 0), s.get("realised", 0.0)
        self.positions = {k: Position(**v) for k, v in s.get("positions", {}).items()}
        self.orders = {int(k): Order(**v) for k, v in s.get("orders", {}).items()}
        self.pending = s.get("pending", [])
        self.last_bid = s.get("last_bid", {})
        self.resume_after = s.get("last_r", 0)

    def save(self):
        if not self.state_path:
            return
        s = {"cash": self.cash, "hwm": self.hwm, "day": self.day, "day_start_nav": self.day_start_nav,
             "blocked_day": self.blocked_day, "halted": self.halted, "seq": self.seq,
             "closed": self.closed, "realised": self.realised, "last_r": self.last_r,
             "positions": {k: asdict(v) for k, v in self.positions.items()},
             "orders": {str(k): asdict(v) for k, v in self.orders.items()},
             "pending": self.pending,
             "last_bid": {k: self.last_bid[k] for k in self.positions if k in self.last_bid},
             "nav": round(self.nav(), 4), "saved_at": _ms_iso(self.now) if self.now else None}
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(s, indent=1), encoding="utf-8")
        tmp.replace(self.state_path)

    def clear_halt(self):
        self.halted = None
        self.hwm = self.nav()

    # ================================================================== helpers
    def log(self, kind: str, **fields):
        self.journal.write({"t": self.now, "at": _ms_iso(self.now) if self.now else None, "k": kind, **fields})

    def nid(self) -> int:
        self.seq += 1
        return self.seq

    def tick(self, token: str) -> float:
        b = self.books.get(token)
        if b is None:
            return 0.01
        if b.tick:
            return b.tick
        return infer_tick(list(b.bids)[:20] + list(b.asks)[:20])

    def nav(self) -> float:
        return self.cash + sum(p.shares * self.last_bid.get(p.token, 0.0) for p in self.positions.values())

    def exposure(self, pred=lambda tok: True) -> float:
        """USDC at risk: positions at cost plus open entry bids."""
        x = sum(p.cost for p in self.positions.values() if pred(p.token))
        x += sum(o.price * o.shares for o in self.orders.values() if o.side == "BUY" and pred(o.token))
        x += sum(a["usdc"] for a in self.pending if a["kind"] == "entry" and pred(a["token"]))
        return x

    def load_markets(self, day: str):
        if not self.data_dir:
            return
        m = load_markets(self.data_dir, day)
        if not m:
            return
        self.markets.update(m)
        self.event_tokens = {}
        for tok, info in self.markets.items():
            self.event_tokens.setdefault(info.get("event_slug") or info["condition_id"], []).append(tok)

    def market_key(self, token: str) -> str:
        return self.markets.get(token, {}).get("condition_id", token)

    def event_key(self, token: str) -> str:
        m = self.markets.get(token, {})
        return m.get("event_slug") or m.get("condition_id") or token

    # ================================================================== events
    def on_event(self, ev: dict):
        e = ev.get("_e")
        if e is not None:
            if e == "day":
                self.load_markets(ev["day"])
            elif e == "markets" and self.day:
                self.load_markets(utc_day(ev["_r"]))
            elif e == "idle":
                self.on_clock(ev["_now"], idle=True)
            return
        r = ev.get("_r")
        if r is None:
            return
        self.now = max(self.now, r)
        self.last_r = self.now
        replaying = self.now <= self.resume_after
        if not replaying:
            self.on_clock(self.now)             # actions due before this event see the book before it
        trade = self.books.apply(ev)
        kind = ev.get("event_type")
        if trade is not None:
            if trade.fee_rate_bps is not None:
                self.fee_bps[trade.token] = trade.fee_rate_bps
            comp = self.markets.get(trade.token, {}).get("complement")
            self.det.on_trade(self.now, trade.token, trade.price, trade.size, trade.side, comp)
            if not replaying:
                self.fill_on_trade(trade.token, trade.price, trade.size)
        elif kind in ("book", "price_change"):
            toks = ([str(c["asset_id"]) for c in ev["price_changes"]] if "price_changes" in ev
                    else [str(ev.get("asset_id"))])
            for tok in dict.fromkeys(toks):
                self.on_book(tok, replaying)

    def on_book(self, token: str, replaying: bool):
        b = self.books.get(token)
        if b is None:
            return
        bid, ask = b.best_bid(), b.best_ask()
        if bid is not None:
            self.last_bid[token] = bid
        if bid is None or ask is None or ask <= bid:
            return
        tick = self.tick(token)
        sig = self.det.on_mid(self.now, token, (bid + ask) / 2, bid, ask, tick)
        if replaying:
            return
        self.fill_on_book(token, bid, ask)
        if sig is not None:
            self.on_signal(sig)

    # ================================================================== signals
    def on_signal(self, sig: Signal):
        sid = self.nid()
        m = self.markets.get(sig.token)
        reason = self.entry_block(sig, m)
        self.log("signal", id=sid, token=sig.token, slug=(m or {}).get("slug"), outcome=(m or {}).get("outcome"),
                 peak=sig.peak_mid, mid=sig.mid, drop=round(sig.drop, 4), sigma=round(sig.sigma, 5),
                 drop_sigmas=round(sig.drop_sigmas, 2), levels=sig.levels, sweep_usdc=sig.sweep_usdc,
                 bid=sig.best_bid, ask=sig.best_ask, tick=sig.tick, skip=reason)
        for minutes in self.cfg.followup_min:
            self.followups.append((self.now + minutes * 60000, sid, sig.token, minutes, sig.mid, sig.drop))
        if reason:
            return
        usdc = self.size(sig.token, sig.best_bid, sig.tick)
        if isinstance(usdc, str):
            self.log("skip", id=sid, why=usdc)
            return
        self.pending.append({"kind": "entry", "due": self.now + int(self.cfg.latency_s * 1000), "token": sig.token,
                             "usdc": usdc, "signal": sid, "peak": sig.peak_mid, "drop": sig.drop})

    def entry_block(self, sig: Signal, m: Optional[dict]) -> Optional[str]:
        cfg = self.cfg
        if self.halted:
            return "halted"
        if self.blocked_day == utc_day(self.now):
            return "day_loss"
        if self.wall and self.wall() * 1000 - self.now > cfg.stale_s * 1000:
            return "stale_feed"
        if m is None:
            return "unknown_market"
        if not (cfg.min_price <= sig.best_bid and sig.best_ask <= cfg.max_price):
            return "price_out_of_range"
        if (sig.best_ask - sig.best_bid) > cfg.max_spread_ticks * sig.tick + 1e-9:
            return "wide_spread"
        end = m.get("end_date")
        if end:
            try:
                end_ms = datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp() * 1000
                if end_ms - self.now < cfg.min_hours_left * 3.6e6:
                    return "ends_soon"
            except ValueError:
                pass
        if sig.token in self.positions or any(o.token == sig.token for o in self.orders.values()) \
                or any(a["token"] == sig.token for a in self.pending):
            return "already_in"
        siblings = [t for t in self.event_tokens.get(self.event_key(sig.token), [])
                    if self.market_key(t) != self.market_key(sig.token)]
        if self.det.moved_recently(siblings, self.now):
            return "siblings_moved"
        return None

    def size(self, token: str, bid: float, tick: float):
        """USDC for an entry, or the reason there is none."""
        cfg, nav = self.cfg, self.nav()
        mk, ev = self.market_key(token), self.event_key(token)
        b = self.books.get(token)
        depth_usdc = b.depth("bid", 2 * tick) * bid if b else 0.0
        vol = self.markets.get(token, {}).get("volume_24h") or 0.0
        caps = {
            "order": cfg.max_order * nav,
            "s1_budget": cfg.s1_budget * nav - self.exposure(),
            "market": cfg.max_market * nav - self.exposure(lambda t: self.market_key(t) == mk),
            "event": cfg.max_event * nav - self.exposure(lambda t: self.event_key(t) == ev),
            "gross": cfg.max_gross * nav - self.exposure(),
            "depth": cfg.depth_frac * depth_usdc,
            "volume": cfg.volume_frac * vol,
            "cash": self.cash - sum(o.price * o.shares for o in self.orders.values() if o.side == "BUY")
                    - sum(a["usdc"] for a in self.pending if a["kind"] == "entry"),
        }
        binding = min(caps, key=caps.get)
        usdc = caps[binding]
        if usdc < cfg.min_order_usdc:
            return f"too_small:{binding}"
        return round(usdc, 4)

    # ================================================================== orders
    def count_order(self):
        self.placed.append(self.now)
        while self.placed and self.placed[0] < self.now - 60000:
            self.placed.popleft()
        if len(self.placed) > self.cfg.max_orders_per_min and not self.halted:
            self.halt("order_rate")

    def place_entry(self, a: dict):
        tok = a["token"]
        b = self.books.get(tok)
        bid, ask = (b.best_bid(), b.best_ask()) if b else (None, None)
        if bid is None or ask is None:
            self.log("entry_missed", id=a["signal"], why="empty_book")
            return
        if self.halted or self.blocked_day == utc_day(self.now):
            self.log("entry_missed", id=a["signal"], why="breaker")
            return
        tick = self.tick(tok)
        mid = (bid + ask) / 2
        if mid >= a["peak"] - (1 - self.cfg.revert_frac) * a["drop"]:
            self.log("entry_missed", id=a["signal"], why="already_reverted", mid=mid)
            return
        price = round(bid + tick, 6) if bid + tick < ask - 1e-9 else bid
        if not (self.cfg.min_price <= price <= self.cfg.max_price):
            self.log("entry_missed", id=a["signal"], why="price_out_of_range", price=price)
            return
        shares = _floor(a["usdc"] / price, 0.01)
        if shares < self.cfg.min_shares or shares * price < self.cfg.min_order_usdc:
            self.log("entry_missed", id=a["signal"], why="below_minimum", shares=shares, price=price)
            return
        o = Order(id=self.nid(), token=tok, side="BUY", price=price, shares=shares, t=self.now, signal=a["signal"])
        self.orders[o.id] = o
        self.count_order()
        target = min(_ceil(mid + self.cfg.revert_frac * a["drop"], tick), self.cfg.max_price + 0.04)
        o.target = round(max(target, price + tick), 6)
        self.log("order", id=o.id, signal=o.signal, token=tok, side="BUY", price=price, shares=shares,
                 bid=bid, ask=ask, target=o.target)

    def fill(self, o: Order, shares: float, how: str):
        shares = min(shares, o.shares)
        if shares <= 0:
            return
        o.shares = round(o.shares - shares, 6)
        if o.side == "BUY":
            cost = shares * o.price
            self.cash -= cost
            p = self.positions.get(o.token)
            if p is None:
                target = o.target or round(o.price + self.tick(o.token), 6)
                p = self.positions[o.token] = Position(
                    token=o.token, shares=0.0, cost=0.0, entry_px=o.price, entry_t=self.now, signal=o.signal,
                    market=self.market_key(o.token), event=self.event_key(o.token), target_px=target)
            p.shares = round(p.shares + shares, 6)
            p.cost += cost
            p.entry_px = p.cost / p.shares
            self.log("fill", id=o.id, signal=o.signal, side="BUY", price=o.price, shares=shares, how=how)
            if o.shares <= 1e-9:
                self.orders.pop(o.id, None)
            self.refresh_target(p)
        else:
            p = self.positions.get(o.token)
            if p is None:
                del self.orders[o.id]
                return
            self.close_part(p, shares, o.price, 0.0, how, o.id)
            if o.shares <= 1e-9:
                self.orders.pop(o.id, None)
                if p.token in self.positions:
                    p.target_order = None

    def refresh_target(self, p: Position):
        """One take-profit ask for the whole position."""
        if p.target_order is not None:
            self.orders.pop(p.target_order, None)
        o = Order(id=self.nid(), token=p.token, side="SELL", price=p.target_px, shares=p.shares,
                  t=self.now, signal=p.signal)
        self.orders[o.id] = o
        p.target_order = o.id
        self.count_order()

    def close_part(self, p: Position, shares: float, price: float, fee: float, how: str, oid=None):
        shares = min(shares, p.shares)
        cost = p.cost * shares / p.shares
        proceeds = shares * price - fee
        pnl = proceeds - cost
        self.cash += proceeds
        p.cost -= cost
        p.shares = round(p.shares - shares, 6)
        self.realised += pnl
        self.log("exit", id=oid, signal=p.signal, token=p.token, price=price, shares=shares, fee=round(fee, 6),
                 pnl=round(pnl, 6), ret=round(pnl / cost, 6) if cost else None, how=how,
                 held_s=round((self.now - p.entry_t) / 1000, 1), entry_px=round(p.entry_px, 6))
        if p.shares <= 1e-6:
            if p.target_order is not None:
                self.orders.pop(p.target_order, None)
            del self.positions[p.token]
            self.closed += 1

    def fill_on_trade(self, token: str, price: float, size: float):
        for o in [o for o in self.orders.values() if o.token == token]:
            if o.id not in self.orders:
                continue
            if o.side == "BUY" and price < o.price - 1e-9:
                self.fill(o, min(o.shares, size), "trade_through")
            elif o.side == "SELL" and price > o.price + 1e-9:
                self.fill(o, min(o.shares, size), "trade_through")

    def fill_on_book(self, token: str, bid: float, ask: float):
        for o in [o for o in self.orders.values() if o.token == token]:
            if o.id not in self.orders:
                continue
            if o.side == "BUY" and ask < o.price - 1e-9:
                self.fill(o, o.shares, "ask_through")
            elif o.side == "SELL" and bid > o.price + 1e-9:
                self.fill(o, o.shares, "bid_through")

    def sell_now(self, p: Position, why: str):
        """Aggressive exit: walk the bids `latency_s` from now."""
        if p.exiting:
            return
        p.exiting = True
        if p.target_order is not None:
            self.orders.pop(p.target_order, None)
            p.target_order = None
        self.pending.append({"kind": "exit", "due": self.now + int(self.cfg.latency_s * 1000), "token": p.token,
                             "why": why, "usdc": 0.0})
        self.count_order()

    def execute_exit(self, a: dict):
        p = self.positions.get(a["token"])
        if p is None:
            return
        b = self.books.get(p.token)
        left, filled_any = p.shares, False
        bps = self.fee_bps.get(p.token, 0.0) or 0.0
        for price, size in (b.levels("bid", 50) if b else []):
            if left <= 1e-9 or p.token not in self.positions:
                break
            q = min(left, size)
            fee = bps / 1e4 * q * min(price, 1 - price)
            self.close_part(p, q, price, fee, a["why"])
            left -= q
            filled_any = True
        if p.token in self.positions:           # book too thin: try again after the latency
            p.exiting = False
            if filled_any:
                self.log("exit_partial", token=p.token, left=p.shares)
            self.sell_now(p, a["why"])

    def cancel_entries(self, why: str):
        for o in [o for o in self.orders.values() if o.side == "BUY"]:
            del self.orders[o.id]
            self.log("cancel", id=o.id, signal=o.signal, why=why)
        self.pending = [a for a in self.pending if a["kind"] != "entry"]

    def halt(self, why: str):
        self.halted = why
        self.log("breaker", what="halt", why=why, nav=round(self.nav(), 4), hwm=round(self.hwm, 4))
        self.cancel_entries(why)

    # ================================================================== clock
    def on_clock(self, now: int, idle: bool = False):
        if idle:
            if self.wall:
                self.log_nav_if_due(int(now))
            return
        if self.pending and min(a["due"] for a in self.pending) <= now:
            due, self.pending = [a for a in self.pending if a["due"] <= now], [a for a in self.pending if a["due"] > now]
            for a in sorted(due, key=lambda a: a["due"]):
                (self.place_entry if a["kind"] == "entry" else self.execute_exit)(a)
        if now < self._next_timer:
            return
        self._next_timer = now + 1000
        cfg = self.cfg
        day = utc_day(now)
        if day != self.day:
            if self.day is not None:
                self.log("day_end", day=self.day, nav=round(self.nav(), 4), start=round(self.day_start_nav, 4))
            self.day, self.day_start_nav = day, self.nav()
        for o in [o for o in self.orders.values() if o.side == "BUY" and now - o.t > cfg.entry_ttl_s * 1000]:
            del self.orders[o.id]
            self.log("cancel", id=o.id, signal=o.signal, why="ttl")
        for p in list(self.positions.values()):
            bid = self.last_bid.get(p.token)
            if now - p.entry_t >= cfg.hold_s * 1000:
                self.sell_now(p, "time")
            elif bid is not None and bid < cfg.rescue_frac * p.entry_px:
                self.sell_now(p, "rescue")
        if self.followups:
            keep = []
            for f in self.followups:
                if f[0] <= now:
                    due_ms, sid, tok, minutes, mid0, drop = f
                    b = self.books.get(tok)
                    mid = b.mid() if b else None
                    self.log("followup", id=sid, token=tok, minutes=minutes, mid=mid, bid=b.best_bid() if b else None,
                             reverted=round((mid - mid0) / drop, 4) if mid is not None and drop else None)
                else:
                    keep.append(f)
            self.followups = keep
        nav = self.nav()
        self.hwm = max(self.hwm, nav)
        if not self.halted:
            if nav < cfg.floor_nav:
                self.halt("nav_floor")
            elif nav < self.hwm * (1 - cfg.max_drawdown):
                self.halt("drawdown")
        if self.blocked_day != day and nav < self.day_start_nav * (1 - cfg.day_loss):
            self.blocked_day = day
            self.log("breaker", what="day_loss", nav=round(nav, 4), start=round(self.day_start_nav, 4))
            self.cancel_entries("day_loss")
        self.log_nav_if_due(now)

    def log_nav_if_due(self, now: int):
        if now >= self._next_nav:
            self._next_nav = now + 300000
            stale = round(self.wall() - self.now / 1000, 1) if self.wall and self.now else None
            self.log("nav", nav=round(self.nav(), 4), cash=round(self.cash, 4), hwm=round(self.hwm, 4),
                     positions=len(self.positions), orders=len(self.orders), halted=self.halted,
                     closed=self.closed, realised=round(self.realised, 4), feed_lag_s=stale)
            self.journal.flush()
        if now >= self._next_save:
            self._next_save = now + 60000
            self.save()
