"""Paper market making on a few chosen markets, replayed on the recorded books.

One quoter per market, on the market's first token (YES). It rests one bid and one ask, earns the
spread when both get hit, and its only real risk is the inventory it holds when the price moves.
Nothing is ever sent: this is a simulator.

Quotes, recomputed at most every `requote_s` and effective `latency_s` later (the old quotes stay
exposed meanwhile, which is how a maker gets picked off):

  fair      mid - skew_ticks * tick * (inventory / max inventory)     lean against the inventory
  bid       the highest tick <= fair - half_spread, and at most one tick inside the best bid
  ask       the lowest tick  >= fair + half_spread, and at most one tick inside the best ask
  never crossing the book, never outside [min_price, max_price], none within `min_hours_left`
  of the end date, and no bid (ask) when the inventory is at +max (-max).

Protection against informed flow (the S1 study: a resting bid filled during a sweep lost 1.8
cents a share): when aggressive selling in the last `sweep_window_s` exceeds `sweep_usdc`, or the
mid fell by the whole spread (`sweep_ticks` ticks at least) in that window, the bid is pulled for `pull_s`; same for the ask.

Fills, as strict as in the other studies: a resting bid is filled only by an aggressive sale that
prints strictly below it (so a quote that merely joins the best price waits behind the whole
queue), for at most the size of that print. Purchases of NO at p count as sales of YES at 1 - p.
Makers pay no fee on Polymarket.

A short position in YES is a long position in NO: selling YES at p without holding it costs
1 - p of capital per share. `max_inventory_usdc` caps the capital at risk on each side.
"""
import json
import math
from collections import deque
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from book_recorder.replay import Books

from .feed import load_markets, replay, utc_day
from .s1 import infer_tick


@dataclass
class MMConfig:
    quote_usdc: float = 3.0             # capital per quote (3 % of 100)
    max_inventory_usdc: float = 10.0    # per market: the most the open position can lose (10 % of 100)
    stop_loss_usdc: float = 5.0         # per market: past this loss at mid, only quote the side that reduces
    half_spread_ticks: float = 1.0      # minimum distance from fair to each quote
    skew_ticks: float = 2.0             # shift of fair at full inventory
    improve: bool = True                # step one tick inside the best price when there is room
    requote_s: float = 1.0
    latency_s: float = 2.0
    min_price: float = 0.03
    max_price: float = 0.97
    min_hours_left: float = 24.0
    min_shares: float = 5.0
    sweep_window_s: float = 30.0
    sweep_usdc: float = 200.0
    sweep_ticks: int = 2
    pull_s: float = 60.0

    @classmethod
    def from_dict(cls, d: dict) -> "MMConfig":
        unknown = set(d) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"unknown parameters: {sorted(unknown)}")
        return cls(**d)


@dataclass
class Quoter:
    token: str
    slug: str
    end_ms: Optional[float]
    inv: float = 0.0                    # shares of YES, negative = short YES (long NO)
    cash: float = 0.0
    avg: float = 0.0                    # average entry price of the open position
    reduce_only: bool = False           # set for good once the market has lost stop_loss_usdc
    bid: Optional[float] = None         # live quotes
    ask: Optional[float] = None
    bid_sz: float = 0.0
    ask_sz: float = 0.0
    pending: Optional[tuple] = None     # (effective ms, bid, bid_sz, ask, ask_sz)
    next_quote: int = 0
    pull_bid_until: int = 0
    pull_ask_until: int = 0
    flow: deque = field(default_factory=deque)      # (t, signed usdc) aggressive flow on YES
    mids: deque = field(default_factory=deque)      # (t, mid)
    fills: List[dict] = field(default_factory=list)
    bought: float = 0.0
    sold: float = 0.0
    buy_cost: float = 0.0
    sell_proceeds: float = 0.0
    max_long: float = 0.0
    max_short: float = 0.0
    pulls: int = 0
    quote_updates: int = 0
    last_mid: Optional[float] = None
    last_bid: Optional[float] = None
    last_ask: Optional[float] = None
    quoted_s: float = 0.0               # seconds with both quotes live
    _last_t: int = 0
    nav_path: List[tuple] = field(default_factory=list)


class MMSim:
    def __init__(self, data_dir, slugs: List[str], cfg: Optional[MMConfig] = None, markouts_s=(60, 300, 1800)):
        self.cfg = cfg or MMConfig()
        self.data_dir = Path(data_dir)
        self.slugs = list(slugs)
        self.books = Books()
        self.markets: Dict[str, dict] = {}
        self.q: Dict[str, Quoter] = {}              # first token -> quoter
        self.mirror: Dict[str, str] = {}            # second token -> first token
        self.now = 0
        self.markouts_s = markouts_s
        self.timers: List[tuple] = []               # (due, fill dict, horizon, token) kept sorted by append order
        self.missing = set(slugs)

    # ------------------------------------------------------------ setup
    def load(self, day: str):
        self.markets.update(load_markets(self.data_dir, day))
        for tok, m in self.markets.items():
            if m.get("slug") in self.slugs and len(m.get("tokens", [])) == 2:
                first, second = m["tokens"][0]["token_id"], m["tokens"][1]["token_id"]
                if first not in self.q:
                    end = None
                    if m.get("end_date"):
                        try:
                            end = datetime.fromisoformat(m["end_date"].replace("Z", "+00:00")).timestamp() * 1000
                        except ValueError:
                            pass
                    self.q[first] = Quoter(token=first, slug=m["slug"], end_ms=end)
                    self.mirror[second] = first
                    self.missing.discard(m["slug"])

    # ------------------------------------------------------------ events
    def on_event(self, ev: dict):
        e = ev.get("_e")
        if e is not None:
            if e == "day":
                self.load(ev["day"])
            elif e == "markets" and ev.get("_r"):
                self.load(utc_day(ev["_r"]))
            return
        r = ev.get("_r")
        if r is None:
            return
        kind = ev.get("event_type")
        if kind == "price_change" and "price_changes" in ev:
            toks = {str(c["asset_id"]) for c in ev["price_changes"]}
        else:
            toks = {str(ev.get("asset_id"))}
        if not any(t in self.q or t in self.mirror for t in toks):
            return
        self.now = max(self.now, r)
        self.run_timers()
        trade = self.books.apply(ev)
        if trade is not None:
            self.on_trade(trade)
        elif kind in ("book", "price_change"):
            for t in toks:
                if t in self.q:
                    self.on_book(self.q[t])

    def run_timers(self):
        while self.timers and self.timers[0][0] <= self.now:
            _, f, h, tok = self.timers.pop(0)
            b = self.books.get(tok)
            mid = b.mid() if b else None
            if mid is not None:
                f[f"markout_{h}s"] = round((mid - f["price"]) * (1 if f["side"] == "BUY" else -1), 6)

    def on_trade(self, t):
        side = (t.side or "").upper()
        if side not in ("BUY", "SELL") or t.size <= 0:
            return
        if t.token in self.q:
            q, eps, px = self.q[t.token], (1 if side == "BUY" else -1), t.price
        elif t.token in self.mirror:
            q, eps, px = self.q[self.mirror[t.token]], (-1 if side == "BUY" else 1), round(1 - t.price, 6)
        else:
            return
        self.apply_pending(q)
        q.flow.append((self.now, eps * t.price * t.size))
        if eps < 0 and q.bid is not None and px < q.bid - 1e-9 and q.bid_sz > 0:
            self.fill(q, "BUY", q.bid, min(q.bid_sz, t.size))
        elif eps > 0 and q.ask is not None and px > q.ask + 1e-9 and q.ask_sz > 0:
            self.fill(q, "SELL", q.ask, min(q.ask_sz, t.size))
        self.protect(q)

    def fill(self, q: Quoter, side: str, price: float, shares: float):
        before, signed = q.inv, (shares if side == "BUY" else -shares)
        after = before + signed
        if before * signed >= 0:                    # opening or adding: weighted entry price
            q.avg = (abs(before) * q.avg + shares * price) / abs(after)
        elif before * after < 0:                    # flipped through zero: the rest opens at this price
            q.avg = price
        if side == "BUY":
            q.inv += shares
            q.cash -= shares * price
            q.bid_sz -= shares
            q.bought += shares
            q.buy_cost += shares * price
            if q.bid_sz < self.cfg.min_shares:
                q.bid, q.bid_sz = None, 0.0
        else:
            q.inv -= shares
            q.cash += shares * price
            q.ask_sz -= shares
            q.sold += shares
            q.sell_proceeds += shares * price
            if q.ask_sz < self.cfg.min_shares:
                q.ask, q.ask_sz = None, 0.0
        q.max_long, q.max_short = max(q.max_long, q.inv), min(q.max_short, q.inv)
        f = {"t": self.now, "slug": q.slug, "side": side, "price": price, "shares": round(shares, 4),
             "mid": q.last_mid, "inv_after": round(q.inv, 4)}
        q.fills.append(f)
        for h in self.markouts_s:
            self.timers.append((self.now + h * 1000, f, h, q.token))
        self.timers.sort(key=lambda x: x[0])
        q.next_quote = 0                            # requote at once after a fill

    def protect(self, q: Quoter):
        cfg, w = self.cfg, int(self.cfg.sweep_window_s * 1000)
        while q.flow and q.flow[0][0] < self.now - w:
            q.flow.popleft()
        while len(q.mids) > 1 and q.mids[1][0] <= self.now - w:
            q.mids.popleft()
        sells = -sum(u for _, u in q.flow if u < 0)
        buys = sum(u for _, u in q.flow if u > 0)
        b = self.books.get(q.token)
        tick = (b.tick or infer_tick(list(b.bids)[:20] + list(b.asks)[:20])) if b else 0.01
        mid = q.last_mid
        hi = max((m for _, m in q.mids), default=mid)
        lo = min((m for _, m in q.mids), default=mid)
        until = self.now + int(cfg.pull_s * 1000)
        spread = (q.last_ask - q.last_bid) if q.last_ask is not None and q.last_bid is not None else 0.0
        move = max(cfg.sweep_ticks * tick, spread) - 1e-9   # a move of the whole spread, 2 ticks at least
        if sells >= cfg.sweep_usdc or (mid is not None and hi - mid >= move):
            if q.pull_bid_until <= self.now:
                q.pulls += 1
            q.pull_bid_until = until
            q.bid, q.bid_sz = None, 0.0             # a cancel is assumed to land at once: optimistic by latency_s
        if buys >= cfg.sweep_usdc or (mid is not None and mid - lo >= move):
            if q.pull_ask_until <= self.now:
                q.pulls += 1
            q.pull_ask_until = until
            q.ask, q.ask_sz = None, 0.0

    def apply_pending(self, q: Quoter):
        if q.pending and q.pending[0] <= self.now:
            _, bid, bsz, ask, asz = q.pending
            q.pending = None
            b = self.books.get(q.token)
            bb, ba = (b.best_bid(), b.best_ask()) if b else (None, None)
            if bid is not None and ba is not None and bid >= ba - 1e-9:
                bid = None                          # would cross now: a post-only order is rejected
            if ask is not None and bb is not None and ask <= bb + 1e-9:
                ask = None
            if bid is not None and self.now < q.pull_bid_until:
                bid = None
            if ask is not None and self.now < q.pull_ask_until:
                ask = None
            q.bid, q.bid_sz = bid, (bsz if bid is not None else 0.0)
            q.ask, q.ask_sz = ask, (asz if ask is not None else 0.0)
            q.quote_updates += 1

    def on_book(self, q: Quoter):
        cfg = self.cfg
        b = self.books.get(q.token)
        bid, ask = b.best_bid(), b.best_ask()
        if q._last_t and q.bid is not None and q.ask is not None:
            q.quoted_s += (self.now - q._last_t) / 1000
        q._last_t = self.now
        self.apply_pending(q)
        if bid is None or ask is None or ask <= bid:
            return
        mid = (bid + ask) / 2
        q.last_mid, q.last_bid, q.last_ask = mid, bid, ask
        if not q.mids or q.mids[-1][1] != mid:
            q.mids.append((self.now, mid))
        self.protect(q)
        if not q.nav_path or self.now - q.nav_path[-1][0] >= 300000:
            q.nav_path.append((self.now, round(q.cash + q.inv * mid, 4), round(mid, 5), round(q.inv, 2)))
        if self.now < q.next_quote:
            return
        q.next_quote = self.now + int(cfg.requote_s * 1000)
        tick = b.tick or infer_tick(list(b.bids)[:20] + list(b.asks)[:20])
        # worst-case loss of the open position, at its entry price: the cap does not grow when
        # the price runs against us (a cap at the current mid let the simulator add to a loser)
        risk = q.inv * q.avg if q.inv >= 0 else -q.inv * (1 - q.avg)
        room = max(cfg.max_inventory_usdc - risk, 0.0)
        frac = (1 if q.inv >= 0 else -1) * min(risk / cfg.max_inventory_usdc, 1.0)
        if not q.reduce_only and q.cash + q.inv * mid <= -cfg.stop_loss_usdc:
            q.reduce_only = True
        fair = mid - cfg.skew_ticks * tick * frac
        h = cfg.half_spread_ticks * tick
        my_bid = math.floor((fair - h) / tick + 1e-9) * tick
        my_ask = math.ceil((fair + h) / tick - 1e-9) * tick
        my_bid = min(my_bid, bid + tick if cfg.improve else bid)
        my_ask = max(my_ask, ask - tick if cfg.improve else ask)
        my_bid, my_ask = min(my_bid, ask - tick), max(my_ask, bid + tick)
        my_bid, my_ask = round(my_bid, 6), round(my_ask, 6)
        late = q.end_ms is not None and q.end_ms - self.now < cfg.min_hours_left * 3.6e6
        qs = cfg.quote_usdc / max(min(mid, 1 - mid), 0.05)      # same share count on both sides
        if q.inv >= 0:                              # a bid adds to a long, an ask reduces it then may open a short
            bsz = min(qs, room / max(my_bid, 0.01))
            asz = min(qs, q.inv + cfg.max_inventory_usdc / max(1 - my_ask, 0.01))
        else:
            bsz = min(qs, -q.inv + cfg.max_inventory_usdc / max(my_bid, 0.01))
            asz = min(qs, room / max(1 - my_ask, 0.01))
        if late or q.reduce_only:                   # never open or flip: at most flatten
            bsz, asz = min(bsz, max(-q.inv, 0.0)), min(asz, max(q.inv, 0.0))
        ok_bid = (not late or q.inv < 0) and cfg.min_price <= my_bid <= cfg.max_price and bsz >= cfg.min_shares \
            and self.now >= q.pull_bid_until
        ok_ask = (not late or q.inv > 0) and cfg.min_price <= my_ask <= cfg.max_price and asz >= cfg.min_shares \
            and self.now >= q.pull_ask_until
        want = (my_bid if ok_bid else None, round(bsz, 2) if ok_bid else 0.0,
                my_ask if ok_ask else None, round(asz, 2) if ok_ask else 0.0)
        if (q.bid, q.ask) != (want[0], want[2]) or q.pending:
            q.pending = (self.now + int(cfg.latency_s * 1000),) + want

    # ------------------------------------------------------------ results
    def result(self, q: Quoter, days: float) -> dict:
        mid = q.last_mid or 0.0
        liq = (q.last_bid if q.inv > 0 else q.last_ask) or mid
        matched = min(q.bought, q.sold)
        avg_buy = q.buy_cost / q.bought if q.bought else None
        avg_sell = q.sell_proceeds / q.sold if q.sold else None
        navs = [x[1] for x in q.nav_path]
        peak, mdd = -1e18, 0.0
        for v in navs:
            peak = max(peak, v)
            mdd = max(mdd, peak - v)

        def mo(h, side=None):
            xs = [f[f"markout_{h}s"] * f["shares"] for f in q.fills if f"markout_{h}s" in f and (side is None or f["side"] == side)]
            sh = sum(f["shares"] for f in q.fills if f"markout_{h}s" in f and (side is None or f["side"] == side))
            return round(100 * sum(xs) / sh, 3) if sh else None

        return {"slug": q.slug, "fills": len(q.fills), "buys": sum(1 for f in q.fills if f["side"] == "BUY"),
                "sells": sum(1 for f in q.fills if f["side"] == "SELL"),
                "shares_bought": round(q.bought, 1), "shares_sold": round(q.sold, 1),
                "avg_buy": round(avg_buy, 4) if avg_buy else None, "avg_sell": round(avg_sell, 4) if avg_sell else None,
                "round_trip_shares": round(matched, 1),
                "spread_captured_usdc": round(matched * (avg_sell - avg_buy), 4) if matched else 0.0,
                "inventory_end_shares": round(q.inv, 2), "last_mid": round(mid, 4),
                "pnl_at_mid_usdc": round(q.cash + q.inv * mid, 4),
                "pnl_if_liquidated_usdc": round(q.cash + q.inv * liq, 4),
                "pnl_per_day_usdc": round((q.cash + q.inv * liq) / days, 4) if days else None,
                "max_drawdown_usdc": round(mdd, 4),
                "max_long_shares": round(q.max_long, 1), "max_short_shares": round(q.max_short, 1),
                "stopped": q.reduce_only, "pulls": q.pulls, "quote_updates": q.quote_updates,
                "hours_two_sided": round(q.quoted_s / 3600, 1),
                **{f"fill_markout_{h}s_c": mo(h) for h in self.markouts_s},
                "buy_markout_300s_c": mo(300, "BUY"), "sell_markout_300s_c": mo(300, "SELL")}


def run(data_dir, days: List[str], slugs: List[str], out_dir, cfg: Optional[MMConfig] = None, progress=None) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    sim = MMSim(data_dir, slugs, cfg)
    n = 0
    first = last = None
    for ev in replay(Path(data_dir) / "book", days):
        sim.on_event(ev)
        n += 1
        r = ev.get("_r")
        if r:
            first = first or r
            last = r
        if progress and n % 2_000_000 == 0:
            progress(n, sim)
    span = (last - first) / 86400000 if first and last else 0.0
    res = [sim.result(q, span) for q in sim.q.values()]
    with open(out / "mm_fills.jsonl", "w", encoding="utf-8") as f:
        for q in sim.q.values():
            for x in q.fills:
                f.write(json.dumps(x, separators=(",", ":")) + "\n")
    with open(out / "mm_nav.jsonl", "w", encoding="utf-8") as f:     # 5 minute samples: P&L at mid, mid, inventory
        for q in sim.q.values():
            for t, nav, mid, inv in q.nav_path:
                f.write(json.dumps({"t": t, "slug": q.slug, "pnl": nav, "mid": mid, "inv": inv}, separators=(",", ":")) + "\n")
    s = {"days": days, "span_days": round(span, 2), "config": asdict(sim.cfg), "markets_not_found": sorted(sim.missing),
         "total_pnl_if_liquidated_usdc": round(sum(r["pnl_if_liquidated_usdc"] for r in res), 4),
         "total_pnl_at_mid_usdc": round(sum(r["pnl_at_mid_usdc"] for r in res), 4),
         "markets": res}
    (out / "mm_sim.json").write_text(json.dumps(s, indent=1), encoding="utf-8")
    return s
