"""Market metadata straight from Gamma, by slug: the shadow and live modes do not depend on the
recorder's market list (a chosen market outside its top 100 would otherwise be missing)."""
from typing import Callable, Dict, List

import requests

from book_recorder.markets import GAMMA, _num, normalise


def http_get(url, **params):
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def fetch(slugs: List[str], get: Callable = http_get) -> Dict[str, dict]:
    """token id -> market dict, the format of smart.feed.load_markets, plus what orders need:
    tick, min_size, neg_risk, accepting."""
    out = {}
    for slug in slugs:
        rows = get(f"{GAMMA}/markets", slug=slug)
        if isinstance(rows, dict):
            rows = rows.get("data") or []
        for raw in rows or []:
            m = normalise(raw)
            if not m or m["slug"] != slug:
                continue
            m.update(tick=_num(raw.get("orderPriceMinTickSize")) or 0.01,
                     min_size=_num(raw.get("orderMinSize")) or 5.0,
                     accepting=raw.get("acceptingOrders") is not False and not raw.get("closed"))
            tokens = [t["token_id"] for t in m["tokens"]]
            for t in m["tokens"]:
                out[t["token_id"]] = {**m, "token_id": t["token_id"], "outcome": t.get("outcome"),
                                      "complement": next((x for x in tokens if x != t["token_id"]), None)
                                      if len(tokens) == 2 else None}
            break
    return out
