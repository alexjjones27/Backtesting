"""Long-only variant: buy whichever asset is 'cheap' relative to the other.

Same z-score signal as the pairs trade, but no short leg:
    z < -threshold  ->  CYPH cheap vs ZEC  ->  buy GBP 1,000 of CYPH
    z > +threshold  ->  ZEC cheap vs CYPH  ->  buy GBP 1,000 of ZEC
    exit when z reverts to 0; hold cash otherwise.

Because a long-only trade also carries the direction of the whole market, the
report splits P&L into a market part and a selection part (see
pairs_backtest/attribution.py) and tests both against chance.

    python run_long_only.py
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

from pairs_backtest import report
from pairs_backtest.attribution import attribute, buy_and_hold, coin_flip_test, random_timing_test
from pairs_backtest.data import NY_TZ, load_panel
from pairs_backtest.engine import Config, compute_zscore, run_backtest
from pairs_backtest.metrics import daily_equity, max_drawdown, summarize
from pairs_backtest.report import gbp, md_table

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "results" / "long_only"
CHARTS = OUT / "charts"

THRESHOLDS = [1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--refresh", action="store_true", help="re-download market data before running")
    p.add_argument("--lookback", type=int, default=70, help="z-score lookback in hourly bars")
    p.add_argument("--capital", type=float, default=10_000.0, help="starting portfolio, GBP")
    p.add_argument("--position", type=float, default=1_000.0, help="GBP per position")
    p.add_argument("--cost-bps-cyph", type=float, default=20.0, help="CYPH fees+slippage per side, bps")
    p.add_argument("--cost-bps-zec", type=float, default=20.0, help="ZEC fees+slippage per side, bps")
    return p.parse_args()


def sharpe(equity: pd.Series) -> float:
    rets = daily_equity(equity).pct_change().dropna()
    return float(rets.mean() / rets.std() * np.sqrt(252)) if rets.std() > 0 else float("nan")


def analyse(panel: pd.DataFrame, cfg: Config, start_idx: int):
    res = run_backtest(panel, cfg)
    s = summarize(res)
    att = attribute(res.trades) if not res.trades.empty else res.trades
    p_coin, coin_sims = coin_flip_test(att) if not att.empty else (float("nan"), np.array([]))
    p_time, _ = random_timing_test(panel, att, start_idx) if not att.empty else (float("nan"), None)
    mid = panel.index[0] + (panel.index[-1] - panel.index[0]) / 2
    t = res.trades
    row = s | {
        "market_gbp": float(att["market_gbp"].sum()) if not att.empty else 0.0,
        "selection_gbp": float(att["selection_gbp"].sum()) if not att.empty else 0.0,
        "rich_instead_net_gbp": float((att["other_gross_gbp"] - att["costs_gbp"]).sum()) if not att.empty else 0.0,
        "coin_flip_p": p_coin,
        "random_timing_p": p_time,
        "h1_pnl_gbp": float(t.loc[t["entry_time"] < mid, "net_pnl_gbp"].sum()) if not t.empty else 0.0,
        "h2_pnl_gbp": float(t.loc[t["entry_time"] >= mid, "net_pnl_gbp"].sum()) if not t.empty else 0.0,
    }
    return row, att, coin_sims, res


def results_table(df: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({
        "Entry \\|z\\|": df["entry_z"].map("{:g}".format),
        "Trades": df["trades"],
        "Win rate": df["win_rate_pct"].map(lambda v: "-" if pd.isna(v) else f"{v:.0f}%"),
        "Net P&L": df["net_pnl_gbp"].map(gbp),
        "Portfolio return": df["portfolio_return_pct"].map("{:+.1f}%".format),
        "Max drawdown": df["max_drawdown_gbp"].map(gbp),
        "Sharpe": df["sharpe"].map("{:.2f}".format),
        "In market": df["time_in_market_pct"].map("{:.0f}%".format),
        "Buy CYPH: trades / P&L": df.apply(lambda r: f"{int(r.long_cyph_trades)} / {gbp(r.long_cyph_pnl_gbp)}", axis=1),
        "Buy ZEC: trades / P&L": df.apply(lambda r: f"{int(r.short_cyph_trades)} / {gbp(r.short_cyph_pnl_gbp)}", axis=1),
        "H1 / H2 P&L": df.apply(lambda r: f"{gbp(r.h1_pnl_gbp)} / {gbp(r.h2_pnl_gbp)}", axis=1),
    })


def attribution_table(df: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({
        "Entry \\|z\\|": df["entry_z"].map("{:g}".format),
        "Net P&L": df["net_pnl_gbp"].map(gbp),
        "= Market part": df["market_gbp"].map(gbp),
        "+ Selection part": df["selection_gbp"].map(gbp),
        "− Costs": df["costs_gbp"].map(gbp),
        "Selection share of gross": (df["selection_gbp"] / (df["market_gbp"] + df["selection_gbp"])).map(
            lambda v: "-" if not np.isfinite(v) else f"{v:.0%}"),
        "If you'd bought the rich asset": df["rich_instead_net_gbp"].map(gbp),
        "Coin-flip p": df["coin_flip_p"].map("{:.2f}".format),
        "Random-timing p": df["random_timing_p"].map("{:.2f}".format),
    })


def main() -> None:
    args = parse_args()
    CHARTS.mkdir(parents=True, exist_ok=True)
    panel = load_panel(refresh=args.refresh)
    base = Config(
        mode="long_only",
        lookback=args.lookback,
        initial_capital_gbp=args.capital,
        leg_notional_gbp=args.position,
        cost_bps_cyph=args.cost_bps_cyph,
        cost_bps_zec=args.cost_bps_zec,
    )
    # Benchmarks start where the strategy could first trade: the open after warm-up.
    start_idx = panel.index.get_loc(compute_zscore(panel, base.lookback)["z"].first_valid_index()) + 1

    rows, stop_rows, trades, coin = [], [], [], {}
    results = {}
    for e in THRESHOLDS:
        row, att, sims, res = analyse(panel, replace(base, entry_z=e), start_idx)
        rows.append(row)
        results[e] = res
        coin[e] = (sims, float(att["gross_pnl_gbp"].sum()) if not att.empty else 0.0)
        if not att.empty:
            trades.append(att.assign(entry_threshold=e))
        stop_rows.append(analyse(panel, replace(base, entry_z=e, stop_z=e + 1.5), start_idx)[0])
    summary, stops = pd.DataFrame(rows), pd.DataFrame(stop_rows)
    summary.to_csv(OUT / "summary_long_only.csv", index=False)
    stops.to_csv(OUT / "summary_long_only_stop.csv", index=False)
    pd.concat(trades, ignore_index=True).to_csv(OUT / "trades_long_only.csv", index=False)

    bh = buy_and_hold(panel, start_idx, base.leg_notional_gbp, base.initial_capital_gbp,
                      base.cost_bps_cyph, base.cost_bps_zec)
    bench = pd.DataFrame([
        {"name": name, "net_pnl_gbp": eq.iloc[-1] - base.initial_capital_gbp,
         "max_drawdown_gbp": max_drawdown(eq)[0], "sharpe": sharpe(eq)}
        for name, eq in bh.items()
    ])
    bench.to_csv(OUT / "benchmarks.csv", index=False)

    report.plot_attribution(summary, CHARTS / "lo_01_attribution.png")
    report.plot_long_only_equity({
        "Long-only, entry |z| > 1": results[1.0].equity.iloc[start_idx:],
        "Long-only, entry |z| > 2": results[2.0].equity.iloc[start_idx:],
        "Buy & hold ZEC": bh["Buy & hold ZEC"],
        "Buy & hold 50/50": bh["Buy & hold 50/50"],
    }, CHARTS / "lo_02_equity_vs_buy_hold.png")
    report.plot_coin_flip({e: coin[e] for e in (1.0, 2.0)}, CHARTS / "lo_03_coin_flip.png")

    write_report(panel, base, start_idx, summary, stops, bench)
    print(attribution_table(summary).to_string(index=False))
    print(f"\nWrote results to {OUT}")


def write_report(panel, base: Config, start_idx: int, summary, stops, bench) -> None:
    day = lambda ts: ts.tz_convert(NY_TZ).strftime("%d %b %Y")  # noqa: E731
    bench_md = pd.DataFrame({
        "Benchmark (£{:,.0f}, held throughout)".format(base.leg_notional_gbp): bench["name"],
        "Net P&L": bench["net_pnl_gbp"].map(gbp),
        "Max drawdown": bench["max_drawdown_gbp"].map(gbp),
        "Sharpe": bench["sharpe"].map("{:.2f}".format),
        "In market": "100%",
    })
    text = f"""# Long-only "buy the cheap one": backtest report

_Generated by `run_long_only.py`. All P&L is in GBP, after trading costs._

## What "cheap" means here

The signal is the same one the pairs trade uses: the z-score of ln(CYPH / ZEC) against its last {base.lookback}
hourly bars.

* **z < −threshold**: CYPH is cheap relative to ZEC, so **buy £{base.leg_notional_gbp:,.0f} of CYPH**.
* **z > +threshold**: ZEC is cheap relative to CYPH, so **buy £{base.leg_notional_gbp:,.0f} of ZEC**.
* **Exit** when z returns to 0. Otherwise hold cash. There is no short leg, so there is no borrow cost.

"Cheap" here is purely *relative*. Both assets can be falling while one is cheap against the other.

## How to judge it

Buying £1,000 of the cheap asset is the same position as:

1. **£500 in each asset** (a 50/50 basket). This is the **market part**: the direction of the whole Zcash trade,
   which you would have earned by holding either asset.
2. **£500 long the cheap asset and £500 short the rich one** (a half-size pairs trade). This is the
   **selection part**, the only part that reflects the cheap/rich signal.

So a long-only result only "qualifies" as relative value if the **selection part** is positive and
better than chance. A big total P&L in a rising market proves nothing on its own. Three checks are used:

* **Coin-flip test:** keep every trade's timing but pick the asset at random, 20,000 times. The p-value is the share
  of random picks that did at least as well as the signal. Below ~0.05 would suggest real skill.
* **Random-timing test:** keep every trade's length but start it at a random hour. This checks whether the market
  part came from good timing or just from being invested while prices rose.
* **Buy & hold benchmarks:** £{base.leg_notional_gbp:,.0f} held in CYPH, ZEC or 50/50 from {day(panel.index[start_idx])}
  (the first possible signal) to the end.

## Results ({base.lookback}-bar lookback, exit at z = 0, no stop-loss)

Period: {day(panel.index[start_idx])} → {day(panel.index[-1])}. Each position is £{base.leg_notional_gbp:,.0f} in a
£{base.initial_capital_gbp:,.0f} portfolio, with {base.cost_bps_cyph:g} bps per side on CYPH and
{base.cost_bps_zec:g} bps per side on ZEC.

{md_table(results_table(summary))}

### Benchmarks over the same window

{md_table(bench_md)}

![Equity vs buy & hold](charts/lo_02_equity_vs_buy_hold.png)

## Where the P&L came from

Net P&L = market part + selection part − costs. "If you'd bought the rich asset" re-runs every trade with the
*other* asset over the same hours.

{md_table(attribution_table(summary))}

![Attribution](charts/lo_01_attribution.png)

![Coin-flip test](charts/lo_03_coin_flip.png)

### Same, with a z stop-loss at entry + 1.5

{md_table(attribution_table(stops))}

## Files

* `summary_long_only.csv`, `summary_long_only_stop.csv`: all metrics per threshold
* `trades_long_only.csv`: every trade with its market/selection split
* `benchmarks.csv`: buy & hold results
"""
    (OUT / "REPORT.md").write_text(text)


if __name__ == "__main__":
    main()
