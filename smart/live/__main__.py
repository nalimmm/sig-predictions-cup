"""Shadow and real execution of the Smart market maker.

  python -m smart.live shadow   live books, real order layer, dry venue: nothing is signed or sent
  python -m smart.live live     real orders; refuses to start unless every safeguard is in place
  python -m smart.live check    read-only checklist before going live (sends no order)
  python -m smart.live stop     write the STOP file and cancel every open order of the wallet
  python -m smart.live clear-halt   the owner's decision after a circuit breaker, never automatic

Files on the server, none in the repository:
  /etc/smart-live/wallet.env   POLYMARKET_FUNDER=<address shown on the Polymarket profile>
                               POLYMARKET_SIGNATURE_TYPE=<0 plain wallet, 1 email/Magic, 2 browser wallet>
  /etc/smart-live/key          the wallet's private key, root only (chmod 600), written by the owner
  /etc/smart-live/ARMED        armed=yes, budget_usdc=100, until=YYYY-MM-DD (see guard.py)
  /etc/smart-live/STOP         present = stop and refuse to start
"""
import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

from ..mm import MMConfig
from .execution import Executor, Journal, LiveMM
from .guard import ETC, Guard, GuardConfig, geoblock, read_arming, stop_requested
from .markets import fetch, http_get
from .runner import Publish, Runner
from .venue import ClobVenue, DryVenue, read_key, wallet_positions

log = logging.getLogger("smart.live")


def slugs_from(a) -> list:
    raw = a.slugs or os.environ.get("SMART_MM_SLUGS", "")
    out = [x.strip() for x in raw.split(",") if x.strip()]
    if not out:
        raise SystemExit("no market: pass --slugs or set SMART_MM_SLUGS")
    return out


def wallet_env(path: Path = ETC / "wallet.env") -> dict:
    d = {k: os.environ[k] for k in ("POLYMARKET_FUNDER", "POLYMARKET_SIGNATURE_TYPE") if os.environ.get(k)}
    if path.exists() and os.access(path, os.R_OK):
        for line in path.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if "=" in line:
                k, v = (x.strip().strip('"') for x in line.split("=", 1))
                d.setdefault(k, v)
    return d


def build(a, mode: str, gcfg: GuardConfig, venue_factory):
    slugs = slugs_from(a)
    meta = fetch(slugs)
    found = {m["slug"] for m in meta.values()}
    for s in slugs:
        if s not in found:
            log.warning("market not found on Gamma: %s", s)
    cfg = MMConfig.from_dict(json.loads(Path(a.config).read_text())) if a.config else MMConfig()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    sim = LiveMM(slugs, meta, cfg, mode)
    st = out / "state.json"
    if st.exists() and not a.fresh:
        s = json.loads(st.read_text())
        sim.restore(s, int(time.time() * 1000))
        log.info("restored %d markets from state.json", len(s.get("quoters", [])))
    guard = Guard(gcfg, out / "guard.json")
    venue = venue_factory(meta)
    ex = Executor(sim, venue, guard, Journal(out / "journal"), stale_feed_s=gcfg.stale_feed_s)
    password = os.environ.get("SMART_DASH_PASSWORD") or None
    client = bucket = None
    if a.upload:
        if not password:
            raise SystemExit("--upload needs SMART_DASH_PASSWORD: the snapshot is never uploaded in clear")
        from book_recorder.offload import client_from_env
        client, bucket, _ = client_from_env()
    pub = Publish(out, password, client, bucket, a.dash_key or f"smart-dashboard/data-{mode}.enc", a.every, gcfg.budget_usdc)
    spot = None
    if not a.no_greeks:
        from ..greeks import binance_spot as spot
    return Runner(sim, ex, guard, pub, http_get, spot, stop_file=ETC / "STOP")


def run(runner: Runner):
    loop = asyncio.new_event_loop()
    for sg in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sg, runner.stop, f"signal {sg.name}")
    loop.run_until_complete(runner.main())
    return runner.exit_reason


def preflight(gcfg: GuardConfig, key_path=None) -> tuple:
    """Everything real mode needs, checked before anything is sent. Raises with the reason."""
    if stop_requested(ETC / "STOP"):
        raise SystemExit("STOP file present: remove /etc/smart-live/STOP to start")
    arm = read_arming()
    gcfg.budget_usdc = arm["budget_usdc"]
    w = wallet_env()
    if not w.get("POLYMARKET_FUNDER") or w.get("POLYMARKET_SIGNATURE_TYPE") is None:
        raise SystemExit("wallet.env incomplete: POLYMARKET_FUNDER and POLYMARKET_SIGNATURE_TYPE are needed")
    key = read_key(path=key_path)
    if not key:
        raise SystemExit("no wallet key: real mode needs the credential polymarket-key")
    if geoblock(http_get) is not False:
        raise SystemExit("geoblock check did not answer 'not blocked': refusing to start")
    pos = wallet_positions(w["POLYMARKET_FUNDER"])
    committed = sum(p["size"] * p["avg"] for p in pos if p["size"] > 0)
    if committed > gcfg.budget_usdc:
        raise SystemExit(f"the wallet already holds positions that cost {committed:.2f} USDC, over the "
                         f"{gcfg.budget_usdc:.2f} budget: refusing to start")
    return key, w, committed


def main(argv=None):
    ap = argparse.ArgumentParser(prog="smart.live", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("shadow", "live"):
        p = sub.add_parser(name)
        p.add_argument("--slugs", help="comma separated; default: SMART_MM_SLUGS")
        p.add_argument("--out", default=f"/var/lib/smart-{name}")
        p.add_argument("--config", help="JSON overriding MMConfig")
        p.add_argument("--guard", help="JSON overriding GuardConfig (the budget comes from the arming file in live)")
        p.add_argument("--every", type=float, default=60.0)
        p.add_argument("--upload", action="store_true")
        p.add_argument("--dash-key", help="object key of the sealed snapshot (default smart-dashboard/data-<mode>.enc)")
        p.add_argument("--no-greeks", action="store_true")
        p.add_argument("--fresh", action="store_true", help="ignore state.json")
    c = sub.add_parser("check")
    c.add_argument("--key-file", default=str(ETC / "key"))
    s = sub.add_parser("stop")
    s.add_argument("--key-file", default=str(ETC / "key"))
    h = sub.add_parser("clear-halt")
    h.add_argument("--out", default="/var/lib/smart-live")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stdout)

    if a.cmd in ("shadow", "live"):
        gcfg = GuardConfig.from_dict(json.loads(Path(a.guard).read_text())) if a.guard else GuardConfig()
        if a.cmd == "shadow":
            runner = build(a, "shadow", gcfg, lambda meta: DryVenue())
        else:
            key, w, committed = preflight(gcfg)
            log.info("armed, budget %.2f USDC, wallet positions cost %.2f", gcfg.budget_usdc, committed)
            venue_box = {}

            def factory(meta):
                v = ClobVenue(key, w["POLYMARKET_FUNDER"], int(w["POLYMARKET_SIGNATURE_TYPE"]), meta)
                v.cancel_all()                      # a clean book: nothing left from an earlier run
                venue_box["v"] = v
                return v
            runner = build(a, "live", gcfg, factory)
            del key
            if runner.guard.halted:
                raise SystemExit(f"halted earlier ({runner.guard.halted['reason']}): the owner clears it with clear-halt")
        why = run(runner)
        if why and why.startswith("STOP"):
            sys.exit(0)
        return

    if a.cmd == "check":
        sys.exit(check(Path(a.key_file)))

    if a.cmd == "stop":
        ETC.mkdir(parents=True, exist_ok=True)
        (ETC / "STOP").write_text(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + "\n")
        print("STOP file written: the running process cancels its orders and exits within a second")
        key, w = read_key(path=a.key_file), wallet_env()
        if key and w.get("POLYMARKET_FUNDER"):
            v = ClobVenue(key, w["POLYMARKET_FUNDER"], int(w.get("POLYMARKET_SIGNATURE_TYPE", 1)), {})
            v.cancel_all()
            print("every open order of the wallet cancelled")
        return

    if a.cmd == "clear-halt":
        g = Guard(GuardConfig(), Path(a.out) / "guard.json")
        print(f"halt was: {g.halted}")
        g.clear()
        print("cleared. Start again with: sudo systemctl start smart-mm-real")


def check(key_file: Path) -> int:
    """Read-only. Prints one line per item, never an address or a key. Returns 0 when ready."""
    bad = 0

    def line(ok, what, detail=""):
        nonlocal bad
        bad += 0 if ok else 1
        print(f"[{'OK ' if ok else 'NON'}] {what}{(' : ' + detail) if detail else ''}")

    try:
        import py_clob_client  # noqa: F401
        line(True, "py-clob-client installed")
    except ImportError:
        line(False, "py-clob-client installed", "pip install py-clob-client in the venv")
    g = geoblock(http_get)
    line(g is False, "geoblock", {False: "not blocked", True: "BLOCKED", None: "check failed"}[g])
    line(not stop_requested(ETC / "STOP"), "no STOP file")
    try:
        arm = read_arming()
        line(True, "armed", f"budget {arm['budget_usdc']:.2f} USDC" + (f", until {arm['until']}" if arm.get("until") else ""))
    except PermissionError as e:
        line(False, "armed", str(e))
    w = wallet_env()
    line(bool(w.get("POLYMARKET_FUNDER")), "wallet address in wallet.env")
    line(w.get("POLYMARKET_SIGNATURE_TYPE") in ("0", "1", "2"), "account type in wallet.env", w.get("POLYMARKET_SIGNATURE_TYPE", "missing"))
    if w.get("POLYMARKET_FUNDER"):
        try:
            pos = wallet_positions(w["POLYMARKET_FUNDER"])
            cost = sum(p["size"] * p["avg"] for p in pos if p["size"] > 0)
            line(True, "wallet positions read", f"{len([p for p in pos if p['size'] > 0])} positions costing {cost:.2f} USDC")
        except Exception as e:
            line(False, "wallet positions read", f"{type(e).__name__}")
    if key_file.exists():
        st = key_file.stat()
        line(st.st_uid == 0 and not st.st_mode & 0o077, "key file root only (600)")
    key = read_key(path=str(key_file)) if key_file.exists() and os.access(key_file, os.R_OK) else None
    line(bool(key), "wallet key present", "" if key else "not yet written (expected until the owner arms)")
    if key and w.get("POLYMARKET_FUNDER"):
        try:
            v = ClobVenue(key, w["POLYMARKET_FUNDER"], int(w.get("POLYMARKET_SIGNATURE_TYPE", 1)), {})
            line(True, "API credentials derived from the key")
            line(True, "USDC available", f"{v.cash():.2f}")
            al = v.allowances() or {}
            ok = any(float(x or 0) > 0 for x in al.values())
            line(ok, "USDC spending approved for the exchange", "" if ok else "approve once on polymarket.com (a first manual trade does it)")
            line(True, "open orders", str(len(v.orders())))
            try:
                closed = v.client.get_closed_only_mode()
                line(not (closed or {}).get("closed_only"), "account not in closed-only mode")
            except Exception:
                pass
        except Exception as e:
            line(False, "CLOB access with the key", f"{type(e).__name__}: {str(e)[:120]}")
        key = None
    print("ready" if not bad else f"{bad} item(s) not ready")
    return 0 if not bad else 1


if __name__ == "__main__":
    main()
