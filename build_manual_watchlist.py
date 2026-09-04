"""
=================================================================================
 BUILD MANUAL WATCHLIST  --  filtered_stocks.csv for a fixed symbol list
=================================================================================

WHY THIS EXISTS
    orb_trading_engine.py's fallback symbol list (CUPID, TCC, KABRAEXTRU,
    GVPIL, DEEPINDS) needs an instrument_key for each symbol, looked up from
    filtered_stocks_v2.csv / filtered_stocks.csv / output/final_shortlist.csv.
    Those files only get created by running fast_stock_filter.py or
    ranking_pipeline.py -- and fast_stock_filter.py only WRITES a symbol if it
    passes the liquidity/ATR%/price-band screen. If you want these 5 symbols
    available regardless of whether they'd pass that screen, you need a
    standalone lookup file.

    This script does exactly that: it downloads Upstox's instrument master
    (same file fast_stock_filter.py uses), looks up instrument_key for just
    the symbols in WATCHLIST below, and writes filtered_stocks.csv --
    which sits second in orb_trading_engine.py's INSTRUMENT_LOOKUP_CSVS list,
    so it's picked up automatically without touching orb_trading_engine.py.

    No screening, no filters, no API rate limits involved -- this only reads
    the free, public instrument master file, not the historical-candle API,
    so it needs no access token.

HOW TO USE
    1. Edit WATCHLIST below if you want different/more symbols.
    2. python3 build_manual_watchlist.py
    3. Output -> filtered_stocks.csv (symbol,instrument_key columns)
    4. Re-run orb_trading_engine.py -- it will now resolve these symbols.
=================================================================================
"""

import io
import gzip
import json

import requests
import pandas as pd

WATCHLIST = ["CUPID", "TCC", "KABRAEXTRU", "GVPIL", "DEEPINDS"]
SEGMENT_FILTER = "NSE_EQ"
OUTPUT_CSV = "filtered_stocks.csv"

INSTRUMENT_MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"


def main():
    print("Downloading instrument master (public file, no token needed)...")
    resp = requests.get(INSTRUMENT_MASTER_URL, timeout=60)
    resp.raise_for_status()

    with gzip.GzipFile(fileobj=io.BytesIO(resp.content)) as f:
        data = json.load(f)

    df = pd.DataFrame(data)
    df = df[df["segment"] == SEGMENT_FILTER]
    if "instrument_type" in df.columns:
        df = df[df["instrument_type"] == "EQ"]

    df = df[["instrument_key", "trading_symbol", "name"]].rename(
        columns={"trading_symbol": "symbol"}
    )

    found = df[df["symbol"].isin(WATCHLIST)][["symbol", "instrument_key", "name"]].copy()
    found_symbols = set(found["symbol"])
    missing = [s for s in WATCHLIST if s not in found_symbols]

    if missing:
        print(f"[warn] Could not find these symbols in the instrument master: {missing}")
        print("       Double-check the exact trading_symbol spelling on Upstox/NSE "
              "(e.g. suffixes, case) and edit WATCHLIST if needed.")

    if found.empty:
        raise SystemExit("No symbols resolved -- nothing to write.")

    found = found.sort_values("symbol").reset_index(drop=True)
    found[["symbol", "instrument_key"]].to_csv(OUTPUT_CSV, index=False)

    print(f"\nResolved {len(found)}/{len(WATCHLIST)} symbol(s):")
    print(found.to_string(index=False))
    print(f"\nSaved: {OUTPUT_CSV}")
    print("orb_trading_engine.py will now find these via its fallback symbol list.")


if __name__ == "__main__":
    main()
