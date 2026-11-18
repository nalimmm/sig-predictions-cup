"""Adapter for the Polymarket CLOB (real money).

Instruments are outcome token ids. Sources, each used for what it is authoritative on:

  CLOB (py-clob-client, authenticated)  open orders, USDC balance, token balances, order entry
  CLOB (public)                         order books with tick size and minimum order size, price history
  Data API (public)                     average cost of each position
  Gamma API (public)                    market slug + outcome -> token id

Differences with the tournament that the adapter absorbs:
  - Cash is the wallet's USDC balance, read directly. Nothing is inferred from equity.
  - Quantities come from the CLOB token balance, not from the Data API, which can lag a fill by
    several seconds. A token held but not yet priced by the Data API is left alone for the round
    (no ladder without a known average cost) and is not bought again.
  - Tick size (0.01 or 0.001), minimum order size and the neg-risk flag are per market and read
    from the book; sizes are in hundredths of a share; a buy must be worth at least 1 USDC.
  - Books are fetched once per round with the quotes, so a round costs one call per instrument.

Without a private key the adapter is read-only: positions come from the Data API for the given
address, there are no open orders and cash is `paper_cash`. Good for a dry run; it cannot trade.
"""
import json
import time
from datetime import datetime
from typing import Dict, List, Optional

import requests

from ..types import Book, Level, OpenOrder, Place, Position, Snapshot, Spec

CLOB = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
USDC = 1e6                 # balances are reported in base units (6 decimals)
SIZE_STEP = 0.01
MIN_BUY_NOTIONAL = 1.0


def make_client(private_key: str, funder: str, signature_type: int = 1, host: str = CLOB):
    """signature_type: 0 = plain wallet (EOA), 1 = email / Magic login, 2 = browser wallet proxy.
    funder: the address that holds the funds (the one shown on your Polymarket profile)."""
    from py_clob_client.client import ClobClient
    client = ClobClient(host, key=private_key, chain_id=137, signature_type=signature_type, funder=funder)
    client.set_api_creds(client.create_or_derive_api_creds())
    return client


class PolymarketVenue:
    def __init__(self, entries: List[dict], funder: str, client=None, paper_cash: float = 0.0,
                 session=None, pause: float = 0.1, sleep=time.sleep):
        """entries: [{"token_id": ..., "reference": 0.43}] or [{"slug": ..., "outcome": "Yes", "reference": 0.43}]."""
        self.entries, self.funder, self.client, self.paper_cash = entries, funder, client, paper_cash
        self.session, self.pause, self.sleep = session or requests.Session(), pause, sleep
        self.labels: Dict[str, str] = {}
        self._basket: Dict[str, float] = {}
        self._books: Dict[str, Book] = {}
        self._meta: Dict[str, dict] = {}     # token -> {"tick": "0.01", "neg_risk": bool}

    # ------------------------------------------------------------------ HTTP
    def _get(self, url, **params):
        r = self.session.get(url, params=params, timeout=15)
        self.sleep(self.pause)
        r.raise_for_status()
        return r.json()

    # ----------------------------------------------------------------- Venue
    def basket(self) -> Dict[str, float]:
        if self._basket:
            return self._basket
        out = {}
        for e in self.entries:
            ref = float(e["reference"])
            if not 0 < ref < 1:
                raise ValueError(f"reference price out of range: {e}")
            if e.get("token_id"):
                token = str(e["token_id"])
                self.labels[token] = e.get("label") or f"token {token[:8]}..."
            else:
                found = self._get(f"{GAMMA}/markets", slug=e["slug"])
                if not found:
                    raise ValueError(f"no Polymarket market with slug {e['slug']!r}")
                m = found[0]
                load = lambda v: json.loads(v) if isinstance(v, str) else list(v)
                outcomes, tokens = load(m["outcomes"]), load(m["clobTokenIds"])
                wanted = str(e.get("outcome", "Yes")).lower()
                match = [t for o, t in zip(outcomes, tokens) if str(o).lower() == wanted]
                if len(match) != 1:
                    raise ValueError(f"outcome {e.get('outcome', 'Yes')!r} not found in {outcomes} for {e['slug']}")
                token = str(match[0])
                self.labels[token] = e.get("label") or f"{m.get('question', e['slug'])} [{e.get('outcome', 'Yes')}]"
            if token in out:
                raise ValueError(f"instrument listed twice in the basket: {self.labels[token]}")
            out[token] = ref
        self._basket = out
        return out

    def _load_book(self, token: str) -> Optional[Book]:
        try:
            raw = self._get(f"{CLOB}/book", token_id=token)
        except requests.RequestException:
            return None   # resolved or delisted market: no book, so no quote and nothing to do
        # The API does not return levels best-first: always sort.
        bids = sorted((Level(float(l["price"]), float(l["size"])) for l in raw.get("bids") or []),
                      key=lambda l: -l.price)
        asks = sorted((Level(float(l["price"]), float(l["size"])) for l in raw.get("asks") or []),
                      key=lambda l: l.price)
        self._meta[token] = {"tick": str(raw.get("tick_size") or "0.01"), "neg_risk": bool(raw.get("neg_risk")),
                             "min_size": float(raw.get("min_order_size") or 5)}
        return Book(bids=bids, asks=asks)

    def _spec(self, token: str) -> Spec:
        m = self._meta.get(token, {})
        return Spec(tick=float(m.get("tick", "0.01")), size_step=SIZE_STEP,
                    min_size=m.get("min_size", 5.0), min_notional=MIN_BUY_NOTIONAL)

    def _data_positions(self) -> List[dict]:
        rows, offset = [], 0
        while True:
            page = self._get(f"{DATA}/positions", user=self.funder, sizeThreshold=0, limit=500, offset=offset)
            rows += page
            if len(page) < 500:
                return rows
            offset += 500

    def _token_balance(self, token: str) -> float:
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
        r = self.client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token))
        return float(r["balance"]) / USDC

    def snapshot(self, basket, with_outside=False) -> Snapshot:
        rows = {str(r["asset"]): r for r in self._data_positions()
                if float(r.get("size") or 0) > 0 and not r.get("redeemable")}
        for token, r in rows.items():
            self.labels.setdefault(token, f"{r.get('title', token[:8])} [{r.get('outcome', '?')}]")
        watched = list(basket) + [t for t in rows if t not in basket and with_outside]
        self._books = {}
        for token in watched:
            b = self._load_book(token)
            if b:
                self._books[token] = b

        snap = Snapshot(equity=0.0, cash=self.paper_cash)
        if self.client is not None:
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
            bal = self.client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            snap.cash = float(bal["balance"]) / USDC
            for o in self.client.get_orders():
                left = float(o["original_size"]) - float(o.get("size_matched") or 0)
                if left > 0:
                    snap.orders.append(OpenOrder(o["id"], str(o["asset_id"]), o["side"].lower(),
                                                 float(o["price"]), round(left, 6)))
        value = 0.0
        for token in list(dict.fromkeys(watched + list(rows))):
            r = rows.get(token)
            qty = float(r["size"]) if r else 0.0
            if self.client is not None and token in watched:
                qty = self._token_balance(token)   # authoritative and immediate
            if qty <= 0:
                continue
            bid = self._books[token].bids[0].price if token in self._books and self._books[token].bids else None
            mark = bid if bid is not None else float(r.get("curPrice") or 0) if r else 0.0
            value += qty * mark     # marked at the bid: what a sale would actually fetch
            if r is None or not float(r.get("avgPrice") or 0):
                snap.blocked.add(token)
                snap.notes.append(f"{self.label(token)}: {qty:g} held but average cost not published yet, "
                                  f"left untouched this round")
                continue
            avg = float(r["avgPrice"])
            snap.positions[token] = Position(token, qty, avg, qty * avg)
        snap.equity = snap.cash + value
        for token in watched:
            b = self._books.get(token)
            snap.quotes[token] = ((b.bids[0].price if b.bids else None, b.asks[0].price if b.asks else None)
                                  if b else (None, None))
            snap.specs[token] = self._spec(token)
        return snap

    def book(self, instrument) -> Optional[Book]:
        return self._books.get(instrument)

    def reference(self, instrument, before: datetime) -> Optional[float]:
        """Last price printed at or before `before` (hourly samples over the 6 hours leading to it)."""
        end = int(before.timestamp())
        h = self._get(f"{CLOB}/prices-history", market=instrument, startTs=end - 6 * 3600, endTs=end, fidelity=60)
        points = [(int(p["t"]), float(p["p"])) for p in h.get("history", []) if int(p["t"]) <= end]
        return max(points)[1] if points else None

    def place(self, order: Place) -> str:
        if self.client is None:
            raise RuntimeError("read-only adapter (no private key): cannot place orders")
        from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
        meta = self._meta.get(order.instrument)
        if meta is None:
            raise RuntimeError("no order book read for this instrument this round")
        args = OrderArgs(token_id=order.instrument, price=order.price, size=order.quantity,
                         side="BUY" if order.action == "buy" else "SELL")
        signed = self.client.create_order(args, PartialCreateOrderOptions(tick_size=meta["tick"],
                                                                          neg_risk=meta["neg_risk"]))
        resp = self.client.post_order(signed, OrderType.GTC)
        if not isinstance(resp, dict) or not resp.get("success", True) or resp.get("errorMsg"):
            raise RuntimeError(f"order refused: {resp}")
        return str(resp.get("status", ""))

    def cancel(self, order: OpenOrder) -> None:
        if self.client is None:
            raise RuntimeError("read-only adapter (no private key): cannot cancel orders")
        resp = self.client.cancel(order_id=order.id)   # order by order: never cancel_all on a shared wallet
        if isinstance(resp, dict) and resp.get("not_canceled"):
            raise RuntimeError(f"cancel refused: {resp}")

    def label(self, instrument) -> str:
        return self.labels.get(instrument, f"token {instrument[:8]}...")
