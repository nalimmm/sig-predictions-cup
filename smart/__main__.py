"""python -m smart {paper,backtest,report,clear-halt} ...

  paper       follow the recorder's live day file and trade S1 on paper (the service). No order
              is ever sent: this package has no live mode.
  backtest    run the same engine over recorded days, into a separate journal
  study       measure what happens after sweeps on recorded days (no strategy, no money)
  mm-study    which markets are worth making: maker edge after each trade, and the SIG cycle replayed
  mm-scan     rank the recorded markets for market making (flow, spread, adverse selection, event risk)
  mm-sim      paper market making on chosen markets, replayed on recorded days
  mm-live     the same simulator on the recorder's live file, feeding the private dashboard
  report      summarise a journal directory
  clear-halt  restart a pod halted by a circuit breaker (the user's decision, never automatic)
"""
import argparse
import json
import logging
import signal
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from .config import Config
from .engine import Engine, Journal
from .feed import follow, replay, utc_day
from .report import read, render, summary

log = logging.getLogger("smart")


def _days(spec: str):
    if ".." in spec:
        a, b = (date.fromisoformat(x) for x in spec.split(".."))
        return [(a + timedelta(d)).isoformat() for d in range((b - a).days + 1)]
    return [d.strip() for d in spec.split(",") if d.strip()]


def _config(path):
    return Config.from_dict(json.loads(Path(path).read_text())) if path else Config()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="smart", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("paper")
    p.add_argument("--data", default="/var/lib/book-recorder", help="the recorder's directory")
    p.add_argument("--out", default="/var/lib/smart", help="journal/ and state.json go here")
    p.add_argument("--config", help="JSON file overriding Config defaults")
    b = sub.add_parser("backtest")
    b.add_argument("--data", default="/var/lib/book-recorder")
    b.add_argument("--days", required=True, help="2026-11-19..2026-11-21 or a comma list")
    b.add_argument("--out", required=True, help="journal directory for this run (emptied first)")
    b.add_argument("--config")
    st = sub.add_parser("study")
    st.add_argument("--data", default="/var/lib/book-recorder")
    st.add_argument("--days", required=True)
    st.add_argument("--out", required=True, help="summary.json and sweeps.jsonl go here")
    st.add_argument("--config")
    mm = sub.add_parser("mm-study")
    mm.add_argument("--data", default="/var/lib/book-recorder")
    mm.add_argument("--days", required=True)
    mm.add_argument("--out", required=True, help="mm_summary.json, markouts.csv, cycles_per_token.json")
    ms = sub.add_parser("mm-sim")
    ms.add_argument("--data", default="/var/lib/book-recorder")
    ms.add_argument("--days", required=True)
    ms.add_argument("--slugs", required=True, help="comma separated market slugs")
    ms.add_argument("--out", required=True, help="mm_sim.json and mm_fills.jsonl go here")
    ms.add_argument("--config", help="JSON file overriding MMConfig defaults")
    ml = sub.add_parser("mm-live", help="paper market making on the recorder's live file, feeding the private dashboard")
    ml.add_argument("--data", default="/var/lib/book-recorder")
    ml.add_argument("--slugs", required=True)
    ml.add_argument("--out", default="/var/lib/smart-mm", help="data.json (clear, local only) and data.enc go here")
    ml.add_argument("--config")
    ml.add_argument("--every", type=float, default=60.0, help="seconds between snapshots")
    ml.add_argument("--upload", action="store_true", help="send data.enc to object storage (BOOK_S3_* variables)")
    ml.add_argument("--no-greeks", action="store_true", help="do not read the BTC price from Binance (no Greeks)")
    sc = sub.add_parser("mm-scan", help="rank the recorded markets for market making")
    sc.add_argument("--data", default="/var/lib/book-recorder")
    sc.add_argument("--days", required=True, help="2026-11-21..2026-11-23, a list, or last:N for the last N complete days")
    sc.add_argument("--out", required=True, help="scan.csv and scan.json go here")
    sc.add_argument("--set", action="append", default=[], metavar="NAME=VALUE", help="override a scan threshold")
    dp = sub.add_parser("dash-page", help="upload the dashboard page to object storage (BOOK_S3_* variables)")
    dp.add_argument("--page", default="deploy/smart-dashboard/index.html")
    r = sub.add_parser("report")
    r.add_argument("journal")
    r.add_argument("--json", action="store_true")
    c = sub.add_parser("clear-halt")
    c.add_argument("--out", default="/var/lib/smart")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        stream=sys.stdout)

    if a.cmd == "report":
        s = summary(read(a.journal))
        print(json.dumps(s, indent=1) if a.json else render(s))
        return

    if a.cmd == "study":
        from .study import run as study_run
        t0 = time.time()
        s = study_run(a.data, _days(a.days), a.out, _config(a.config),
                      progress=lambda x: log.info("%d events, %d sweeps, %s", x.n_events, len(x.events),
                                                  x.now and utc_day(x.now)))
        log.info("done in %.0f s", time.time() - t0)
        print(json.dumps(s, indent=1))
        return

    if a.cmd == "mm-study":
        from .mm_study import run as mm_run
        t0 = time.time()
        s = mm_run(a.data, _days(a.days), a.out, progress=lambda n, x: log.info("%d events, %d markets", n, len(x.acc)))
        log.info("done in %.0f s", time.time() - t0)
        print(json.dumps(s, indent=1))
        return

    if a.cmd == "mm-sim":
        from .mm import MMConfig, run as mm_sim
        mcfg = MMConfig.from_dict(json.loads(Path(a.config).read_text())) if a.config else MMConfig()
        s = mm_sim(a.data, _days(a.days), [x.strip() for x in a.slugs.split(",") if x.strip()], a.out, mcfg,
                   progress=lambda n, x: log.info("%d events, %d fills", n, sum(len(q.fills) for q in x.q.values())))
        print(json.dumps(s, indent=1))
        return

    if a.cmd == "mm-live":
        import os
        from .dash import Publisher
        from .mm import MMConfig, MMSim
        mcfg = MMConfig.from_dict(json.loads(Path(a.config).read_text())) if a.config else MMConfig()
        password = os.environ.get("SMART_DASH_PASSWORD") or None        # from an env file, never from the repository
        client = bucket = None
        if a.upload:
            if not password:
                raise SystemExit("--upload needs SMART_DASH_PASSWORD: the snapshot is never uploaded in clear")
            from book_recorder.offload import client_from_env
            client, bucket, _ = client_from_env()
        sim = MMSim(a.data, [x.strip() for x in a.slugs.split(",") if x.strip()], mcfg)
        spot = None
        if not a.no_greeks:
            from .greeks import binance_spot as spot
        pub = Publisher(a.out, password, client, bucket, every_s=a.every)
        spot_next, spot_at = 0.0, 0.0
        stopping = []
        for sg in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sg, lambda *_: stopping.append(1))
        log.info("paper market making on %s, %d markets asked, upload=%s; no order is ever sent", a.data, len(sim.slugs), a.upload)
        for ev in follow(Path(a.data) / "book", stop=lambda: bool(stopping)):
            if spot and time.time() >= spot_next:       # BTC price once a minute; dropped when 10 minutes stale
                spot_next = time.time() + 60
                px = spot()
                if px:
                    sim.spot, spot_at = px, time.time()
                    with open(Path(a.out) / "btc_spot.jsonl", "a") as f:        # kept so a replay can apply the delta limit
                        f.write(json.dumps({"t": int(spot_at * 1000), "px": px}) + "\n")
                elif time.time() - spot_at > 600:
                    sim.spot = None
            sim.on_event(ev)
            if pub.maybe(sim) and pub.errors:
                log.warning("%d failed uploads so far", pub.errors)
        pub.next = 0
        pub.maybe(sim)
        return

    if a.cmd == "mm-scan":
        import csv
        from .mm_study import Markouts, scan
        if a.days.startswith("last:"):              # the last N complete UTC days that were recorded
            have = sorted(f.name[:10] for f in (Path(a.data) / "book").glob("????-??-??.jsonl.gz") if f.name[:10] < utc_day(time.time() * 1000))
            days = have[-int(a.days[5:]):]
            if not days:
                raise SystemExit("no complete recorded day yet")
        else:
            days = _days(a.days)
        mk, n = Markouts(a.data), 0
        for ev in replay(Path(a.data) / "book", days):
            mk.on_event(ev)
            n += 1
            if n % 2_000_000 == 0:
                log.info("%d events, %d markets", n, len(mk.acc))
        over = {k: float(v) for k, v in (x.split("=", 1) for x in a.set)}
        rows = scan(mk.rows(), date.fromisoformat(days[-1]).isoformat(), **over)
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        if rows:
            names = list(dict.fromkeys(k for r in rows for k in r))
            with open(out / "scan.csv", "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=names)
                w.writeheader()
                w.writerows(rows)
        keep = [r for r in rows if r.get("rank")]
        cols = ("slug", "event", "end_date", "mid", "tick", "trades_per_day", "usdc_per_day", "spread_ticks", "depth_best_usdc",
                "half_spread_earned_c", "edge_300s_c", "edge_1800s_c", "edge_300s_t", "adverse_selection_c", "score",
                "days_left", "rejected")
        near = [{k: r.get(k) for k in cols} for r in rows
                if not r.get("rank") and r["score"] > 0 and r["rejected"].count(";") == 0][:15]
        reasons = {}
        for r in rows:
            for w in filter(None, (x.strip() for x in r["rejected"].split(";"))):
                reasons[w] = reasons.get(w, 0) + 1
        (out / "scan.json").write_text(json.dumps({"days": days, "generated_ms": int(time.time() * 1000), "events_read": n, "markets_measured": len(rows),
                                                   "selected": keep, "slugs": ",".join(r["slug"] for r in keep),
                                                   "rejections": reasons, "near_misses_one_reason": near}, indent=1))
        print(json.dumps({"markets_measured": len(rows), "selected": keep}, indent=1))
        return

    if a.cmd == "dash-page":
        from book_recorder.offload import client_from_env
        from .dash import upload_page
        client, bucket, _ = client_from_env()
        key = upload_page(client, bucket, a.page)
        print(f"uploaded {key}")          # the bucket name and endpoint stay out of the logs
        return

    if a.cmd == "clear-halt":
        state = Path(a.out) / "state.json"
        s = json.loads(state.read_text())
        print(f"halted: {s.get('halted')}, nav {s.get('nav')}")
        s["halted"] = None
        s["hwm"] = s.get("nav", s["hwm"])
        state.write_text(json.dumps(s, indent=1))
        print("cleared; restart the service: sudo systemctl restart smart-paper")
        return

    cfg = _config(a.config)
    if a.cmd == "backtest":
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        for f in out.glob("*.jsonl"):
            f.unlink()
        eng = Engine(cfg, Journal(out), data_dir=a.data)
        eng.log("start", mode="backtest", days=a.days, config=cfg.to_dict())
        n, t0 = 0, time.time()
        for ev in replay(Path(a.data) / "book", _days(a.days)):
            eng.on_event(ev)
            n += 1
            if n % 2_000_000 == 0:
                log.info("%d events, %s, nav %.2f", n, eng.now and utc_day(eng.now), eng.nav())
        eng.log("end", events=n, seconds=round(time.time() - t0), nav=round(eng.nav(), 4))
        eng.journal.close()
        print(render(summary(read(out))))
        return

    out = Path(a.out)
    (out / "journal").mkdir(parents=True, exist_ok=True)
    eng = Engine(cfg, Journal(out / "journal"), data_dir=a.data, wall=time.time, state_path=out / "state.json")
    stopping = []
    for sg in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sg, lambda *_: stopping.append(1))
    eng.log("start", mode="paper", resume_after=eng.resume_after, nav=round(eng.nav(), 4),
            halted=eng.halted, config=cfg.to_dict())
    eng.journal.flush()
    log.info("paper trading S1 on %s, nav %.2f, halted=%s; catching up on today's file first",
             a.data, eng.nav(), eng.halted)
    caught_up = False
    try:
        for ev in follow(Path(a.data) / "book", stop=lambda: bool(stopping)):
            eng.on_event(ev)
            if not caught_up and ev.get("_e") == "idle":
                caught_up = True
                eng.log("caught_up", lag_s=round(time.time() - eng.now / 1000, 1) if eng.now else None)
                log.info("caught up with the recorder, trading on paper")
    finally:
        eng.save()
        eng.log("stop", nav=round(eng.nav(), 4))
        eng.journal.close()


if __name__ == "__main__":
    main()
