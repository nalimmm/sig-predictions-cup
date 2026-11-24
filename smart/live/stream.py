"""Polymarket market channel, for a handful of tokens, as events in the recorder's format
(`_r` receive time in ms, `_c` connection), so the paper market maker's code reads them as is.

Same protocol as book_recorder.ws: subscribe with {"assets_ids": [...], "type": "market"}, send
the text PING every 10 s. A connection silent for `stale_s` is reopened; each (re)connection is
announced with {"_e": "open"} and {"_e": "close"} so the caller can cancel its orders while the
books are not current.
"""
import asyncio
import json
import time
from typing import AsyncIterator, Callable, List

from book_recorder.ws import WS_URL, default_connect


async def market_events(tokens: List[str], connect: Callable = default_connect, url: str = WS_URL,
                        ping_s: float = 10.0, stale_s: float = 60.0, max_backoff: float = 30.0,
                        clock: Callable[[], float] = time.time, stop: Callable[[], bool] = lambda: False
                        ) -> AsyncIterator[dict]:
    backoff, gen = 1.0, 0
    while not stop():
        gen += 1
        cid = f"live{gen}"
        pinger = None
        try:
            async with connect(url) as ws:
                await ws.send(json.dumps({"assets_ids": list(tokens), "type": "market"}))
                yield {"_r": int(clock() * 1000), "_c": cid, "_e": "open"}

                async def ping():
                    while True:
                        await asyncio.sleep(ping_s)
                        await ws.send("PING")
                pinger = asyncio.ensure_future(ping())
                while not stop():
                    msg = await asyncio.wait_for(ws.recv(), timeout=stale_s)
                    if isinstance(msg, bytes):
                        msg = msg.decode("utf-8", "replace")
                    msg = msg.strip()
                    if not msg or msg[0] not in "{[":
                        continue
                    try:
                        data = json.loads(msg)
                    except ValueError:
                        continue
                    r = int(clock() * 1000)
                    for ev in data if isinstance(data, list) else [data]:
                        if isinstance(ev, dict):
                            yield {**ev, "_r": r, "_c": cid}
                    backoff = 1.0
        except asyncio.CancelledError:
            raise
        except Exception as e:      # timeouts, network errors, server closes
            yield {"_r": int(clock() * 1000), "_c": cid, "_e": "close", "why": f"{type(e).__name__}: {e}"[:200]}
        finally:
            if pinger:
                pinger.cancel()
        if stop():
            break
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, max_backoff)
