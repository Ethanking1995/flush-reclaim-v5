# V5 Institutional Flush & Reclaim Engine + Trader Execution Dashboard

Intraday 5-minute **Flush & Reclaim** quantitative strategy engine (`V1` through `V5`), local **Trader Execution Web Dashboard** (FastAPI + React + TradingView Lightweight Charts), and real-time **CLI Execution HUD** (`--live` / `--watch`) designed to exploit morning retail liquidity sweeps (`09:50–13:00 EST`) into institutional/corporate Rule 10b-18 buyback absorption below VWAP.

## Key Features (`V5` Final Production Engine)
1. **Ex-Ante Daily Institutional Accumulation Regime Filter:** Requires 60-day dislocation (`<= -12%`), price above rising `SMA5`, and either 5-day up-volume accumulation (`>= 55%`) or market-bloodbath relative-strength divergence + high 3-day Closing Location Value (`CLV3 >= 0.60`).
2. **14-Day Pre-Earnings Rule 10b-18 Blackout Filter:** Automatically blocks entries within `[ER_Date - 14 calendar days, ER_Date]` ahead of earnings reports.
3. **Intraday `SPY` Waterfall Circuit Breaker:** Pauses new long entries if `SPY` is in an active intraday crash waterfall (`<= -1.00%` from `SPY` morning high and `<= -0.35%` below `SPY` intraday `VWAP`).
4. **Universal Time-of-Day Relative Volume (`ToD_MedVol`) & `VolMA10_ExOpen`:** Compares morning flush volume against both the rolling 15-day time-of-day median volume and the 10-bar rolling average excluding the `09:30` and `09:35` opening auction bars.
5. **Two-Leg Scale-Out + Afternoon `EMA20` Runner + Pre-10b18 Flatten:**
   - **Hard Stop:** `<= -0.35%` below `FlushLow` (capped at `-0.65%` from entry).
   - **Leg 1 (50%):** Scales out at `max(VWAP, EntryPrice * 1.0040)` and moves the stop on Leg 2 to breakeven.
   - **Leg 2 (50%):** Takes `+0.85%` target OR activates the Afternoon Runner (trailing 5m `EMA20` up to `+1.60%` stretch target when the breakout candle closes `>= +0.75%` and above `EMA12`), with a mandatory flatten at `15:40 EST` (10 minutes before the `15:50 EST` Rule 10b-18 buyback cutoff).
6. **Local Web Execution Workstation & Split-Tranche OTOCO Bridge:** FastAPI + SQLite background scanner serving a live SSE React workstation with versioned order plans, clipboard tickets, and optional Tastytrade Split-Tranche OTOCO preflight/submission.

---

## Bringing Up the Application

### Prerequisites
- **Python 3.9+**
- **Node.js 18+ & `npm`** (only needed if rebuilding the `frontend/` bundle)

### 1. Optional Environment Configuration
The dashboard works out of the box with zero credentials in `LIVE_DATA_TRADING_DISABLED`, `PAPER_TRADING`, and deterministic demo modes. To connect the Tastytrade execution bridge, copy `.env.example` to `.env` and populate your credentials:

```bash
cp .env.example .env
```

### 2. One-Command Startup (`./start.sh`)
Run the startup script from the repository root:

```bash
chmod +x start.sh
./start.sh
```

`./start.sh` automatically:
1. Creates `.venv` and installs `requirements.txt` if not already set up
2. Builds the React production bundle (`frontend/dist`) if missing
3. Stops any prior running instance of the dashboard (and picks the next available port if `8000` is already in use)
4. Launches the FastAPI + background worker server on **`http://127.0.0.1:8000`**

**Useful startup options:**
```bash
# Force a fresh frontend production build before starting
./start.sh --build

# Specify a custom host or port
PORT=8080 ./start.sh
```

Once started, open **`http://127.0.0.1:8000`** (or the printed fallback port) in your browser.

### 3. Manual Setup / Frontend Hot-Reload Dev Mode (Optional)
If you are actively editing `frontend/src` and want Vite hot module replacement alongside the FastAPI backend:

```bash
# Terminal 1 — Python backend API & background scanner (127.0.0.1:8000)
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn backend.app:app --host 127.0.0.1 --port 8000 --reload

# Terminal 2 — Vite React dev server (127.0.0.1:5173, proxies /api -> 127.0.0.1:8000)
cd frontend
npm install
npm run dev
```

---

## CLI Trader HUD & Backtest Modes

### 1. Live Trader Execution HUD (Single Snapshot)
```bash
./.venv/bin/python test_regimes.py --live
```

### 2. Auto-Refreshing Terminal HUD (Every 30 Seconds)
```bash
./.venv/bin/python test_regimes.py --watch --clip 150000
```

### 3. Full `V1` vs `V2` vs `V3` vs `V4` vs `V5` Backtest Audit
```bash
./.venv/bin/python test_regimes.py
```

---

## Testing & Architecture Docs

Run the automated test suite (`pytest`):

```bash
./.venv/bin/pytest -v
```

See [DASHBOARD_GUIDE.md](DASHBOARD_GUIDE.md) for detailed architecture notes, Tastytrade execution boundaries, and data-provenance specifications.
