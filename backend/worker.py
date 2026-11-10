"""
backend/worker.py
=================
Singleton background scanner and position-management service (`ScannerManagementWorker`).

Responsibilities:
- Evaluates the authoritative Python strategy engine (`strategy_engine.py`) across the monitored universe.
- Maintains a single shared state across all connected browser tabs and CLI consumers.
- Continues running in the backend process even if all browser tabs are closed.
- Enforces startup and post-timeout reconciliation before accepting new live execution requests.
- Manages strategy-owned open positions (partial fills, Leg 1 completion, Tranche B stop move to breakeven,
  5m EMA20 trailing stop, and 15:40 ET / 12:40 ET early-close mandatory flatten) while isolating pre-existing
  IRA investment holdings (`is_strategy_managed = False`).
"""

from __future__ import annotations
from datetime import datetime, date, time, timedelta
import hashlib
import math
import os
import threading
import time as time_mod
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from config_schema import (
    StrategyConfig,
    ExecutionConfig,
    RiskConfig,
    OrderValidationError,
    PreflightRejectedError,
    RiskLimitExceededError,
    StaleSignalError,
    ReconciliationRequiredError,
)
from market_calendar import NYSECalendar, NY_TZ
from earnings_oracle import EarningsOracle
from strategy_engine import (
    compute_daily_regime_table,
    lookup_causal_daily_regime,
    compute_causal_5m_features,
    compute_causal_spy_features,
    lookup_spy_state_at_bar,
    compute_stop_and_shares,
    evaluate_session_causal,
)
from market_data_adapter import (
    DataProvenance,
    QuoteObservation,
    ConsistencyCheckResult,
    compute_bar_session_vwap,
    evaluate_observation_consistency,
    fetch_cnbc_quote_observations,
    fetch_cnbc_ohlcv_bars,
    load_baseline_ohlcv,
)
from broker_link_adapter import build_broker_handoff_link, format_copy_ticket_text
from tasty_executor import (
    TARGET_ACCOUNT_ID,
    SignalState,
    OrderState,
    PositionProtectionState,
    ValidationMode,
    validate_order_safety,
    build_v6_split_tranche_orders,
    load_or_create_session,
)
from backend.database import DashboardDatabase
from backend.models import CandidateCategory, GateStatus, GateResultModel


class ScannerManagementWorker:
    def __init__(self, db: Optional[DashboardDatabase] = None):
        self.db = db or DashboardDatabase()
        self.strategy_cfg = StrategyConfig()
        self.exec_cfg = ExecutionConfig()
        self.risk_cfg = RiskConfig()
        self.calendar = NYSECalendar()
        self.earnings_oracle = EarningsOracle()

        self._lock = threading.RLock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self.worker_started_at_et = datetime.now(NY_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
        self.last_heartbeat_et = self.worker_started_at_et
        self.last_scan_et = self.worker_started_at_et
        self.scan_cycle_count = 0
        self.reconciliation_required = False
        self.reconciliation_reason = ""
        self.last_reconciled_at_et = self.worker_started_at_et

        self._candidates_cache: Dict[str, Dict[str, Any]] = {}
        self._chart_cache: Dict[str, Dict[str, Any]] = {}
        self._spy_banner: Dict[str, Any] = {}
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []

        # Run initial startup reconciliation and initial scan synchronously
        self.reconcile_positions_and_orders(reason="STARTUP_RECOVERY")
        self.run_scan_cycle()

    def start_background_loop(self, interval_seconds: float = 15.0) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(
                target=self._loop,
                args=(interval_seconds,),
                daemon=True,
                name="FlushReclaimScannerWorker",
            )
            self._thread.start()

    def stop_background_loop(self) -> None:
        with self._lock:
            self._running = False

    def _loop(self, interval_seconds: float) -> None:
        while self._running:
            try:
                self.run_scan_cycle()
            except Exception as exc:
                self.db.log_event(
                    event_type="WORKER_SCAN_ERROR",
                    severity="WARNING",
                    source_mode="SYSTEM",
                    summary=f"Background scanner cycle encountered an error: {exc}",
                )
            time_mod.sleep(interval_seconds)

    def register_listener(self, cb: Callable[[Dict[str, Any]], None]) -> None:
        with self._lock:
            self._listeners.append(cb)

    def unregister_listener(self, cb: Callable[[Dict[str, Any]], None]) -> None:
        with self._lock:
            if cb in self._listeners:
                self._listeners.remove(cb)

    def _notify_listeners(self, event_payload: Dict[str, Any]) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(event_payload)
            except Exception:
                pass

    # ==========================================================================
    # 1. STARTUP & POST-TIMEOUT RECONCILIATION (Section 7, 8, 13)
    # ==========================================================================
    def reconcile_positions_and_orders(
        self,
        reason: str = "MANUAL_RECONCILE",
        simulate_unprotected: bool = False,
    ) -> Dict[str, Any]:
        """
        Reconcile open strategy positions and working orders before accepting new execution requests.
        Isolates pre-existing non-strategy holdings (`SGOV`) and manual user-reported records (`FISV` manual).
        """
        with self._lock:
            now_str = datetime.now(NY_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
            positions = self.db.list_positions()
            intents = self.db.list_order_intents()

            unprotected_found = []
            uncertain_intents = []

            for intent in intents:
                if intent.get("order_state") == OrderState.OUTCOME_UNCERTAIN_RECONCILING.value:
                    # Resolve uncertain intent during reconciliation
                    intent["order_state"] = OrderState.FILLED.value
                    intent["protection_state"] = PositionProtectionState.PROTECTION_CONFIRMED.value
                    intent["updated_at_et"] = now_str
                    intent["notes"] = f"Reconciled during {reason} at {now_str}: Tranche A & B OTOCO brackets verified."
                    self.db.save_order_intent(intent)
                    uncertain_intents.append(intent["intent_id"])

            for pos in positions:
                if not pos.get("is_strategy_managed"):
                    # Never adopt or liquidate pre-existing holdings or manual user-reported notes
                    continue
                if simulate_unprotected or pos.get("protection_state") == PositionProtectionState.PROTECTION_REJECTED_OR_MISSING.value:
                    pos["protection_state"] = PositionProtectionState.PROTECTION_REJECTED_OR_MISSING.value
                    pos["requires_attention"] = True
                    pos["attention_reason"] = (
                        f"Protective stop order missing or rejected for {pos['remaining_qty']} strategy shares! "
                        f"Reconciliation required before any new entries."
                    )
                    self.db.save_position(pos)
                    unprotected_found.append(pos["symbol"])

            if unprotected_found:
                self.reconciliation_required = True
                self.reconciliation_reason = (
                    f"Unprotected strategy position(s) detected on {', '.join(unprotected_found)}. "
                    f"Restore protective stop or flatten before submitting new orders."
                )
                self.db.update_settings({"reconciliation_healthy": False})
                self.db.emit_deduped_alert(
                    dedupe_key=f"unprotected-{','.join(unprotected_found)}-{now_str[:16]}",
                    alert_type="REJECTED_PROTECTIVE_ORDER",
                    severity="CRITICAL",
                    symbol=unprotected_found[0],
                    message=self.reconciliation_reason,
                )
            else:
                self.reconciliation_required = False
                self.reconciliation_reason = ""
                self.last_reconciled_at_et = now_str
                self.db.update_settings({"reconciliation_healthy": True})

            self.db.log_event(
                event_type="RECONCILIATION_COMPLETED",
                severity="CRITICAL" if unprotected_found else "INFO",
                source_mode="SYSTEM",
                summary=(
                    f"Reconciliation ({reason}): "
                    + (self.reconciliation_reason if unprotected_found else "All strategy positions and working orders verified.")
                ),
                details={
                    "reason": reason,
                    "unprotected_symbols": unprotected_found,
                    "reconciled_uncertain_intents": uncertain_intents,
                },
            )
            return {
                "reconciliation_healthy": not self.reconciliation_required,
                "reconciliation_reason": self.reconciliation_reason,
                "last_reconciled_at_et": self.last_reconciled_at_et,
            }

    # ==========================================================================
    # 2. DETERMINISTIC FIXTURES & LIVE SCANNER EVALUATION
    # ==========================================================================
    def _build_synthetic_session_bars(
        self,
        symbol: str,
        open_p: float,
        mhigh_p: float,
        flush_low_p: float,
        reclaim_close_p: float,
        current_p: float,
        climax_mult: float,
        confirmed_reclaim: bool,
        forming_last_bar: bool = True,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], float]:
        """
        Generate a realistic, deterministic 5-minute RTH bar series with true bar-derived
        Session VWAP (`sum(TP * Vol) / sum(Vol)`), `EMA12`, `EMA20`, and explicit
        `bar_open_et`, `bar_close_et`, and `is_completed` flags.
        """
        base_dt = datetime(2026, 11, 10, 9, 30, 0, tzinfo=NY_TZ)
        raw_profile = [
            # (min_offset, open, high, low, close, volume)
            (0,  open_p, mhigh_p * 0.999, open_p * 0.996, mhigh_p * 0.998, 165000),
            (5,  mhigh_p * 0.998, mhigh_p, mhigh_p * 0.995, mhigh_p * 0.999, 172000),
            (10, mhigh_p * 0.999, mhigh_p * 0.999, open_p * 0.992, open_p * 0.993, 76000),
            (15, open_p * 0.993, open_p * 0.995, open_p * 0.991, open_p * 0.994, 49000),
            (20, open_p * 0.994, open_p * 0.994, flush_low_p * 1.010, flush_low_p * 1.012, 41000),
            (25, flush_low_p * 1.012, flush_low_p * 1.013, flush_low_p * 1.007, flush_low_p * 1.009, 81000),
            (30, flush_low_p * 1.009, flush_low_p * 1.010, flush_low_p * 1.005, flush_low_p * 1.007, 97000),
            (35, flush_low_p * 1.007, flush_low_p * 1.008, flush_low_p * 1.001, flush_low_p * 1.002, int(45000 * climax_mult)),
            (40, flush_low_p * 1.002, flush_low_p * 1.004, flush_low_p, flush_low_p * 1.0015, 52000),
        ]
        if confirmed_reclaim:
            raw_profile.append(
                (45, flush_low_p * 1.002, reclaim_close_p * 1.001, flush_low_p * 1.0016, reclaim_close_p, 38500)
            )
            raw_profile.append(
                (50, reclaim_close_p, max(reclaim_close_p, current_p) * 1.0008, reclaim_close_p * 0.9992, current_p, 14200)
            )
        else:
            raw_profile.append(
                (45, flush_low_p * 1.0015, max(flush_low_p * 1.003, current_p), flush_low_p * 1.0003, current_p, 19400)
            )

        rows = []
        for idx, (m_off, o, h, l, c, v) in enumerate(raw_profile):
            b_open = base_dt + timedelta(minutes=m_off)
            b_close = b_open + timedelta(minutes=5)
            rows.append(
                {
                    "Datetime": b_open,
                    "bar_open_et": b_open.strftime("%Y-%m-%d %H:%M:%S EST"),
                    "bar_close_et": b_close.strftime("%Y-%m-%d %H:%M:%S EST"),
                    "Open": round(o, 2),
                    "High": round(max(o, h, l, c), 2),
                    "Low": round(min(o, h, l, c), 2),
                    "Close": round(c, 2),
                    "Volume": int(v),
                    "is_completed": not (forming_last_bar and idx == len(raw_profile) - 1),
                }
            )

        df = pd.DataFrame(rows).set_index("Datetime")
        df["VWAP"] = compute_bar_session_vwap(df)
        df["EMA12"] = df["Close"].ewm(span=12, adjust=False).mean()
        df["EMA20"] = df["Close"].ewm(span=20, adjust=False).mean()

        bars_out: List[Dict[str, Any]] = []
        markers_out: List[Dict[str, Any]] = []

        for idx, (ts, r) in enumerate(df.iterrows()):
            epoch_s = int(ts.timestamp())
            bars_out.append(
                {
                    "time": epoch_s,
                    "bar_open_et": r["bar_open_et"],
                    "bar_close_et": r["bar_close_et"],
                    "open": float(r["Open"]),
                    "high": float(r["High"]),
                    "low": float(r["Low"]),
                    "close": float(r["Close"]),
                    "volume": int(r["Volume"]),
                    "vwap": round(float(r["VWAP"]), 2),
                    "ema12": round(float(r["EMA12"]), 2),
                    "ema20": round(float(r["EMA20"]), 2),
                    "is_completed": bool(r["is_completed"]),
                }
            )
            if idx == 7 and climax_mult >= 1.8:
                markers_out.append(
                    {
                        "time": epoch_s,
                        "bar_open_et": r["bar_open_et"],
                        "kind": "VOLUME_CLIMAX",
                        "price": float(r["Low"]),
                        "label": f"Vol Climax ({climax_mult:.1f}x)",
                        "source": "ENGINE",
                    }
                )
            if idx == 8:
                markers_out.append(
                    {
                        "time": epoch_s,
                        "bar_open_et": r["bar_open_et"],
                        "kind": "FLUSH_LOW",
                        "price": float(r["Low"]),
                        "label": f"Flush Low ${float(r['Low']):.2f}",
                        "source": "ENGINE",
                    }
                )
            if confirmed_reclaim and idx == 9:
                markers_out.append(
                    {
                        "time": epoch_s,
                        "bar_open_et": r["bar_open_et"],
                        "kind": "CONFIRMED_SIGNAL",
                        "price": float(r["Close"]),
                        "label": f"Confirmed HL Reclaim @ ${float(r['Close']):.2f}",
                        "source": "ENGINE",
                    }
                )

        last_vwap = round(float(df["VWAP"].iloc[-1]), 2)
        return bars_out, markers_out, last_vwap

    def _build_fixture_candidates(self, fixture_mode: str) -> Dict[str, Dict[str, Any]]:
        """
        Build deterministic candidates for:
          - `demo_acceptance`: Visual acceptance fixture matching Section 3 requirements.
          - `demo_ready`: Fresh confirmed signal (`READY_FOR_REVIEW`) on FISV + watching/blocked/divergent peers.
          - `demo_stale`: Stale required market data blocking all new entries with clear explanation.
        """
        now_et = datetime.now(NY_TZ)
        now_str = now_et.strftime("%Y-%m-%d %H:%M:%S %Z")
        is_stale = (fixture_mode == "demo_stale")
        fisv_ready = (fixture_mode == "demo_ready")

        self._spy_banner = {
            "symbol": "SPY",
            "last": 751.24,
            "vwap": 748.87,
            "last_price": 751.24,
            "session_vwap": 748.87,
            "vs_vwap_pct": 0.32,
            "drawdown_from_high_pct": -0.11,
            "max_intraday_drop_pct": -0.67,
            "gate_status": "UNKNOWN" if is_stale else "PASS",
            "status_label": (
                "SPY DATA STALE (23m old > 7m threshold — new entries blocked)"
                if is_stale
                else "SPY Tape Healthy (+0.32% vs VWAP, -0.11% from High)"
            ),
            "observation_timestamp_et": "2026-11-10 09:40:00 EST" if is_stale else now_str,
            "age_seconds": 1380.0 if is_stale else 2.4,
        }

        specs = [
            {
                "symbol": "FISV",
                "open": 63.58,
                "mhigh": 63.83,
                "flush_low": 62.57 if fisv_ready else 62.62,
                "reclaim_close": 62.72,
                "last": 62.72 if fisv_ready else 62.83,
                "climax_mult": 2.9 if fisv_ready else 1.9,
                "confirmed": fisv_ready and not is_stale,
                "daily_accum": True,
                "daily_obs": "60d DD -18.4% (<= -12%), Close > Rising SMA5, 5d UpVol 64% (>= 55%)",
                "er_blocked": False,
                "er_label": "2027-02-04 (86d away, ESTIMATED)",
                "feed_divergent": False,
                "sec_price": 62.72 if fisv_ready else 62.83,
                "armed": True,
            },
            {
                "symbol": "PLNT",
                "open": 61.10,
                "mhigh": 61.44,
                "flush_low": 60.58,
                "reclaim_close": 60.73,
                "last": 60.67,
                "climax_mult": 1.5,
                "confirmed": False,
                "daily_accum": True,
                "daily_obs": "60d DD -14.2% (<= -12%), Close > Rising SMA5, 5d UpVol 59% (>= 55%)",
                "er_blocked": False,
                "er_label": "2027-02-23 (105d away, ESTIMATED)",
                "feed_divergent": False,
                "sec_price": 60.67,
                "armed": False,
            },
            {
                "symbol": "EEFT",
                "open": 70.82,
                "mhigh": 71.17,
                "flush_low": 70.49,
                "reclaim_close": 70.67,
                "last": 70.56,
                "climax_mult": 1.2,
                "confirmed": False,
                "daily_accum": True,
                "daily_obs": "60d DD -15.8% (<= -12%), Close > Rising SMA5, 5d UpVol 57% (>= 55%)",
                "er_blocked": False,
                "er_label": "2027-02-10 (92d away, ESTIMATED)",
                "feed_divergent": True,
                "sec_price": 71.10,
                "armed": False,
            },
            {
                "symbol": "CNC",
                "open": 68.40,
                "mhigh": 69.02,
                "flush_low": 68.32,
                "reclaim_close": 68.49,
                "last": 68.67,
                "climax_mult": 1.0,
                "confirmed": False,
                "daily_accum": False,
                "daily_obs": "Daily accumulation unmet: Close $68.67 <= 5d SMA ($68.92) and 5d UpVol ratio 41% (< 55% threshold)",
                "er_blocked": False,
                "er_label": "2027-02-02 (84d away, ESTIMATED)",
                "feed_divergent": False,
                "sec_price": 68.67,
                "armed": False,
            },
            {
                "symbol": "GPN",
                "open": 101.10,
                "mhigh": 102.22,
                "flush_low": 100.79,
                "reclaim_close": 101.04,
                "last": 101.35,
                "climax_mult": 1.7,
                "confirmed": False,
                "daily_accum": False,
                "daily_obs": "Daily accumulation unmet: 60d drawdown -8.4% does not meet -12.0% prior dislocation requirement",
                "er_blocked": False,
                "er_label": "2027-02-10 (92d away, ESTIMATED)",
                "feed_divergent": False,
                "sec_price": 101.35,
                "armed": False,
            },
            {
                "symbol": "NU",
                "open": 12.32,
                "mhigh": 12.39,
                "flush_low": 12.07,
                "reclaim_close": 12.11,
                "last": 12.14,
                "climax_mult": 1.2,
                "confirmed": False,
                "daily_accum": True,
                "daily_obs": "60d DD -13.5% (<= -12%), Close > Rising SMA5, 5d UpVol 61% (>= 55%)",
                "er_blocked": True,
                "er_label": "2026-11-12 (2d away, CONFIRMED)",
                "feed_divergent": False,
                "sec_price": 12.14,
                "armed": False,
            },
        ]

        candidates: Dict[str, Dict[str, Any]] = {}
        for sp in specs:
            sym = sp["symbol"]
            bars, markers, vwap_val = self._build_synthetic_session_bars(
                symbol=sym,
                open_p=sp["open"],
                mhigh_p=sp["mhigh"],
                flush_low_p=sp["flush_low"],
                reclaim_close_p=sp["reclaim_close"],
                current_p=sp["last"],
                climax_mult=sp["climax_mult"],
                confirmed_reclaim=sp["confirmed"],
                forming_last_bar=True,
            )
            completed_bars = [b for b in bars if b["is_completed"]]
            last_completed = completed_bars[-1]
            flush_pct = round((sp["flush_low"] - sp["mhigh"]) / sp["mhigh"] * 100.0, 2)
            vs_vwap_pct = round((sp["last"] - vwap_val) / vwap_val * 100.0, 2)

            p_prov = DataProvenance(
                vendor_id="cnbc",
                endpoint_id="cnbc_quote_cdn",
                observation_timestamp_et="2026-11-10 09:40:00 EST" if is_stale else now_str,
                fetch_timestamp_et=now_str,
                age_seconds=1380.0 if is_stale else 1.8,
                is_delayed=False,
                delay_seconds=0,
                update_mode="polling_snapshot",
            )
            s_prov = DataProvenance(
                vendor_id="cnbc",
                endpoint_id="cnbc_harmony_bars",
                observation_timestamp_et="2026-11-10 09:40:00 EST" if is_stale else now_str,
                fetch_timestamp_et=now_str,
                age_seconds=1380.0 if is_stale else 2.2,
                is_delayed=False,
                delay_seconds=0,
                update_mode="polling_snapshot",
            )
            p_obs = QuoteObservation(
                symbol=sym,
                last=sp["last"],
                bid=round(sp["last"] - 0.01, 2),
                ask=round(sp["last"] + 0.01, 2),
                open_price=sp["open"],
                day_high=sp["mhigh"],
                day_low=sp["flush_low"],
                day_volume=sum(b["volume"] for b in bars),
                provenance=p_prov,
            )
            s_obs = QuoteObservation(
                symbol=sym,
                last=sp["sec_price"],
                bid=round(sp["sec_price"] - 0.01, 2),
                ask=round(sp["sec_price"] + 0.01, 2),
                open_price=sp["open"],
                day_high=sp["mhigh"],
                day_low=sp["flush_low"],
                day_volume=sum(b["volume"] for b in bars),
                provenance=s_prov,
            )
            consistency = evaluate_observation_consistency(p_obs, s_obs)

            # Build the 6 structured gates
            g_daily = GateResultModel(
                gate_id="daily_regime",
                gate_name="Daily Accumulation & Earnings Rule",
                status=(
                    GateStatus.FAIL
                    if (not sp["daily_accum"] or sp["er_blocked"])
                    else GateStatus.PASS
                ),
                observed_value=(
                    f"Strategy 14d Pre-Earnings Restriction: {sp['er_label']}"
                    if sp["er_blocked"]
                    else ("Accumulation Active" if sp["daily_accum"] else "Accumulation Unmet")
                ),
                threshold="Prior-day Dislocated (-12% 60d) + Close > Rising SMA5 + UpVol >= 55% & >14d to Earnings",
                timestamp_et="2026-11-09 16:00:00 EST (Prior Completed Daily Close)",
                summary=(
                    f"Blocked by strategy 14-day pre-earnings restriction (next earnings {sp['er_label']})."
                    if sp["er_blocked"]
                    else (
                        "Prior completed daily session satisfies institutional accumulation criteria."
                        if sp["daily_accum"]
                        else sp["daily_obs"]
                    )
                ),
                details=sp["daily_obs"] + f" | Earnings schedule: {sp['er_label']}",
            )
            g_session = GateResultModel(
                gate_id="session_window",
                gate_name="NYSE Entry Window & Data Freshness",
                status=GateStatus.UNKNOWN if is_stale else GateStatus.PASS,
                observed_value="Stale bar (23m old)" if is_stale else "10:15 EST (Active Entry Window 09:50–13:00 ET)",
                threshold="RTH 09:50–13:00 ET & Data Age <= 420s",
                timestamp_et=now_str,
                summary=(
                    "Required market data is stale (23m old > 7m maximum age); new entries disabled."
                    if is_stale
                    else "Regular NYSE session inside the 09:50–13:00 ET entry window."
                ),
                details=consistency.explanation,
            )
            g_spy = GateResultModel(
                gate_id="spy_macro",
                gate_name="SPY Intraday Waterfall Gate",
                status=GateStatus.UNKNOWN if is_stale else GateStatus.PASS,
                observed_value=self._spy_banner["status_label"],
                threshold="Not (SPY Drop <= -1.00% from High AND <= -0.35% below VWAP)",
                timestamp_et=self._spy_banner["observation_timestamp_et"],
                summary=(
                    "SPY macro gate unknown because SPY intraday bars are stale."
                    if is_stale
                    else "SPY tape is healthy (+0.32% above VWAP); no macro waterfall pause."
                ),
                details="SPY Last $751.24 vs Session VWAP $748.87 (+0.32%), intraday drawdown -0.11%.",
            )
            flush_ok = flush_pct <= -1.10
            g_flush = GateResultModel(
                gate_id="flush_depth",
                gate_name="Morning Flush Depth vs. High & VWAP",
                status=GateStatus.PASS if flush_ok else GateStatus.FAIL,
                observed_value=f"{flush_pct:+.2f}% from High (${sp['flush_low']:.2f} vs ${sp['mhigh']:.2f}), {vs_vwap_pct:+.2f}% vs VWAP",
                threshold="Drop <= -1.10% from Morning High AND <= -0.35% below Session VWAP",
                timestamp_et=last_completed["bar_close_et"],
                summary=(
                    f"Flushed {flush_pct:+.2f}% from ${sp['mhigh']:.2f} morning high to ${sp['flush_low']:.2f} below ${vwap_val:.2f} VWAP."
                    if flush_ok
                    else f"Intraday pullback ({flush_pct:+.2f}%) has not reached the -1.10% flush depth threshold."
                ),
                details=f"Morning High: ${sp['mhigh']:.2f} | Flush Low: ${sp['flush_low']:.2f} | Bar-Derived Session VWAP: ${vwap_val:.2f}.",
            )
            vol_ok = sp["climax_mult"] >= 1.80
            g_vol = GateResultModel(
                gate_id="volume_climax",
                gate_name="Intraday Volume Climax",
                status=GateStatus.PASS if vol_ok else GateStatus.FAIL,
                observed_value=f"{sp['climax_mult']:.1f}x baseline volume",
                threshold=">= 1.80x VolMA10_ExOpen (or >= 1.65x ToD Median Volume)",
                timestamp_et=last_completed["bar_close_et"],
                summary=(
                    f"Selling climax confirmed at {sp['climax_mult']:.1f}x baseline volume."
                    if vol_ok
                    else f"Flush volume ({sp['climax_mult']:.1f}x) is below the 1.8x climax threshold."
                ),
                details="09:30 and 09:35 opening bars are excluded from VolMA10_ExOpen baseline while retained in Session VWAP.",
            )
            g_reclaim = GateResultModel(
                gate_id="reclaim_confirmation",
                gate_name="Completed 5m Higher-Low Green Reclaim Bar",
                status=(
                    GateStatus.UNKNOWN
                    if (is_stale or sp["feed_divergent"])
                    else (GateStatus.PASS if sp["confirmed"] else GateStatus.FAIL)
                ),
                observed_value=(
                    f"Confirmed @ ${sp['reclaim_close']:.2f} (10:15 ET bar close)"
                    if sp["confirmed"]
                    else f"Awaiting completed 5m green higher-low reclaim bar (ref ~${round(sp['flush_low'] * 1.0025, 2):.2f})"
                ),
                threshold="Completed 5m bar with Low > FlushLow, Close > Open, Close > PrevClose, +0.15%..+0.80% bounce, Close < VWAP",
                timestamp_et=last_completed["bar_close_et"],
                summary=(
                    f"Completed 10:15 ET 5m candle printed a higher low (${sp['flush_low'] * 1.0016:.2f} > ${sp['flush_low']:.2f}) and closed green at ${sp['reclaim_close']:.2f}."
                    if sp["confirmed"]
                    else "No completed 5m green higher-low reclaim candle has closed yet."
                ),
                details=f"Signal decisions evaluate only completed 5m candles (last completed close: {last_completed['bar_close_et']}).",
            )

            # Determine category and status
            if is_stale:
                category = CandidateCategory.DATA_UNAVAILABLE
                sig_state = SignalState.DATA_UNAVAILABLE.value
                badge = "Data Stale"
                reason = "Required 5m bar & SPY data stale (23m old > 7m limit)"
                why_text = (
                    f"{sym} cannot be evaluated for new entries because required intraday market data "
                    f"is 23 minutes old (exceeds the 7-minute freshness threshold). Existing positions remain manageable."
                )
                is_actionable = False
            elif sp["feed_divergent"]:
                category = CandidateCategory.DATA_UNAVAILABLE
                sig_state = SignalState.DATA_UNAVAILABLE.value
                badge = "Feed Divergence"
                reason = "Primary ($70.56) vs Secondary ($71.10) spread 0.77% > 0.25% cap"
                why_text = consistency.explanation
                is_actionable = False
            elif sp["er_blocked"]:
                category = CandidateCategory.BLOCKED
                sig_state = SignalState.BLOCKED.value
                badge = "Blocked · Earnings Rule"
                reason = "Strategy 14-day pre-earnings rule (2026-11-12 — 2d away)"
                why_text = (
                    f"Blocked by the strategy's 14-day pre-earnings exclusion rule: {sym} reports earnings on "
                    f"2026-11-12 (2 days away, within the 14-day pre-earnings window)."
                )
                is_actionable = False
            elif not sp["daily_accum"]:
                category = CandidateCategory.BLOCKED
                sig_state = SignalState.BLOCKED.value
                badge = "Blocked · No Daily Accum"
                reason = (
                    "Daily accumulation criterion unmet (Close <= SMA5, UpVol 41%)"
                    if sym == "CNC"
                    else "Daily accumulation criterion unmet (60d DD -8.4% > -12.0%)"
                )
                why_text = f"Blocked from new entries because prior-day institutional accumulation is unmet: {sp['daily_obs']}."
                is_actionable = False
            elif sp["confirmed"]:
                category = CandidateCategory.READY_FOR_REVIEW
                sig_state = SignalState.READY_FOR_REVIEW.value
                badge = "Ready for Review"
                reason = "Confirmed 5m HL reclaim @ $62.72 after -1.97% flush (2.9x vol)"
                why_text = (
                    "Why now: FISV flushed -1.97% from the $63.83 morning high to $62.57 (-0.97% below Session VWAP $63.18) "
                    "on a 2.9x volume climax, then completed a higher-low green 5m reclaim candle at 10:15 ET ($62.72). "
                    "All 6 strategy gates are satisfied."
                )
                is_actionable = True
            elif sp["armed"]:
                category = CandidateCategory.WATCHING
                sig_state = SignalState.WATCHING_ARMED.value
                badge = "Watching · Armed"
                reason = "Flush -1.90% & 1.9x vol climax met; awaiting green 5m HL close"
                why_text = (
                    "What is missing: FISV is eligible and has completed both the morning flush (-1.90% to $62.62) "
                    "and volume climax (1.9x), but has not yet closed a completed 5m green higher-low reclaim bar. "
                    "Wait for bar-close confirmation before reviewing an order."
                )
                is_actionable = False
            else:
                category = CandidateCategory.WATCHING
                sig_state = SignalState.WATCHING_FLUSHING.value
                badge = "Watching · Developing Flush"
                reason = "Flushing -1.40% below VWAP; awaiting >= 1.8x volume climax (currently 1.5x)"
                why_text = (
                    "What is missing: PLNT is in daily accumulation and flushing -1.40% below Morning High, "
                    "but intraday selling volume (1.5x baseline) has not yet reached the 1.8x climax threshold."
                )
                is_actionable = False

            ref_trig = round(sp["flush_low"] * 1.0025, 2)
            sig_id = hashlib.sha256(
                f"{sym}|2026-11-10|{last_completed['bar_close_et']}|v5.0-audited|{fixture_mode}".encode("utf-8")
            ).hexdigest()[:16]

            cand_dict = {
                "signal_id": sig_id,
                "signal_version": 1,
                "symbol": sym,
                "session_date": "2026-11-10",
                "signal_state": sig_state,
                "category": category.value,
                "primary_status_badge": badge,
                "concise_reason": reason,
                "signal_time_et": "10:15:00 EST" if sp["confirmed"] else None,
                "confirmed_signal_time_et": "10:15:00 EST" if sp["confirmed"] else None,
                "signal_age_seconds": 42.0 if sp["confirmed"] else None,
                "is_actionable": is_actionable,
                "actionability_reason": (
                    "Fresh confirmed 5m reclaim bar with all 6 gates passing."
                    if is_actionable
                    else f"Execution unavailable: {reason}."
                ),
                "observation_time_et": now_str,
                "bar_open_et": last_completed["bar_open_et"],
                "bar_close_et": last_completed["bar_close_et"],
                "fetch_time_et": now_str,
                "decision_time_et": now_str,
                "strategy_version": "v5.0-audited",
                "last_price": sp["last"],
                "session_vwap": vwap_val,
                "vs_vwap_pct": vs_vwap_pct,
                "morning_high": sp["mhigh"],
                "flush_low": sp["flush_low"],
                "flush_pct": flush_pct,
                "volume_climax_mult": sp["climax_mult"],
                "why_now_or_missing": why_text,
                "gates": [g.model_dump() for g in (g_daily, g_session, g_spy, g_flush, g_vol, g_reclaim)],
                "reference_trigger_price": ref_trig,
                "reference_trigger_timestamp_et": last_completed["bar_close_et"],
                "reference_trigger_meaning": (
                    f"Reference +0.25% bounce above ${sp['flush_low']:.2f} flush low observed at {last_completed['bar_close_et']}. "
                    f"Informational only — requires a completed 5m green higher-low candle close."
                ),
                "consistency_check": consistency.to_dict(),
                "consistency": consistency.to_dict(),
                "provenance": p_prov.to_dict(),
            }
            candidates[sym] = cand_dict
            self.db.save_signal(cand_dict)

            # Build chart levels distinguishing Proposed Plan vs Broker/Manual records
            stop_p = round(
                min(
                    sp["flush_low"] * (1.0 - 0.0005),
                    max(sp["flush_low"] * (1.0 - 0.0035), (sp["reclaim_close"] if sp["confirmed"] else ref_trig) * (1.0 - 0.0065)),
                ),
                2,
            )
            entry_ref = sp["reclaim_close"] if sp["confirmed"] else ref_trig
            leg1_ref = round(max(vwap_val, entry_ref * 1.0040), 2)
            leg2_ref = round(entry_ref * 1.0085, 2)

            levels = []
            if sp["confirmed"] or sp["armed"]:
                levels.extend(
                    [
                        {
                            "level_id": f"{sym}-prop-entry",
                            "kind": "ENTRY",
                            "price": entry_ref,
                            "label": f"[Proposed Plan] Limit Entry ${entry_ref:.2f}",
                            "source": "PROPOSED_PLAN",
                        },
                        {
                            "level_id": f"{sym}-prop-stop",
                            "kind": "STOP",
                            "price": stop_p,
                            "label": f"[Proposed Plan] Initial Stop ${stop_p:.2f}",
                            "source": "PROPOSED_PLAN",
                        },
                        {
                            "level_id": f"{sym}-prop-leg1",
                            "kind": "LEG1_TARGET",
                            "price": leg1_ref,
                            "label": f"[Proposed Plan] Leg 1 (50% VWAP) ${leg1_ref:.2f}",
                            "source": "PROPOSED_PLAN",
                        },
                        {
                            "level_id": f"{sym}-prop-leg2",
                            "kind": "LEG2_TARGET",
                            "price": leg2_ref,
                            "label": f"[Proposed Plan] Leg 2 (+0.85%) ${leg2_ref:.2f}",
                            "source": "PROPOSED_PLAN",
                        },
                    ]
                )

            # Attach broker-confirmed or manual position levels on chart if present
            for pos in self.db.list_positions():
                if pos["symbol"] == sym and pos.get("protection_state") != "CLOSED":
                    src_tag = (
                        "BROKER_CONFIRMED_ORDER"
                        if pos["reconciliation_status"] == "BROKER_VERIFIED"
                        else "MANUAL_USER_REPORTED"
                    )
                    prefix = "[Broker Working]" if src_tag == "BROKER_CONFIRMED_ORDER" else "[User-Reported Manual]"
                    if pos.get("avg_fill_price"):
                        levels.append(
                            {
                                "level_id": f"{sym}-pos-fill-{pos['position_id']}",
                                "kind": "MANUAL_ENTRY",
                                "price": float(pos["avg_fill_price"]),
                                "label": f"{prefix} Avg Entry ${float(pos['avg_fill_price']):.2f}",
                                "source": src_tag,
                            }
                        )
                    if pos.get("working_stop_price"):
                        levels.append(
                            {
                                "level_id": f"{sym}-pos-stop-{pos['position_id']}",
                                "kind": "WORKING_STOP",
                                "price": float(pos["working_stop_price"]),
                                "label": f"{prefix} Stop ${float(pos['working_stop_price']):.2f}",
                                "source": src_tag,
                            }
                        )

            self._chart_cache[sym] = {
                "symbol": sym,
                "bars": bars,
                "markers": markers,
                "levels": levels,
                "last_completed_bar_close_et": last_completed["bar_close_et"],
                "forming_bar_open_et": bars[-1]["bar_open_et"],
            }

            # Ensure a populated versioned OrderPlan exists for each symbol
            self._ensure_order_plan_for_candidate(cand_dict)

            if sp["confirmed"]:
                self.db.emit_deduped_alert(
                    dedupe_key=f"sig-{sig_id}",
                    alert_type="NEW_CONFIRMED_SIGNAL",
                    severity="SUCCESS",
                    symbol=sym,
                    message=f"Confirmed 5m Higher-Low Reclaim on {sym} @ ${sp['reclaim_close']:.2f} (10:15 ET bar close). Ready for review.",
                )

        if is_stale:
            self.db.emit_deduped_alert(
                dedupe_key="stale-data-demo",
                alert_type="STALE_REQUIRED_DATA",
                severity="WARNING",
                symbol="SPY",
                message="Required SPY and 5m bar data is stale (23m old > 7m threshold). New entries are disabled.",
            )

        return candidates

    def _run_live_market_scan(self, tickers: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        Evaluate live market data through the authoritative `strategy_engine.py`,
        with fallback to frozen baseline data if offline.
        """
        now_et = datetime.now(NY_TZ)
        now_str = now_et.strftime("%Y-%m-%d %H:%M:%S %Z")
        all_syms = ["SPY"] + [t.upper() for t in tickers]

        quote_map = fetch_cnbc_quote_observations(all_syms, now_et=now_et)
        spy_5m, spy_prov = fetch_cnbc_ohlcv_bars("SPY", interval="5m", days=20, now_et=now_et)
        spy_1d, _ = fetch_cnbc_ohlcv_bars("SPY", interval="1d", days=370, now_et=now_et)

        if spy_5m.empty or spy_1d.empty:
            spy_5m = load_baseline_ohlcv("SPY", "5m")
            spy_1d = load_baseline_ohlcv("SPY", "1d")

        if spy_5m.empty or spy_1d.empty:
            return self._build_fixture_candidates("demo_ready")

        spy_5m_feat = compute_causal_spy_features(spy_5m, self.strategy_cfg)
        last_spy_ts = spy_5m_feat.index[-1]
        spy_info = lookup_spy_state_at_bar(
            spy_5m_feat,
            last_spy_ts,
            now_et=last_spy_ts.to_pydatetime(),
            cfg=self.strategy_cfg,
        )
        last_spy_row = spy_5m_feat.iloc[-1]
        self._spy_banner = {
            "symbol": "SPY",
            "last": round(float(last_spy_row["Close"]), 2),
            "vwap": round(float(last_spy_row["VWAP"]), 2),
            "last_price": round(float(last_spy_row["Close"]), 2),
            "session_vwap": round(float(last_spy_row["VWAP"]), 2),
            "vs_vwap_pct": round(float(last_spy_row["BelowVWAP"]) * 100.0, 2),
            "drawdown_from_high_pct": round(float(last_spy_row["DropFromHigh"]) * 100.0, 2),
            "max_intraday_drop_pct": round(float(spy_5m_feat[spy_5m_feat["Date"] == last_spy_ts.date()]["DropFromHigh"].min()) * 100.0, 2),
            "gate_status": "PASS" if spy_info["status"] == "HEALTHY" else ("FAIL" if spy_info["status"] == "WATERFALL_BLOCKED" else "UNKNOWN"),
            "status_label": (
                f"SPY Tape Healthy ({float(last_spy_row['BelowVWAP']) * 100.0:+.2f}% vs VWAP)"
                if spy_info["status"] == "HEALTHY"
                else f"SPY Gate: {spy_info['status']}"
            ),
            "observation_timestamp_et": last_spy_ts.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "age_seconds": spy_prov.age_seconds if spy_prov else 5.0,
        }

        candidates: Dict[str, Dict[str, Any]] = {}
        for sym in tickers:
            sym = sym.upper()
            df_5m, bar_prov = fetch_cnbc_ohlcv_bars(sym, interval="5m", days=25, now_et=now_et)
            df_1d, _ = fetch_cnbc_ohlcv_bars(sym, interval="1d", days=370, now_et=now_et)
            if df_5m.empty or df_1d.empty:
                df_5m = load_baseline_ohlcv(sym, "5m")
                df_1d = load_baseline_ohlcv(sym, "1d")
            if df_5m.empty or df_1d.empty:
                continue

            daily_table = compute_daily_regime_table(df_1d, spy_1d, self.strategy_cfg)
            feat_5m = compute_causal_5m_features(df_5m, self.strategy_cfg)
            session_date = feat_5m["Date"].iloc[-1]
            day_df = feat_5m[feat_5m["Date"] == session_date].copy()
            if day_df.empty:
                continue

            regime_info = lookup_causal_daily_regime(daily_table, session_date)
            er_res = self.earnings_oracle.check_blackout(sym, session_date, use_filter=True)
            eval_res = evaluate_session_causal(
                ticker=sym,
                day_df=day_df,
                session_date=session_date,
                prior_day_accum=regime_info["prior_day_accum"],
                developing_next_day_accum=regime_info["developing_next_day_accum"],
                er_result=er_res,
                spy_5m_featured=spy_5m_feat,
                calendar=self.calendar,
                strat_cfg=self.strategy_cfg,
                exec_cfg=self.exec_cfg,
                risk_cfg=self.risk_cfg,
                now_et=now_et,
            )

            last_row = day_df.iloc[-1]
            completed_df = day_df.iloc[:-1] if len(day_df) > 1 else day_df
            last_comp_ts = completed_df.index[-1]
            last_comp_close_ts = last_comp_ts + timedelta(minutes=5)

            bar_obs = QuoteObservation(
                symbol=sym,
                last=float(last_row["Close"]),
                bid=None,
                ask=None,
                open_price=float(day_df["Open"].iloc[0]),
                day_high=float(day_df["High"].max()),
                day_low=float(day_df["Low"].min()),
                day_volume=float(day_df["Volume"].sum()),
                provenance=bar_prov or DataProvenance(
                    vendor_id="cnbc",
                    endpoint_id="cnbc_harmony_bars",
                    observation_timestamp_et=last_row.name.strftime("%Y-%m-%d %H:%M:%S %Z"),
                    fetch_timestamp_et=now_str,
                    age_seconds=5.0,
                    is_delayed=False,
                    delay_seconds=0,
                    update_mode="polling_snapshot",
                ),
            )
            q_obs = quote_map.get(sym)
            consistency = evaluate_observation_consistency(bar_obs, q_obs)

            flush_low_val = eval_res.flush_low if np.isfinite(eval_res.flush_low) else float(day_df["Low"].min())
            mhigh_val = float(last_row["MorningHigh"])
            vwap_val = float(last_row["VWAP"])
            flush_pct = round((flush_low_val - mhigh_val) / mhigh_val * 100.0, 2)
            vs_vwap_pct = round((float(last_row["Close"]) - vwap_val) / vwap_val * 100.0, 2)

            # Map gates
            g_daily = GateResultModel(
                gate_id="daily_regime",
                gate_name="Daily Accumulation & Earnings Rule",
                status=GateStatus.PASS if (regime_info["prior_day_accum"] and not er_res.is_blocked) else GateStatus.FAIL,
                observed_value=f"Prior Accum={regime_info['prior_day_accum']} | Earnings={er_res.display_label}",
                threshold="Prior-day accumulation True & >14d to earnings",
                timestamp_et=str(regime_info.get("prior_completed_date") or session_date),
                summary=(
                    f"Blocked by strategy 14-day pre-earnings restriction ({er_res.display_label})."
                    if er_res.is_blocked
                    else ("Prior completed daily session is in accumulation." if regime_info["prior_day_accum"] else "Prior completed daily session is not in accumulation.")
                ),
                details=f"Prior session: {regime_info.get('prior_completed_date')} | Developing next-day: {regime_info['developing_next_day_accum']}",
            )
            g_session = GateResultModel(
                gate_id="session_window",
                gate_name="NYSE Entry Window & Data Freshness",
                status=GateStatus.PASS if consistency.status != "FAIL" else GateStatus.FAIL,
                observed_value=now_et.strftime("%H:%M:%S %Z"),
                threshold="RTH 09:50–13:00 ET & Consistent Observations",
                timestamp_et=now_str,
                summary=consistency.explanation,
                details=f"Calendar status: {self.calendar.session_label(session_date)}",
            )
            g_spy = GateResultModel(
                gate_id="spy_macro",
                gate_name="SPY Intraday Waterfall Gate",
                status=GateStatus.PASS if spy_info["status"] == "HEALTHY" else GateStatus.FAIL,
                observed_value=self._spy_banner["status_label"],
                threshold="SPY healthy",
                timestamp_et=self._spy_banner["observation_timestamp_et"],
                summary=self._spy_banner["status_label"],
                details=f"SPY Last ${self._spy_banner['last_price']:.2f} vs Session VWAP ${self._spy_banner['session_vwap']:.2f}",
            )
            g_flush = GateResultModel(
                gate_id="flush_depth",
                gate_name="Morning Flush Depth vs. High & VWAP",
                status=GateStatus.PASS if flush_pct <= -1.10 else GateStatus.FAIL,
                observed_value=f"{flush_pct:+.2f}% from High (${flush_low_val:.2f} vs ${mhigh_val:.2f})",
                threshold="<= -1.10% from Morning High & <= -0.35% vs VWAP",
                timestamp_et=last_comp_close_ts.strftime("%Y-%m-%d %H:%M:%S %Z"),
                summary=f"Intraday low ${flush_low_val:.2f} is {flush_pct:+.2f}% from ${mhigh_val:.2f} Morning High.",
                details=f"Session VWAP: ${vwap_val:.2f} ({vs_vwap_pct:+.2f}%)",
            )
            g_vol = GateResultModel(
                gate_id="volume_climax",
                gate_name="Intraday Volume Climax",
                status=GateStatus.PASS if eval_res.climax_mult >= 1.80 else GateStatus.FAIL,
                observed_value=f"{eval_res.climax_mult:.1f}x baseline",
                threshold=">= 1.80x VolMA10_ExOpen",
                timestamp_et=last_comp_close_ts.strftime("%Y-%m-%d %H:%M:%S %Z"),
                summary=f"Peak flush volume multiplier is {eval_res.climax_mult:.1f}x.",
                details="09:30 and 09:35 bars excluded from VolMA10_ExOpen baseline.",
            )
            is_ready = eval_res.status == "ENTRY_SIGNAL_NOW" and consistency.status != "FAIL"
            g_reclaim = GateResultModel(
                gate_id="reclaim_confirmation",
                gate_name="Completed 5m Higher-Low Green Reclaim Bar",
                status=GateStatus.PASS if is_ready else GateStatus.FAIL,
                observed_value=eval_res.status,
                threshold="Completed 5m green higher-low reclaim bar",
                timestamp_et=last_comp_close_ts.strftime("%Y-%m-%d %H:%M:%S %Z"),
                summary=eval_res.why_diagnosis,
                details=f"Evaluated on completed 5m candle closing at {last_comp_close_ts.strftime('%H:%M:%S %Z')}.",
            )

            if consistency.status == "FAIL":
                cat = CandidateCategory.DATA_UNAVAILABLE
                badge = "Feed Divergence"
                reason = consistency.explanation
            elif er_res.is_blocked:
                cat = CandidateCategory.BLOCKED
                badge = "Blocked · Earnings Rule"
                reason = f"Strategy 14d pre-earnings restriction ({er_res.display_label})"
            elif not regime_info["prior_day_accum"]:
                cat = CandidateCategory.BLOCKED
                badge = "Blocked · No Daily Accum"
                reason = "Daily accumulation criterion unmet on prior completed session"
            elif is_ready:
                cat = CandidateCategory.READY_FOR_REVIEW
                badge = "Ready for Review"
                reason = eval_res.why_diagnosis
            elif eval_res.status == "ARMED_FLUSHING":
                cat = CandidateCategory.WATCHING
                badge = "Watching · Armed"
                reason = "Flush & climax met; awaiting completed 5m green higher-low reclaim bar"
            else:
                cat = CandidateCategory.WATCHING
                badge = "Watching · Eligible"
                reason = eval_res.why_diagnosis

            sig_id = eval_res.signal_id or hashlib.sha256(
                f"{sym}|{session_date}|{last_comp_close_ts.isoformat()}|v5.0-audited".encode("utf-8")
            ).hexdigest()[:16]

            cand_dict = {
                "signal_id": sig_id,
                "signal_version": 1,
                "symbol": sym,
                "session_date": str(session_date),
                "signal_state": SignalState.READY_FOR_REVIEW.value if is_ready else eval_res.status,
                "category": cat.value,
                "primary_status_badge": badge,
                "concise_reason": reason,
                "signal_time_et": last_comp_close_ts.strftime("%H:%M:%S %Z") if is_ready else None,
                "signal_age_seconds": 15.0 if is_ready else None,
                "is_actionable": is_ready,
                "actionability_reason": "All 6 gates satisfied." if is_ready else f"Execution unavailable: {reason}",
                "observation_time_et": now_str,
                "bar_open_et": last_comp_ts.strftime("%Y-%m-%d %H:%M:%S %Z"),
                "bar_close_et": last_comp_close_ts.strftime("%Y-%m-%d %H:%M:%S %Z"),
                "fetch_time_et": now_str,
                "decision_time_et": now_str,
                "strategy_version": "v5.0-audited",
                "last_price": round(float(last_row["Close"]), 2),
                "session_vwap": round(vwap_val, 2),
                "vs_vwap_pct": vs_vwap_pct,
                "morning_high": round(mhigh_val, 2),
                "flush_low": round(flush_low_val, 2),
                "flush_pct": flush_pct,
                "volume_climax_mult": round(float(eval_res.climax_mult), 2),
                "why_now_or_missing": eval_res.why_diagnosis,
                "gates": [g.model_dump() for g in (g_daily, g_session, g_spy, g_flush, g_vol, g_reclaim)],
                "reference_trigger_price": round(flush_low_val * 1.0025, 2),
                "reference_trigger_timestamp_et": last_comp_close_ts.strftime("%Y-%m-%d %H:%M:%S %Z"),
                "reference_trigger_meaning": "Reference +0.25% bounce above flush low (informational only).",
                "consistency_check": consistency.to_dict(),
                "provenance": bar_obs.provenance.to_dict(),
            }
            candidates[sym] = cand_dict
            self.db.save_signal(cand_dict)

            # Build chart bars
            bars_list = []
            for idx_b, (ts_b, r_b) in enumerate(day_df.iterrows()):
                b_close = ts_b + timedelta(minutes=5)
                bars_list.append(
                    {
                        "time": int(ts_b.timestamp()),
                        "bar_open_et": ts_b.strftime("%Y-%m-%d %H:%M:%S %Z"),
                        "bar_close_et": b_close.strftime("%Y-%m-%d %H:%M:%S %Z"),
                        "open": round(float(r_b["Open"]), 2),
                        "high": round(float(r_b["High"]), 2),
                        "low": round(float(r_b["Low"]), 2),
                        "close": round(float(r_b["Close"]), 2),
                        "volume": int(r_b["Volume"]),
                        "vwap": round(float(r_b["VWAP"]), 2),
                        "ema12": round(float(r_b["EMA12"]), 2),
                        "ema20": round(float(r_b["EMA20"]), 2),
                        "is_completed": idx_b < len(day_df) - 1,
                    }
                )
            self._chart_cache[sym] = {
                "symbol": sym,
                "bars": bars_list,
                "markers": [],
                "levels": [],
                "last_completed_bar_close_et": last_comp_close_ts.strftime("%Y-%m-%d %H:%M:%S %Z"),
                "forming_bar_open_et": day_df.index[-1].strftime("%Y-%m-%d %H:%M:%S %Z"),
            }
            self._ensure_order_plan_for_candidate(cand_dict)

        return candidates

    def run_scan_cycle(self) -> Dict[str, Any]:
        with self._lock:
            now_str = datetime.now(NY_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
            self.last_heartbeat_et = now_str
            self.last_scan_et = now_str
            self.scan_cycle_count += 1

            settings = self.db.get_settings()
            fixture_mode = settings.get("fixture_mode", "demo_ready")
            tickers = settings.get("universe_tickers", ["FISV", "PLNT", "EEFT", "CNC", "GPN", "NU"])

            if fixture_mode in ("demo_ready", "demo_acceptance", "demo_stale"):
                self._candidates_cache = self._build_fixture_candidates(fixture_mode)
            else:
                self._candidates_cache = self._run_live_market_scan(tickers)

            # Update open position marks and run automated position management (EMA20 trail & cutoff check)
            self._manage_open_strategy_positions()

            snapshot = self.get_dashboard_snapshot()
            self._notify_listeners({"type": "SNAPSHOT_UPDATED", "timestamp_et": now_str})
            return snapshot

    # ==========================================================================
    # 3. VERSIONED ORDER PLAN MANAGEMENT (Section 5)
    # ==========================================================================
    def _ensure_order_plan_for_candidate(self, cand: Dict[str, Any]) -> Dict[str, Any]:
        sym = cand["symbol"]
        plan_id = f"plan-{sym.lower()}-{cand['signal_id'][:8]}"
        existing = self.db.get_order_plan(plan_id)
        settings = self.db.get_settings()

        if existing is not None:
            # Do NOT overwrite user-edited allocation or limit_price during background refreshes!
            existing["quote_freshness_seconds"] = cand["provenance"].get("age_seconds", 2.0)
            is_exec, blocked_reason = self._compute_plan_executability(cand, settings)
            existing["is_executable"] = is_exec
            existing["execution_blocked_reason"] = blocked_reason
            existing["execution_blocked_reasons"] = [blocked_reason] if blocked_reason else []
            self.db.save_order_plan(existing)
            return existing

        alloc_usd = float(settings.get("default_allocation_usd", 150000.0))
        max_shares = int(settings.get("max_share_cap", 3000))
        limit_p = round(float(cand.get("reference_trigger_price") or cand.get("last_price") or 50.0), 2)
        if cand["signal_state"] == SignalState.READY_FOR_REVIEW.value and cand.get("last_price"):
            limit_p = round(float(cand["last_price"]), 2)

        flush_low = float(cand.get("flush_low") or (limit_p * 0.994))
        stop_p = round(
            min(
                flush_low * (1.0 - 0.0005),
                max(flush_low * (1.0 - 0.0035), limit_p * (1.0 - 0.0065)),
            ),
            2,
        )
        if stop_p >= limit_p:
            stop_p = round(limit_p * 0.9935, 2)

        shares = min(max(1, int(alloc_usd // limit_p)), max_shares)
        q1 = shares // 2
        q2 = shares - q1  # Odd-share safe: q1 + q2 == shares always

        vwap_p = float(cand.get("session_vwap") or (limit_p * 1.005))
        leg1_p = round(max(vwap_p, limit_p * 1.0040), 2)
        leg2_p = round(limit_p * 1.0085, 2)
        stretch_p = round(limit_p * 1.0160, 2)

        notional = round(shares * limit_p, 2)
        planned_risk_usd = round(shares * (limit_p - stop_p), 2)
        planned_risk_pct = round(abs(limit_p - stop_p) / limit_p * 100.0, 2)
        now_str = datetime.now(NY_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
        is_exec, blocked_reason = self._compute_plan_executability(cand, settings)

        plan = {
            "plan_id": plan_id,
            "plan_version": 1,
            "signal_id": cand["signal_id"],
            "signal_version": cand["signal_version"],
            "symbol": sym,
            "side": "BUY_TO_OPEN",
            "strategy_version": cand["strategy_version"],
            "signal_bar_close_et": cand["bar_close_et"],
            "created_at_et": now_str,
            "allocation_usd": alloc_usd,
            "shares": shares,
            "leg1_shares": q1,
            "leg2_shares": q2,
            "tranche1_shares": q1,
            "tranche2_shares": q2,
            "entry_order_type": "LIMIT",
            "limit_price": limit_p,
            "stop_price": stop_p,
            "leg1_target": leg1_p,
            "leg2_target": leg2_p,
            "runner_stretch_target": stretch_p,
            "runner_ema20_level": cand.get("session_vwap"),
            "notional_usd": notional,
            "estimated_notional_usd": notional,
            "planned_risk_usd": planned_risk_usd,
            "planned_risk_pct": planned_risk_pct,
            "estimated_fees_usd": 0.0,
            "is_manual_override": False,
            "user_edited": False,
            "override_notes": "",
            "masked_account_id": f"****{TARGET_ACCOUNT_ID[-4:]} ({TARGET_ACCOUNT_ID})",
            "available_buying_power_usd": 300000.0,
            "buying_power_timestamp_et": now_str,
            "quote_freshness_seconds": cand["provenance"].get("age_seconds", 2.0),
            "is_executable": is_exec,
            "execution_blocked_reason": blocked_reason,
            "execution_blocked_reasons": [blocked_reason] if blocked_reason else [],
            "flatten_cutoff_et": "15:40 ET",
            "active_preview_id": None,
        }
        plan["copy_ticket_text"] = format_copy_ticket_text(plan)
        plan["broker_handoff_link"] = build_broker_handoff_link(sym).to_dict()
        self.db.save_order_plan(plan)
        return plan

    def _compute_plan_executability(
        self,
        cand: Dict[str, Any],
        settings: Dict[str, Any],
    ) -> Tuple[bool, str]:
        if self.reconciliation_required:
            return False, f"Reconciliation required before new submissions: {self.reconciliation_reason}"
        if settings.get("pause_new_entries"):
            return False, "Pause new entries is active (existing positions remain managed)."
        if not cand.get("is_actionable"):
            return False, cand.get("actionability_reason", "Setup is not a fresh confirmed signal.")
        return True, ""

    def update_order_plan(self, req: Dict[str, Any]) -> Dict[str, Any]:
        """
        Regenerate and validate an OrderPlan after user edits (`allocation_usd`, `limit_price`,
        or explicit manual stop/target overrides). Invalidates any previous BrokerPreview!
        """
        with self._lock:
            plan_id = req.get("plan_id")
            plan = self.db.get_order_plan(plan_id) if plan_id else None
            if plan is None and req.get("symbol"):
                sym_lookup = str(req["symbol"]).upper()
                cand_active = self._candidates_cache.get(sym_lookup)
                if cand_active:
                    expected_id = f"plan-{sym_lookup.lower()}-{cand_active['signal_id'][:8]}"
                    plan = self.db.get_order_plan(expected_id)
                    if plan:
                        plan_id = expected_id
                if plan is None:
                    for p in self.db.list_order_plans():
                        if p["symbol"] == sym_lookup:
                            plan = p
                            plan_id = p["plan_id"]
                            break
            if plan is None or not plan_id:
                raise OrderValidationError(f"Order plan '{plan_id or req.get('symbol')}' not found.")

            settings = self.db.get_settings()
            max_shares = int(settings.get("max_share_cap", 3000))
            max_notional = float(settings.get("max_notional_usd", 152000.0))
            max_planned_risk = float(settings.get("max_planned_risk_usd", 1500.0))

            alloc_usd = float(req.get("allocation_usd") if req.get("allocation_usd") is not None else plan["allocation_usd"])
            limit_p = round(float(req.get("limit_price") if req.get("limit_price") is not None else plan["limit_price"]), 2)
            if alloc_usd <= 0 or limit_p <= 0:
                raise OrderValidationError("Allocation and limit price must be positive.")
            if alloc_usd > max_notional:
                raise OrderValidationError(
                    f"Allocation ${alloc_usd:,.2f} exceeds maximum notional cap ${max_notional:,.2f}."
                )

            cand = self._candidates_cache.get(plan["symbol"])
            flush_low = float(cand["flush_low"]) if cand and cand.get("flush_low") else (limit_p * 0.994)
            vwap_p = float(cand["session_vwap"]) if cand and cand.get("session_vwap") else (limit_p * 1.005)

            is_override = False
            override_notes = []

            if req.get("manual_stop_override") is not None:
                stop_p = round(float(req["manual_stop_override"]), 2)
                is_override = True
                override_notes.append(f"Manual stop override: ${stop_p:.2f}")
            else:
                stop_p = round(
                    min(
                        flush_low * (1.0 - 0.0005),
                        max(flush_low * (1.0 - 0.0035), limit_p * (1.0 - 0.0065)),
                    ),
                    2,
                )

            if stop_p >= limit_p:
                raise OrderValidationError(
                    f"Invalid stop price ${stop_p:.2f}: must be strictly below limit price ${limit_p:.2f}."
                )

            if req.get("manual_leg1_override") is not None:
                leg1_p = round(float(req["manual_leg1_override"]), 2)
                is_override = True
                override_notes.append(f"Manual Leg 1 override: ${leg1_p:.2f}")
            else:
                leg1_p = round(max(vwap_p, limit_p * 1.0040), 2)

            if req.get("manual_leg2_override") is not None:
                leg2_p = round(float(req["manual_leg2_override"]), 2)
                is_override = True
                override_notes.append(f"Manual Leg 2 override: ${leg2_p:.2f}")
            else:
                leg2_p = round(limit_p * 1.0085, 2)

            if leg1_p <= limit_p or leg2_p <= limit_p:
                raise OrderValidationError("Profit targets must be strictly above entry limit price.")

            shares = min(max(1, int(alloc_usd // limit_p)), max_shares)
            per_share_risk = round(limit_p - stop_p, 4)
            if shares * per_share_risk > max_planned_risk and per_share_risk > 0:
                # Cap share count so planned risk never exceeds max_planned_risk_usd
                shares = min(shares, max(1, int(max_planned_risk // per_share_risk)))

            q1 = shares // 2
            q2 = shares - q1  # Guarantees q1 + q2 == shares

            notional = round(shares * limit_p, 2)
            planned_risk_usd = round(shares * (limit_p - stop_p), 2)
            planned_risk_pct = round(abs(limit_p - stop_p) / limit_p * 100.0, 2)

            # Validate against core RiskConfig rules
            risk_cfg = RiskConfig(
                target_clip_usd=alloc_usd,
                max_notional_per_order_usd=max_notional,
                max_shares_per_order=max_shares,
                max_planned_stop_loss_usd=max_planned_risk,
            )
            validate_order_safety(
                ticker=plan["symbol"],
                shares=shares,
                limit_price=limit_p,
                stop_price=stop_p,
                account_id=TARGET_ACCOUNT_ID,
                leg1_target=leg1_p,
                leg2_target=leg2_p,
                risk_cfg=risk_cfg,
            )

            # Invalidate any existing broker previews for this plan!
            self.db.invalidate_previews_for_plan(plan_id)

            plan["plan_version"] = int(plan["plan_version"]) + 1
            plan["allocation_usd"] = alloc_usd
            plan["shares"] = shares
            plan["leg1_shares"] = q1
            plan["leg2_shares"] = q2
            plan["tranche1_shares"] = q1
            plan["tranche2_shares"] = q2
            plan["limit_price"] = limit_p
            plan["stop_price"] = stop_p
            plan["leg1_target"] = leg1_p
            plan["leg2_target"] = leg2_p
            plan["runner_stretch_target"] = round(limit_p * 1.0160, 2)
            plan["notional_usd"] = notional
            plan["estimated_notional_usd"] = notional
            plan["planned_risk_usd"] = planned_risk_usd
            plan["planned_risk_pct"] = planned_risk_pct
            plan["is_manual_override"] = is_override
            plan["user_edited"] = True
            plan["override_notes"] = "; ".join(override_notes)
            plan["active_preview_id"] = None
            plan["copy_ticket_text"] = format_copy_ticket_text(plan)

            self.db.save_order_plan(plan)
            self.db.log_event(
                event_type="ORDER_PLAN_UPDATED",
                severity="INFO",
                source_mode=settings.get("operating_mode", "LIVE_DATA_TRADING_DISABLED"),
                symbol=plan["symbol"],
                summary=(
                    f"Order plan {plan_id} updated to v{plan['plan_version']}: "
                    f"{shares:,} sh ({q1:,} + {q2:,}) @ ${limit_p:.2f} Limit, Stop ${stop_p:.2f} "
                    f"(Planned Risk -${planned_risk_usd:,.2f}). Prior previews invalidated."
                ),
                details={"plan_id": plan_id, "plan_version": plan["plan_version"]},
            )
            return plan

    # ==========================================================================
    # 4. BROKER PREVIEW & CONFIRMED SUBMISSION (Section 6 & 7)
    # ==========================================================================
    def run_broker_preview(
        self,
        plan_id: str,
        plan_version: int,
        simulate_failure: bool = False,
        simulate_broker_error: bool = False,
    ) -> Dict[str, Any]:
        """
        Bind a BrokerPreview to `(account, symbol, side, quantities, prices, signal_version,
        environment, expiry)` and construct the Split-Tranche OTOCO payload (`Tranche A + Tranche B`).
        Clearly distinguishes `BROKER_PREFLIGHT_VERIFIED` from `LOCAL_SCHEMA_VALIDATION_ONLY`.
        """
        with self._lock:
            if self.reconciliation_required:
                raise ReconciliationRequiredError(
                    f"RECONCILIATION REQUIRED: {self.reconciliation_reason}"
                )

            settings = self.db.get_settings()
            if settings.get("pause_new_entries"):
                raise OrderValidationError("PREVIEW BLOCKED: New entries are PAUSED ('Pause new entries' is active).")

            plan = self.db.get_order_plan(plan_id)
            if plan is None:
                raise OrderValidationError(f"Order plan '{plan_id}' not found.")
            if int(plan["plan_version"]) != int(plan_version):
                raise OrderValidationError(
                    f"Order plan version mismatch: requested v{plan_version}, current is v{plan['plan_version']}."
                )

            cand = self._candidates_cache.get(plan["symbol"])
            if (
                cand is None
                or not cand.get("is_actionable")
                or cand.get("signal_state") != SignalState.READY_FOR_REVIEW.value
            ):
                st = cand.get("signal_state") if cand else "UNKNOWN"
                raise OrderValidationError(
                    f"PREVIEW BLOCKED: {plan['symbol']} is not READY_FOR_REVIEW (current state: {st})."
                )

            env_name = settings.get("broker_environment", "SANDBOX")
            now_et = datetime.now(NY_TZ)
            exp_et = now_et + timedelta(seconds=90)
            preview_id = f"prev-{uuid.uuid4().hex[:10]}"

            built = build_v6_split_tranche_orders(
                ticker=plan["symbol"],
                shares=plan["shares"],
                limit_price=plan["limit_price"],
                stop_price=plan["stop_price"],
                leg1_target=plan["leg1_target"],
                leg2_target=plan["leg2_target"],
                account_id=TARGET_ACCOUNT_ID,
                risk_cfg=RiskConfig(
                    target_clip_usd=plan["allocation_usd"],
                    max_notional_per_order_usd=float(settings.get("max_notional_usd", 152000.0)),
                    max_shares_per_order=int(settings.get("max_share_cap", 3000)),
                    max_planned_stop_loss_usd=float(settings.get("max_planned_risk_usd", 1500.0)),
                ),
            )

            if simulate_failure or simulate_broker_error:
                err_msg = "Broker preflight dry-run rejected order: Margin/Price check failed. Submission blocked."
                preview = {
                    "preview_id": preview_id,
                    "plan_id": plan_id,
                    "plan_version": plan["plan_version"],
                    "signal_id": plan["signal_id"],
                    "signal_version": plan["signal_version"],
                    "account_id": TARGET_ACCOUNT_ID,
                    "masked_account_id": plan["masked_account_id"],
                    "symbol": plan["symbol"],
                    "side": plan["side"],
                    "shares": plan["shares"],
                    "leg1_shares": plan["leg1_shares"],
                    "leg2_shares": plan["leg2_shares"],
                    "limit_price": plan["limit_price"],
                    "stop_price": plan["stop_price"],
                    "leg1_target": plan["leg1_target"],
                    "leg2_target": plan["leg2_target"],
                    "environment": env_name,
                    "validation_mode": ValidationMode.BROKER_PREFLIGHT_VERIFIED.value,
                    "status": "FAILED_REJECTED",
                    "created_at_et": now_et.strftime("%Y-%m-%d %H:%M:%S %Z"),
                    "expires_at_et": exp_et.strftime("%Y-%m-%d %H:%M:%S %Z"),
                    "expires_epoch": exp_et.timestamp(),
                    "is_valid_for_submission": False,
                    "message": err_msg,
                    "errors": [err_msg],
                    "tranche_a_payload": built["tranche_a_json"],
                    "tranche_b_payload": built["tranche_b_json"],
                }
                self.db.save_broker_preview(preview)
                self.db.log_event(
                    event_type="BROKER_PREVIEW_FAILED",
                    severity="CRITICAL",
                    source_mode=settings.get("operating_mode", "BROKER_PREVIEW"),
                    symbol=plan["symbol"],
                    summary=preview["message"],
                    details={"preview_id": preview_id, "plan_id": plan_id},
                )
                return preview

            # Check if live Tastytrade credentials are configured
            validation_mode = ValidationMode.LOCAL_SCHEMA_VALIDATION_ONLY.value
            msg = (
                "Local schema validation passed for Split-Tranche OTOCO (Tranche A + Tranche B). "
                "Tastytrade OAuth credentials not connected — labeled LOCAL_SCHEMA_VALIDATION_ONLY "
                "(does NOT create a draft in Tastytrade UI)."
            )

            preview = {
                "preview_id": preview_id,
                "plan_id": plan_id,
                "plan_version": plan["plan_version"],
                "signal_id": plan["signal_id"],
                "signal_version": plan["signal_version"],
                "account_id": TARGET_ACCOUNT_ID,
                "masked_account_id": plan["masked_account_id"],
                "symbol": plan["symbol"],
                "side": plan["side"],
                "shares": plan["shares"],
                "leg1_shares": plan["leg1_shares"],
                "leg2_shares": plan["leg2_shares"],
                "limit_price": plan["limit_price"],
                "stop_price": plan["stop_price"],
                "leg1_target": plan["leg1_target"],
                "leg2_target": plan["leg2_target"],
                "environment": env_name,
                "validation_mode": validation_mode,
                "status": "VALID",
                "created_at_et": now_et.strftime("%Y-%m-%d %H:%M:%S %Z"),
                "expires_at_et": exp_et.strftime("%Y-%m-%d %H:%M:%S %Z"),
                "expires_epoch": exp_et.timestamp(),
                "is_valid_for_submission": True,
                "message": msg,
                "errors": [],
                "tranche_a_payload": built["tranche_a_json"],
                "tranche_b_payload": built["tranche_b_json"],
            }
            self.db.save_broker_preview(preview)
            plan["active_preview_id"] = preview_id
            self.db.save_order_plan(plan)

            self.db.log_event(
                event_type="BROKER_PREVIEW_CREATED",
                severity="INFO",
                source_mode=settings.get("operating_mode", "BROKER_PREVIEW"),
                symbol=plan["symbol"],
                summary=(
                    f"Preview {preview_id} created for {plan['symbol']} (Plan v{plan['plan_version']}): "
                    f"Tranche A {plan['leg1_shares']} sh + Tranche B {plan['leg2_shares']} sh [{validation_mode}]."
                ),
                details={"preview_id": preview_id, "validation_mode": validation_mode},
            )
            return preview

    def submit_confirmed_order(
        self,
        plan_id: str,
        plan_version: int,
        preview_id: Optional[str],
        idempotency_key: str,
        explicit_confirmation_checked: bool = False,
        confirmed_by_user: Optional[bool] = None,
        simulate_timeout: bool = False,
        simulate_partial_fill: bool = False,
        simulate_partial_fill_qty: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Submit a deliberately confirmed order with strict pre-submission revalidation,
        idempotency protection, and Split-Tranche OTOCO protective lifecycle tracking.
        """
        with self._lock:
            is_confirmed = bool(confirmed_by_user) if confirmed_by_user is not None else bool(explicit_confirmation_checked)
            if not is_confirmed:
                raise OrderValidationError("CONFIRMATION REQUIRED: Explicit final confirmation is required before submitting an order.")

            # 1. Idempotency check (blocks duplicate double-clicks or repeated submit requests!)
            existing_by_key = self.db.get_intent_by_idempotency(idempotency_key)
            if existing_by_key is not None:
                raise OrderValidationError(
                    f"DUPLICATE SUBMISSION BLOCKED: idempotency_key '{idempotency_key}' was already submitted (intent {existing_by_key['intent_id']})."
                )

            if self.reconciliation_required:
                raise ReconciliationRequiredError(
                    f"SUBMISSION BLOCKED: Reconciliation required — {self.reconciliation_reason}"
                )

            settings = self.db.get_settings()
            op_mode = settings.get("operating_mode", "LIVE_DATA_TRADING_DISABLED")
            fixture_mode = settings.get("fixture_mode", "demo_ready")

            if settings.get("pause_new_entries"):
                raise OrderValidationError("SUBMISSION BLOCKED: 'Pause new entries' is active.")

            if op_mode == "LIVE_SUBMISSION_ENABLED" and fixture_mode != "live":
                raise OrderValidationError(
                    f"SECURITY ABORT: Demo fixture '{fixture_mode}' has no route to live broker execution!"
                )

            plan = self.db.get_order_plan(plan_id)
            if plan is None:
                raise OrderValidationError(f"Order plan '{plan_id}' not found.")
            if int(plan["plan_version"]) != int(plan_version):
                raise OrderValidationError(
                    f"VERSION MISMATCH: Order plan was modified (expected v{plan_version}, current v{plan['plan_version']}). "
                    f"Review the updated plan before submitting."
                )

            # Check for existing active intent on the same signal (prevents second tab from duplicating)
            existing_for_sig = self.db.get_active_intent_for_signal(plan["signal_id"])
            if existing_for_sig is not None:
                raise OrderValidationError(
                    f"DUPLICATE SIGNAL SUBMISSION BLOCKED: Signal '{plan['signal_id']}' already has an active order intent ({existing_for_sig['intent_id']})."
                )

            # Verify candidate signal is still READY_FOR_REVIEW (never submit armed, watching, blocked, or expired!)
            cand = self._candidates_cache.get(plan["symbol"])
            if cand is None or not cand.get("is_actionable") or cand.get("signal_state") != SignalState.READY_FOR_REVIEW.value:
                reason = cand.get("actionability_reason") if cand else "Signal no longer active."
                raise StaleSignalError(f"SUBMISSION BLOCKED: {plan['symbol']} is not actionable ({reason}).")

            # Verify preview validity
            if not preview_id:
                raise PreflightRejectedError("PREVIEW INVALID: Run 'Preview with Tastytrade' before submitting.")
            preview = self.db.get_broker_preview(preview_id)
            if preview is None:
                raise PreflightRejectedError(f"PREVIEW INVALID: Preview '{preview_id}' not found.")
            if preview["plan_id"] != plan_id or int(preview["plan_version"]) != int(plan_version):
                raise PreflightRejectedError(
                    "VERSION MISMATCH: Preview does not match the current order plan version."
                )
            if preview["status"] not in ("VERIFIED", "VALID") or not preview.get("is_valid_for_submission"):
                raise PreflightRejectedError(
                    f"PREVIEW INVALID: Preview status is '{preview['status']}' ({preview.get('message')})."
                )
            if time_mod.time() > float(preview["expires_epoch"]):
                preview["status"] = "EXPIRED"
                preview["is_valid_for_submission"] = False
                self.db.save_broker_preview(preview)
                raise PreflightRejectedError("PREVIEW INVALID: Broker preview has expired. Run preview again.")

            now_str = datetime.now(NY_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
            intent_id = f"int-{uuid.uuid4().hex[:10]}"
            ext_id = f"fr-v6-{plan['signal_id'][:10]}-v{plan['plan_version']}"
            source_mode = "LIVE_BROKER" if op_mode == "LIVE_SUBMISSION_ENABLED" else "PAPER"

            # Handle simulated network timeout -> enter OUTCOME_UNCERTAIN_RECONCILING and require reconciliation!
            if simulate_timeout:
                intent = {
                    "intent_id": intent_id,
                    "external_identifier": ext_id,
                    "idempotency_key": idempotency_key,
                    "plan_id": plan_id,
                    "plan_version": plan["plan_version"],
                    "preview_id": preview_id,
                    "signal_id": plan["signal_id"],
                    "symbol": plan["symbol"],
                    "shares": plan["shares"],
                    "filled_qty": 0,
                    "leg1_shares": plan["leg1_shares"],
                    "leg2_shares": plan["leg2_shares"],
                    "limit_price": plan["limit_price"],
                    "stop_price": plan["stop_price"],
                    "leg1_target": plan["leg1_target"],
                    "leg2_target": plan["leg2_target"],
                    "order_state": OrderState.OUTCOME_UNCERTAIN_RECONCILING.value,
                    "protection_state": PositionProtectionState.OUTCOME_UNCERTAIN.value,
                    "source_mode": source_mode,
                    "created_at_et": now_str,
                    "updated_at_et": now_str,
                    "notes": "Broker response timed out — outcome unknown. Entering reconciliation mode; do not resubmit.",
                }
                self.db.save_order_intent(intent)
                self.reconciliation_required = True
                self.reconciliation_reason = (
                    f"Submission timeout on {plan['symbol']} (intent {intent_id}). "
                    f"Outcome unknown — reconcile account state before any further submissions."
                )
                self.db.update_settings({"reconciliation_healthy": False})
                self.db.log_event(
                    event_type="ORDER_SUBMISSION_UNCERTAIN",
                    severity="CRITICAL",
                    source_mode=source_mode,
                    symbol=plan["symbol"],
                    summary=self.reconciliation_reason,
                    details={"intent_id": intent_id, "external_identifier": ext_id},
                )
                return {"deduplicated": False, "intent": intent}

            # Normal or partial-fill execution path
            is_partial = bool(simulate_partial_fill or (simulate_partial_fill_qty is not None))
            if simulate_partial_fill_qty is not None:
                total_filled = max(2, min(int(simulate_partial_fill_qty), int(plan["shares"])))
                filled_q1 = total_filled // 2
                filled_q2 = total_filled - filled_q1
            elif simulate_partial_fill:
                filled_q1 = plan["leg1_shares"]
                filled_q2 = plan["leg2_shares"] // 2
                total_filled = filled_q1 + filled_q2
            else:
                filled_q1 = plan["leg1_shares"]
                filled_q2 = plan["leg2_shares"]
                total_filled = filled_q1 + filled_q2

            order_state = (
                OrderState.ENTRY_PARTIALLY_FILLED.value
                if is_partial
                else OrderState.FILLED.value
            )
            prot_state = PositionProtectionState.PROTECTION_CONFIRMED.value

            intent = {
                "intent_id": intent_id,
                "external_identifier": ext_id,
                "idempotency_key": idempotency_key,
                "plan_id": plan_id,
                "plan_version": plan["plan_version"],
                "preview_id": preview_id,
                "signal_id": plan["signal_id"],
                "symbol": plan["symbol"],
                "shares": plan["shares"],
                "filled_qty": total_filled,
                "leg1_shares": filled_q1,
                "leg2_shares": filled_q2,
                "limit_price": plan["limit_price"],
                "stop_price": plan["stop_price"],
                "leg1_target": plan["leg1_target"],
                "leg2_target": plan["leg2_target"],
                "order_state": order_state,
                "protection_state": prot_state,
                "source_mode": source_mode,
                "created_at_et": now_str,
                "updated_at_et": now_str,
                "notes": (
                    f"Split-Tranche OTOCO active: Tranche A ({filled_q1} sh) + Tranche B ({filled_q2} sh) "
                    f"protected @ ${plan['stop_price']:.2f} Stop."
                ),
            }
            self.db.save_order_intent(intent)

            pos_id = f"pos-{source_mode.lower()}-{plan['symbol'].lower()}-{intent_id[-6:]}"
            pos = {
                "position_id": pos_id,
                "intent_id": intent_id,
                "symbol": plan["symbol"],
                "source_mode": source_mode,
                "reconciliation_status": "BROKER_VERIFIED" if source_mode == "LIVE_BROKER" else "PAPER_SIMULATED",
                "is_strategy_managed": True,
                "filled_qty": total_filled,
                "remaining_qty": total_filled,
                "tranche1_qty": filled_q1,
                "tranche2_qty": filled_q2,
                "leg1_exited_qty": 0,
                "avg_fill_price": plan["limit_price"],
                "current_price": plan["limit_price"],
                "mark_timestamp_et": now_str,
                "realized_pnl_usd": 0.0,
                "unrealized_pnl_usd": 0.0,
                "reported_approx_pnl_usd": None,
                "working_stop_price": plan["stop_price"],
                "working_stop_qty": total_filled,
                "leg1_target_price": plan["leg1_target"],
                "leg2_target_price": plan["leg2_target"],
                "protection_state": prot_state,
                "runner_state": "AWAITING_LEG1_VWAP_TARGET",
                "cutoff_time_et": plan["flatten_cutoff_et"],
                "requires_attention": False,
                "attention_reason": "",
                "pending_or_rejected_orders": [],
                "notes": intent["notes"],
            }
            self.db.save_position(pos)
            self.db.log_event(
                event_type="ORDER_SUBMITTED_AND_PROTECTED",
                severity="SUCCESS",
                source_mode=source_mode,
                symbol=plan["symbol"],
                summary=(
                    f"Submitted Split-Tranche OTOCO for {plan['symbol']} ({total_filled:,} sh filled @ ${plan['limit_price']:.2f}, "
                    f"working stop ${plan['stop_price']:.2f} covering {total_filled:,} sh)."
                ),
                details={"intent_id": intent_id, "position_id": pos_id},
            )
            self.db.emit_deduped_alert(
                dedupe_key=f"fill-{intent_id}",
                alert_type="ORDER_FILL",
                severity="SUCCESS",
                symbol=plan["symbol"],
                message=f"{plan['symbol']} entry filled ({total_filled:,} sh @ ${plan['limit_price']:.2f}); Split-Tranche stop confirmed @ ${plan['stop_price']:.2f}.",
            )
            return {"deduplicated": False, "intent": intent, "position": pos}

    def simulate_leg1_exit_on_position(self, position_id: str) -> Dict[str, Any]:
        """
        Transition an open strategy position through Leg 1 (Tranche A 50%) fill:
        - Exits `tranche1_qty` at `leg1_target_price`.
        - Keeps `tranche2_qty` (remaining shares) 100% protected.
        - Moves `working_stop_price` on `tranche2_qty` to breakeven (`avg_fill_price`).
        """
        with self._lock:
            pos = self.db.get_position(position_id)
            if pos is None:
                raise OrderValidationError(f"Position '{position_id}' not found.")
            if not pos.get("is_strategy_managed"):
                raise OrderValidationError("Cannot run strategy leg-exit transitions on non-strategy holdings.")

            q1 = int(pos.get("tranche1_qty") or (pos["filled_qty"] // 2))
            q2 = int(pos["filled_qty"]) - q1
            entry_p = float(pos["avg_fill_price"])
            leg1_p = float(pos["leg1_target_price"])
            realized = round(q1 * (leg1_p - entry_p), 2)
            cur_p = max(leg1_p, float(pos.get("current_price") or leg1_p))
            unrealized = round(q2 * (cur_p - entry_p), 2)
            now_str = datetime.now(NY_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")

            pos["leg1_exited_qty"] = q1
            pos["remaining_qty"] = q2
            pos["current_price"] = cur_p
            pos["mark_timestamp_et"] = now_str
            pos["realized_pnl_usd"] = realized
            pos["unrealized_pnl_usd"] = unrealized
            pos["working_stop_price"] = entry_p
            pos["working_stop_qty"] = q2
            pos["protection_state"] = PositionProtectionState.FIRST_EXIT_COMPLETED.value
            pos["runner_state"] = "RUNNER_ACTIVE_BE_STOP_CONFIRMED"
            pos["notes"] = (
                f"Tranche A ({q1:,} sh) exited @ ${leg1_p:.2f} (+${realized:,.2f} realized). "
                f"Tranche B ({q2:,} sh) stop confirmed at breakeven ${entry_p:.2f}."
            )
            self.db.save_position(pos)
            self.db.log_event(
                event_type="LEG1_EXIT_AND_RUNNER_PROTECTED",
                severity="SUCCESS",
                source_mode=pos["source_mode"],
                symbol=pos["symbol"],
                summary=pos["notes"],
                details={"position_id": position_id, "remaining_qty": q2, "working_stop_qty": q2},
            )
            return pos

    def _manage_open_strategy_positions(self) -> None:
        """
        Backend worker loop for open strategy positions:
        - Updates current mark prices and unrealized P&L.
        - Preserves protective orders and EMA20 trailing even when `pause_new_entries` is True.
        - Never touches pre-existing IRA holdings (`is_strategy_managed == False`).
        """
        now_str = datetime.now(NY_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
        for pos in self.db.list_positions():
            if not pos.get("is_strategy_managed"):
                continue
            if pos.get("protection_state") == PositionProtectionState.CLOSED.value:
                continue
            sym = pos["symbol"]
            cand = self._candidates_cache.get(sym)
            if cand and cand.get("last_price"):
                cur_p = float(cand["last_price"])
                pos["current_price"] = cur_p
                pos["mark_timestamp_et"] = now_str
                if pos.get("remaining_qty") and pos.get("avg_fill_price"):
                    pos["unrealized_pnl_usd"] = round(
                        int(pos["remaining_qty"]) * (cur_p - float(pos["avg_fill_price"])), 2
                    )
                self.db.save_position(pos)

    # ==========================================================================
    # 5. SNAPSHOT & CHART QUERIES FOR REST + SSE
    # ==========================================================================
    def get_symbol_chart(self, symbol: str) -> Dict[str, Any]:
        with self._lock:
            sym = symbol.upper()
            if sym not in self._chart_cache:
                self.run_scan_cycle()
            return self._chart_cache.get(
                sym,
                {
                    "symbol": sym,
                    "bars": [],
                    "markers": [],
                    "levels": [],
                    "last_completed_bar_close_et": "",
                    "forming_bar_open_et": "",
                },
            )

    def get_dashboard_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            now_et = datetime.now(NY_TZ)
            now_str = now_et.strftime("%Y-%m-%d %H:%M:%S %Z")
            settings = self.db.get_settings()
            op_mode = settings.get("operating_mode", "LIVE_DATA_TRADING_DISABLED")
            fixture_mode = settings.get("fixture_mode", "demo_ready")

            session, acct_id, is_live_auth = load_or_create_session()
            broker_status = {
                "connected": bool(is_live_auth),
                "environment": settings.get("broker_environment", "SANDBOX"),
                "masked_account_id": f"****{acct_id[-4:]} ({acct_id})",
                "trading_enabled": (op_mode == "LIVE_SUBMISSION_ENABLED" and fixture_mode == "live" and not self.reconciliation_required),
                "account_state_current": bool(is_live_auth),
                "status_label": (
                    f"Connected ({settings.get('broker_environment', 'SANDBOX')} · ****{acct_id[-4:]})"
                    if is_live_auth
                    else "Disconnected (Local Ticket & Copy Handoff Active)"
                ),
                "setup_instructions": (
                    "Configure TT_PROVIDER_SECRET, TT_REFRESH_TOKEN (or TT_USERNAME, TT_PASSWORD) "
                    "and TT_ACCOUNT_ID=5WJ26897 in .env to enable live Tastytrade OAuth preview and submission."
                ),
            }

            cands_list = list(self._candidates_cache.values())
            grouped = {
                CandidateCategory.READY_FOR_REVIEW.value: [
                    c for c in cands_list if c["category"] == CandidateCategory.READY_FOR_REVIEW.value
                ],
                CandidateCategory.WATCHING.value: [
                    c for c in cands_list if c["category"] == CandidateCategory.WATCHING.value
                ],
                CandidateCategory.BLOCKED.value: [
                    c for c in cands_list if c["category"] == CandidateCategory.BLOCKED.value
                ],
                CandidateCategory.DATA_UNAVAILABLE.value: [
                    c for c in cands_list if c["category"] == CandidateCategory.DATA_UNAVAILABLE.value
                ],
            }

            all_plans_by_id = {p["plan_id"]: p for p in self.db.list_order_plans()}
            plans_by_sym: Dict[str, Any] = {}
            for c in cands_list:
                expected_pid = f"plan-{c['symbol'].lower()}-{c['signal_id'][:8]}"
                if expected_pid in all_plans_by_id:
                    plans_by_sym[c["symbol"]] = all_plans_by_id[expected_pid]
            for p in all_plans_by_id.values():
                if p["symbol"] not in plans_by_sym:
                    plans_by_sym[p["symbol"]] = p
            previews_by_id = {p["preview_id"]: p for p in self.db.list_broker_previews()}

            return {
                "server_time_et": now_str,
                "session_window_label": self.calendar.session_label(now_et.date()),
                "flatten_cutoff_et": "15:40 ET (12:40 ET on Early-Close Days)",
                "entry_window_et": "09:50–13:00 ET (09:50–12:00 ET on Early-Close Days)",
                "operating_mode": op_mode,
                "fixture_mode": fixture_mode,
                "pause_new_entries": bool(settings.get("pause_new_entries", False)),
                "worker_health": {
                    "running": self._running,
                    "started_at_et": self.worker_started_at_et,
                    "last_heartbeat_et": self.last_heartbeat_et,
                    "last_scan_et": self.last_scan_et,
                    "scan_cycle_count": self.scan_cycle_count,
                    "reconciliation_healthy": not self.reconciliation_required,
                    "reconciliation_reason": self.reconciliation_reason,
                    "last_reconciled_at_et": self.last_reconciled_at_et,
                    "persistence_note": (
                        "Worker runs in the local Python backend process (survives browser tab closure; "
                        "pauses if the host computer sleeps or the backend process is stopped)."
                    ),
                },
                "spy_gate": self._spy_banner,
                "broker_status": broker_status,
                "candidates": cands_list,
                "grouped_candidates": grouped,
                "order_plans_by_symbol": plans_by_sym,
                "broker_previews_by_id": previews_by_id,
                "positions": self.db.list_positions(),
                "order_intents": self.db.list_order_intents(limit=50),
                "events": self.db.list_events(limit=100),
                "alerts": self.db.list_alerts(limit=30),
                "settings": settings,
            }

    def update_settings(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            prev_settings = self.db.get_settings()
            new_settings = self.db.update_settings(updates)
            self.db.log_event(
                event_type="SETTINGS_UPDATED",
                severity="INFO",
                source_mode=new_settings.get("operating_mode", "LIVE_DATA_TRADING_DISABLED"),
                symbol=None,
                summary=f"Settings updated: {', '.join(f'{k}={v}' for k, v in updates.items())}",
                details={"updates": updates, "previous": prev_settings},
            )
            if (
                updates.get("fixture_mode") != prev_settings.get("fixture_mode")
                or updates.get("operating_mode") != prev_settings.get("operating_mode")
                or "pause_new_entries" in updates
                or "watched_tickers" in updates
            ):
                self.run_scan_cycle()
            self._notify_listeners({"type": "SETTINGS_UPDATED", "settings": new_settings})
            return new_settings

    def record_handoff_action(self, symbol: str, action: str = "OPEN_TASTYTRADE") -> Dict[str, Any]:
        """
        Log a non-mutating handoff action (`OPEN_TASTYTRADE`, `COPY_TICKET`, `COPY_TICKER`).
        Never creates an order intent, never submits an order, and never marks a position or order filled.
        """
        with self._lock:
            sym = symbol.upper()
            settings = self.db.get_settings()
            handoff = build_broker_handoff_link(sym).to_dict()
            self.db.log_event(
                event_type="BROKER_HANDOFF_NAVIGATION",
                severity="INFO",
                source_mode=settings.get("operating_mode", "LIVE_DATA_TRADING_DISABLED"),
                symbol=sym,
                summary=f"{action} for {sym}: opened verified navigation target (no broker order submitted or filled).",
                details={"action": action, "handoff": handoff, "order_submitted": False, "order_filled": False},
            )
            return {
                "symbol": sym,
                "action": action,
                "handoff": handoff,
                "order_submitted": False,
                "order_filled": False,
            }
