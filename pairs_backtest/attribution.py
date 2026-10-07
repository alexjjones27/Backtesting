"""Is buying the 'cheap' asset a real edge, or just market exposure?

Buying GBP N of the cheap asset is exactly the same position as:
    GBP N/2 in each asset (a 50/50 "market" basket)
  + a half-size pairs trade (GBP N/2 long the cheap asset, GBP N/2 short the rich one).

So a long-only trade's gross P&L splits exactly into
    market    = what the 50/50 basket made over the same hours
    selection = gross - market = (return of cheap asset - return of rich asset) / 2
Only the selection part says anything about the cheap/rich signal; the market
part is the direction of the whole Zcash complex, which you would have earned
holding either asset.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def attribute(trades: pd.DataFrame) -> pd.DataFrame:
    """Per-trade split of gross P&L into market and selection components (GBP)."""
    t = trades.copy()
    notional_usd = t["cyph_shares"].abs() * t["cyph_entry"] + t["zec_units"].abs() * t["zec_entry"]
    r_cyph = t["cyph_exit"] / t["cyph_entry"] - 1
    r_zec = t["zec_exit"] / t["zec_entry"] - 1
    bought_cyph = t["cyph_shares"] > 0
    r_other = np.where(bought_cyph, r_zec, r_cyph)
    t["notional_usd"], t["r_cyph"], t["r_zec"] = notional_usd, r_cyph, r_zec
    t["market_gbp"] = notional_usd * (r_cyph + r_zec) / 2 / t["fx_exit"]
    t["selection_gbp"] = t["gross_pnl_gbp"] - t["market_gbp"]
    # Gross P&L had the same trade bought the *rich* asset instead.
    t["other_gross_gbp"] = notional_usd * r_other / t["fx_exit"]
    return t


def random_timing_test(panel: pd.DataFrame, att: pd.DataFrame, start_idx: int,
                       n_sims: int = 10_000, seed: int = 11) -> tuple[float, np.ndarray]:
    """Keep every trade's size and holding time but start it at a random bar.

    Compares the market (50/50 basket) P&L the signal's timing captured with what
    randomly timed trades of the same lengths captured. Random trades may overlap,
    so this is a rough check of whether the timing was skill or luck. Returns the
    one-sided p-value and the simulated market P&L totals.
    """
    if att.empty:
        return float("nan"), np.array([])
    n = len(panel)
    co, zo = panel["cyph_open"].to_numpy(), panel["zec_open"].to_numpy()
    # Exit at the open h bars later, or at the final close if that runs off the end.
    c_exit = np.append(co, panel["cyph_close"].iloc[-1])
    z_exit = np.append(zo, panel["zec_close"].iloc[-1])
    held = att["bars_held"].to_numpy()
    scale = (att["notional_usd"] / att["fx_exit"]).to_numpy()
    k = np.random.default_rng(seed).integers(start_idx, n - 1, size=(n_sims, len(att)))
    j = np.minimum(k + held, n)
    basket_ret = 0.5 * (c_exit[j] / co[k] - 1) + 0.5 * (z_exit[j] / zo[k] - 1)
    sims = (basket_ret * scale).sum(axis=1)
    return float((sims >= att["market_gbp"].sum()).mean()), sims


BENCHMARK_WEIGHTS = {"Buy & hold CYPH": 1.0, "Buy & hold ZEC": 0.0, "Buy & hold 50/50": 0.5}


def buy_and_hold(panel: pd.DataFrame, start_idx: int, notional_gbp: float, initial_gbp: float,
                 cost_bps_cyph: float, cost_bps_zec: float) -> pd.DataFrame:
    """Equity curves for holding GBP notional of CYPH, ZEC or a 50/50 mix from bar start_idx.

    Bought at the open of bar start_idx and marked at every close. USD P&L is
    converted at the current GBPUSD rate, as in the engine. One round trip of costs.
    """
    p = panel.iloc[start_idx:]
    usd = notional_gbp * p["fx_open"].iloc[0]
    curves = {}
    for name, w in BENCHMARK_WEIGHTS.items():
        value_usd = usd * (w * p["cyph_close"] / p["cyph_open"].iloc[0]
                           + (1 - w) * p["zec_close"] / p["zec_open"].iloc[0])
        cost_rate = (w * cost_bps_cyph + (1 - w) * cost_bps_zec) / 1e4
        equity = initial_gbp + (value_usd - usd) / p["fx_close"] - notional_gbp * cost_rate
        equity.iloc[-1] -= value_usd.iloc[-1] / p["fx_close"].iloc[-1] * cost_rate  # exit cost
        curves[name] = equity
    return pd.DataFrame(curves)
