"""
backend/database.py
===================
SQLite persistence layer for signals, versioned order plans, broker previews,
order intents, positions, durable event history, deduplicated alerts, and settings.
"""

from __future__ import annotations
from datetime import datetime
import json
import os
import sqlite3
import threading
import uuid
from typing import Any, Dict, List, Optional

from market_calendar import NY_TZ


DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "flush_reclaim_dashboard.db",
)


class DashboardDatabase:
    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        self._lock = threading.RLock()
        self._init_schema()
        self._seed_defaults()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at_et TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS signals (
                    signal_id TEXT PRIMARY KEY,
                    signal_version INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    session_date TEXT NOT NULL,
                    signal_state TEXT NOT NULL,
                    category TEXT NOT NULL,
                    is_actionable INTEGER NOT NULL,
                    observation_time_et TEXT NOT NULL,
                    bar_open_et TEXT NOT NULL,
                    bar_close_et TEXT NOT NULL,
                    fetch_time_et TEXT NOT NULL,
                    decision_time_et TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS order_plans (
                    plan_id TEXT PRIMARY KEY,
                    plan_version INTEGER NOT NULL,
                    signal_id TEXT NOT NULL,
                    signal_version INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    created_at_et TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS broker_previews (
                    preview_id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL,
                    plan_version INTEGER NOT NULL,
                    signal_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    status TEXT NOT NULL,
                    expires_epoch REAL NOT NULL,
                    created_at_et TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS order_intents (
                    intent_id TEXT PRIMARY KEY,
                    external_identifier TEXT UNIQUE NOT NULL,
                    idempotency_key TEXT UNIQUE NOT NULL,
                    plan_id TEXT NOT NULL,
                    plan_version INTEGER NOT NULL,
                    preview_id TEXT,
                    symbol TEXT NOT NULL,
                    order_state TEXT NOT NULL,
                    protection_state TEXT NOT NULL,
                    source_mode TEXT NOT NULL,
                    created_at_et TEXT NOT NULL,
                    updated_at_et TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS positions (
                    position_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    source_mode TEXT NOT NULL,
                    reconciliation_status TEXT NOT NULL,
                    is_strategy_managed INTEGER NOT NULL,
                    protection_state TEXT NOT NULL,
                    updated_at_et TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    timestamp_et TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    symbol TEXT,
                    source_mode TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    details_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS alerts (
                    alert_id TEXT PRIMARY KEY,
                    dedupe_key TEXT UNIQUE NOT NULL,
                    timestamp_et TEXT NOT NULL,
                    alert_type TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    symbol TEXT,
                    message TEXT NOT NULL,
                    acknowledged INTEGER NOT NULL DEFAULT 0
                );
                """
            )

    def _now_et_str(self) -> str:
        return datetime.now(NY_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")

    def _seed_defaults(self) -> None:
        now_str = self._now_et_str()
        default_settings = {
            "operating_mode": "LIVE_DATA_TRADING_DISABLED",
            "fixture_mode": "demo_ready",
            "pause_new_entries": False,
            "default_allocation_usd": 150000.0,
            "max_notional_usd": 152000.0,
            "max_share_cap": 3000,
            "max_planned_risk_usd": 1500.0,
            "max_concurrent_positions": 2,
            "sound_alerts_enabled": False,
            "broker_environment": "SANDBOX",
            "allowed_account_id": "5WJ26897",
            "universe_tickers": ["FISV", "PLNT", "EEFT", "CNC", "GPN", "NU"],
            "reconciliation_healthy": True,
        }
        with self._lock, self._conn() as conn:
            for k, v in default_settings.items():
                conn.execute(
                    "INSERT OR IGNORE INTO settings (key, value_json, updated_at_et) VALUES (?, ?, ?)",
                    (k, json.dumps(v), now_str),
                )

            # Seed the user's reported FISV manual trade (Section 1 & Section 8):
            # Treat as user-reported until reconciled with broker records. Do NOT invent quantity,
            # exit, timestamps, or realized/unrealized breakdown.
            manual_fisv_id = "pos-manual-fisv-reported"
            row = conn.execute(
                "SELECT position_id FROM positions WHERE position_id = ?",
                (manual_fisv_id,),
            ).fetchone()
            if row is None:
                manual_pos = {
                    "position_id": manual_fisv_id,
                    "symbol": "FISV",
                    "source_mode": "MANUAL_USER_REPORTED",
                    "reconciliation_status": "UNRECONCILED_USER_REPORTED",
                    "reconciliation_badge": "UNRECONCILED_USER_REPORTED",
                    "is_strategy_managed": False,
                    "filled_qty": None,
                    "remaining_qty": None,
                    "leg1_exited_qty": None,
                    "avg_fill_price": 62.72,
                    "current_price": 63.73,
                    "mark_timestamp_et": now_str,
                    "realized_pnl_usd": None,
                    "unrealized_pnl_usd": None,
                    "reported_approx_pnl_usd": 3000.0,
                    "reported_Approximate_pnl_usd": 3000.0,
                    "working_stop_price": 63.50,
                    "working_stop_qty": None,
                    "leg1_target_price": 63.18,
                    "leg2_target_price": 63.67,
                    "protection_state": "OUTCOME_UNCERTAIN",
                    "runner_state": "USER_REPORTED_STOP_AT_63_50",
                    "cutoff_time_et": "15:40 ET",
                    "requires_attention": False,
                    "attention_reason": "User-reported manual trade awaiting broker account reconciliation.",
                    "pending_or_rejected_orders": [],
                    "notes": (
                        "User-reported workflow record: FISV entry at $62.72, 50% scaled at $63.18, "
                        "stop trailed to $63.50, approx. +$3,000 reported profit. "
                        "Not combined with broker-verified P&L; exact quantity and fill timestamps unverified."
                    ),
                }
                conn.execute(
                    """
                    INSERT INTO positions
                    (position_id, symbol, source_mode, reconciliation_status, is_strategy_managed, protection_state, updated_at_et, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        manual_fisv_id,
                        "FISV",
                        "MANUAL_USER_REPORTED",
                        "UNRECONCILED_USER_REPORTED",
                        0,
                        "OUTCOME_UNCERTAIN",
                        now_str,
                        json.dumps(manual_pos),
                    ),
                )

            # Seed pre-existing non-strategy holding (Section 7: Existing investment holdings must not be adopted or liquidated)
            pre_existing_id = "pos-preexisting-sgov"
            row_sgov = conn.execute(
                "SELECT position_id FROM positions WHERE position_id = ?",
                (pre_existing_id,),
            ).fetchone()
            if row_sgov is None:
                sgov_pos = {
                    "position_id": pre_existing_id,
                    "symbol": "SGOV",
                    "source_mode": "LIVE_BROKER",
                    "reconciliation_status": "PRE_EXISTING_HOLDING_EXCLUDED",
                    "reconciliation_badge": "PRE_EXISTING_HOLDING_ISOLATED",
                    "is_strategy_managed": False,
                    "filled_qty": 500,
                    "remaining_qty": 500,
                    "leg1_exited_qty": 0,
                    "avg_fill_price": 100.51,
                    "current_price": 100.52,
                    "mark_timestamp_et": now_str,
                    "realized_pnl_usd": 0.0,
                    "unrealized_pnl_usd": 50.0,
                    "reported_approx_pnl_usd": None,
                    "working_stop_price": None,
                    "working_stop_qty": 0,
                    "leg1_target_price": None,
                    "leg2_target_price": None,
                    "protection_state": "CLOSED",
                    "runner_state": "EXCLUDED_NON_STRATEGY_HOLDING",
                    "cutoff_time_et": "N/A (Excluded Treasury ETF Holding)",
                    "requires_attention": False,
                    "attention_reason": "",
                    "pending_or_rejected_orders": [],
                    "notes": "Pre-existing IRA cash-equivalent holding. Explicitly excluded from strategy management and 15:40 ET flatten.",
                }
                conn.execute(
                    """
                    INSERT INTO positions
                    (position_id, symbol, source_mode, reconciliation_status, is_strategy_managed, protection_state, updated_at_et, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        pre_existing_id,
                        "SGOV",
                        "LIVE_BROKER",
                        "PRE_EXISTING_HOLDING_EXCLUDED",
                        0,
                        "CLOSED",
                        now_str,
                        json.dumps(sgov_pos),
                    ),
                )

    def get_settings(self) -> Dict[str, Any]:
        with self._lock, self._conn() as conn:
            rows = conn.execute("SELECT key, value_json FROM settings").fetchall()
            return {r["key"]: json.loads(r["value_json"]) for r in rows}

    def update_settings(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        now_str = self._now_et_str()
        with self._lock, self._conn() as conn:
            for k, v in updates.items():
                if v is not None:
                    conn.execute(
                        "INSERT OR REPLACE INTO settings (key, value_json, updated_at_et) VALUES (?, ?, ?)",
                        (k, json.dumps(v), now_str),
                    )
        return self.get_settings()

    def save_signal(self, sig: Dict[str, Any]) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO signals
                (signal_id, signal_version, symbol, session_date, signal_state, category, is_actionable,
                 observation_time_et, bar_open_et, bar_close_et, fetch_time_et, decision_time_et, strategy_version, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sig["signal_id"],
                    int(sig["signal_version"]),
                    sig["symbol"],
                    sig["session_date"],
                    sig["signal_state"],
                    sig["category"],
                    1 if sig["is_actionable"] else 0,
                    sig["observation_time_et"],
                    sig["bar_open_et"],
                    sig["bar_close_et"],
                    sig["fetch_time_et"],
                    sig["decision_time_et"],
                    sig["strategy_version"],
                    json.dumps(sig),
                ),
            )

    def list_signals(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM signals ORDER BY decision_time_et DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [json.loads(r["payload_json"]) for r in rows]

    def save_order_plan(self, plan: Dict[str, Any]) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO order_plans
                (plan_id, plan_version, signal_id, signal_version, symbol, created_at_et, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan["plan_id"],
                    int(plan["plan_version"]),
                    plan["signal_id"],
                    int(plan["signal_version"]),
                    plan["symbol"],
                    plan["created_at_et"],
                    json.dumps(plan),
                ),
            )

    def get_order_plan(self, plan_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT payload_json FROM order_plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
            return json.loads(row["payload_json"]) if row else None

    def list_order_plans(self) -> List[Dict[str, Any]]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM order_plans ORDER BY created_at_et DESC"
            ).fetchall()
            return [json.loads(r["payload_json"]) for r in rows]

    def save_broker_preview(self, preview: Dict[str, Any]) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO broker_previews
                (preview_id, plan_id, plan_version, signal_id, symbol, status, expires_epoch, created_at_et, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    preview["preview_id"],
                    preview["plan_id"],
                    int(preview["plan_version"]),
                    preview["signal_id"],
                    preview["symbol"],
                    preview["status"],
                    float(preview["expires_epoch"]),
                    preview["created_at_et"],
                    json.dumps(preview),
                ),
            )

    def invalidate_previews_for_plan(self, plan_id: str) -> None:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT preview_id, payload_json FROM broker_previews WHERE plan_id = ?",
                (plan_id,),
            ).fetchall()
            for r in rows:
                p = json.loads(r["payload_json"])
                p["status"] = "INVALIDATED_BY_EDIT"
                p["is_valid_for_submission"] = False
                p["message"] = "Preview invalidated because the order plan was edited. Run 'Preview with Tastytrade' again."
                conn.execute(
                    "UPDATE broker_previews SET status = ?, payload_json = ? WHERE preview_id = ?",
                    ("INVALIDATED_BY_EDIT", json.dumps(p), r["preview_id"]),
                )

    def get_broker_preview(self, preview_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT payload_json FROM broker_previews WHERE preview_id = ?",
                (preview_id,),
            ).fetchone()
            return json.loads(row["payload_json"]) if row else None

    def list_broker_previews(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM broker_previews ORDER BY created_at_et DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [json.loads(r["payload_json"]) for r in rows]

    def get_intent_by_idempotency(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT payload_json FROM order_intents WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            return json.loads(row["payload_json"]) if row else None

    def get_active_intent_for_signal(self, signal_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._conn() as conn:
            rows = conn.execute("SELECT payload_json FROM order_intents").fetchall()
            for r in rows:
                d = json.loads(r["payload_json"])
                if d.get("signal_id") == signal_id and d.get("order_state") not in ("REJECTED", "CANCELED"):
                    return d
            return None

    def save_order_intent(self, intent: Dict[str, Any]) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO order_intents
                (intent_id, external_identifier, idempotency_key, plan_id, plan_version, preview_id,
                 symbol, order_state, protection_state, source_mode, created_at_et, updated_at_et, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    intent["intent_id"],
                    intent["external_identifier"],
                    intent["idempotency_key"],
                    intent["plan_id"],
                    int(intent["plan_version"]),
                    intent.get("preview_id"),
                    intent["symbol"],
                    intent["order_state"],
                    intent["protection_state"],
                    intent["source_mode"],
                    intent["created_at_et"],
                    intent["updated_at_et"],
                    json.dumps(intent),
                ),
            )

    def list_order_intents(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM order_intents ORDER BY created_at_et DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [json.loads(r["payload_json"]) for r in rows]

    def save_position(self, pos: Dict[str, Any]) -> None:
        now_str = self._now_et_str()
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO positions
                (position_id, symbol, source_mode, reconciliation_status, is_strategy_managed,
                 protection_state, updated_at_et, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pos["position_id"],
                    pos["symbol"],
                    pos["source_mode"],
                    pos["reconciliation_status"],
                    1 if pos.get("is_strategy_managed") else 0,
                    pos["protection_state"],
                    now_str,
                    json.dumps(pos),
                ),
            )

    def get_position(self, position_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT payload_json FROM positions WHERE position_id = ?",
                (position_id,),
            ).fetchone()
            return json.loads(row["payload_json"]) if row else None

    def list_positions(self) -> List[Dict[str, Any]]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM positions ORDER BY is_strategy_managed DESC, updated_at_et DESC"
            ).fetchall()
            return [json.loads(r["payload_json"]) for r in rows]

    def log_event(
        self,
        event_type: str,
        severity: str,
        source_mode: str,
        summary: str,
        symbol: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        ev = {
            "event_id": f"ev-{uuid.uuid4().hex[:12]}",
            "timestamp_et": self._now_et_str(),
            "event_type": event_type,
            "severity": severity,
            "symbol": symbol,
            "source_mode": source_mode,
            "summary": summary,
            "details": details or {},
        }
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT INTO events
                (event_id, timestamp_et, event_type, severity, symbol, source_mode, summary, details_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ev["event_id"],
                    ev["timestamp_et"],
                    ev["event_type"],
                    ev["severity"],
                    ev["symbol"],
                    ev["source_mode"],
                    ev["summary"],
                    json.dumps(ev["details"]),
                ),
            )
        return ev

    def list_events(self, limit: int = 150) -> List[Dict[str, Any]]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT event_id, timestamp_et, event_type, severity, symbol, source_mode, summary, details_json "
                "FROM events ORDER BY rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [
                {
                    "event_id": r["event_id"],
                    "timestamp_et": r["timestamp_et"],
                    "event_type": r["event_type"],
                    "severity": r["severity"],
                    "symbol": r["symbol"],
                    "source_mode": r["source_mode"],
                    "summary": r["summary"],
                    "details": json.loads(r["details_json"]),
                }
                for r in rows
            ]

    def emit_deduped_alert(
        self,
        dedupe_key: str,
        alert_type: str,
        severity: str,
        message: str,
        symbol: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        now_str = self._now_et_str()
        with self._lock, self._conn() as conn:
            existing = conn.execute(
                "SELECT alert_id FROM alerts WHERE dedupe_key = ?",
                (dedupe_key,),
            ).fetchone()
            if existing is not None:
                return None
            alert = {
                "alert_id": f"alt-{uuid.uuid4().hex[:10]}",
                "dedupe_key": dedupe_key,
                "timestamp_et": now_str,
                "alert_type": alert_type,
                "severity": severity,
                "symbol": symbol,
                "message": message,
                "acknowledged": False,
            }
            conn.execute(
                """
                INSERT INTO alerts
                (alert_id, dedupe_key, timestamp_et, alert_type, severity, symbol, message, acknowledged)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    alert["alert_id"],
                    alert["dedupe_key"],
                    alert["timestamp_et"],
                    alert["alert_type"],
                    alert["severity"],
                    alert["symbol"],
                    alert["message"],
                ),
            )
            return alert

    def list_alerts(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT alert_id, dedupe_key, timestamp_et, alert_type, severity, symbol, message, acknowledged "
                "FROM alerts ORDER BY rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [
                {
                    "alert_id": r["alert_id"],
                    "dedupe_key": r["dedupe_key"],
                    "timestamp_et": r["timestamp_et"],
                    "alert_type": r["alert_type"],
                    "severity": r["severity"],
                    "symbol": r["symbol"],
                    "message": r["message"],
                    "acknowledged": bool(r["acknowledged"]),
                }
                for r in rows
            ]

    def acknowledge_alert(self, alert_id: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "UPDATE alerts SET acknowledged = 1 WHERE alert_id = ?",
                (alert_id,),
            )
