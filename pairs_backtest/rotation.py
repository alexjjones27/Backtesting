"""Always-invested rotation between CYPH and ZEC.

The sleeve holds a 50/50 mix by default and tilts into whichever asset the
z-score says is cheap, on exactly the same entry/exit/stop schedule as the pairs
trade (see engine.run_backtest):

    z < -entry_z                    -> `tilt` of the sleeve in CYPH (default 100%)
    z > +entry_z                    -> `tilt` of the sleeve in ZEC
    z back through exit_z, a stop,
    or the end of the test          -> back to 50/50

Because the sleeve is always fully invested, its market exposure matches simply
holding both assets. Comparing it with a 50/50 mix rebalanced at the *same*
moments isolates the selection edge (minus the extra trading costs).

Rebalances fill at the open of the bar after the signal, like every other trade
in this project. Holdings are fractional and the sleeve compounds. USD P&L is
converted to GBP at the current GBPUSD rate.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

TO_CYPH, TO_ZEC, NEUTRAL = 1, -1, 0


@dataclass
class Schedule:
    bars: np.ndarray  # bar index of each rebalance (filled at that bar's open)
    sides: np.ndarray  # TO_CYPH, TO_ZEC or NEUTRAL


def schedule_from_trades(panel: pd.DataFrame, trades: pd.DataFrame, start_idx: int) -> Schedule:
    """Turn engine trades into rebalance events, starting with a 50/50 buy at start_idx."""
    bar_of = pd.Series(np.arange(len(panel)), index=pd.DatetimeIndex(panel["bar_start"]))
    events = {start_idx: NEUTRAL}
    for t in trades.itertuples(index=False):
        # Engine side +1 = long CYPH (CYPH cheap), -1 = short CYPH (ZEC cheap).
        events[int(bar_of[t.entry_time])] = TO_CYPH if t.side == 1 else TO_ZEC
        if t.exit_reason != "end of test":  # end-of-test exits are just the final valuation
            events[int(bar_of[t.exit_time])] = NEUTRAL
    bars = np.array(sorted(events))
    return Schedule(bars, np.array([events[b] for b in bars]))


def cyph_weights(sides: np.ndarray, tilt: float) -> np.ndarray:
    """Target weight in CYPH for each event side."""
    return np.select([sides == TO_CYPH, sides == TO_ZEC], [tilt, 1 - tilt], 0.5)


@dataclass
class Rotation:
    pnl_gbp: np.ndarray  # final P&L per simulated path, after all costs
    costs_gbp: np.ndarray  # trading costs per path
    units: tuple[np.ndarray, np.ndarray]  # (CYPH, ZEC) holdings after each event, shape (paths, events)


def simulate(panel: pd.DataFrame, sched: Schedule, weights: np.ndarray, sleeve_gbp: float,
             cost_bps_cyph: float, cost_bps_zec: float) -> Rotation:
    """Run the rotation for one or many weight paths at once.

    `weights` has shape (paths, events): the CYPH weight targeted at each event.
    """
    weights = np.atleast_2d(weights)
    co, zo = panel["cyph_open"].to_numpy(), panel["zec_open"].to_numpy()
    c_c, c_z = cost_bps_cyph / 1e4, cost_bps_zec / 1e4
    k0 = sched.bars[0]
    v0 = sleeve_gbp * panel["fx_open"].iloc[k0]  # USD
    paths = weights.shape[0]
    u_c, u_z = np.zeros(paths), np.zeros(paths)
    value = np.full(paths, v0)
    costs_usd = np.zeros(paths)
    hist_c, hist_z = [], []
    for e, k in enumerate(sched.bars):
        if e > 0:
            value = u_c * co[k] + u_z * zo[k]
        tgt_c = weights[:, e] * value / co[k]
        tgt_z = (1 - weights[:, e]) * value / zo[k]
        cost = np.abs(tgt_c - u_c) * co[k] * c_c + np.abs(tgt_z - u_z) * zo[k] * c_z
        scale = (value - cost) / value  # costs come out of the sleeve
        u_c, u_z = tgt_c * scale, tgt_z * scale
        costs_usd += cost
        hist_c.append(u_c)
        hist_z.append(u_z)

    cc, zc = panel["cyph_close"].iloc[-1], panel["zec_close"].iloc[-1]
    exit_cost = u_c * cc * c_c + u_z * zc * c_z  # sell everything at the end
    final = u_c * cc + u_z * zc - exit_cost
    fx_end = panel["fx_close"].iloc[-1]
    return Rotation(
        pnl_gbp=(final - v0) / fx_end,
        costs_gbp=(costs_usd + exit_cost) / fx_end,
        units=(np.column_stack(hist_c), np.column_stack(hist_z)),
    )


def equity_curve(panel: pd.DataFrame, sched: Schedule, rot: Rotation, path: int, sleeve_gbp: float,
                 initial_gbp: float, cost_bps_cyph: float, cost_bps_zec: float) -> pd.Series:
    """Hourly GBP equity (portfolio = initial + sleeve P&L) for one simulated path."""
    n = len(panel)
    k0 = sched.bars[0]
    v0 = sleeve_gbp * panel["fx_open"].iloc[k0]
    active = np.searchsorted(sched.bars, np.arange(n), side="right") - 1  # last event at or before each bar
    live = active >= 0
    u_c = np.where(live, rot.units[0][path][np.clip(active, 0, None)], 0.0)
    u_z = np.where(live, rot.units[1][path][np.clip(active, 0, None)], 0.0)
    value = u_c * panel["cyph_close"].to_numpy() + u_z * panel["zec_close"].to_numpy()
    pnl = np.where(live, (value - v0) / panel["fx_close"].to_numpy(), 0.0)
    pnl[-1] = rot.pnl_gbp[path]  # includes the final selling cost
    return pd.Series(initial_gbp + pnl, index=panel.index, name="equity_gbp").iloc[k0:]

