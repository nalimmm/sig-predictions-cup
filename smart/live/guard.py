"""The safeguards of real mode, independent of the quoting logic.

Arming. Real mode starts only if the owner wrote, as root, /etc/smart-live/ARMED:

    armed=yes
    budget_usdc=100
    until=2026-12-31          # optional: past this date the file no longer arms anything

A STOP file (/etc/smart-live/STOP) stops everything: the running process cancels its orders and
exits, and real mode refuses to start while it exists.

Budget. Smart owns the whole wallet but may only engage `budget_usdc`. Committed capital is what
the open positions cost (their worst-case loss) plus what the resting buy orders would cost. An
order that would take it past the budget is refused, and so is any single order above
`max_order_usdc`. At start, the wallet's committed capital (every position in the wallet, Smart's
or not, plus every open order) must already be within the budget.

Circuit breakers on the pod's value, budget + P&L at mid:
  - a loss of `daily_loss_pct` % of the budget since the start of the UTC day,
  - a drawdown of `drawdown_pct` % from the highest value,
  - a value under `floor_usdc`,
  - the geoblock check answering blocked (or failing for longer than `geoblock_grace_s`),
  - positions in the wallet that disagree with the pod's own book.
A breaker halts the pod: every order is cancelled, nothing new is placed, positions are kept.
The halt is written to disk and survives a restart; only the owner clears it
(`python -m smart.live clear-halt`).
"""
import json
import os
import stat
import time
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

ETC = Path("/etc/smart-live")
GEOBLOCK_URL = "https://polymarket.com/api/geoblock"


@dataclass
class GuardConfig:
    budget_usdc: float = 100.0
    max_order_usdc: float = 10.0        # one order: at most the per-market cap of the quoting logic
    daily_loss_pct: float = 5.0
    drawdown_pct: float = 15.0
    floor_usdc: float = 60.0
    stale_feed_s: float = 30.0          # no book update for this long: cancel everything until it comes back
    geoblock_every_s: float = 900.0
    geoblock_grace_s: float = 3600.0
    position_tolerance: float = 1.0     # shares of difference between the wallet and the pod's book
    position_strikes: int = 3           # consecutive disagreements before halting

    @classmethod
    def from_dict(cls, d: dict) -> "GuardConfig":
        unknown = set(d) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"unknown guard parameters: {sorted(unknown)}")
        return cls(**d)


def read_arming(path: Path = ETC / "ARMED", today: Optional[date] = None, check_owner: bool = True) -> dict:
    """The arming file as a dict, or raises with the reason it does not arm."""
    if not path.exists():
        raise PermissionError(f"{path} is missing: real mode is not armed")
    if check_owner:
        st = path.stat()
        if st.st_uid != 0 or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise PermissionError(f"{path} must belong to root and be writable by root only")
    d = {}
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            k, v = (x.strip() for x in line.split("=", 1))
            d[k] = v
    if d.get("armed") != "yes":
        raise PermissionError(f"{path} does not say armed=yes")
    try:
        d["budget_usdc"] = float(d["budget_usdc"])
    except (KeyError, ValueError):
        raise PermissionError(f"{path} has no valid budget_usdc")
    if d.get("until"):
        if (today or datetime.now(timezone.utc).date()) > date.fromisoformat(d["until"]):
            raise PermissionError(f"{path} expired on {d['until']}")
    return d


def stop_requested(path: Path = ETC / "STOP") -> bool:
    try:
        return path.exists()
    except OSError:             # a directory this user cannot read: no STOP seen, the real service checks as itself
        return False


def geoblock(get: Callable) -> Optional[bool]:
    """True if Polymarket says this server's location is blocked, None if the check failed.
    Only the verdict is kept: the IP and country in the answer are not logged."""
    try:
        d = get(GEOBLOCK_URL)
        return bool(d.get("blocked"))
    except Exception:
        return None


def risk(inv: float, avg: float) -> float:
    return inv * avg if inv >= 0 else -inv * (1 - avg)


class Guard:
    def __init__(self, cfg: GuardConfig, state_path: Optional[Path] = None, clock: Callable[[], float] = time.time):
        self.cfg, self.path, self.clock = cfg, Path(state_path) if state_path else None, clock
        self.st = {"halt": None, "hwm": cfg.budget_usdc, "day": None, "day_start": cfg.budget_usdc}
        if self.path and self.path.exists():
            self.st.update(json.loads(self.path.read_text()))
        self.geo_ok_at = clock()
        self.next_geo = 0.0
        self.strikes = 0

    # ------------------------------------------------------------ state
    @property
    def halted(self) -> Optional[dict]:
        return self.st.get("halt")

    def save(self):
        if self.path:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.st, indent=1))
            tmp.replace(self.path)

    def halt(self, reason: str, **detail):
        if not self.st.get("halt"):
            self.st["halt"] = {"reason": reason, "t": int(self.clock() * 1000), **detail}
            self.save()

    def clear(self, nav: Optional[float] = None):
        self.st["halt"] = None
        if nav is not None:
            self.st["hwm"] = nav
            self.st["day_start"] = nav
        self.save()

    # ------------------------------------------------------------ value
    @staticmethod
    def pnl(quoters) -> float:
        return sum(q.cash + q.inv * (q.last_mid if q.last_mid is not None else q.avg) for q in quoters)

    def nav(self, quoters) -> float:
        return self.cfg.budget_usdc + self.pnl(quoters)

    @staticmethod
    def committed(quoters, orders) -> float:
        return sum(risk(q.inv, q.avg) for q in quoters) + sum(o.notional() for o in orders)

    # ------------------------------------------------------------ checks
    def breakers(self, quoters) -> Optional[str]:
        """Updates the day's start and the high-water mark; returns why to halt, if any."""
        cfg, nav = self.cfg, self.nav(quoters)
        day = datetime.fromtimestamp(self.clock(), timezone.utc).strftime("%Y-%m-%d")
        if self.st["day"] != day:
            self.st["day"], self.st["day_start"] = day, nav
            self.save()
        if nav > self.st["hwm"]:
            self.st["hwm"] = nav
        if nav <= self.st["day_start"] - cfg.daily_loss_pct / 100 * cfg.budget_usdc:
            return f"daily loss: value {nav:.2f} against {self.st['day_start']:.2f} at the start of the day"
        if nav <= self.st["hwm"] * (1 - cfg.drawdown_pct / 100):
            return f"drawdown: value {nav:.2f} against a high of {self.st['hwm']:.2f}"
        if nav < cfg.floor_usdc:
            return f"value {nav:.2f} under the floor of {cfg.floor_usdc:.2f}"
        return None

    def allow(self, side: str, price: float, size: float, quoters, orders) -> Optional[str]:
        """None if the order may be sent, else why not."""
        if self.halted:
            return "halted"
        cost = price * size if side == "BUY" else 0.0
        if cost > self.cfg.max_order_usdc + 1e-9:
            return f"order of {cost:.2f} over the {self.cfg.max_order_usdc:.2f} limit"
        if cost and self.committed(quoters, orders) + cost > self.cfg.budget_usdc + 1e-9:
            return "budget"
        return None

    def check_geoblock(self, get: Callable) -> Optional[str]:
        now = self.clock()
        if now < self.next_geo:
            return None
        self.next_geo = now + self.cfg.geoblock_every_s
        b = geoblock(get)
        if b is True:
            return "geoblock: Polymarket says this location is blocked"
        if b is False:
            self.geo_ok_at = now
        elif now - self.geo_ok_at > self.cfg.geoblock_grace_s:
            return "geoblock check failing for too long"
        return None

    def check_positions(self, mine: Dict[str, float], wallet: Dict[str, float]) -> Optional[str]:
        """mine and wallet: YES-equivalent shares per market slug."""
        bad = {s: (round(mine.get(s, 0.0), 2), round(wallet.get(s, 0.0), 2)) for s in set(mine) | set(wallet)
               if abs(mine.get(s, 0.0) - wallet.get(s, 0.0)) > self.cfg.position_tolerance}
        self.strikes = self.strikes + 1 if bad else 0
        if self.strikes >= self.cfg.position_strikes:
            return f"positions differ from the wallet: {bad}"
        return None
