"""Build results/dashboard.html: the recommended strategy and its equity curve vs the alternatives.

Recommended (chosen from the middle of the robust region, not the single best cell):
    rotation, 70-hour lookback, tilt 100% into the cheap asset when |z| > 1.5,
    back to 50/50 when z crosses 0, or if |z| reaches 3.0 (stop-loss).

Run `python run_rotation.py` first: the page quotes its shuffled-history p-values.

    python make_dashboard.py
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

from pairs_backtest.data import NY_TZ, load_panel
from pairs_backtest.engine import Config, compute_zscore, run_backtest
from pairs_backtest.metrics import daily_equity, max_drawdown
from pairs_backtest.rotation import NEUTRAL, Schedule, cyph_weights, equity_curve, schedule_from_trades, simulate

ROOT = Path(__file__).resolve().parent
TEMPLATE = ROOT / "dashboard" / "template.html"
OUT = ROOT / "results" / "dashboard.html"
PVALUES = ROOT / "results" / "rotation" / "pvalues.csv"

SLEEVE = 1_000.0
COSTS = (20.0, 20.0)
LOOKBACK, ENTRY, STOP = 70, 1.5, 3.0
GRID_LOOKBACKS = [35, 70, 140]
GRID_THRESHOLDS = [1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 3.0]
GRID_STOP_OFFSETS = [None, 1.0, 1.5, 2.0]


def rotation_value(panel, trades, start):
    sched = schedule_from_trades(panel, trades, start)
    rot = simulate(panel, sched, cyph_weights(sched.sides, 1.0)[None, :], SLEEVE, *COSTS)
    return SLEEVE + equity_curve(panel, sched, rot, 0, SLEEVE, 0.0, *COSTS), sched


def hold_value(panel, start, w_cyph):
    sched = Schedule(np.array([start]), np.array([NEUTRAL]))
    rot = simulate(panel, sched, np.array([[w_cyph]]), SLEEVE, *COSTS)
    return SLEEVE + equity_curve(panel, sched, rot, 0, SLEEVE, 0.0, *COSTS)


def engine_value(panel, cfg, start):
    """Engine P&L re-based to GBP 1,000 committed (one position, GBP 1,000 per leg)."""
    res = run_backtest(panel, cfg)
    return SLEEVE + (res.equity.iloc[start:] - cfg.initial_capital_gbp), res


def series_stats(value: pd.Series) -> dict:
    daily = daily_equity(value)
    rets = daily.pct_change().dropna()
    return {
        "final": round(float(value.iloc[-1]), 2),
        "return_pct": round(float(value.iloc[-1] / SLEEVE - 1) * 100, 1),
        "max_dd_pct": round(max_drawdown(value)[1] * 100, 1),
        "sharpe": round(float(rets.mean() / rets.std() * np.sqrt(252)), 2),
    }


def robustness_grid(panel):
    """Rotation vs a 50/50 mix rebalanced at the same moments, on a window every lookback can trade."""
    start = panel.index.get_loc(compute_zscore(panel, max(GRID_LOOKBACKS))["z"].first_valid_index()) + 1
    mid = panel.index[start] + (panel.index[-1] - panel.index[start]) / 2
    rows = []
    for lb in GRID_LOOKBACKS:
        for e in GRID_THRESHOLDS:
            for off in GRID_STOP_OFFSETS:
                trades = run_backtest(panel, Config(lookback=lb, entry_z=e, stop_z=None if off is None else e + off)).trades
                trades = trades[trades["entry_time"] >= panel["bar_start"].iloc[start]]
                v_rot, sched = rotation_value(panel, trades, start)
                shadow = simulate(panel, sched, np.full((1, len(sched.bars)), 0.5), SLEEVE, *COSTS)
                v_sh = SLEEVE + equity_curve(panel, sched, shadow, 0, SLEEVE, 0.0, *COSTS)
                rel = v_rot / v_sh
                rel_mid = rel[rel.index <= mid].iloc[-1]
                rows.append({"lookback": lb, "entry": e, "stop": off, "ahead_pct": (rel.iloc[-1] - 1) * 100,
                             "h1_pct": (rel_mid - 1) * 100, "h2_pct": (rel.iloc[-1] / rel_mid - 1) * 100})
    return pd.DataFrame(rows), start, mid


def main() -> None:
    panel = load_panel()
    start = panel.index.get_loc(compute_zscore(panel, LOOKBACK)["z"].first_valid_index()) + 1
    day = lambda ts: ts.tz_convert(NY_TZ).strftime("%-d %b %Y")  # noqa: E731

    pair_cfg = Config(lookback=LOOKBACK, entry_z=ENTRY, stop_z=STOP)
    pairs, pairs_res = engine_value(panel, pair_cfg, start)
    long_only, _ = engine_value(panel, replace(pair_cfg, mode="long_only"), start)
    rotation, sched = rotation_value(panel, pairs_res.trades, start)
    curves = {
        "rotation": rotation,
        "hold_zec": hold_value(panel, start, 0.0),
        "hold_5050": hold_value(panel, start, 0.5),
        "pairs": pairs,
        "long_only": long_only,
        "hold_cyph": hold_value(panel, start, 1.0),
    }
    daily = pd.DataFrame({k: daily_equity(v) for k, v in curves.items()})

    pv = pd.read_csv(PVALUES)
    pv = pv[(pv["variant"] == "stop") & (pv["entry_z"] == ENTRY)]
    p_of = lambda strat: {r.shuffle: round(float(r.p), 2) for r in pv[pv["strategy"] == strat].itertuples()}  # noqa: E731

    grid, grid_start, grid_mid = robustness_grid(panel)
    fam = grid[grid["stop"] == STOP - ENTRY]
    best_h1 = grid.loc[grid["h1_pct"].idxmax()]
    top5 = grid.nlargest(5, "h1_pct")
    chosen = grid[(grid["lookback"] == LOOKBACK) & (grid["entry"] == ENTRY) & (grid["stop"] == STOP - ENTRY)].iloc[0]

    data = {
        "period": {"start": day(panel.index[start]), "end": day(panel.index[-1]), "days": len(daily)},
        "dates": [str(d) for d in daily.index],
        "series": {k: [round(float(v), 2) for v in daily[k]] for k in daily},
        "stats": {k: series_stats(v) for k, v in curves.items()},
        "pvalues": {"rotation": p_of("rotation"), "pairs": p_of("pairs"), "long_only": p_of("long_only_selection")},
        "rotation": {"tilts": int((sched.sides != NEUTRAL).sum()),
                     "to_cyph": int((sched.sides == 1).sum()), "to_zec": int((sched.sides == -1).sum())},
        "grid": {
            "window_start": day(panel.index[grid_start]), "split": day(grid_mid),
            "lookbacks": GRID_LOOKBACKS, "thresholds": GRID_THRESHOLDS,
            "values": [[round(float(fam[(fam["lookback"] == lb) & (fam["entry"] == e)]["ahead_pct"].iloc[0]), 1)
                        for e in GRID_THRESHOLDS] for lb in GRID_LOOKBACKS],
            "chosen": {"lookback": LOOKBACK, "entry": ENTRY},
            "chosen_h1": round(float(chosen["h1_pct"]), 1), "chosen_h2": round(float(chosen["h2_pct"]), 1),
            "n_settings": len(grid),
            "best_h1": {"lookback": int(best_h1["lookback"]), "entry": float(best_h1["entry"]),
                        "stop": None if pd.isna(best_h1["stop"]) else float(best_h1["stop"]),
                        "h1": round(float(best_h1["h1_pct"]), 1), "h2": round(float(best_h1["h2_pct"]), 1)},
            "top5_h2_mean": round(float(top5["h2_pct"].mean()), 1),
            "median_h2": round(float(grid["h2_pct"].median()), 1),
            "share_positive_both": round(float(((grid["h1_pct"] > 0) & (grid["h2_pct"] > 0)).mean()) * 100),
        },
    }
    html = TEMPLATE.read_text().replace("/*__DATA__*/null", json.dumps(data, separators=(",", ":")))
    OUT.write_text(html)
    print(json.dumps({k: data[k] for k in ("period", "stats", "pvalues", "rotation")}, indent=1))
    print(json.dumps({k: v for k, v in data["grid"].items() if k not in ("values",)}, indent=1))
    print(np.array(data["grid"]["values"]))
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
