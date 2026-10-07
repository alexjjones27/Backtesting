"""Performance statistics for a backtest result."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .data import NY_TZ
from .engine import Result

TRADING_DAYS_PER_YEAR = 252


def daily_equity(equity: pd.Series) -> pd.Series:
    """Last equity mark of each New York trading day."""
    return equity.groupby(equity.index.tz_convert(NY_TZ).date).last()


def max_drawdown(equity: pd.Series) -> tuple[float, float]:
    peak = equity.cummax()
    dd = equity - peak
    return float(dd.min()), float((dd / peak).min())


def summarize(res: Result) -> dict:
    cfg, trades, equity = res.config, res.trades, res.equity
    daily = daily_equity(equity)
    rets = daily.pct_change().dropna()
    sharpe = float(rets.mean() / rets.std() * np.sqrt(TRADING_DAYS_PER_YEAR)) if rets.std() > 0 else float("nan")
    dd_gbp, dd_pct = max_drawdown(equity)
    final = float(equity.iloc[-1])
    n = len(trades)

    out = {
        "entry_z": cfg.entry_z,
        "exit_z": cfg.exit_z,
        "lookback": cfg.lookback,
        "trades": n,
        "net_pnl_gbp": final - cfg.initial_capital_gbp,
        "final_equity_gbp": final,
        "portfolio_return_pct": (final / cfg.initial_capital_gbp - 1) * 100,
        "sharpe": sharpe,
        "max_drawdown_gbp": dd_gbp,
        "max_drawdown_pct": dd_pct * 100,
        "time_in_market_pct": float((res.signals["position"] != 0).mean() * 100),
    }
    if n == 0:
        return out | {
            "win_rate_pct": np.nan, "avg_trade_gbp": np.nan, "best_trade_gbp": np.nan,
            "worst_trade_gbp": np.nan, "profit_factor": np.nan, "avg_hold_hours": np.nan,
            "gross_pnl_gbp": 0.0, "costs_gbp": 0.0, "borrow_gbp": 0.0,
            "short_cyph_trades": 0, "short_cyph_pnl_gbp": 0.0, "long_cyph_trades": 0, "long_cyph_pnl_gbp": 0.0,
        }

    pnl = trades["net_pnl_gbp"]
    wins, losses = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    short_c = trades[trades["side"] == -1]
    long_c = trades[trades["side"] == 1]
    return out | {
        "win_rate_pct": float((pnl > 0).mean() * 100),
        "avg_trade_gbp": float(pnl.mean()),
        "best_trade_gbp": float(pnl.max()),
        "worst_trade_gbp": float(pnl.min()),
        "profit_factor": float(wins / losses) if losses > 0 else float("inf"),
        "avg_hold_hours": float(trades["hours_held"].mean()),
        "gross_pnl_gbp": float(trades["gross_pnl_gbp"].sum()),
        "costs_gbp": float(trades["costs_gbp"].sum()),
        "borrow_gbp": float(trades["borrow_gbp"].sum()),
        "short_cyph_trades": len(short_c),
        "short_cyph_pnl_gbp": float(short_c["net_pnl_gbp"].sum()),
        "long_cyph_trades": len(long_c),
        "long_cyph_pnl_gbp": float(long_c["net_pnl_gbp"].sum()),
    }


def pair_diagnostics(panel: pd.DataFrame) -> dict:
    """Statistics describing how tightly CYPH and ZEC move together."""
    logp = np.log(panel[["cyph_close", "zec_close"]])
    hourly = logp.diff().dropna()
    daily = logp.groupby(panel.index.tz_convert(NY_TZ).date).last().diff().dropna()
    beta = float(np.polyfit(daily["zec_close"], daily["cyph_close"], 1)[0])

    spread = (logp["cyph_close"] - logp["zec_close"]).to_numpy()
    # Dickey-Fuller regression on the spread: d(s_t) = a + b * s_{t-1} + e
    ds, lag = np.diff(spread), spread[:-1]
    X = np.column_stack([np.ones_like(lag), lag])
    coef, *_ = np.linalg.lstsq(X, ds, rcond=None)
    resid = ds - X @ coef
    sigma2 = resid @ resid / (len(ds) - 2)
    se_b = np.sqrt(sigma2 * np.linalg.inv(X.T @ X)[1, 1])
    b = coef[1]
    half_life_bars = float(-np.log(2) / np.log(1 + b)) if -1 < b < 0 else float("inf")

    return {
        "bars": len(panel),
        "start": panel.index[0],
        "end": panel.index[-1],
        "hourly_return_corr": float(hourly.corr().iloc[0, 1]),
        "daily_return_corr": float(daily.corr().iloc[0, 1]),
        "daily_beta_cyph_on_zec": beta,
        "df_stat": float(b / se_b),
        "half_life_bars": half_life_bars,
        "cyph_return_pct": float((panel["cyph_close"].iloc[-1] / panel["cyph_close"].iloc[0] - 1) * 100),
        "zec_return_pct": float((panel["zec_close"].iloc[-1] / panel["zec_close"].iloc[0] - 1) * 100),
    }
