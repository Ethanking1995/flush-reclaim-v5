# Flush-Reclaim Audited V6 — Engineering & Quantitative Validation Report

## 1. Reproducible Baseline & Frozen Data Manifest

- **Review Baseline Commit:** `8bd8ad6ea73faf62e2a70b0c9ae3f7194f7e90cd` (`main`)
- **Audited Branch:** `audited-v6`
- **Data Provider:** Yahoo Finance (`yfinance==1.2.0`) + TradingView America Scanner API
- **Frozen Snapshot Timestamp:** `2026-11-10 08:51:11 EST`
- **Historical Daily Window (`1d`):** `2025-11-10` to `2026-11-09` (`251` daily bars per symbol)
- **Historical Intraday Window (`5m`):** `2026-08-17 09:30:00-04:00` to `2026-11-09 15:55:00-05:00` (`60` trading sessions, `4,680` 5-minute bars per symbol)
- **Exchange Timezone & Session Definitions:**
  - Timezone: `America/New_York` (handling `EDT` UTC-4 through `2026-10-30` and `EST` UTC-5 from `2026-11-02` onward)
  - Regular Session: `09:30–16:00 ET` | Entry Window: `09:50–13:00 ET` | Mandatory Flatten: `15:40 ET`
  - Early-Close Session (`13:00 ET` close, e.g., `2026-11-27`): Entry Window: `09:50–12:00 ET` | Mandatory Flatten: `12:40 ET`
- **Manifest & Checksums:** Stored in `baseline_data/manifest.json` with SHA-256 digests for all 24 raw `.csv.gz` files (`baseline_data/raw_ohlcv/`) and legacy benchmark output (`baseline_data/legacy_v5_benchmark.json`).

| Universe Tier | Symbols | Daily Regime Policy |
| :--- | :--- | :--- |
| **Core-3 Monitored** | `FISV`, `PLNT`, `NU` | `core_v5_dislocated_sma5` |
| **Expanded-6 Hardcoded** | `FISV`, `PLNT`, `NU`, `CNC`, `EEFT`, `GPN` | `core_v5_dislocated_sma5` |
| **Dynamic Discovery Snapshot** | `VIAV`, `CHTR`, `MGM`, `MOS`, `FISV`, `RDDT` | `scanner_hl10_atr_compression` |

---

## 2. Summary of Review Findings Confirmed on Baseline (`8bd8ad6`) & V6 Fixes

All findings identified in the review prompt were confirmed present at commit `8bd8ad6ea73faf62e2a70b0c9ae3f7194f7e90cd` and resolved in `audited-v6`:

1. **Unprotected Live Entry & Ignored Preflight Failures (`tasty_executor.py`):**
   - *Baseline Defect:* `execute_1click_order` submitted `staged["entry_order"]` (a standalone limit buy with no protective stop or target), caught `Exception` during `dry_run=True` preflight and continued to live submission, and built an `OTOCO` pairing a full-lot stop (`2,373` sh) with a half-lot Leg 1 limit (`1,186` sh) that would cancel the entire stop when Leg 1 filled.
   - *V6 Fix:* Implemented `build_v6_split_tranche_orders()` creating **two independent OTOCO brackets**: **Tranche A (`Q_1` shares)** (`Buy Q_1` -> OCO `Limit Sell Q_1 @ Leg1` + `Stop Q_1 @ StopPrice`) and **Tranche B (`Q_2` shares)** (`Buy Q_2` -> OCO `Limit Sell Q_2 @ Leg2` + `Stop Q_2 @ StopPrice`). When Tranche A's Leg 1 fills, Tranche B's `Q_2` stop remains 100% active at the broker. Any failed, incomplete, or error-containing preflight raises `PreflightRejectedError` and hard-blocks live submission.
2. **Offline Schema Validation vs. Broker Preflight:**
   - *V6 Fix:* Local Pydantic serialization (`model_dump_json()`) is explicitly labeled `LOCAL_SCHEMA_VALIDATION_ONLY` (`broker_preflight_verified = False`). Only an actual broker `place_complex_order(..., dry_run=True)` response with zero errors sets `BROKER_PREFLIGHT_VERIFIED`.
3. **Validation Under `python -O` (`PYTHONOPTIMIZE=1`):**
   - *Baseline Defect:* `validate_order_safety` used Python `assert` statements, which are stripped under `python -O`.
   - *V6 Fix:* Replaced all `assert` statements with explicit exceptions (`OrderValidationError`, `PreflightRejectedError`, `SessionEnvironmentMismatchError`, `RiskLimitExceededError`, `StaleSignalError`, `ReconciliationRequiredError`, `ModeConflictError`). Verified via `python -O -m unittest discover -s tests -v`.
4. **Shared Causal Engine & Lookahead Removal (`strategy_engine.py`):**
   - *Baseline Defects:*
     - `render_trader_hud` evaluated `accum_map.get(last_dt, False) or latest_accum` (leaking today's developing daily bar into today's entry eligibility) and omitted `is_hammer_reclaim`.
     - `run_v5_production` required `len(day_df) >= 30` and flattened on `is_last_bar` even on partial morning slices.
     - `ToD_MedVol` and `midday_med` used `.bfill()`, and `ref_midday_vol` fell back to same-session `day_df['Volume'].median()`.
     - Leg 1 target used current bar's end-of-bar `row['VWAP']` tested against earlier `row['High']`.
     - Runner mode activated on the same bar that touched `+0.85%` using end-of-bar `Close >= entry * 1.0075 and Close > EMA12` (retroactively canceling a resting `+0.85%` limit order that would already have filled), or paid `+1.60%` stretch without prior runner activation.
     - Stop fills ignored gap-downs (`Low <= stop_price` always filled at `stop_price`).
   - *V6 Fix:* Extracted `strategy_engine.py` consumed by replay, paper, and HUD. Removed all `.bfill()` and same-day fallbacks; enforced `min_tod_history_sessions=5` warm-up; separated `prior_session_accum_ok` (`< session_date`) from display-only `developing_next_session_accum`; evaluated active resting orders first (`min(Open, stop_price) - slippage` for stops, `prev_bar_vwap` for Leg 1, and `+1.60%` stretch target only on bars *after* runner activation at a prior bar close).
5. **Reconciling `$150,000` Clip, `3,000`-Share Ceiling, and `0.65%` Stop Cap:**
   - *Baseline Defect:* Initial stops could exceed `-0.65%` (up to `-0.80%` on `FISV 2026-11-06` and `NU 2026-09-08`, and `-0.72%` on `EEFT 2026-08-27`), and backtest returns assumed `$150,000` notional on `NU` (`~$15/sh`, which would require `~10,000` shares) while `tasty_executor.py` capped orders at `3,000` shares (`~$46,200` notional on `NU`).
   - *V6 Fix:* `compute_stop_and_shares` enforces identical integer share sizing across replay, HUD, and broker execution (`min(floor(clip / price), 3_000, floor(max_risk / per_share_risk))`), and provides explicit stop policies: `structural_risk_sized` (scales shares down when structural stop > `0.65%` so dollar risk `<= $975`) and `strict_cap_reject` (rejects setups where structural stop > `0.65%`).

---

## 3. Single-Factor Ablation & Sensitivity Matrix (`baseline_data/`)

All runs below are evaluated over the **55 post-warmup NYSE trading sessions** (`2026-08-24` to `2026-11-09`, after the 5-session causal `ToD_MedVol` / `Midday_MedVol` warm-up `2026-08-17` to `2026-08-21`), with initial account equity `$300,000`, target clip `$150,000`, `3,000`-share hard ceiling, `max_concurrent_positions=2`, and `1.5 bps/side` (`3.0 bps` round-trip) base execution slippage:

| Configuration | Universe | Days | Exec Trades | Skipped (Cap) | Ambig Bars | Win Rate | Avg Net% | Total Net PnL | Portfolio Ret% | Max DD% | **Daily Portfolio Sharpe** | *Legacy Trade-Level Sharpe (Comparison Only)* |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **1. Raw Signal (No Filters)** | Core-3 | 55 | 79 | 1 | 0 | 69.6% | +0.151% | +\$13,296 | +4.43% | 1.61% | **4.01** | *4.17* |
| **2. +Accum Regime Filter Only** | Core-3 | 55 | 29 | 0 | 0 | 79.3% | +0.246% | +\$9,305 | +3.10% | 0.87% | **4.48** | *7.89* |
| **3. +Accum + 14d ER Filter** | Core-3 | 55 | 24 | 0 | 0 | 83.3% | +0.290% | +\$9,049 | +3.02% | 0.34% | **5.16** | *10.11* |
| **4. Core-3 All Filters (`strict_cap_reject`)** | Core-3 | 55 | 23 | 0 | 0 | 87.0% | +0.333% | +\$10,069 | +3.36% | 0.34% | **6.04** | *12.70* |
| **5. Core-3 All Filters (`structural_risk_sized`)** | Core-3 | 55 | 26 | 0 | 0 | 92.3% | +0.412% | +\$11,941 | +3.98% | 0.34% | **6.82** | *19.24* |
| **6. Core-3 All Filters (`legacy_v5_capped`)** | Core-3 | 55 | 26 | 0 | 0 | 92.3% | +0.412% | +\$12,108 | +4.04% | 0.34% | **6.79** | *19.24* |
| **7. Sensitivity: `5.0 bps/side` Slippage (`10 bps` RT)** | Core-3 | 55 | 22 | 1 | 0 | 86.4% | +0.265% | +\$7,751 | +2.58% | 0.37% | **5.31** | *9.87* |
| **8. Sensitivity: `+1 Bar Open` Entry Delay** | Core-3 | 55 | 22 | 1 | 0 | 90.9% | +0.305% | +\$8,578 | +2.86% | 0.34% | **5.87** | *11.97* |
| **9. Expanded-6 (`strict_cap_reject`)** | Exp-6 | 55 | 30 | 1 | 0 | 83.3% | +0.325% | +\$13,212 | +4.40% | 0.34% | **7.23** | *11.76* |
| **10. Expanded-6 (`structural_risk_sized`)** | Exp-6 | 55 | 33 | 1 | 0 | 87.9% | +0.387% | +\$14,932 | +4.98% | 0.34% | **7.46** | *15.86* |
| **11. Separate Dynamic Scanner Regime Proxy** | Exp-6 | 55 | 20 | 0 | 0 | 75.0% | +0.216% | +\$3,872 | +1.29% | 0.67% | **2.25** | *5.97* |

### Key Takeaways from the Ablation & Sensitivity Matrix
1. **Daily Portfolio Sharpe vs. Trade-Level `sqrt(252)` Sharpe:** Replacing trade-level `sqrt(252)` annualization with true daily account-level portfolio returns (which include the 33–35 flat `0.0%` no-trade sessions) reduces the headline Sharpe on Core-3 (`structural_risk_sized`) from an overstated **19.24** to **6.82** (and **6.04** under `strict_cap_reject`).
2. **Each Independent Filter Contributes Measurably:**
   - Adding the **Daily Accumulation Proxy (`+Accum`)** cuts trade count from `79` to `29`, lifting win rate from `69.6%` to `79.3%` and cutting max drawdown from `1.61%` to `0.87%`.
   - Adding the **14-Day Pre-Earnings Filter (`+14d ER`)** removes 5 pre-earnings trades (including `PLNT` and `CNC` pre-ER stops), cutting max drawdown from `0.87%` to `0.34%` and raising Daily Sharpe from `4.48` to `5.16`.
   - Adding the **Intraday SPY Waterfall Filter (`+SPY`)** blocks entries during market-wide morning cascades, lifting win rate to `87.0%–92.3%` and Daily Sharpe to `6.04–6.82`.
3. **Execution Cost & Latency Sensitivity:** Tripling per-side slippage from `1.5 bps` to `5.0 bps` (`10.0 bps` round-trip) reduces Core-3 net PnL from `+$10,069` to `+$7,751` (Daily Sharpe `5.31`), and delaying fills to the next 5m bar's `Open + 1.5 bps` yields `+$8,578` (Daily Sharpe `5.87`).
4. **Core-3 V5 Regime Proxy vs. Dynamic Scanner Regime Proxy:** The Dynamic Scanner's `HL_10d >= 6 & ATR_Pct <= 4.5% & Vol_Ratio >= 1.0` proxy (`scanner_hl10_atr_compression`) is a materially different strategy rule (`20` trades, `75.0%` WR, Daily Sharpe `2.25`) than the Core-3 `core_v5_dislocated_sma5` proxy (`33` trades, `87.9%` WR, Daily Sharpe `7.46`) and is therefore kept strictly separate.

---

## 4. Per-Ticker Breakdown & Top-Trade Concentration (`Expanded-6`, `structural_risk_sized`)

| Ticker | Executed Trades | Win Rate | Sum Net Return% | Executable Net PnL (\$) | Share Cap Impact |
| :--- | ---: | ---: | ---: | ---: | :--- |
| **FISV** | 7 | 100.0% | +3.63% | +\$5,198.77 | Sized at `1,974–3,000` sh (`~$122k–$150k` notional) |
| **PLNT** | 10 | 80.0% | +3.51% | +\$5,093.80 | Sized at `2,484–3,000` sh (`~$127k–$150k` notional) |
| **EEFT** | 3 | 66.7% | +1.14% | +\$1,604.59 | Sized at `1,951–2,329` sh (`~$135k–$150k` notional) |
| **CNC** | 4 | 75.0% | +0.92% | +\$1,386.48 | Sized at `2,253–2,348` sh (`~$148k–$150k` notional) |
| **NU** | 9 | 100.0% | +3.57% | +\$1,648.35 | Capped at **`3,000` shares** (`~$45k–$48k` notional at `$15–$16/sh`) |
| **GPN** | 0 | N/A | 0.00% | \$0.00 | Aug 17 trade dropped due to `<5` warm-up sessions (no `.bfill()`) |
| **Total** | **33** | **87.9%** | **+12.76%** | **+\$14,931.99** | **1 simultaneous signal skipped by capital (`NU 2026-10-12`)** |

- **Top-3 Trade Concentration:** The top 3 trades (`2026-08-26 FISV +$1,765.92`, `2026-08-28 CNC +$1,188.10`, and `2026-11-09 PLNT +$1,109.60`) generated **+\$4,063.62** (**27.2%** of total net PnL). Removing the top 3 trades leaves **+\$10,868.37** across the remaining 30 trades (`86.7%` win rate).

---

## 5. Step-by-Step & Trade-by-Trade Comparison: Legacy V5 (37 Trades) vs. Audited V6

### Why Trade Count Changed from 37 (Legacy V5) to 33 (`structural_risk_sized`) / 30 (`strict_cap_reject`):
1. **Removal of `.bfill()` and Same-Day Midday Fallback (`37 -> 35` trades in Step 1):**
   - `2026-08-17 CNC` (`-0.66%` in V5) and `2026-08-17 GPN` (`-0.54%` in V5) occurred on the **very first day** of the 60-day 5m dataset (`2026-08-17`), where zero prior 5m sessions existed. Legacy V5 only triggered them because `.bfill()` leaked `2026-08-18` volume backward into `2026-08-17`.
2. **Enforcing `min_tod_history_sessions = 5` Warm-Up Window (`35 -> 33` executed + `1` capital-skipped in Step 2):**
   - `2026-08-19 PLNT` (`+1.25%` in V5, session #3) dropped because fewer than 5 prior sessions were available for `ToD_MedVol`.
   - Note: `2026-08-26 EEFT` had a morning bar rejected when `.bfill()` was removed; on `2026-10-12`, `EEFT`, `PLNT`, and `NU` all had active positions simultaneously, so `max_concurrent_positions=2` (`$300,000` account cap) skipped the third concurrent signal.
3. **Stop-Distance Policy (`33` trades in `structural_risk_sized` vs. `30` trades in `strict_cap_reject`):**
   - Three trades in the post-warmup window had structural stops (`flush_low * 0.9995`) wider than `-0.65%` below entry:
     - `2026-08-27 EEFT` (`-0.72%` stop): Scaled down from `2,160` sh to `1,951` sh in `structural_risk_sized` (capping planned risk at `$975`), or rejected in `strict_cap_reject`.
     - `2026-09-08 NU` (`-0.80%` stop): Already at `3,000` sh (`$368` planned risk `< $975`) in `structural_risk_sized`, or rejected in `strict_cap_reject`.
     - `2026-11-06 FISV` (`-0.80%` stop): Scaled down from `2,427` sh to `1,974` sh in `structural_risk_sized` (capping planned risk at `$975`), or rejected in `strict_cap_reject`.
4. **Intrabar Causal Sequencing (`prev_bar_vwap` & Runner Activation Chronology):**
   - In Legacy V5, a bar touching `+0.85%` (`ret_high >= 0.85`) checked the **same bar's end-of-candle** `Close >= entry * 1.0075` to cancel the `+0.85%` target and become a runner, or paid `+1.60%` without prior runner activation (`2026-11-06 FISV`, `2026-10-08 NU`, `2026-10-09 NU`).
   - In Audited V6, resting limit orders are evaluated **before** end-of-bar runner activation. Consequently, when price rallies from below `+0.75%` straight through `+0.85%` within a single 5m candle before runner mode was active at the start of that candle, the resting `+0.85%` limit order fills (`+0.595%` net return across both tranches) rather than being retroactively canceled at the end of the candle.

---

## 6. Remaining Limitations & Mandatory Conditions Before Live Execution

1. **No Untouched Out-of-Sample Intraday Window:** Yahoo Finance restricts `5m` intraday history to the most recent 60 trading days (`2026-08-17` to `2026-11-09`), and all V1–V5 design iterations were developed on this same 60-day window (`FISV`, `PLNT`, `NU`). Therefore, **no untouched historical out-of-sample 5-minute period exists**. Prospective validation must be accumulated via `--mode paper` and `--mode broker-preview` forward logging.
2. **Bar Granularity (5-Minute OHLCV):** Even with causal `prev_bar_vwap`, gap-aware stop fills, and conservative `stop_first` ambiguous-bar resolution, 5-minute bars cannot observe sub-minute quote queue priority or short-lived bid-ask widening during fast flushes.
3. **Mandatory Conditions Required Before Enabling `--mode execute-live`:**
   - Complete at least 20 prospective trading sessions in `--mode paper` / `--mode broker-preview` recording live RTH spreads and preflight responses in `execution_journal.json`.
   - Verify `.env` credentials (`TT_USERNAME`, `TT_PASSWORD`, `TT_ACCOUNT_ID=5WJ26897`, `TT_ENVIRONMENT=SANDBOX` first, then `PRODUCTION`) pass `OrderLifecycleManager.reconcile_account_state()` and `run_broker_preflight()` with zero errors.
   - Run an active order-status websocket/polling listener alongside `OrderLifecycleManager` during market hours so Tranche A Leg 1 fills automatically trigger `handle_leg1_exit_and_amend_tranche_b_to_breakeven()`, runner updates trigger `amend_tranche_b_runner_stop()`, and `15:40 ET` (`12:40 ET` early close) triggers `flatten_remaining_at_cutoff()`.
