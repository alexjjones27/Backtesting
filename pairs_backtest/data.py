"""Download, cache and align the market data used by the backtest.

Sources (all free, no API key):
  * CYPH  - Yahoo Finance hourly bars (Nasdaq regular session only).
  * ZEC   - Coinbase Exchange ZEC-USD 15-minute candles. 15m resolution lets us
            price ZEC at the exact moment each CYPH hourly bar closes (CYPH bars
            close on the half hour: 10:30, 11:30 ... 16:00 ET).
  * GBPUSD - Yahoo Finance hourly FX bars, used to size positions in GBP and to
            convert USD P&L back into GBP.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
NY_TZ = "America/New_York"

YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
COINBASE_URL = "https://api.exchange.coinbase.com/products/{product}/candles"
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (pairs-backtest)"}

CYPH_FILE = "cyph_1h.csv"
ZEC_FILE = "zec_15m.csv"
FX_FILE = "gbpusd_1h.csv"

# A ZEC/FX quote older than this when we need a price is treated as missing.
MAX_QUOTE_STALENESS = pd.Timedelta(hours=2)
MAX_FX_STALENESS = pd.Timedelta(days=4)  # FX is closed at weekends


def _get(url: str, params: dict, retries: int = 4) -> requests.Response:
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, headers=HTTP_HEADERS, timeout=30)
            if resp.status_code == 429:
                raise requests.HTTPError("rate limited")
            resp.raise_for_status()
            return resp
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** (attempt + 1))
    raise RuntimeError("unreachable")


def fetch_yahoo(symbol: str, interval: str = "1h", range_: str = "1y") -> pd.DataFrame:
    """OHLCV bars from Yahoo Finance, indexed by bar *start* time (UTC)."""
    resp = _get(
        YAHOO_URL.format(symbol=symbol),
        {"interval": interval, "range": range_, "includePrePost": "false"},
    )
    chart = resp.json()["chart"]
    if not chart["result"]:
        raise RuntimeError(f"Yahoo returned no data for {symbol}: {chart.get('error')}")
    result = chart["result"][0]
    quote = result["indicators"]["quote"][0]
    df = pd.DataFrame(
        {k: quote[k] for k in ("open", "high", "low", "close", "volume")},
        index=pd.to_datetime(result["timestamp"], unit="s", utc=True),
    )
    df.index.name = "start"
    return df


def fetch_coinbase(product: str, granularity: int, start: datetime, end: datetime) -> pd.DataFrame:
    """Candles from Coinbase Exchange, indexed by candle *start* time (UTC).

    The public endpoint returns at most 300 candles per request, so the range is
    paged through in chunks.
    """
    step = timedelta(seconds=granularity * 300)
    frames = []
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + step, end)
        resp = _get(
            COINBASE_URL.format(product=product),
            {"granularity": granularity, "start": cursor.isoformat(), "end": chunk_end.isoformat()},
        )
        rows = resp.json()
        if rows:
            frames.append(pd.DataFrame(rows, columns=["time", "low", "high", "open", "close", "volume"]))
        cursor = chunk_end
        time.sleep(0.25)  # stay well inside the public rate limit
    df = pd.concat(frames, ignore_index=True).drop_duplicates("time")
    df.index = pd.to_datetime(df.pop("time"), unit="s", utc=True)
    df.index.name = "start"
    return df.sort_index()[["open", "high", "low", "close", "volume"]]


def download_all(data_dir: Path = DATA_DIR) -> None:
    """Fetch the latest year of data for every instrument and write it to CSV."""
    data_dir.mkdir(parents=True, exist_ok=True)

    print("Downloading CYPH hourly bars (Yahoo Finance)...")
    cyph = fetch_yahoo("CYPH", "1h", "1y")
    cyph.to_csv(data_dir / CYPH_FILE)

    print("Downloading GBPUSD hourly bars (Yahoo Finance)...")
    fx = fetch_yahoo("GBPUSD=X", "1h", "1y")
    fx.to_csv(data_dir / FX_FILE)

    # Pull ZEC from a few days before the first CYPH bar so the first bars can be priced.
    zec_start = (cyph.index.min() - pd.Timedelta(days=3)).to_pydatetime()
    zec_end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    print(f"Downloading ZEC-USD 15m candles (Coinbase) from {zec_start:%Y-%m-%d}...")
    zec = fetch_coinbase("ZEC-USD", 900, zec_start, zec_end)
    zec.to_csv(data_dir / ZEC_FILE)
    print(f"Saved {len(cyph)} CYPH bars, {len(zec)} ZEC candles, {len(fx)} FX bars to {data_dir}")


def _read(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, index_col="start")
    df.index = pd.to_datetime(df.index, utc=True)
    return df


def load_raw(data_dir: Path = DATA_DIR, refresh: bool = False) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    files = [data_dir / f for f in (CYPH_FILE, ZEC_FILE, FX_FILE)]
    if refresh or not all(f.exists() for f in files):
        download_all(data_dir)
    return tuple(_read(f) for f in files)  # type: ignore[return-value]


def _price_at(series: pd.Series, times: pd.DatetimeIndex, max_staleness: pd.Timedelta) -> pd.Series:
    """Last observed value at or before each requested time (NaN if too stale)."""
    src = pd.DataFrame({"t_obs": series.index, "value": series.to_numpy()}).dropna()
    req = pd.DataFrame({"t": times})
    merged = pd.merge_asof(req, src, left_on="t", right_on="t_obs", direction="backward")
    stale = (merged["t"] - merged["t_obs"]) > max_staleness
    merged.loc[stale, "value"] = float("nan")
    return pd.Series(merged["value"].to_numpy(), index=times)


def build_panel(cyph: pd.DataFrame, zec: pd.DataFrame, fx: pd.DataFrame) -> pd.DataFrame:
    """Align both legs onto the CYPH hourly trading grid.

    Each row is one CYPH regular-session hourly bar, indexed by its *close* time
    (UTC). Columns:
        bar_start              when the bar opened (orders are filled here)
        cyph_open / cyph_close CYPH prices at bar open / close
        zec_open  / zec_close  ZEC price at exactly the same two instants
        fx_open   / fx_close   GBPUSD at the same two instants
    """
    bars = cyph.dropna(subset=["open", "close"])
    # Regular-session bars start on the half hour (09:30 ... 15:30 ET). Yahoo also
    # appends "snapshot" bars at 16:00 (and 13:00 on half days) that are not real
    # trading intervals, so keep only the half-hour grid.
    bars = bars[bars.index.tz_convert(NY_TZ).minute == 30]

    start = bars.index
    local_start = start.tz_convert(NY_TZ)
    session_close = local_start.normalize() + pd.Timedelta(hours=16)
    end_local = (local_start + pd.Timedelta(hours=1)).where(
        local_start + pd.Timedelta(hours=1) <= session_close, session_close
    )
    end = pd.DatetimeIndex(end_local).tz_convert("UTC")

    # ZEC price at time T = close of the 15m candle that ends at T.
    zec_close_by_end = pd.Series(zec["close"].to_numpy(), index=zec.index + pd.Timedelta(minutes=15))
    fx_close_by_end = pd.Series(fx["close"].to_numpy(), index=fx.index + pd.Timedelta(hours=1))

    panel = pd.DataFrame(
        {
            "bar_start": start,
            "cyph_open": bars["open"].to_numpy(),
            "cyph_close": bars["close"].to_numpy(),
            "cyph_volume": bars["volume"].to_numpy(),
            "zec_open": _price_at(zec_close_by_end, start, MAX_QUOTE_STALENESS).to_numpy(),
            "zec_close": _price_at(zec_close_by_end, end, MAX_QUOTE_STALENESS).to_numpy(),
            "fx_open": _price_at(fx_close_by_end, start, MAX_FX_STALENESS).to_numpy(),
            "fx_close": _price_at(fx_close_by_end, end, MAX_FX_STALENESS).to_numpy(),
        },
        index=end,
    )
    panel.index.name = "time"
    missing = panel[["zec_open", "zec_close", "fx_open", "fx_close"]].isna().any(axis=1)
    if missing.any():
        print(f"Warning: dropping {int(missing.sum())} bars with no ZEC/FX quote")
    return panel[~missing]


def load_panel(data_dir: Path = DATA_DIR, refresh: bool = False) -> pd.DataFrame:
    return build_panel(*load_raw(data_dir, refresh))
