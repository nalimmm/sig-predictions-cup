"""Which markets to record: the most active open markets on Gamma, filtered for books worth having.

Gamma returns `clobTokenIds` and `outcomes` as JSON strings; everything is normalised here so the
recorder only sees plain dicts:

  {"condition_id", "slug", "question", "event_slug", "neg_risk", "end_date", "volume_24h",
   "liquidity", "tokens": [{"token_id", "outcome"}]}
"""
import json
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, List, Optional

GAMMA = "https://gamma-api.polymarket.com"


def _load(v):
    if v is None:
        return []
    return json.loads(v) if isinstance(v, str) else list(v)


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _date(v) -> Optional[datetime]:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def normalise(m: dict) -> Optional[dict]:
    """One Gamma market as the recorder wants it, or None if it has no tradable book."""
    tokens, outcomes = [str(t) for t in _load(m.get("clobTokenIds"))], _load(m.get("outcomes"))
    if not tokens or not m.get("conditionId"):
        return None
    events = m.get("events") or []
    end = _date(m.get("endDate") or m.get("endDateIso"))
    return {
        "condition_id": m["conditionId"],
        "slug": m.get("slug"),
        "question": m.get("question"),
        "event_slug": events[0].get("slug") if events and isinstance(events[0], dict) else None,
        "neg_risk": bool(m.get("negRisk")),
        "end_date": end.isoformat() if end else None,
        "volume_24h": _num(m.get("volume24hr") if m.get("volume24hr") is not None else m.get("volume24hrClob")),
        "liquidity": _num(m.get("liquidityNum") if m.get("liquidityNum") is not None else m.get("liquidity")),
        "tokens": [{"token_id": t, "outcome": str(o) if o is not None else None}
                   for t, o in zip(tokens, outcomes + [None] * (len(tokens) - len(outcomes)))],
    }


def eligible(m: dict, raw: dict, now: datetime, min_hours_left: float, min_liquidity: float) -> bool:
    if raw.get("closed") or raw.get("archived") or raw.get("active") is False:
        return False
    if raw.get("enableOrderBook") is False or raw.get("acceptingOrders") is False:
        return False
    if m["liquidity"] < min_liquidity:
        return False
    # Very short markets (15 minute and hourly crypto candles) top the volume ranking but churn
    # every hour: they would replace most of the set at each refresh. Excluded by default.
    end = _date(m["end_date"])
    if end is not None and end < now + timedelta(hours=min_hours_left):
        return False
    return True


def select(get: Callable[..., object], n: int = 100, min_hours_left: float = 24.0,
           min_liquidity: float = 1000.0, max_per_event: Optional[int] = None,
           pinned: Iterable[str] = (), now: Optional[datetime] = None, page: int = 100,
           max_pages: int = 10) -> List[dict]:
    """The `n` most active eligible markets, by 24h volume. `get(url, **params)` returns parsed JSON.

    `pinned` lists slugs or condition ids that are always included (and count towards `n`).
    `max_per_event` caps how many markets one event (one neg-risk group) may take."""
    now = now or datetime.now(timezone.utc)
    pinned = {p for p in pinned}
    chosen, per_event, seen = [], {}, set()
    pinned_found = []
    for i in range(max_pages):
        rows = get(f"{GAMMA}/markets", active="true", closed="false", archived="false",
                   order="volume24hr", ascending="false", limit=page, offset=i * page)
        if isinstance(rows, dict):               # tolerate a {"data": [...]} wrapper
            rows = rows.get("data") or []
        if not rows:
            break
        for raw in rows:
            m = normalise(raw)
            if m is None or m["condition_id"] in seen:
                continue
            seen.add(m["condition_id"])
            if m["slug"] in pinned or m["condition_id"] in pinned:
                pinned_found.append(m)
                continue
            if not eligible(m, raw, now, min_hours_left, min_liquidity):
                continue
            ev = m["event_slug"] or m["condition_id"]
            if max_per_event and per_event.get(ev, 0) >= max_per_event:
                continue
            per_event[ev] = per_event.get(ev, 0) + 1
            chosen.append(m)
        if len(chosen) + len(pinned_found) >= n and len(pinned_found) == len(pinned):
            break
        if len(rows) < page:
            break
    missing = pinned - {m["slug"] for m in pinned_found} - {m["condition_id"] for m in pinned_found}
    for p in sorted(missing):                    # pinned markets outside the active ranking
        rows = get(f"{GAMMA}/markets", **({"condition_ids": p} if p.startswith("0x") else {"slug": p}))
        if isinstance(rows, dict):
            rows = rows.get("data") or []
        for raw in rows or []:
            m = normalise(raw)
            if m and not raw.get("closed"):
                pinned_found.append(m)
                break
    return pinned_found + chosen[:max(0, n - len(pinned_found))]


def asset_ids(markets: List[dict]) -> List[str]:
    return [t["token_id"] for m in markets for t in m["tokens"]]
