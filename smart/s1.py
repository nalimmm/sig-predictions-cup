"""Signal S1: reversion of the transient impact after a sweep of the book.

Bouchaud, Farmer and Lillo (2008, sections 6.2 to 6.4): the impact of a trade decays over time,
so a sweep that carries no information moves the price further than its permanent part. Smart
provides liquidity after such a move and waits for part of it to come back.

Long only: Polymarket publishes both books of a binary market, and a sweep that pushes YES up
pushes NO down by the same amount. So the detector only looks for falls, on every token, and
the answer is always a passive bid on the token that fell.

Trigger, per token (design note, "Détail de S1"), on each change of the mid:
  - the mid fell, from its highest value of the last `window_s` seconds, by more than
    `sigma_mult` local standard deviations and at least `min_move_ticks` ticks. The local
    standard deviation is that of one-minute mid changes over the last `sigma_bars` minutes,
    floored at one tick;
  - aggressive selling in the same window traded at `min_levels` distinct prices or more, for at
    least `min_sweep_usdc`. A sale of this token and a purchase of its complement (seen at 1 - p)
    both count: on Polymarket they hit the same liquidity.
"""
import math
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

from .config import Config


@dataclass
class Signal:
    t: int                      # ms, receive time of the event that fired it
    token: str
    peak_mid: float             # highest mid of the window
    mid: float                  # mid when it fired
    drop: float
    sigma: float
    levels: int
    sweep_usdc: float
    best_bid: float
    best_ask: float
    tick: float

    @property
    def drop_sigmas(self) -> float:
        return self.drop / self.sigma if self.sigma else float("inf")


@dataclass
class TokenState:
    mids: Deque[Tuple[int, float]] = field(default_factory=deque)          # (t, mid) in the window
    bars: Deque[Tuple[int, float]] = field(default_factory=deque)          # (minute, last mid)
    sells: Deque[Tuple[int, float, float]] = field(default_factory=deque)  # (t, price, usdc) aggressive sells
    last_signal: int = -10 ** 15
    last_big_move: int = -10 ** 15      # any move over the threshold, up or down (sibling filter)
    last_mid: Optional[float] = None


def infer_tick(prices) -> float:
    for p in prices:
        if abs(round(p * 100) - p * 100) > 1e-6:
            return 0.001
    return 0.01


class S1Detector:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.state: Dict[str, TokenState] = {}

    def st(self, token: str) -> TokenState:
        s = self.state.get(token)
        if s is None:
            s = self.state[token] = TokenState()
        return s

    # ------------------------------------------------------------------ inputs
    def on_trade(self, t: int, token: str, price: float, size: float, side: Optional[str],
                 complement: Optional[str] = None):
        """An aggressive trade. side is the taker's side as Polymarket sends it."""
        side = (side or "").upper()
        if side == "SELL":
            self.st(token).sells.append((t, round(price, 4), price * size))
        elif side == "BUY" and complement:
            # buying the complement at p takes the bids of this token at 1 - p
            self.st(complement).sells.append((t, round(1 - price, 4), price * size))

    def sigma(self, s: TokenState, tick: float) -> Optional[float]:
        if len(s.bars) < self.cfg.min_bars:
            return None
        xs = [b[1] for b in s.bars]
        diffs = [b - a for a, b in zip(xs, xs[1:])]
        if len(diffs) < 2:
            return None
        m = sum(diffs) / len(diffs)
        var = sum((d - m) ** 2 for d in diffs) / (len(diffs) - 1)
        return max(math.sqrt(var), tick)

    def on_mid(self, t: int, token: str, mid: float, best_bid: float, best_ask: float,
               tick: float) -> Optional[Signal]:
        """Called after every book update of `token`. Returns a Signal when S1 fires."""
        cfg, s = self.cfg, self.st(token)
        w = int(cfg.window_s * 1000)
        minute = t // 60000
        if s.bars and s.bars[-1][0] == minute:
            s.bars[-1] = (minute, mid)
        else:
            s.bars.append((minute, mid))
            while len(s.bars) > cfg.sigma_bars + 1:
                s.bars.popleft()
        if s.last_mid == mid and s.mids:
            return None
        s.last_mid = mid
        s.mids.append((t, mid))
        while len(s.mids) > 1 and s.mids[1][0] <= t - w:
            s.mids.popleft()                    # the first entry is the mid in force at t - w
        while s.sells and s.sells[0][0] < t - w:
            s.sells.popleft()
        sigma = self.sigma(s, tick)
        if sigma is None:
            return None
        lo = min(m for _, m in s.mids)
        hi = max(m for _, m in s.mids)          # includes the mid at the start of the window
        threshold = max(cfg.sigma_mult * sigma, cfg.min_move_ticks * tick - 1e-9)
        if hi - lo > threshold:
            s.last_big_move = t
        drop = hi - mid
        if drop <= threshold or t - s.last_signal < cfg.cooldown_s * 1000:
            return None
        levels = len({p for _, p, _ in s.sells})
        usdc = sum(u for _, _, u in s.sells)
        if levels < cfg.min_levels or usdc < cfg.min_sweep_usdc:
            return None
        s.last_signal = t
        return Signal(t=t, token=token, peak_mid=hi, mid=mid, drop=drop, sigma=sigma, levels=levels,
                      sweep_usdc=round(usdc, 2), best_bid=best_bid, best_ask=best_ask, tick=tick)

    def moved_recently(self, tokens: List[str], t: int) -> List[str]:
        """Which of `tokens` moved by more than the threshold in the sibling window."""
        w = int(self.cfg.sibling_window_s * 1000)
        return [x for x in tokens if x in self.state and t - self.state[x].last_big_move <= w]
