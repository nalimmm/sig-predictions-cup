"""Live trading dashboard for the basket strategy (SIG Predictions Cup, virtual money).

Serves a single page at http://<server>/<token> (the token is stored in dashboard_token.txt),
refreshed every minute: race to the top of the leaderboard, account value and rolling Sharpe over
time, the basket contract by contract with sell targets and rescue thresholds, settlement scenarios,
bot health. One thread collects from the tournament API (about 10 reads per minute), a plain
http.server thread serves the page. No framework, no database, no javascript: the page is static
HTML regenerated on every request.

The dashboard ran as a systemd service next to the bot and duplicates the BASKET table on purpose:
the two were independent services on the same box, each able to restart without the other.

Usage: python3 dashboard.py   (port 80; set DASH_PORT to change it)
Requires the SUPERMARKET_API_KEY environment variable.
"""
import html
import math
import os
import secrets
import statistics
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

API_KEY = os.environ.get("SUPERMARKET_API_KEY")
BASE = os.environ.get("SUPERMARKET_BASE", "https://sig.thesuper.market/api/v1")
SLUG = "midterm-elections"
PORT = int(os.environ.get("DASH_PORT", "80"))

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
PRICE_CAP, CASH_BUFFER, HIGH_LEG_BELOW, RESCUE_FRACTION = 1.10, 0.02, 0.20, 0.50
DAY_BUYS_END = datetime(2026, 11, 3, 23, 0, tzinfo=timezone.utc)
COUNT_START = datetime(2026, 11, 4, 0, 0, tzinfo=timezone.utc)
MARKET_FREEZE = datetime(2026, 11, 4, 17, 0, tzinfo=timezone.utc)
SHARPE_WINDOW_H = 72
BOT_LOG = "basket_bot.log"
INTERVAL = 60

session = requests.Session()
session.headers["Authorization"] = f"Bearer {API_KEY}"
state = {"at": None, "error": None}
lock = threading.Lock()
names, basket_ids, ids_at, hist, hist_at, lb, lb_at = {}, {}, 0.0, [], 0.0, None, 0.0


def log(msg):
    with open("dashboard.log", "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%d/%m %H:%M:%S}  {msg}\n")


def get(path, **params):
    for _ in range(3):
        r = session.get(BASE + path, params={k: v for k, v in params.items() if v is not None}, timeout=30)
        time.sleep(0.8)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", 30)))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"{path}: rate limited")


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def now():
    return datetime.now(timezone.utc)


def short(t):
    t = (t or "").replace("Will the ", "").replace(" win the", "").rstrip("?")
    for a, b in (("Democratic Party", "D"), ("Independent Party", "Ind."), ("Republican Party", "R")):
        t = t.replace(a, b)
    return t


def tick_up(p):
    return round(max(0.005, min(0.995, math.ceil(round(p / 0.005, 6)) * 0.005)), 3)


def resolve_ids():
    global ids_at
    if time.time() - ids_at < 3600 and basket_ids:
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


def read_bot_log():
    out = {"last_round": None, "line": "", "events": []}
    try:
        lines = open(BOT_LOG, encoding="utf-8").readlines()[-3000:]
    except OSError:
        return out
    cutoff = now() - timedelta(hours=24)
    for line in lines:
        try:
            t = datetime.strptime(f"{now().year}/{line[:14]}", "%Y/%d/%m %H:%M:%S").astimezone(timezone.utc)
        except ValueError:
            continue
        if "Round OK" in line:
            out["last_round"], out["line"] = t, line[16:].strip()
        if t >= cutoff and ("FAIL" in line or "Error" in line or "RESCUE" in line or "WARNING" in line):
            out["events"].append((t, line[16:].strip()))
    out["events"] = out["events"][-8:]
    return out


def collect():
    global hist, hist_at, lb, lb_at
    tour = get(f"/tournaments/{SLUG}")
    tid, initial = tour["id"], tour["initialBalance"]
    resolve_ids()
    pnl = get(f"/tournaments/{SLUG}/portfolio/pnl", period="all")
    pnl_day = get(f"/tournaments/{SLUG}/portfolio/pnl", period="day")
    pos = [p for p in get(f"/tournaments/{SLUG}/portfolio/positions")["positions"]
           if not p["settled"] and p["quantity"]]
    orders, cursor = [], None
    while True:
        page = get("/orders", tournamentId=tid, status="open", limit=200, cursor=cursor)
        orders += page["data"]
        if not page["pagination"]["hasMore"]:
            break
        cursor = page["pagination"]["nextCursor"]
    if time.time() - lb_at >= 300 or lb is None:
        lb, lb_at = get(f"/tournaments/{SLUG}/leaderboard", period="all", sort="pnl", limit=3), time.time()
    if time.time() - hist_at >= 600 or not hist:
        hist = get(f"/tournaments/{SLUG}/portfolio/history", interval="1h")["data"]
        hist_at = time.time()
    ids = set(basket_ids) | {p["exchangeId"] for p in pos}
    prices = {}
    ids = list(ids)
    for i in range(0, len(ids), 100):
        for q in get("/exchanges/prices", ids=",".join(ids[i:i + 100]), tournamentId=tid)["data"]:
            prices[q["exchangeId"]] = (q.get("bestBid"), q.get("bestAsk"))
    with lock:
        state.update(at=now(), error=None, initial=initial, pnl=pnl, pnl_day=pnl_day, pos=pos, orders=orders,
                     lb=lb, hist=hist, prices=prices, bot=read_bot_log())


def loop():
    while True:
        try:
            collect()
        except Exception as e:
            with lock:
                state["error"] = f"{now():%H:%M} UTC: {e}"
            log(f"Collection error: {e}")
        time.sleep(INTERVAL)


# ---------- rendering ----------

def n(x, d=0):
    if x is None:
        return "-"
    return f"{x:,.{d}f}"


def pct(x, d=1):
    return "-" if x is None else n(100 * x, d) + "%"


def signed(x, d=0):
    if x is None:
        return "-"
    return ("+" if x > 0 else "") + n(x, d)


def cls(x):
    return "" if not x else "up" if x > 0 else "down"


def countdown(target, t):
    d = target - t
    if d.total_seconds() <= 0:
        return "passed"
    return f"{d.days}d {d.seconds // 3600}h" if d.days else f"{d.seconds // 3600}h {d.seconds % 3600 // 60:02d}"


def sharpe_series(points):
    """Annualized Sharpe over a rolling window of SHARPE_WINDOW_H hourly returns."""
    rets = []
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if v0 and v0 > 0:
            rets.append((t1, v1 / v0 - 1))
    out = []
    for i in range(len(rets)):
        win = [r for _, r in rets[max(0, i - SHARPE_WINDOW_H + 1):i + 1]]
        if len(win) >= 24:
            sd = statistics.pstdev(win)
            out.append((rets[i][0], (statistics.fmean(win) / sd * math.sqrt(24 * 365)) if sd > 1e-12 else None))
    return [(t, v) for t, v in out if v is not None]


def curve(points, height=200, fmt=lambda v: n(v / 1000, 0) + "k", zero=False):
    if len(points) < 2:
        return "<p class='empty'>Not enough history yet.</p>"
    width = 900
    t0, t1 = points[0][0].timestamp(), points[-1][0].timestamp()
    vs = [v for _, v in points]
    lo, hi = min(vs + ([0] if zero else [])), max(vs + ([0] if zero else []))
    pad = (hi - lo) * 0.08 or 1
    lo, hi = lo - pad, hi + pad
    X = lambda t: 52 + (t.timestamp() - t0) / ((t1 - t0) or 1) * (width - 62)
    Y = lambda v: 10 + (hi - v) / (hi - lo) * (height - 40)
    pts = " ".join(f"{X(t):.1f},{Y(v):.1f}" for t, v in points)
    grid = "".join(
        f"<line x1='52' x2='{width - 10}' y1='{Y(v):.1f}' y2='{Y(v):.1f}' class='grid'/>"
        f"<text x='46' y='{Y(v) + 4:.1f}' class='axis' text-anchor='end'>{fmt(v)}</text>"
        for v in (lo + (hi - lo) * k / 4 for k in range(1, 4)))
    zero_line = f"<line x1='52' x2='{width - 10}' y1='{Y(0):.1f}' y2='{Y(0):.1f}' class='zero'/>" \
        if zero and lo < 0 < hi else ""
    days, d = "", datetime.fromtimestamp(t0, timezone.utc).replace(hour=0, minute=0) + timedelta(days=1)
    step = max(1, int((t1 - t0) / 86400 / 8))
    while d.timestamp() < t1:
        days += f"<text x='{X(d):.1f}' y='{height - 10}' class='axis' text-anchor='middle'>{d:%b %d}</text>"
        d += timedelta(days=step)
    area = f"52,{Y(max(lo, 0) if zero else lo):.1f} {pts} {X(points[-1][0]):.1f},{Y(max(lo, 0) if zero else lo):.1f}"
    return (f"<svg viewBox='0 0 {width} {height}' class='curve'>{grid}{zero_line}{days}"
            f"<polygon points='{area}' class='area'/><polyline points='{pts}' class='line'/></svg>")


def page():
    with lock:
        e = dict(state)
    if not e.get("pnl"):
        return f"<!doctype html><meta charset='utf-8'><p>Collecting... {html.escape(e.get('error') or '')}</p>" \
               "<meta http-equiv='refresh' content='10'>"
    t = now()
    initial, pnl = e["initial"], e["pnl"]
    value = pnl["totalAccountValue"]
    gain = value - initial
    top = (e["lb"] or {}).get("leaderboard", [])
    rank = (e["lb"] or {}).get("myRank")
    third = top[2]["pnl"] if len(top) >= 3 else None
    progress = max(0.0, min(1.0, gain / third)) if third else 0

    points = [(ts(h["time"]), h["totalValue"]) for h in e["hist"] if h.get("totalValue") is not None]
    points.append((t, value))
    peak, dd_max = -1e18, 0.0
    for _, v in points:
        peak = max(peak, v)
        dd_max = max(dd_max, (peak - v) / peak if peak > 0 else 0)
    dd_now = (peak - value) / peak if peak > 0 else 0
    sh = sharpe_series(points)
    sharpe_now = sh[-1][1] if sh else pnl.get("sharpe")

    pos = {p["exchangeId"]: p for p in e["pos"]}
    cash = value - sum(p["marketValue"] for p in pos.values())
    budget = (1 - CASH_BUFFER) * value / max(len(basket_ids), 1)
    buys = {o["exchangeId"]: o for o in e["orders"] if o["action"] == "buy" and o["exchangeId"] in basket_ids}

    rows, cost_total, basket_value, held = [], 0.0, 0.0, {}
    for ex, ref in sorted(basket_ids.items(), key=lambda kv: -abs(pos.get(kv[0], {}).get("marketValue", 0))):
        p = pos.get(ex)
        q = int(p["quantity"]) if p else 0
        bid, ask = e["prices"].get(ex, (None, None))
        mid = (bid + ask) / 2 if bid is not None and ask is not None else None
        avg = p["avgCost"] if p else None
        cost = abs(p["costBasis"]) if p else 0.0
        cost_total += cost
        basket_value += p["marketValue"] if p else 0.0
        held[ex] = (q, avg)
        t2 = tick_up(min(0.95, 2 * avg)) if avg else None
        th = tick_up(min(0.95, (5 if (avg or 1) < HIGH_LEG_BELOW else 3) * avg)) if avg else None
        tranche = buys.get(ex)
        status = "full" if cost >= 0.98 * budget else \
            f"tranche {n((tranche['quantity'] - (tranche.get('quantityFilled') or 0)))} @ {n(tranche['priceLimit'], 3)}" \
            if tranche else ("above cap" if mid and mid > ref * PRICE_CAP else "building")
        rows.append(
            f"<tr><td>{html.escape(short(names.get(ex, ex)))}</td>"
            f"<td class='num'>{n(ref, 3)}</td><td class='num'>{n(mid, 3)}</td>"
            f"<td class='num'>{n(q)}</td><td class='num'>{n(avg, 3) if avg else '-'}</td>"
            f"<td class='num'><span class='gauge mini'><i style='width:{min(100, 100 * cost / budget):.0f}%'></i></span> "
            f"{pct(cost / budget, 0)}</td>"
            f"<td class='num {cls(p['unrealizedPnl'] if p else None)}'>{signed(p['unrealizedPnl'] if p else None)}</td>"
            f"<td class='num'>{n(t2, 2) if t2 else '-'} / {n(th, 2) if th else '-'}</td>"
            f"<td class='num'>{n(RESCUE_FRACTION * avg, 3) if avg else '-'}</td>"
            f"<td>{status}</td></tr>")

    # Settlement scenarios
    sc_targets = cash + sum((q // 2) * tick_up(min(0.95, 2 * a)) + (q - q // 2) *
                            tick_up(min(0.95, (5 if a < HIGH_LEG_BELOW else 3) * a))
                            for q, a in held.values() if q and a)
    sc_sweep_win = cash + sum(q for q, _ in held.values() if q)
    sc_sweep_lose = cash + sum(q * RESCUE_FRACTION * a for q, a in held.values() if q and a)
    sc_floor = cash

    outside = [(ex, p) for ex, p in pos.items() if ex not in basket_ids]
    bot = e["bot"]
    alive = bot["last_round"] and (t - bot["last_round"]) < timedelta(minutes=10)
    events = "".join(f"<li><time>{x:%d/%m %H:%M}</time> {html.escape(l[:160])}</li>"
                     for x, l in reversed(bot["events"])) or "<li class='empty'>Nothing to report over 24h.</li>"
    alert = f"<p class='alert'>Collection down: {html.escape(e['error'])}</p>" if e.get("error") else ""
    phase = ("Counting: rescue armed" if t >= COUNT_START else
             f"Buys open for another {countdown(DAY_BUYS_END, t)}" if t < DAY_BUYS_END else "Buys closed")

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>Predictions Cup: {n(value)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Sans+Condensed:wght@500;600&display=swap" rel="stylesheet">
<style>
:root{{--bg:#EEF1F5;--card:#FFFFFF;--ink:#14213D;--soft:#5B6B82;--rule:#D6DDE7;--up:#127A5B;--down:#B3261E;
--accent:#14213D;color-scheme:light}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0F1520;--card:#172031;--ink:#E6ECF5;--soft:#8D9AB0;--rule:#273349;
--up:#3FC59A;--down:#F2685E;--accent:#E6ECF5;color-scheme:dark}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 "IBM Plex Sans",system-ui,sans-serif;
font-variant-numeric:tabular-nums}}
main{{max-width:1180px;margin:0 auto;padding:20px 16px 48px}}
header{{display:flex;flex-wrap:wrap;gap:8px 20px;align-items:baseline;justify-content:space-between;margin-bottom:18px}}
h1{{font:600 20px/1.2 "IBM Plex Sans Condensed",sans-serif;margin:0}}
.status{{color:var(--soft);font-size:14px;display:flex;flex-wrap:wrap;gap:4px 18px}}
.dot{{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;background:var(--down)}}
.dot.ok{{background:var(--up)}}
.podium{{background:var(--card);border:1px solid var(--rule);border-radius:10px;padding:22px 22px 18px;margin-bottom:16px}}
.podium .value{{font:600 46px/1 "IBM Plex Sans Condensed",sans-serif;letter-spacing:-.5px}}
.podium .row{{display:flex;flex-wrap:wrap;gap:6px 28px;align-items:baseline;margin-bottom:16px}}
.track{{position:relative;height:14px;background:var(--bg);border-radius:7px;overflow:hidden}}
.track i{{position:absolute;inset:0 auto 0 0;background:var(--accent);border-radius:7px}}
.marks{{display:flex;flex-wrap:wrap;gap:2px 16px;justify-content:space-between;color:var(--soft);font-size:13px;margin-top:6px}}
.grid2{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px;margin-bottom:16px}}
section{{background:var(--card);border:1px solid var(--rule);border-radius:10px;padding:16px 18px}}
h2{{font:600 15px/1.3 "IBM Plex Sans Condensed",sans-serif;margin:0 0 10px}}
dl{{display:grid;grid-template-columns:1fr auto;gap:5px 12px;margin:0}}
dt{{color:var(--soft)}} dd{{margin:0;text-align:right;font-weight:500}}
.up{{color:var(--up)}} .down{{color:var(--down)}}
.curve{{width:100%;height:auto;display:block}}
.line{{fill:none;stroke:var(--accent);stroke-width:2}} .area{{fill:var(--accent);opacity:.07}}
.grid{{stroke:var(--rule)}} .zero{{stroke:var(--soft);stroke-dasharray:4 4}} .axis{{fill:var(--soft);font-size:12px}}
.scroll{{overflow-x:auto}}
table{{border-collapse:collapse;width:100%;min-width:880px;font-size:14px}}
th{{text-align:left;font-weight:500;color:var(--soft);padding:6px 8px;border-bottom:1px solid var(--rule);white-space:nowrap}}
td{{padding:6px 8px;border-bottom:1px solid var(--rule);white-space:nowrap}}
.num{{text-align:right}} th.num{{text-align:right}}
.gauge{{height:10px;background:var(--bg);border-radius:5px;overflow:hidden;display:inline-block}}
.gauge.mini{{width:52px;vertical-align:middle;margin-right:6px}}
.gauge i{{display:block;height:100%;background:var(--accent)}}
ul{{list-style:none;padding:0;margin:0;font-size:13px}} li{{padding:4px 0;border-bottom:1px solid var(--rule)}}
time{{color:var(--soft);margin-right:8px}}
.empty{{color:var(--soft)}}
.alert{{background:var(--down);color:#fff;padding:8px 12px;border-radius:8px}}
.foot{{color:var(--soft);font-size:13px;margin-top:6px}}
</style></head><body><main>
<header><h1>Predictions Cup, basket desk</h1>
<div class="status"><span><span class="dot {'ok' if alive else ''}"></span>Bot {'alive' if alive else 'silent'}{f", round at {bot['last_round']:%H:%M} UTC" if bot['last_round'] else ""}</span>
<span>{phase}</span><span>Market freezes in {countdown(MARKET_FREEZE, t)}</span></div></header>
{alert}
<div class="podium">
<div class="row"><span class="value">{n(value)}</span>
<span class="{cls(gain)}">{signed(gain)} ({pct(gain / initial, 1)})</span>
<span>Rank {rank or '-'}</span>
<span class="{cls(e['pnl_day'].get('periodPnl'))}">Day {signed(e['pnl_day'].get('periodPnl'))}</span>
<span>72h Sharpe: {n(sharpe_now, 1)}</span></div>
<div class="track"><i style="width:{100 * progress:.2f}%"></i></div>
<div class="marks"><span>{pct(progress, 1)} of 3rd place's gain</span>
<span>3rd: {signed(third)}&nbsp;&nbsp;1st: {signed(top[0]['pnl'] if top else None)}</span></div>
</div>
<section style="margin-bottom:16px"><h2>Basket ({len(basket_ids)} contracts), invested {n(cost_total)} / {n(8 * budget)}, value {n(basket_value)}, cash {n(cash)}</h2>
<div class="scroll"><table>
<tr><th>Contract</th><th class="num">Ref</th><th class="num">Price</th><th class="num">Shares</th>
<th class="num">Avg cost</th><th class="num">Budget filled</th><th class="num">Unrealized</th>
<th class="num">Targets 2x / high</th><th class="num">Rescue</th><th>Status</th></tr>
{''.join(rows)}</table></div>
<p class="foot">Rescue: while votes are counted, the full position is sold if the best bid drops below this price.
{f"{len(outside)} inherited position(s) still being cleaned up." if outside else ""}</p></section>
<div class="grid2">
<section><h2>Account value</h2>{curve(points)}</section>
<section><h2>Annualized Sharpe, 72h window</h2>{curve(sh, fmt=lambda v: n(v, 1), zero=True)}</section>
</div>
<div class="grid2">
<section><h2>Settlement scenarios</h2><dl>
<dt>All 2x to 5x targets hit</dt><dd>{n(sc_targets)}</dd>
<dt>Full sweep our way: everything resolves to 1</dt><dd class="up">{n(sc_sweep_win)}</dd>
<dt>Full sweep against, rescues filled at 50%</dt><dd class="down">{n(sc_sweep_lose)}</dd>
<dt>Sweep against, no rescue fills (floor)</dt><dd class="down">{n(sc_floor)}</dd>
<dt>Drawdown now / max</dt><dd>{pct(dd_now)} / {pct(dd_max)}</dd></dl>
<p class="foot">On current positions, cash included; intermediate sales on spikes improve these numbers.</p></section>
<section><h2>Bot log (24h)</h2><ul>{events}</ul>
<p class="foot">Last round: {html.escape(bot['line'][:240])}</p></section>
</div>
</main></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.rstrip("/") != "/" + TOKEN:
            self.send_response(404)
            self.end_headers()
            return
        try:
            body = page().encode("utf-8")
            code = 200
        except Exception as e:
            log(f"Rendering error: {e}")
            body, code = f"Rendering error: {html.escape(str(e))}".encode("utf-8"), 500
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    if not API_KEY:
        sys.exit("Set the SUPERMARKET_API_KEY environment variable first.")
    try:
        TOKEN = open("dashboard_token.txt").read().strip()
    except OSError:
        TOKEN = secrets.token_urlsafe(12)
        open("dashboard_token.txt", "w").write(TOKEN)
    log(f"Starting: http://<server>/{TOKEN}")
    threading.Thread(target=loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
