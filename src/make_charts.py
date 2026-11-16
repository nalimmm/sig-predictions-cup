"""Regenerate every chart of the write-up from the raw data in data/.

Run this script and the four PNG files land in charts/; the committed PNGs are its output.
All numbers on the charts are computed from the two CSV files; nothing is hard coded.

  charts/equity_full.png          account value, hourly, Oct 13 to the Nov 4 market freeze
  charts/equity_before_count.png  the same series truncated at Nov 3 23:59 UTC (before any ballot)
  charts/rolling_sharpe.png       annualized Sharpe on a rolling 72 hour window of hourly returns
  charts/exit_capacity.png        measured exit capacity per contract (the turbo scan)

Usage: python3 src/make_charts.py   (from the repository root)
"""
import csv
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "charts"
OUT.mkdir(exist_ok=True)

INK, SOFT, BLUE, RED, GRID = "#14213D", "#5B6B82", "#2563EB", "#B3261E", "#E4E8EE"
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9, "axes.edgecolor": "#C9CFD8",
                     "axes.linewidth": 0.8, "text.color": INK, "axes.labelcolor": SOFT,
                     "xtick.color": SOFT, "ytick.color": SOFT})

COUNT_START = datetime(2026, 11, 4, 0, 0, tzinfo=timezone.utc)
FULL_PORT = datetime(2026, 10, 24, 10, 0, tzinfo=timezone.utc)
SHARPE_WINDOW = 72   # hourly returns per rolling window
SHARPE_MIN = 48      # windows shorter than this are too noisy to plot


def load_history():
    rows = list(csv.DictReader(open(ROOT / "data" / "account_history_hourly.csv")))
    return ([datetime.fromisoformat(r["time"].replace("Z", "+00:00")) for r in rows],
            [float(r["totalValue"]) for r in rows])


def style(ax):
    ax.grid(axis="y", color=GRID, lw=0.7)
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))


def equity_full():
    x, y = load_history()
    f, ax = plt.subplots(figsize=(7.0, 2.9), dpi=200)
    ax.plot(x, y, color=BLUE, lw=1.2)
    ax.fill_between(x, y, 90000, color=BLUE, alpha=0.06)
    ax.annotate(f"{y[-1]:,.0f}", (x[-1], y[-1]), textcoords="offset points", xytext=(-4, 8),
                ha="right", fontsize=9, fontweight="bold", color=INK)
    ax.axvline(COUNT_START, color="#AEB7C4", lw=0.9)
    ax.annotate("midnight UTC, Nov 4", (COUNT_START, max(y) * 0.6), textcoords="offset points",
                xytext=(-6, 0), ha="right", fontsize=8, color=SOFT)
    ax.set_ylim(90000, max(y) * 1.1)
    ax.yaxis.set_major_formatter(lambda v, _: f"{int(v / 1000)}k")
    style(ax)
    f.tight_layout()
    f.savefig(OUT / "equity_full.png")
    plt.close(f)


def equity_before_count():
    x, y = load_history()
    pre = [(t, v) for t, v in zip(x, y) if t < COUNT_START]
    x, y = [t for t, _ in pre], [v for _, v in pre]
    gain = y[-1] / y[0] - 1
    f, ax = plt.subplots(figsize=(7.0, 2.9), dpi=200)
    ax.plot(x, y, color=BLUE, lw=1.3)
    ax.fill_between(x, y, min(y) * 0.98, color=BLUE, alpha=0.06)
    ax.axvline(FULL_PORT, color="#AEB7C4", lw=0.9)
    ax.annotate("full port, Oct 24", (FULL_PORT, max(y)), textcoords="offset points",
                xytext=(5, 0), fontsize=8, color=SOFT)
    ax.annotate(f"{y[-1]:,.0f}", (x[-1], y[-1]), textcoords="offset points", xytext=(-4, -18),
                ha="right", fontsize=9, fontweight="bold", color=INK)
    ax.set_title(f"{gain:+.0%} before a single vote was counted", loc="left",
                 fontsize=11.5, fontweight="bold", color=INK, pad=10)
    ax.set_ylim(min(y) * 0.98, max(y) * 1.05)
    ax.yaxis.set_major_formatter(lambda v, _: f"{int(v / 1000)}k")
    style(ax)
    f.tight_layout()
    f.savefig(OUT / "equity_before_count.png")
    plt.close(f)


def rolling_sharpe():
    x, y = load_history()
    rets = [(x[i], y[i] / y[i - 1] - 1) for i in range(1, len(y)) if y[i - 1] > 0]
    series = []
    for i in range(len(rets)):
        win = [r for _, r in rets[max(0, i - SHARPE_WINDOW + 1):i + 1]]
        if len(win) >= SHARPE_MIN:
            sd = statistics.stdev(win)
            if sd > 1e-12:
                series.append((rets[i][0], statistics.fmean(win) / sd * math.sqrt(24 * 365)))
    full = [r for _, r in rets]
    full_run = statistics.fmean(full) / statistics.stdev(full) * math.sqrt(24 * 365)
    sx, sy = [t for t, _ in series], [v for _, v in series]
    f, ax = plt.subplots(figsize=(7.0, 2.9), dpi=200)
    ax.axhline(0, color=SOFT, lw=0.8, ls=(0, (4, 4)))
    ax.axhline(full_run, color=RED, lw=1.0, ls=(0, (6, 4)))
    ax.plot(sx, sy, color=BLUE, lw=1.4)
    ax.annotate(f"full run: {full_run:.1f}", (sx[-1], full_run), textcoords="offset points",
                xytext=(0, -13), ha="right", fontsize=8, color=RED)
    for d, label in ((FULL_PORT, "full port"), (COUNT_START, "election night")):
        ax.axvline(d, color="#AEB7C4", lw=0.9)
        ax.annotate(label, (d, max(sy) * 1.08), textcoords="offset points", xytext=(-5, 0),
                    ha="right", fontsize=8, color=SOFT)
    ax.set_title("Rolling 72h Sharpe: earned, spent at the full port, repaid by the night",
                 loc="left", fontsize=11.5, fontweight="bold", color=INK, pad=10)
    ax.set_ylim(min(sy) * 1.15, max(sy) * 1.15)
    style(ax)
    f.tight_layout()
    f.savefig(OUT / "rolling_sharpe.png")
    plt.close(f)


def exit_capacity():
    rows = list(csv.DictReader(open(ROOT / "data" / "turbo_scan_results.csv")))
    col = "entry06_target15_capacity_median_susqies"
    caps = sorted(((r["title"], float(r[col])) for r in rows if r[col] and float(r[col]) > 5000),
                  key=lambda x: -x[1])
    labels = [t.replace("Will the ", "").replace(" win the", "").rstrip("?") for t, _ in caps]
    values = [c for _, c in caps]
    cycles = sum(int(r["entry06_target15_cycles"] or 0) for r in rows)
    wins = sum(int(r["entry06_target15_wins"] or 0) for r in rows)
    f, ax = plt.subplots(figsize=(7.0, 0.42 * len(caps) + 1.2), dpi=200)
    ypos = list(range(len(caps)))[::-1]
    ax.barh(ypos, values, color="#AEB7C4", height=0.62)
    for yp, c in zip(ypos, values):
        ax.text(c + max(values) * 0.015, yp, f"{c:,.0f}", va="center", fontsize=8.5, color=INK)
    ax.set_yticks(ypos)
    ax.set_yticklabels(labels, fontsize=8.5, color=INK)
    ax.set_title(f"Exit capacity, not hit rate, was the binding constraint "
                 f"(the cycle won {wins} of {cycles})", loc="left", fontsize=11, fontweight="bold",
                 color=INK, pad=10)
    ax.set_xlim(0, max(values) * 1.15)
    ax.set_xticks([])
    ax.spines[["top", "right", "bottom"]].set_visible(False)
    f.tight_layout()
    f.savefig(OUT / "exit_capacity.png")
    plt.close(f)


if __name__ == "__main__":
    equity_full()
    equity_before_count()
    rolling_sharpe()
    exit_capacity()
    print(f"4 charts written to {OUT}")
