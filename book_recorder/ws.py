"""Record the Polymarket CLOB websocket market channel to daily files.

Protocol (public, no authentication):
  connect  wss://ws-subscriptions-clob.polymarket.com/ws/market
  send     {"assets_ids": [...token ids...], "type": "market"}
  keepalive: send the text "PING" every 10 s, the server answers "PONG"
  receive  JSON objects, or JSON arrays of them (the initial snapshot), with an `event_type` of
           "book" (full snapshot of one token), "price_change" (new aggregate size at some levels),
           "last_trade_price" (a trade), "tick_size_change", and a few others.

Every message is stored as received, without parsing, so nothing is lost if Polymarket changes a
field: parsing belongs to replay.py, which can be fixed and rerun on old files. One line per
message:

  {"r": 1763510400123, "c": "s0g3", "m": <the message as sent by Polymarket>}

and one line per connection event, so gaps are visible in the data instead of silently missing:

  {"r": ..., "c": "s0g3", "e": "open" | "close" | "subscribe" | "markets" | "disk_low" | "disk_ok", ...}

`r` is the local receive time in ms (UTC), `c` the connection: shard number and generation.

Robustness:
  - the token list is split into shards (50 tokens per connection by default), each with its own
    reconnect loop and exponential backoff;
  - a connection silent for `stale_seconds` is dropped and reopened;
  - the market set is re-selected at each UTC midnight (and every `refresh_hours` if set). The new
    connections are opened *before* the old ones are closed, so a rotation leaves no gap, and each
    day file starts with fresh "book" snapshots of every token, so a day file can be replayed alone;
  - below `min_free_gb` of free disk, messages are dropped (with a "disk_low" marker) instead of
    filling the disk and taking the trading services down with it.
"""
import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .markets import asset_ids
from .store import DailyWriter, free_gb, utc_day, write_json

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
log = logging.getLogger("book_recorder")


def default_connect(url):
    import websockets
    # Our own PING text frames are the keepalive Polymarket asks for; protocol pings are off so a
    # slow pong on a busy single core does not drop the connection.
    return websockets.connect(url, ping_interval=None, max_size=None, open_timeout=20, close_timeout=5)


def chunks(items: List[str], size: int) -> List[List[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


class Shard:
    """One websocket connection for a subset of tokens, reconnecting until stopped."""

    def __init__(self, rec: "Recorder", cid: str, assets: List[str]):
        self.rec, self.cid, self.assets = rec, cid, assets
        self.ready = asyncio.Event()      # set on the first message after (re)connecting
        self.messages = 0
        self.task: Optional[asyncio.Task] = None

    def start(self):
        self.task = asyncio.ensure_future(self.run())
        return self

    async def stop(self):
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def _pinger(self, ws):
        while True:
            await asyncio.sleep(self.rec.ping_seconds)
            await ws.send("PING")

    async def run(self):
        rec, backoff = self.rec, 1.0
        while True:
            pinger = None
            try:
                async with rec.connect(rec.url) as ws:
                    rec.event(self.cid, "open", n=len(self.assets))
                    await ws.send(json.dumps({"assets_ids": self.assets, "type": "market"}))
                    rec.event(self.cid, "subscribe", n=len(self.assets))
                    pinger = asyncio.ensure_future(self._pinger(ws))
                    while True:
                        msg = await asyncio.wait_for(ws.recv(), timeout=rec.stale_seconds)
                        if isinstance(msg, bytes):
                            msg = msg.decode("utf-8", "replace")
                        if msg == "PONG":
                            continue
                        self.messages += 1
                        rec.record(self.cid, msg)
                        if not self.ready.is_set():
                            self.ready.set()
                        backoff = 1.0
            except asyncio.CancelledError:
                rec.event(self.cid, "close", why="stopped")
                raise
            except asyncio.TimeoutError:
                rec.event(self.cid, "close", why=f"silent for {rec.stale_seconds:.0f}s")
            except Exception as e:  # network errors, server closes, handshake failures
                rec.event(self.cid, "close", why=f"{type(e).__name__}: {e}"[:300])
            finally:
                if pinger:
                    pinger.cancel()
            log.warning("%s disconnected, reconnecting in %.0fs", self.cid, backoff)
            await rec.sleep(backoff)
            backoff = min(backoff * 2, rec.max_backoff)


class Recorder:
    def __init__(self, out, select: Callable[[], List[dict]], connect: Callable = default_connect,
                 url: str = WS_URL, shard_size: int = 50, refresh_hours: float = 0.0,
                 ping_seconds: float = 10.0, stale_seconds: float = 120.0, max_backoff: float = 60.0,
                 min_free_gb: float = 2.0, ready_timeout: float = 30.0, stats_seconds: float = 300.0,
                 clock: Callable[[], float] = time.time, sleep=asyncio.sleep, writer: DailyWriter = None):
        self.out = Path(out)
        self.select, self.connect, self.url = select, connect, url
        self.shard_size, self.refresh_hours = shard_size, refresh_hours
        self.ping_seconds, self.stale_seconds, self.max_backoff = ping_seconds, stale_seconds, max_backoff
        self.min_free_gb, self.ready_timeout, self.stats_seconds = min_free_gb, ready_timeout, stats_seconds
        self.clock, self.sleep = clock, sleep
        self.writer = writer or DailyWriter(self.out, "book", clock=clock)
        self.shards: List[Shard] = []
        self.markets: List[dict] = []
        self.generation = 0
        self.disk_low = False
        self.dropped = 0

    # ------------------------------------------------------------ writing
    def _ms(self) -> int:
        return int(self.clock() * 1000)

    def record(self, cid: str, raw: str):
        if self.disk_low:
            self.dropped += 1
            return
        raw = raw.strip()
        if raw and raw[0] in "{[":
            line = '{"r":%d,"c":"%s","m":%s}' % (self._ms(), cid, raw)
        else:                                   # a text answer such as "INVALID OPERATION"
            line = json.dumps({"r": self._ms(), "c": cid, "x": raw[:1000]})
        self.writer.write(line)

    def event(self, cid: str, kind: str, **fields):
        if self.disk_low and kind not in ("disk_low", "disk_ok"):
            return
        self.writer.write(json.dumps({"r": self._ms(), "c": cid, "e": kind, **fields}))

    def _save_markets(self, markets: List[dict]):
        """markets/<day>.json lists every market set used that day, with when it started."""
        day = utc_day(self.clock())
        path = self.out / "markets" / f"{day}.json"
        try:
            history = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            history = []
        history.append({"from_ms": self._ms(), "generation": self.generation, "markets": markets})
        write_json(path, history)

    # ------------------------------------------------------------ rotation
    async def rotate(self) -> bool:
        """Select markets and replace the connections with fresh ones. False if selection failed."""
        try:
            markets = await asyncio.get_running_loop().run_in_executor(None, self.select)
        except Exception as e:
            log.error("market selection failed: %s", e)
            self.event("main", "select_failed", why=f"{type(e).__name__}: {e}"[:300])
            return False
        if not markets:
            log.error("market selection returned nothing")
            self.event("main", "select_failed", why="empty")
            return False
        self.generation += 1
        assets = asset_ids(markets)
        self._save_markets(markets)
        self.event("main", "markets", generation=self.generation, markets=len(markets), assets=len(assets))
        new = [Shard(self, f"s{i}g{self.generation}", part).start()
               for i, part in enumerate(chunks(assets, self.shard_size))]
        try:
            await asyncio.wait_for(asyncio.gather(*(s.ready.wait() for s in new)), self.ready_timeout)
        except asyncio.TimeoutError:
            late = [s.cid for s in new if not s.ready.is_set()]
            log.warning("shards without data after %.0fs: %s", self.ready_timeout, late)
        old, self.shards, self.markets = self.shards, new, markets
        for s in old:
            await s.stop()
        log.info("generation %d: %d markets, %d tokens, %d connections",
                 self.generation, len(markets), len(assets), len(new))
        return True

    def _check_disk(self):
        try:
            free = free_gb(self.out)
        except OSError:
            return
        if not self.disk_low and free < self.min_free_gb:
            self.event("main", "disk_low", free_gb=round(free, 2))
            self.writer.flush()
            self.disk_low = True
            log.error("only %.1f GB free: recording paused", free)
        elif self.disk_low and free >= self.min_free_gb + 0.5:
            self.disk_low = False
            self.event("main", "disk_ok", free_gb=round(free, 2), dropped=self.dropped)
            log.warning("%.1f GB free again: recording resumed (%d messages dropped)", free, self.dropped)

    # ------------------------------------------------------------ main loop
    async def run(self, stop: Optional[asyncio.Event] = None, tick: float = 5.0):
        stop = stop or asyncio.Event()
        day, next_refresh, retry_at, last_stats = None, float("inf"), 0.0, self.clock()
        try:
            while not stop.is_set():
                now = self.clock()
                self._check_disk()
                due = day != utc_day(now) or now >= next_refresh
                if due and now >= retry_at:
                    if await self.rotate():
                        day = utc_day(self.clock())
                        next_refresh = self.clock() + self.refresh_hours * 3600 if self.refresh_hours else float("inf")
                    else:
                        retry_at = self.clock() + (300 if self.shards else 60)
                if self.clock() - last_stats >= self.stats_seconds:
                    last_stats = self.clock()
                    log.info("%d lines, %.1f MB raw today so far in this process, %s",
                             self.writer.lines, self.writer.bytes / 1e6,
                             {s.cid: s.messages for s in self.shards})
                try:
                    await asyncio.wait_for(stop.wait(), tick)
                except asyncio.TimeoutError:
                    pass
        finally:
            for s in self.shards:
                await s.stop()
            self.writer.close()
