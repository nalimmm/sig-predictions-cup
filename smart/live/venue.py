"""Where orders go: nowhere (DryVenue, shadow mode) or the Polymarket CLOB (ClobVenue, real mode).

Both answer the same calls, so the order layer and the safeguards run the same code in both
modes. Calls are blocking; the runner makes them from one worker thread, one at a time.
"""
import itertools
import os
from pathlib import Path
from typing import Dict, List, Optional

import requests

from .orders import Want

CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"


class OrderRefused(Exception):
    pass


class DryVenue:
    """Builds nothing, signs nothing, sends nothing: hands out an id and remembers the order."""
    live = False

    def __init__(self):
        self.ids = itertools.count(1)
        self.open: Dict[str, dict] = {}

    def place(self, w: Want, post_only: bool = True, taker: bool = False) -> str:
        oid = f"dry-{next(self.ids)}"
        self.open[oid] = {"id": oid, "asset_id": w.token, "side": w.side, "price": w.price,
                          "original_size": w.size, "size_matched": 0.0}
        return oid

    def cancel(self, ids: List[str]):
        for i in ids:
            self.open.pop(i, None)

    def cancel_all(self):
        self.open.clear()

    def heartbeat(self):
        pass


def read_key(cred_name: str = "polymarket-key", path: Optional[str] = None) -> Optional[str]:
    """The wallet key from a systemd credential ($CREDENTIALS_DIRECTORY/<name>) or a root-only
    file. Never logged, never written anywhere."""
    p = Path(path) if path else (Path(os.environ["CREDENTIALS_DIRECTORY"]) / cred_name
                                 if os.environ.get("CREDENTIALS_DIRECTORY") else None)
    if p is None or not p.exists():
        return None
    k = p.read_text().strip()
    return k or None


class ClobVenue:
    """Real orders through py-clob-client. `funder` is the address that holds the funds and
    `signature_type` the kind of account (0 plain wallet, 1 email or Magic login, 2 browser wallet),
    both from /etc/smart-live/wallet.env, never from the repository."""
    live = True

    def __init__(self, key: str, funder: str, signature_type: int, markets: Dict[str, dict], client=None):
        if client is None:
            from py_clob_client.client import ClobClient
            client = ClobClient(CLOB, key=key, chain_id=137, signature_type=signature_type, funder=funder)
            client.set_api_creds(client.create_or_derive_api_creds())
        self.client, self.funder, self.markets = client, funder, markets
        self.hb_id: Optional[str] = None

    def _opts(self, token: str):
        from py_clob_client.clob_types import PartialCreateOrderOptions
        m = self.markets[token]
        tick = format(m["tick"], "g")
        return PartialCreateOrderOptions(tick_size=tick, neg_risk=bool(m.get("neg_risk")))

    def place(self, w: Want, post_only: bool = True, taker: bool = False) -> str:
        from py_clob_client.clob_types import OrderArgs, OrderType
        signed = self.client.create_order(OrderArgs(token_id=w.token, price=w.price, size=w.size, side=w.side),
                                          self._opts(w.token))
        resp = self.client.post_order(signed, OrderType.FAK if taker else OrderType.GTC, post_only=post_only and not taker)
        if not isinstance(resp, dict) or resp.get("success") is False or resp.get("errorMsg") or not resp.get("orderID"):
            raise OrderRefused(str(resp)[:300])
        return str(resp["orderID"])

    def cancel(self, ids: List[str]):
        if ids:
            resp = self.client.cancel_orders(list(ids))
            bad = resp.get("not_canceled") if isinstance(resp, dict) else None
            if bad:
                # an order already filled or gone cannot be cancelled: not an error for us
                return bad
        return None

    def cancel_all(self):
        return self.client.cancel_all()

    def heartbeat(self):
        """With heartbeats running, Polymarket cancels every order of the account if none arrives
        for 10 s: if this process dies or hangs, its orders do not stay in the book."""
        resp = self.client.post_heartbeat(self.hb_id)
        if isinstance(resp, dict) and resp.get("heartbeat_id"):
            self.hb_id = resp["heartbeat_id"]

    def orders(self) -> Dict[str, dict]:
        return {str(o["id"]): o for o in self.client.get_orders() or []}

    def order(self, oid: str) -> Optional[dict]:
        return self.client.get_order(oid)

    def token_balance(self, token: str) -> float:
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
        r = self.client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token))
        return float(r.get("balance", 0)) / 1e6

    def cash(self) -> float:
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
        r = self.client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        return float(r.get("balance", 0)) / 1e6

    def allowances(self) -> dict:
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
        r = self.client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        return r.get("allowances") or {"allowance": r.get("allowance")}


def wallet_positions(funder: str, get=None) -> List[dict]:
    """Every position of the wallet from the Data API v2 (public, by address): token, size, avg."""
    get = get or (lambda url, **p: requests.get(url, params=p, timeout=15).json())
    out, cursor = [], None
    for _ in range(50):
        params = {"user": funder, "limit": 500}
        if cursor:
            params["cursor"] = cursor
        d = get(f"{DATA}/v2/positions", **params)
        rows = d.get("data", []) if isinstance(d, dict) else d
        for r in rows or []:
            out.append({"token": str(r.get("token_id") or r.get("asset")),
                        "size": float(r.get("current_size") or r.get("size") or 0),
                        "avg": float(r.get("avg_price") or r.get("avgPrice") or 0),
                        "price": float(r.get("current_price") or r.get("curPrice") or 0)})
        cursor = (d.get("pagination") or {}).get("next_cursor") if isinstance(d, dict) else None
        if not cursor:
            break
    return out
