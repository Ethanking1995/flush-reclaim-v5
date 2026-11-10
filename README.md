# V5 Institutional Flush & Reclaim Engine + Trader Execution HUD

Intraday 5-minute **Flush & Reclaim** quantitative backtest engine (`V1` through `V5`) and real-time **Trader Execution HUD** (`--live` / `--watch`) designed to exploit morning retail liquidity sweeps (`09:50–13:00 EST`) into institutional/corporate Rule 10b-18 buyback absorption below VWAP.

## Key Features (`V5` Final Production Engine)
1. **Ex-Ante Daily Institutional Accumulation Regime Filter:** Requires 60-day dislocation (`<= -12%`), price above rising `SMA5`, and either 5-day up-volume accumulation (`>= 55%`) or market-bloodbath relative-strength divergence + high 3-day Closing Location Value (`CLV3 >= 0.60`).
2. **14-Day Pre-Earnings Rule 10b-18 Blackout Filter:** Automatically blocks entries within `[ER_Date - 14 calendar days, ER_Date]` when corporate buyback bots are legally shut off ahead of earnings.
3. **Intraday `SPY` Waterfall Circuit Breaker:** Pauses new long entries if `SPY` is in an active intraday crash waterfall (`<= -1.00%` from `SPY` morning high and `<= -0.35%` below `SPY` intraday `VWAP`).
4. **Universal Time-of-Day Relative Volume (`ToD_MedVol`) & `VolMA10_ExOpen`:** Compares morning flush volume against both the rolling 15-day time-of-day median volume and the 10-bar rolling average excluding the `09:30` and `09:35` opening auction bars.
5. **Two-Leg Scale-Out + Afternoon `EMA20` Runner + Pre-10b18 Flatten:**
   - **Hard Stop:** `<= -0.35%` below `FlushLow` (capped at `-0.65%` from entry).
   - **Leg 1 (50%):** Scales out at `max(VWAP, EntryPrice * 1.0040)` and moves the stop on Leg 2 to breakeven.
   - **Leg 2 (50%):** Takes `+0.85%` target OR activates the Afternoon Runner (trailing 5m `EMA20` up to `+1.60%` stretch target when the breakout candle closes `>= +0.75%` and above `EMA12`), with a mandatory flatten at `15:40 EST` (10 minutes before the `15:50 EST` Rule 10b-18 buyback cutoff).

---

## Quick Start

```bash
./start.sh              # live HUD, refreshes every 30s (sets up .venv/.env on first run)
./start.sh once         # single snapshot
./start.sh backtest     # full V1–V5 backtest
./start.sh watch 2500   # override clip size ($ per trade)
```

### Robinhood automation (Trading MCP)

When a signal fires, the HUD writes the order plan to `pending_orders.json`. In Cursor, ask the
agent to "place pending orders" — it uses the official Robinhood Trading MCP (configured in
`.cursor/mcp.json`) and asks you to confirm each order. Orders only go to your separately funded
Robinhood Agentic account. Connect once via Cursor Settings → Tools & MCPs → `robinhood-trading` → Connect.

### Broker: Robinhood (default)

Robinhood has **no official stock trading REST API**. This repo defaults to `BROKER=robinhood`:
the live HUD still scans and stages signals, then prints a **manual order ticket** for you to
enter in the Robinhood app (entry limit, stop, 50% scale-out, leg-2 target). It will not
auto-submit stock orders.

Set your size in `.env` (`TARGET_CLIP_USD`, risk caps) or pass `--clip 5000` on the CLI.

### 1. Run the Live Trader Execution HUD (Single Snapshot)
```bash
python test_regimes.py --live --clip 5000
```

### 2. Run the Auto-Refreshing Terminal HUD (Every 30 Seconds)
```bash
python test_regimes.py --watch --clip 5000
```

### 3. Run the Full `V1` vs `V2` vs `V3` vs `V4` vs `V5` Backtest Audit
```bash
python test_regimes.py
```

### Tastytrade (optional live routing)

Set `BROKER=tastytrade`, install `tastytrade`, and fill TT credentials in `.env`. Live transmit
still requires `--execute-live` plus an Enter confirmation.
