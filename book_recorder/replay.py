"""Read recorder files back: raw lines, Polymarket events, L2 books and the trade tape.

    for ev in iter_events("data/book/2026-11-19.jsonl.gz"):      # flattened, with receive time
        ...
    books = Books()
    for ev in iter_events(path):
        trade = books.apply(ev)          # updates the L2 book of ev's token, returns a Trade or None
        books[token].best_bid(), books[token].mid(), books[token].depth("ask", 0.05) ...

Both price_change layouts are handled: the current one (a `price_changes` list with an asset_id
per entry) and the older one (one `asset_id` with a `changes` list). Sizes in book and
price_change messages are absolute (the new aggregate size at that price, 0 removes the level),
so applying a message twice is harmless. Trades can be duplicated when two connections overlap
during a rotation: `Books.apply` drops a trade seen in the last few seconds with the same token,
time, price, size and side.
"""
import gzip
import json
import zlib
from collections import Counter, OrderedDict
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Tuple

from .compact import Expander


def _inflate(data: bytes, chunk: int = 1 << 16) -> Iterator[bytes]:
    """Decompressed bytes of concatenated gzip members, up to the first corrupt byte if any."""
    while data:
        d = zlib.decompressobj(zlib.MAX_WBITS | 16)
        pos = 0
        while pos < len(data) and not d.eof:
            piece = data[pos:pos + chunk]
            backup = d.copy()
            try:
                yield d.decompress(piece)
            except zlib.error:
                # A crash left a damaged tail: decode what precedes it byte by byte, then stop.
                d = backup
                for i in range(len(piece)):
                    try:
                        yield d.decompress(piece[i:i + 1])
                    except zlib.error:
                        return
                return
            pos += len(piece)
        if not d.eof:
            return                              # truncated member: nothing after it
        data = d.unused_data + data[pos:] if d.unused_data else data[pos:]


def iter_lines(path) -> Iterator[dict]:
    """Every complete JSON line of a (possibly truncated, possibly multi-member) gzip file."""
    with open(path, "rb") as f:
        data = f.read()
    buf = b""
    for out in _inflate(data):
        buf += out
        *complete, buf = buf.split(b"\n")
        for raw in complete:
            if raw:
                try:
                    yield json.loads(raw)
                except ValueError:          # a line cut by a crash
                    continue


def iter_events(path, with_meta: bool = False) -> Iterator[dict]:
    """Polymarket messages, one dict each, with `_r` (receive ms) and `_c` (connection) added.
    With `with_meta`, connection events are yielded too, as {"_r", "_c", "_e", ...}.
    Compacted books ("bd" lines) come back as full "book" events, marked "_diff": True."""
    books = Expander()
    for line in iter_lines(path):
        if "m" in line:
            m = line["m"]
            for ev in (m if isinstance(m, list) else [m]):
                if isinstance(ev, dict):
                    if ev.get("event_type") == "book":
                        books.full(line["c"], ev)
                    ev["_r"], ev["_c"] = line["r"], line["c"]
                    yield ev
        elif "bd" in line:
            ev = books.expand(line["c"], line["bd"])
            if ev is not None:
                ev["_r"], ev["_c"] = line["r"], line["c"]
                yield ev
        elif with_meta and "e" in line:
            meta = {k: v for k, v in line.items() if k not in ("r", "c", "e")}
            yield {"_r": line["r"], "_c": line["c"], "_e": line["e"], **meta}


def _px(p) -> float:
    return round(float(p), 6)


class L2:
    """Aggregate book of one token: price -> size on each side."""

    def __init__(self):
        self.bids: Dict[float, float] = {}
        self.asks: Dict[float, float] = {}
        self.tick: Optional[float] = None
        self.ts: Optional[int] = None         # exchange timestamp (ms) of the last update
        self.snapshots = 0

    def side(self, name: str) -> Dict[float, float]:
        return self.bids if name.upper() in ("BUY", "BID", "BIDS") else self.asks

    def snapshot(self, bids, asks):
        self.bids = {_px(l["price"]): float(l["size"]) for l in bids or [] if float(l["size"]) > 0}
        self.asks = {_px(l["price"]): float(l["size"]) for l in asks or [] if float(l["size"]) > 0}
        self.snapshots += 1

    def set(self, side: str, price, size):
        book, p, s = self.side(side), _px(price), float(size)
        if s > 0:
            book[p] = s
        else:
            book.pop(p, None)

    def best_bid(self) -> Optional[float]:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> Optional[float]:
        return min(self.asks) if self.asks else None

    def mid(self) -> Optional[float]:
        b, a = self.best_bid(), self.best_ask()
        return (a + b) / 2 if a is not None and b is not None else None

    def spread(self) -> Optional[float]:
        b, a = self.best_bid(), self.best_ask()
        return a - b if a is not None and b is not None else None

    def depth(self, side: str, within: float) -> float:
        """Shares resting within `within` of the best price on that side."""
        if side.lower().startswith("b"):
            b = self.best_bid()
            return sum(s for p, s in self.bids.items() if b is not None and p >= b - within - 1e-9)
        a = self.best_ask()
        return sum(s for p, s in self.asks.items() if a is not None and p <= a + within + 1e-9)

    def levels(self, side: str, n: int = 10) -> List[Tuple[float, float]]:
        if side.lower().startswith("b"):
            return sorted(self.bids.items(), reverse=True)[:n]
        return sorted(self.asks.items())[:n]


@dataclass
class Trade:
    recv_ms: int
    ts: int                 # exchange timestamp, ms
    token: str
    market: Optional[str]
    price: float
    size: float
    side: Optional[str]     # as sent by Polymarket ("BUY" / "SELL")
    fee_rate_bps: Optional[float]
    tx: Optional[str] = None


def _ts(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class Books(dict):
    """token id -> L2, kept up to date by `apply`."""

    def __init__(self, dedupe_ms: int = 10_000):
        super().__init__()
        self.dedupe_ms = dedupe_ms
        self._recent: "OrderedDict[tuple, int]" = OrderedDict()
        self.unknown = Counter()

    def book(self, token: str) -> L2:
        if token not in self:
            self[token] = L2()
        return self[token]

    def apply(self, ev: dict) -> Optional[Trade]:
        kind = ev.get("event_type")
        ts = _ts(ev.get("timestamp"))
        if kind == "book":
            b = self.book(str(ev["asset_id"]))
            b.snapshot(ev.get("bids", ev.get("buys")), ev.get("asks", ev.get("sells")))
            b.ts = ts
        elif kind == "price_change":
            if "price_changes" in ev:
                for c in ev["price_changes"]:
                    b = self.book(str(c["asset_id"]))
                    b.set(c["side"], c["price"], c["size"])
                    b.ts = ts
            else:
                b = self.book(str(ev["asset_id"]))
                for c in ev.get("changes", []):
                    b.set(c["side"], c["price"], c["size"])
                b.ts = ts
        elif kind == "tick_size_change":
            self.book(str(ev["asset_id"])).tick = float(ev.get("new_tick_size"))
        elif kind == "last_trade_price":
            t = Trade(recv_ms=ev.get("_r"), ts=ts, token=str(ev["asset_id"]), market=ev.get("market"),
                      price=float(ev["price"]), size=float(ev.get("size") or 0), side=ev.get("side"),
                      fee_rate_bps=float(ev["fee_rate_bps"]) if ev.get("fee_rate_bps") not in (None, "") else None,
                      tx=ev.get("transaction_hash"))
            key = (t.token, t.ts, t.price, t.size, t.side, t.tx)
            now = ev.get("_r") or 0
            while self._recent and next(iter(self._recent.values())) < now - self.dedupe_ms:
                self._recent.popitem(last=False)
            if key in self._recent:
                return None
            self._recent[key] = now
            return t
        else:
            self.unknown[kind] += 1
        return None


def stats(path) -> dict:
    """What a day file holds: counts per event type, tokens, connection events, gaps, trades."""
    kinds, meta, closes = Counter(), Counter(), Counter()
    first = last = None
    tokens, books, trades, notional = set(), Books(), 0, 0.0
    for ev in iter_events(path, with_meta=True):
        r = ev["_r"]
        first = r if first is None else first
        last = r
        if "_e" in ev:
            meta[ev["_e"]] += 1
            if ev["_e"] == "close":
                closes[ev.get("why", "?").split(":")[0]] += 1
            continue
        kinds[ev.get("event_type")] += 1
        if ev.get("asset_id"):
            tokens.add(str(ev["asset_id"]))
        for c in ev.get("price_changes") or []:
            tokens.add(str(c.get("asset_id")))
        t = books.apply(ev)
        if t:
            trades += 1
            notional += t.price * t.size
    two_sided = sum(1 for b in books.values() if b.bids and b.asks)
    return {"file": str(path), "from_ms": first, "to_ms": last,
            "hours": round((last - first) / 3.6e6, 2) if first is not None else 0,
            "events": dict(kinds), "connection_events": dict(meta), "close_reasons": dict(closes),
            "tokens_seen": len(tokens), "books_two_sided": two_sided,
            "trades": trades, "trade_notional_usdc": round(notional, 2)}
