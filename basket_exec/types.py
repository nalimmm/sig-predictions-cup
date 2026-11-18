"""Venue-neutral data containers. Nothing in this file knows about any exchange.

An *instrument* is whatever the venue lets you hold long: a Polymarket outcome token, or one
side (YES or NO) of a tournament contract. The engine only ever buys and sells instruments;
there is no notion of a short position.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple


@dataclass(frozen=True)
class Spec:
    """Trading rules of one instrument."""
    tick: float = 0.01          # price increment; prices live in [tick, 1 - tick]
    size_step: float = 1.0      # quantity increment
    min_size: float = 0.0       # smallest order the venue accepts, in units
    min_notional: float = 0.0   # smallest buy the venue accepts, in cash


@dataclass(frozen=True)
class Level:
    price: float
    size: float


@dataclass
class Book:
    bids: List[Level]           # best (highest) first
    asks: List[Level]           # best (lowest) first


@dataclass
class Position:
    instrument: str
    quantity: float             # always positive
    avg_cost: float             # per unit
    cost_basis: float           # cash paid for what is still held


@dataclass
class OpenOrder:
    id: str
    instrument: str
    action: str                 # "buy" or "sell"
    price: float
    remaining: float            # unfilled quantity


@dataclass
class Snapshot:
    """Everything the engine needs to know about the account, re-read from the venue each round."""
    equity: float                                   # cash + positions marked to market
    cash: float                                     # spendable cash: NEVER equity minus cost basis
    positions: Dict[str, Position] = field(default_factory=dict)
    orders: List[OpenOrder] = field(default_factory=list)
    quotes: Dict[str, Tuple[Optional[float], Optional[float]]] = field(default_factory=dict)  # (bid, ask)
    specs: Dict[str, Spec] = field(default_factory=dict)
    blocked: Set[str] = field(default_factory=set)  # basket instruments that must not be bought this round
    notes: List[str] = field(default_factory=list)  # anything the adapter wants in the log


@dataclass(frozen=True)
class Place:
    instrument: str
    action: str                 # "buy" or "sell"
    quantity: float
    price: float
    reason: str = ""


@dataclass(frozen=True)
class Cancel:
    order: OpenOrder
    reason: str = ""


@dataclass
class Plan:
    actions: list = field(default_factory=list)     # Place / Cancel, in execution order
    notes: List[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
