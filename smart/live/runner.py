"""The event loop of shadow and real mode.

One task reads the books from Polymarket's websocket and feeds the market maker; one ticker
runs the clock-driven work once a second: STOP file, circuit breakers, geoblock check, and in
real mode the heartbeat, the poll of our orders' fills and the position check against the
wallet. Venue calls go through a single worker thread, one batch at a time; slow side work
(dashboard upload, BTC price, geoblock) through another, so neither ever holds the books.
"""
import asyncio
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

from ..dash import encrypt, snapshot
from ..feed import utc_day
from .execution import Executor, Journal, LiveMM
from .guard import Guard, stop_requested
from .stream import market_events

log = logging.getLogger("smart.live")


class Publish:
    """data.json (clear, local only), state.json, and the sealed data-<mode>.enc uploaded for the page."""

    def __init__(self, out: Path, password: Optional[str], client=None, bucket=None, key: Optional[str] = None,
                 every_s: float = 60.0, capital: float = 100.0, guest_password: Optional[str] = None):
        self.out, self.password, self.client, self.bucket, self.key = Path(out), password, client, bucket, key
        # a second copy sealed with the guest password (same convention as smart.dash.Publisher):
        # <key>-guest.enc; without a guest password it is overwritten once with b"revoked"
        self.guest, self.revoked = guest_password, False
        self.guest_key = key.replace(".enc", "-guest.enc") if key else None
        self.every_s, self.capital, self.next, self.errors = every_s, capital, 0.0, 0
        self.live_from = int(time.time() * 1000)

    def due(self) -> bool:
        if time.time() < self.next:
            return False
        self.next = time.time() + self.every_s
        return True

    def build(self, sim: LiveMM, ex: Executor) -> tuple:
        snap = snapshot(sim, int(time.time() * 1000), self.capital, self.live_from)
        snap["mode"] = sim.mode
        snap["execution"] = ex.summary()
        t0 = snap["t0"]
        for m in snap["markets"]:      # resting quotes over time, YES terms: [s after t0, bid, bid size, ask, ask size]
            m["ord"] = [[round((x[0] - t0) / 1000)] + x[1:] for x in ex.quote_path.get(m["slug"], []) if x[0] >= t0]
        state = {**sim.state(), "live_from": self.live_from,
                 "orders": [o.__dict__ for o in ex.orders.values()] if sim.mode == "live" else []}
        return json.dumps(snap, separators=(",", ":")).encode(), json.dumps(state, separators=(",", ":"))

    def write(self, raw: bytes, state: str):
        for name, data in (("state.json", state.encode()), ("data.json", raw)):
            tmp = self.out / (name + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(self.out / name)
        if self.password:
            blob = encrypt(raw, self.password)
            (self.out / "data.enc").write_bytes(blob)
            if self.client is not None and self.key:
                try:
                    self.client.put_object(Bucket=self.bucket, Key=self.key, Body=blob, ACL="public-read",
                                           ContentType="application/octet-stream", CacheControl="no-store")
                except Exception:
                    self.errors += 1
            self.write_guest(raw)

    def write_guest(self, raw: bytes):
        if self.guest:
            blob = encrypt(json.dumps({**json.loads(raw), "guest": True}, separators=(",", ":")).encode(), self.guest)
            (self.out / "data-guest.enc").write_bytes(blob)
        elif self.revoked:
            return
        else:
            blob = b"revoked"
            (self.out / "data-guest.enc").unlink(missing_ok=True)
        if self.client is not None and self.guest_key:
            try:
                self.client.put_object(Bucket=self.bucket, Key=self.guest_key, Body=blob, ACL="public-read",
                                       ContentType="application/octet-stream", CacheControl="no-store")
                self.revoked = not self.guest
            except Exception:
                self.errors += 1


class Runner:
    def __init__(self, sim: LiveMM, ex: Executor, guard: Guard, pub: Publish, get: Callable,
                 spot: Optional[Callable] = None, connect=None, stop_file: Path = Path("/etc/smart-live/STOP"),
                 heartbeat_s: float = 5.0, poll_s: float = 1.0, positions_s: float = 60.0):
        self.sim, self.ex, self.guard, self.pub, self.get, self.spot = sim, ex, guard, pub, get, spot
        self.connect, self.stop_file = connect, stop_file
        self.heartbeat_s, self.poll_s, self.positions_s = heartbeat_s, poll_s, positions_s
        self.io = ThreadPoolExecutor(1, thread_name_prefix="venue")
        self.side = ThreadPoolExecutor(1, thread_name_prefix="side")
        self.stopping = False
        self.exit_reason = None
        self.busy = False
        self.side_busy = False
        self.day = None
        self.events = 0
        self.lags = []

    @property
    def live(self) -> bool:
        return self.sim.mode == "live"

    def stop(self, why: str):
        if not self.stopping:
            log.warning("stopping: %s", why)
            self.exit_reason = why
            self.stopping = True

    def halt(self, why: str):
        if not self.guard.halted:
            log.error("HALT: %s", why)
            self.ex.journal.write("halt", why=why)
        self.guard.halt(why)

    # ------------------------------------------------------------ orders
    async def trade(self):
        """Bring the orders in line with the quotes. In real mode one batch at a time."""
        if not self.live:
            self.ex.step()
            return
        if self.busy:
            return
        d = self.ex.decide()
        if not (d["cancel"] or d["place"]):
            return
        self.busy = True
        try:
            r = await asyncio.get_running_loop().run_in_executor(self.io, self.ex.execute, d)
            self.ex.commit(r)
            if r["errors"]:
                log.error("venue errors: %s", r["errors"])
        finally:
            self.busy = False

    async def venue(self, fn, *a):
        return await asyncio.get_running_loop().run_in_executor(self.io, fn, *a)

    # ------------------------------------------------------------ tasks
    async def feed(self):
        tokens = list(self.sim.meta)
        async for ev in market_events(tokens, **({"connect": self.connect} if self.connect else {}),
                                      stop=lambda: self.stopping):
            e = ev.get("_e")
            if e == "open":
                self.ex.feed_ok = False
                log.info("market feed open, %d tokens", len(tokens))
                continue
            if e == "close":
                self.ex.feed_ok = False
                log.warning("market feed closed: %s", ev.get("why"))
                await self.trade()                  # cancels: the books are not current
                continue
            self.events += 1
            ts = ev.get("timestamp")
            if ts and str(ts).isdigit() and len(self.lags) < 5000:
                self.lags.append(ev["_r"] - int(ts))
            self.sim.on_event(ev)
            if ev.get("event_type") == "book":
                self.ex.feed_ok = True
            await self.trade()

    async def ticker(self):
        loop = asyncio.get_running_loop()
        next_hb = next_poll = next_pos = next_spot = 0.0
        while not self.stopping:
            now = time.time()
            today = utc_day(now * 1000)
            if today != self.day:
                self.day = today
                self.sim.on_event({"_r": None, "_e": "day", "day": today})
            self.sim.now = max(self.sim.now, int(now * 1000))
            if stop_requested(self.stop_file):
                self.halt("STOP file")
                self.stop("STOP file present")
                break
            why = self.guard.breakers(self.sim.q.values())
            if why:
                self.halt(why)
            if not self.side_busy:
                self.side_busy = True
                try:
                    why = await loop.run_in_executor(self.side, self.guard.check_geoblock, self.get)
                    if why:
                        self.halt(why)
                    if self.spot and now >= next_spot:
                        next_spot = now + 60
                        px = await loop.run_in_executor(self.side, self.spot)
                        if px:
                            self.sim.spot = px
                    if self.pub.due():
                        raw, state = self.pub.build(self.sim, self.ex)
                        loop.run_in_executor(self.side, self.pub.write, raw, state)
                finally:
                    self.side_busy = False
            if self.live and not self.busy:
                self.busy = True
                try:
                    if now >= next_hb:
                        next_hb = now + self.heartbeat_s
                        await self.venue(self.ex.venue.heartbeat)
                    if now >= next_poll:
                        next_poll = now + self.poll_s
                        open_, gone = await self.venue(self.ex.poll_io)
                        self.ex.ingest(open_, gone)
                    if now >= next_pos:
                        next_pos = now + self.positions_s
                        wallet = await self.venue(self.wallet_inventory)
                        why = self.guard.check_positions({q.slug: q.inv for q in self.sim.q.values()}, wallet)
                        if why:
                            self.halt(why)
                except Exception as e:
                    log.error("venue: %s: %s", type(e).__name__, str(e)[:300])
                    self.ex.journal.write("error", why=f"{type(e).__name__}: {e}"[:300])
                finally:
                    self.busy = False
            await self.trade()                      # stale feed or a halt cancels even with no new event
            await asyncio.sleep(1.0)

    def wallet_inventory(self) -> dict:
        out = {}
        for q in self.sim.q.values():
            yes, no = self.ex.tokens(q)
            out[q.slug] = self.ex.venue.token_balance(yes) - (self.ex.venue.token_balance(no) if no else 0.0)
        return out

    async def main(self):
        log.info("%s mode on %d markets; %s", self.sim.mode, len(self.sim.slugs),
                 "orders are signed and sent" if self.live else "nothing is signed or sent")
        feed = asyncio.ensure_future(self.feed())
        try:
            await self.ticker()
        finally:
            self.stopping = True
            feed.cancel()
            try:
                await feed
            except (asyncio.CancelledError, Exception):
                pass
            if self.live:
                try:
                    await self.venue(self.ex.venue.cancel_all)
                    log.warning("all orders cancelled on exit")
                except Exception as e:
                    log.error("cancel on exit failed: %s (the heartbeat stops, Polymarket cancels within 10 s)", e)
            raw, state = self.pub.build(self.sim, self.ex)
            self.pub.write(raw, state)
            if self.lags:
                lag = sorted(self.lags)
                log.info("feed delay, ms: median %d, 90%% %d", lag[len(lag) // 2], lag[int(len(lag) * 0.9)])
