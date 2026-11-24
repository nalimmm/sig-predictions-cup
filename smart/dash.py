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

from .greeks import underlying

MAGIC = b"BCM1"
ITERATIONS = 600_000


def at_risk(q) -> float:
    """Dollars a position can still lose: what was paid for it (shares x entry for a long,
    shares x (1 - entry) for a short). This is the amount the inventory cap limits."""
    return q.inv * q.avg if q.inv >= 0 else -q.inv * (1 - q.avg)


def hourly(total, t0: int) -> dict:
    """Mean, variance and Sharpe of the portfolio's P&L change per clock hour (not annualised).
    n is the number of hourly changes: with a few dozen the figures say little."""
    last = {}
    for s, v in total:
        last[(t0 + s * 1000) // 3600000] = v
    vals = [last[h] for h in sorted(last)]
    ch = [b - a for a, b in zip(vals, vals[1:])]
    n = len(ch)
    if n < 2:
        return {"n": n, "mean": None, "var": None, "sharpe": None}
    mean = sum(ch) / n
    var = sum((c - mean) ** 2 for c in ch) / (n - 1)
    return {"n": n, "mean": round(mean, 4), "var": round(var, 4), "sharpe": round(mean / var ** 0.5, 3) if var > 0 else None}


def snapshot(sim, now_ms: Optional[int] = None, capital: float = 100.0, live_from: Optional[int] = None) -> dict:
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
            "stopped": r["stopped"], "inv": r["inventory_end_shares"], "mid": r["last_mid"],
            "risk": round(at_risk(q), 4), "fees": round(q.fees, 4), "spread": r["spread_captured_usdc"], "under": underlying(q.slug),
            "title": next((m.get("question") for m in sim.markets.values() if m.get("slug") == q.slug and m.get("question")), None),
            "nav": [[sec(t), nav, mid, inv] for t, nav, mid, inv in q.nav_path],
            "qt": [[sec(t), b, a] for t, b, a in getattr(q, "quotes", ())],
            "fl": [[sec(f["t"]), 1 if f["side"] == "BUY" else -1, f["price"], f["shares"], f["inv_after"]] for f in q.fills]})
    last, total = {}, []
    for g in sorted(grid):
        last.update(grid[g])
        total.append([sec(g * 300000), round(sum(last.values()), 4)])
    markets.sort(key=lambda m: -m["pnl"])
    from .greeks import portfolio
    g = portfolio([(q.slug, q.inv, q.last_mid, q.end_ms) for q in qs], getattr(sim, "spot", None), now_ms or sim.now)
    for m in markets:
        m.update((g or {}).get("markets", {}).get(m["slug"], {}))
    if g:
        del g["markets"]
    peak, dd, expo = -1e18, 0.0, {}
    for _, v in total:
        peak = max(peak, v)
        dd = max(dd, peak - v)
    for m in markets:
        expo[m["under"]] = round(expo.get(m["under"], 0.0) + m["risk"], 4)
    return {"t0": t0, "updated": now_ms or sim.now, "mode": "paper", "capital": capital,
            "live_from": live_from, "missing": sorted(sim.missing), "greeks": g,
            "invested": round(sum(m["risk"] for m in markets), 4), "exposure": expo, "limits": {"group": sim.cfg.max_group_usdc, "delta": sim.cfg.max_net_delta_usdc}, "dd": round(dd, 4),
            "spread": round(sum(m["spread"] for m in markets), 4), "hourly": hourly(total, t0),
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
                 key: str = "smart-dashboard/data.enc", every_s: float = 60.0, clock=time.time,
                 capital: float = 100.0, guest_password: Optional[str] = None):
        self.out = Path(out_dir)
        self.guest, self.guest_key, self.revoked = guest_password, key.replace(".enc", "-guest.enc"), False
        self.out.mkdir(parents=True, exist_ok=True)
        self.password, self.client, self.bucket, self.key = password, client, bucket, key
        self.every_s, self.clock, self.next = every_s, clock, 0.0
        self.uploads = self.errors = 0
        self.capital, self.live_from = capital, int(clock() * 1000)     # earlier events are today's catch-up

    def maybe(self, sim) -> bool:
        if self.clock() < self.next:
            return False
        self.next = self.clock() + self.every_s
        snap = snapshot(sim, int(self.clock() * 1000), self.capital, self.live_from)
        snap["scan"] = self.scan(sim)
        raw = json.dumps(snap, separators=(",", ":")).encode("utf-8")
        st = self.out / "state.json.tmp"             # positions and history, read back at the next start
        st.write_text(json.dumps({**sim.state(), "live_from": self.live_from}, separators=(",", ":")))
        st.replace(self.out / "state.json")
        tmp = self.out / "data.json.tmp"
        tmp.write_bytes(raw)
        tmp.replace(self.out / "data.json")
        if self.password:
            blob = encrypt(raw, self.password)
            (self.out / "data.enc").write_bytes(blob)
            if self.client is not None:
                try:
                    # readable by link: the content is sealed, and the page fetches it without credentials
                    self.client.put_object(Bucket=self.bucket, Key=self.key, Body=blob, ACL="public-read",
                                           ContentType="application/octet-stream", CacheControl="no-store")
                    self.uploads += 1
                except Exception:               # the simulator must not stop because storage is down
                    self.errors += 1
            self.publish_guest(snap)
        return True

    def publish_guest(self, snap: dict):
        """A second copy sealed with the guest password, for a visitor: the owner's password is
        never shared, and the guest one can be changed or removed alone. Without a guest password
        the copy in storage is overwritten once, so an old guest password stops opening anything."""
        if self.guest:
            blob = encrypt(json.dumps({**snap, "guest": True}, separators=(",", ":")).encode("utf-8"), self.guest)
            (self.out / "data-guest.enc").write_bytes(blob)
        elif self.revoked:
            return
        else:
            blob, self.revoked = b"revoked", True
            (self.out / "data-guest.enc").unlink(missing_ok=True)
        if self.client is not None:
            try:
                self.client.put_object(Bucket=self.bucket, Key=self.guest_key, Body=blob, ACL="public-read",
                                       ContentType="application/octet-stream", CacheControl="no-store")
            except Exception:
                self.errors += 1
                self.revoked = False                # try the revocation again at the next snapshot


    def scan(self, sim) -> Optional[dict]:
        """The last regular scan (deploy/smart-mm-scan.timer), as a proposal: the quoted list only
        changes when the owner edits it."""
        try:
            d = json.loads((self.out / "scan" / "scan.json").read_text())
            sel = [x for x in d["slugs"].split(",") if x]
            return {"days": d["days"], "generated": d.get("generated_ms"), "measured": d["markets_measured"],
                    "selected": sel, "new": [x for x in sel if x not in sim.slugs],
                    "dropped": [x for x in sim.slugs if x not in sel]}
        except Exception:
            return None


def upload_page(client, bucket: str, page_path, key: str = "smart-dashboard/index.html") -> str:
    """Put the static page next to data.enc, readable by link. It holds no data and no secret."""
    client.put_object(Bucket=bucket, Key=key, Body=Path(page_path).read_bytes(), ACL="public-read",
                      ContentType="text/html; charset=utf-8", CacheControl="no-cache")
    return key
