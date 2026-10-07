import numpy as np
import pandas as pd
import pytest

from pairs_backtest.engine import Config, run_backtest
from pairs_backtest.placebo import shuffled_panel
from pairs_backtest.rotation import NEUTRAL, TO_CYPH, TO_ZEC, Schedule, cyph_weights, equity_curve, \
    schedule_from_trades, simulate
from test_engine import ZERO_COSTS, make_panel, noisy_flat

FX = 1.25


def test_schedule_follows_engine_trades():
    cyph, zec = noisy_flat(80)
    cyph[50] *= 0.90  # CYPH cheap -> tilt to CYPH, then back to 50/50 on reversion
    panel = make_panel(cyph, zec)
    trades = run_backtest(panel, Config(entry_z=2.0, lookback=20, **ZERO_COSTS)).trades
    sched = schedule_from_trades(panel, trades, start_idx=25)
    assert sched.bars[0] == 25 and sched.sides[0] == NEUTRAL
    assert list(sched.sides[1:3]) == [TO_CYPH, NEUTRAL]
    assert sched.bars[1] == 51  # filled at the open after the signal bar


def test_rotation_matches_hand_calculation():
    # Flat ZEC; CYPH doubles while the sleeve is 100% CYPH.
    cyph = [1.0, 1.0, 1.0, 2.0, 2.0, 2.0]
    zec = [100.0] * 6
    panel = make_panel(cyph, zec)
    sched = Schedule(np.array([1, 2, 4]), np.array([NEUTRAL, TO_CYPH, NEUTRAL]))
    rot = simulate(panel, sched, cyph_weights(sched.sides, 1.0)[None, :], 1000, 0, 0)
    # 1,000 GBP -> 100% CYPH at 1.0 -> CYPH doubles -> 2,000 GBP.
    assert rot.pnl_gbp[0] == pytest.approx(1000.0)
    shadow = simulate(panel, sched, np.full((1, 3), 0.5), 1000, 0, 0)
    assert shadow.pnl_gbp[0] == pytest.approx(500.0)  # half the sleeve in CYPH


def test_rotation_costs_and_equity_curve():
    cyph = [1.0, 1.0, 1.0, 2.0, 2.0, 2.0]
    panel = make_panel(cyph, [100.0] * 6)
    sched = Schedule(np.array([1, 2, 4]), np.array([NEUTRAL, TO_ZEC, NEUTRAL]))
    rot = simulate(panel, sched, cyph_weights(sched.sides, 1.0)[None, :], 1000, 50, 50)
    # Buy 50/50 (1,000 traded), switch to 100% ZEC (1,000 traded), back to 50/50 (~1,000), sell (~1,000):
    # about 4,000 GBP traded at 50 bps.
    assert rot.costs_gbp[0] == pytest.approx(20.0, rel=0.02)
    assert rot.pnl_gbp[0] == pytest.approx(-rot.costs_gbp[0], rel=1e-9)  # ZEC was flat, so only costs
    eq = equity_curve(panel, sched, rot, 0, 1000, 10_000, 50, 50)
    assert eq.iloc[-1] == pytest.approx(10_000 + rot.pnl_gbp[0])


def test_many_paths_at_once_match_single_runs():
    rng = np.random.default_rng(1)
    cyph = 2 * np.exp(np.cumsum(rng.normal(0, 0.02, 60)))
    zec = 400 * np.exp(np.cumsum(rng.normal(0, 0.02, 60)))
    panel = make_panel(cyph, zec)
    sides = np.array([NEUTRAL, TO_CYPH, NEUTRAL, TO_ZEC, NEUTRAL])
    sched = Schedule(np.array([5, 12, 20, 33, 47]), sides)
    w = np.vstack([cyph_weights(sides, 1.0), cyph_weights(-sides, 0.75)])
    both = simulate(panel, sched, w, 1000, 20, 20).pnl_gbp
    one = [simulate(panel, sched, w[i:i + 1], 1000, 20, 20).pnl_gbp[0] for i in range(2)]
    np.testing.assert_allclose(both, one)


def test_shuffled_panel_keeps_zec_and_the_moves():
    rng = np.random.default_rng(0)
    cyph = 2 * np.exp(np.cumsum(rng.normal(0, 0.02, 200)))
    zec = 400 * np.exp(np.cumsum(rng.normal(0, 0.02, 200)))
    panel = make_panel(cyph, zec, cyph_open=cyph * np.exp(rng.normal(0, 0.01, 200)))
    for block in ("bar", "day"):
        fake = shuffled_panel(panel, np.random.default_rng(5), block)
        pd.testing.assert_series_equal(fake["zec_close"], panel["zec_close"])
        spread = lambda df: np.log(df["cyph_close"] / df["zec_close"])  # noqa: E731
        assert spread(fake).iloc[-1] == pytest.approx(spread(panel).iloc[-1])  # same end point
        assert not np.allclose(spread(fake), spread(panel))  # but a different path
        moves = lambda df: np.sort(np.diff(np.log(df["cyph_close"] / df["zec_close"])))  # noqa: E731
        assert moves(fake).std() == pytest.approx(moves(panel).std(), rel=0.3)
