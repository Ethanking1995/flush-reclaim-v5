# Flush & Reclaim V5 — Private Trader Execution Web Dashboard

This document describes the architecture, operating modes, Tastytrade broker boundaries, data-quality fixes, and verification suite for the **Flush & Reclaim V5** private web application.

---

## 1. Architecture & Shared Engine

The web dashboard and the existing CLI ([`test_regimes.py`](file:///Users/ethanking/Desktop/flush-reclaim/test_regimes.py), [`universe_scanner.py`](file:///Users/ethanking/Desktop/flush-reclaim/universe_scanner.py)) share a single authoritative Python strategy and risk engine:

- **Causal Strategy Engine ([`strategy_engine.py`](file:///Users/ethanking/Desktop/flush-reclaim/strategy_engine.py)):**
  - Evaluates lagged prior-session daily accumulation (`Session_Date < D`), causal 15-session time-of-day median volume (`ToD_MedVol`) and midday median volume (`Midday_MedVol`), `VolMA10_ExOpen`, bar-derived cumulative `SessionVWAP`, `EMA12` / `EMA20`, and completed 5-minute higher-low / hammer reclaim bars.
- **Market Data & Provenance Adapter ([`market_data_adapter.py`](file:///Users/ethanking/Desktop/flush-reclaim/market_data_adapter.py)):**
  - Provides explicit `DataProvenance` (`vendor_id`, `endpoint_id`, `observation_timestamp_et`, `fetch_timestamp_et`, `age_seconds`, `is_delayed`) and compatible-timestamp `ConsistencyCheckResult` (`SAME_PROVIDER_CROSS_ENDPOINT` vs. `INDEPENDENT_PROVIDERS`).
- **Broker Link & Clipboard Ticket Adapter ([`broker_link_adapter.py`](file:///Users/ethanking/Desktop/flush-reclaim/broker_link_adapter.py)):**
  - Documents and enforces verified Tastytrade navigation (`https://my.tastytrade.com/app.html#/trading`) and generates human-verified tabular clipboard tickets without inventing undocumented URL query schemes.
- **Split-Tranche OTOCO Executor ([`tasty_executor.py`](file:///Users/ethanking/Desktop/flush-reclaim/tasty_executor.py)):**
  - Builds and validates Split-Tranche OTOCO bracket orders (`Tranche A Q_1 OTOCO` + `Tranche B Q_2 OTOCO`) so that when Leg 1 (`Q_1`) fills at VWAP, the remaining `Q_2` runner shares retain active stop protection on the broker's servers.
- **Singleton Background Worker & SQLite Store ([`backend/worker.py`](file:///Users/ethanking/Desktop/flush-reclaim/backend/worker.py), [`backend/database.py`](file:///Users/ethanking/Desktop/flush-reclaim/backend/database.py)):**
  - Runs inside the local Python backend process (`127.0.0.1:8000`), persisting signals, versioned order plans, bound broker previews, order intents, positions, and audit events in SQLite (`flush_reclaim_dashboard.db`).
  - Continues running even if all browser tabs are closed (pauses only if the host machine sleeps or the backend process is stopped).
- **Frontend Workstation ([`frontend/src/App.tsx`](file:///Users/ethanking/Desktop/flush-reclaim/frontend/src/App.tsx), [`frontend/src/components/SymbolChart.tsx`](file:///Users/ethanking/Desktop/flush-reclaim/frontend/src/components/SymbolChart.tsx)):**
  - Built with React, TypeScript, and TradingView Lightweight Charts. Connects via Server-Sent Events (`/api/stream`) with full snapshot recovery (`/api/snapshot`) on initial load and reconnect.

---

## 2. Quick Start

### Start the Web Dashboard (Single Command)

```bash
./start_dashboard.sh
```

Then open **`http://127.0.0.1:8000`** in your browser.

### Optional: Frontend Hot-Reload Dev Server

```bash
# Terminal 1: FastAPI backend on 127.0.0.1:8000
./.venv/bin/uvicorn backend.app:app --host 127.0.0.1 --port 8000

# Terminal 2: Vite dev server on 127.0.0.1:5173 (proxies /api to 127.0.0.1:8000)
cd frontend && npm run dev
```

### Run the Existing CLI Interface

```bash
./.venv/bin/python test_regimes.py --clip 150000
```

---

## 3. Tastytrade Actions & Enforced Boundaries

| Action | Behavior | Mutates Broker State? |
| :--- | :--- | :--- |
| **`Review trade`** | Focuses and populates the local Order Review Ticket for an actionable (`READY_FOR_REVIEW`) candidate. | **No** |
| **`Copy ticker`** | Copies the symbol (`FISV`) to the clipboard and logs a non-mutating handoff event. | **No** |
| **`Copy ticket`** | Copies the formatted Split-Tranche OTOCO order plan (shares, limit, stop, Leg 1/Leg 2 targets, BTO/STC, DAY,planned risk) to the clipboard. | **No** |
| **`Open Tastytrade`** | Opens `https://my.tastytrade.com/app.html#/trading` in a new tab. Does **not** prefill OTOCO brackets via undocumented URL params and **never** marks an order submitted or filled. | **No** |
| **`Preview with Tastytrade`** | Validates the current `OrderPlan` version (`dry_run=True` preflight when OAuth is connected, or `LOCAL_SCHEMA_VALIDATION_ONLY` when disconnected) and binds a 90-second expiry. Does **not** create a draft in Tastytrade's UI. | **No** |
| **`Submit to Tastytrade`** | Opens the Final Confirmation Modal and, upon explicit confirmation, revalidates freshness, risk caps, and idempotency before transmitting the Split-Tranche OTOCO pair. | **Yes (Paper or Live depending on Operating Mode)** |

---

## 4. Data-Quality & Correctness Fixes (Section 10)

1. **Removed Fabricated 3-Price "VWAP" from Quote Snapshots:**
   - Previously, `fetch_secondary_quotes` in `tasty_executor.py` computed `"vwap": round((high_p + low_p + last_p) / 3.0, 4)` from daily high/low/last and returned it as `"vwap"`. That was a single-tick typical price, not a volume-weighted average price.
   - Fixed in [`tasty_executor.py`](file:///Users/ethanking/Desktop/flush-reclaim/tasty_executor.py) and [`market_data_adapter.py`](file:///Users/ethanking/Desktop/flush-reclaim/market_data_adapter.py): Session VWAP is now exclusively computed from cumulative intraday RTH 5-minute bars ($\sum \text{TP}_i \cdot V_i / \sum V_i$ including the 09:30 and 09:35 opening bars, while `VolMA10_ExOpen` separately excludes 09:30/09:35).
2. **Honest Feed Provenance (`SAME_PROVIDER_CROSS_ENDPOINT` vs. `INDEPENDENT_PROVIDERS`):**
   - Comparing CNBC quote snapshots against CNBC Harmony bars checks cross-endpoint consistency from the same upstream provider (`vendor_id="cnbc"`), not independent market-data vendors. [`market_data_adapter.py`](file:///Users/ethanking/Desktop/flush-reclaim/market_data_adapter.py) labels this `SAME_PROVIDER_CROSS_ENDPOINT` and only compares observations with compatible timestamps.
3. **Strategy 14-Day Pre-Earnings Rule Wording:**
   - Candidates within 14 calendar days of earnings (such as `NU` reporting on `2026-11-12`) are labeled as blocked by the **Strategy 14-day pre-earnings rule**, never as a universal corporate buyback blackout law.
4. **User-Reported `FISV` Workflow Example Isolation:**
   - The user-reported `FISV` trade (`$62.72` entry, `50%` scaled at `$63.18`, stop moved to `$63.50`, `~$3,000` reported profit) is stored and displayed as `MANUAL_USER_REPORTED` (`UNRECONCILED_USER_REPORTED`) without inventing unverified share quantities, timestamps, or realized/unrealized splits.

---

## 5. Automated Verification Suite

Run the full 31-test automated verification suite (`15` audited engine/executor tests + `16` web dashboard acceptance & regression tests):

```bash
./.venv/bin/pytest -v
```
