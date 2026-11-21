"""Summary of a journal directory (paper trading or backtest): what S1 saw and what it earned.

Two readings, kept apart on purpose:
  - the signal study: after every signal, traded or not, how much of the fall came back after
    1, 5, 15, 30 and 60 minutes, against the round trip cost (the spread when it fired). The
    design note's bar for S1: a mean return over twice the round trip cost on at least 200
    out-of-sample events;
  - the paper book: orders, fills, closed trades, P&L and drawdown of the virtual 100 USDC.
"""
import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, median
from typing import Dict, List


def read(journal_dir) -> List[dict]:
    out = []
    for f in sorted(Path(journal_dir).glob("*.jsonl")):
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


def summary(recs: List[dict]) -> dict:
    sig = {r["id"]: r for r in recs if r["k"] == "signal"}
    skips = Counter(r.get("skip") or "traded" for r in sig.values())
    skips.update(r["why"].split(":")[0] if r["k"] == "skip" else "entry_missed:" + r["why"]
                 for r in recs if r["k"] in ("skip", "entry_missed"))
    study: Dict[int, List[tuple]] = defaultdict(list)
    for r in recs:
        if r["k"] == "followup" and r.get("mid") is not None and r["id"] in sig:
            s = sig[r["id"]]
            gain = r["mid"] - s["mid"]
            cost = s["ask"] - s["bid"]
            study[r["minutes"]].append((r.get("reverted"), gain, cost))
    horizons = {}
    for h, rows in sorted(study.items()):
        rev = [x[0] for x in rows if x[0] is not None]
        horizons[f"{h} min"] = {
            "n": len(rows),
            "mean_reverted_frac": round(mean(rev), 3) if rev else None,
            "median_reverted_frac": round(median(rev), 3) if rev else None,
            "mean_gain": round(mean(x[1] for x in rows), 4),
            "mean_round_trip_cost": round(mean(x[2] for x in rows), 4),
            "gain_over_cost": round(mean(x[1] for x in rows) / mean(x[2] for x in rows), 2)
            if mean(x[2] for x in rows) > 0 else None,
            "share_positive": round(sum(1 for x in rows if x[1] > 0) / len(rows), 3),
        }
    trades = defaultdict(lambda: {"pnl": 0.0, "cost": 0.0, "how": set()})
    for r in recs:
        if r["k"] == "exit":
            t = trades[r["signal"]]
            t["pnl"] += r["pnl"]
            t["cost"] += r["pnl"] / r["ret"] if r.get("ret") else 0.0
            t["how"].add(r["how"])
    pnls = [t["pnl"] for t in trades.values()]
    navs = [r for r in recs if r["k"] == "nav"]
    peak, mdd = None, 0.0
    for r in navs:
        peak = r["nav"] if peak is None else max(peak, r["nav"])
        mdd = max(mdd, 1 - r["nav"] / peak if peak else 0.0)
    exits = Counter(h for t in trades.values() for h in t["how"])
    return {
        "from": recs[0].get("at") if recs else None,
        "to": recs[-1].get("at") if recs else None,
        "signals": len(sig),
        "outcomes": dict(skips),
        "study": horizons,
        "orders": sum(1 for r in recs if r["k"] == "order"),
        "fills": sum(1 for r in recs if r["k"] == "fill"),
        "closed_trades": len(trades),
        "exit_kinds": dict(exits),
        "pnl_usdc": round(sum(pnls), 4),
        "mean_pnl_per_trade": round(mean(pnls), 4) if pnls else None,
        "win_rate": round(sum(1 for p in pnls if p > 0) / len(pnls), 3) if pnls else None,
        "nav": navs[-1]["nav"] if navs else None,
        "max_drawdown": round(mdd, 4),
        "halted": navs[-1].get("halted") if navs else None,
        "breakers": [{"at": r["at"], "what": r["what"], "why": r.get("why")} for r in recs if r["k"] == "breaker"],
    }


def render(s: dict) -> str:
    lines = [f"Période : {s['from']} -> {s['to']}",
             f"Signaux S1 : {s['signals']}   issues : {s['outcomes']}",
             "", "Étude (tous les signaux, tradés ou non) :"]
    for h, x in s["study"].items():
        lines.append(f"  {h:>7} n={x['n']:<5} retour moyen {x['mean_reverted_frac']} de la chute "
                     f"(médiane {x['median_reverted_frac']}), gain moyen {x['mean_gain']} "
                     f"pour un coût aller-retour de {x['mean_round_trip_cost']} "
                     f"(ratio {x['gain_over_cost']}), part positive {x['share_positive']}")
    lines += ["", f"Papier : {s['orders']} ordres, {s['fills']} remplissages, {s['closed_trades']} trades fermés "
                  f"{s['exit_kinds']}",
              f"P&L {s['pnl_usdc']} USDC, moyenne {s['mean_pnl_per_trade']} par trade, "
              f"gagnants {s['win_rate']}",
              f"Valeur liquidative {s['nav']}, drawdown max {s['max_drawdown']}, arrêt : {s['halted']}"]
    for b in s["breakers"]:
        lines.append(f"  coupe-circuit {b['at']} {b['what']} {b['why'] or ''}")
    return "\n".join(lines)
