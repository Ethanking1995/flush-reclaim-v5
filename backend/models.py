"""
backend/models.py
=================
Typed Pydantic v2 request and response models for the Flush & Reclaim Web Dashboard.
"""

from __future__ import annotations
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, model_validator


class OperatingMode(str, Enum):
    DEMO_REPLAY = "DEMO_REPLAY"
    LIVE_DATA_TRADING_DISABLED = "LIVE_DATA_TRADING_DISABLED"
    PAPER_TRADING = "PAPER_TRADING"
    BROKER_PREVIEW = "BROKER_PREVIEW"
    LIVE_SUBMISSION_ENABLED = "LIVE_SUBMISSION_ENABLED"


class CandidateCategory(str, Enum):
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    WATCHING = "WATCHING"
    BLOCKED = "BLOCKED"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"


class GateStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class GateResultModel(BaseModel):
    gate_id: str
    gate_name: str
    status: GateStatus
    observed_value: str
    threshold: str
    required_threshold: Optional[str] = None
    timestamp_et: str
    summary: str
    explanation: Optional[str] = None
    details: str

    @model_validator(mode="after")
    def _populate_frontend_aliases(self) -> "GateResultModel":
        if not self.required_threshold:
            self.required_threshold = self.threshold
        if not self.explanation:
            self.explanation = self.summary
        return self


class CandleBarModel(BaseModel):
    time: int
    bar_open_et: str
    bar_close_et: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    vwap: float
    ema12: float
    ema20: float
    is_completed: bool


class ChartMarkerModel(BaseModel):
    time: int
    bar_open_et: str
    kind: str  # "FLUSH_LOW" | "VOLUME_CLIMAX" | "CONFIRMED_SIGNAL" | "BROKER_FILL"
    price: float
    label: str
    source: str  # "ENGINE" | "BROKER_FILL" | "MANUAL_REPORTED"


class ChartLevelModel(BaseModel):
    level_id: str
    kind: str  # "ENTRY" | "STOP" | "LEG1_TARGET" | "LEG2_TARGET" | "WORKING_STOP" | "WORKING_LIMIT" | "MANUAL_ENTRY"
    price: float
    label: str
    source: str  # "PROPOSED_PLAN" | "BROKER_CONFIRMED_ORDER" | "MANUAL_USER_REPORTED"


class CandidateSignalModel(BaseModel):
    signal_id: str
    signal_version: int
    symbol: str
    session_date: str
    signal_state: str
    category: CandidateCategory
    primary_status_badge: str
    concise_reason: str
    signal_time_et: Optional[str] = None
    signal_age_seconds: Optional[float] = None
    is_actionable: bool
    actionability_reason: str
    observation_time_et: str
    bar_open_et: str
    bar_close_et: str
    fetch_time_et: str
    decision_time_et: str
    strategy_version: str
    last_price: Optional[float] = None
    session_vwap: Optional[float] = None
    vs_vwap_pct: Optional[float] = None
    morning_high: Optional[float] = None
    flush_low: Optional[float] = None
    flush_pct: Optional[float] = None
    volume_climax_mult: Optional[float] = None
    why_now_or_missing: str
    gates: List[GateResultModel]
    reference_trigger_price: Optional[float] = None
    reference_trigger_timestamp_et: Optional[str] = None
    reference_trigger_meaning: Optional[str] = None
    consistency_check: Dict[str, Any]
    provenance: Dict[str, Any]


class OrderPlanModel(BaseModel):
    plan_id: str
    plan_version: int
    signal_id: str
    signal_version: int
    symbol: str
    side: str = "BUY_TO_OPEN"
    strategy_version: str
    signal_bar_close_et: str
    created_at_et: str
    allocation_usd: float
    shares: int
    leg1_shares: int
    leg2_shares: int
    entry_order_type: str = "LIMIT"
    limit_price: float
    stop_price: float
    leg1_target: float
    leg2_target: float
    runner_stretch_target: float
    runner_ema20_level: Optional[float] = None
    notional_usd: float
    planned_risk_usd: float
    planned_risk_pct: float
    estimated_fees_usd: float = 0.0
    is_manual_override: bool = False
    override_notes: str = ""
    masked_account_id: str = "****6897 (5WJ26897)"
    available_buying_power_usd: float = 300000.0
    buying_power_timestamp_et: str = ""
    quote_freshness_seconds: float = 0.0
    is_executable: bool = False
    execution_blocked_reason: str = ""
    flatten_cutoff_et: str = "15:40 ET"
    copy_ticket_text: str = ""
    broker_handoff_link: Dict[str, Any] = Field(default_factory=dict)
    active_preview_id: Optional[str] = None


class BrokerPreviewModel(BaseModel):
    preview_id: str
    plan_id: str
    plan_version: int
    signal_id: str
    signal_version: int
    account_id: str
    masked_account_id: str
    symbol: str
    side: str
    shares: int
    leg1_shares: int
    leg2_shares: int
    limit_price: float
    stop_price: float
    leg1_target: float
    leg2_target: float
    environment: str
    validation_mode: str  # "BROKER_PREFLIGHT_VERIFIED" | "LOCAL_SCHEMA_VALIDATION_ONLY"
    status: str           # "VERIFIED" | "FAILED" | "EXPIRED" | "INVALIDATED_BY_EDIT"
    created_at_et: str
    expires_at_et: str
    expires_epoch: float
    is_valid_for_submission: bool
    message: str
    tranche_a_payload: Dict[str, Any]
    tranche_b_payload: Dict[str, Any]


class OrderIntentModel(BaseModel):
    intent_id: str
    external_identifier: str
    idempotency_key: str
    plan_id: str
    plan_version: int
    preview_id: Optional[str] = None
    symbol: str
    shares: int
    leg1_shares: int
    leg2_shares: int
    limit_price: float
    stop_price: float
    leg1_target: float
    leg2_target: float
    order_state: str
    protection_state: str
    source_mode: str
    created_at_et: str
    updated_at_et: str
    notes: str = ""


class PositionModel(BaseModel):
    position_id: str
    symbol: str
    source_mode: str               # "LIVE_BROKER" | "PAPER" | "REPLAY" | "MANUAL_USER_REPORTED"
    reconciliation_status: str     # "BROKER_VERIFIED" | "PAPER_SIMULATED" | "UNRECONCILED_USER_REPORTED" | "PRE_EXISTING_HOLDING_EXCLUDED" | "RECONCILIATION_REQUIRED"
    is_strategy_managed: bool
    filled_qty: Optional[int] = None
    remaining_qty: Optional[int] = None
    leg1_exited_qty: Optional[int] = None
    avg_fill_price: Optional[float] = None
    current_price: Optional[float] = None
    mark_timestamp_et: str
    realized_pnl_usd: Optional[float] = None
    unrealized_pnl_usd: Optional[float] = None
    reported_approx_pnl_usd: Optional[float] = None
    working_stop_price: Optional[float] = None
    working_stop_qty: Optional[int] = None
    leg1_target_price: Optional[float] = None
    leg2_target_price: Optional[float] = None
    protection_state: str
    runner_state: str
    cutoff_time_et: str
    requires_attention: bool
    attention_reason: str = ""
    pending_or_rejected_orders: List[Dict[str, Any]] = Field(default_factory=list)
    notes: str = ""


class EventModel(BaseModel):
    event_id: str
    timestamp_et: str
    event_type: str
    severity: str  # "INFO" | "SUCCESS" | "WARNING" | "CRITICAL"
    symbol: Optional[str] = None
    source_mode: str
    summary: str
    details: Dict[str, Any] = Field(default_factory=dict)


class AlertModel(BaseModel):
    alert_id: str
    dedupe_key: str
    timestamp_et: str
    alert_type: str  # "NEW_CONFIRMED_SIGNAL" | "ORDER_FILL" | "REJECTED_PROTECTIVE_ORDER" | "STALE_REQUIRED_DATA" | "POSITION_ATTENTION"
    severity: str
    symbol: Optional[str] = None
    message: str
    acknowledged: bool = False


class PlanEditRequest(BaseModel):
    plan_id: Optional[str] = None
    symbol: Optional[str] = None
    allocation_usd: Optional[float] = None
    limit_price: Optional[float] = None
    manual_stop_override: Optional[float] = None
    manual_leg1_override: Optional[float] = None
    manual_leg2_override: Optional[float] = None


class PreviewOrderRequest(BaseModel):
    plan_id: str
    plan_version: int
    simulate_broker_error: bool = False
    simulate_failure: bool = False


class SubmitOrderRequest(BaseModel):
    plan_id: str
    plan_version: int
    preview_id: Optional[str] = None
    idempotency_key: str
    confirmed_by_user: Optional[bool] = None
    explicit_confirmation_checked: Optional[bool] = None
    simulate_timeout: bool = False
    simulate_partial_fill: bool = False
    simulate_partial_fill_qty: Optional[int] = None


class SettingsUpdateRequest(BaseModel):
    operating_mode: Optional[OperatingMode] = None
    fixture_mode: Optional[str] = None  # "live" | "demo_ready" | "demo_acceptance" | "demo_stale"
    pause_new_entries: Optional[bool] = None
    default_allocation_usd: Optional[float] = None
    max_share_cap: Optional[int] = None
    max_planned_risk_usd: Optional[float] = None
    sound_alerts_enabled: Optional[bool] = None
    broker_environment: Optional[str] = None  # "SANDBOX" | "PRODUCTION"
    universe_tickers: Optional[List[str]] = None


class ManualTradeNoteRequest(BaseModel):
    symbol: str
    avg_fill_price: Optional[float] = None
    reported_entry_price: Optional[float] = None
    reported_shares: Optional[int] = None
    reported_approx_pnl_usd: Optional[float] = None
    working_stop_price: Optional[float] = None
    reported_stop_price: Optional[float] = None
    reported_leg1_exit_price: Optional[float] = None
    notes: str = ""


class ReconcileRequest(BaseModel):
    simulate_unprotected: bool = False


ManualTradeCreateRequest = ManualTradeNoteRequest
ReconcileTriggerRequest = ReconcileRequest
