"""python -m book_recorder {record,markets,trades,stats} ...

  record   follow the most active markets on the websocket and write daily files (the service)
  markets  print the market set `record` would pick now, and exit
  trades   download historical trades (with wallets) from the Data API v2
  stats    summarise day files: event counts, tokens, disconnections, trades
"""
import argparse
import asyncio
import json
import logging
import signal
import sys
import time
from functools import partial

from . import markets as mk
from .replay import stats
from .trades import TradeDownloader, recorded_markets


def http_get(session, url, retries=3, pause=0.2, **params):
    import requests
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=20)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
            r.raise_for_status()
            time.sleep(pause)
            return r.json()
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt * 2)


def _session():
    import requests
    s = requests.Session()
    s.headers["User-Agent"] = "book-recorder/1.0"
    return s


def _selector(a):
    get = partial(http_get, _session())
    pinned = [p.strip() for p in (a.pin or "").split(",") if p.strip()]
    return lambda: mk.select(get, n=a.markets, min_hours_left=a.min_hours_left, min_liquidity=a.min_liquidity,
                             max_per_event=a.max_per_event or None, pinned=pinned)


def _selection_args(p):
    p.add_argument("--markets", type=int, default=100, help="how many markets to follow (default 100)")
    p.add_argument("--min-hours-left", type=float, default=24.0,
                   help="skip markets ending sooner than this (default 24, drops hourly crypto candles)")
    p.add_argument("--min-liquidity", type=float, default=1000.0, help="minimum Gamma liquidity in USDC")
    p.add_argument("--max-per-event", type=int, default=0, help="cap per event, 0 = no cap")
    p.add_argument("--pin", help="comma separated slugs or condition ids always recorded")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="book_recorder", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("record", help="run the websocket recorder")
    r.add_argument("--out", required=True, help="data directory (book/, markets/ go there)")
    _selection_args(r)
    r.add_argument("--shard-size", type=int, default=50, help="tokens per websocket connection")
    r.add_argument("--refresh-hours", type=float, default=0.0,
                   help="re-select markets every N hours as well as at midnight UTC (0 = midnight only)")
    r.add_argument("--min-free-gb", type=float, default=2.0, help="pause recording below this much free disk")

    m = sub.add_parser("markets", help="print the current selection")
    _selection_args(m)

    t = sub.add_parser("trades", help="download historical trades")
    t.add_argument("--out", required=True)
    t.add_argument("--market", action="append", default=[], help="condition id (repeatable)")
    t.add_argument("--recorded-days", type=int, default=2,
                   help="without --market: every market recorded in the last N day files (default 2)")
    t.add_argument("--days-back", type=float, default=30.0, help="first download goes back this far (default 30)")
    t.add_argument("--endpoint", default="/v2/trades")

    s = sub.add_parser("stats", help="summarise day files")
    s.add_argument("files", nargs="+")

    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        stream=sys.stdout)

    if a.cmd == "markets":
        for x in _selector(a)():
            print(f"{x['volume_24h']:>12,.0f}  {x['liquidity']:>10,.0f}  {len(x['tokens'])}  {x['slug']}")
        return
    if a.cmd == "stats":
        for f in a.files:
            print(json.dumps(stats(f), indent=1))
        return
    if a.cmd == "trades":
        d = TradeDownloader(a.out, partial(http_get, _session()), endpoint=a.endpoint)
        ids = a.market or recorded_markets(a.out, a.recorded_days)
        if not ids:
            sys.exit("no market: pass --market or run the recorder first")
        res = d.download_all(ids, since=time.time() - a.days_back * 86400)
        errors = {k: v for k, v in res.items() if isinstance(v, str)}
        logging.info("%d markets, %d new trades, %d errors", len(res),
                     sum(v for v in res.values() if isinstance(v, int)), len(errors))
        sys.exit(1 if errors and len(errors) == len(res) else 0)

    from .ws import Recorder
    rec = Recorder(a.out, _selector(a), shard_size=a.shard_size, refresh_hours=a.refresh_hours,
                   min_free_gb=a.min_free_gb)

    async def run():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await rec.run(stop)

    asyncio.run(run())


if __name__ == "__main__":
    main()
