"""Daily gzip JSON-lines files that survive crashes and restarts.

Layout under the output directory:

  book/2026-11-19.jsonl.gz      every websocket message, one line each (see ws.py for the format)
  markets/2026-11-19.json       the market set in force, rewritten at each refresh
  trades/<condition_id>.jsonl.gz  historical trades from the Data API (trades.py)

A day file is opened in append mode: each (re)start adds a new gzip member, and gzip readers
(`zcat`, Python's gzip module) read concatenated members as one stream. The writer flushes with
Z_SYNC_FLUSH every few seconds, so a crash or a power cut loses at most those seconds; the
truncated tail is skipped by `replay.iter_lines`.
"""
import gzip
import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional


def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


class DailyWriter:
    """Appends lines to <root>/<kind>/<UTC day>.jsonl.gz and rolls over at midnight UTC."""

    def __init__(self, root, kind: str = "book", flush_seconds: float = 5.0, level: int = 6,
                 clock: Callable[[], float] = time.time):
        self.dir = Path(root) / kind
        self.dir.mkdir(parents=True, exist_ok=True)
        self.flush_seconds, self.level, self.clock = flush_seconds, level, clock
        self.day: Optional[str] = None
        self._f = None
        self._last_flush = 0.0
        self.lines = 0
        self.bytes = 0

    def path(self, day: str) -> Path:
        return self.dir / f"{day}.jsonl.gz"

    def _open(self, day: str):
        self.close()
        self._f = gzip.open(self.path(day), "ab", compresslevel=self.level)
        self.day = day

    def write(self, line: str, now: Optional[float] = None) -> bool:
        """Write one line; returns True when this line opened a new day file."""
        now = self.clock() if now is None else now
        day, rolled = utc_day(now), False
        if day != self.day:
            rolled = self.day is not None
            self._open(day)
        data = line.encode("utf-8") + b"\n"
        self._f.write(data)
        self.lines += 1
        self.bytes += len(data)
        if now - self._last_flush >= self.flush_seconds:
            self.flush(now)
        return rolled

    def flush(self, now: Optional[float] = None):
        if self._f is not None:
            self._f.flush()          # Z_SYNC_FLUSH: everything so far is decodable on disk
            try:
                os.fsync(self._f.fileobj.fileno())
            except (AttributeError, OSError, ValueError):
                pass
        self._last_flush = self.clock() if now is None else now

    def close(self):
        if self._f is not None:
            self._f.close()
            self._f = None
        self.day = None


def write_json(path, payload) -> None:
    """Atomic write: a reader never sees half a file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def free_gb(path) -> float:
    return shutil.disk_usage(path).free / 1e9
