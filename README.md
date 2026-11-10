# Flush-Reclaim Audited V6 (`audited-v6`)

Audited intraday mean-reversion engine, portfolio simulator, universe scanner, and Tastytrade execution bridge built on top of review baseline commit `8bd8ad6ea73faf62e2a70b0c9ae3f7194f7e90cd`.

---

## 1. Architecture & Core Modules

| Module | Responsibility |
| :--- | :--- |
| `config_schema.py` | Separates `StrategyConfig`, `ExecutionConfig`, and `RiskConfig`; defines explicit validation exceptions (`OrderValidationError`, `PreflightRejectedError`, `SessionEnvironmentMismatchError`, `RiskLimitExceededError`, `StaleSignalError`, `ReconciliationRequiredError`, `ModeConflictError`) that remain active under `python -O`. |
| `market_calendar.py` | NYSE trading session calendar (`NYSECalendar`) handling market holidays, early-close sessions (`13:00 ET` close / `12:40 ET` mandatory flatten / `12:00 ET` entry cutoff), US DST transitions (`America/New_York`), and causal prior-completed-session lookups. |
| `earnings_oracle.py` | Unified `EarningsOracle` shared by replay, scanner, and HUD. Tracks `(earnings_date, status, source, observed_at_et, schedule_provenance)`. Keeps unknown earnings as `UNKNOWN` (never labeled `">14d"`). |
| `strategy_engine.py` | Single shared causal state machine (`evaluate_session_causal`) consumed by historical replay, paper trading, and the live HUD. Enforces strictly lagged prior-session daily accumulation (`< session_date`), separate display-only developing next-session diagnostics, causal `ToD_MedVol` and `Midday_MedVol` without `.bfill()`, gap-aware stop-market fills, prior-bar VWAP Leg 1 targets, prior-bar-activated runner targets, and conservative `stop_first` ambiguous-bar resolution. |
| `portfolio_simulator.py` | Account-level daily portfolio simulator (`run_portfolio_simulation`) replacing trade-level `sqrt(252)` Sharpe with daily portfolio Sharpe across all trading days (including no-trade days and open-position mark-to-market), integer share sizing, buying-power reservation, concurrent position limits, and deterministic tie-breaking. |
| `tasty_executor.py` | Audited Tastytrade SDK (`tastytrade==11.1.0`) execution adapter & `OrderLifecycleManager`. Implements the **Split-Tranche OTOCO** protective bracket (`Tranche A Q_1 OTOCO + Tranche B Q_2 OTOCO`) so remaining `Q_2` shares stay 100% protected when Leg 1 (`Q_1`) fills, plus breakeven/runner stop amendments, `15:40 ET` (`12:40 ET` early-close) flatten, idempotency journaling (`execution_journal.json`), and startup reconciliation distinguishing pre-existing IRA holdings. |
| `universe_scanner.py` | Dynamic candidate discovery funnel separating overnight discovery (`UNVERIFIED_OFF_HOURS`) from regular-trading-hours spread verification (`VERIFIED_RTH`), deep-validating all selected candidates, separating `core_monitored_tickers` from `order_eligible_tickers`, and ranking candidates via a heuristic score (`0–100`). |
| `test_regimes.py` | Unambiguous `argparse` CLI entrypoint (`--mode replay | scan | monitor | paper | broker-preview | execute-live`). |

---

## 2. Key Methodological & Terminology Clarifications

- **Daily Accumulation Proxy:** The daily filter (`core_v5_dislocated_sma5` for Core-3 / Expanded-6, or `scanner_hl10_atr_compression` for dynamic discovery) is an **operational price/volume proxy** evaluated strictly on completed daily bars prior to the current session (`< session_date`). It does not prove institutional identity or Rule 10b-18 buyback activity.
- **14-Day Pre-Earnings Filter:** Excluding trades within 14 calendar days prior to a scheduled earnings release is a **strategy filter rule** designed to avoid pre-earnings positioning volatility. It is not a universal legal prohibition and does not guarantee zero gap risk.
- **Planned Initial Stop Risk vs. Realized Loss:** Stop distances (`<= 0.65%` under `strict_cap_reject` or dollar-risk-sized under `structural_risk_sized`) represent **planned initial stop risk** (`-$975` to `-$1,500` planned risk on a `$150,000` clip). Intraday gap openings or illiquid sweeps through the stop price execute as stop-market fills (`min(Open, StopPrice) - slippage`) and can exceed planned stop risk.
- **Discovery Score:** The `0–100` score in `universe_scanner.py` is a **heuristic ranking score** combining higher-low count, volume ratio, ATR compression, and dollar volume.
- **Validation Modes:** Offline order serialization is labeled `LOCAL_SCHEMA_VALIDATION_ONLY`. Only an explicit Tastytrade API `place_complex_order(..., dry_run=True)` response with zero errors is labeled `BROKER_PREFLIGHT_VERIFIED`.

---

## 3. Exact Reproduction Commands

```bash
# 1. Run the offline regression test suite (15 tests, zero network/credentials required)
python -m unittest discover -s tests -v

# 2. Run the offline regression test suite under optimized Python (`python -O`)
python -O -m unittest discover -s tests -v

# 3. Run the causal portfolio replay & ablation matrix on frozen baseline data
python test_regimes.py --mode replay

# 4. Regenerate the Legacy V5 vs. Audited V6 trade-by-trade comparison JSON
python generate_validation_report.py

# 5. Run read-only HUD monitor mode
python test_regimes.py --mode monitor --clip 150000

# 6. Run dynamic universe discovery scan
python test_regimes.py --mode scan
```
