"""What really happens after a sweep, measured on the recorded books, before any strategy runs.

Three questions, in the order the design note asks them:

1. Stylised facts, per market, in trade time (Bouchaud, Farmer, Lillo 2008, sections 4 to 6):
     C(l) = E[eps_n * eps_(n+l)]                 autocorrelation of the sign of aggressive trades
     R(l) = E[eps_n * (m_(n+l) - m_n)]           response function, m_n = mid when trade n arrives
   Both tokens of a binary market are folded onto the first one: a purchase of NO is a sale of
   YES. If C(l) has no long memory here, the thesis behind S1 and S2 falls.

2. After a sweep (the S1 trigger, with a loose threshold so events can be bucketed by size):
   where is the mid 10 s, 30 s, 1, 2, 5, 15, 30 and 60 minutes later, as a fraction of the fall?
   Split by what the flow did in the 5 minutes that followed, known only afterwards:
     "suivi"   more aggressive selling, net, for at least half the sweep's notional
     "contre"  net aggressive buying for at least half of it
     "isole"   neither
   The paper's own model says the average over all three is zero predictable return.

3. What could be earned: a passive bid one tick above the best bid, placed `latency` seconds after
   the signal, filled only if the market trades through it within 5 minutes, sold at half the fall
   recovered (passive, filled only on a bid through it) or into the bid after 30 minutes. No
   sizing, no caps: one share per signal, result in cents per share. The gap between question 2
   (all signals) and question 3 (filled orders only) is the adverse selection.

It also checks two assumptions the engine relies on: that `side` on last_trade_price is the
taker's side (a BUY should print at or above the mid), and whether the trade message arrives
before or after the book update it caused.
"""
import heapq
import json
import math
from collections import defaultdict
from pathlib import Path
from statistics import mean, median
from typing import Dict, List, Optional

from book_recorder.replay import Books

from .config import Config
from .feed import load_markets, replay, utc_day
from .s1 import S1Detector, infer_tick

LAGS = (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000)
HORIZONS_S = (10, 30, 60, 120, 300, 900, 1800, 3600)
LATENCIES_S = (2.0, 7.0)
FLOW_WINDOW_S = 300.0


class Study:
    def __init__(self, data_dir, cfg: Optional[Config] = None, events_path=None):
        self.cfg = cfg or Config()
        # loose trigger: any fall of min_move_ticks with a sweep; sigma only used to bucket
        loose = Config.from_dict({**self.cfg.to_dict(), "sigma_mult": 0.0})
        self.det = S1Detector(loose)
        self.data_dir = Path(data_dir)
        self.books = Books()
        self.markets: Dict[str, dict] = {}
        self.now = 0
        self.seq = 0
        self.heap: List[tuple] = []                 # (due ms, seq, kind, payload)
        self.events: List[dict] = []
        self.open_flow: Dict[str, List[dict]] = defaultdict(list)       # token -> events collecting flow
        self.trials: Dict[str, List[dict]] = defaultdict(list)          # token -> live paper orders
        self.series: Dict[str, list] = defaultdict(lambda: ([], []))    # market -> (signs, mids)
        self.side = {"buy_at_or_above_mid": 0, "buy_below_mid": 0, "sell_at_or_below_mid": 0,
                     "sell_above_mid": 0, "buy_at_or_above_ask": 0, "sell_at_or_below_bid": 0,
                     "trades": 0, "no_side": 0, "no_book": 0}
        self.n_events = 0
        self.gaps = 0
        self._out = open(events_path, "w", encoding="utf-8") if events_path else None

    # ------------------------------------------------------------ plumbing
    def push(self, due: int, kind: str, payload):
        self.seq += 1
        heapq.heappush(self.heap, (due, self.seq, kind, payload))

    def tick(self, token: str) -> float:
        b = self.books.get(token)
        if b is None:
            return 0.01
        return b.tick or infer_tick(list(b.bids)[:20] + list(b.asks)[:20])

    def canonical(self, token: str):
        """(first token of the market, +1 or -1): trades of the other token are mirrored."""
        m = self.markets.get(token)
        if not m or len(m.get("tokens", [])) != 2:
            return token, 1
        first = m["tokens"][0]["token_id"]
        return first, (1 if first == token else -1)

    # ------------------------------------------------------------ main loop
    def on_event(self, ev: dict):
        e = ev.get("_e")
        if e is not None:
            if e == "day":
                self.markets.update(load_markets(self.data_dir, ev["day"]))
            elif e == "markets" and ev.get("_r"):
                self.markets.update(load_markets(self.data_dir, utc_day(ev["_r"])))
            elif e == "close" and ev.get("why") != "stopped":
                self.gaps += 1
            return
        r = ev.get("_r")
        if r is None:
            return
        self.now = max(self.now, r)
        self.n_events += 1
        while self.heap and self.heap[0][0] <= self.now:
            due, _, kind, payload = heapq.heappop(self.heap)
            getattr(self, "_" + kind)(due, payload)
        kind = ev.get("event_type")
        if kind == "last_trade_price":
            tok = str(ev["asset_id"])
            b = self.books.get(tok)
            pre = (b.best_bid(), b.best_ask()) if b else (None, None)
            trade = self.books.apply(ev)
            if trade is not None:
                self.on_trade(trade, pre)
            return
        self.books.apply(ev)
        if kind in ("book", "price_change"):
            toks = ([str(c["asset_id"]) for c in ev["price_changes"]] if "price_changes" in ev
                    else [str(ev.get("asset_id"))])
            for tok in dict.fromkeys(toks):
                self.on_book(tok)

    # ------------------------------------------------------------ trades
    def on_trade(self, t, pre):
        s = self.side
        s["trades"] += 1
        side = (t.side or "").upper()
        bid, ask = pre
        if side not in ("BUY", "SELL"):
            s["no_side"] += 1
            return
        if bid is None or ask is None or ask <= bid:
            s["no_book"] += 1
        else:
            mid = (bid + ask) / 2
            if side == "BUY":
                s["buy_at_or_above_mid" if t.price >= mid - 1e-9 else "buy_below_mid"] += 1
                s["buy_at_or_above_ask"] += t.price >= ask - 1e-9
            else:
                s["sell_at_or_below_mid" if t.price <= mid + 1e-9 else "sell_above_mid"] += 1
                s["sell_at_or_below_bid"] += t.price <= bid + 1e-9
        comp = self.markets.get(t.token, {}).get("complement")
        self.det.on_trade(self.now, t.token, t.price, t.size, side, comp)
        # stylised facts on the market's first token
        canon, flip = self.canonical(t.token)
        cb = self.books.get(canon)
        cmid = cb.mid() if cb else None
        if cmid is not None:
            signs, mids = self.series[canon]
            signs.append((1 if side == "BUY" else -1) * flip)
            mids.append(cmid)
        # flow after a sweep, and paper fills: as seen from each token of the market
        usdc = t.price * t.size
        for tok, sgn, px in ((t.token, 1 if side == "BUY" else -1, t.price),
                             (comp, -1 if side == "BUY" else 1, round(1 - t.price, 6))):
            if not tok:
                continue
            for evr in self.open_flow.get(tok, ()):
                evr["flow_after"] += sgn * usdc
            if sgn < 0:
                for tr in list(self.trials.get(tok, ())):
                    if tr["state"] == "resting" and px < tr["price"] - 1e-9:
                        self._filled(tr)

    # ------------------------------------------------------------ books
    def on_book(self, token: str):
        b = self.books.get(token)
        if b is None:
            return
        bid, ask = b.best_bid(), b.best_ask()
        if bid is None or ask is None or ask <= bid:
            return
        for tr in list(self.trials.get(token, ())):
            if tr["state"] == "resting" and ask < tr["price"] - 1e-9:
                self._filled(tr)
            elif tr["state"] == "long" and bid > tr["target"] + 1e-9:
                self._closed(tr, tr["target"], "target")
        tick = self.tick(token)
        sig = self.det.on_mid(self.now, token, (bid + ask) / 2, bid, ask, tick)
        if sig is None:
            return
        m = self.markets.get(token, {})
        hours_left = None
        if m.get("end_date"):
            try:
                from datetime import datetime
                hours_left = round((datetime.fromisoformat(m["end_date"].replace("Z", "+00:00")).timestamp()
                                    - self.now / 1000) / 3600, 1)
            except ValueError:
                pass
        evr = {"t": sig.t, "token": token, "slug": m.get("slug"), "outcome": m.get("outcome"),
               "peak": sig.peak_mid, "mid": sig.mid, "drop": round(sig.drop, 5), "sigma": round(sig.sigma, 5),
               "drop_sigmas": round(sig.drop_sigmas, 2), "levels": sig.levels, "sweep_usdc": sig.sweep_usdc,
               "bid": bid, "ask": ask, "tick": tick, "hours_left": hours_left,
               "volume_24h": m.get("volume_24h"), "flow_after": 0.0, "mids": {}, "trials": {}}
        self.events.append(evr)
        self.open_flow[token].append(evr)
        self.push(self.now + int(FLOW_WINDOW_S * 1000), "flow_end", evr)
        for h in HORIZONS_S:
            self.push(self.now + h * 1000, "horizon", (evr, h))
        for lat in LATENCIES_S:
            self.push(self.now + int(lat * 1000), "place", (evr, lat))

    # ------------------------------------------------------------ timers
    def _flow_end(self, due, evr):
        self.open_flow[evr["token"]].remove(evr)

    def _horizon(self, due, payload):
        evr, h = payload
        b = self.books.get(evr["token"])
        evr["mids"][str(h)] = b.mid() if b else None
        if h == HORIZONS_S[-1] and self._out:
            self._out.write(json.dumps(evr, separators=(",", ":")) + "\n")

    def _place(self, due, payload):
        evr, lat = payload
        tok, key = evr["token"], str(lat)
        b = self.books.get(tok)
        bid, ask = (b.best_bid(), b.best_ask()) if b else (None, None)
        if bid is None or ask is None or ask <= bid:
            evr["trials"][key] = {"state": "no_book"}
            return
        mid, tick = (bid + ask) / 2, evr["tick"]
        if mid >= evr["peak"] - (1 - self.cfg.revert_frac) * evr["drop"]:
            evr["trials"][key] = {"state": "already_reverted"}
            return
        price = round(bid + tick, 6) if bid + tick < ask - 1e-9 else bid
        target = round(max(math.ceil((mid + self.cfg.revert_frac * evr["drop"]) / tick - 1e-9) * tick, price + tick), 6)
        tr = {"state": "resting", "price": price, "target": target, "token": tok, "placed": due,
              "spread": round(ask - bid, 6)}
        evr["trials"][key] = tr
        self.trials[tok].append(tr)
        self.push(due + int(self.cfg.entry_ttl_s * 1000), "ttl", tr)

    def _ttl(self, due, tr):
        if tr["state"] == "resting":
            tr["state"] = "unfilled"
            self.trials[tr["token"]].remove(tr)

    def _filled(self, tr):
        tr["state"], tr["fill_t"] = "long", self.now
        self.push(self.now + int(self.cfg.hold_s * 1000), "time_exit", tr)

    def _time_exit(self, due, tr):
        if tr["state"] != "long":
            return
        b = self.books.get(tr["token"])
        bid = b.best_bid() if b else None
        self._closed(tr, bid if bid is not None else 0.0, "time")

    def _closed(self, tr, px, how):
        tr["state"], tr["exit"], tr["how"] = "closed", px, how
        tr["pnl"] = round(px - tr["price"], 6)
        tr["held_s"] = round((self.now - tr["fill_t"]) / 1000, 1)
        self.trials[tr["token"]].remove(tr)

    def close(self):
        if self._out:
            done = HORIZONS_S[-1]
            for evr in self.events:                     # events whose last horizon never came
                if str(done) not in evr["mids"]:
                    self._out.write(json.dumps(evr, separators=(",", ":"), default=str) + "\n")
            self._out.close()

    # ------------------------------------------------------------ results
    def stylised(self, min_trades: int = 200) -> dict:
        num_c, num_r, cnt = defaultdict(float), defaultdict(float), defaultdict(int)
        markets, trades, buys = 0, 0, 0
        per_market_c1 = []
        for canon, (signs, mids) in self.series.items():
            n = len(signs)
            if n < min_trades:
                continue
            markets += 1
            trades += n
            buys += sum(1 for s in signs if s > 0)
            mu = sum(signs) / n
            for l in LAGS:
                if l >= n:
                    break
                c = sum((signs[i] - mu) * (signs[i + l] - mu) for i in range(n - l))
                rr = sum(signs[i] * (mids[i + l] - mids[i]) for i in range(n - l))
                num_c[l] += c
                num_r[l] += rr
                cnt[l] += n - l
                if l == 1:
                    per_market_c1.append(c / (n - l))
        out = {"markets": markets, "trades": trades, "share_buys": round(buys / trades, 3) if trades else None,
               "median_C1_per_market": round(median(per_market_c1), 4) if per_market_c1 else None,
               "C": {}, "R_cents": {}}
        for l in LAGS:
            if cnt[l]:
                out["C"][l] = round(num_c[l] / cnt[l], 4)
                out["R_cents"][l] = round(100 * num_r[l] / cnt[l], 4)
        xs = [(math.log(l), math.log(out["C"][l])) for l in LAGS if out["C"].get(l, 0) > 0 and l >= 2]
        if len(xs) >= 4:                                # C(l) ~ l^-gamma: OLS of log C on log l
            mx, my = mean(x for x, _ in xs), mean(y for _, y in xs)
            sxx = sum((x - mx) ** 2 for x, _ in xs)
            slope = sum((x - mx) * (y - my) for x, y in xs) / sxx
            out["gamma"] = round(-slope, 3)
            out["gamma_fit_lags"] = len(xs)
        return out

    def summary(self) -> dict:
        thr = 0.5

        def kind(e):
            f = e["flow_after"]
            if f <= -thr * e["sweep_usdc"]:
                return "suivi"
            if f >= thr * e["sweep_usdc"]:
                return "contre"
            return "isole"

        def reverted(rows, h):
            xs = [(e["mids"][str(h)] - e["mid"]) / e["drop"] for e in rows
                  if e["mids"].get(str(h)) is not None and e["drop"] > 0]
            if not xs:
                return None
            sd = (sum((x - mean(xs)) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5 if len(xs) > 1 else None
            return {"n": len(xs), "mean": round(mean(xs), 3), "median": round(median(xs), 3),
                    "t": round(mean(xs) / (sd / len(xs) ** 0.5), 2) if sd else None}

        def trials(rows, lat):
            ts = [e["trials"].get(str(lat)) for e in rows]
            ts = [t for t in ts if t]
            states = defaultdict(int)
            for t in ts:
                states[t["state"] if t["state"] != "closed" else "closed_" + t["how"]] += 1
            closed = [t for t in ts if t["state"] == "closed"]
            pn = [100 * t["pnl"] for t in closed]
            sd = (sum((x - mean(pn)) ** 2 for x in pn) / (len(pn) - 1)) ** 0.5 if len(pn) > 1 else None
            return {"signals": len(ts), "states": dict(states), "filled": len(closed),
                    "mean_pnl_cents": round(mean(pn), 3) if pn else None,
                    "median_pnl_cents": round(median(pn), 3) if pn else None,
                    "t": round(mean(pn) / (sd / len(pn) ** 0.5), 2) if sd else None,
                    "win_rate": round(sum(1 for x in pn if x > 0) / len(pn), 3) if pn else None,
                    "mean_spread_cents": round(100 * mean(t["spread"] for t in closed), 3) if closed else None}

        def block(rows):
            return {"n": len(rows),
                    "mean_drop_cents": round(100 * mean(e["drop"] for e in rows), 2) if rows else None,
                    "mean_spread_cents": round(100 * mean(e["ask"] - e["bid"] for e in rows), 2) if rows else None,
                    "reverted": {f"{h}s": reverted(rows, h) for h in HORIZONS_S},
                    "paper": {f"latence_{lat:g}s": trials(rows, lat) for lat in LATENCIES_S}}

        ev = self.events
        cfg = self.cfg
        s1 = [e for e in ev if e["drop_sigmas"] > cfg.sigma_mult]
        clean = [e for e in s1 if cfg.min_price <= e["bid"] and e["ask"] <= cfg.max_price
                 and (e["hours_left"] is None or e["hours_left"] >= cfg.min_hours_left)
                 and e["ask"] - e["bid"] <= cfg.max_spread_ticks * e["tick"] + 1e-9]
        st = self.side
        sided = st["trades"] - st["no_side"] - st["no_book"]
        return {
            "events_read": self.n_events, "disconnections": self.gaps,
            "side_check": {**st,
                           "consistent_share": round((st["buy_at_or_above_mid"] + st["sell_at_or_below_mid"]) / sided, 4)
                           if sided else None,
                           "trade_seen_before_book_update_share":
                               round((st["buy_at_or_above_ask"] + st["sell_at_or_below_bid"]) / sided, 4) if sided else None},
            "stylised": self.stylised(),
            "sweeps_all": block(ev),
            "sweeps_s1_threshold": block(s1),
            "sweeps_s1_with_filters": block(clean),
            "by_flow_after_(s1_with_filters)": {k: block([e for e in clean if kind(e) == k])
                                                for k in ("isole", "suivi", "contre")},
            "by_drop_sigmas": {name: block([e for e in ev if lo <= e["drop_sigmas"] < hi])
                               for name, lo, hi in (("<3", 0, 3), ("3-5", 3, 5), ("5-10", 5, 10), (">10", 10, 1e9))},
        }


def run(data_dir, days: List[str], out_dir, cfg: Optional[Config] = None, progress=None) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    st = Study(data_dir, cfg, events_path=out / "sweeps.jsonl")
    for ev in replay(Path(data_dir) / "book", days):
        st.on_event(ev)
        if progress and st.n_events % 2_000_000 == 0 and st.n_events:
            progress(st)
    st.close()
    s = st.summary()
    s["days"] = days
    (out / "summary.json").write_text(json.dumps(s, indent=1), encoding="utf-8")
    return s
