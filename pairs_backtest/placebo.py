"""Shuffled-history (placebo) significance test.

Keeps ZEC's real price path but rebuilds CYPH from the real hour-by-hour moves of
the spread ln(CYPH / ZEC), put in a random order. The shuffled pair has exactly
the same size and fat tails of moves, and even ends at the same place, but any
mean reversion is gone. Running the same rules on hundreds of shuffled histories
shows how well they do on a pair with nothing to exploit; the p-value is the
share of shuffles that did at least as well as the real history.

block="bar"  shuffles every hourly move independently.
block="day"  shuffles whole trading days, keeping each day's hours in order. That
             keeps intraday reversal (for example bid/ask bounce in CYPH's hourly
             closes), so it asks whether the strategy earns more than that alone.

A simpler "coin-flip" test (keep the trade times, pick the direction at random)
was tried first and rejected: on random-walk histories it reported p < 0.05 far
more often than 5% of the time, because the exits depend on the spread's path.
"""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace

import numpy as np
import pandas as pd

from .data import NY_TZ
from .engine import Config, run_backtest
from .rotation import cyph_weights, schedule_from_trades, simulate

STAT_NAMES = ("pair_gross_gbp", "pair_net_gbp", "rotation_log_excess")


def _spread_moves(panel: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """Spread change from the previous close to each open, and within each bar."""
    s_close = np.log(panel["cyph_close"].to_numpy()) - np.log(panel["zec_close"].to_numpy())
    s_open = np.log(panel["cyph_open"].to_numpy()) - np.log(panel["zec_open"].to_numpy())
    gap = np.r_[0.0, s_open[1:] - s_close[:-1]]
    return gap, s_close - s_open


def shuffled_panel(panel: pd.DataFrame, rng: np.random.Generator, block: str = "bar") -> pd.DataFrame:
    gap, bar = _spread_moves(panel)
    n = len(panel)
    if block == "bar":
        order = np.r_[0, 1 + rng.permutation(n - 1)]
    elif block == "day":
        day = panel.index.tz_convert(NY_TZ).date
        firsts = np.flatnonzero(np.r_[True, day[1:] != day[:-1]])
        days = np.split(np.arange(n), firsts[1:])
        order = np.concatenate([days[0]] + [days[i] for i in 1 + rng.permutation(len(days) - 1)])
    else:
        raise ValueError(f"unknown block {block!r}")
    g, b = gap[order], bar[order]
    g[0] = 0.0
    s_open0 = np.log(panel["cyph_open"].iloc[0] / panel["zec_open"].iloc[0])
    path = s_open0 + np.cumsum(np.column_stack([g, b]).ravel())
    out = panel.copy()
    out["cyph_open"] = panel["zec_open"].to_numpy() * np.exp(path[0::2])
    out["cyph_close"] = panel["zec_close"].to_numpy() * np.exp(path[1::2])
    return out


def strategy_stats(panel: pd.DataFrame, cfg: Config, start_idx: int, sleeve_gbp: float, tilt: float,
                   rot_cost_bps: tuple[float, float]) -> tuple[float, float, float]:
    """(pairs gross P&L, pairs net P&L, rotation log excess vs 50/50 rebalanced at the same times)."""
    trades = run_backtest(panel, cfg).trades
    if trades.empty:
        return 0.0, 0.0, 0.0
    sched = schedule_from_trades(panel, trades, start_idx)
    rot = simulate(panel, sched, cyph_weights(sched.sides, tilt), sleeve_gbp, *rot_cost_bps).pnl_gbp[0]
    shadow = simulate(panel, sched, np.full((1, len(sched.bars)), 0.5), sleeve_gbp, *rot_cost_bps).pnl_gbp[0]
    return (float(trades["gross_pnl_gbp"].sum()), float(trades["net_pnl_gbp"].sum()),
            float(np.log((sleeve_gbp + rot) / (sleeve_gbp + shadow))))


def _worker(job):
    panel, configs, start_idx, sleeve, tilt, costs, block, seed, count = job
    rng = np.random.default_rng(seed)
    out = np.empty((count, len(configs), len(STAT_NAMES)))
    for i in range(count):
        fake = shuffled_panel(panel, rng, block)
        for j, cfg in enumerate(configs):
            out[i, j] = strategy_stats(fake, cfg, start_idx, sleeve, tilt, costs)
    return out


def run_placebo(panel: pd.DataFrame, configs: list[Config], start_idx: int, sleeve_gbp: float, tilt: float,
                rot_cost_bps: tuple[float, float], block: str, n: int, seed: int = 2026,
                workers: int = 4) -> np.ndarray:
    """Statistics on `n` shuffled histories, shape (n, configs, stats)."""
    workers = max(1, min(workers, n))
    counts = [n // workers + (i < n % workers) for i in range(workers)]
    jobs = [(panel, configs, start_idx, sleeve_gbp, tilt, rot_cost_bps, block, seed + i, c)
            for i, c in enumerate(counts) if c]
    if workers == 1:
        return np.concatenate([_worker(j) for j in jobs])
    with ProcessPoolExecutor(max_workers=workers) as pool:
        return np.concatenate(list(pool.map(_worker, jobs)))


def p_values(actual: np.ndarray, sims: np.ndarray) -> np.ndarray:
    """Share of shuffles at least as good as the real history, per config and statistic."""
    return (sims >= actual[None, :, :] - 1e-12).mean(axis=0)


def configs_for(base: Config, thresholds: list[float], stop_offset: float | None) -> list[Config]:
    return [replace(base, entry_z=e, stop_z=None if stop_offset is None else e + stop_offset) for e in thresholds]
