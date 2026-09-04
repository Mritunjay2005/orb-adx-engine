"""
=================================================================================
 ORB (Opening Range Breakout) 15-min SCREENING BACKTESTER  --  Upstox API
=================================================================================

STRATEGY (as specified):
 - Timeframe: 15-min candles.
 - The FIRST 15-min candle of the day sets the opening range (High1 / Low1).
 - Whichever level breaks first during the day triggers the trade:
       price > High1  -> LONG
       price < Low1   -> SHORT
   Only ONE trade per stock per day (whichever direction breaks first wins;
   the other side is ignored for the rest of the day).
 - Stop-loss (default): the OPPOSITE opening-range level.
       Long  -> SL = Low1
       Short -> SL = High1
 - "Big candle" exception: if (High1-Low1)/Open1 > 1.5%, ignore the opposite-level
   SL and instead use SL = entry +/- 2 * (High1-Low1)   [ (High1-Low1) is being
   used as "ATR" per your instruction: just the first candle's own range ].
 - Trade filter: Daily ADX(14) must be > 25, recomputed "live" at the moment of
   entry -- i.e. using the last 13 COMPLETE daily candles plus a SYNTHETIC
   "today" daily candle built only from the 15-min bars up to (and including)
   the breakout candle. This avoids any lookahead bias.
 - Risk:Reward = 1 : 1.5   (target = entry +/- 1.5 * risk)
 - Trailing stop: ratchets continuously. Every time price advances a further
   0.2% (of entry price) in the favourable direction, the SL is moved by the
   same 0.2% (of entry price) in the trade's favour.
 - Costs: 0.5% of the ENTRY value only (exit side is free), deducted from PnL.
 - All open trades are force-closed at SQUARE_OFF_TIME (before market close).

RANKING:
 - Every instrument in the CSV is screened over the last N days (default 60).
 - Ranked by NET P&L (highest first).
 - A "breakout_days" column shows how many days the stock actually broke its
   ORB high/low (i.e. was NOT just sideways) -- this is independent of the
   ADX filter, so you can see how "active" a stock's ORB setup really is
   versus how many of those actually became live trades after the ADX gate.

ASSUMPTIONS CALLED OUT (documented inline where they occur too):
 1. Entry price = the exact breakout level (High1 or Low1), simulating a stop
    order sitting at that level (we only have 15-min OHLC, not tick data).
 2. If a single 15-min candle breaches BOTH High1 and Low1, we assume the
    breakout that happened FIRST is the one nearer the candle's open-to-close
    direction: if candle closed above its open we assume High1 broke first
    (LONG), otherwise we assume Low1 broke first (SHORT). This is a
    necessary approximation given candle (not tick) data.
 3. Within the SAME candle as entry (or any candle after), if both the target
    and the SL are breached, the SL is assumed to trigger first (conservative).
 4. "Exit at close before market close" uses the last candle at/after
    SQUARE_OFF_TIME.
=================================================================================
"""

import os
import time
import math
from datetime import datetime, timedelta, date

import requests
import pandas as pd
import numpy as np
import pytz

# =================================================================================
# CONFIG  --  FILL THESE IN
# =================================================================================

# >>> PASTE YOUR UPSTOX ACCESS TOKEN HERE (regenerate daily, it expires) <<<
UPSTOX_ACCESS_TOKEN = "eyJ0eXAiOiJKV1QiLCJrZXlfaWQiOiJza192MS4wIiwiYWxnIjoiSFMyNTYifQ.eyJzdWIiOiI4NEJaTUgiLCJqdGkiOiI2YThjOGNhMDQ4ODI1ODU1MjUxYTg2MGEiLCJpc011bHRpQ2xpZW50IjpmYWxzZSwiaXNQbHVzUGxhbiI6ZmFsc2UsImlzRXh0ZW5kZWQiOnRydWUsImlhdCI6MTc4NzU5NTkzNiwiaXNzIjoidWRhcGktZ2F0ZXdheS1zZXJ2aWNlIiwiZXhwIjoxODE5MTQ0ODAwfQ.uKXERjcIVTgCmcNkQwwDLDDDwdNQDoHTIT3HymGij5E"   # <-- put your token here

# Path to your instruments CSV. Expected columns (yours already has these):
# symbol,instrument_key,close,atr,atr_pct,avg_volume_20d
INSTRUMENT_CSV_PATH = "filtered_stocks.csv"

# Where fetched candle data is cached locally (so re-runs don't re-hit the API)
CACHE_DIR = "data_cache"
# Where results are written
OUTPUT_DIR = "output"

# Screening window: how many CALENDAR days back to backtest (per your 45-60 day ask)
BACKTEST_LOOKBACK_DAYS = 60

# End date of the backtest window. None = today.
BACKTEST_END_DATE = None   # or e.g. date(2026, 9, 1)

# Extra calendar days fetched BEFORE the window, purely so the daily ADX(14)
# has enough prior complete daily candles to be stable before day 1 of the window.
ADX_WARMUP_CALENDAR_DAYS = 45

# --- Upstox V3 historical candle API ---
UPSTOX_BASE_URL = "https://api.upstox.com/v3/historical-candle"
INTERVAL_UNIT = "minutes"
INTERVAL_VALUE = "15"
API_CHUNK_DAYS = 25          # split big date ranges into chunks (safe margin)
API_SLEEP_SECONDS = 0.35     # politeness delay between API calls
API_MAX_RETRIES = 3

# --- Session / time config (NSE) ---
IST = pytz.timezone("Asia/Kolkata")
MARKET_OPEN_TIME = "09:15"
FIRST_CANDLE_END_TIME = "09:30"   # first 15-min candle: 09:15-09:30
SQUARE_OFF_TIME = "15:14"         # force-exit any open trade at/after this time

# --- Strategy parameters ---
ADX_PERIOD = 14
ADX_THRESHOLD = 25.0
BIG_CANDLE_THRESHOLD_PCT = 1.5     # (High1-Low1)/Open1 * 100
ATR_SL_MULTIPLIER = 2.0            # SL distance = 2 * (High1-Low1) for big candles
TRAIL_STEP_PCT = 0.2               # ratchet SL every 0.2% of entry price
RISK_REWARD = 1.5                  # target = entry +/- 1.5 * risk
ENTRY_COST_PCT = 0.5               # 0.5% of entry value, entry side only

os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)


# =================================================================================
# DATA FETCHING  (Upstox V3 historical candle API, with local caching)
# =================================================================================

def _daterange_chunks(start_date, end_date, chunk_days):
    """Yield (chunk_start, chunk_end) tuples covering [start_date, end_date]."""
    cur = start_date
    while cur <= end_date:
        chunk_end = min(cur + timedelta(days=chunk_days - 1), end_date)
        yield cur, chunk_end
        cur = chunk_end + timedelta(days=1)


def _fetch_upstox_chunk(instrument_key, from_date, to_date):
    """Fetch one chunk of 15-min candles from Upstox V3 API. Returns list of candles."""
    url = (
        f"{UPSTOX_BASE_URL}/{instrument_key}/{INTERVAL_UNIT}/{INTERVAL_VALUE}/"
        f"{to_date.isoformat()}/{from_date.isoformat()}"
    )
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {UPSTOX_ACCESS_TOKEN}",
    }

    for attempt in range(1, API_MAX_RETRIES + 1):
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("data", {}).get("candles", [])
        elif resp.status_code == 429:
            # rate limited -- back off and retry
            wait = 2 * attempt
            print(f"  [rate limited] waiting {wait}s...")
            time.sleep(wait)
        elif resp.status_code == 401:
            raise RuntimeError(
                "Upstox returned 401 Unauthorized -- your access token is "
                "missing/expired. Paste a fresh UPSTOX_ACCESS_TOKEN at the top "
                "of this script."
            )
        else:
            print(f"  [warn] {instrument_key} {from_date}->{to_date}: "
                  f"HTTP {resp.status_code} - {resp.text[:200]}")
            time.sleep(1)
    return []  # give up on this chunk after retries


def fetch_15min_candles(symbol, instrument_key, from_date, to_date):
    """
    Get 15-min candles for [from_date, to_date] (inclusive), using a local CSV
    cache so repeated runs don't re-hit the API for dates already fetched.
    Returns a DataFrame with columns: timestamp, open, high, low, close, volume, oi
    (timestamp is tz-aware, Asia/Kolkata).
    """
    cache_path = os.path.join(CACHE_DIR, f"{symbol}_15min.csv")

    cached = None
    if os.path.exists(cache_path):
        cached = pd.read_csv(cache_path, parse_dates=["timestamp"])
        cached["timestamp"] = cached["timestamp"].dt.tz_localize(None).dt.tz_localize(IST)
        have_from = cached["timestamp"].dt.date.min()
        have_to = cached["timestamp"].dt.date.max()
        # If cache already fully covers the requested range, skip the API entirely
        if have_from <= from_date and have_to >= to_date:
            mask = (cached["timestamp"].dt.date >= from_date) & (cached["timestamp"].dt.date <= to_date)
            return cached.loc[mask].sort_values("timestamp").reset_index(drop=True)

    print(f"  fetching {symbol} ({instrument_key}) {from_date} -> {to_date} ...")
    all_candles = []
    for chunk_start, chunk_end in _daterange_chunks(from_date, to_date, API_CHUNK_DAYS):
        candles = _fetch_upstox_chunk(instrument_key, chunk_start, chunk_end)
        all_candles.extend(candles)
        time.sleep(API_SLEEP_SECONDS)

    if not all_candles:
        print(f"  [warn] no data returned for {symbol}")
        cols = ["timestamp", "open", "high", "low", "close", "volume", "oi"]
        return pd.DataFrame(columns=cols)

    # Upstox candle format: [timestamp, open, high, low, close, volume, oi]
    df = pd.DataFrame(all_candles, columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
    df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_convert(IST)
    df = df.sort_values("timestamp").drop_duplicates(subset="timestamp").reset_index(drop=True)

    # merge with cache (if any) and re-save
    if cached is not None:
        df = pd.concat([cached, df], ignore_index=True)
        df = df.sort_values("timestamp").drop_duplicates(subset="timestamp").reset_index(drop=True)
    df.to_csv(cache_path, index=False)

    mask = (df["timestamp"].dt.date >= from_date) & (df["timestamp"].dt.date <= to_date)
    return df.loc[mask].sort_values("timestamp").reset_index(drop=True)


# =================================================================================
# DAILY BARS  +  ADX(14)  ("live", no-lookahead)
# =================================================================================

def build_daily_bars(df_15min):
    """Resample 15-min candles into daily OHLC bars (used only for ADX)."""
    if df_15min.empty:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close"])
    d = df_15min.copy()
    d["date"] = d["timestamp"].dt.date
    daily = d.groupby("date").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
    ).reset_index()
    return daily.sort_values("date").reset_index(drop=True)


def wilder_adx(daily_df, period=ADX_PERIOD):
    """
    Standard Wilder's ADX(period) computed on a daily OHLC dataframe
    (columns: date, open, high, low, close). Returns the daily_df with an
    'adx' column appended (NaN until enough bars have accumulated).
    The LAST row of daily_df may be a partial/synthetic "today so far" bar --
    that's exactly what makes the ADX value "live" at the point it's read.
    """
    df = daily_df.copy().reset_index(drop=True)
    if len(df) < period + 1:
        df["adx"] = np.nan
        return df

    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    prev_high = high.shift(1)
    prev_low = low.shift(1)

    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    up_move = high - prev_high
    down_move = prev_low - low

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr = tr.to_numpy()
    plus_dm = pd.Series(plus_dm)
    minus_dm = pd.Series(minus_dm)

    # Wilder's smoothing (RMA)
    def wilder_smooth(series, period):
        result = np.full(len(series), np.nan)
        if len(series) < period:
            return result
        result[period - 1] = series[:period].sum()
        for i in range(period, len(series)):
            result[i] = result[i - 1] - (result[i - 1] / period) + series.iloc[i]
        return result

    tr_smooth = wilder_smooth(pd.Series(tr), period)
    plus_dm_smooth = wilder_smooth(plus_dm, period)
    minus_dm_smooth = wilder_smooth(minus_dm, period)

    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100 * (plus_dm_smooth / tr_smooth)
        minus_di = 100 * (minus_dm_smooth / tr_smooth)
        dx = 100 * (np.abs(plus_di - minus_di) / (plus_di + minus_di))

    dx = pd.Series(dx)
    adx = np.full(len(dx), np.nan)
    first_valid = dx.first_valid_index()
    if first_valid is not None and first_valid + period <= len(dx):
        start = first_valid + period - 1
        if start < len(dx):
            adx[start] = dx[first_valid:first_valid + period].mean()
            for i in range(start + 1, len(dx)):
                if np.isnan(dx.iloc[i]) or np.isnan(adx[i - 1]):
                    continue
                adx[i] = (adx[i - 1] * (period - 1) + dx.iloc[i]) / period

    df["adx"] = adx
    return df


def live_adx_at_entry(prior_daily_bars, today_partial_bar):
    """
    Compute the 'live' daily ADX(14) at the moment of entry:
      prior_daily_bars   -> COMPLETE daily bars strictly before today
      today_partial_bar  -> dict(open, high, low, close) built ONLY from
                             today's 15-min candles up to & including the
                             breakout candle.
    Returns the ADX value for that synthetic "today" bar, or NaN if not
    enough history yet.
    """
    if prior_daily_bars.empty:
        return np.nan
    combined = pd.concat(
        [prior_daily_bars, pd.DataFrame([today_partial_bar])],
        ignore_index=True,
    )
    result = wilder_adx(combined, ADX_PERIOD)
    return result["adx"].iloc[-1]


# =================================================================================
# ORB TRADE SIMULATION  (one trading day)
# =================================================================================

def simulate_orb_day(day_candles, prior_daily_bars):
    """
    Run the ORB strategy for a single day's 15-min candles.
    Returns a dict describing the outcome:
        {
          'breakout_occurred': bool,   # broke High1 or Low1 at all (not sideways)
          'trade_taken': bool,         # breakout_occurred AND passed ADX filter
          'direction': 'LONG'/'SHORT'/None,
          'entry_price', 'exit_price', 'sl', 'target',
          'pnl_pct', 'pnl_value_per_share', 'exit_reason', 'adx_at_entry'
        }
    """
    out = {
        "breakout_occurred": False, "trade_taken": False, "direction": None,
        "entry_price": None, "exit_price": None, "sl": None, "target": None,
        "pnl_pct": None, "exit_reason": None, "adx_at_entry": None,
    }

    if len(day_candles) < 2:
        return out

    day_candles = day_candles.sort_values("timestamp").reset_index(drop=True)
    first = day_candles.iloc[0]
    high1, low1, open1 = first["high"], first["low"], first["open"]
    if open1 == 0 or pd.isna(open1):
        return out

    range1 = high1 - low1
    big_candle = (range1 / open1 * 100.0) > BIG_CANDLE_THRESHOLD_PCT

    square_off_dt = pd.Timestamp.combine(
        day_candles["timestamp"].iloc[0].date(),
        pd.to_datetime(SQUARE_OFF_TIME).time()
    ).tz_localize(IST)

    # --- find the breakout candle (first candle AFTER the opening one that
    #     breaches High1 or Low1) ---
    entry_idx = None
    direction = None
    for i in range(1, len(day_candles)):
        c = day_candles.iloc[i]
        broke_high = c["high"] > high1
        broke_low = c["low"] < low1
        if broke_high and broke_low:
            # Same candle breached both sides -- approximate using candle's
            # own open->close direction (see assumption #2 in the file header).
            direction = "LONG" if c["close"] >= c["open"] else "SHORT"
            entry_idx = i
            break
        elif broke_high:
            direction = "LONG"
            entry_idx = i
            break
        elif broke_low:
            direction = "SHORT"
            entry_idx = i
            break

    if entry_idx is None:
        return out  # sideways day -- no breakout at all

    out["breakout_occurred"] = True
    out["direction"] = direction
    entry_price = high1 if direction == "LONG" else low1

    # --- ADX gate, computed live using only data up to & including entry candle ---
    today_so_far = day_candles.iloc[:entry_idx + 1]
    today_partial_bar = {
        "date": today_so_far["timestamp"].iloc[0].date(),
        "open": today_so_far["open"].iloc[0],
        "high": today_so_far["high"].max(),
        "low": today_so_far["low"].min(),
        "close": today_so_far["close"].iloc[-1],
    }
    adx_val = live_adx_at_entry(prior_daily_bars, today_partial_bar)
    out["adx_at_entry"] = adx_val

    if pd.isna(adx_val) or adx_val <= ADX_THRESHOLD:
        out["exit_reason"] = "ADX_FILTER_BLOCKED"
        return out  # breakout happened, but ADX filter blocks the trade

    # --- initial SL / target ---
    if big_candle:
        sl_distance = ATR_SL_MULTIPLIER * range1
        sl = entry_price - sl_distance if direction == "LONG" else entry_price + sl_distance
    else:
        sl = low1 if direction == "LONG" else high1
        sl_distance = abs(entry_price - sl)

    if sl_distance <= 0:
        out["exit_reason"] = "INVALID_SL"
        return out

    target = (entry_price + RISK_REWARD * sl_distance if direction == "LONG"
              else entry_price - RISK_REWARD * sl_distance)

    out["trade_taken"] = True
    out["entry_price"] = entry_price
    out["sl"] = sl
    out["target"] = target

    trail_step = entry_price * (TRAIL_STEP_PCT / 100.0)
    trail_ref = entry_price          # last ratchet reference price
    current_sl = sl

    exit_price = None
    exit_reason = None

    # Walk forward candle-by-candle from the entry candle onward
    for i in range(entry_idx, len(day_candles)):
        c = day_candles.iloc[i]

        # Force square-off before market close
        if c["timestamp"] >= square_off_dt:
            exit_price = c["open"] if i == entry_idx else c["open"]
            exit_price = c["close"]
            exit_reason = "SQUARE_OFF_EOD"
            break

        if direction == "LONG":
            # ratchet trailing SL upward on favourable moves
            while c["high"] >= trail_ref + trail_step:
                trail_ref += trail_step
                current_sl += trail_step
            # SL checked before target within same candle (conservative, see assumption #3)
            if c["low"] <= current_sl:
                exit_price = current_sl
                exit_reason = "STOP_LOSS" if current_sl <= sl else "TRAILING_SL"
                break
            if c["high"] >= target:
                exit_price = target
                exit_reason = "TARGET"
                break
        else:  # SHORT
            while c["low"] <= trail_ref - trail_step:
                trail_ref -= trail_step
                current_sl -= trail_step
            if c["high"] >= current_sl:
                exit_price = current_sl
                exit_reason = "STOP_LOSS" if current_sl >= sl else "TRAILING_SL"
                break
            if c["low"] <= target:
                exit_price = target
                exit_reason = "TARGET"
                break

    if exit_price is None:
        # ran out of candles without hitting square-off condition explicitly
        exit_price = day_candles.iloc[-1]["close"]
        exit_reason = "EOD_LAST_CANDLE"

    # --- PnL (in % of entry price), net of entry-side cost only ---
    if direction == "LONG":
        gross_pct = (exit_price - entry_price) / entry_price * 100.0
    else:
        gross_pct = (entry_price - exit_price) / entry_price * 100.0
    net_pct = gross_pct - ENTRY_COST_PCT

    out["exit_price"] = exit_price
    out["exit_reason"] = exit_reason
    out["pnl_pct"] = net_pct
    return out


# =================================================================================
# PER-INSTRUMENT BACKTEST
# =================================================================================

def backtest_instrument(symbol, instrument_key, window_start, window_end):
    """
    Backtest one instrument over [window_start, window_end] and return
    (summary_dict, trade_log_dataframe).
    """
    fetch_start = window_start - timedelta(days=ADX_WARMUP_CALENDAR_DAYS)
    df_15min = fetch_15min_candles(symbol, instrument_key, fetch_start, window_end)

    if df_15min.empty:
        return {
            "symbol": symbol, "instrument_key": instrument_key,
            "trading_days": 0, "breakout_days": 0, "trades_taken": 0,
            "wins": 0, "losses": 0, "win_rate_pct": None,
            "net_pnl_pct": 0.0, "avg_pnl_per_trade_pct": None,
        }, pd.DataFrame()

    daily_bars_all = build_daily_bars(df_15min)
    df_15min["date"] = df_15min["timestamp"].dt.date

    all_dates = sorted(d for d in df_15min["date"].unique() if window_start <= d <= window_end)

    trading_days = 0
    breakout_days = 0
    trades = []

    for d in all_dates:
        day_candles = df_15min[df_15min["date"] == d]
        if len(day_candles) < 2:
            continue
        trading_days += 1

        prior_daily_bars = daily_bars_all[daily_bars_all["date"] < d].reset_index(drop=True)

        result = simulate_orb_day(day_candles, prior_daily_bars)
        if result["breakout_occurred"]:
            breakout_days += 1
        if result["trade_taken"]:
            trades.append({
                "date": d,
                "direction": result["direction"],
                "entry_price": result["entry_price"],
                "sl": result["sl"],
                "target": result["target"],
                "exit_price": result["exit_price"],
                "exit_reason": result["exit_reason"],
                "adx_at_entry": result["adx_at_entry"],
                "pnl_pct": result["pnl_pct"],
            })

    trade_log = pd.DataFrame(trades)
    n_trades = len(trade_log)
    wins = int((trade_log["pnl_pct"] > 0).sum()) if n_trades else 0
    losses = n_trades - wins
    net_pnl_pct = float(trade_log["pnl_pct"].sum()) if n_trades else 0.0
    win_rate = (wins / n_trades * 100.0) if n_trades else None
    avg_pnl = (net_pnl_pct / n_trades) if n_trades else None

    summary = {
        "symbol": symbol,
        "instrument_key": instrument_key,
        "trading_days": trading_days,
        "breakout_days": breakout_days,     # broke ORB high/low (not sideways)
        "trades_taken": n_trades,           # breakout AND passed ADX filter
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(win_rate, 2) if win_rate is not None else None,
        "net_pnl_pct": round(net_pnl_pct, 3),
        "avg_pnl_per_trade_pct": round(avg_pnl, 3) if avg_pnl is not None else None,
    }
    return summary, trade_log


# =================================================================================
# MAIN
# =================================================================================

def main():
    if UPSTOX_ACCESS_TOKEN == "PASTE_YOUR_UPSTOX_ACCESS_TOKEN_HERE":
        raise RuntimeError(
            "Please paste your Upstox access token into UPSTOX_ACCESS_TOKEN "
            "at the top of this script before running."
        )

    instruments = pd.read_csv(INSTRUMENT_CSV_PATH)
    required_cols = {"symbol", "instrument_key"}
    if not required_cols.issubset(instruments.columns):
        raise ValueError(
            f"CSV must contain columns {required_cols}. "
            f"Found: {list(instruments.columns)}"
        )

    end_date = BACKTEST_END_DATE or date.today()
    start_date = end_date - timedelta(days=BACKTEST_LOOKBACK_DAYS)

    print(f"Screening {len(instruments)} instruments from {start_date} to {end_date} "
          f"on 15-min ORB strategy...\n")

    all_summaries = []
    for _, row in instruments.iterrows():
        symbol = row["symbol"]
        instrument_key = row["instrument_key"]
        try:
            summary, trade_log = backtest_instrument(symbol, instrument_key, start_date, end_date)
        except Exception as e:
            print(f"  [error] {symbol}: {e}")
            continue

        all_summaries.append(summary)

        if not trade_log.empty:
            trade_log.to_csv(os.path.join(OUTPUT_DIR, f"trades_{symbol}.csv"), index=False)

    results = pd.DataFrame(all_summaries)
    if results.empty:
        print("No results produced -- check your CSV / API token / date range.")
        return

    results = results.sort_values("net_pnl_pct", ascending=False).reset_index(drop=True)
    results.insert(0, "rank", results.index + 1)

    out_path = os.path.join(OUTPUT_DIR, "ranked_results.csv")
    results.to_csv(out_path, index=False)

    print("\n=== RANKED RESULTS (by net P&L %) ===")
    print(results.to_string(index=False))
    print(f"\nSaved: {out_path}")
    print(f"Per-trade logs saved as {OUTPUT_DIR}/trades_<symbol>.csv")


if __name__ == "__main__":
    main()
