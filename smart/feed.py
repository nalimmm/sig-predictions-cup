"""Read the recorder's day files as a stream of Polymarket events, for a backtest or live.

The Smart pod does not open its own websocket: book_recorder already follows the 100 most
active markets and flushes its day file every few seconds (Z_SYNC_FLUSH), so the paper trader
reads the same bytes the backtest reads. One code path for both, and no second connection on a
one core server.

`DayFile` decompresses a day file incrementally, in bounded memory (the whole-file reader in
book_recorder.replay would load a 1 GB day into RAM). It copes with the three things a file
written by a live, restartable recorder can hold:

  - several gzip members (one per recorder start);
  - a member cut short by a crash, followed by a new member: decoding resumes at the next gzip
    header;
  - an incomplete tail that is still being written: kept until the next read.

Events come out exactly as `book_recorder.replay.iter_events` yields them: Polymarket messages
with `_r` (receive time, ms) and `_c` (connection) added, compacted books expanded back to full
"book" events, and connection markers as {"_e": "open" | "close" | "markets" ...}.
"""
import json
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, List, Optional

from book_recorder.compact import Expander

GZIP_MAGIC = b"\x1f\x8b\x08"


def utc_day(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")


class DayFile:
    """Incremental reader of one book/<day>.jsonl.gz file."""

    def __init__(self, path, chunk: int = 1 << 20, stall_bytes: int = 4 << 20):
        self.path = Path(path)
        self.chunk = chunk
        self.stall_bytes = stall_bytes  # this much input without a complete line = a dead member
        self.pos = 0                    # bytes of the file consumed so far
        self._z = self._new()
        self._buf = b""                 # decompressed bytes after the last newline
        self._anchor = 0                # where a resync starts looking: start of the last good chunk
        self._since_line = 0            # compressed bytes fed since the last complete line
        self._lost = False              # after a crash, until the next member shows up
        self._expander = Expander()     # compaction state is per day file
        self.skipped = 0                # corrupt regions jumped over
        self.resyncs = 0

    @staticmethod
    def _new():
        return zlib.decompressobj(zlib.MAX_WBITS | 16)

    def _resync(self, f) -> bool:
        """A crash cut a member short and a restart appended a new one: the deflate stream of
        the old member never ends. Find the next gzip header after the last good chunk whose
        first bytes decode to a recorder line (a bare magic number can occur inside deflate
        data), and restart there. False while no such member has been written yet."""
        f.seek(self._anchor + 1)
        data = f.read()
        i = data.find(GZIP_MAGIC)
        while i >= 0:
            try:
                head = self._new().decompress(data[i:i + 65536])
            except zlib.error:
                head = b""
            if head.startswith(b'{"r":'):
                if not self._lost:
                    self.skipped += 1
                self.pos = self._anchor = self._anchor + 1 + i
                self._z, self._buf, self._since_line, self._lost = self._new(), b"", 0, False
                self.resyncs += 1
                return True
            i = data.find(GZIP_MAGIC, i + 1)
        if not self._lost:
            self.skipped += 1
        self._lost = True
        self.pos = self._anchor
        return False

    def read_lines(self) -> List[bytes]:
        """Complete lines from the next chunk of the file (at most `chunk` compressed bytes, so
        memory stays bounded even when catching up on a whole day). Empty at the end of the file."""
        out: List[bytes] = []
        if not self.path.exists():
            return out
        with open(self.path, "rb") as f:
            if self._lost and not self._resync(f):
                return out
            f.seek(self.pos)
            data = f.read(self.chunk)
            if not data:
                return out
            start = self.pos
            self.pos += len(data)
            while data:
                try:
                    text = self._z.decompress(data)
                except zlib.error:
                    self._resync(f)
                    return out
                self._buf += text
                if self._z.eof:                         # end of one member: the next one follows
                    data = self._z.unused_data
                    self._z = self._new()
                else:
                    data = b""
            *complete, self._buf = self._buf.split(b"\n")
            out.extend(c for c in complete if c)
            if out:
                self._anchor, self._since_line = start, 0
            else:
                self._since_line += self.pos - start
                if self._since_line > self.stall_bytes:
                    self._resync(f)
        return out

    def events(self, with_meta: bool = True, to_end: bool = True) -> Iterator[dict]:
        """Events of the next chunk, or (to_end) of every chunk up to the current end of file."""
        while True:
            before = (self.pos, self.resyncs)
            for raw in self.read_lines():
                try:
                    line = json.loads(raw)
                except ValueError:
                    continue
                yield from self.expand(line, with_meta)
            if not to_end or (self.pos, self.resyncs) == before:
                return

    def expand(self, line: dict, with_meta: bool = True) -> Iterator[dict]:
        if "m" in line:
            m = line["m"]
            for ev in (m if isinstance(m, list) else [m]):
                if isinstance(ev, dict):
                    if ev.get("event_type") == "book":
                        self._expander.full(line["c"], ev)
                    ev["_r"], ev["_c"] = line["r"], line["c"]
                    yield ev
        elif "bd" in line:
            ev = self._expander.expand(line["c"], line["bd"])
            if ev is not None:
                ev["_r"], ev["_c"] = line["r"], line["c"]
                yield ev
        elif with_meta and "e" in line:
            meta = {k: v for k, v in line.items() if k not in ("r", "c", "e")}
            yield {"_r": line["r"], "_c": line["c"], "_e": line["e"], **meta}


def replay(book_dir, days: List[str]) -> Iterator[dict]:
    """Every event of the given days, in order (backtest)."""
    for day in days:
        path = Path(book_dir) / f"{day}.jsonl.gz"
        if not path.exists():
            continue
        yield {"_r": None, "_e": "day", "day": day}
        yield from DayFile(path).events()


def follow(book_dir, start_day: Optional[str] = None, poll: float = 1.0,
           clock: Callable[[], float] = time.time, sleep=time.sleep,
           stop: Callable[[], bool] = lambda: False) -> Iterator[dict]:
    """Today's events from the start of the file, then new ones as the recorder writes them.

    Yields {"_e": "idle", "_now": ms} after each empty poll so the caller can run its clock-driven
    checks (stale data, timeouts) while the market is quiet. Moves to the next day file at UTC
    midnight once the old one has been read to its end."""
    day = start_day or utc_day(clock() * 1000)
    cur = DayFile(Path(book_dir) / f"{day}.jsonl.gz")
    yield {"_r": None, "_e": "day", "day": day}
    while not stop():
        before = (cur.pos, cur.resyncs)
        for ev in cur.events(to_end=False):
            yield ev
        if (cur.pos, cur.resyncs) != before:
            continue
        today = utc_day(clock() * 1000)
        nxt = Path(book_dir) / f"{today}.jsonl.gz"
        if today > day and nxt.exists():
            for ev in cur.events():             # whatever the old file still had
                yield ev
            day, cur = today, DayFile(nxt)
            yield {"_r": None, "_e": "day", "day": day}
            continue
        yield {"_r": None, "_e": "idle", "_now": int(clock() * 1000)}
        sleep(poll)


def load_markets(data_dir, day: str) -> dict:
    """token id -> market dict (with the token's outcome and its sibling tokens), from the
    recorder's markets/<day>.json (last set in force that day; earlier sets fill gaps)."""
    path = Path(data_dir) / "markets" / f"{day}.json"
    try:
        history = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for entry in history:                       # later generations override earlier ones
        for m in entry.get("markets", []):
            tokens = [t["token_id"] for t in m.get("tokens", [])]
            for t in m.get("tokens", []):
                out[t["token_id"]] = {**m, "token_id": t["token_id"], "outcome": t.get("outcome"),
                                      "complement": next((x for x in tokens if x != t["token_id"]), None)
                                      if len(tokens) == 2 else None}
    return out
