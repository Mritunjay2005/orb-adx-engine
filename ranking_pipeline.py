"""
=================================================================================
 FULL PIPELINE  --  fast_stock_filter -> ORB+ADX backtest -> top-5 shortlist
=================================================================================

WHAT THIS DOES (end to end, one command)

  STEP 1  fast_stock_filter.screen_stocks()
          Liquidity + ATR% + price-band filter over the NSE universe.
          -> filtered_stocks_v2.csv   (unchanged, this is fast_stock_filter's
             own output file, reused as-is)

  STEP 2  orb_backtester.main()
          Runs the EXACT SAME unmodified strategy engine you already have:
              [ORB breakout occurs] -> [ADX(14) > 25 at entry] -> trade
          over every symbol from step 1, over the last BACKTEST_LOOKBACK_DAYS.
          -> output/ranked_results.csv  (orb_backtester's own output, unchanged)
             output/trades_<symbol>.csv (per-symbol trade logs, unchanged)

  STEP 3  Shortlist filter (this script, new)
          Reads output/ranked_results.csv and keeps only symbols that pass
          MIN_TRADES / MIN_WIN_RATE_PCT / MIN_NET_PNL_PCT, ranks the survivors,
          and takes the top TOP_N (default 5). Merges back in the fast_stock_filter
          columns (close, atr_pct, avg_volume_20d) so the final CSV is self-contained.
          -> output/final_shortlist.csv

NOTHING about the strategy logic, ADX threshold, ORB rules, cost model, or the
liquidity/ATR/price filter is touched -- both engines are imported and called
as-is. This file only wires them together and adds the final ranking filter.

HOW TO USE
  1. Put this file in the same folder as fast_stock_filter.py and
     orb_backtester.py (the folder you already have).
  2. Make sure both scripts have a valid, un-expired Upstox access token.
  3. Adjust STEP 3 thresholds below if you want a stricter/looser shortlist.
  4. Run:  python3 run_full_pipeline.py
  5. Final answer: output/final_shortlist.csv
=================================================================================
"""

import os
import sys
import pandas as pd

# Make sure we can import the two existing scripts from the same folder
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fast_stock_filter as fsf   # STEP 1 -- liquidity + ATR% + price band screen
import orb_backtester as bt       # STEP 2 -- ORB breakout + ADX(14)>25 backtest engine


# =================================================================================
# STEP 3 CONFIG -- the "win_rate / net_pnl / avg_pnl filter" -- EDIT THESE
# =================================================================================

MIN_TRADES = 5              # need at least this many trades to trust the stats
MIN_WIN_RATE_PCT = 50.0     # win_rate_pct must be >= this
MIN_NET_PNL_PCT = 0.0       # net_pnl_pct must be > this (i.e. net profitable)
MIN_AVG_PNL_PCT = 0.0       # avg_pnl_per_trade_pct must be > this
TOP_N = 5

FINAL_SHORTLIST_CSV = os.path.join(bt.OUTPUT_DIR, "final_shortlist.csv")


# =================================================================================
# STEP 1: fast_stock_filter -- liquidity + ATR% + price band
# =================================================================================

def run_step1_fast_filter():
    print("=" * 80)
    print("STEP 1/3: fast_stock_filter -- liquidity + ATR% + price band screen")
    print("=" * 80)
    candidates_df = fsf.screen_stocks()   # writes fsf.OUTPUT_CSV itself, unchanged
    if candidates_df.empty:
        raise SystemExit(
            "STEP 1 produced 0 candidates -- nothing to backtest. "
            "Loosen MIN_AVG_VOLUME / ATR_PCT_THRESHOLD / PRICE_MIN-MAX "
            "in fast_stock_filter.py and re-run."
        )
    print(f"\nSTEP 1 done: {len(candidates_df)} candidates -> {fsf.OUTPUT_CSV}\n")
    return candidates_df


# =================================================================================
# STEP 2: orb_backtester -- ORB breakout + ADX(14)>25 -- unmodified engine
# =================================================================================

def run_step2_orb_backtest():
    print("=" * 80)
    print("STEP 2/3: orb_backtester -- ORB breakout -> ADX(14)>25 -> trade")
    print("=" * 80)

    # Point the backtester at STEP 1's output instead of its default CSV.
    # (Same pattern run_selected_instruments.py / add_price_range_columns.py
    # already use: reuse orb_backtester's config, don't touch its logic.)
    bt.INSTRUMENT_CSV_PATH = fsf.OUTPUT_CSV

    bt.main()   # writes output/ranked_results.csv + output/trades_<symbol>.csv, unchanged

    ranked_path = os.path.join(bt.OUTPUT_DIR, "ranked_results.csv")
    if not os.path.exists(ranked_path):
        raise SystemExit(
            f"STEP 2 did not produce {ranked_path} -- check the errors printed "
            "above (likely an expired/invalid Upstox token, or 0 symbols with "
            "enough candle history)."
        )
    ranked_df = pd.read_csv(ranked_path)
    print(f"\nSTEP 2 done: {len(ranked_df)} symbols backtested -> {ranked_path}\n")
    return ranked_df


# =================================================================================
# STEP 3: win_rate / net_pnl / avg_pnl filter -> top-N shortlist
# =================================================================================

def run_step3_shortlist(ranked_df, candidates_df):
    print("=" * 80)
    print("STEP 3/3: win_rate / net_pnl / avg_pnl filter -> "
          f"top {TOP_N} shortlist")
    print("=" * 80)

    df = ranked_df.copy()

    # Only symbols that actually produced trades and pass every threshold.
    passed = df[
        (df["trades_taken"] >= MIN_TRADES)
        & (df["win_rate_pct"].fillna(0) >= MIN_WIN_RATE_PCT)
        & (df["net_pnl_pct"] > MIN_NET_PNL_PCT)
        & (df["avg_pnl_per_trade_pct"].fillna(0) > MIN_AVG_PNL_PCT)
    ].copy()

    print(f"{len(df)} symbols backtested -> {len(passed)} pass the shortlist filter "
          f"(trades>={MIN_TRADES}, win_rate>={MIN_WIN_RATE_PCT}%, "
          f"net_pnl>{MIN_NET_PNL_PCT}%, avg_pnl>{MIN_AVG_PNL_PCT}%)")

    if passed.empty:
        print("\n[!] Nobody passed the filter -- loosen MIN_TRADES / MIN_WIN_RATE_PCT / "
              "MIN_NET_PNL_PCT / MIN_AVG_PNL_PCT at the top of this script and re-run "
              "just run_step3_shortlist() (no need to re-hit the API).")
        passed.to_csv(FINAL_SHORTLIST_CSV, index=False)
        return passed

    # Rank survivors by net P&L first, avg P&L per trade as tiebreaker.
    passed = passed.sort_values(
        ["net_pnl_pct", "avg_pnl_per_trade_pct"], ascending=[False, False]
    ).reset_index(drop=True)

    shortlist = passed.head(TOP_N).copy()
    shortlist.insert(0, "shortlist_rank", shortlist.index + 1)

    # Merge back in the fast_stock_filter screening details (close, atr_pct,
    # avg_volume_20d) so the final CSV is self-contained -- one file, everything
    # a person needs to sanity-check the pick.
    extra_cols = ["symbol", "close", "atr", "atr_pct", "avg_volume_20d"]
    shortlist = shortlist.merge(
        candidates_df[extra_cols], on="symbol", how="left"
    )

    # Nice, deliberate column order.
    col_order = [
        "shortlist_rank", "symbol", "instrument_key",
        "close", "atr", "atr_pct", "avg_volume_20d",
        "trading_days", "breakout_days", "trades_taken", "wins", "losses",
        "win_rate_pct", "net_pnl_pct", "avg_pnl_per_trade_pct",
    ]
    col_order = [c for c in col_order if c in shortlist.columns]
    shortlist = shortlist[col_order]

    shortlist.to_csv(FINAL_SHORTLIST_CSV, index=False)

    print(f"\n=== FINAL TOP {len(shortlist)} SHORTLIST ===")
    print(shortlist.to_string(index=False))
    print(f"\nSaved: {FINAL_SHORTLIST_CSV}")
    return shortlist


# =================================================================================
# MAIN
# =================================================================================

def main():
    candidates_df = run_step1_fast_filter()
    ranked_df = run_step2_orb_backtest()
    run_step3_shortlist(ranked_df, candidates_df)


if __name__ == "__main__":
    main()
