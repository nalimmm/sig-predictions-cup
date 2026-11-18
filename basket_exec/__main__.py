"""python -m basket_exec --config examples/polymarket_basket.yaml [--live] [--stop]

Without --live nothing is sent: one round is planned and printed. Secrets come from the
environment only (see the example config), never from the YAML file.
"""
import argparse
import os
import sys

import yaml

from .config import Config
from .runner import Runner


def build_venue(cfg: dict, live: bool):
    kind = cfg["venue"]
    if kind == "polymarket":
        from .adapters.polymarket import PolymarketVenue, make_client
        key, funder = os.environ.get("POLYMARKET_PRIVATE_KEY"), os.environ.get("POLYMARKET_FUNDER")
        if not funder:
            sys.exit("Set POLYMARKET_FUNDER (the address holding the funds) first.")
        if live and not key:
            sys.exit("--live needs POLYMARKET_PRIVATE_KEY.")
        client = make_client(key, funder, int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "1"))) if key else None
        return PolymarketVenue(cfg["basket"], funder, client, paper_cash=float(cfg.get("paper_cash", 0)))
    if kind == "supermarket":
        from .adapters.supermarket import SupermarketVenue
        key = os.environ.get("SUPERMARKET_API_KEY")
        if not key:
            sys.exit("Set the SUPERMARKET_API_KEY environment variable first.")
        return SupermarketVenue(key, {e["title"]: float(e["reference"]) for e in cfg["basket"]},
                                slug=cfg.get("slug", "midterm-elections"))
    sys.exit(f"unknown venue {kind!r}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="basket_exec", description=__doc__.splitlines()[0])
    ap.add_argument("--config", required=True)
    ap.add_argument("--live", action="store_true", help="send orders for real")
    ap.add_argument("--stop", action="store_true", help="cancel the strategy's open orders and exit")
    a = ap.parse_args(argv)
    with open(a.config, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    runner = Runner(build_venue(raw, a.live), Config.from_dict(raw["strategy"]), live=a.live)
    if a.stop:
        runner.stop()
    else:
        runner.loop(float(raw.get("round_seconds", 60)))


if __name__ == "__main__":
    main()
