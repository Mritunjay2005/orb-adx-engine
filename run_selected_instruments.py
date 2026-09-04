"""
=================================================================================
 RUN SELECTED INSTRUMENTS  ->  trade_log.csv + equity_curve.csv
 (for trade_diagnostic_and_robustness_suite.ipynb)
=================================================================================

WHAT THIS SCRIPT DOES
 - You looked at output/ranked_results.csv from orb_backtester.py and picked a
   shortlist of symbols you actually want to inspect.
 - This script reruns THE EXACT SAME STRATEGY ENGINE from orb_backtester.py on
   just that shortlist, and writes two files the diagnostic notebook expects:
       output/trade_log.csv
       output/equity_curve.csv

WHAT IS AND ISN'T CHANGED VS. orb_backtester.py
 - Every function that decides WHEN to enter, WHERE the stop/target/trail sit,
   WHETHER the ADX filter blocks a trade, and HOW cost is deducted is imported
   UNCHANGED from orb_backtester.py (fetch_15min_candles, build_daily_bars,
   wilder_adx, live_adx_at_entry). Nothing about data fetching or the ADX
   calculation is touched.
 - The one function this script does NOT import as-is is `simulate_orb_day`.
   Below is `simulate_orb_day_logged`, a line-for-line copy of it with a few
   ADDITIVE statements inserted (marked "# >>> ADDED") that only *record*
   which candle triggered entry/exit and a couple of extra descriptive fields
   (or_high, or_low, gap_pct). Every existing condition, threshold, formula,
   and branch -- the actual trading logic -- is untouched, in the same order.
   Diff it against orb_backtester.py's simulate_orb_day if you want to verify
   this yourself; the added lines are commented and do not feed back into any
   entry/exit/SL/target/trailing/cost decision.
 - Position size: the original engine works entirely in % of entry price (it
   never tracked quantity or rupee P&L). The diagnostic notebook wants rupee
   P&L + a quantity column, so this script multiplies each trade's % return
   by ENTRY_PRICE x POSITION_SIZE_QTY to get a rupee number. This is a
   *presentation* conversion only -- it scales every trade by the same rule,
   so win rate, R-multiples, profit factor etc. in the notebook are identical
   to what the % numbers already implied. Change POSITION_SIZE_QTY below if
   you want a different (fixed) size per trade.

HOW TO USE
 1. Fill in SELECTED_SYMBOLS below with the symbols you picked from
    ranked_results.csv (must exist in your instrument CSV).
 2. Make sure orb_backtester.py in this same folder has a valid, un-expired
    UPSTOX_ACCESS_TOKEN and the correct INSTRUMENT_CSV_PATH -- this script
    reuses that config as-is.
 3. Run:  python3 run_selected_instruments.py
 4. Point the notebook's TRADE_LOG_PATH / EQUITY_CURVE_PATH at
    output/trade_log.csv and output/equity_curve.csv.
=================================================================================
"""

import os
from datetime import timedelta, date

import pandas as pd
import numpy as np

import orb_backtester as bt   # <-- your unmodified strategy engine, reused as-is

# =================================================================================
# CONFIG
# =================================================================================

# Symbols you picked from output/ranked_results.csv (must match the `symbol`
# column in your instrument CSV -- bt.INSTRUMENT_CSV_PATH).
SELECTED_SYMBOLS = [
"CUPID","TCC","KABRAEXTRU","GVPIL","DEEPINDS" #  ,"DUCON"
    
]

# Same screening window logic as orb_backtester.main() -- reused, not changed.
BACKTEST_END_DATE = bt.BACKTEST_END_DATE
BACKTEST_LOOKBACK_DAYS = bt.BACKTEST_LOOKBACK_DAYS

# Assumed flat position size (see note above) used ONLY to turn the engine's
# %-return trades into rupee P&L for the notebook. Does not affect win rate,
# R-multiples, or any pass/fail ratio -- only the absolute rupee figures.
POSITION_SIZE_QTY = 6

# Starting equity for the reconstructed equity curve.
STARTING_CAPITAL = 5000.0

OUTPUT_DIR = bt.OUTPUT_DIR
os.makedirs(OUTPUT_DIR, exist_ok=True)


# =================================================================================
# INSTRUMENTED COPY OF simulate_orb_day -- decision logic untouched, see docstring
# =================================================================================

def simulate_orb_day_logged(day_candles, prior_daily_bars):
    out = {
        "breakout_occurred": False, "trade_taken": False, "direction": None,
        "entry_price": None, "exit_price": None, "sl": None, "target": None,
        "pnl_pct": None, "exit_reason": None, "adx_at_entry": None,
        # >>> ADDED: extra descriptive / timestamp fields (no effect on the
        # trading logic below -- purely recorded for the trade log)
        "entry_time": None, "exit_time": None,
        "or_high": None, "or_low": None, "gap_pct": None,
    }

    if len(day_candles) < 2:
        return out

    day_candles = day_candles.sort_values("timestamp").reset_index(drop=True)
    first = day_candles.iloc[0]
    high1, low1, open1 = first["high"], first["low"], first["open"]
    if open1 == 0 or pd.isna(open1):
        return out

    range1 = high1 - low1
    big_candle = (range1 / open1 * 100.0) > bt.BIG_CANDLE_THRESHOLD_PCT

    # >>> ADDED: opening-range levels + gap vs. prior day's close (informational
    # only -- the strategy itself never uses gap_pct)
    out["or_high"] = high1
    out["or_low"] = low1
    if prior_daily_bars is not None and len(prior_daily_bars):
        prev_close = prior_daily_bars.iloc[-1]["close"]
        if prev_close:
            out["gap_pct"] = (open1 - prev_close) / prev_close * 100.0

    square_off_dt = pd.Timestamp.combine(
        day_candles["timestamp"].iloc[0].date(),
        pd.to_datetime(bt.SQUARE_OFF_TIME).time()
    ).tz_localize(bt.IST)

    entry_idx = None
    direction = None
    for i in range(1, len(day_candles)):
        c = day_candles.iloc[i]
        broke_high = c["high"] > high1
        broke_low = c["low"] < low1
        if broke_high and broke_low:
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
        return out

    out["breakout_occurred"] = True
    out["direction"] = direction
    entry_price = high1 if direction == "LONG" else low1
    out["entry_time"] = day_candles.iloc[entry_idx]["timestamp"]   # >>> ADDED

    today_so_far = day_candles.iloc[:entry_idx + 1]
    today_partial_bar = {
        "date": today_so_far["timestamp"].iloc[0].date(),
        "open": today_so_far["open"].iloc[0],
        "high": today_so_far["high"].max(),
        "low": today_so_far["low"].min(),
        "close": today_so_far["close"].iloc[-1],
    }
    adx_val = bt.live_adx_at_entry(prior_daily_bars, today_partial_bar)
    out["adx_at_entry"] = adx_val

    if pd.isna(adx_val) or adx_val <= bt.ADX_THRESHOLD:
        out["exit_reason"] = "ADX_FILTER_BLOCKED"
        return out

    if big_candle:
        sl_distance = bt.ATR_SL_MULTIPLIER * range1
        sl = entry_price - sl_distance if direction == "LONG" else entry_price + sl_distance
    else:
        sl = low1 if direction == "LONG" else high1
        sl_distance = abs(entry_price - sl)

    if sl_distance <= 0:
        out["exit_reason"] = "INVALID_SL"
        return out

    target = (entry_price + bt.RISK_REWARD * sl_distance if direction == "LONG"
              else entry_price - bt.RISK_REWARD * sl_distance)

    out["trade_taken"] = True
    out["entry_price"] = entry_price
    out["sl"] = sl
    out["target"] = target

    trail_step = entry_price * (bt.TRAIL_STEP_PCT / 100.0)
    trail_ref = entry_price
    current_sl = sl

    exit_price = None
    exit_reason = None
    exit_time = None   # >>> ADDED

    for i in range(entry_idx, len(day_candles)):
        c = day_candles.iloc[i]

        if c["timestamp"] >= square_off_dt:
            exit_price = c["close"]
            exit_reason = "SQUARE_OFF_EOD"
            exit_time = c["timestamp"]   # >>> ADDED
            break

        if direction == "LONG":
            while c["high"] >= trail_ref + trail_step:
                trail_ref += trail_step
                current_sl += trail_step
            if c["low"] <= current_sl:
                exit_price = current_sl
                exit_reason = "STOP_LOSS" if current_sl <= sl else "TRAILING_SL"
                exit_time = c["timestamp"]   # >>> ADDED
                break
            if c["high"] >= target:
                exit_price = target
                exit_reason = "TARGET"
                exit_time = c["timestamp"]   # >>> ADDED
                break
        else:  # SHORT
            while c["low"] <= trail_ref - trail_step:
                trail_ref -= trail_step
                current_sl -= trail_step
            if c["high"] >= current_sl:
                exit_price = current_sl
                exit_reason = "STOP_LOSS" if current_sl >= sl else "TRAILING_SL"
                exit_time = c["timestamp"]   # >>> ADDED
                break
            if c["low"] <= target:
                exit_price = target
                exit_reason = "TARGET"
                exit_time = c["timestamp"]   # >>> ADDED
                break

    if exit_price is None:
        exit_price = day_candles.iloc[-1]["close"]
        exit_reason = "EOD_LAST_CANDLE"
        exit_time = day_candles.iloc[-1]["timestamp"]   # >>> ADDED

    if direction == "LONG":
        gross_pct = (exit_price - entry_price) / entry_price * 100.0
    else:
        gross_pct = (entry_price - exit_price) / entry_price * 100.0
    net_pct = gross_pct - bt.ENTRY_COST_PCT

    out["exit_price"] = exit_price
    out["exit_reason"] = exit_reason
    out["exit_time"] = exit_time   # >>> ADDED
    out["pnl_pct"] = net_pct
    out["gross_pct"] = gross_pct   # >>> ADDED (net_pct - gross_pct = ENTRY_COST_PCT, unchanged math)
    return out


# =================================================================================
# PER-INSTRUMENT RUN (mirrors bt.backtest_instrument, but keeps the extra fields)
# =================================================================================

def run_instrument(symbol, instrument_key, window_start, window_end):
    fetch_start = window_start - timedelta(days=bt.ADX_WARMUP_CALENDAR_DAYS)
    df_15min = bt.fetch_15min_candles(symbol, instrument_key, fetch_start, window_end)

    if df_15min.empty:
        print(f"  [warn] {symbol}: no candle data returned -- skipping")
        return []

    daily_bars_all = bt.build_daily_bars(df_15min)
    df_15min["date"] = df_15min["timestamp"].dt.date

    all_dates = sorted(d for d in df_15min["date"].unique() if window_start <= d <= window_end)

    rows = []
    for d in all_dates:
        day_candles = df_15min[df_15min["date"] == d]
        if len(day_candles) < 2:
            continue

        prior_daily_bars = daily_bars_all[daily_bars_all["date"] < d].reset_index(drop=True)
        result = simulate_orb_day_logged(day_candles, prior_daily_bars)

        if not result["trade_taken"]:
            continue

        qty = POSITION_SIZE_QTY
        entry_price = result["entry_price"]
        gross_pnl = result["gross_pct"] / 100.0 * entry_price * qty
        costs = bt.ENTRY_COST_PCT / 100.0 * entry_price * qty
        net_pnl = gross_pnl - costs

        rows.append({
            "symbol": symbol,
            "date": d,
            "entry_time": result["entry_time"],
            "exit_time": result["exit_time"],
            "entry_price": entry_price,
            "exit_price": result["exit_price"],
            "stop_loss": result["sl"],
            "target": result["target"],
            "exit_reason": result["exit_reason"],
            "qty": qty,
            "gross_pnl": round(gross_pnl, 4),
            "costs": round(costs, 4),
            "net_pnl": round(net_pnl, 4),
            "adx_at_entry": result["adx_at_entry"],
            "or_high": result["or_high"],
            "or_low": result["or_low"],
            "gap": result["gap_pct"],
        })

    return rows


# =================================================================================
# MAIN
# =================================================================================

def main():
    if not SELECTED_SYMBOLS:
        raise RuntimeError(
            "SELECTED_SYMBOLS is empty. Paste the symbols you picked from "
            "output/ranked_results.csv into SELECTED_SYMBOLS at the top of "
            "this script before running."
        )

    instruments = pd.read_csv(bt.INSTRUMENT_CSV_PATH)
    required_cols = {"symbol", "instrument_key"}
    if not required_cols.issubset(instruments.columns):
        raise ValueError(f"CSV must contain columns {required_cols}. Found: {list(instruments.columns)}")

    lookup = dict(zip(instruments["symbol"], instruments["instrument_key"]))
    missing = [s for s in SELECTED_SYMBOLS if s not in lookup]
    if missing:
        raise ValueError(f"These symbols aren't in {bt.INSTRUMENT_CSV_PATH}: {missing}")

    end_date = BACKTEST_END_DATE or date.today()
    start_date = end_date - timedelta(days=BACKTEST_LOOKBACK_DAYS)

    print(f"Re-running {len(SELECTED_SYMBOLS)} selected instrument(s) from {start_date} to {end_date} ...\n")

    all_rows = []
    for symbol in SELECTED_SYMBOLS:
        instrument_key = lookup[symbol]
        print(f"  {symbol} ({instrument_key}) ...")
        all_rows.extend(run_instrument(symbol, instrument_key, start_date, end_date))

    if not all_rows:
        print("No trades produced for the selected symbols/date range -- nothing to write.")
        return

    trade_log = pd.DataFrame(all_rows)

    # Chronological order (by exit, since that's when P&L is realized) for the equity curve
    trade_log = trade_log.sort_values(["exit_time", "entry_time"]).reset_index(drop=True)
    trade_log["cumulative_pnl"] = trade_log["net_pnl"].cumsum()
    trade_log["equity"] = STARTING_CAPITAL + trade_log["cumulative_pnl"]

    trade_log_path = os.path.join(OUTPUT_DIR, "trade_log.csv")
    trade_log.to_csv(trade_log_path, index=False)

    # Standalone 2-column equity curve (timestamp, equity), with a starting-capital
    # row before the first trade so the curve begins at STARTING_CAPITAL.
    eq_rows = [{"timestamp": trade_log["entry_time"].min(), "equity": STARTING_CAPITAL}]
    eq_rows += [{"timestamp": r["exit_time"], "equity": r["equity"]} for _, r in trade_log.iterrows()]
    equity_curve = pd.DataFrame(eq_rows)
    equity_curve_path = os.path.join(OUTPUT_DIR, "equity_curve.csv")
    equity_curve.to_csv(equity_curve_path, index=False)

    print(f"\n{len(trade_log)} trades across {trade_log['symbol'].nunique()} symbol(s).")
    print(f"Saved: {trade_log_path}")
    print(f"Saved: {equity_curve_path}")
    print("\nPoint the notebook at these two files (TRADE_LOG_PATH / EQUITY_CURVE_PATH) -- "
          "the COLUMN_MAP in the notebook you shared already matches these column names "
          "(net_pnl, qty, stop_loss, target, gross_pnl, costs, or_high, or_low, gap).")


if __name__ == "__main__":
    main()
