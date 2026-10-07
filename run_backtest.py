"""Run the CYPH vs ZEC z-score pairs-trading study and write results/.

    python run_backtest.py              # use cached data in data/
    python run_backtest.py --refresh    # re-download the latest year first
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

import pandas as pd

from pairs_backtest.data import NY_TZ, load_panel
from pairs_backtest.engine import Config, compute_zscore, run_backtest
from pairs_backtest.metrics import pair_diagnostics, summarize
from pairs_backtest import report
from pairs_backtest.report import gbp, md_table

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
CHARTS = RESULTS / "charts"

THRESHOLDS = [1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0]
LOOKBACKS = [35, 70, 140, 280]
CHART_THRESHOLDS = [1.0, 1.5, 2.0, 2.5, 3.0]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--refresh", action="store_true", help="re-download market data before running")
    p.add_argument("--lookback", type=int, default=70, help="headline z-score lookback in hourly bars")
    p.add_argument("--exit-z", type=float, default=0.0, help="close when z reverts to this level")
    p.add_argument("--capital", type=float, default=10_000.0, help="starting portfolio, GBP")
    p.add_argument("--leg-notional", type=float, default=1_000.0, help="GBP allocated to each leg")
    p.add_argument("--cost-bps-cyph", type=float, default=20.0, help="CYPH fees+slippage per side, bps")
    p.add_argument("--cost-bps-zec", type=float, default=20.0, help="ZEC fees+slippage per side, bps")
    p.add_argument("--borrow-cyph", type=float, default=0.10, help="annual cost of shorting CYPH")
    p.add_argument("--borrow-zec", type=float, default=0.10, help="annual cost of shorting ZEC")
    return p.parse_args()


# ---------------------------------------------------------------- formatting


def headline_table(summary: pd.DataFrame, leg_notional: float) -> pd.DataFrame:
    s = summary
    return pd.DataFrame(
        {
            "Entry \\|z\\|": s["entry_z"].map("{:g}".format),
            "Trades": s["trades"],
            "Win rate": s["win_rate_pct"].map(lambda v: "-" if pd.isna(v) else f"{v:.0f}%"),
            "Net P&L": s["net_pnl_gbp"].map(gbp),
            "Portfolio return": s["portfolio_return_pct"].map("{:+.1f}%".format),
            f"Return on £{leg_notional:,.0f} leg": (s["net_pnl_gbp"] / leg_notional * 100).map("{:+.0f}%".format),
            "Avg trade": s["avg_trade_gbp"].map(lambda v: "-" if pd.isna(v) else gbp(v)),
            "Worst trade": s["worst_trade_gbp"].map(lambda v: "-" if pd.isna(v) else gbp(v)),
            "Profit factor": s["profit_factor"].map(lambda v: "-" if pd.isna(v) else f"{v:.2f}"),
            "Max drawdown": s.apply(lambda r: f"{gbp(r.max_drawdown_gbp)} ({r.max_drawdown_pct:.1f}%)", axis=1),
            "Sharpe": s["sharpe"].map("{:.2f}".format),
            "Avg hold": s["avg_hold_hours"].map(lambda v: "-" if pd.isna(v) else f"{v / 24:.1f} days"),
            "In market": s["time_in_market_pct"].map("{:.0f}%".format),
            "Costs + borrow": (s["costs_gbp"] + s["borrow_gbp"]).map(gbp),
        }
    )


# ---------------------------------------------------------------- study


def main() -> None:
    args = parse_args()
    CHARTS.mkdir(parents=True, exist_ok=True)

    panel = load_panel(refresh=args.refresh)
    diag = pair_diagnostics(panel)
    base = Config(
        exit_z=args.exit_z,
        lookback=args.lookback,
        initial_capital_gbp=args.capital,
        leg_notional_gbp=args.leg_notional,
        cost_bps_cyph=args.cost_bps_cyph,
        cost_bps_zec=args.cost_bps_zec,
        borrow_rate_cyph=args.borrow_cyph,
        borrow_rate_zec=args.borrow_zec,
    )

    # 1) Headline: every threshold at the headline lookback.
    headline = {e: run_backtest(panel, replace(base, entry_z=e)) for e in THRESHOLDS}
    summary = pd.DataFrame([summarize(r) for r in headline.values()])
    summary.to_csv(RESULTS / "summary_headline.csv", index=False)
    trades = pd.concat(
        [r.trades.assign(entry_threshold=e) for e, r in headline.items() if not r.trades.empty], ignore_index=True
    )
    trades.to_csv(RESULTS / "trades_headline.csv", index=False)
    pd.DataFrame({f"entry_{e:g}": r.equity for e, r in headline.items()}).to_csv(RESULTS / "equity_headline.csv")

    # 2) Lookback x threshold grid.
    lookbacks = sorted(set(LOOKBACKS) | {args.lookback})
    grid = pd.DataFrame(
        [summarize(run_backtest(panel, replace(base, entry_z=e, lookback=lb))) for lb in lookbacks for e in THRESHOLDS]
    )
    grid.to_csv(RESULTS / "grid_lookback_threshold.csv", index=False)

    # 3) Robustness variants at the headline lookback.
    variants = {
        "Headline": lambda e: replace(base, entry_z=e),
        "Zero costs": lambda e: replace(base, entry_z=e, cost_bps_cyph=0, cost_bps_zec=0,
                                        borrow_rate_cyph=0, borrow_rate_zec=0),
        "Double costs": lambda e: replace(base, entry_z=e, cost_bps_cyph=2 * base.cost_bps_cyph,
                                          cost_bps_zec=2 * base.cost_bps_zec,
                                          borrow_rate_cyph=2 * base.borrow_rate_cyph,
                                          borrow_rate_zec=2 * base.borrow_rate_zec),
        "Exit at \\|z\\| 0.5": lambda e: replace(base, entry_z=e, exit_z=0.5),
        "Stop at entry+1.5": lambda e: replace(base, entry_z=e, stop_z=e + 1.5),
        "5-day time stop": lambda e: replace(base, entry_z=e, max_hold_bars=35),
        "Short-CYPH side only": lambda e: replace(base, entry_z=e, direction="short_cyph_only"),
    }
    robust_rows = []
    for e in THRESHOLDS:
        row = {"entry_z": e}
        for name, make in variants.items():
            row[name] = summarize(run_backtest(panel, make(e)))["net_pnl_gbp"]
        robust_rows.append(row)
    robust = pd.DataFrame(robust_rows)
    robust.to_csv(RESULTS / "robustness.csv", index=False)

    # 4) Stability: split headline trades at the midpoint of the test window.
    mid = panel.index[0] + (panel.index[-1] - panel.index[0]) / 2
    halves = []
    for e, r in headline.items():
        t = r.trades
        first = t[t["entry_time"] < mid] if not t.empty else t
        second = t[t["entry_time"] >= mid] if not t.empty else t
        halves.append({
            "entry_z": e,
            "h1_trades": len(first), "h1_pnl": first["net_pnl_gbp"].sum() if len(first) else 0.0,
            "h2_trades": len(second), "h2_pnl": second["net_pnl_gbp"].sum() if len(second) else 0.0,
        })
    halves = pd.DataFrame(halves)
    halves.to_csv(RESULTS / "stability_halves.csv", index=False)

    # 5) Charts.
    report.plot_prices(panel, CHARTS / "01_prices_rebased.png")
    report.plot_spread_and_z(headline[2.0], CHARTS / "02_spread_zscore.png")
    report.plot_equity_curves([headline[e] for e in CHART_THRESHOLDS], CHARTS / "03_equity_curves.png")
    report.plot_direction_bars(summary, CHARTS / "04_pnl_by_direction.png")
    report.plot_heatmap(grid[grid["lookback"].isin(lookbacks)], CHARTS / "05_lookback_heatmap.png")

    write_report(panel, diag, base, summary, grid, robust, halves, mid)
    print(headline_table(summary, base.leg_notional_gbp).to_string(index=False))
    print(f"\nWrote results to {RESULTS}")


def write_report(panel, diag, base: Config, summary, grid, robust, halves, mid) -> None:
    ny = lambda ts: ts.tz_convert(NY_TZ).strftime("%d %b %Y %H:%M ET")  # noqa: E731
    day = lambda ts: ts.tz_convert(NY_TZ).strftime("%d %b %Y")  # noqa: E731
    best = summary.loc[summary["net_pnl_gbp"].idxmax()]
    df_note = (
        "The Dickey-Fuller statistic is below the 5% critical value, consistent with a mean-reverting ratio."
        if diag["df_stat"] < -2.86
        else "The Dickey-Fuller statistic is above the 5% critical value, so over the full window the ratio is "
        "**not** statistically proven to mean-revert. That is why a rolling z-score (which follows a drifting "
        "mean) is used rather than a fixed long-run mean."
    )
    warmup_end = {lb: compute_zscore(panel, lb)["z"].first_valid_index() for lb in sorted(grid["lookback"].unique())}

    grid_tbl = grid.pivot(index="lookback", columns="entry_z", values="net_pnl_gbp").sort_index()
    grid_md = pd.DataFrame(
        [[f"{lb} bars"] + [gbp(v) for v in grid_tbl.loc[lb]] for lb in grid_tbl.index],
        columns=["Lookback"] + [f"z {z:g}" for z in grid_tbl.columns],
    )
    robust_md = robust.copy()
    robust_md["entry_z"] = robust_md["entry_z"].map("{:g}".format)
    for c in robust_md.columns[1:]:
        robust_md[c] = robust_md[c].map(gbp)
    robust_md = robust_md.rename(columns={"entry_z": "Entry \\|z\\|"})
    halves_md = pd.DataFrame({
        "Entry \\|z\\|": halves["entry_z"].map("{:g}".format),
        "1st half trades": halves["h1_trades"],
        "1st half P&L": halves["h1_pnl"].map(gbp),
        "2nd half trades": halves["h2_trades"],
        "2nd half P&L": halves["h2_pnl"].map(gbp),
    })

    text = f"""# CYPH vs ZEC z-score pairs trade: backtest report

_Generated by `run_backtest.py`. All P&L is in GBP and **after** trading costs and short financing unless stated._

## Setup

| | |
|---|---|
| Pair | **CYPH** (Cypherpunk Technologies, Nasdaq) vs **ZEC** (Zcash, Coinbase ZEC-USD) |
| Period | {ny(panel["bar_start"].iloc[0])} → {ny(panel.index[-1])} ({diag["bars"]:,} hourly bars) |
| Bar size | 1 hour, CYPH regular session (09:30-16:00 ET); ZEC priced at the same instants |
| Spread | ln(CYPH) − ln(ZEC) (CYPH's price relative to ZEC) |
| Z-score | (spread − rolling mean) / rolling std, **{base.lookback}-bar** lookback (~{base.lookback / 7:.0f} trading days) |
| Entry | z > +threshold → **short CYPH / long ZEC**; z < −threshold → **long CYPH / short ZEC** |
| Exit | z crosses back through {base.exit_z:g} (convergence); open trades closed at the end of the test |
| Warm-up | first signal possible at {ny(warmup_end[base.lookback])}, once {base.lookback} bars of history exist |
| Execution | signal on the bar close, filled at the **next bar's open** (no look-ahead) |
| Capital | £{base.initial_capital_gbp:,.0f} portfolio, **£{base.leg_notional_gbp:,.0f} per leg** (£{base.leg_notional_gbp:,.0f} long + £{base.leg_notional_gbp:,.0f} short, dollar-neutral), one pair position at a time |
| Costs | {base.cost_bps_cyph:g} bps per side on CYPH, {base.cost_bps_zec:g} bps per side on ZEC (fees + spread/slippage) |
| Short financing | {base.borrow_rate_cyph:.0%} p.a. when short CYPH (stock borrow), {base.borrow_rate_zec:.0%} p.a. when short ZEC |
| FX | positions sized and P&L converted at the live GBPUSD rate |

## Pair diagnostics

| | |
|---|---|
| CYPH return over period | {diag["cyph_return_pct"]:+.0f}% |
| ZEC return over period | {diag["zec_return_pct"]:+.0f}% |
| Correlation of hourly returns | {diag["hourly_return_corr"]:.2f} |
| Correlation of daily returns | {diag["daily_return_corr"]:.2f} |
| Daily beta of CYPH to ZEC | {diag["daily_beta_cyph_on_zec"]:.2f} |
| Dickey-Fuller t-stat on ln(CYPH/ZEC) | {diag["df_stat"]:.2f} (5% critical value ≈ −2.86; 10% ≈ −2.57) |
| Mean-reversion half-life of the spread | {diag["half_life_bars"]:.0f} hourly bars (~{diag["half_life_bars"] / 7:.0f} trading days) |

A beta close to 1 means equal-sized legs are close to beta-neutral. {df_note}

## Headline results ({base.lookback}-bar lookback, exit at z = {base.exit_z:g})

{md_table(headline_table(summary, base.leg_notional_gbp))}

"Return on £{base.leg_notional_gbp:,.0f} leg" is net P&L divided by the £{base.leg_notional_gbp:,.0f} committed per leg. "Portfolio return" is on the full
£{base.initial_capital_gbp:,.0f}. Sharpe uses daily equity changes, annualised with √252.

Best threshold in this run: **|z| > {best.entry_z:g}**, net {gbp(best.net_pnl_gbp)} from {int(best.trades)} trades
({best.portfolio_return_pct:+.1f}% on the portfolio).

![Prices](charts/01_prices_rebased.png)

![Spread and z-score](charts/02_spread_zscore.png)

![Equity curves](charts/03_equity_curves.png)

![P&L by direction](charts/04_pnl_by_direction.png)

## Sensitivity to the z-score lookback

Net P&L for every combination of lookback window and entry threshold (other settings as headline).
Longer windows need more history before the first signal, so they start trading later
({", ".join(f"{lb} bars: {day(t)}" for lb, t in warmup_end.items())}).

{md_table(grid_md)}

![Lookback heatmap](charts/05_lookback_heatmap.png)

## Robustness checks ({base.lookback}-bar lookback, net P&L)

* **Zero costs / Double costs**: no fees, slippage or borrow; or twice the headline assumptions.
* **Exit at |z| 0.5**: take profit before full convergence.
* **Stop at entry+1.5**: close if the gap widens a further 1.5 standard deviations (no re-entry on that side until z
  is back inside the entry band).
* **5-day time stop**: close any trade still open after 35 hourly bars.
* **Short-CYPH side only**: only take the "CYPH rich vs ZEC" trade from your example (short CYPH, long ZEC).

{md_table(robust_md)}

## Stability across the two halves of the period

Headline trades split by entry date at {day(mid)}.

{md_table(halves_md)}

## Files

* `summary_headline.csv`: every metric for each threshold (headline settings)
* `trades_headline.csv`: every trade for each threshold, with entry/exit prices, sizes, costs and P&L
* `equity_headline.csv`: hourly equity curve for each threshold
* `grid_lookback_threshold.csv`, `robustness.csv`, `stability_halves.csv`: the sensitivity tables above
"""
    (RESULTS / "REPORT.md").write_text(text)


if __name__ == "__main__":
    main()
