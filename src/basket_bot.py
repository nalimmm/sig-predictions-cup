"""Basket bot: the whole account in a basket of 8 same-side contracts, with laddered exits at 2x to 5x.

This is the strategy that ran the final week of the SIG Predictions Cup (virtual money), including
election night. Every 60 seconds, stateless (all state is re-read from the API, so the bot can be
stopped and restarted at any time without losing or doubling a position):

  - CLEANUP: any position outside the basket (inherited from earlier strategies) is sold to the best
    bidder to free up capital; any buy order outside the basket is cancelled.
  - BUYS in tranches, slippage free: for each basket contract not yet at budget, read the order book
    and take ONLY the quantity shown at the best offer, with a limit order at that price. Never above
    PRICE_CAP = reference price plus 10 percent: if the market runs away, do not chase it.
    Per-contract budget = account value / number of contracts, recomputed every round.
  - LADDERED SELLS posted as soon as a position exists: half at 2x the average cost, half at 3x
    (5x if the average cost is below 0.20), capped at 0.95. Every spike before election day is
    monetized automatically; no holding to resolution once a target prints. From DAY_BUYS_END
    onward no leg is posted below NIGHT_SELL_FLOOR (0.70): during the count we sell confirmation,
    not mid river.
  - GUARD from DAY_BUYS_END onward: RESCUE: if the best bid drops below the greater of 50 percent of
    the average cost and the absolute floor of 0.10, the whole position is sold at market instead of
    riding to zero.
  - ELECTION NIGHT ROLLING (from COUNT_START, while votes are being counted): day buys are off; the
    cash from every sale is continuously reinvested into basket contracts still below NIGHT_PRICE_CAP,
    below NIGHT_MOMENTUM_MAX times their pre-count price, and whose bid holds at NIGHT_DROP_GUARD of
    that price (never roll into a race that is losing), cheapest first, at most NIGHT_MAX_SHARE of the
    account per contract. Last night buy at NIGHT_BUYS_END; the market freezes at MARKET_FREEZE.
  - PRE-COUNT REFERENCE, frozen: between DAY_BUYS_END and COUNT_START every round snapshots the mid
    quote of each basket contract and writes it to PRECOUNT_FILE. At COUNT_START the snapshot stops
    and the reference is frozen for the rest of the night, including across restarts. Night filters
    never read a price observed after the count started.

Usage:
  python3 basket_bot.py          -> simulation: one round, no orders are sent
  python3 basket_bot.py --live   -> trade for real
  python3 basket_bot.py --stop   -> cancel ALL open orders on the account

Requires the SUPERMARKET_API_KEY environment variable (the tournament API key).
"""
import json
import math
import os
import sys
import time
import uuid
from datetime import datetime, timezone

import requests

API_KEY = os.environ.get("SUPERMARKET_API_KEY")
BASE = os.environ.get("SUPERMARKET_BASE", "https://sig.thesuper.market/api/v1")
SLUG = "midterm-elections"

# Democratic-side basket: exact market title -> reference price (mid quote recorded on Oct 24, 2026)
BASKET = {
    "Will the Democratic Party win the Iowa Senate?": 0.438,
    "Will the Democratic Party win the Kansas Senate?": 0.417,
    "Will the Democratic Party win the South Carolina Senate?": 0.232,
    "Will the Democratic Party win the Texas Governor?": 0.297,
    "Will the Democratic Party win the Florida Governor?": 0.203,
    "Will the Democratic Party win the Kansas Governor?": 0.253,
    "Will the Democratic Party win the Vermont Governor?": 0.263,
    "Will the Independent Party win the Nebraska Senate?": 0.268,
}
PRICE_CAP = 1.10                  # never buy above reference x 1.10 during the build phase
CASH_BUFFER = 0.02                # keep 2 percent of the account as a cushion
HIGH_LEG_BELOW = 0.20             # average cost below 0.20: the second half targets 5x instead of 3x
RESCUE_FRACTION = 0.50            # full sale if the bid drops below 50 percent of the average cost
RESCUE_FLOOR = 0.10               # full sale if the bid drops below this absolute price, whatever the cost
DAY_BUYS_END = "2026-11-03T23:00:00Z"   # guard (rescue) armed from here
COUNT_START = "2026-11-04T00:00:00Z"    # vote counting starts: night rolling armed
NIGHT_BUYS_END = "2026-11-04T16:00:00Z"  # last night buy one hour before the freeze
MARKET_FREEZE = "2026-11-04T17:00:00Z"
NIGHT_PRICE_CAP = 0.80            # night: never buy above this (remaining upside too small)
NIGHT_MOMENTUM_MAX = 1.5          # night: never buy above 1.5x the pre-count price
NIGHT_DROP_GUARD = 0.80           # night: no buy if the bid sits below 80 percent of the pre-count price
NIGHT_MAX_SHARE = 0.35            # night: cost per contract capped at 35 percent of the account
NIGHT_SELL_FLOOR = 0.70           # from DAY_BUYS_END: no laddered sell leg below this price
PRECOUNT_FILE = "precount_refs.json"  # frozen pre-count references, survives restarts
ROUND_SECONDS, IDS_REFRESH = 60, 3600

LIVE = "--live" in sys.argv
STOP = "--stop" in sys.argv
TICK = 0.005

session = requests.Session()
session.headers["Authorization"] = f"Bearer {API_KEY}"
names, basket_ids, ids_at, writes, precount_refs = {}, {}, 0.0, 0, {}


def log(msg):
    line = f"{datetime.now():%d/%m %H:%M:%S}  {msg}"
    print(line, flush=True)
    with open("basket_bot.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def now():
    return datetime.now(timezone.utc)


def parse(ts):
    return datetime.fromisoformat(ts[:19] + "+00:00")


def call(method, path, **kw):
    global writes
    for _ in range(3):
        try:
            r = session.request(method, BASE + path, timeout=30, **kw)
        except requests.RequestException as e:
            log(f"Network error ({e}), retrying in 30 s")
            time.sleep(30)
            continue
        if method != "GET":
            writes += 1
        time.sleep(0.7 if method == "GET" else 2.1)  # API limits: 100 reads and 30 writes per minute
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", 60)))
            continue
        return r
    return None


def why(r):
    return "no response" if r is None else f"{r.status_code} {r.text[:300]}"


def get(path, **params):
    r = call("GET", path, params={k: v for k, v in params.items() if v is not None})
    if r is None or not r.ok:
        raise RuntimeError(f"GET {path}: {why(r)}")
    return r.json()


def tick_down(p):
    return round(max(TICK, min(0.995, math.floor(round(p / TICK, 6)) * TICK)), 3)


def tick_up(p):
    return round(max(TICK, min(0.995, math.ceil(round(p / TICK, 6)) * TICK)), 3)


def same(a, b):
    return abs((a or 0) - b) < 1e-9


def name(ex):
    return names.get(ex, ex).replace("Will the ", "").replace(" win the", "").rstrip("?")[:55]


def place(tid, ex, action, qty, price, side="yes"):
    desc = f"{action.upper():4} {side.upper():3} {qty:>9,} @ {price:.3f}  {name(ex)}"
    if not LIVE:
        log(f"[simulation] {desc}")
        return
    body = {"exchangeId": ex, "side": side, "action": action, "quantity": int(qty), "price": round(price, 3),
            "tournamentId": tid, "idempotencyKey": str(uuid.uuid4())}
    r = call("POST", "/orders", json=body)
    if r is not None and r.ok:
        log(f"OK   {desc}  (filled immediately: {r.json().get('quantityTraded', 0):,.0f})")
    else:
        log(f"FAIL {desc}  {why(r)}")


def cancel(o, reason):
    desc = f"cancel {o['action']} {o['side']} {o['quantity']:,.0f} @ {o['priceLimit']}  {name(o['exchangeId'])} ({reason})"
    if not LIVE:
        log(f"[simulation] {desc}")
        return
    r = call("DELETE", f"/orders/{o['id']}")  # order by order: never a blanket cancel-all endpoint
    log(f"{'OK  ' if r is not None and r.ok else 'FAIL'} {desc}" + ("" if r is not None and r.ok else f"  {why(r)}"))


def resolve_ids():
    """Market title -> exchangeId, plus names for every contract (used by the cleanup sales)."""
    global basket_ids, ids_at
    if time.time() - ids_at < IDS_REFRESH and basket_ids:
        return
    cursor = None
    while True:
        page = get(f"/tournaments/{SLUG}/markets", status="open", limit=100, cursor=cursor)
        for m in page["data"]:
            for ex in m["exchanges"]:
                names[ex["id"]] = m["title"]
                if m["title"] in BASKET:
                    basket_ids[ex["id"]] = BASKET[m["title"]]
        if not page["pagination"]["hasMore"]:
            break
        cursor = page["pagination"]["nextCursor"]
    ids_at = time.time()
    missing = set(BASKET) - {names[ex] for ex in basket_ids}
    if missing:
        log(f"WARNING: basket contracts not found: {missing}")
    log(f"Basket: {len(basket_ids)}/{len(BASKET)} contracts resolved")


def load_precount_refs():
    """Reload the frozen references written before the count (a restart must not recompute them)."""
    try:
        with open(PRECOUNT_FILE, encoding="utf-8") as f:
            precount_refs.update({k: float(v) for k, v in json.load(f).items() if v})
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        log(f"WARNING: {PRECOUNT_FILE} unreadable ({e}), pre-count references start empty")


def save_precount_refs():
    tmp = PRECOUNT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(precount_refs, f, indent=1, sort_keys=True)
    os.replace(tmp, PRECOUNT_FILE)  # atomic: a crash never leaves a half written file


def snapshot_precount(prices):
    """Before COUNT_START only: record the current mid of every basket contract as its pre-count price.

    Called every round of the last pre-count hour, so the value frozen at COUNT_START is the last
    quote seen before the first vote is counted (at most one round old)."""
    changed = False
    for ex in basket_ids:
        bid, ask = prices.get(ex, (None, None))
        mid = (bid + ask) / 2 if bid and ask else (bid or None)
        if mid and not same(precount_refs.get(ex), mid):
            precount_refs[ex] = round(mid, 4)
            changed = True
    if changed:
        save_precount_refs()


def precount_ref(tid, ex):
    """The frozen pre-count price. Never recomputed from prices observed after COUNT_START.

    Normal path: the snapshot taken before the count (memory, or PRECOUNT_FILE after a restart).
    Fallback, only if the bot was down for the whole last pre-count hour: the close of the last
    hourly candle that ENDED by COUNT_START (a candle still open at the count would mix in counted
    prices). The fallback is frozen and saved the first time it succeeds; a failure is not cached."""
    if precount_refs.get(ex):
        return precount_refs[ex]
    try:
        h = get(f"/exchanges/{ex}/price-history", tournamentId=tid, resolution="1h",
                **{"from": "2026-11-03T16:00:00Z"}, limit=12)
    except RuntimeError as e:
        log(f"Pre-count reference for {name(ex)} unreadable: {e}")
        return None
    count = parse(COUNT_START)
    closes = [(parse(c["time"]), c["close"]) for c in h["candles"]
              if c["close"] is not None and parse(c["time"]).timestamp() + 3600 <= count.timestamp()]
    if not closes:
        log(f"Pre-count reference for {name(ex)}: no complete candle before the count, no night buy")
        return None
    precount_refs[ex] = max(closes)[1]
    log(f"WARNING: no pre-count snapshot for {name(ex)}, frozen from the hourly history at {precount_refs[ex]:.3f}")
    save_precount_refs()
    return precount_refs[ex]


def quotes(tid, ids):
    out = {}
    ids = list(ids)
    for i in range(0, len(ids), 100):
        for q in get("/exchanges/prices", ids=",".join(ids[i:i + 100]), tournamentId=tid)["data"]:
            out[q["exchangeId"]] = (q.get("bestBid"), q.get("bestAsk"))
    return out


def dump_at_best_bid(tid, ex, side, sells, qty, price):
    """Immediate sale to the best bidder (positions outside the basket, or a rescue)."""
    keep = [o for o in sells if same(o["priceLimit"], price)]
    for o in sells:
        if o not in keep:
            cancel(o, "selling to the best bidder")
    left = qty - sum(o["quantity"] for o in keep)
    if left > 0:
        place(tid, ex, "sell", left, price, side=side)


def sell_legs(avg, floor=0.0):
    """Laddered sell prices: 2x and 3x (5x if avg below 0.20) the average cost, raised to `floor`,
    capped at 0.95."""
    leg_low = tick_up(min(0.95, max(floor, 2 * avg)))
    leg_high = tick_up(min(0.95, max(floor, (5 if avg < HIGH_LEG_BELOW else 3) * avg)))
    return leg_low, leg_high


def laddered_sells(tid, ex, sells, q, avg, floor=0.0):
    """Half at the low leg, half at the high leg (see sell_legs). Reuses orders already at the right price."""
    leg_low, leg_high = sell_legs(avg, floor)
    q_low = q // 2
    wanted = [(leg_low, q_low), (leg_high, q - q_low)] if leg_high > leg_low else [(leg_low, q)]
    keep, drop = [], []
    for o in sells:
        target = next((w for w in wanted if same(o["priceLimit"], w[0])), None)
        already = sum(k["quantity"] for k in keep if same(k["priceLimit"], o["priceLimit"]))
        if target and already + o["quantity"] <= target[1] + 1e-9:
            keep.append(o)
        else:
            drop.append(o)
    for o in drop:
        cancel(o, "sell target recomputed")
    for price, qty in wanted:
        already = sum(k["quantity"] for k in keep if same(k["priceLimit"], price))
        if qty - already > 0:
            place(tid, ex, "sell", qty - already, price)


def one_round(tid):
    global writes
    writes = 0
    resolve_ids()
    t = now()
    day = t < parse(DAY_BUYS_END)
    guard = t >= parse(DAY_BUYS_END)  # rescue and floor armed as soon as day buys end
    night = t >= parse(COUNT_START)
    night_buys = night and t < parse(NIGHT_BUYS_END)

    orders, cursor = [], None
    while True:
        params = {"tournamentId": tid, "status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        page = get("/orders", **params)
        orders += page["data"]
        if not page["pagination"]["hasMore"]:
            break
        cursor = page["pagination"]["nextCursor"]
    pos = {p["exchangeId"]: p for p in get(f"/tournaments/{SLUG}/portfolio/positions")["positions"]
           if not p["settled"] and p["quantity"]}
    equity = get(f"/tournaments/{SLUG}/portfolio/pnl", period="all")["totalAccountValue"]
    # Spendable cash is equity minus the MARKET value of positions: unrealized gains are not cash.
    cash = equity - sum(p["marketValue"] for p in pos.values())
    managed = set(basket_ids) | set(pos)
    prices = quotes(tid, managed) if managed else {}
    if guard and not night:
        snapshot_precount(prices)  # last pre-count hour: keep the reference fresh until it freezes
    sell_floor = NIGHT_SELL_FLOOR if guard else 0.0

    # 1. Positions: basket -> laddered sells (or rescue once the guard is armed); outside -> dump
    for ex in managed:
        p = pos.get(ex)
        q = int(p["quantity"]) if p else 0
        bid, ask = prices.get(ex, (None, None))
        mine = [o for o in orders if o["exchangeId"] == ex and o["action"] == "sell"]
        yes_s = [o for o in mine if o["side"] == "yes"]
        no_s = [o for o in mine if o["side"] == "no"]
        if sum(o["quantity"] for o in yes_s) > max(q, 0):
            for o in yes_s:
                cancel(o, "YES sells exceed the position")
            yes_s = []
        if sum(o["quantity"] for o in no_s) > max(-q, 0):
            for o in no_s:
                cancel(o, "NO sells exceed the position")
            no_s = []
        if q > 0 and ex in basket_ids:
            threshold = max(RESCUE_FRACTION * p["avgCost"], RESCUE_FLOOR)
            if guard and bid is not None and bid <= threshold + 1e-9:
                log(f"RESCUE {name(ex)}: bid {bid:.3f} below threshold {threshold:.3f} "
                    f"(max of {RESCUE_FRACTION:.0%} of avg cost {p['avgCost']:.3f} and floor {RESCUE_FLOOR})")
                dump_at_best_bid(tid, ex, "yes", yes_s, q, tick_down(bid))
            else:
                laddered_sells(tid, ex, yes_s, q, p["avgCost"], sell_floor)
        elif q > 0 and bid:
            dump_at_best_bid(tid, ex, "yes", yes_s, q, tick_down(bid))
        elif q < 0 and ask:
            dump_at_best_bid(tid, ex, "no", no_s, -q, round(1 - tick_up(ask), 3))

    # 2. Buys in tranches: only the quantity shown at the best offer, never above the cap.
    #    Day: building the basket below reference x 1.10.
    #    Night (East to West rolling): the cash from sales is reinvested into basket contracts still
    #    below NIGHT_PRICE_CAP and below NIGHT_MOMENTUM_MAX x their pre-count price, provided they hold
    #    (bid at NIGHT_DROP_GUARD of that price); cost per contract capped at NIGHT_MAX_SHARE of equity,
    #    cheapest first.
    def price_cap(ex):
        if day:
            return tick_down(basket_ids[ex] * PRICE_CAP)
        if night_buys:
            ref = precount_ref(tid, ex)
            if ref:
                return tick_down(min(NIGHT_PRICE_CAP, NIGHT_MOMENTUM_MAX * ref))
        return None

    buys = [o for o in orders if o["action"] == "buy"]
    budget = (1 - CASH_BUFFER) * equity / max(len(basket_ids), 1)
    committed = {}
    for o in buys:
        ex = o["exchangeId"]
        cap = price_cap(ex) if o["side"] == "yes" and ex in basket_ids else None
        remaining = (o["quantity"] - (o.get("quantityFilled") or 0)) * o["priceLimit"]
        if cap is not None and o["priceLimit"] <= cap + 1e-9 and ex not in committed:
            committed[ex] = remaining
        else:
            cancel(o, "buy outside the basket" if ex not in basket_ids else
                   "buy window closed" if cap is None else "stale level or duplicate")
    if day or night_buys:
        spendable = cash - sum(committed.values()) - 200
        if day:
            order_of_play = sorted(basket_ids, key=lambda e: basket_ids[e])
        else:
            order_of_play = sorted(basket_ids, key=lambda e: (prices.get(e, (None, None))[1] or 1.0))
        for ex in order_of_play:
            p = pos.get(ex)
            invested = abs(p["costBasis"]) if p else 0.0
            if (p and p["quantity"] < 0) or ex in committed:
                continue
            cap = price_cap(ex)
            if cap is None:
                continue
            if day:
                limit = budget
            else:
                bid = prices.get(ex, (None, None))[0]
                ref = precount_ref(tid, ex)
                if ref is None or bid is None or bid < NIGHT_DROP_GUARD * ref - 1e-9:
                    continue  # the race is dropping: never roll into it, the rescue handles the exit
                limit = NIGHT_MAX_SHARE * equity
            room = limit - invested
            if room < 100:
                continue
            ob = get(f"/exchanges/{ex}/orderbook", tournamentId=tid, depth=5)
            asks = sorted((a for a in (ob or {}).get("asks", [])), key=lambda a: a["price"])
            if not asks:
                continue
            price = asks[0]["price"]
            if price > cap + 1e-9:
                continue  # above the cap: wait for the market to come back
            qty = int(min(asks[0]["quantity"], room / price, max(spendable, 0) / price))
            if qty > 0:
                place(tid, ex, "buy", qty, tick_up(price))
                spendable -= qty * price

    held = "  ".join(f"{name(ex)[:28]}:{int(p['quantity']):+,}@{p['avgCost']:.3f}"
                     for ex, p in pos.items() if ex in basket_ids and p["quantity"] > 0) or "none"
    outside = sum(1 for ex, p in pos.items() if ex not in basket_ids)
    filled = sum(min(abs(pos[ex]["costBasis"]) / budget, 1) for ex in basket_ids if ex in pos) / max(len(basket_ids), 1)
    log(f"Round OK{' (NIGHT)' if night else ''}: account {equity:,.0f}, cash ~{cash:,.0f}, basket "
        f"{filled:.0%} filled ({budget:,.0f}/contract), positions {held}, {outside} to clean up, {writes} writes")


if __name__ == "__main__":
    if not API_KEY:
        sys.exit("Set the SUPERMARKET_API_KEY environment variable first.")
    tid = get(f"/tournaments/{SLUG}")["id"]
    if STOP:
        resolve_ids()
        while True:  # every open order on the account
            page = get("/orders", tournamentId=tid, status="open", limit=200)["data"]
            if not page:
                break
            for o in page:
                r = call("DELETE", f"/orders/{o['id']}")
                log(f"Cancelled order {o['id']} ({name(o['exchangeId'])}): {'OK' if r is not None and r.ok else why(r)}")
        sys.exit()
    load_precount_refs()
    # Printed at every start so a deploy can be checked from the log (see "What failed" in the README)
    log(f"Starting the basket bot in {'LIVE' if LIVE else 'SIMULATION'} mode: night sell floor "
        f"{NIGHT_SELL_FLOOR}, {len(precount_refs)} frozen pre-count references loaded from {PRECOUNT_FILE}")
    while True:
        try:
            one_round(tid)
        except Exception as e:  # log and carry on, the next round re-reads true state
            log(f"Error during the round: {e}")
        if not LIVE:
            log("Simulation finished. Run again with --live to trade.")
            break
        time.sleep(ROUND_SECONDS)
