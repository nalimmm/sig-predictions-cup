"""Every parameter of the Smart pod in one place, with the values of the design note (19/11/2026).

No hidden constant elsewhere: the engine reads only this object, and the journal records it at
each start so a result can always be traced to the parameters that produced it.
"""
from dataclasses import asdict, dataclass, fields


@dataclass
class Config:
    # ---- virtual budget (design note, "Cadre du pod")
    start_nav: float = 100.0            # first month: Smart acts as if the wallet held 100 USDC

    # ---- S1 trigger: in less than `window_s`, the mid falls by more than `sigma_mult` local
    # standard deviations, and aggressive selling hit at least `min_levels` price levels
    window_s: float = 60.0
    sigma_mult: float = 3.0
    sigma_bars: int = 60                # one-minute bars used for the local volatility
    min_bars: int = 20                  # no signal before this many bars of history on the token
    min_move_ticks: int = 2             # the fall must also be at least this many ticks
    min_levels: int = 2
    min_sweep_usdc: float = 50.0        # aggressive notional in the window
    cooldown_s: float = 600.0           # one signal per token per 10 minutes
    sibling_window_s: float = 120.0     # a big move on a sibling market at the same time = news

    # ---- S1 filters
    min_price: float = 0.05
    max_price: float = 0.95
    min_hours_left: float = 24.0        # no entry within 24 h of the market's end date
    max_spread_ticks: int = 10

    # ---- S1 action and exit
    entry_ttl_s: float = 300.0          # passive bid cancelled after 5 minutes
    revert_frac: float = 0.5            # take-profit at half the fall recovered
    hold_s: float = 1800.0              # time exit after 30 minutes
    latency_s: float = 2.0              # decision to execution, for aggressive orders
    rescue_frac: float = 0.5            # sell if the bid falls under half the entry price
    followup_min: tuple = (1, 5, 15, 30, 60)   # mid recorded after each signal, for the study

    # ---- sizing (fractions of the virtual NAV)
    max_order: float = 0.03
    max_market: float = 0.10
    max_event: float = 0.20
    max_gross: float = 0.50
    s1_budget: float = 0.25
    depth_frac: float = 0.20            # at most 20 % of the bid depth within 2 ticks
    volume_frac: float = 0.01           # at most 1 % of the market's 24 h volume
    min_order_usdc: float = 1.0
    min_shares: float = 5.0             # most Polymarket books have a 5 share minimum

    # ---- circuit breakers
    day_loss: float = 0.05              # no new entry until 00:00 UTC
    max_drawdown: float = 0.15          # pod halted until the user restarts it
    floor_nav: float = 60.0             # pod halted for a full review
    stale_s: float = 10.0               # live: no new entry if the newest event is older (recorder flushes every 5 s)
    max_orders_per_min: int = 30

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        names = {f.name for f in fields(cls)}
        unknown = set(d) - names
        if unknown:
            raise ValueError(f"unknown parameters: {sorted(unknown)}")
        return cls(**d)
