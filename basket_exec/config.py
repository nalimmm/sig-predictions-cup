"""Strategy parameters. Defaults are venue-neutral; `Config.sig_cup()` reproduces the tournament bot."""
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from typing import Optional


def parse_time(ts) -> datetime:
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class Rolling:
    """Reinvestment window (election night in the tournament): cash from sales is rolled into
    basket instruments that are still cheap and still holding up."""
    start: datetime
    buys_end: datetime
    price_cap: float = 0.80        # never buy above this (remaining upside too small)
    momentum_max: float = 1.5      # never buy above this multiple of the pre-window price
    drop_guard: float = 0.80       # no buy if the bid sits below this share of the pre-window price
    max_share: float = 0.35        # cost per instrument capped at this share of the account

    def __post_init__(self):
        self.start, self.buys_end = parse_time(self.start), parse_time(self.buys_end)


@dataclass
class Config:
    build_until: datetime                    # tranche buys at reference x price_cap_mult stop here
    guard_from: Optional[datetime] = None    # rescue armed from here (default: build_until)
    rolling: Optional[Rolling] = None

    # Buys
    price_cap_mult: float = 1.10             # never pay more than reference x this
    cash_buffer: float = 0.02                # share of the account kept out of the budgets
    min_cash_reserve: float = 0.0            # absolute cash never spent
    min_tranche_notional: float = 0.0        # do not bother with a tranche when less room than this is left
    max_capital: Optional[float] = None      # size on min(equity, max_capital); None = the whole account

    # Laddered sells
    leg_low_mult: float = 2.0                # first half at 2x the average cost
    leg_high_mult: float = 3.0               # second half at 3x ...
    leg_high_mult_cheap: float = 5.0         # ... or 5x when the average cost is below high_leg_below
    high_leg_below: float = 0.20
    sell_cap: float = 0.95                   # no target above this
    guard_sell_floor: Optional[float] = None  # once the guard is armed, no leg below this price
                                              # (sell confirmation, not mid river)

    # Rescue
    rescue_fraction: float = 0.50            # full sale if bid <= this share of the average cost ...
    rescue_floor: float = 0.10               # ... or <= this absolute price, whichever is higher

    # Housekeeping
    cleanup_outside: bool = False            # True: sell every position and cancel every order outside the
                                             # basket. Only for an account dedicated to this strategy.

    def __post_init__(self):
        self.build_until = parse_time(self.build_until)
        self.guard_from = parse_time(self.guard_from) if self.guard_from else self.build_until
        if isinstance(self.rolling, dict):
            self.rolling = Rolling(**self.rolling)
        if not 0 < self.rescue_fraction < 1 or not 0 <= self.cash_buffer < 1 or self.price_cap_mult <= 0:
            raise ValueError("inconsistent strategy parameters")

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        unknown = set(d) - {f.name for f in fields(cls)}
        if unknown:  # a typo in a risk parameter must not silently fall back to a default
            raise ValueError(f"unknown strategy parameters: {sorted(unknown)}")
        return cls(**d)

    @classmethod
    def sig_cup(cls) -> "Config":
        """The exact parameters of src/basket_bot.py (SIG Predictions Cup, amounts in SUSQies)."""
        return cls(
            build_until="2026-11-03T23:00:00Z",
            rolling=Rolling(start="2026-11-04T00:00:00Z", buys_end="2026-11-04T16:00:00Z"),
            guard_sell_floor=0.70, min_cash_reserve=200, min_tranche_notional=100, cleanup_outside=True)
