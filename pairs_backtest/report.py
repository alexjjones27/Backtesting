"""Static PNG charts for the backtest report."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap, TwoSlopeNorm  # noqa: E402

from .data import NY_TZ  # noqa: E402
from .engine import LONG_CYPH, SHORT_CYPH, Result  # noqa: E402

# Palette (validated light-mode tokens).
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
BLUE, ORANGE = "#2a78d6", "#eb6834"
NEG_RED = "#e34948"
MIDPOINT = "#f0efec"
ORDINAL_BLUES = ["#86b6ef", "#5598e7", "#2a78d6", "#1c5cab", "#0d366b"]

plt.rcParams.update(
    {
        "figure.facecolor": SURFACE,
        "axes.facecolor": SURFACE,
        "savefig.facecolor": SURFACE,
        "font.family": "sans-serif",
        "font.size": 10,
        "text.color": INK,
        "axes.labelcolor": INK_2,
        "axes.edgecolor": AXIS,
        "axes.linewidth": 1,
        "axes.grid": True,
        "axes.axisbelow": True,
        "grid.color": GRID,
        "grid.linewidth": 0.8,
        "grid.linestyle": "-",
        "xtick.color": MUTED,
        "ytick.color": MUTED,
        "xtick.labelcolor": INK_2,
        "ytick.labelcolor": INK_2,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.titleweight": "bold",
        "axes.titlesize": 12,
        "axes.titlelocation": "left",
        "legend.frameon": False,
        "lines.linewidth": 1.6,
        "lines.solid_capstyle": "round",
        "lines.solid_joinstyle": "round",
    }
)


def _local(idx: pd.DatetimeIndex) -> pd.DatetimeIndex:
    # Plot in New York time without tz info so matplotlib labels dates sensibly.
    return idx.tz_convert(NY_TZ).tz_localize(None)


def _save(fig, path: Path) -> None:
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def _gbp(x, _pos=None) -> str:
    return f"£{x:,.0f}" if x >= 0 else f"-£{-x:,.0f}"


def plot_prices(panel: pd.DataFrame, path: Path) -> None:
    t = _local(panel.index)
    cyph = panel["cyph_close"] / panel["cyph_close"].iloc[0] * 100
    zec = panel["zec_close"] / panel["zec_close"].iloc[0] * 100
    fig, ax = plt.subplots(figsize=(11, 4.2))
    ax.plot(t, zec, color=BLUE, label="ZEC (Zcash)")
    ax.plot(t, cyph, color=ORANGE, label="CYPH (Cypherpunk Technologies)")
    ax.axhline(100, color=AXIS, linewidth=1)
    ax.set_yscale("log")
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:g}"))
    ax.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    ax.set_yticks([25, 50, 100, 200, 400])
    ax.set_ylim(15, 400)
    for series, color in ((zec, BLUE), (cyph, ORANGE)):
        ax.plot(t[-1], series.iloc[-1], "o", color=color, markersize=6, markeredgecolor=SURFACE, markeredgewidth=2)
        ax.annotate(f"{series.iloc[-1]:.0f}", (t[-1], series.iloc[-1]), xytext=(8, 0),
                    textcoords="offset points", va="center", color=INK_2)
    ax.set_title("CYPH and ZEC, rebased to 100 at the first hourly bar (log scale)", pad=26)
    ax.set_ylabel("Index (start = 100)")
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncols=2)
    _save(fig, path)


def plot_spread_and_z(res: Result, path: Path) -> None:
    sig, cfg = res.signals, res.config
    t = _local(sig.index)
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 7), sharex=True, height_ratios=[1, 1.2])

    ax1.plot(t, sig["spread"], color=BLUE, label="ln(CYPH / ZEC)")
    ax1.plot(t, sig["mean"], color=INK_2, linewidth=1.2, label=f"Rolling mean ({cfg.lookback} bars)")
    ax1.set_title("Relative value: log price ratio of CYPH to ZEC", pad=26)
    ax1.set_ylabel("ln(CYPH / ZEC)")
    ax1.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncols=2)

    # Shade the bars where the headline strategy held a position.
    pos = sig["position"].to_numpy()
    for side, color, label in ((SHORT_CYPH, ORANGE, "Holding: short CYPH / long ZEC"),
                               (LONG_CYPH, BLUE, "Holding: long CYPH / short ZEC")):
        ax2.fill_between(t, -6, 6, where=pos == side, color=color, alpha=0.12, linewidth=0, label=label, step="mid")
    ax2.plot(t, sig["z"], color=INK, linewidth=1.1, label="Z-score")
    for lvl in (1, 2, 3):
        for s in (1, -1):
            ax2.axhline(s * lvl, color=AXIS if lvl != cfg.entry_z else INK_2, linewidth=0.9)
    ax2.axhline(0, color=INK_2, linewidth=0.9)
    ax2.set_ylim(-5.5, 5.5)
    ax2.set_yticks([-5, -3, -2, -1, 0, 1, 2, 3, 5])
    ax2.set_title(f"Z-score ({cfg.lookback}-bar window); shaded = position held at entry ±{cfg.entry_z:g}", pad=26)
    ax2.set_ylabel("Z-score")
    ax2.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncols=3)
    fig.tight_layout()
    _save(fig, path)


def plot_equity_curves(results: list[Result], path: Path) -> None:
    """Small multiples: one panel per threshold, the other thresholds ghosted in grey."""
    start = results[0].config.initial_capital_gbp
    fig, axes = plt.subplots(len(results), 1, figsize=(11, 2.0 * len(results)), sharex=True, sharey=True)
    for ax, res in zip(axes, results):
        for other in results:
            if other is not res:
                ax.plot(_local(other.equity.index), other.equity, color=AXIS, linewidth=1)
        eq = res.equity
        ax.plot(_local(eq.index), eq, color=BLUE, linewidth=1.8)
        ax.axhline(start, color=INK_2, linewidth=0.9)
        final = eq.iloc[-1]
        ax.set_title(f"Entry |z| > {res.config.entry_z:g}:  final {_gbp(final)} ({(final / start - 1) * 100:+.1f}%), "
                     f"{len(res.trades)} trades", fontsize=10)
        ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_gbp))
        ax.set_yticks([9_000, 10_000, 11_000])
    fig.suptitle(f"Portfolio equity by entry threshold (£{start:,.0f} start, "
                 f"£{results[0].config.leg_notional_gbp:,.0f} per leg, "
                 f"{results[0].config.lookback}-bar lookback, after costs)", x=0.01, ha="left",
                 fontweight="bold", fontsize=12)
    fig.tight_layout()
    _save(fig, path)


def plot_direction_bars(summary: pd.DataFrame, path: Path) -> None:
    x = np.arange(len(summary))
    w = 0.36
    fig, ax = plt.subplots(figsize=(11, 4.2))
    ax.bar(x - w / 2 - 0.01, summary["short_cyph_pnl_gbp"], width=w, color=ORANGE, label="Short CYPH / Long ZEC")
    ax.bar(x + w / 2 + 0.01, summary["long_cyph_pnl_gbp"], width=w, color=BLUE, label="Long CYPH / Short ZEC")
    ax.plot(x, summary["net_pnl_gbp"], "o", color=INK, markersize=6, markeredgecolor=SURFACE,
            markeredgewidth=2, label="Total net P&L")
    for xi, v in zip(x, summary["net_pnl_gbp"]):
        ax.annotate(_gbp(v), (xi, v), xytext=(0, 9 if v >= 0 else -14), textcoords="offset points",
                    ha="center", color=INK, fontsize=9)
    ax.axhline(0, color=AXIS, linewidth=1)
    ax.set_xticks(x, [f"{z:g}" for z in summary["entry_z"]])
    ax.set_xlabel("Entry threshold |z|")
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_gbp))
    lo, hi = ax.get_ylim()
    ax.set_ylim(lo - 0.08 * (hi - lo), hi + 0.08 * (hi - lo))
    ax.set_title("Net P&L by entry threshold, split by trade direction", pad=30)
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncols=3)
    _save(fig, path)


def plot_heatmap(grid: pd.DataFrame, path: Path) -> None:
    pivot = grid.pivot(index="lookback", columns="entry_z", values="net_pnl_gbp").sort_index()
    cmap = LinearSegmentedColormap.from_list("div", [NEG_RED, MIDPOINT, BLUE])
    lim = float(np.nanmax(np.abs(pivot.to_numpy())))
    norm = TwoSlopeNorm(vmin=-lim, vcenter=0, vmax=lim)
    fig, ax = plt.subplots(figsize=(11, 3.6))
    ax.grid(False)
    im = ax.imshow(pivot.to_numpy(), cmap=cmap, norm=norm, aspect="auto")
    for (r, c), v in np.ndenumerate(pivot.to_numpy()):
        rgba = cmap(norm(v))
        lum = 0.2126 * rgba[0] + 0.7152 * rgba[1] + 0.0722 * rgba[2]
        ax.text(c, r, _gbp(v), ha="center", va="center", fontsize=9, color=INK if lum > 0.55 else "white")
    ax.set_xticks(range(len(pivot.columns)), [f"{z:g}" for z in pivot.columns])
    days = {35: "~1 wk", 70: "~2 wks", 140: "~1 mth", 280: "~2 mths"}
    ax.set_yticks(range(len(pivot.index)), [f"{lb} bars ({days.get(lb, '')})" for lb in pivot.index])
    ax.set_xlabel("Entry threshold |z|")
    ax.set_ylabel("Z-score lookback")
    ax.set_title("Net P&L (GBP) by entry threshold and z-score lookback")
    cb = fig.colorbar(im, ax=ax, fraction=0.025, pad=0.01, format=matplotlib.ticker.FuncFormatter(_gbp))
    cb.outline.set_visible(False)
    _save(fig, path)
