"""Rotation: always hold the pair, tilted into whichever asset is cheap.

    default                         50% CYPH / 50% ZEC
    z < -threshold (CYPH cheap)     100% CYPH            (--tilt sets the share)
    z > +threshold (ZEC cheap)      100% ZEC
    z back to 0 (or a stop)         back to 50/50

The sleeve is always fully invested, so it carries the same market exposure as
holding both assets. Comparing it with a 50/50 mix rebalanced at the same
moments isolates the selection edge. A shuffled-history test then checks whether
that edge (and the pairs / long-only versions' edge) beats chance.

    python run_rotation.py
"""

from __future__ import annotations

import argparse
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

from pairs_backtest import report
from pairs_backtest.data import NY_TZ, load_panel
from pairs_backtest.engine import Config, compute_zscore, run_backtest
from pairs_backtest.metrics import daily_equity, max_drawdown
from pairs_backtest.placebo import STAT_NAMES, configs_for, p_values, run_placebo, strategy_stats
from pairs_backtest.report import gbp, md_table
from pairs_backtest.rotation import NEUTRAL, TO_CYPH, TO_ZEC, Schedule, cyph_weights, equity_curve, \
    schedule_from_trades, simulate

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "results" / "rotation"
CHARTS = OUT / "charts"

THRESHOLDS = [1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0]
CHART_THRESHOLDS = [1.0, 1.5, 2.0, 2.5, 3.0]
STOP_OFFSET = 1.5


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--refresh", action="store_true", help="re-download market data before running")
    p.add_argument("--lookback", type=int, default=70, help="z-score lookback in hourly bars")
    p.add_argument("--sleeve", type=float, default=1_000.0, help="GBP rotated between the two assets")
    p.add_argument("--tilt", type=float, default=1.0, help="share of the sleeve put in the cheap asset (0.5-1)")
    p.add_argument("--capital", type=float, default=10_000.0, help="starting portfolio, GBP")
    p.add_argument("--cost-bps-cyph", type=float, default=20.0, help="CYPH fees+slippage per side, bps")
    p.add_argument("--cost-bps-zec", type=float, default=20.0, help="ZEC fees+slippage per side, bps")
    p.add_argument("--placebo-runs", type=int, default=300, help="shuffled histories per shuffle style (0 = skip)")
    p.add_argument("--workers", type=int, default=4, help="processes for the shuffled-history test")
    return p.parse_args()


def pct(x: float) -> str:
    return f"{x:+.1f}%"


def sharpe(value: pd.Series) -> float:
    rets = daily_equity(value).pct_change().dropna()
    return float(rets.mean() / rets.std() * np.sqrt(252)) if rets.std() > 0 else float("nan")


class Study:
    def __init__(self, panel: pd.DataFrame, args: argparse.Namespace):
        self.panel, self.args = panel, args
        self.costs = (args.cost_bps_cyph, args.cost_bps_zec)
        self.base = Config(lookback=args.lookback)  # pairs-trade settings; only its signal schedule is used here
        self.start = panel.index.get_loc(compute_zscore(panel, args.lookback)["z"].first_valid_index()) + 1
        self.mid = panel.index[0] + (panel.index[-1] - panel.index[0]) / 2

    def run(self, sched: Schedule, weights: np.ndarray):
        rot = simulate(self.panel, sched, weights, self.args.sleeve, *self.costs)
        value = self.args.sleeve + equity_curve(self.panel, sched, rot, 0, self.args.sleeve, 0.0, *self.costs)
        return rot, value

    def hold(self, w_cyph: float):
        sched = Schedule(np.array([self.start]), np.array([NEUTRAL]))
        return self.run(sched, np.array([[w_cyph]]))

    def analyse(self, cfg: Config, bh_5050_pnl: float):
        trades = run_backtest(self.panel, cfg).trades
        sched = schedule_from_trades(self.panel, trades, self.start)
        rot, v_rot = self.run(sched, cyph_weights(sched.sides, self.args.tilt)[None, :])
        shadow, v_sh = self.run(sched, np.full((1, len(sched.bars)), 0.5))
        rel = v_rot / v_sh
        rel_mid = rel[rel.index <= self.mid].iloc[-1]
        active = np.searchsorted(sched.bars, np.arange(self.start, len(self.panel)), side="right") - 1
        dd_gbp, dd_pct = max_drawdown(v_rot)
        pnl = float(rot.pnl_gbp[0])
        row = {
            "entry_z": cfg.entry_z,
            "cyph_tilts": int((sched.sides == TO_CYPH).sum()),
            "zec_tilts": int((sched.sides == TO_ZEC).sum()),
            "pnl_gbp": pnl,
            "return_pct": pnl / self.args.sleeve * 100,
            "vs_buy_hold_5050_gbp": pnl - bh_5050_pnl,
            "same_time_5050_pnl_gbp": float(shadow.pnl_gbp[0]),
            "relative_wealth_pct": (rel.iloc[-1] - 1) * 100,
            "h1_relative_pct": (rel_mid - 1) * 100,
            "h2_relative_pct": (rel.iloc[-1] / rel_mid - 1) * 100,
            "costs_gbp": float(rot.costs_gbp[0]),
            "max_drawdown_gbp": dd_gbp,
            "max_drawdown_pct": dd_pct * 100,
            "sharpe": sharpe(v_rot),
            "time_tilted_pct": float((sched.sides[active] != NEUTRAL).mean() * 100),
        }
        return row, v_rot, rel


def results_table(df: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({
        "Entry \\|z\\|": df["entry_z"].map("{:g}".format),
        "Tilts (CYPH / ZEC)": df.apply(lambda r: f"{int(r.cyph_tilts)} / {int(r.zec_tilts)}", axis=1),
        "Time tilted": df["time_tilted_pct"].map("{:.0f}%".format),
        "P&L": df["pnl_gbp"].map(gbp),
        "Return on sleeve": df["return_pct"].map("{:+.0f}%".format),
        "vs buy & hold 50/50": df["vs_buy_hold_5050_gbp"].map(gbp),
        "Ahead of same-time 50/50": df["relative_wealth_pct"].map(pct),
        "H1 / H2": df.apply(lambda r: f"{pct(r.h1_relative_pct)} / {pct(r.h2_relative_pct)}", axis=1),
        "Costs": df["costs_gbp"].map(gbp),
        "Max drawdown": df.apply(lambda r: f"{gbp(r.max_drawdown_gbp)} ({r.max_drawdown_pct:.0f}%)", axis=1),
        "Sharpe": df["sharpe"].map("{:.2f}".format),
    })


def significance_table(actual: np.ndarray, p_bar: np.ndarray | None, p_day: np.ndarray | None) -> pd.DataFrame:
    """actual / p arrays have shape (thresholds, stats) in STAT_NAMES order."""
    g, n, r = (STAT_NAMES.index(k) for k in ("pair_gross_gbp", "pair_net_gbp", "rotation_log_excess"))

    def p_col(arr, j):
        return ["-"] * len(actual) if arr is None else [f"{v:.2f}" for v in arr[:, j]]

    columns = {
        "Entry \\|z\\|": [f"{e:g}" for e in THRESHOLDS],
        "Pairs trade net": [gbp(v) for v in actual[:, n]],
        "p (hours)": p_col(p_bar, n), "p (days)": p_col(p_day, n),
        "Long-only selection": [gbp(v / 2) for v in actual[:, g]],
        "p (hours)#2": p_col(p_bar, g), "p (days)#2": p_col(p_day, g),
        "Rotation vs same-time 50/50": [pct((np.exp(v) - 1) * 100) for v in actual[:, r]],
        "p (hours)#3": p_col(p_bar, r), "p (days)#3": p_col(p_day, r),
    }
    # Repeat the p-value headings under each strategy.
    return pd.DataFrame(list(zip(*columns.values())), columns=[c.split("#")[0] for c in columns])


def main() -> None:
    args = parse_args()
    CHARTS.mkdir(parents=True, exist_ok=True)
    panel = load_panel(refresh=args.refresh)
    st = Study(panel, args)

    holds = {"Buy & hold 50/50": st.hold(0.5), "Buy & hold ZEC": st.hold(0.0), "Buy & hold CYPH": st.hold(1.0)}
    bh_5050 = float(holds["Buy & hold 50/50"][0].pnl_gbp[0])
    bench = pd.DataFrame([
        {"name": name, "pnl_gbp": float(rot.pnl_gbp[0]), "return_pct": float(rot.pnl_gbp[0]) / args.sleeve * 100,
         "max_drawdown_gbp": max_drawdown(v)[0], "max_drawdown_pct": max_drawdown(v)[1] * 100, "sharpe": sharpe(v)}
        for name, (rot, v) in holds.items()
    ])
    bench.to_csv(OUT / "benchmarks.csv", index=False)

    variants = {"none": None, "stop": STOP_OFFSET}
    summaries, values, rels = {}, {}, {}
    for key, offset in variants.items():
        rows = []
        for cfg in configs_for(st.base, THRESHOLDS, offset):
            row, v, rel = st.analyse(cfg, bh_5050)
            rows.append(row)
            values[(key, cfg.entry_z)], rels[(key, cfg.entry_z)] = v, rel
        summaries[key] = pd.DataFrame(rows)
        summaries[key].to_csv(OUT / f"summary_rotation_{key}.csv", index=False)

    # Shuffled-history test for all three versions (pairs, long-only selection, rotation).
    all_cfgs = configs_for(st.base, THRESHOLDS, None) + configs_for(st.base, THRESHOLDS, STOP_OFFSET)
    actual = np.array([strategy_stats(panel, c, st.start, args.sleeve, args.tilt, st.costs) for c in all_cfgs])
    p = {"bar": None, "day": None}
    sims = {}
    if args.placebo_runs > 0:
        for block in p:
            t0 = time.time()
            sims[block] = run_placebo(panel, all_cfgs, st.start, args.sleeve, args.tilt, st.costs, block,
                                      args.placebo_runs, workers=args.workers)
            p[block] = p_values(actual, sims[block])
            print(f"shuffled-history test ({block} shuffles, {args.placebo_runs} runs): {time.time() - t0:.0f}s")
        med = {b: np.median(sims[b], axis=0) for b in sims}
        pd.DataFrame([
            {"variant": "none" if i < len(THRESHOLDS) else "stop", "entry_z": c.entry_z, "shuffle": b,
             **{f"median_{name}": med[b][i, j] for j, name in enumerate(STAT_NAMES)}}
            for b in med for i, c in enumerate(all_cfgs)
        ]).to_csv(OUT / "shuffled_medians.csv", index=False)
    k = len(THRESHOLDS)
    sig = {
        "none": significance_table(actual[:k], *(None if p[b] is None else p[b][:k] for b in ("bar", "day"))),
        "stop": significance_table(actual[k:], *(None if p[b] is None else p[b][k:] for b in ("bar", "day"))),
    }
    pd.concat([s.assign(variant=v) for v, s in sig.items()]).to_csv(OUT / "significance.csv", index=False)

    # Charts.
    report.plot_relative_wealth({e: rels[("none", e)] for e in CHART_THRESHOLDS}, CHARTS / "rot_01_relative_wealth.png")
    to_portfolio = lambda v: args.capital - args.sleeve + v  # noqa: E731
    report.plot_long_only_equity({
        "Rotation, entry |z| > 1": to_portfolio(values[("none", 1.0)]),
        "Rotation, entry |z| > 2": to_portfolio(values[("none", 2.0)]),
        "Buy & hold ZEC": to_portfolio(holds["Buy & hold ZEC"][1]),
        "Buy & hold 50/50": to_portfolio(holds["Buy & hold 50/50"][1]),
    }, CHARTS / "rot_02_equity_vs_buy_hold.png",
        title=f"Rotation vs simply holding (£{args.sleeve:,.0f} sleeve, £{args.capital:,.0f} portfolio, after costs)")
    if "bar" in sims:
        r = STAT_NAMES.index("rotation_log_excess")
        report.plot_null_distribution(
            {f"Entry |z| > {e:g}": (sims["bar"][:, THRESHOLDS.index(e), r], actual[THRESHOLDS.index(e), r])
             for e in (1.0, 2.0)},
            CHARTS / "rot_03_shuffled_history.png",
            title="Rotation on the real history vs on shuffled histories (no mean reversion)",
            xlabel="Rotation vs same-time 50/50",
            fmt=lambda v: f"{(np.exp(v) - 1) * 100:+.0f}%",
            ticks=np.log1p([-0.75, -0.5, 0.0, 1.0, 3.0]),
        )

    write_report(st, summaries, bench, sig)
    print(results_table(summaries["none"]).to_string(index=False))
    print(sig["none"].to_string(index=False))
    print(f"\nWrote results to {OUT}")


def write_report(st: Study, summaries, bench, sig) -> None:
    args, panel = st.args, st.panel
    day = lambda ts: ts.tz_convert(NY_TZ).strftime("%d %b %Y")  # noqa: E731
    bench_md = pd.DataFrame({
        f"Benchmark (£{args.sleeve:,.0f})": bench["name"],
        "P&L": bench["pnl_gbp"].map(gbp),
        "Return": bench["return_pct"].map("{:+.0f}%".format),
        "Max drawdown": bench.apply(lambda r: f"{gbp(r.max_drawdown_gbp)} ({r.max_drawdown_pct:.0f}%)", axis=1),
        "Sharpe": bench["sharpe"].map("{:.2f}".format),
    })
    runs = f"{args.placebo_runs} shuffled histories of each kind" if args.placebo_runs else "skipped (--placebo-runs 0)"
    text = f"""# Rotation ("always hold the pair, tilt into the cheap one"): backtest report

_Generated by `run_rotation.py`. All P&L is in GBP, after trading costs._

## Rules

A £{args.sleeve:,.0f} sleeve is always fully invested in CYPH and ZEC:

| Signal (same z-score as the pairs trade, {args.lookback}-bar lookback) | Holding |
|---|---|
| default | 50% CYPH / 50% ZEC |
| z < −threshold (CYPH cheap vs ZEC) | {args.tilt:.0%} CYPH / {1 - args.tilt:.0%} ZEC |
| z > +threshold (ZEC cheap vs CYPH) | {1 - args.tilt:.0%} CYPH / {args.tilt:.0%} ZEC |
| z back through 0 (or the stop-loss, or the end of the test) | back to 50% / 50% |

* Rebalances fill at the next bar's open, with {args.cost_bps_cyph:g} bps per side on CYPH and {args.cost_bps_zec:g} bps
  per side on ZEC. There is no shorting and no borrow.
* The sleeve compounds: gains stay invested, so later tilts are bigger in £.
* Period: {day(panel.index[st.start])} (first possible signal) → {day(panel.index[-1])}.

## Why this isolates the signal

The sleeve is always 100% invested in the Zcash complex, so the market's direction affects it about as much as
holding 50/50. The fairest comparison is a **50/50 mix rebalanced at the same moments** the rotation trades
("same-time 50/50"). The gap between the two comes only from which asset was overweighted, minus the extra trading
costs. That gap is the selection edge. "Ahead of same-time 50/50" is how much more the rotation ended with, as a
percentage. H1 / H2 splits it at {day(st.mid)}.

## Results (no stop-loss)

{md_table(results_table(summaries["none"]))}

## Results with a z stop-loss at entry + {STOP_OFFSET:g}

{md_table(results_table(summaries["stop"]))}

### Benchmarks over the same window

{md_table(bench_md)}

![Rotation vs same-time 50/50](charts/rot_01_relative_wealth.png)

![Equity vs buy & hold](charts/rot_02_equity_vs_buy_hold.png)

## Is any of it better than chance?

The **shuffled-history test** keeps ZEC's real path but rebuilds CYPH from the real hour-by-hour moves of the
CYPH/ZEC gap, put in a random order. The fake histories have the same size of moves and even end at the same
place, but any mean reversion is gone. The same rules are run on each one. The p-value is the share of shuffled
histories where the strategy did at least as well as on the real one. Below about 0.05 would mean the result is
hard to explain by luck. {runs}:

* **p (hours):** every hourly move is shuffled.
* **p (days):** whole days are shuffled, keeping each day's hours in order. This keeps intraday reversal (such as
  bid/ask bounce in CYPH's hourly prices), so it asks whether the strategy does better than that alone.

The long-only version's selection part is exactly half the pairs trade's gross P&L, so its test uses that.

### No stop-loss

{md_table(sig["none"])}

### With the stop-loss

{md_table(sig["stop"])}

![Shuffled-history test](charts/rot_03_shuffled_history.png)

## Files

* `summary_rotation_none.csv`, `summary_rotation_stop.csv`: all metrics per threshold
* `benchmarks.csv`: buy & hold results
* `significance.csv`: the shuffled-history tables above
"""
    (OUT / "REPORT.md").write_text(text)


if __name__ == "__main__":
    main()
