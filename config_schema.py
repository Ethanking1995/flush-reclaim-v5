"""
config_schema.py
================
Explicit separation of Strategy Parameters, Execution Assumptions, and Account Risk Limits,
plus explicit validation exception types that remain active under `python -O`.
"""

from __future__ import annotations
from dataclasses import dataclass, asdict
import hashlib
import json
from typing import Tuple


# ==============================================================================
# EXPLICIT VALIDATION & EXECUTION EXCEPTIONS (Active under `python -O`)
# ==============================================================================

class AuditError(Exception):
    """Base exception for all audited V6 strategy and execution errors."""


class OrderValidationError(AuditError):
    """Raised when an order fails hard safety or parameter validation."""


class PreflightRejectedError(AuditError):
    """Raised when broker preflight fails, returns errors, or is incomplete/uncertain."""


class SessionEnvironmentMismatchError(AuditError):
    """Raised when a cached session environment or account does not match the target configuration."""


class RiskLimitExceededError(AuditError):
    """Raised when an order would violate account, symbol, buying power, or daily loss limits."""


class StaleSignalError(AuditError):
    """Raised when a signal is not from the latest completed bar, outside the entry window, or fails pre-submit revalidation."""


class ReconciliationRequiredError(AuditError):
    """Raised when startup or post-disconnect reconciliation detects unmanaged or conflicting broker state."""


class ModeConflictError(AuditError):
    """Raised when CLI operating mode flags conflict or are ambiguous."""


# ==============================================================================
# 1. STRATEGY PARAMETERS (Preserving V5 Economic Hypothesis & Thresholds)
# ==============================================================================

@dataclass(frozen=True)
class StrategyConfig:
    # Session & Entry Window (America/New_York)
    timezone: str = "America/New_York"
    entry_start_hhmm: Tuple[int, int] = (9, 50)
    entry_end_hhmm: Tuple[int, int] = (13, 0)
    regular_flatten_hhmm: Tuple[int, int] = (15, 40)
    early_close_flatten_hhmm: Tuple[int, int] = (12, 40)

    # Explicitly named daily regime policy:
    # - "core_v5_dislocated_sma5": V5 Core-3 daily regime proxy (Dislocated >=12% off 60d high + Close > Rising SMA5 + 5d UpVol >=55% or AfternoonRampAccum)
    # - "scanner_hl10_atr_compression": Dynamic Universe Scanner proxy (HL_10d >= 6, ATR_Pct <= 4.5%, Vol_10d/20d >= 1.0)
    daily_regime_policy: str = "core_v5_dislocated_sma5"

    # Dynamic Scanner Daily Proxy Parameters
    accum_lookback_days: int = 10
    accum_min_higher_lows: int = 6
    accum_atr_period: int = 14
    accum_max_atr_pct: float = 0.045
    accum_vol_short_window: int = 10
    accum_vol_long_window: int = 20
    accum_min_vol_ratio: float = 1.0

    # Intraday 5-Minute Flush & Volume Climax Thresholds (Exact V5 Thresholds)
    min_tod_history_sessions: int = 5     # Minimum prior sessions required for valid ToD_MedVol / Midday_MedVol
    tod_rolling_sessions: int = 15        # V5 15-session rolling median per HH:MM slot
    min_flush_drop_pct: float = -0.0110   # V5: drop_from_high <= -1.10%
    min_vwap_discount_pct: float = -0.0035  # V5: below_vwap <= -0.35%
    vol_ma10_ex_open_mult: float = 1.80   # V5: Volume >= 1.80x VolMA10_ExOpen
    tod_climax_mult: float = 1.65         # V5: Volume >= 1.65x min(ToD_MedVol, Midday_MedVol)
    deep_flush_drop_pct: float = -0.0150
    deep_vwap_discount_pct: float = -0.0070

    # Higher-Low & Hammer Reclaim Thresholds (Exact V5 Thresholds)
    hl_min_bounce_pct: float = 0.0015
    hl_max_bounce_base_pct: float = 0.0065
    hl_max_bounce_rally_pct: float = 0.0080
    hl_max_close_vs_vwap_pct: float = -0.0010
    hammer_min_close_pos_pct: float = 0.75
    hammer_min_bounce_pct: float = 0.0040
    hammer_max_bounce_pct: float = 0.0080
    hammer_max_below_vwap_pct: float = -0.0065

    # Stop & Target Parameters (Exact V5 Thresholds + Explicit Stop-Cap Policy)
    flush_stop_buffer_pct: float = 0.0005  # 5 bps below flush low
    min_stop_pct: float = 0.0035           # 0.35% below flush_low/entry
    max_stop_pct: float = 0.0065           # 0.65% advertised max stop distance
    max_structural_stop_pct: float = 0.0125  # 1.25% hard structural ceiling
    # Stop policy options:
    # - "strict_cap_reject": Rejects any trade where structural stop (flush_low * 0.9995) is >0.65% below entry
    # - "structural_risk_sized": Places stop at structural level (flush_low * 0.9995) and sizes shares down so $ risk <= planned cap ($975 on $150k)
    # - "legacy_v5_capped": Reproduces legacy V5 formula `min(flush_low*0.9995, max(flush_low*0.9965, entry*0.9935))` without rejecting >0.65%
    stop_policy: str = "structural_risk_sized"

    # Leg 1 & Leg 2 Exit Parameters
    leg1_min_profit_pct: float = 0.0040    # +0.40% minimum on Leg 1 (or prior bar VWAP)
    leg2_base_target_pct: float = 0.0085   # +0.85% standard target on Leg 2
    leg2_stretch_target_pct: float = 0.0160  # +1.60% stretch target when runner active
    runner_activation_profit_pct: float = 0.0075  # Close >= +0.75% and Close > EMA12 activates runner at bar close
    runner_ema20_buffer_pct: float = 0.0015       # Trail EMA20 * (1 - 0.0015)
    runner_close_safety_buffer_pct: float = 0.0010 # Runner stop must be <= Close * (1 - 0.0010)

    # Independent Filter Switches (for clean single-factor ablations)
    use_accum_regime_filter: bool = True
    use_er_blackout_filter: bool = True
    use_spy_waterfall_filter: bool = True

    # Filter Parameters
    er_blackout_calendar_days: int = 14
    er_unknown_policy: str = "block"       # "block" (conservative) or "allow"
    spy_drop_from_high_limit: float = -0.0100  # V5: SPY DropFromHigh <= -1.00%
    spy_vwap_dist_limit: float = -0.0035       # V5: SPY BelowVWAP <= -0.35%
    spy_deep_crash_limit: float = -0.0200      # V5: SPY DropFromHigh <= -2.00%

    def to_dict(self) -> dict:
        return asdict(self)

    def config_sha256(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ==============================================================================
# 2. EXECUTION ASSUMPTIONS (Slippage, Latency, Spread & Drift Guards)
# ==============================================================================

@dataclass(frozen=True)
class ExecutionConfig:
    entry_slippage_bps: float = 1.5        # 1.5 bps per side on entry (3.0 bps round-trip = 0.03%)
    exit_slippage_bps: float = 1.5         # 1.5 bps per side on stop/flatten exits
    limit_exit_slippage_bps: float = 1.5   # 1.5 bps per side fee/friction matching V5 -0.03% round-trip
    entry_delay_bars: int = 0              # 0 = enter at signal bar close; 1 = next bar open sensitivity
    ambiguous_bar_policy: str = "stop_first"  # Conservative: if both stop and target hit in same bar, stop triggers first

    # Live Pre-Submission Revalidation Thresholds
    max_bid_ask_spread_dollars: float = 0.04
    max_bid_ask_spread_pct: float = 0.0008  # 0.08%
    max_signal_age_seconds: float = 360.0   # Signal must belong to the latest completed 5m bar
    max_price_drift_pct: float = 0.0020     # Reject if live quote drifted > 0.20% from signal close
    min_share_price: float = 20.00
    max_share_price: float = 200.00

    def to_dict(self) -> dict:
        return asdict(self)


# ==============================================================================
# 3. ACCOUNT & PORTFOLIO RISK LIMITS
# ==============================================================================

@dataclass(frozen=True)
class RiskConfig:
    target_account_id: str = "5WJ26897"
    target_environment: str = "PRODUCTION"  # "SANDBOX" or "PRODUCTION"
    initial_account_equity_usd: float = 300_000.00
    target_clip_usd: float = 150_000.00
    max_notional_per_order_usd: float = 152_000.00
    max_shares_per_order: int = 3_000
    max_planned_stop_loss_usd: float = 1_500.00
    max_concurrent_positions: int = 2
    max_aggregate_exposure_usd: float = 300_000.00
    max_symbol_exposure_usd: float = 152_000.00
    max_daily_realized_loss_usd: float = 2_500.00
    simultaneous_signal_tiebreaker: str = "climax_vol_ratio_desc"

    def to_dict(self) -> dict:
        return asdict(self)
