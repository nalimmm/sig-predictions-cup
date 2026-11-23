"""Which markets are worth making, measured before any quoting code is written.

Two read-only measurements, one per style of market making.

A. Tight quoting, on the recorded books (`markouts`). For every aggressive trade, from the side of
   the maker who was hit, per share, in the price of the market's first token (a purchase of NO at
   p is a sale of YES at 1 - p):

     half spread earned     s    = eps * (p - m)            m: mid just before the trade
     markout at horizon h   a(h) = eps * (m(t+h) - m)       what the mid then did against the maker
     maker edge             e(h) = s - a(h) = eps * (p - m(t+h))

   A market is worth quoting when e(h) stays positive at the horizon over which inventory is
   held. e(h) * shares traded is the whole pie the makers of that market shared that day; a small
   quoter gets a slice of it, and only the part of the flow that reaches its price.

B. The SIG Predictions Cup cycle, on the 30 days of trade history (`cycles`). The tournament's
   scan (data/turbo_scan_results.csv) rested a bid at 0.6x a contract's 7 day hourly median and
   sold at 1.5x the entry: 83 wins out of 84. Same replay here, per token, with the anchor
   computed from past hours only:

     anchor(t)  median of the hourly last prices over the 7 days before the hour of t
     bid        anchor * entry (or anchor - entry_cents); filled by a print strictly below it
     target     bid * gain (or bid + gain_cents);        sold by a print strictly above it

   A cycle still open at the end of the data is marked at the last price: on real money a
   contract can also go to zero, which the tournament's 83/84 never showed.
"""
import csv
import heapq
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean, median
from typing import Dict, List, Optional

from book_recorder.replay import Books
from book_recorder.trades import iter_trades

from .feed import load_markets, replay, utc_day
from .s1 import infer_tick

HORIZONS_S = (10, 60, 300, 1800)


# ============================================================ A. markouts on the books
class Acc:
    __slots__ = ("n", "shares", "usdc", "buys", "s", "e", "e_n", "e300", "e300_sq", "spread", "depth", "samples",
                 "mid", "first", "last")

    def __init__(self):
        self.n, self.shares, self.usdc, self.buys = 0, 0.0, 0.0, 0
        self.s = 0.0                                    # share-weighted half spread earned
        self.e = {h: 0.0 for h in HORIZONS_S}           # share-weighted maker edge
        self.e_n = {h: 0.0 for h in HORIZONS_S}         # shares with a mid at that horizon
        self.e300, self.e300_sq = [0.0, 0], 0.0         # unweighted, for a t statistic
        self.spread, self.depth, self.samples, self.mid = 0.0, 0.0, 0, 0.0
        self.first = self.last = None


class Markouts:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.books = Books()
        self.markets: Dict[str, dict] = {}
        self.acc: Dict[str, Acc] = defaultdict(Acc)
        self.heap: List[tuple] = []
        self.seq = 0
        self.now = 0
        self.next_sample: Dict[str, int] = {}
        self.days = 0

    def canonical(self, token: str):
        m = self.markets.get(token)
        if not m or len(m.get("tokens", [])) != 2:
            return token, 1
        first = m["tokens"][0]["token_id"]
        return first, (1 if first == token else -1)

    def on_event(self, ev: dict):
        e = ev.get("_e")
        if e is not None:
            if e == "day":
                self.days += 1
                self.markets.update(load_markets(self.data_dir, ev["day"]))
            elif e == "markets" and ev.get("_r"):
                self.markets.update(load_markets(self.data_dir, utc_day(ev["_r"])))
            return
        r = ev.get("_r")
        if r is None:
            return
        self.now = max(self.now, r)
        while self.heap and self.heap[0][0] <= self.now:
            _, _, canon, h, eps, px, shares = heapq.heappop(self.heap)
            b = self.books.get(canon)
            mid = b.mid() if b else None
            if mid is None:
                continue
            a = self.acc[canon]
            edge = eps * (px - mid)
            a.e[h] += edge * shares
            a.e_n[h] += shares
            if h == 300:
                a.e300[0] += edge
                a.e300[1] += 1
                a.e300_sq += edge * edge
        trade = self.books.apply(ev)
        kind = ev.get("event_type")
        if trade is not None:
            side = (trade.side or "").upper()
            if side not in ("BUY", "SELL") or trade.size <= 0:
                return
            canon, flip = self.canonical(trade.token)
            b = self.books.get(canon)
            mid = b.mid() if b else None
            if mid is None:
                return
            eps = (1 if side == "BUY" else -1) * flip
            px = trade.price if flip == 1 else 1 - trade.price
            a = self.acc[canon]
            a.n += 1
            a.shares += trade.size
            a.usdc += trade.price * trade.size
            a.buys += eps > 0
            a.s += eps * (px - mid) * trade.size
            a.first = a.first or self.now
            a.last = self.now
            for h in HORIZONS_S:
                self.seq += 1
                heapq.heappush(self.heap, (self.now + h * 1000, self.seq, canon, h, eps, px, trade.size))
        elif kind in ("book", "price_change"):
            toks = ([str(c["asset_id"]) for c in ev["price_changes"]] if "price_changes" in ev
                    else [str(ev.get("asset_id"))])
            for tok in dict.fromkeys(toks):
                if self.now >= self.next_sample.get(tok, 0) and self.canonical(tok)[0] == tok:
                    b = self.books.get(tok)
                    bid, ask = (b.best_bid(), b.best_ask()) if b else (None, None)
                    if bid is not None and ask is not None and ask > bid:
                        self.next_sample[tok] = self.now + 60000
                        a = self.acc[tok]
                        a.spread += ask - bid
                        a.depth += (b.bids[bid] * bid + b.asks[ask] * ask) / 2
                        a.mid += (bid + ask) / 2
                        a.samples += 1

    def rows(self, min_trades: int = 50) -> List[dict]:
        out = []
        days = max(self.days, 1)
        for canon, a in self.acc.items():
            if a.n < min_trades or not a.shares:
                continue
            m = self.markets.get(canon, {})
            b = self.books.get(canon)
            tick = (b.tick or infer_tick(list(b.bids)[:20] + list(b.asks)[:20])) if b else None
            n300 = a.e300[1]
            mu = a.e300[0] / n300 if n300 else None
            var = (a.e300_sq / n300 - mu * mu) if n300 > 1 else None
            row = {
                "slug": m.get("slug"), "event": m.get("event_slug"), "end_date": (m.get("end_date") or "")[:10],
                "token": canon, "tick": tick,
                "mid": round(a.mid / a.samples, 3) if a.samples else None,
                "trades_per_day": round(a.n / days), "usdc_per_day": round(a.usdc / days),
                "share_buys": round(a.buys / a.n, 2),
                "spread_c": round(100 * a.spread / a.samples, 2) if a.samples else None,
                "depth_best_usdc": round(a.depth / a.samples) if a.samples else None,
                "half_spread_earned_c": round(100 * a.s / a.shares, 3),
            }
            for h in HORIZONS_S:
                row[f"edge_{h}s_c"] = round(100 * a.e[h] / a.e_n[h], 3) if a.e_n[h] else None
            row["edge_300s_t"] = round(mu / (var / n300) ** 0.5, 1) if var and var > 0 else None
            e300 = a.e[300] / a.e_n[300] if a.e_n[300] else 0.0
            row["maker_pie_usdc_per_day"] = round(e300 * a.shares / days, 1)
            out.append(row)
        out.sort(key=lambda r: r["maker_pie_usdc_per_day"], reverse=True)
        return out


# ============================================================ A2. Market scan
SCAN = {"min_trades_per_day": 30, "min_usdc_per_day": 500, "min_mid": 0.10, "max_mid": 0.90, "min_spread_ticks": 2.0,
        "min_days_left": 3, "min_edge_300s_c": 0.0, "min_edge_1800s_c": 0.0, "min_t": 2.0, "per_event": 1, "top": 8}


def scan(rows: List[dict], today: str, **over) -> List[dict]:
    """Rank the markets of `rows` (Markouts.rows) for market making and say why each one is in or out.

    liquidity          trades and dollars per day, dollars resting at the best price
    room               the spread in ticks: with one tick there is nothing to earn inside
    adverse selection  what the price takes back after a fill: half spread earned minus the edge
                       left 5 minutes later (in cents per share). The edge must stay positive at 30 min.
    confidence         the t statistic of the 5 minute edge; the score is the edge minus one
                       standard error, so a noisy market ranks below a steady one
    event risk         days to the end date, mid away from 0 and 1, and one market per event
                       (markets of one event move together: the replay lost 16 $ that way)
    Measured on recorded days only: a few days give a noisy ranking, to be re-run as data grows."""
    c = {**SCAN, **over}
    unknown = set(over) - set(SCAN)
    if unknown:
        raise ValueError(f"unknown scan parameters: {sorted(unknown)}")
    from datetime import date
    out = []
    for r in rows:
        e300, e1800, t, tick = r.get("edge_300s_c"), r.get("edge_1800s_c"), r.get("edge_300s_t"), r.get("tick")
        left = None
        try:
            left = (date.fromisoformat(r["end_date"]) - date.fromisoformat(today)).days if r.get("end_date") else None
        except ValueError:
            pass
        ticks = (r["spread_c"] / 100 / tick) if r.get("spread_c") is not None and tick else None
        why = []
        if r["trades_per_day"] < c["min_trades_per_day"] or r["usdc_per_day"] < c["min_usdc_per_day"]:
            why.append("too little flow")
        if r.get("mid") is None or not c["min_mid"] <= r["mid"] <= c["max_mid"]:
            why.append("price too close to 0 or 1")
        if ticks is None or ticks < c["min_spread_ticks"]:
            why.append("spread too tight")
        if left is not None and left < c["min_days_left"]:
            why.append("ends too soon")
        if e300 is None or e300 <= c["min_edge_300s_c"]:
            why.append("no edge after 5 min")
        if e1800 is None or e1800 <= c["min_edge_1800s_c"]:
            why.append("edge gone after 30 min")
        if t is None or t < c["min_t"]:
            why.append("edge not significant")
        score = round(e300 * (1 - 1 / t), 3) if e300 and t and t > 1 else 0.0
        out.append({**r, "spread_ticks": round(ticks, 1) if ticks is not None else None, "days_left": left,
                    "adverse_selection_c": round(r["half_spread_earned_c"] - e300, 3) if e300 is not None else None,
                    "score": score, "rejected": "; ".join(why)})
    out.sort(key=lambda r: (bool(r["rejected"]), -r["score"]))
    seen, rank = {}, 0
    for r in out:
        if r["rejected"]:
            continue
        ev = r.get("event") or r["slug"]
        seen[ev] = seen.get(ev, 0) + 1
        if seen[ev] > c["per_event"]:
            r["rejected"] = "another market of the same event ranks higher"
        elif rank >= c["top"]:
            r["rejected"] = "below the top %d" % c["top"]
        else:
            rank += 1
            r["rank"] = rank
    out.sort(key=lambda r: (r.get("rank") is None, r.get("rank") or 0, -r["score"]))
    return out


# ============================================================ B. SIG cycles on the trade history
SPECS = (("x0.60_x1.50", "mult", 0.60, 1.50), ("x0.75_x1.50", "mult", 0.75, 1.50),
         ("x0.90_x1.15", "mult", 0.90, 1.15), ("-3c_+3c", "add", 0.03, 0.03), ("-5c_+5c", "add", 0.05, 0.05))


def token_prints(path) -> Dict[str, list]:
    """token -> [(ts, price, shares)] sorted by time, each print also mirrored on the other token."""
    raw = defaultdict(list)
    for t in iter_trades(path, dedupe=True):
        if 0 < t["price"] < 1:
            raw[t["token"]].append((t["ts"], t["price"], t["size"]))
    toks = list(raw)
    out = {}
    for tok in toks:
        rows = list(raw[tok])
        if len(toks) == 2:
            other = toks[1] if tok == toks[0] else toks[0]
            rows += [(ts, round(1 - p, 6), s) for ts, p, s in raw[other]]
        out[tok] = sorted(rows)
    return out


def scan_token(prints: list, anchor_days: float = 7.0, min_hours: int = 72) -> Dict[str, dict]:
    """Replay every cycle spec on one token's prints."""
    res = {name: {"cycles": 0, "wins": 0, "open": 0, "open_mark_ret": [], "hours": [], "ret": [], "capacity": []}
           for name, *_ in SPECS}
    if not prints:
        return res
    hourly: Dict[int, float] = {}
    state = {name: None for name, *_ in SPECS}          # None or {"bid","target","t","cap"}
    anchor, anchor_hour = None, None
    window = int(anchor_days * 24)
    for ts, p, size in prints:
        hour = int(ts // 3600)
        if hour != anchor_hour:
            past = [v for h, v in hourly.items() if hour - window <= h < hour]
            anchor = median(past) if len(past) >= min_hours else None
            anchor_hour = hour
        for name, kind, entry, gain in SPECS:
            st = state[name]
            if st is None:
                if anchor is None or not (0.02 <= anchor <= 0.98):
                    continue
                bid = anchor * entry if kind == "mult" else anchor - entry
                if bid < 0.01:
                    continue
                if p < bid - 1e-9:
                    target = min(bid * gain if kind == "mult" else bid + gain, 0.99)
                    state[name] = {"bid": bid, "target": target, "t": ts, "cap": size * bid}
            else:
                if p < st["bid"] - 1e-9 and ts - st["t"] < 3600:
                    st["cap"] += size * st["bid"]         # more could have been bought in the first hour
                if p > st["target"] + 1e-9:
                    r = res[name]
                    r["cycles"] += 1
                    r["wins"] += 1
                    r["hours"].append((ts - st["t"]) / 3600)
                    r["ret"].append(st["target"] / st["bid"] - 1)
                    r["capacity"].append(st["cap"])
                    state[name] = None
        hourly[hour] = p
    last = prints[-1][1]
    for name, *_ in SPECS:
        st = state[name]
        if st is not None:
            r = res[name]
            r["cycles"] += 1
            r["open"] += 1
            r["open_mark_ret"].append(last / st["bid"] - 1)
            r["ret"].append(last / st["bid"] - 1)
            r["capacity"].append(st["cap"])
    return res


def cycles(data_dir, slugs: Optional[Dict[str, str]] = None) -> dict:
    """Aggregate per spec, and the tokens where the SIG cycle would have run."""
    total = {name: {"tokens_with_cycles": 0, "cycles": 0, "wins": 0, "open": 0, "ret": [], "hours": [],
                    "open_mark_ret": [], "capacity": []} for name, *_ in SPECS}
    per_token = []
    files = sorted((Path(data_dir) / "trades").glob("*.jsonl.gz"))
    span = []
    for f in files:
        try:
            by_token = token_prints(f)
        except Exception as e:                          # one unreadable file must not stop the scan
            per_token.append({"market": f.name, "error": f"{type(e).__name__}: {e}"[:200]})
            continue
        for tok, prints in by_token.items():
            if len(prints) < 200:
                continue
            span.append((prints[-1][0] - prints[0][0]) / 86400)
            res = scan_token(prints)
            for name, r in res.items():
                if not r["cycles"]:
                    continue
                t = total[name]
                t["tokens_with_cycles"] += 1
                for k in ("cycles", "wins", "open"):
                    t[k] += r[k]
                for k in ("ret", "hours", "open_mark_ret", "capacity"):
                    t[k] += r[k]
                per_token.append({"market": f.name.split(".")[0], "slug": (slugs or {}).get(tok), "token": tok,
                                  "spec": name, "cycles": r["cycles"], "wins": r["wins"], "open": r["open"],
                                  "mean_ret": round(mean(r["ret"]), 4),
                                  "median_hours": round(median(r["hours"]), 1) if r["hours"] else None,
                                  "median_capacity_usdc": round(median(r["capacity"]), 1),
                                  "last_price": prints[-1][1]})
    summary = {}
    for name, t in total.items():
        summary[name] = {
            "tokens_with_cycles": t["tokens_with_cycles"], "cycles": t["cycles"], "wins": t["wins"], "open": t["open"],
            "win_rate": round(t["wins"] / t["cycles"], 3) if t["cycles"] else None,
            "mean_ret_per_cycle": round(mean(t["ret"]), 4) if t["ret"] else None,
            "median_ret_per_cycle": round(median(t["ret"]), 4) if t["ret"] else None,
            "mean_ret_of_open": round(mean(t["open_mark_ret"]), 4) if t["open_mark_ret"] else None,
            "worst_open": round(min(t["open_mark_ret"]), 4) if t["open_mark_ret"] else None,
            "median_hours_to_win": round(median(t["hours"]), 1) if t["hours"] else None,
            "median_capacity_usdc": round(median(t["capacity"]), 1) if t["capacity"] else None,
        }
    return {"files": len(files), "tokens_scanned": len(span),
            "median_history_days": round(median(span), 1) if span else None,
            "by_spec": summary, "per_token": per_token}


# ============================================================ run both
def run(data_dir, days: List[str], out_dir, progress=None) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    mk = Markouts(data_dir)
    n = 0
    for ev in replay(Path(data_dir) / "book", days):
        mk.on_event(ev)
        n += 1
        if progress and n % 2_000_000 == 0:
            progress(n, mk)
    rows = mk.rows()
    if rows:
        with open(out / "markouts.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    slugs = {tok: f"{m.get('slug')}|{m.get('outcome')}" for tok, m in mk.markets.items()}
    cyc = cycles(data_dir, slugs)
    per_token = cyc.pop("per_token")
    (out / "cycles_per_token.json").write_text(json.dumps(per_token, indent=0), encoding="utf-8")
    pos = [r for r in rows if r["maker_pie_usdc_per_day"] > 0 and (r["edge_300s_t"] or 0) >= 2
           and (r["edge_1800s_c"] or 0) > 0]
    tot_sh = sum(r["trades_per_day"] for r in rows) or 1
    best_cycles = sorted((r for r in per_token if r.get("spec") and r["cycles"] >= 2),
                         key=lambda r: (r["wins"] - r["open"], r["mean_ret"]), reverse=True)[:25]
    s = {"days": days, "events_read": n, "markets_with_50_trades": len(rows),
         "markets_maker_edge_positive_5min_t2_and_30min": len(pos),
         "all_markets_trade_weighted": {
             k: round(sum((r[k] or 0) * r["trades_per_day"] for r in rows) / tot_sh, 3)
             for k in ("spread_c", "half_spread_earned_c", "edge_10s_c", "edge_60s_c", "edge_300s_c", "edge_1800s_c")},
         "top_by_maker_pie": rows[:25], "bottom_by_maker_pie": rows[-8:],
         "cycles": cyc, "best_cycle_tokens": best_cycles}
    (out / "mm_summary.json").write_text(json.dumps(s, indent=1), encoding="utf-8")
    return s
