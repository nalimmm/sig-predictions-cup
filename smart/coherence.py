"""Price coherence between linked markets, measured on the recorded books (read only, no orders).

Two families of logical constraints, both checked on executable prices (best bid / best ask,
then deeper levels for the size):

1. Nested markets (`pair`). Same question, only the threshold or the deadline differs:
     "Will Bitcoin reach $120,000 by December 31?"  is at most as likely as  "... $110,000 by December 31?"
     "... reach $120,000 by November 30?"          is at most as likely as  "... $120,000 by December 31?"
   P(harder) <= P(easier). A violation is bid(harder YES) > ask(easier YES): sell the harder YES
   (buy its NO at 1 - bid), buy the easier YES. Every outcome pays at least 1 for a cost of
   1 + ask - bid, so the gain per share is bid - ask, before fees.
   Deadlines may differ only when both questions are cumulative ("by", "before"); "in November"
   against "in December" is not nested and is left out.

2. Mutually exclusive outcomes of a neg risk event (`basket`). At most one YES pays.
   - sell side, valid on any subset of the outcomes: sum of YES bids > 1. Buy every NO at
     1 - bid: k NO cost k - sum(bids) and at least k - 1 of them pay. Gain sum(bids) - 1.
   - buy side, valid only when every live outcome of the event is recorded: sum of YES asks < 1.
     Exactly one YES pays. Gain 1 - sum(asks). Completeness is checked on Gamma (--gamma), never
     assumed.

Fees: taker fee per share = rate * p * (1 - p), on every leg (Polymarket fee page, read
2026-11-22). The rate comes from Gamma's feeSchedule when --gamma is given and the field exists,
otherwise from --fee-rate (0.07 by default, the highest category: conservative).

A violation is followed as an episode, from the first book update that shows it to the first
that does not. What matters for a bot is not that it existed but whether it was still there
`latency` seconds later, with how many shares and how much money after fees: that is measured
too. A book is trusted only between a snapshot and the close of the connection that carried it,
so a dropped connection cannot leave a stale price that fakes a violation.

The families are matched from the question text. That matching is a heuristic: families.csv lists
every pair and basket used, to be read before believing any figure.
"""
import csv
import json
import re
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from book_recorder.replay import Books

from .feed import load_markets, replay, utc_day

GAMMA = "https://gamma-api.polymarket.com"

_MONEY = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)(?:\s*(k|m|b|t|thousand|million|billion|trillion))?\b", re.I)
_UNIT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "billion": 1e9, "t": 1e12, "trillion": 1e12}
_UP = re.compile(r"\b(reach|reaches|hit|hits|above|over|exceed|exceeds|greater than|more than|at least|"
                 r"higher than|surpass|surpasses)\b", re.I)
_DOWN = re.compile(r"\b(dip|dips|fall|falls|drop|drops|below|under|less than|lower than|sink|sinks)\b", re.I)
_MONTHS = ("january|february|march|april|may|june|july|august|september|october|november|december|"
           "jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec")
_DATE = re.compile(rf"\b(?:{_MONTHS})\b\.?(?:\s+\d{{1,2}}(?:st|nd|rd|th)?)?(?:,?\s+\d{{4}})?|\b20\d\d\b|"
                   r"\bend of (?:the )?year\b|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b", re.I)
_CUMUL = re.compile(r"\b(by|before)\b", re.I)


def parse_question(q: str) -> Optional[dict]:
    """Direction, threshold and template of a threshold question, or None if it is not one."""
    if not q:
        return None
    amounts = list(_MONEY.finditer(q))
    if len(amounts) != 1 or re.search(r"\bbetween\b", q, re.I):
        return None
    up, down = bool(_UP.search(q)), bool(_DOWN.search(q))
    if up == down:
        return None
    a = amounts[0]
    strike = float(a.group(1).replace(",", "")) * _UNIT.get((a.group(2) or "").lower(), 1.0)
    tmpl = q[:a.start()] + "<X>" + q[a.end():]
    tmpl = _DATE.sub("<D>", tmpl.lower())
    tmpl = re.sub(r"\s+", " ", re.sub(r"[?!.]", "", tmpl)).strip()
    return {"dir": "up" if up else "down", "strike": strike, "template": tmpl,
            "cumulative": bool(_CUMUL.search(q))}


def _end(m: dict) -> Optional[float]:
    v = m.get("end_date")
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


def _yes(m: dict) -> Optional[str]:
    toks = m.get("tokens") or []
    if len(toks) != 2:
        return None
    for t in toks:
        if str(t.get("outcome") or "").strip().lower() == "yes":
            return str(t["token_id"])
    return None


@dataclass
class Constraint:
    cid: str
    kind: str                    # "pair", "basket_sell", "basket_buy"
    legs: List[str]              # pair: [harder_yes, easier_yes]; basket: YES tokens
    label: str
    fee: Dict[str, float] = field(default_factory=dict)   # token -> taker rate


def families(markets: List[dict], complete_events: Optional[Dict[str, bool]] = None,
             hour_tol: float = 1.0) -> List[Constraint]:
    """Constraints between the given markets (one dict per market, recorder layout)."""
    complete_events = complete_events or {}
    by_key: Dict[tuple, List[tuple]] = defaultdict(list)
    events: Dict[str, List[tuple]] = defaultdict(list)
    seen = set()
    for m in markets:
        cond = m.get("condition_id")
        if cond in seen:
            continue
        seen.add(cond)
        yes = _yes(m)
        if yes is None:
            continue
        if m.get("neg_risk") and m.get("event_slug"):
            events[m["event_slug"]].append((yes, m))
        p = parse_question(m.get("question") or "")
        end = _end(m)
        if p and end is not None:
            by_key[(p["template"], p["dir"])].append((yes, m, p, end))
    out: List[Constraint] = []
    for (tmpl, d), rows in by_key.items():
        for i, (ya, ma, pa, ea) in enumerate(rows):
            for yb, mb, pb, eb in rows[i + 1:]:
                same_end = abs(ea - eb) <= hour_tol * 3600
                if not same_end and not (pa["cumulative"] and pb["cumulative"]):
                    continue
                sa, sb = pa["strike"], pb["strike"]
                # a is harder than b if its threshold is further away and its deadline not later
                harder_strike = (sa > sb) if d == "up" else (sa < sb)
                easier_strike = (sa < sb) if d == "up" else (sa > sb)
                if sa == sb and same_end:
                    continue
                if (harder_strike or sa == sb) and (ea <= eb or same_end):
                    h, e, mh, me = ya, yb, ma, mb
                elif (easier_strike or sa == sb) and (eb <= ea or same_end):
                    h, e, mh, me = yb, ya, mb, ma
                else:
                    continue            # higher threshold but later deadline: no order
                out.append(Constraint(f"pair:{mh.get('slug')}>{me.get('slug')}", "pair", [h, e],
                                      f"{mh.get('slug')}  <=  {me.get('slug')}"))
    for ev, rows in events.items():
        if len(rows) < 2:
            continue
        legs = [y for y, _ in rows]
        out.append(Constraint(f"basket_sell:{ev}", "basket_sell", legs, f"{ev} ({len(legs)} outcomes recorded)"))
        if complete_events.get(ev):
            out.append(Constraint(f"basket_buy:{ev}", "basket_buy", legs, f"{ev} (complete, {len(legs)} outcomes)"))
    return out


# ------------------------------------------------------------ executable value of a violation
def fee_per_share(rate: float, p: float) -> float:
    return rate * p * (1 - p)


def eval_pair(bh: List[Tuple[float, float]], ae: List[Tuple[float, float]], rh: float, re_: float):
    """bh: bids of the harder YES (best first), ae: asks of the easier YES (best first).
    Returns (gross gain per share at the top, net gain per share at the top, shares, net $)."""
    if not bh or not ae:
        return None
    gross = bh[0][0] - ae[0][0]
    net_top = gross - fee_per_share(rh, bh[0][0]) - fee_per_share(re_, ae[0][0])
    i = j = 0
    qb, qa = bh[0][1], ae[0][1]
    shares = money = 0.0
    while i < len(bh) and j < len(ae):
        b, a = bh[i][0], ae[j][0]
        unit = b - a - fee_per_share(rh, b) - fee_per_share(re_, a)
        if unit <= 0:
            break
        q = min(qb, qa)
        shares += q
        money += q * unit
        qb -= q
        qa -= q
        if qb <= 1e-9:
            i += 1
            qb = bh[i][1] if i < len(bh) else 0
        if qa <= 1e-9:
            j += 1
            qa = ae[j][1] if j < len(ae) else 0
    return gross, net_top, shares, money


def eval_basket(levels: List[List[Tuple[float, float]]], rates: List[float], sell: bool, max_steps: int = 50):
    """levels: per leg, YES bids (sell) or YES asks (buy), best first.
    sell: gain per set = sum(bids) - 1; buy: gain per set = 1 - sum(asks)."""
    if any(not lv for lv in levels):
        return None
    top = [lv[0][0] for lv in levels]
    gross = (sum(top) - 1) if sell else (1 - sum(top))
    net_top = gross - sum(fee_per_share(r, p) for r, p in zip(rates, top))
    idx = [0] * len(levels)
    left = [lv[0][1] for lv in levels]
    shares = money = 0.0
    for _ in range(max_steps):
        px = [levels[k][idx[k]][0] for k in range(len(levels))]
        unit = ((sum(px) - 1) if sell else (1 - sum(px))) - sum(fee_per_share(r, p) for r, p in zip(rates, px))
        if unit <= 0:
            break
        q = min(left)
        shares += q
        money += q * unit
        done = False
        for k in range(len(levels)):
            left[k] -= q
            if left[k] <= 1e-9:
                idx[k] += 1
                if idx[k] >= len(levels[k]):
                    done = True
                    break
                left[k] = levels[k][idx[k]][1]
        if done:
            break
    return gross, net_top, shares, money


# ------------------------------------------------------------ replay
@dataclass
class Episode:
    cid: str
    kind: str
    start: int
    end: Optional[int] = None
    max_gross: float = 0.0
    max_net_top: float = -1.0
    max_money: float = 0.0
    max_shares: float = 0.0
    updates: int = 0
    lat_net_top: Optional[float] = None        # measured `latency` after the start
    lat_money: Optional[float] = None
    lat_shares: Optional[float] = None


class Coherence:
    def __init__(self, constraints: List[Constraint], latency_ms: int = 2000, default_fee: float = 0.07,
                 fee_rates: Optional[Dict[str, float]] = None, depth: int = 10):
        self.cons = constraints
        self.latency_ms, self.depth = latency_ms, depth
        self.fee_rates, self.default_fee = fee_rates or {}, default_fee
        self.by_token: Dict[str, List[int]] = defaultdict(list)
        for k, c in enumerate(constraints):
            for t in c.legs:
                self.by_token[t].append(k)
        self.books = Books()
        self.valid: Dict[str, bool] = {}
        self.top: Dict[str, Tuple[Optional[float], Optional[float]]] = {}
        self.conn_tokens: Dict[str, set] = defaultdict(set)
        self.open: Dict[int, Episode] = {}
        self.pending: List[Tuple[int, Episode]] = []
        self.episodes: List[Episode] = []
        self.violated_ms: Dict[int, int] = defaultdict(int)
        self.observed_ms: Dict[int, int] = defaultdict(int)
        self.last_state: Dict[int, Tuple[int, bool, bool]] = {}   # k -> (since ms, observable, violated)
        self.now = 0
        self.first = None

    def rate(self, tok: str) -> float:
        return self.fee_rates.get(tok, self.default_fee)

    def _bids(self, t):
        b = self.books.get(t)
        return sorted(b.bids.items(), reverse=True)[:self.depth] if b else []

    def _asks(self, t):
        b = self.books.get(t)
        return sorted(b.asks.items())[:self.depth] if b else []

    def _refresh(self, t):
        b = self.books.get(t)
        self.top[t] = (b.best_bid(), b.best_ask()) if b is not None else (None, None)

    def _usable(self, t) -> bool:
        if not self.valid.get(t):
            return False
        bb, ba = self.top.get(t, (None, None))
        return not (bb is not None and ba is not None and bb >= ba)     # crossed = stale, distrust

    def evaluate(self, k: int):
        c = self.cons[k]
        if not all(self._usable(t) for t in c.legs):
            return None
        # fast path: most updates leave the constraint satisfied, decided on best prices alone
        if c.kind == "pair":
            h, e = c.legs
            bb, ba = self.top[h][0], self.top[e][1]
            if bb is None or ba is None:
                return None
            if bb - ba <= 1e-9:
                return (bb - ba, 0.0, 0.0, 0.0)
            return eval_pair(self._bids(h), self._asks(e), self.rate(h), self.rate(e))
        sell = c.kind == "basket_sell"
        tops = [self.top[t][0 if sell else 1] for t in c.legs]
        if any(x is None for x in tops):
            return None
        gross = (sum(tops) - 1) if sell else (1 - sum(tops))
        if gross <= 1e-9:
            return (gross, 0.0, 0.0, 0.0)
        levels = [self._bids(t) if sell else self._asks(t) for t in c.legs]
        return eval_basket(levels, [self.rate(t) for t in c.legs], sell)

    def _account(self, k: int, observable: bool, violated: bool):
        prev = self.last_state.get(k)
        if prev is not None:
            since, obs, vio = prev
            dt = max(0, self.now - since)
            if obs:
                self.observed_ms[k] += dt
            if vio:
                self.violated_ms[k] += dt
        self.last_state[k] = (self.now, observable, violated)

    def check(self, k: int):
        r = self.evaluate(k)
        violated = r is not None and r[0] > 1e-9
        self._account(k, r is not None, violated)
        ep = self.open.get(k)
        if violated:
            gross, net_top, shares, money = r
            if ep is None:
                ep = Episode(self.cons[k].cid, self.cons[k].kind, self.now)
                self.open[k] = ep
                self.pending.append((self.now + self.latency_ms, ep, k))
            ep.updates += 1
            ep.max_gross = max(ep.max_gross, gross)
            ep.max_net_top = max(ep.max_net_top, net_top)
            if money > ep.max_money:
                ep.max_money, ep.max_shares = money, shares
        elif ep is not None:
            ep.end = self.now
            self.episodes.append(self.open.pop(k))

    def _due(self):
        keep = []
        for due, ep, k in self.pending:
            if due > self.now:
                keep.append((due, ep, k))
                continue
            if self.open.get(k) is ep:
                r = self.evaluate(k)
                if r is not None and r[0] > 1e-9:
                    ep.lat_net_top, ep.lat_shares, ep.lat_money = r[1], r[2], r[3]
            if ep.lat_money is None:
                ep.lat_net_top, ep.lat_shares, ep.lat_money = None, 0.0, 0.0
        self.pending = keep

    def on_event(self, ev: dict):
        r = ev.get("_r")
        if r is None:
            return
        self.now = max(self.now, r)
        self.first = self.first or r
        if self.pending:
            self._due()
        e = ev.get("_e")
        if e is not None:
            if e == "close":
                for t in self.conn_tokens.pop(ev.get("_c"), ()):
                    self.valid[t] = False
                    for k in self.by_token.get(t, ()):
                        self.check(k)
            return
        kind = ev.get("event_type")
        touched = []
        if kind == "book":
            t = str(ev.get("asset_id"))
            touched.append(t)
        elif kind == "price_change":
            if "price_changes" in ev:
                touched.extend(str(c.get("asset_id")) for c in ev["price_changes"])
            else:
                touched.append(str(ev.get("asset_id")))
        self.books.apply(ev)
        if not touched:
            return
        c = ev.get("_c")
        for t in set(touched):
            if t not in self.by_token:
                continue
            self._refresh(t)
            if kind == "book":
                self.valid[t] = True
            if c:
                self.conn_tokens[c].add(t)
            for k in self.by_token.get(t, ()):
                self.check(k)

    def finish(self):
        for k, ep in list(self.open.items()):
            self._account(k, False, False)
            ep.end = self.now
            self.episodes.append(ep)
        self.open.clear()
        self.now += self.latency_ms + 1
        self._due()


# ------------------------------------------------------------ Gamma (optional, network)
def _get(url: str, timeout: float = 20.0):
    req = urllib.request.Request(url, headers={"User-Agent": "smart-coherence/1"})
    with urllib.request.urlopen(req, timeout=timeout) as f:
        return json.loads(f.read().decode("utf-8"))


def gamma_info(markets: List[dict], get=_get) -> Tuple[Dict[str, bool], Dict[str, float], dict]:
    """(event slug -> every live outcome recorded?, YES token -> taker fee rate, notes)."""
    complete, rates, notes = {}, {}, {"fee_from_gamma": 0, "events_checked": 0, "errors": []}
    recorded = defaultdict(set)
    for m in markets:
        if m.get("neg_risk") and m.get("event_slug"):
            recorded[m["event_slug"]].add(m.get("condition_id"))
    for ev, conds in recorded.items():
        try:
            rows = get(f"{GAMMA}/events?" + urllib.parse.urlencode({"slug": ev}))
            evo = rows[0] if isinstance(rows, list) and rows else rows
            live = {x.get("conditionId") for x in (evo or {}).get("markets", [])
                    if not x.get("closed") and x.get("active", True) is not False}
            complete[ev] = bool(live) and live <= conds
            notes["events_checked"] += 1
        except Exception as e:      # network or shape: stay conservative (not complete)
            complete[ev] = False
            notes["errors"].append(f"{ev}: {type(e).__name__}")
    for m in markets:
        yes = _yes(m)
        if yes is None or yes in rates:
            continue
        try:
            rows = get(f"{GAMMA}/markets?" + urllib.parse.urlencode({"condition_ids": m["condition_id"]}))
            raw = rows[0] if isinstance(rows, list) and rows else None
        except Exception:
            raw = None
        if not raw:
            continue
        fs = raw.get("feeSchedule")
        if isinstance(fs, str):
            try:
                fs = json.loads(fs)
            except ValueError:
                fs = None
        if raw.get("feesEnabled") is False:
            rates[yes] = 0.0
            notes["fee_from_gamma"] += 1
        elif isinstance(fs, dict) and fs.get("rate") is not None:
            try:
                rates[yes] = float(fs["rate"])
                notes["fee_from_gamma"] += 1
            except (TypeError, ValueError):
                pass
    return complete, rates, notes


# ------------------------------------------------------------ run
def _all_markets(data_dir, days: List[str]) -> List[dict]:
    seen, out = set(), []
    for d in days:
        for m in load_markets(data_dir, d).values():
            if m.get("condition_id") not in seen:
                seen.add(m["condition_id"])
                out.append(m)
    return out


def run(data_dir, days: List[str], out_dir, latency_s: float = 2.0, fee_rate: float = 0.07,
        use_gamma: bool = False, progress=None) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    markets = _all_markets(data_dir, days)
    complete, rates, notes = gamma_info(markets) if use_gamma else ({}, {}, {"gamma": "not used"})
    cons = families(markets, complete)
    slug = {}
    for m in markets:
        y = _yes(m)
        if y:
            slug[y] = m.get("slug")
    with open(out / "families.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["constraint", "kind", "legs", "fee_rates"])
        for c in cons:
            w.writerow([c.cid, c.kind, " | ".join(slug.get(t, t) for t in c.legs),
                        " ".join(f"{rates.get(t, fee_rate):g}" for t in c.legs)])
    co = Coherence(cons, latency_ms=int(latency_s * 1000), default_fee=fee_rate, fee_rates=rates)
    n = 0
    for ev in replay(Path(data_dir) / "book", days):
        co.on_event(ev)
        n += 1
        if progress and n % 2_000_000 == 0:
            progress(n, co)
    co.finish()
    eps = sorted(co.episodes, key=lambda e: e.start)
    with open(out / "episodes.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["constraint", "kind", "start_utc", "duration_s", "updates", "max_gross_c", "max_net_top_c",
                    "max_shares", "max_net_usd", "after_latency_net_top_c", "after_latency_shares",
                    "after_latency_net_usd"])
        for e in eps:
            w.writerow([e.cid, e.kind, datetime.fromtimestamp(e.start / 1000, timezone.utc).isoformat(timespec="seconds"),
                        round(((e.end or e.start) - e.start) / 1000, 1), e.updates, round(100 * e.max_gross, 2),
                        round(100 * e.max_net_top, 2), round(e.max_shares, 1), round(e.max_money, 4),
                        None if e.lat_net_top is None else round(100 * e.lat_net_top, 2),
                        round(e.lat_shares or 0, 1), round(e.lat_money or 0, 4)])
    per = defaultdict(lambda: {"episodes": 0, "still_there_after_latency": 0, "net_positive_after_latency": 0,
                               "usd_after_latency": 0.0, "usd_at_best": 0.0, "max_gross_c": 0.0})
    for e in eps:
        p = per[e.cid]
        p["episodes"] += 1
        p["max_gross_c"] = max(p["max_gross_c"], round(100 * e.max_gross, 2))
        p["usd_at_best"] += e.max_money
        if e.lat_net_top is not None:
            p["still_there_after_latency"] += 1
        if (e.lat_money or 0) > 0:
            p["net_positive_after_latency"] += 1
            p["usd_after_latency"] += e.lat_money
    for k, c in enumerate(co.cons):
        p = per[c.cid]
        obs = co.observed_ms.get(k, 0)
        p["kind"] = c.kind
        p["hours_observed"] = round(obs / 3.6e6, 2)
        p["share_of_time_violated"] = round(co.violated_ms.get(k, 0) / obs, 5) if obs else None
        p["usd_after_latency"] = round(p["usd_after_latency"], 2)
        p["usd_at_best"] = round(p["usd_at_best"], 2)
    kinds = defaultdict(lambda: defaultdict(float))
    for cid, p in per.items():
        kk = kinds[p.get("kind", cid.split(":")[0])]
        kk["constraints"] += 1
        for f_ in ("episodes", "still_there_after_latency", "net_positive_after_latency", "usd_after_latency", "usd_at_best"):
            kk[f_] += p[f_]
    hours = (co.now - (co.first or co.now)) / 3.6e6
    s = {"days": days, "events_read": n, "hours": round(hours, 2), "markets": len(markets),
         "latency_s": latency_s, "default_fee_rate": fee_rate, "gamma": notes,
         "by_kind": {k: {f_: round(v, 2) for f_, v in d.items()} for k, d in kinds.items()},
         "top_constraints": sorted(({"constraint": k, **v} for k, v in per.items()),
                                   key=lambda x: (-x["usd_after_latency"], -x["episodes"]))[:30],
         "reading": "usd_after_latency adds, for each episode, the net gain of taking every profitable share still "
                    "offered `latency` seconds after the violation appeared. It is an upper bound for one bot that "
                    "sees everything and is first in line; check families.csv before trusting any pair."}
    (out / "coherence_summary.json").write_text(json.dumps(s, indent=1), encoding="utf-8")
    return s
