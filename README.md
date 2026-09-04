# ⚡ ORB + ADX Trading System

### Opening Range Breakout Strategy Engine for NSE Equities  
**Powered by Upstox API · 15-minute candles · ADX(14) filter · Paper + Backtest modes**

---

```text
 ██████╗ ██████╗ ██████╗     ██╗ █████╗ ██████╗ ██╗  ██╗
██╔═══██╗██╔══██╗██╔══██╗    ██║██╔══██╗██╔══██╗╚██╗██╔╝
██║   ██║██████╔╝██████╔╝    ██║███████║██║  ██║ ╚███╔╝ 
██║   ██║██╔══██╗██╔══██╗    ██║██╔══██║██║  ██║ ██╔██╗ 
╚██████╔╝██║  ██║██████╔╝    ██║██║  ██║██████╔╝██╔╝ ██╗
 ╚═════╝ ╚═╝  ╚═╝╚═════╝     ╚═╝╚═╝  ╚═╝╚═════╝ ╚═╝  ╚═╝
```

---

## 📌 What This Project Is

A complete, production-ready research & paper-trading pipeline for an **Opening Range Breakout (ORB)** strategy on Indian equities (NSE), enhanced with a live **ADX(14) > 25** trend filter.

It includes:

- Universe screening (liquidity + volatility + price band)
- Full historical backtesting with realistic assumptions
- Ranking & shortlisting of best-performing stocks
- Crash-safe **paper trading** engine that runs during market hours
- Detailed trade logs + equity curves for analysis

> **Important**: This is currently a **paper-trading + research** system only.  
> No real orders are ever sent to the exchange.

---

## 🧠 Strategy Rules (Unchanged Across All Scripts)

| Parameter              | Value                          | Description |
|------------------------|--------------------------------|-----------|
| Timeframe              | 15-minute candles              | Opening range = first candle (09:15–09:30) |
| Entry                  | Break of High1 or Low1         | First side that breaks wins (only one trade per day) |
| Stop Loss              | Opposite ORB level             | Or 2× range if opening candle is “big” (>1.5%) |
| Target                 | 1.5R                           | Risk : Reward = 1 : 1.5 |
| Trailing Stop          | 0.2% of entry price            | Ratchets every further 0.2% favourable move |
| Trend Filter           | Daily ADX(14) > 25             | Computed live (no look-ahead) using synthetic “today so far” bar |
| Costs                  | 0.5% of entry value only       | Exit side is free |
| Square-off             | 15:14 IST                      | Force close any open position |

All decision logic lives in `orb_backtester.py` and is reused unchanged by every other module.

---

## 🗂️ Project Structure

```text
.
├── build_manual_watchlist.py     # Force-include specific symbols (no screening)
├── fast_stock_filter.py          # Liquidity + ATR% + Price band screener
├── orb_backtester.py             # Core strategy engine + historical backtester
├── ranking_pipeline.py           # Full pipeline → top-N shortlist
├── run_selected_instruments.py   # Generate detailed trade_log + equity_curve
├── orb_trading_engine.py         # Paper trading (live) + backtest runner
├── filtered_stocks.csv           # Manual / fallback instrument list
└── output/                       # All results land here
    ├── ranked_results.csv
    ├── final_shortlist.csv
    ├── trade_log.csv
    ├── equity_curve.csv
    ├── trades_<SYMBOL>.csv
    └── state/                    # Paper-mode crash recovery
```

---

## 🛠️ Setup

### 1. Requirements

```bash
python3 -m venv venv
source venv/bin/activate          # Linux / macOS
# or  venv\Scripts\activate       # Windows

pip install requests pandas numpy pytz
```

### 2. Upstox Access Token

You need a valid **Upstox API access token** (regenerate daily / when it expires).

**Recommended (secure) way**:

```bash
export UPSTOX_ACCESS_TOKEN="your_token_here"
```

Then edit the scripts that currently hardcode the token (`fast_stock_filter.py` and `orb_backtester.py`) and change them to:

```python
ACCESS_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "")
# or
UPSTOX_ACCESS_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "")
```

> ⚠️ Never commit a real token to git. Rotate it immediately if it has ever been shared.

### 3. Folder Setup

```bash
mkdir -p output data_cache
```

---

## 🚀 How to Use – Recommended Workflows

### Workflow A – Full Automated Pipeline (Recommended first run)

```bash
python3 ranking_pipeline.py
```

This does three steps automatically:

1. Screens the entire NSE equity universe (`fast_stock_filter.py`)
2. Backtests every survivor with the ORB+ADX strategy (`orb_backtester.py`)
3. Applies win-rate / net-PnL filters and produces **top-5 shortlist** → `output/final_shortlist.csv`

### Workflow B – Manual Watchlist (Force specific symbols)

Edit the list inside `build_manual_watchlist.py`, then:

```bash
python3 build_manual_watchlist.py
# → creates / updates filtered_stocks.csv
```

### Workflow C – Deep Dive on Selected Symbols

1. Pick symbols from `output/ranked_results.csv`
2. Put them in `SELECTED_SYMBOLS` inside `run_selected_instruments.py`
3. Run:

```bash
python3 run_selected_instruments.py
```

Produces rich `trade_log.csv` + `equity_curve.csv` perfect for notebooks / further analysis.

### Workflow D – Daily Paper Trading / Backtesting

```bash
# Automatic mode selection (paper during market hours, backtest otherwise)
python3 orb_trading_engine.py

# Force specific symbols
python3 orb_trading_engine.py CUPID TCC KABRAEXTRU GVPIL DEEPINDS

# Force mode
python3 orb_trading_engine.py --mode paper
python3 orb_trading_engine.py --mode backtest --lookback-days 45
```

---

## ▶️ Running Individual Components

| Script                        | Command                              | Output |
|-------------------------------|--------------------------------------|--------|
| Manual watchlist              | `python3 build_manual_watchlist.py`  | `filtered_stocks.csv` |
| Universe screener             | `python3 fast_stock_filter.py`       | `filtered_stocks_v2.csv` |
| Full backtester               | `python3 orb_backtester.py`          | `output/ranked_results.csv` + per-symbol trade files |
| Ranking pipeline              | `python3 ranking_pipeline.py`        | `output/final_shortlist.csv` |
| Selected instruments runner   | `python3 run_selected_instruments.py`| `output/trade_log.csv` + `equity_curve.csv` |
| Live paper / backtest engine  | `python3 orb_trading_engine.py`      | Appends to `trade_log.csv` & `equity_curve.csv` |

---

## 📊 How to Interpret the Outputs

### 1. `output/ranked_results.csv` (from backtester)

| Column                | Meaning |
|-----------------------|--------|
| `rank`                | Sorted by net_pnl_pct (highest first) |
| `breakout_days`       | Days the stock actually broke High1 or Low1 (activity measure) |
| `trades_taken`        | Days that also passed the ADX > 25 filter |
| `win_rate_pct`        | % of trades that were profitable after costs |
| `net_pnl_pct`         | Sum of all trade % returns (after 0.5% entry cost) |
| `avg_pnl_per_trade_pct` | Average return per trade |

**Good candidates** usually show:
- Decent number of trades (≥ 5–8)
- Win rate ≥ 50–55%
- Positive net_pnl_pct
- Reasonable breakout_days (not pure noise)

### 2. `output/final_shortlist.csv`

The production-ready top-N list after applying hard filters (trades, win-rate, net PnL).  
This is what `orb_trading_engine.py` uses by default when you run it with no symbols.

### 3. `output/trade_log.csv`

One row per **closed** trade. Key columns:

- `mode` → `PAPER` or `BACKTEST`
- `direction`, `entry_price`, `exit_price`, `stop_loss`, `target`
- `exit_reason` → `TARGET` / `STOP_LOSS` / `TRAILING_SL` / `SQUARE_OFF_EOD`
- `adx_at_entry`
- `gross_pnl`, `costs`, `net_pnl` (rupee figures when quantity is applied)
- `cumulative_pnl`, `equity`

### 4. `output/equity_curve.csv`

Simple running equity series. Perfect for plotting.

### 5. Paper Mode State Files (`output/state/`)

JSON files that allow the paper engine to resume exactly where it left off after a crash or restart — no double entries, no missed exits.

---

## ⚙️ Important Configuration Knobs

| File                      | What you can safely change |
|---------------------------|----------------------------|
| `fast_stock_filter.py`    | `MIN_AVG_VOLUME`, `ATR_PCT_THRESHOLD`, `PRICE_MIN/MAX`, rate limits |
| `orb_backtester.py`       | `ADX_THRESHOLD`, `BIG_CANDLE_THRESHOLD_PCT`, `RISK_REWARD`, `TRAIL_STEP_PCT`, lookback days |
| `ranking_pipeline.py`     | `MIN_TRADES`, `MIN_WIN_RATE_PCT`, `MIN_NET_PNL_PCT`, `TOP_N` |
| `orb_trading_engine.py`   | `POSITION_SIZE_QTY`, `STARTING_CAPITAL`, `POLL_INTERVAL_SEC` |
| `build_manual_watchlist.py` | `WATCHLIST` list |

---

## 🔮 Future Upgrade Path

**Only one major next step remains:**

> **Convert the paper-trading engine into a real live trading system.**

This would involve:

1. Replacing simulated fills with actual Upstox order placement APIs
2. Adding proper position sizing, risk limits, and capital management
3. Robust order-status reconciliation and partial-fill handling
4. Real-time risk monitoring + kill switches
5. Comprehensive audit logging and alerting

Everything else (strategy logic, screening, ranking, crash recovery, logging) is already production-grade and ready to be reused.

---

## ⚠️ Disclaimers & Notes

- This is **not** financial advice. Past backtest performance does not guarantee future results.
- The system currently places **zero real orders**. Paper mode is fully simulated.
- Upstox rate limits are respected, but aggressive use can still get you throttled.
- NSE holidays are **not** automatically detected — populate `NSE_HOLIDAYS` if needed.
- Always double-check the instrument keys and symbol spellings.

---

## 📜 License & Credits

Built for personal research and systematic trading exploration.  
Strategy core, data handling, and paper engine designed for maximum transparency and auditability.

Happy trading — and remember: **edge first, automation second**.

---

*Generated for the ORB + ADX project · Keep the rules pure · Keep the logs clean*