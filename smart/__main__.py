"""python -m smart {paper,backtest,report,clear-halt} ...

  paper       follow the recorder's live day file and trade S1 on paper (the service). No order
              is ever sent: this package has no live mode.
  backtest    run the same engine over recorded days, into a separate journal
  study       measure what happens after sweeps on recorded days (no strategy, no money)
  mm-study    which markets are worth making: maker edge after each trade, and the SIG cycle replayed
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
