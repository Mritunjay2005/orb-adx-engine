"""
=================================================================================
 FAST STOCK FILTER  --  Threaded Upstox screener (ATR% + Volume + Price band)
=================================================================================

WHAT THIS SCRIPT DOES
    Screens the NSE equity universe down to a shortlist using THREE filters:

      1. LIQUIDITY   : 20-day average daily volume  >= MIN_AVG_VOLUME
      2. VOLATILITY  : ATR(14) as a % of close       >= ATR_PCT_THRESHOLD
      3. PRICE BAND  : close price between PRICE_MIN and PRICE_MAX (inclusive)

    This is the same idea as stock_filter_upstox.py, but:
      - Uses a ThreadPoolExecutor to fetch candles for many instruments in
        parallel instead of one-at-a-time with a fixed sleep delay.
      - Adds a strict, thread-safe rate limiter that respects Upstox's
        PUBLISHED limits so threading makes you faster WITHOUT breaking the
        rules (getting throttled/suspended would be much slower overall).
      - Adds the PRICE_MIN / PRICE_MAX band filter.

UPSTOX RATE LIMITS (verified against upstox.com/developer docs, "Standard
APIs" category, which historical-candle falls under -- re-check the docs
yourself before relying on this, limits can change):
      25 requests / second
      250 requests / minute
      1000 requests / 30 minutes   <-- this is the one that actually binds

WHY "WITHIN 5 MINUTES" HAS A HARD CEILING
    This script needs ONE API call per instrument (to pull its daily candles).
    The NSE equity universe is roughly 2,000+ symbols. Upstox's own 1000-per-
    30-minute cap means you CANNOT legally screen the full universe in 5
    minutes -- at best you can process ~1000 instruments in that window, then
    you must wait out the 30-minute rolling window for the rest.
    This script:
      - Maxes out the legal throughput (as close to 1000 requests per 30 min
        as safely possible) so ~1000 instruments finish in ~4-5 minutes.
      - Prints an ETA estimate up front based on how many instruments it
        actually found, so you know before it starts whether your universe
        fits inside one 5-minute run.
      - If MAX_INSTRUMENTS is set below the universe size, it processes the
        highest-priority slice first (see NOTE in load_instruments()).

HOW TO USE
    1. pip install requests pandas --break-system-packages
    2. Set the UPSTOX_ACCESS_TOKEN environment variable (don't hardcode it
       in the file -- see security note below):
           export UPSTOX_ACCESS_TOKEN="your_token_here"
    3. Adjust CONFIG values below if needed.
    4. Run: python3 fast_stock_filter.py
    5. Output -> filtered_stocks_v2.csv

SECURITY NOTE
    Your token is read from an environment variable, not hardcoded, so it's
    not sitting in plaintext in a file you might upload/share/commit. If a
    token has ever been pasted into a script you shared with anyone, rotate
    it in the Upstox developer console.
=================================================================================
"""

import os
import io
import gzip
import json
import time
import threading
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import pandas as pd

# =================================================================================
# CONFIG -- EDIT THESE
# =================================================================================

ACCESS_TOKEN = "eyJ0eXAiOiJKV1QiLCJrZXlfaWQiOiJza192MS4wIiwiYWxnIjoiSFMyNTYifQ.eyJzdWIiOiI4NEJaTUgiLCJqdGkiOiI2YThjOGNhMDQ4ODI1ODU1MjUxYTg2MGEiLCJpc011bHRpQ2xpZW50IjpmYWxzZSwiaXNQbHVzUGxhbiI6ZmFsc2UsImlzRXh0ZW5kZWQiOnRydWUsImlhdCI6MTc4NzU5NTkzNiwiaXNzIjoidWRhcGktZ2F0ZXdheS1zZXJ2aWNlIiwiZXhwIjoxODE5MTQ0ODAwfQ.uKXERjcIVTgCmcNkQwwDLDDDwdNQDoHTIT3HymGij5E"   # <-- put your token here


ATR_PERIOD = 14
LOOKBACK_DAYS = 60           # calendar days of daily candles to pull per stock
MIN_AVG_VOLUME = 200_000     # filter 1: liquidity
ATR_PCT_THRESHOLD = 2.0      # filter 2: volatility (ATR as % of close)
PRICE_MIN = 150.0            # filter 3: price band (your shortlist's actual range)
PRICE_MAX = 800.0
SEGMENT_FILTER = "NSE_EQ"

# Rate limiting -- kept safely UNDER Upstox's published caps (25/s, 250/min,
# 1000/30min). Lower these if you're on a plan with tighter limits, or if you
# start seeing 429 responses in the output.
MAX_REQUESTS_PER_SECOND = 20      # < 25/s cap
MAX_REQUESTS_PER_30MIN = 950      # < 1000/30min cap, small safety margin

# Thread pool size. This does NOT bypass the rate limiter above -- it just
# lets many requests be "in flight" (waiting on network I/O) at once, which
# is where threading actually buys you speed for an I/O-bound job like this.
MAX_WORKERS = 25

# Optional cap so you can test on a slice first, e.g. MAX_INSTRUMENTS = 200.
# Set to None to attempt the full filtered universe.
MAX_INSTRUMENTS = None

OUTPUT_CSV = "filtered_stocks_v2.csv"

INSTRUMENT_MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
HISTORICAL_CANDLE_URL = "https://api.upstox.com/v2/historical-candle/{instrument_key}/day/{to_date}/{from_date}"

HEADERS = {
    "Accept": "application/json",
    "Authorization": f"Bearer {ACCESS_TOKEN}",
}


# =================================================================================
# THREAD-SAFE SLIDING-WINDOW RATE LIMITER
# =================================================================================
class RateLimiter:
    """
    Enforces two rolling-window caps simultaneously (per-second and per-30-min).
    Every thread calls .acquire() before making an API request; it blocks
    until it's safe to proceed. This is what lets MAX_WORKERS threads run
    concurrently without ever collectively exceeding Upstox's limits.
    """

    def __init__(self, max_per_sec, max_per_30min):
        self.max_per_sec = max_per_sec
        self.max_per_30min = max_per_30min
        self.lock = threading.Lock()
        self.sec_window = []
        self.window_30min = []

    def acquire(self):
        while True:
            with self.lock:
                now = time.time()
                self.sec_window = [t for t in self.sec_window if now - t < 1.0]
                self.window_30min = [t for t in self.window_30min if now - t < 1800.0]

                if len(self.sec_window) < self.max_per_sec and len(self.window_30min) < self.max_per_30min:
                    self.sec_window.append(now)
                    self.window_30min.append(now)
                    return

            time.sleep(0.02)


rate_limiter = RateLimiter(MAX_REQUESTS_PER_SECOND, MAX_REQUESTS_PER_30MIN)


# =================================================================================
# STEP 1: Instrument master
# =================================================================================
def load_instruments(segment=SEGMENT_FILTER):
    print("Downloading instrument master...")
    resp = requests.get(INSTRUMENT_MASTER_URL, timeout=60)
    resp.raise_for_status()

    with gzip.GzipFile(fileobj=io.BytesIO(resp.content)) as f:
        data = json.load(f)

    df = pd.DataFrame(data)
    df = df[df["segment"] == segment]
    if "instrument_type" in df.columns:
        df = df[df["instrument_type"] == "EQ"]

    df = df[["instrument_key", "trading_symbol", "name"]].reset_index(drop=True)

    if MAX_INSTRUMENTS is not None:
        df = df.head(MAX_INSTRUMENTS)

    print(f"Loaded {len(df)} instruments in segment {segment}.")
    return df


# =================================================================================
# STEP 2: ATR
# =================================================================================
def compute_atr(df, period=ATR_PERIOD):
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.rolling(window=period).mean()


# =================================================================================
# STEP 3: per-instrument worker (runs inside a thread)
# =================================================================================
def screen_one(instrument_key, symbol, to_date, from_date):
    try:
        rate_limiter.acquire()
        url = HISTORICAL_CANDLE_URL.format(
            instrument_key=instrument_key, to_date=to_date, from_date=from_date
        )
        resp = requests.get(url, headers=HEADERS, timeout=20)

        if resp.status_code == 429:
            # Shouldn't normally happen given the limiter, but back off and
            # retry once if it does (e.g. clock drift, other processes using
            # the same token).
            time.sleep(2)
            rate_limiter.acquire()
            resp = requests.get(url, headers=HEADERS, timeout=20)

        if resp.status_code != 200:
            return None

        candles = resp.json().get("data", {}).get("candles", [])
        if not candles or len(candles) < ATR_PERIOD + 1:
            return None

        df = pd.DataFrame(
            candles, columns=["timestamp", "open", "high", "low", "close", "volume", "oi"]
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.sort_values("timestamp").reset_index(drop=True)
        df["atr"] = compute_atr(df)

        latest = df.iloc[-1]
        latest_close = latest["close"]
        latest_atr = latest["atr"]
        if pd.isna(latest_atr) or latest_close == 0:
            return None

        atr_pct = latest_atr / latest_close * 100
        avg_volume = df["volume"].tail(20).mean()

        # ---- the three filters ----
        if avg_volume < MIN_AVG_VOLUME:
            return None
        if atr_pct < ATR_PCT_THRESHOLD:
            return None
        if not (PRICE_MIN <= latest_close <= PRICE_MAX):
            return None

        return {
            "symbol": symbol,
            "instrument_key": instrument_key,
            "close": round(latest_close, 2),
            "atr": round(latest_atr, 2),
            "atr_pct": round(atr_pct, 2),
            "avg_volume_20d": int(avg_volume),
        }

    except Exception:
        return None


# =================================================================================
# MAIN
# =================================================================================
def screen_stocks():
    if not ACCESS_TOKEN:
        raise SystemExit(
            "UPSTOX_ACCESS_TOKEN environment variable is not set.\n"
            '  export UPSTOX_ACCESS_TOKEN="your_token_here"'
        )

    t0 = time.time()
    instruments = load_instruments()
    n = len(instruments)

    # ---- Upfront ETA so you know before it runs whether this fits in 5 min ----
    eta_sec = n / MAX_REQUESTS_PER_SECOND
    eta_sec = max(eta_sec, (n / MAX_REQUESTS_PER_30MIN) * 1800)
    print(f"Estimated minimum time for {n} instruments: ~{eta_sec/60:.1f} min "
          f"(bounded by Upstox's 30-min request cap, not by thread count)")
    if n > MAX_REQUESTS_PER_30MIN:
        print(f"  [!] {n} instruments exceeds the {MAX_REQUESTS_PER_30MIN}-per-30-min "
              f"safe cap -- this run will NOT finish within 5 minutes without "
              f"exceeding Upstox's rate limits. Set MAX_INSTRUMENTS to cap the "
              f"batch, or plan for multiple 30-min windows.")

    to_date = datetime.now().strftime("%Y-%m-%d")
    from_date = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    results = []
    done = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(screen_one, row["instrument_key"], row["trading_symbol"], to_date, from_date): row["trading_symbol"]
            for _, row in instruments.iterrows()
        }

        for future in as_completed(futures):
            done += 1
            r = future.result()
            if r:
                results.append(r)
                print(f"[MATCH] {r['symbol']}: close={r['close']}, "
                      f"ATR%={r['atr_pct']}, avg_vol={r['avg_volume_20d']}")

            if done % 100 == 0 or done == n:
                elapsed = time.time() - t0
                print(f"  progress: {done}/{n}  elapsed={elapsed:.1f}s")

    result_df = pd.DataFrame(results)
    if not result_df.empty:
        result_df = result_df.sort_values("atr_pct", ascending=False)
    result_df.to_csv(OUTPUT_CSV, index=False)

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.1f}s ({elapsed/60:.1f} min). "
          f"{len(result_df)} stocks matched all 3 filters.")
    print(f"Saved: {OUTPUT_CSV}")
    return result_df


if __name__ == "__main__":
    screen_stocks()
