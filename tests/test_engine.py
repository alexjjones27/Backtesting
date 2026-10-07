import numpy as np
import pandas as pd
import pytest

from pairs_backtest.engine import LONG_CYPH, SHORT_CYPH, Config, compute_zscore, run_backtest
from pairs_backtest.attribution import attribute, coin_flip_test
from pairs_backtest.metrics import summarize

FX = 1.25


def make_panel(cyph_close, zec_close, cyph_open=None, zec_open=None):
    """Synthetic hourly panel; opens default to the previous close (no gaps)."""
    cyph_close = np.asarray(cyph_close, dtype=float)
    zec_close = np.asarray(zec_close, dtype=float)
    n = len(cyph_close)
    if cyph_open is None:
        cyph_open = np.r_[cyph_close[0], cyph_close[:-1]]
    if zec_open is None:
        zec_open = np.r_[zec_close[0], zec_close[:-1]]
    end = pd.date_range("2026-01-05 15:30", periods=n, freq="h", tz="UTC")
    return pd.DataFrame(
        {
            "bar_start": end - pd.Timedelta(hours=1),
            "cyph_open": cyph_open,
            "cyph_close": cyph_close,
            "cyph_volume": 1e6,
            "zec_open": zec_open,
            "zec_close": zec_close,
            "fx_open": FX,
            "fx_close": FX,
        },
        index=end,
    )


def noisy_flat(n, seed=0):
    """Gently oscillating spread whose rolling |z| stays below ~1.5 on its own."""
    wiggle = 0.002 * np.sin(np.arange(n) * 0.7 + seed)
    return 2.0 * np.exp(wiggle), np.full(n, 400.0)


ZERO_COSTS = dict(cost_bps_cyph=0, cost_bps_zec=0, borrow_rate_cyph=0, borrow_rate_zec=0)


def test_zscore_has_no_lookahead():
    cyph, zec = noisy_flat(100)
    base = compute_zscore(make_panel(cyph, zec), 20)["z"]
    cyph2 = cyph.copy()
    cyph2[60:] *= 3  # change the future only
    changed = compute_zscore(make_panel(cyph2, zec), 20)["z"]
    pd.testing.assert_series_equal(base.iloc[:60], changed.iloc[:60])


def test_short_cyph_trade_on_positive_dislocation_fills_next_open():
    cyph, zec = noisy_flat(80)
    cyph[50] *= 1.10  # CYPH spikes 10% vs ZEC at bar 50's close...
    cyph_open = np.r_[cyph[0], cyph[:-1]]
    cyph_open[51] = cyph[50] * 0.99  # ...and opens slightly lower on bar 51
    panel = make_panel(cyph, zec, cyph_open=cyph_open)
    res = run_backtest(panel, Config(entry_z=2.0, lookback=20, **ZERO_COSTS))

    t = res.trades.iloc[0]
    assert t["side"] == SHORT_CYPH
    assert t["entry_time"] == panel["bar_start"].iloc[51]  # next bar's open, not bar 50's close
    assert t["cyph_entry"] == pytest.approx(cyph_open[51])
    # GBP 1,000 per leg, converted to USD at the fill-time FX rate.
    assert t["cyph_shares"] == -np.floor(1000 * FX / cyph_open[51])
    assert t["zec_units"] * t["zec_entry"] == pytest.approx(1000 * FX)
    assert t["exit_reason"] == "mean reversion"
    assert t["net_pnl_gbp"] > 0  # CYPH fell back towards ZEC


def test_long_cyph_trade_on_negative_dislocation():
    cyph, zec = noisy_flat(80)
    cyph[50] *= 0.90
    res = run_backtest(make_panel(cyph, zec), Config(entry_z=2.0, lookback=20, **ZERO_COSTS))
    assert res.trades.iloc[0]["side"] == LONG_CYPH
    assert res.trades.iloc[0]["zec_units"] < 0


def test_direction_filter():
    cyph, zec = noisy_flat(80)
    cyph[50] *= 0.90  # only a long-CYPH signal
    res = run_backtest(make_panel(cyph, zec), Config(entry_z=2.0, lookback=20, direction="short_cyph_only"))
    assert res.trades.empty or (res.trades["side"] == SHORT_CYPH).all()


def test_costs_and_equity_reconcile_with_trades():
    cyph, zec = noisy_flat(300, seed=3)
    cyph = cyph * np.exp(np.cumsum(np.random.default_rng(4).normal(0, 0.01, 300)))
    panel = make_panel(cyph, zec)
    cfg = Config(entry_z=1.0, lookback=20, cost_bps_cyph=20, cost_bps_zec=30, borrow_rate_cyph=0.5)
    res = run_backtest(panel, cfg)
    assert len(res.trades) > 3
    # Final equity = starting capital + sum of trade P&L.
    assert res.equity.iloc[-1] == pytest.approx(cfg.initial_capital_gbp + res.trades["net_pnl_gbp"].sum())
    # Round-trip trading cost is ~ (20 + 30) bps x 2 sides x GBP 1,000.
    assert res.trades["costs_gbp"].mean() == pytest.approx(10.0, rel=0.05)
    net = res.trades["gross_pnl_gbp"] - res.trades["costs_gbp"] - res.trades["borrow_gbp"]
    np.testing.assert_allclose(net, res.trades["net_pnl_gbp"])
    assert summarize(res)["trades"] == len(res.trades)


def test_stop_loss_blocks_immediate_reentry():
    n = 120
    cyph, zec = noisy_flat(n, seed=1)
    cyph = cyph * np.exp(0.01 * np.sin(np.arange(n) * 0.9))
    cyph[50:] *= 1.025  # z ~2.9 at bar 50 -> enter short CYPH
    cyph[52:] *= 1.04  # z ~3.2 at bar 52 -> stop out; z is still ~2.3 on bar 53
    res = run_backtest(make_panel(cyph, zec), Config(entry_z=2.0, stop_z=3.0, lookback=20, **ZERO_COSTS))
    assert res.trades.iloc[0]["exit_reason"] == "z stop-loss"
    # Without the re-entry block, bar 53 (z > 2) would re-open the same losing trade.
    assert len(res.trades) == 1


def test_no_entry_when_z_already_beyond_stop():
    cyph, zec = noisy_flat(80)
    cyph[50] *= 1.10  # single-bar gap that takes z far past the stop level
    res = run_backtest(make_panel(cyph, zec), Config(entry_z=1.0, stop_z=2.0, lookback=20, **ZERO_COSTS))
    assert res.trades.empty or (res.trades["entry_z"].abs() < 2.0).all()


def test_long_only_buys_the_cheap_asset_without_shorting():
    cyph, zec = noisy_flat(80)
    cyph[50] *= 0.90  # CYPH cheap vs ZEC
    res = run_backtest(make_panel(cyph, zec), Config(entry_z=2.0, lookback=20, mode="long_only", borrow_rate_zec=0.5))
    t = res.trades.iloc[0]
    assert t["direction"].startswith("Buy CYPH")
    assert t["cyph_shares"] > 0 and t["zec_units"] == 0
    assert (res.trades["borrow_gbp"] == 0).all()

    cyph, zec = noisy_flat(80)
    cyph[50] *= 1.10  # CYPH rich, so ZEC is the cheap one
    t = run_backtest(make_panel(cyph, zec), Config(entry_z=2.0, lookback=20, mode="long_only")).trades.iloc[0]
    assert t["direction"].startswith("Buy ZEC")
    assert t["zec_units"] > 0 and t["cyph_shares"] == 0


def test_long_only_selection_is_half_the_pairs_trade():
    rng = np.random.default_rng(5)
    cyph = 2.0 * np.exp(np.cumsum(rng.normal(0, 0.02, 400)))
    zec = 400.0 * np.exp(np.cumsum(rng.normal(0, 0.02, 400)))
    panel = make_panel(cyph, zec)
    pair = run_backtest(panel, Config(entry_z=1.5, lookback=30, **ZERO_COSTS))
    att = attribute(run_backtest(panel, Config(entry_z=1.5, lookback=30, mode="long_only", **ZERO_COSTS)).trades)
    assert len(att) > 3
    np.testing.assert_allclose(att["market_gbp"] + att["selection_gbp"], att["gross_pnl_gbp"])
    # Same trades, so the long-only selection edge is half the hedged spread P&L
    # (up to whole-share rounding on the CYPH leg).
    assert att["selection_gbp"].sum() == pytest.approx(pair.trades["gross_pnl_gbp"].sum() / 2, abs=5)


def test_coin_flip_rejects_a_perfect_picker():
    att = pd.DataFrame({"gross_pnl_gbp": np.full(12, 50.0), "other_gross_gbp": np.full(12, -50.0)})
    p, sims = coin_flip_test(att, n_sims=5000)
    assert p < 0.01 and sims.max() <= 600
