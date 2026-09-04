"""
=================================================================================
 ORB TRADING ENGINE  --  same strategy, same rules, two modes, one script
=================================================================================

STRATEGY / RULES  -- UNCHANGED, reused as-is from orb_backtester.py
    [ORB breakout occurs] -> [ADX(14) > 25 at entry] -> trade
    Opening range = first 15-min candle (09:15-09:30). Whichever level
    (High1/Low1) breaks first triggers LONG/SHORT. SL = opposite ORB level,
    unless the opening candle is a "big candle" (range > 1.5% of open), in
    which case SL = entry +/- 2x that candle's range. Target = 1.5R. SL
    trails by 0.2% of entry price on every further 0.2% favourable move.
    Entry-side cost = 0.5%. Force square-off at 15:14 IST.
    -> ALL of this is imported unchanged from orb_backtester.py (constants:
       ADX_THRESHOLD, BIG_CANDLE_THRESHOLD_PCT, ATR_SL_MULTIPLIER,
       TRAIL_STEP_PCT, RISK_REWARD, ENTRY_COST_PCT, SQUARE_OFF_TIME) and the
       functions build_daily_bars / wilder_adx / live_adx_at_entry.
    -> Backtest mode calls orb_backtester.simulate_orb_day() directly, and
       run_selected_instruments.simulate_orb_day_logged() (already an
       additive-only, line-for-line copy your own codebase uses) for the
       extra descriptive fields in the trade log. Neither is modified here.
    -> Paper mode needed one genuinely NEW piece of plumbing: the batch
       version reads a whole day's candles at once, but live trading only
       ever has candles "so far". process_symbol_live() below re-expresses
       the exact same rules (same order, same thresholds, same formulas) so
       they fire candle-by-candle as each 15-min bar closes live, instead of
       over a completed day_candles dataframe. The RULES are identical; only
       the data-arrival mechanism differs, because it has to.

TWO MODES, chosen automatically by the clock (IST), or forced with --mode:
    PAPER MODE      08:00 - 15:14 IST on a weekday
                    Polls Upstox's intraday candle endpoint for each closed
                    15-min candle, simulates entries/exits against LIVE data
                    (no real orders are placed -- this is a paper/simulated
                    fill, tracked the same way the backtester tracks a trade).
                    Runs its full cycle for the day, force-squares off any
                    open position at 15:14, then exits the process.
    BACKTEST MODE   everything else (pre-market, after-hours, weekends)
                    Re-runs the unmodified backtest engine over the same
                    instrument set for the last N days.

INSTRUMENT SELECTION
    python3 orb_trading_engine.py RELIANCE TCS HDFCBANK
        -> works ONLY on exactly those symbols (resolved to instrument_key
           via filtered_stocks_v2.csv / filtered_stocks.csv / final_shortlist.csv,
           whichever has them -- if any symbol can't be resolved, this stops
           and tells you which one, rather than silently dropping it).
    python3 orb_trading_engine.py
        -> uses output/final_shortlist.csv, the top-5 shortlist already
           produced by ranking_pipeline.py.

OUTPUTS  (both modes write to the SAME two files, tagged by a "mode" column,
so paper history and backtest history coexist without overwriting each other)
    output/trade_log.csv      one row per CLOSED trade
    output/equity_curve.csv   one point per closed trade (running equity)

CRASH / RESTART SAFETY
    Paper mode persists per-symbol, per-day state to
    output/state/<symbol>_<date>.json after every processed candle (atomic
    write). If the process is killed and restarted the same trading day, it
    reloads that state and resumes exactly where it left off -- it will not
    re-enter a trade it already took, re-log a trade it already closed, or
    miss the exit of a position it already opened. trade_log.csv / 
    equity_curve.csv writes are idempotent (deduped on symbol+date+entry_time)
    for the same reason.

ERROR HANDLING
    Every network call and every per-symbol step is wrapped so one bad
    response / one bad symbol never kills the run. Failures are appended to
    output/error_log.txt with a timestamp and the loop continues.

LIMITATIONS (called out explicitly, not silently papered over)
    - "Paper trading" here means simulated fills against live Upstox market
      data -- it does NOT place real orders. If you want that, it's a
      separate, much higher-stakes piece of work.
    - Weekday check only; NSE holidays are NOT known to this script. Populate
      NSE_HOLIDAYS below (a set of date(YYYY,M,D)) if you want those excluded
      from auto-PAPER-mode too -- otherwise it will try paper mode on a
      holiday and simply see no candles until you stop it.
    - Upstox's intraday-candle endpoint path is assumed to be
      /v3/historical-candle/intraday/{instrument_key}/minutes/15, matching
      the v3 historical-candle path style already used in orb_backtester.py.
      Verify this against your current Upstox API docs before relying on it
      -- endpoints do change.
=================================================================================
"""

import argparse
import json
import os
import sys
import time
import traceback
from datetime import date, datetime, time as dtime, timedelta

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import orb_backtester as bt                 # unmodified strategy engine + config
import run_selected_instruments as rsi       # unmodified additive trade-log helper


# =================================================================================
# CONFIG (additive only -- nothing here overrides an orb_backtester constant)
# =================================================================================

IST = bt.IST

PAPER_WINDOW_START = dtime(8, 00)
PAPER_WINDOW_END = pd.to_datetime(bt.SQUARE_OFF_TIME).time()   # 15:14, reused from bt

POLL_INTERVAL_SEC = 20          # how often to check for a newly closed candle
POSITION_SIZE_QTY = 6           # same presentation-only rupee conversion as run_selected_instruments.py
STARTING_CAPITAL = 2500.0

NSE_HOLIDAYS = set()            # e.g. {date(2026, 10, 21), ...} -- populate if you want it honoured

STATE_DIR = os.path.join(bt.OUTPUT_DIR, "state")
TRADE_LOG_PATH = os.path.join(bt.OUTPUT_DIR, "trade_log.csv")
EQUITY_CURVE_PATH = os.path.join(bt.OUTPUT_DIR, "equity_curve.csv")
ERROR_LOG_PATH = os.path.join(bt.OUTPUT_DIR, "error_log.txt")

INSTRUMENT_LOOKUP_CSVS = [
    "filtered_stocks_v2.csv",                              # fast_stock_filter.py output
    "filtered_stocks.csv",                                 # stock_filter_upstox.py output / orb_backtester default
    os.path.join(bt.OUTPUT_DIR, "final_shortlist.csv"),    # ranking_pipeline.py output
]
DEFAULT_SHORTLIST_CSV = os.path.join(bt.OUTPUT_DIR, "final_shortlist.csv")

TRADE_LOG_COLUMNS = [
    "mode", "symbol", "date", "direction",
    "entry_time", "exit_time", "entry_price", "exit_price",
    "stop_loss", "target", "exit_reason", "adx_at_entry",
    "or_high", "or_low", "gap_pct",
    "qty", "gross_pnl", "costs", "net_pnl",
    "cumulative_pnl", "equity",
]

# Upstox v3 intraday candle endpoint (same host/auth/style as bt.UPSTOX_BASE_URL,
# just the "in-progress trading day" variant -- see LIMITATIONS above).
INTRADAY_CANDLE_URL = "https://api.upstox.com/v3/historical-candle/intraday/{instrument_key}/minutes/15"


os.makedirs(STATE_DIR, exist_ok=True)
os.makedirs(bt.OUTPUT_DIR, exist_ok=True)


# =================================================================================
# ERROR LOGGING -- so one bad symbol/response never kills the run
# =================================================================================

def log_error(message):
    line = f"[{datetime.now(IST).isoformat()}] {message}"
    print(f"  [error] {message}")
    try:
        with open(ERROR_LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass  # even logging failing shouldn't crash the run


# =================================================================================
# INSTRUMENT SELECTION
# =================================================================================

def _build_symbol_lookup():
    lookup = {}
    for path in INSTRUMENT_LOOKUP_CSVS:
        if not os.path.exists(path):
            continue
        try:
            d = pd.read_csv(path)
        except Exception as e:
            log_error(f"couldn't read {path}: {e}")
            continue
        if {"symbol", "instrument_key"}.issubset(d.columns):
            for _, r in d.iterrows():
                lookup.setdefault(r["symbol"], r["instrument_key"])
    return lookup


def resolve_instruments(cli_symbols):
    """
    cli_symbols non-empty -> work ONLY on exactly those (error out naming any
    symbol that can't be resolved -- never silently drop one you asked for).
    cli_symbols empty     -> use output/final_shortlist.csv from ranking_pipeline.py.
    Returns a list of (symbol, instrument_key) tuples, or None if there are no
    CLI symbols AND no shortlist file yet -- in that case the caller (main())
    falls back to its own hardcoded default list instead of exiting.
    """
    lookup = _build_symbol_lookup()

    if cli_symbols:
        missing = [s for s in cli_symbols if s not in lookup]
        if missing:
            raise SystemExit(
                f"Can't resolve instrument_key for: {missing}. "
                f"They need to appear (with a symbol,instrument_key column) in one of: "
                f"{INSTRUMENT_LOOKUP_CSVS}. Run stock_filter_upstox.py / "
                f"fast_stock_filter.py first, or add them there manually."
            )
        return [(s, lookup[s]) for s in cli_symbols]

    if not os.path.exists(DEFAULT_SHORTLIST_CSV):
        # No CLI symbols and no shortlist file -- don't exit here. Return None
        # so main() can fall back to its own hardcoded default symbol list
        # (resolved through this same function) instead of the process dying.
        print(
            f"[info] No symbols given on the command line and {DEFAULT_SHORTLIST_CSV} "
            f"doesn't exist yet -- falling back to the hardcoded default symbol list "
            f"in main(). Run ranking_pipeline.py to generate a real shortlist, or pass "
            f"symbols explicitly: python3 {os.path.basename(__file__)} SYMBOL1 SYMBOL2 ..."
        )
        return None

    d = pd.read_csv(DEFAULT_SHORTLIST_CSV)
    if not {"symbol", "instrument_key"}.issubset(d.columns):
        raise SystemExit(f"{DEFAULT_SHORTLIST_CSV} is missing symbol/instrument_key columns.")
    return list(zip(d["symbol"], d["instrument_key"]))


# =================================================================================
# MODE SELECTION
# =================================================================================

def determine_mode(now=None):
    now = now or datetime.now(IST)
    if now.weekday() >= 5 or now.date() in NSE_HOLIDAYS:
        return "BACKTEST"
    if PAPER_WINDOW_START <= now.time() <= PAPER_WINDOW_END:
        return "PAPER"
    return "BACKTEST"


# =================================================================================
# TRADE LOG + EQUITY CURVE  (idempotent, resume-safe, mode-tagged)
# =================================================================================

def _ensure_trade_log():
    if not os.path.exists(TRADE_LOG_PATH):
        pd.DataFrame(columns=TRADE_LOG_COLUMNS).to_csv(TRADE_LOG_PATH, index=False)


def _ensure_equity_curve():
    if not os.path.exists(EQUITY_CURVE_PATH):
        seed = pd.DataFrame([{
            "timestamp": datetime.now(IST).isoformat(),
            "equity": STARTING_CAPITAL,
        }])
        seed.to_csv(EQUITY_CURVE_PATH, index=False)


def current_equity():
    _ensure_equity_curve()
    df = pd.read_csv(EQUITY_CURVE_PATH)
    if df.empty:
        return STARTING_CAPITAL
    return float(df["equity"].iloc[-1])


def record_closed_trade(trade):
    """
    Append one closed trade to trade_log.csv and one point to equity_curve.csv.
    Deduped on (symbol, date, entry_time) so calling this twice for the same
    trade (e.g. after a restart) is always safe.
    """
    _ensure_trade_log()
    log_df = pd.read_csv(TRADE_LOG_PATH)
    if not log_df.empty:
        dup = log_df[
            (log_df["symbol"] == trade["symbol"])
            & (log_df["date"] == trade["date"])
            & (log_df["entry_time"] == trade["entry_time"])
        ]
        if not dup.empty:
            return  # already recorded

    equity_before = current_equity()
    equity_after = equity_before + trade["net_pnl"]
    trade["cumulative_pnl"] = round(equity_after - STARTING_CAPITAL, 4)
    trade["equity"] = round(equity_after, 4)

    row = {c: trade.get(c) for c in TRADE_LOG_COLUMNS}
    log_df = pd.concat([log_df, pd.DataFrame([row])], ignore_index=True)
    log_df.to_csv(TRADE_LOG_PATH, index=False)

    eq_df = pd.read_csv(EQUITY_CURVE_PATH)
    eq_df = pd.concat(
        [eq_df, pd.DataFrame([{"timestamp": trade["exit_time"], "equity": trade["equity"]}])],
        ignore_index=True,
    )
    eq_df.to_csv(EQUITY_CURVE_PATH, index=False)

    print(f"  [{trade['mode']}] {trade['symbol']} {trade['direction']} closed "
          f"{trade['exit_reason']} net_pnl={trade['net_pnl']:.2f} equity={trade['equity']:.2f}")


# =================================================================================
# PER-SYMBOL, PER-DAY STATE  (crash/restart safety for paper mode)
# =================================================================================

def _state_path(symbol, trading_day):
    return os.path.join(STATE_DIR, f"{symbol}_{trading_day.isoformat()}.json")


def load_state(symbol, trading_day):
    p = _state_path(symbol, trading_day)
    if os.path.exists(p):
        try:
            with open(p) as f:
                return json.load(f)
        except Exception as e:
            log_error(f"corrupt state file {p} ({e}) -- starting fresh for {symbol}")
    return {
        "symbol": symbol,
        "date": trading_day.isoformat(),
        "phase": "AWAITING_OPENING_RANGE",
        # AWAITING_OPENING_RANGE -> AWAITING_BREAKOUT -> IN_TRADE -> DONE
        "open1": None, "high1": None, "low1": None, "big_candle": None,
        "gap_pct": None,
        "last_processed_ts": None,
        "position": None,
        "blocked_reason": None,
    }


def save_state(symbol, trading_day, state):
    p = _state_path(symbol, trading_day)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, default=str)
    os.replace(tmp, p)   # atomic on POSIX -- crash mid-write can't corrupt the real file


# =================================================================================
# LIVE INTRADAY CANDLES  (new plumbing -- see docstring: data source differs,
# the decision rules applied to it below do not)
# =================================================================================

def fetch_intraday_15min_candles(instrument_key):
    headers = {"Accept": "application/json", "Authorization": f"Bearer {bt.UPSTOX_ACCESS_TOKEN}"}
    url = INTRADAY_CANDLE_URL.format(instrument_key=instrument_key)
    empty = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])

    for attempt in range(1, bt.API_MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=15)
        except requests.RequestException as e:
            log_error(f"network error fetching intraday candles for {instrument_key}: {e}")
            time.sleep(2 * attempt)
            continue

        if resp.status_code == 200:
            try:
                candles = resp.json().get("data", {}).get("candles", [])
            except Exception as e:
                log_error(f"bad JSON from intraday candles for {instrument_key}: {e}")
                return empty
            if not candles:
                return empty
            df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
            df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_convert(IST)
            return df.sort_values("timestamp").drop_duplicates(subset="timestamp").reset_index(drop=True)
        elif resp.status_code == 429:
            time.sleep(2 * attempt)
            continue
        elif resp.status_code == 401:
            raise RuntimeError(
                "Upstox 401 Unauthorized -- refresh UPSTOX_ACCESS_TOKEN in orb_backtester.py "
                "(both backtest and paper mode share it)."
            )
        else:
            log_error(f"HTTP {resp.status_code} for {instrument_key}: {resp.text[:200]}")
            time.sleep(1)

    return empty


def get_closed_candles(instrument_key, now):
    """Only candles that have FULLY closed by `now` -- never act on a still-forming bar."""
    df = fetch_intraday_15min_candles(instrument_key)
    if df.empty:
        return df
    closed_mask = df["timestamp"] + pd.Timedelta(minutes=15) <= now
    return df.loc[closed_mask].reset_index(drop=True)


def get_prior_daily_bars(symbol, instrument_key, trading_day):
    """Complete daily bars strictly before trading_day -- unchanged historical fetch."""
    fetch_start = trading_day - timedelta(days=bt.ADX_WARMUP_CALENDAR_DAYS)
    fetch_end = trading_day - timedelta(days=1)
    hist = bt.fetch_15min_candles(symbol, instrument_key, fetch_start, fetch_end)
    return bt.build_daily_bars(hist)


# =================================================================================
# INCREMENTAL (LIVE) VERSION OF THE SAME RULES  --  see module docstring
# =================================================================================

def _check_exit_on_candle(pos, c, window_end_time):
    """
    Exactly the exit checks inside orb_backtester.simulate_orb_day's per-candle
    loop: square-off first, then trailing ratchet, then SL, then target (SL
    checked before target within the same candle -- conservative, per the
    original's assumption #3). Returns (exit_price, exit_reason) or (None, None).
    """
    if c["timestamp"].time() >= window_end_time:
        return float(c["close"]), "SQUARE_OFF_EOD"

    direction = pos["direction"]
    if direction == "LONG":
        while c["high"] >= pos["trail_ref"] + pos["trail_step"]:
            pos["trail_ref"] += pos["trail_step"]
            pos["current_sl"] += pos["trail_step"]
        if c["low"] <= pos["current_sl"]:
            reason = "STOP_LOSS" if pos["current_sl"] <= pos["sl"] else "TRAILING_SL"
            return pos["current_sl"], reason
        if c["high"] >= pos["target"]:
            return pos["target"], "TARGET"
    else:  # SHORT
        while c["low"] <= pos["trail_ref"] - pos["trail_step"]:
            pos["trail_ref"] -= pos["trail_step"]
            pos["current_sl"] -= pos["trail_step"]
        if c["high"] >= pos["current_sl"]:
            reason = "STOP_LOSS" if pos["current_sl"] >= pos["sl"] else "TRAILING_SL"
            return pos["current_sl"], reason
        if c["low"] <= pos["target"]:
            return pos["target"], "TARGET"

    return None, None


def process_symbol_live(symbol, instrument_key, trading_day, prior_daily_bars, state, now):
    """
    Advance `state` using any newly closed candles, applying the identical
    rule set as orb_backtester.simulate_orb_day. Mutates `state` in place and
    returns a completed-trade dict if one closed on this call, else None.
    """
    candles = get_closed_candles(instrument_key, now)
    if candles.empty:
        return None

    last_ts = pd.to_datetime(state["last_processed_ts"]) if state["last_processed_ts"] else None
    new_candles = candles if last_ts is None else candles[candles["timestamp"] > last_ts]
    if new_candles.empty:
        return None

    closed_trade = None

    for _, c in new_candles.iterrows():
        if state["phase"] == "DONE":
            break

        if state["phase"] == "AWAITING_OPENING_RANGE":
            state["open1"] = float(c["open"])
            state["high1"] = float(c["high"])
            state["low1"] = float(c["low"])
            range1 = state["high1"] - state["low1"]
            state["big_candle"] = bool(
                state["open1"] != 0 and (range1 / state["open1"] * 100.0) > bt.BIG_CANDLE_THRESHOLD_PCT
            )
            if not prior_daily_bars.empty:
                prev_close = float(prior_daily_bars.iloc[-1]["close"])
                if prev_close:
                    state["gap_pct"] = (state["open1"] - prev_close) / prev_close * 100.0
            state["phase"] = "AWAITING_BREAKOUT"
            state["last_processed_ts"] = str(c["timestamp"])
            continue

        if state["phase"] == "AWAITING_BREAKOUT":
            high1, low1 = state["high1"], state["low1"]
            broke_high = c["high"] > high1
            broke_low = c["low"] < low1

            direction = None
            if broke_high and broke_low:
                direction = "LONG" if c["close"] >= c["open"] else "SHORT"
            elif broke_high:
                direction = "LONG"
            elif broke_low:
                direction = "SHORT"

            state["last_processed_ts"] = str(c["timestamp"])

            if direction is None:
                if c["timestamp"].time() >= PAPER_WINDOW_END:
                    state["phase"] = "DONE"   # sideways day, no breakout -- matches batch outcome
                continue

            entry_price = high1 if direction == "LONG" else low1
            today_partial_bar = {
                "date": trading_day,
                "open": state["open1"],
                "high": max(state["high1"], float(c["high"])),
                "low": min(state["low1"], float(c["low"])),
                "close": float(c["close"]),
            }
            adx_val = bt.live_adx_at_entry(prior_daily_bars, today_partial_bar)

            if pd.isna(adx_val) or adx_val <= bt.ADX_THRESHOLD:
                state["phase"] = "DONE"
                state["blocked_reason"] = "ADX_FILTER_BLOCKED"
                continue

            range1 = high1 - low1
            if state["big_candle"]:
                sl_distance = bt.ATR_SL_MULTIPLIER * range1
                sl = entry_price - sl_distance if direction == "LONG" else entry_price + sl_distance
            else:
                sl = low1 if direction == "LONG" else high1
                sl_distance = abs(entry_price - sl)

            if sl_distance <= 0:
                state["phase"] = "DONE"
                state["blocked_reason"] = "INVALID_SL"
                continue

            target = (entry_price + bt.RISK_REWARD * sl_distance if direction == "LONG"
                      else entry_price - bt.RISK_REWARD * sl_distance)

            pos = {
                "direction": direction,
                "entry_price": entry_price,
                "sl": sl,
                "target": target,
                "adx_at_entry": float(adx_val),
                "trail_step": entry_price * (bt.TRAIL_STEP_PCT / 100.0),
                "trail_ref": entry_price,
                "current_sl": sl,
                "entry_time": str(c["timestamp"]),
                "or_high": high1,
                "or_low": low1,
            }
            state["position"] = pos
            state["phase"] = "IN_TRADE"

            # Same candle that triggered entry can also already hit SL/target
            # within its own range -- the batch loop starts at entry_idx too.
            exit_price, exit_reason = _check_exit_on_candle(pos, c, PAPER_WINDOW_END)
            if exit_price is not None:
                closed_trade = _finalize_trade(symbol, trading_day, state, pos, exit_price, exit_reason, c)
            continue

        if state["phase"] == "IN_TRADE":
            pos = state["position"]
            state["last_processed_ts"] = str(c["timestamp"])
            exit_price, exit_reason = _check_exit_on_candle(pos, c, PAPER_WINDOW_END)
            if exit_price is not None:
                closed_trade = _finalize_trade(symbol, trading_day, state, pos, exit_price, exit_reason, c)
            continue

    return closed_trade


def _finalize_trade(symbol, trading_day, state, pos, exit_price, exit_reason, exit_candle):
    direction = pos["direction"]
    if direction == "LONG":
        gross_pct = (exit_price - pos["entry_price"]) / pos["entry_price"] * 100.0
    else:
        gross_pct = (pos["entry_price"] - exit_price) / pos["entry_price"] * 100.0
    net_pct = gross_pct - bt.ENTRY_COST_PCT

    qty = POSITION_SIZE_QTY
    gross_pnl = gross_pct / 100.0 * pos["entry_price"] * qty
    costs = bt.ENTRY_COST_PCT / 100.0 * pos["entry_price"] * qty
    net_pnl = gross_pnl - costs

    trade = {
        "mode": "PAPER",
        "symbol": symbol,
        "date": str(trading_day),
        "direction": direction,
        "entry_time": pos["entry_time"],
        "exit_time": str(exit_candle["timestamp"]),
        "entry_price": pos["entry_price"],
        "exit_price": exit_price,
        "stop_loss": pos["sl"],
        "target": pos["target"],
        "exit_reason": exit_reason,
        "adx_at_entry": pos["adx_at_entry"],
        "or_high": pos["or_high"],
        "or_low": pos["or_low"],
        "gap_pct": state.get("gap_pct"),
        "qty": qty,
        "gross_pnl": round(gross_pnl, 4),
        "costs": round(costs, 4),
        "net_pnl": round(net_pnl, 4),
    }
    state["phase"] = "DONE"
    state["position"] = None
    record_closed_trade(trade)
    return trade


# =================================================================================
# PAPER TRADING MODE  -- runs one full day's cycle, then exits
# =================================================================================

def run_paper_mode(instruments):
    trading_day = datetime.now(IST).date()
    print("=" * 80)
    print(f"PAPER MODE -- {trading_day} -- {len(instruments)} symbol(s) -- "
          f"window {PAPER_WINDOW_START}-{PAPER_WINDOW_END} IST")
    print("=" * 80)

    prior_bars_cache = {}
    for symbol, instrument_key in instruments:
        try:
            prior_bars_cache[symbol] = get_prior_daily_bars(symbol, instrument_key, trading_day)
        except Exception as e:
            log_error(f"{symbol}: failed to fetch prior daily bars ({e}) -- will retry each poll")
            prior_bars_cache[symbol] = pd.DataFrame(columns=["date", "open", "high", "low", "close"])

    states = {symbol: load_state(symbol, trading_day) for symbol, _ in instruments}

    print(f"Resumed state -- phases: "
          f"{ {s: st['phase'] for s, st in states.items()} }")

    while True:
        now = datetime.now(IST)

        if now.time() > PAPER_WINDOW_END:
            print(f"\n{PAPER_WINDOW_END} reached -- ending paper session for {trading_day}.")
            break

        for symbol, instrument_key in instruments:
            state = states[symbol]
            if state["phase"] == "DONE":
                continue
            try:
                if prior_bars_cache[symbol].empty:
                    prior_bars_cache[symbol] = get_prior_daily_bars(symbol, instrument_key, trading_day)
                process_symbol_live(
                    symbol, instrument_key, trading_day,
                    prior_bars_cache[symbol], state, now,
                )
            except Exception as e:
                log_error(f"{symbol}: {e}\n{traceback.format_exc(limit=2)}")
            finally:
                try:
                    save_state(symbol, trading_day, state)
                except Exception as e:
                    log_error(f"{symbol}: failed to persist state ({e})")

        if all(states[s]["phase"] == "DONE" for s, _ in instruments):
            print(f"\nAll {len(instruments)} symbol(s) finished their cycle for {trading_day} "
                  f"before window close -- ending session.")
            break

        time.sleep(POLL_INTERVAL_SEC)

    print(f"\nPaper session complete. See {TRADE_LOG_PATH} and {EQUITY_CURVE_PATH}.")


# =================================================================================
# BACKTEST MODE -- unmodified engine, same instrument set, mode-tagged log
# =================================================================================

def run_backtest_mode(instruments, lookback_days):
    end_date = date.today()
    start_date = end_date - timedelta(days=lookback_days)
    print("=" * 80)
    print(f"BACKTEST MODE -- {start_date} to {end_date} -- {len(instruments)} symbol(s)")
    print("=" * 80)

    for symbol, instrument_key in instruments:
        try:
            fetch_start = start_date - timedelta(days=bt.ADX_WARMUP_CALENDAR_DAYS)
            df_15min = bt.fetch_15min_candles(symbol, instrument_key, fetch_start, end_date)
            if df_15min.empty:
                log_error(f"{symbol}: no candle data returned -- skipping")
                continue

            daily_bars_all = bt.build_daily_bars(df_15min)
            df_15min["date"] = df_15min["timestamp"].dt.date
            all_dates = sorted(d for d in df_15min["date"].unique() if start_date <= d <= end_date)

            n_trades = 0
            for d in all_dates:
                day_candles = df_15min[df_15min["date"] == d]
                if len(day_candles) < 2:
                    continue
                prior_daily_bars = daily_bars_all[daily_bars_all["date"] < d].reset_index(drop=True)

                # Unmodified, additive-only instrumented copy already used by
                # run_selected_instruments.py -- same decision logic as
                # orb_backtester.simulate_orb_day(), plus entry/exit timestamps.
                result = rsi.simulate_orb_day_logged(day_candles, prior_daily_bars)
                if not result["trade_taken"]:
                    continue

                qty = POSITION_SIZE_QTY
                entry_price = result["entry_price"]
                gross_pnl = result["gross_pct"] / 100.0 * entry_price * qty
                costs = bt.ENTRY_COST_PCT / 100.0 * entry_price * qty
                net_pnl = gross_pnl - costs

                trade = {
                    "mode": "BACKTEST",
                    "symbol": symbol,
                    "date": str(d),
                    "direction": result["direction"],
                    "entry_time": str(result["entry_time"]),
                    "exit_time": str(result["exit_time"]),
                    "entry_price": entry_price,
                    "exit_price": result["exit_price"],
                    "stop_loss": result["sl"],
                    "target": result["target"],
                    "exit_reason": result["exit_reason"],
                    "adx_at_entry": result["adx_at_entry"],
                    "or_high": result["or_high"],
                    "or_low": result["or_low"],
                    "gap_pct": result["gap_pct"],
                    "qty": qty,
                    "gross_pnl": round(gross_pnl, 4),
                    "costs": round(costs, 4),
                    "net_pnl": round(net_pnl, 4),
                }
                record_closed_trade(trade)
                n_trades += 1

            print(f"  {symbol}: {n_trades} trade(s) over {len(all_dates)} trading day(s)")

        except Exception as e:
            log_error(f"{symbol}: {e}\n{traceback.format_exc(limit=2)}")
            continue

    print(f"\nBacktest complete. See {TRADE_LOG_PATH} and {EQUITY_CURVE_PATH}.")


# =================================================================================
# MAIN
# =================================================================================

def parse_args():
    parser = argparse.ArgumentParser(
        description="ORB+ADX strategy -- auto paper-trading (08:55-15:14 IST) / "
                     "backtesting (all other times) engine."
    )
    parser.add_argument("symbols", nargs="*",
                         help="Optional: exact symbols to trade/backtest. "
                              "If omitted, uses output/final_shortlist.csv.")
    parser.add_argument("--mode", choices=["auto", "paper", "backtest"], default="auto",
                         help="Override automatic time-based mode selection.")
    parser.add_argument("--lookback-days", type=int, default=bt.BACKTEST_LOOKBACK_DAYS,
                         help="Backtest mode only: how many calendar days back to test.")
    return parser.parse_args()


DEFAULT_FALLBACK_SYMBOLS = ["CUPID", "TCC", "KABRAEXTRU", "GVPIL", "DEEPINDS"]


def main():
    args = parse_args()
    instruments = resolve_instruments(args.symbols)
    if instruments is None:
        # No CLI symbols and no shortlist file -- fall back to the hardcoded
        # default symbols, but still resolve them to (symbol, instrument_key)
        # tuples via the same lookup logic as everywhere else, instead of
        # passing bare strings downstream (which would crash on the first
        # `for symbol, instrument_key in instruments` unpack).
        print(f"[info] Using hardcoded default symbols: {DEFAULT_FALLBACK_SYMBOLS}")
        instruments = resolve_instruments(DEFAULT_FALLBACK_SYMBOLS)

    mode = args.mode.upper() if args.mode != "auto" else determine_mode()

    print(f"Instruments: {[s for s, _ in instruments]}")
    print(f"Mode: {mode} (now={datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S %Z')})")

    try:
        if mode == "PAPER":
            run_paper_mode(instruments)
        else:
            run_backtest_mode(instruments, args.lookback_days)
    except KeyboardInterrupt:
        print("\nInterrupted -- state already persisted after each processed candle "
              "(paper mode) / each closed trade (backtest mode). Safe to restart.")
        sys.exit(1)
    except Exception as e:
        log_error(f"FATAL: {e}\n{traceback.format_exc()}")
        sys.exit(1)


if __name__ == "__main__":
    main()