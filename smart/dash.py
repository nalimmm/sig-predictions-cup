"""Private dashboard feed for the paper market maker.

The simulator's state (P&L path, fills, results per market) is written as one JSON snapshot.
`encrypt` seals it with a password (PBKDF2-SHA256 then AES-256-GCM) so the file can sit on object
storage and only be read in a browser that knows the password: nothing listens on the trading
server. The snapshot never holds a wallet address or a key; this package has neither.

File layout of an encrypted snapshot: b"BCM1" + salt (16) + nonce (12) + ciphertext and tag.
"""
import json
import os
import time
from pathlib import Path
from typing import Optional

MAGIC = b"BCM1"
ITERATIONS = 600_000


def snapshot(sim, now_ms: Optional[int] = None) -> dict:
    """What the page draws. Times are seconds after t0 to keep the file small."""
    qs = list(sim.q.values())
    starts = [q.nav_path[0][0] for q in qs if q.nav_path]
    t0 = min(starts) if starts else (now_ms or sim.now)
    sec = lambda t: round((t - t0) / 1000)
    markets, grid = [], {}
    for q in qs:
        r = sim.result(q, 0.0)
        for t, nav, _, _ in q.nav_path:
            grid.setdefault(t // 300000, {})[q.slug] = nav
        markets.append({
            "slug": q.slug, "fills": r["fills"], "buys": r["buys"], "sells": r["sells"],
            "pnl": r["pnl_if_liquidated_usdc"], "pnl_mid": r["pnl_at_mid_usdc"], "dd": r["max_drawdown_usdc"],
            "stopped": r["stopped"], "inv": r["inventory_end_shares"],
            "nav": [[sec(t), nav, mid, inv] for t, nav, mid, inv in q.nav_path],
            "fl": [[sec(f["t"]), 1 if f["side"] == "BUY" else -1, f["price"], f["shares"], f["inv_after"]] for f in q.fills]})
    last, total = {}, []
    for g in sorted(grid):
        last.update(grid[g])
        total.append([sec(g * 300000), round(sum(last.values()), 4)])
    markets.sort(key=lambda m: -m["pnl"])
    return {"t0": t0, "updated": now_ms or sim.now, "mode": "paper",
            "total_liq": round(sum(m["pnl"] for m in markets), 4), "total_mid": round(sum(m["pnl_mid"] for m in markets), 4),
            "total": total, "markets": markets}


def _key(password: str, salt: bytes) -> bytes:
    import hashlib
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS, 32)


def encrypt(data: bytes, password: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if len(password) < 16:
        raise ValueError("the dashboard password must be at least 16 characters: the file can be attacked offline")
    salt, nonce = os.urandom(16), os.urandom(12)
    return MAGIC + salt + nonce + AESGCM(_key(password, salt)).encrypt(nonce, data, MAGIC)


def decrypt(blob: bytes, password: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if blob[:4] != MAGIC:
        raise ValueError("not a dashboard snapshot")
    return AESGCM(_key(password, blob[4:20])).decrypt(blob[20:32], blob[32:], MAGIC)


class Publisher:
    """Writes data.json locally and, when a password is set, data.enc; uploads data.enc when a
    client is given. The clear file never leaves the machine."""

    def __init__(self, out_dir, password: Optional[str] = None, client=None, bucket: Optional[str] = None,
                 key: str = "smart-dashboard/data.enc", every_s: float = 60.0, clock=time.time):
        self.out = Path(out_dir)
        self.out.mkdir(parents=True, exist_ok=True)
        self.password, self.client, self.bucket, self.key = password, client, bucket, key
        self.every_s, self.clock, self.next = every_s, clock, 0.0
        self.uploads = self.errors = 0

    def maybe(self, sim) -> bool:
        if self.clock() < self.next:
            return False
        self.next = self.clock() + self.every_s
        raw = json.dumps(snapshot(sim, int(self.clock() * 1000)), separators=(",", ":")).encode("utf-8")
        tmp = self.out / "data.json.tmp"
        tmp.write_bytes(raw)
        tmp.replace(self.out / "data.json")
        if self.password:
            blob = encrypt(raw, self.password)
            (self.out / "data.enc").write_bytes(blob)
            if self.client is not None:
                try:
                    self.client.put_object(Bucket=self.bucket, Key=self.key, Body=blob,
                                           ContentType="application/octet-stream", CacheControl="no-store")
                    self.uploads += 1
                except Exception:               # the simulator must not stop because storage is down
                    self.errors += 1
        return True
