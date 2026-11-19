"""Historical trades per market from the Data API v2, with the wallet behind each trade.

The websocket tape (last_trade_price) has no wallet; signal S2 (following the metaorders of
well-rated wallets) needs one, and the Data API has it. This module downloads it, market by market,
into trades/<condition_id>.jsonl.gz, one line per trade:

  {"f": <fetch time ms>, "t": <normalised trade>, "raw": <the row as the API sent it>}

The raw row is kept so a field renamed or added by Polymarket is never lost. Runs are incremental:
trades/_state.json remembers, per market, the newest trade time and the keys of the trades at that
edge, and the download stops at the first page that only holds trades already known.

Data API v2 /v2/trades, probed live from the server on 2026-11-19:
  - filters: condition=<condition id> (market= is rejected with 400), token=, user=, side=;
    limit up to 1000; next page with cursor=<next_cursor>; offset= is rejected.
  - no time filter at all (start/end/after/before... are silently ignored), newest first: a
    backfill pages until it reaches a trade older than the target.
  - rows: proxy_wallet, side, token_id, condition_id, size, price (numbers), timestamp (unix
    seconds), transaction_hash, outcome, outcome_index, title, slug, event_slug, plus profile fields.
Anything unexpected raises instead of writing a silent partial.
"""
import gzip
import json
import logging
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from .store import write_json

DATA = "https://data-api.polymarket.com"
ENDPOINT = "/v2/trades"
log = logging.getLogger("book_recorder.trades")


def _pick(r: dict, *names):
    for n in names:
        if r.get(n) is not None:
            return r[n]
    return None


def _seconds(v) -> Optional[float]:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        from datetime import datetime
        try:
            return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return x / 1000 if x > 1e11 else x


def normalise(r: dict) -> dict:
    """One trade row, v2 names first, v1 names as a fallback."""
    t = {
        "token": _pick(r, "token_id", "asset", "asset_id"),
        "market": _pick(r, "condition_id", "conditionId", "market"),
        "wallet": _pick(r, "proxy_wallet", "wallet", "user", "proxyWallet", "maker_address"),
        "side": _pick(r, "side"),
        "price": _pick(r, "price"),
        "size": _pick(r, "size", "shares"),
        "ts": _seconds(_pick(r, "timestamp", "match_time", "created_at", "time")),
        "tx": _pick(r, "transaction_hash", "transactionHash", "tx_hash"),
        "outcome": _pick(r, "outcome"),
    }
    if t["token"] is None or t["ts"] is None or t["price"] is None:
        raise RuntimeError(f"Data API trade row not understood: fields {sorted(r)}")
    t["token"] = str(t["token"])
    t["price"], t["size"] = float(t["price"]), float(t["size"] or 0)
    return t


# Profile fields repeated on every row (bio, pictures): not trade data, and they dominate the bytes.
DROP = ("bio", "profile_image", "profile_image_optimized", "icon", "profileImage", "profileImageOptimized")


def key(t: dict) -> str:
    return "|".join(str(t.get(k)) for k in ("tx", "token", "wallet", "side", "price", "size", "ts"))


class TradeDownloader:
    def __init__(self, out, get: Callable[..., object], endpoint: str = ENDPOINT, limit: int = 1000,
                 max_pages: int = 500, clock: Callable[[], float] = time.time):
        self.dir = Path(out) / "trades"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.get, self.url, self.limit, self.max_pages, self.clock = get, DATA + endpoint, limit, max_pages, clock
        self.state_path = self.dir / "_state.json"
        try:
            self.state: Dict[str, dict] = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.state = {}

    def download(self, condition_id: str, since: Optional[float] = None) -> int:
        """Fetch the market's trades newer than what is already stored (and than `since`, seconds).
        Returns the number of new trades written.

        Rows are written page by page, so memory stays flat whatever the market's history (the
        first run on the server pulled 414,000 trades). Pages come newest first, so a run writes
        its trades newest first: sort by `t.ts` when reading. The state is saved once the run ends;
        a run killed halfway is redone from the same point next time, which can repeat some rows
        (`iter_trades(..., dedupe=True)` drops them)."""
        st = self.state.get(condition_id, {})
        newest, edge = st.get("newest_ts"), set(st.get("edge_keys", []))
        floor = max(x for x in (newest, since, float("-inf")) if x is not None)
        written, top, top_keys, prev_keys, cursor, f = 0, None, set(), set(), None, None
        now = int(self.clock() * 1000)
        try:
            for _ in range(self.max_pages):
                params = {"condition": condition_id, "limit": self.limit}
                if cursor:
                    params["cursor"] = cursor
                page = self.get(self.url, **params)
                if not isinstance(page, dict) or not isinstance(page.get("data"), list):
                    raise RuntimeError(f"unexpected answer from {self.url}: {str(page)[:200]}")
                rows = [(normalise(r), r) for r in page["data"]]
                keys = [key(t) for t, _ in rows]
                if rows and set(keys) <= prev_keys:
                    raise RuntimeError("Data API pagination is not advancing: trades would be duplicated")
                prev_keys = set(keys)
                older, below_floor = 0, False
                for (t, raw), k in zip(rows, keys):
                    if k in edge:
                        older += 1
                        continue
                    if t["ts"] < floor:
                        older += 1
                        below_floor = True
                        continue
                    if f is None:
                        f = gzip.open(self.dir / f"{condition_id}.jsonl.gz", "ab")
                    slim = {k2: v for k2, v in raw.items() if k2 not in DROP}
                    f.write((json.dumps({"f": now, "t": t, "raw": slim}, separators=(",", ":")) + "\n").encode())
                    written += 1
                    if top is None or t["ts"] > top:
                        top, top_keys = t["ts"], {k}
                    elif t["ts"] == top:
                        top_keys.add(k)
                pagination = page.get("pagination") or {}
                cursor = pagination.get("next_cursor")
                if not pagination.get("has_more") or not cursor:
                    break
                if below_floor or (rows and older == len(rows)):   # newest first: everything after is older
                    break
            else:
                log.warning("%s: stopped after %d pages, older trades not fetched", condition_id, self.max_pages)
        finally:
            if f is not None:
                f.close()
        if written:
            if newest is None or top > newest:
                newest, edge = top, top_keys
            elif top == newest:
                edge |= top_keys
            self.state[condition_id] = {"newest_ts": newest, "edge_keys": sorted(edge)}
            write_json(self.state_path, self.state)
        return written

    def download_all(self, condition_ids: Iterable[str], since: Optional[float] = None) -> Dict[str, object]:
        """One bad market does not stop the others; the summary says which failed."""
        done = {}
        for cid in condition_ids:
            try:
                done[cid] = self.download(cid, since)
            except Exception as e:
                log.error("%s: %s", cid, e)
                done[cid] = f"error: {type(e).__name__}: {e}"[:300]
        return done


def recorded_markets(out, days: int = 2, today: Optional[str] = None) -> List[str]:
    """Condition ids of every market the recorder followed in the last `days` day files."""
    files = sorted((Path(out) / "markets").glob("*.json"))
    if today:
        files = [f for f in files if f.stem <= today]
    ids = []
    for f in files[-days:]:
        try:
            for gen in json.loads(f.read_text(encoding="utf-8")):
                for m in gen["markets"]:
                    if m["condition_id"] not in ids:
                        ids.append(m["condition_id"])
        except (OSError, ValueError, KeyError):
            continue
    return ids


def iter_trades(path, dedupe: bool = False) -> Iterable[dict]:
    """Normalised trades of one market file, in file order (newest first within each run)."""
    from .replay import iter_lines
    seen = set()
    for line in iter_lines(path):
        if dedupe:
            k = key(line["t"])
            if k in seen:
                continue
            seen.add(k)
        yield line["t"]
