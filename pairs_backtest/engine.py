"""Z-score pairs-trading backtest engine for CYPH vs ZEC.

Spread  = ln(CYPH) - ln(ZEC), i.e. the log of CYPH's price relative to ZEC.
Z-score = (spread - rolling mean) / rolling std over `lookback` hourly bars.

  z > +entry_z  -> CYPH rich vs ZEC  -> SHORT CYPH / LONG ZEC
  z < -entry_z  -> CYPH cheap vs ZEC -> LONG CYPH / SHORT ZEC
  exit when z crosses back through +/-exit_z (default 0 = full convergence),
  or optionally on a z stop-loss or a maximum holding time.

Each trade puts `leg_notional_gbp` (default GBP 1,000) on EACH leg, converted to
USD at the fill-time GBPUSD rate, so the position is dollar-neutral.

Timing (no look-ahead): the z-score is computed from closes up to and including
bar t; any resulting order is filled at the OPEN of bar t+1 (the next tradeable
price - overnight signals fill at the next morning's open).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

SECONDS_PER_YEAR = 365.0 * 24 * 3600

SHORT_CYPH = -1  # short spread: short CYPH, long ZEC
LONG_CYPH = 1  # long spread: long CYPH, short ZEC
DIRECTION_LABEL = {SHORT_CYPH: "Short CYPH / Long ZEC", LONG_CYPH: "Long CYPH / Short ZEC"}


@dataclass(frozen=True)
class Config:
    entry_z: float = 2.0
    exit_z: float = 0.0
    lookback: int = 70  # hourly bars (~7 per trading day, so 70 = ~2 weeks)
    stop_z: float | None = None  # close if |z| moves further against us to this level
    max_hold_bars: int | None = None  # time stop, in hourly bars
    direction: str = "both"  # "both" | "short_cyph_only" | "long_cyph_only"
    initial_capital_gbp: float = 10_000.0
    leg_notional_gbp: float = 1_000.0
    cost_bps_cyph: float = 20.0  # fees + spread/slippage per side, CYPH leg
    cost_bps_zec: float = 20.0  # fees + spread/slippage per side, ZEC leg
    borrow_rate_cyph: float = 0.10  # annual cost of being short CYPH (stock borrow)
    borrow_rate_zec: float = 0.10  # annual cost of being short ZEC (borrow/funding)

    def label(self) -> str:
        return f"entry {self.entry_z:g} / exit {self.exit_z:g} / lookback {self.lookback}"


@dataclass
class Result:
    config: Config
    trades: pd.DataFrame
    equity: pd.Series  # GBP equity marked at every bar close
    signals: pd.DataFrame  # spread, rolling mean/std, z, position


def compute_zscore(panel: pd.DataFrame, lookback: int) -> pd.DataFrame:
    spread = np.log(panel["cyph_close"]) - np.log(panel["zec_close"])
    roll = spread.rolling(lookback, min_periods=lookback)
    mean, std = roll.mean(), roll.std()
    return pd.DataFrame({"spread": spread, "mean": mean, "std": std, "z": (spread - mean) / std})


@dataclass
class _Position:
    side: int
    entry_idx: int
    entry_time: pd.Timestamp
    entry_z: float
    fx_entry: float
    cyph_px: float
    zec_px: float
    q_cyph: float  # signed shares
    q_zec: float  # signed coins
    entry_cost_gbp: float


def _borrow_gbp(pos: _Position, cfg: Config, elapsed_s: float) -> float:
    """Financing accrued on whichever leg is short, on its entry notional."""
    if pos.side == SHORT_CYPH:
        notional_usd, rate = abs(pos.q_cyph) * pos.cyph_px, cfg.borrow_rate_cyph
    else:
        notional_usd, rate = abs(pos.q_zec) * pos.zec_px, cfg.borrow_rate_zec
    return notional_usd / pos.fx_entry * rate * elapsed_s / SECONDS_PER_YEAR


def _gross_pnl_usd(pos: _Position, cyph_px: float, zec_px: float) -> float:
    return pos.q_cyph * (cyph_px - pos.cyph_px) + pos.q_zec * (zec_px - pos.zec_px)


def run_backtest(panel: pd.DataFrame, cfg: Config) -> Result:
    sig = compute_zscore(panel, cfg.lookback)
    z = sig["z"].to_numpy()
    times = panel.index
    bar_start = pd.DatetimeIndex(panel["bar_start"])
    cyph_o, cyph_c = panel["cyph_open"].to_numpy(), panel["cyph_close"].to_numpy()
    zec_o, zec_c = panel["zec_open"].to_numpy(), panel["zec_close"].to_numpy()
    fx_o, fx_c = panel["fx_open"].to_numpy(), panel["fx_close"].to_numpy()
    c_cyph, c_zec = cfg.cost_bps_cyph / 1e4, cfg.cost_bps_zec / 1e4

    allowed = {
        "both": {SHORT_CYPH, LONG_CYPH},
        "short_cyph_only": {SHORT_CYPH},
        "long_cyph_only": {LONG_CYPH},
    }[cfg.direction]

    realized = 0.0
    pos: _Position | None = None
    pending: tuple[str, int, str] | None = None  # ("open"|"close", side, reason)
    # After a stop/time exit, don't re-enter the same side until z is back inside
    # the entry band - otherwise we would immediately re-open the losing trade.
    blocked_side: int | None = None
    trades, equity, position_flag = [], np.empty(len(panel)), np.zeros(len(panel), dtype=int)

    def close_position(i: int, cyph_px: float, zec_px: float, fx: float, when: pd.Timestamp, reason: str, z_exit: float):
        nonlocal realized, pos
        assert pos is not None
        gross_gbp = _gross_pnl_usd(pos, cyph_px, zec_px) / fx
        exit_cost_gbp = (abs(pos.q_cyph) * cyph_px * c_cyph + abs(pos.q_zec) * zec_px * c_zec) / fx
        borrow_gbp = _borrow_gbp(pos, cfg, (when - pos.entry_time).total_seconds())
        costs_gbp = pos.entry_cost_gbp + exit_cost_gbp
        net = gross_gbp - costs_gbp - borrow_gbp
        realized += gross_gbp - exit_cost_gbp - borrow_gbp  # entry cost already booked
        trades.append(
            {
                "entry_time": pos.entry_time,
                "exit_time": when,
                "direction": DIRECTION_LABEL[pos.side],
                "side": pos.side,
                "entry_z": pos.entry_z,
                "exit_z": z_exit,
                "exit_reason": reason,
                "cyph_entry": pos.cyph_px,
                "cyph_exit": cyph_px,
                "zec_entry": pos.zec_px,
                "zec_exit": zec_px,
                "cyph_shares": pos.q_cyph,
                "zec_units": pos.q_zec,
                "bars_held": i - pos.entry_idx,
                "hours_held": (when - pos.entry_time).total_seconds() / 3600,
                "gross_pnl_gbp": gross_gbp,
                "costs_gbp": costs_gbp,
                "borrow_gbp": borrow_gbp,
                "net_pnl_gbp": net,
                "return_on_leg": net / cfg.leg_notional_gbp,
            }
        )
        pos = None

    for i in range(len(panel)):
        # 1) Fill the order generated at the previous bar's close, at this bar's open.
        if pending is not None:
            action, side, reason = pending
            pending = None
            if action == "close" and pos is not None:
                close_position(i, cyph_o[i], zec_o[i], fx_o[i], bar_start[i], reason, z[i - 1])
            elif action == "open" and pos is None:
                notional_usd = cfg.leg_notional_gbp * fx_o[i]
                q_cyph = side * math.floor(notional_usd / cyph_o[i])  # whole shares
                q_zec = -side * notional_usd / zec_o[i]  # crypto is fractional
                entry_cost_gbp = (abs(q_cyph) * cyph_o[i] * c_cyph + abs(q_zec) * zec_o[i] * c_zec) / fx_o[i]
                realized -= entry_cost_gbp
                pos = _Position(side, i, bar_start[i], z[i - 1], fx_o[i], cyph_o[i], zec_o[i], q_cyph, q_zec, entry_cost_gbp)

        # 2) Mark to market at this bar's close.
        unrealized = 0.0
        if pos is not None:
            unrealized = _gross_pnl_usd(pos, cyph_c[i], zec_c[i]) / fx_c[i] - _borrow_gbp(
                pos, cfg, (times[i] - pos.entry_time).total_seconds()
            )
            position_flag[i] = pos.side
        equity[i] = cfg.initial_capital_gbp + realized + unrealized

        # 3) Generate the order for the next bar from this bar's close.
        zi = z[i]
        if i == len(panel) - 1 or np.isnan(zi):
            continue
        if blocked_side is not None and abs(zi) < cfg.entry_z:
            blocked_side = None
        if pos is None:
            side = SHORT_CYPH if zi > cfg.entry_z else LONG_CYPH if zi < -cfg.entry_z else None
            beyond_stop = cfg.stop_z is not None and abs(zi) >= cfg.stop_z  # would be stopped out at once
            if side is not None and side in allowed and side != blocked_side and not beyond_stop:
                pending = ("open", side, "")
        else:
            # Express z from the position's point of view: positive = still stretched.
            stretch = -zi if pos.side == LONG_CYPH else zi
            if stretch <= cfg.exit_z:
                pending = ("close", pos.side, "mean reversion")
            elif cfg.stop_z is not None and stretch >= cfg.stop_z:
                pending, blocked_side = ("close", pos.side, "z stop-loss"), pos.side
            elif cfg.max_hold_bars is not None and i + 1 - pos.entry_idx >= cfg.max_hold_bars:
                pending, blocked_side = ("close", pos.side, "time stop"), pos.side

    # Anything still open is closed at the final bar's close.
    if pos is not None:
        last = len(panel) - 1
        close_position(last, cyph_c[last], zec_c[last], fx_c[last], times[last], "end of test", z[last])
        equity[last] = cfg.initial_capital_gbp + realized

    sig["position"] = position_flag
    trades_df = pd.DataFrame(trades)
    return Result(cfg, trades_df, pd.Series(equity, index=times, name="equity_gbp"), sig)


def config_dict(cfg: Config) -> dict:
    return asdict(cfg)
