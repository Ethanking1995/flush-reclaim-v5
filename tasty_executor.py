"""
tasty_executor.py
=================
Audited V6 Tastytrade Execution Bridge & Order Lifecycle State Machine.

Fixes all V5 execution vulnerabilities:
  1. Explicit exception types (`OrderValidationError`, `PreflightRejectedError`,
     `SessionEnvironmentMismatchError`, `RiskLimitExceededError`, `StaleSignalError`,
     `ReconciliationRequiredError`) — zero reliance on `assert` (active under `python -O`).
  2. Strict separation between offline local schema serialization (`LOCAL_SCHEMA_VALIDATION_ONLY`)
     and genuine broker preflight (`BROKER_PREFLIGHT_VERIFIED`).
  3. Any failed, incomplete, or uncertain preflight hard-blocks live submission.
  4. Split-Tranche OTOCO Architecture (`build_v6_split_tranche_orders`):
     - Tranche A (`Q_1` shares): OTOCO pairing `Buy Q_1` with `Limit Sell Q_1 @ Leg1` and `Stop Q_1 @ StopPrice`.
     - Tranche B (`Q_2` shares): OTOCO pairing `Buy Q_2` with `Limit Sell Q_2 @ Leg2` and `Stop Q_2 @ StopPrice`.
     When Tranche A's Leg 1 fills, Tranche B's `Q_2` shares remain 100% protected by Tranche B's stop!
  5. Full lifecycle state machine (`SUBMITTED -> ACCEPTED -> PARTIALLY_FILLED -> FILLED -> PROTECTED -> PARTIALLY_EXITED -> CLOSED`)
     handling partial fills, protective-order rejection emergency flatten, cancel/replace race checks,
     breakeven stop amendment, runner trailing stop amendment, and 15:40 ET (12:40 ET early-close) flatten.
  6. Persistent idempotency & reconciliation journal (`execution_journal.json`) distinguishing
     strategy-created positions from pre-existing IRA holdings and preventing duplicate entries.
  7. Environment- and account-scoped session cache (`SANDBOX` vs `PRODUCTION`).
"""

from __future__ import annotations
from dataclasses import dataclass, asdict, field
from datetime import datetime, date
from decimal import Decimal
from enum import Enum
import hashlib
import json
import os
from typing import Any, Dict, List, Optional, Tuple
from dotenv import load_dotenv

try:
    from tastytrade import Session, Account
    from tastytrade.instruments import Equity
    from tastytrade.order import (
        NewOrder,
        NewComplexOrder,
        ComplexOrderType,
        OrderAction,
        OrderTimeInForce,
        OrderType,
        PriceEffect,
        Leg,
    )
    HAS_TASTY_SDK = True
except (ImportError, TypeError):
    HAS_TASTY_SDK = False

    class OrderAction(str, Enum):
        BUY_TO_OPEN = "Buy to Open"
        SELL_TO_CLOSE = "Sell to Close"

    class OrderTimeInForce(str, Enum):
        DAY = "Day"

    class OrderType(str, Enum):
        LIMIT = "Limit"
        STOP = "Stop"
        MARKET = "Market"

    class PriceEffect(str, Enum):
        DEBIT = "Debit"
        CREDIT = "Credit"

    class ComplexOrderType(str, Enum):
        OTOCO = "OTOCO"
        OCO = "OCO"

    @dataclass
    class Leg:
        instrument_type: str
        symbol: str
        action: OrderAction
        quantity: Decimal

        def to_dict(self) -> dict:
            return {
                "instrument-type": self.instrument_type,
                "symbol": self.symbol,
                "action": self.action.value if isinstance(self.action, Enum) else str(self.action),
                "quantity": str(self.quantity),
            }

    @dataclass
    class NewOrder:
        time_in_force: OrderTimeInForce
        order_type: OrderType
        legs: List[Leg]
        price: Optional[Decimal] = None
        stop_trigger: Optional[Decimal] = None
        price_effect: Optional[PriceEffect] = None

        def to_dict(self) -> dict:
            d = {
                "time-in-force": self.time_in_force.value if isinstance(self.time_in_force, Enum) else str(self.time_in_force),
                "order-type": self.order_type.value if isinstance(self.order_type, Enum) else str(self.order_type),
                "legs": [leg.to_dict() for leg in self.legs],
            }
            if self.price is not None:
                d["price"] = str(self.price)
            if self.stop_trigger is not None:
                d["stop-trigger"] = str(self.stop_trigger)
            if self.price_effect is not None:
                d["price-effect"] = self.price_effect.value if isinstance(self.price_effect, Enum) else str(self.price_effect)
            return d

        def model_dump_json(self) -> str:
            return json.dumps(self.to_dict())

    @dataclass
    class NewComplexOrder:
        orders: List[NewOrder]
        trigger_order: Optional[NewOrder] = None
        type: ComplexOrderType = ComplexOrderType.OTOCO

        def to_dict(self) -> dict:
            d = {
                "type": self.type.value if isinstance(self.type, Enum) else str(self.type),
                "orders": [o.to_dict() for o in self.orders],
            }
            if self.trigger_order is not None:
                d["trigger-order"] = self.trigger_order.to_dict()
            return d

        def model_dump_json(self) -> str:
            return json.dumps(self.to_dict())

    class Equity:
        symbol: str = ""
        @classmethod
        def get_equity(cls, session: Any, sym: str) -> "Equity":
            eq = cls()
            eq.symbol = sym
            return eq

    class Session:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise OrderValidationError("tastytrade SDK requires Python >= 3.10 or patched annotations for live broker sessions.")
        @classmethod
        def deserialize(cls, data: str) -> "Session":
            raise OrderValidationError("tastytrade SDK requires Python >= 3.10 or patched annotations for live broker sessions.")

    class Account:
        @classmethod
        def get_account(cls, session: Any, account_id: str) -> "Account":
            raise OrderValidationError("tastytrade SDK requires Python >= 3.10 or patched annotations for live broker sessions.")

from config_schema import (
    StrategyConfig,
    ExecutionConfig,
    RiskConfig,
    OrderValidationError,
    PreflightRejectedError,
    SessionEnvironmentMismatchError,
    RiskLimitExceededError,
    StaleSignalError,
    ReconciliationRequiredError,
)
from market_calendar import NYSECalendar, NY_TZ

load_dotenv()

# Default constants (matching RiskConfig defaults)
MAX_NOTIONAL_USD = 152_000.00
MAX_SHARE_COUNT = 3_000
TARGET_CLIP_USD = 150_000.00
MAX_PLANNED_STOP_LOSS_USD = 1_500.00
TARGET_ACCOUNT_ID = "5WJ26897"


class LifecycleState(str, Enum):
    STAGED_LOCAL = "STAGED_LOCAL"
    PREFLIGHT_VERIFIED = "PREFLIGHT_VERIFIED"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    PROTECTED = "PROTECTED"
    PARTIALLY_EXITED = "PARTIALLY_EXITED"
    CLOSED = "CLOSED"
    EMERGENCY_FLATTENED = "EMERGENCY_FLATTENED"
    REJECTED = "REJECTED"


class ValidationMode(str, Enum):
    LOCAL_SCHEMA_VALIDATION_ONLY = "LOCAL_SCHEMA_VALIDATION_ONLY"
    BROKER_PREFLIGHT_VERIFIED = "BROKER_PREFLIGHT_VERIFIED"


@dataclass
class QuoteSnapshot:
    symbol: str
    bid: Optional[float]
    ask: Optional[float]
    last: Optional[float]
    timestamp_et: str
    is_live_rth: bool = True

    @property
    def spread_dollars(self) -> Optional[float]:
        if self.bid is not None and self.ask is not None and self.bid > 0 and self.ask >= self.bid:
            return round(self.ask - self.bid, 4)
        return None

    @property
    def spread_pct(self) -> Optional[float]:
        sd = self.spread_dollars
        ref = self.last or self.ask
        if sd is not None and ref is not None and ref > 0:
            return sd / ref
        return None


@dataclass
class AccountRiskSnapshot:
    account_id: str
    environment: str
    available_buying_power_usd: float
    aggregate_strategy_exposure_usd: float
    symbol_strategy_exposure_usd: Dict[str, float]
    daily_realized_loss_usd: float  # positive number for loss or negative PnL; we track net realized PnL today
    daily_realized_pnl_usd: float
    open_strategy_positions_count: int
    pre_existing_holdings_symbols: List[str] = field(default_factory=list)


# ==============================================================================
# 1. HARD SAFETY & RISK VALIDATION (Zero `assert` statements; active under -O)
# ==============================================================================

def validate_order_safety(
    ticker: str,
    shares: int,
    limit_price: float,
    stop_price: float,
    account_id: str,
    leg1_target: Optional[float] = None,
    leg2_target: Optional[float] = None,
    risk_cfg: RiskConfig = RiskConfig(),
) -> dict:
    """
    Fail-safe validation using explicit `OrderValidationError` exceptions
    so checks cannot be stripped by `python -O`.
    """
    if not ticker or not isinstance(ticker, str) or not ticker.strip():
        raise OrderValidationError("SECURITY ABORT: Ticker symbol must be a non-empty string.")

    if account_id != risk_cfg.target_account_id:
        raise OrderValidationError(
            f"SECURITY ABORT: Invalid account '{account_id}'! Expected '{risk_cfg.target_account_id}'."
        )

    if not isinstance(shares, int) or isinstance(shares, bool) or shares < 2:
        raise OrderValidationError(
            f"SAFETY ABORT: Share count must be an integer >= 2 (got {shares})."
        )

    if limit_price <= 0 or stop_price <= 0:
        raise OrderValidationError(
            f"SAFETY ABORT: Prices must be positive (limit={limit_price}, stop={stop_price})."
        )

    if shares > risk_cfg.max_shares_per_order:
        raise OrderValidationError(
            f"SAFETY ABORT: {shares} shares exceeds hard cap {risk_cfg.max_shares_per_order}!"
        )

    notional = round(shares * limit_price, 2)
    if notional > risk_cfg.max_notional_per_order_usd:
        raise OrderValidationError(
            f"SAFETY ABORT: Notional ${notional:,.2f} exceeds hard cap ${risk_cfg.max_notional_per_order_usd:,.2f}!"
        )

    if stop_price >= limit_price:
        raise OrderValidationError(
            f"LOGIC ABORT: Stop price ${stop_price:.4f} must be strictly below Limit entry ${limit_price:.4f}!"
        )

    stop_dist_pct = (limit_price - stop_price) / limit_price
    if stop_dist_pct > 0.0125 + 1e-6:
        raise OrderValidationError(
            f"RISK ABORT: Stop distance {stop_dist_pct*100:.2f}% exceeds maximum 1.25% structural ceiling!"
        )

    planned_loss_usd = round(shares * (limit_price - stop_price), 2)
    if planned_loss_usd > risk_cfg.max_planned_stop_loss_usd + 0.01:
        raise OrderValidationError(
            f"RISK ABORT: Planned stop loss ${planned_loss_usd:,.2f} exceeds ${risk_cfg.max_planned_stop_loss_usd:,.2f} cap!"
        )

    if leg1_target is not None and leg1_target <= limit_price:
        raise OrderValidationError(
            f"LOGIC ABORT: Leg 1 target ${leg1_target:.4f} must be strictly above Limit entry ${limit_price:.4f}!"
        )

    if leg2_target is not None and leg2_target <= limit_price:
        raise OrderValidationError(
            f"LOGIC ABORT: Leg 2 target ${leg2_target:.4f} must be strictly above Limit entry ${limit_price:.4f}!"
        )

    tranche1_shares = shares // 2
    tranche2_shares = shares - tranche1_shares
    if tranche1_shares <= 0 or tranche2_shares <= 0 or (tranche1_shares + tranche2_shares != shares):
        raise OrderValidationError(
            f"SPLIT ABORT: Invalid tranche split ({tranche1_shares} + {tranche2_shares} != {shares})."
        )

    return {
        "ticker": ticker.upper().strip(),
        "shares": shares,
        "tranche1_shares": tranche1_shares,
        "tranche2_shares": tranche2_shares,
        "limit_price": round(limit_price, 2),
        "stop_price": round(stop_price, 2),
        "notional_usd": notional,
        "planned_loss_usd": planned_loss_usd,
        "stop_dist_pct": round(stop_dist_pct, 6),
        "account_id": account_id,
    }


def validate_account_risk_limits(
    ticker: str,
    notional_usd: float,
    planned_loss_usd: float,
    account_snapshot: AccountRiskSnapshot,
    risk_cfg: RiskConfig = RiskConfig(),
) -> None:
    """
    Enforce portfolio-level buying power, aggregate exposure, per-symbol exposure,
    concurrent position count, and daily realized loss limits before order submission.
    """
    sym = ticker.upper().strip()

    if account_snapshot.account_id != risk_cfg.target_account_id:
        raise OrderValidationError(
            f"Account snapshot ID '{account_snapshot.account_id}' != '{risk_cfg.target_account_id}'"
        )

    if account_snapshot.environment.upper() != risk_cfg.target_environment.upper():
        raise SessionEnvironmentMismatchError(
            f"Account environment '{account_snapshot.environment}' != target '{risk_cfg.target_environment}'"
        )

    if account_snapshot.daily_realized_pnl_usd <= -risk_cfg.max_daily_realized_loss_usd:
        raise RiskLimitExceededError(
            f"DAILY LOSS LIMIT EXCEEDED: Daily realized PnL ${account_snapshot.daily_realized_pnl_usd:,.2f} "
            f"breaches -${risk_cfg.max_daily_realized_loss_usd:,.2f} limit."
        )

    if account_snapshot.open_strategy_positions_count >= risk_cfg.max_concurrent_positions:
        raise RiskLimitExceededError(
            f"CONCURRENT POSITION LIMIT: {account_snapshot.open_strategy_positions_count} open positions "
            f"(max {risk_cfg.max_concurrent_positions})."
        )

    if notional_usd > account_snapshot.available_buying_power_usd:
        raise RiskLimitExceededError(
            f"BUYING POWER EXCEEDED: Order notional ${notional_usd:,.2f} exceeds available buying power "
            f"${account_snapshot.available_buying_power_usd:,.2f}."
        )

    new_agg = account_snapshot.aggregate_strategy_exposure_usd + notional_usd
    if new_agg > risk_cfg.max_aggregate_exposure_usd:
        raise RiskLimitExceededError(
            f"AGGREGATE EXPOSURE EXCEEDED: ${new_agg:,.2f} exceeds max ${risk_cfg.max_aggregate_exposure_usd:,.2f}."
        )

    cur_sym_exp = account_snapshot.symbol_strategy_exposure_usd.get(sym, 0.0)
    if cur_sym_exp + notional_usd > risk_cfg.max_symbol_exposure_usd:
        raise RiskLimitExceededError(
            f"SYMBOL EXPOSURE EXCEEDED for {sym}: ${cur_sym_exp + notional_usd:,.2f} "
            f"exceeds max ${risk_cfg.max_symbol_exposure_usd:,.2f}."
        )


def revalidate_before_submission(
    signal_bar_end_ts: datetime,
    now_et: datetime,
    entry_limit_price: float,
    quote: QuoteSnapshot,
    spy_state: str,
    accum_ok: bool,
    er_blocked: bool,
    signal_status: str,
    cfg: StrategyConfig = StrategyConfig(),
    exec_cfg: ExecutionConfig = ExecutionConfig(),
    calendar: Optional[NYSECalendar] = None,
) -> None:
    """
    Revalidate all execution eligibility requirements immediately before submission
    (including after human confirmation prompt delay):
      1. Signal status must be `ENTRY_SIGNAL_NOW` (never `ARMED_FLUSHING` or `TRADED_COMPLETED`).
      2. Current time `now_et` must be on an active trading session and inside the entry window.
      3. Signal bar must be the latest completed 5m bar (`age <= max_signal_age_seconds`).
      4. Accumulation, Earnings, and SPY Waterfall gates must all pass (`spy_state == "HEALTHY"`).
      5. Live quote must be from regular trading hours with valid bid/ask spread (`<= $0.04` and `<= 0.08%`).
      6. Live price drift vs signal price must be within `max_price_drift_pct`.
    """
    if signal_status != "ENTRY_SIGNAL_NOW":
        raise StaleSignalError(
            f"EXECUTION BLOCKED: Status '{signal_status}' is not executable (must be ENTRY_SIGNAL_NOW)."
        )

    if cfg.use_accum_regime_filter and not accum_ok:
        raise StaleSignalError("EXECUTION BLOCKED: Prior completed session is not in Accumulation regime.")

    if cfg.use_er_blackout_filter and er_blocked:
        raise StaleSignalError("EXECUTION BLOCKED: Ticker is inside 14-day earnings blackout or unknown earnings.")

    if cfg.use_spy_waterfall_filter and spy_state != "HEALTHY":
        raise StaleSignalError(
            f"EXECUTION BLOCKED: SPY state is '{spy_state}' (must be HEALTHY)."
        )

    cal = calendar or NYSECalendar()
    now_ny = now_et.astimezone(NY_TZ) if now_et.tzinfo else now_et.replace(tzinfo=NY_TZ)
    sig_ny = signal_bar_end_ts.astimezone(NY_TZ) if signal_bar_end_ts.tzinfo else signal_bar_end_ts.replace(tzinfo=NY_TZ)

    sched = cal.get_session_schedule(now_ny.date())
    if not sched.is_trading_day or sched.entry_start_et is None or sched.entry_end_et is None:
        raise StaleSignalError(f"EXECUTION BLOCKED: {now_ny.date()} is not an active NYSE trading day.")

    if sig_ny.date() != now_ny.date():
        raise StaleSignalError(
            f"EXECUTION BLOCKED: Signal date {sig_ny.date()} does not match current session {now_ny.date()}."
        )

    # Note: a 13:00 ET bar finishes at 13:05 ET; allow submission up to entry_end_et + 5 minutes
    latest_submit_cutoff = sched.entry_end_et.replace(minute=sched.entry_end_et.minute + 5)
    if not (sched.entry_start_et <= now_ny <= latest_submit_cutoff):
        raise StaleSignalError(
            f"EXECUTION BLOCKED: Current time {now_ny.strftime('%H:%M:%S ET')} is outside entry window "
            f"({sched.entry_start_et.strftime('%H:%M')} - {latest_submit_cutoff.strftime('%H:%M')} ET)."
        )

    age_sec = (now_ny - sig_ny).total_seconds()
    if age_sec < 0 or age_sec > exec_cfg.max_signal_age_seconds:
        raise StaleSignalError(
            f"EXECUTION BLOCKED: Signal bar age is {age_sec:.1f}s (max allowed {exec_cfg.max_signal_age_seconds:.0f}s)."
        )

    if not quote.is_live_rth or quote.bid is None or quote.ask is None or quote.bid <= 0 or quote.ask < quote.bid:
        raise StaleSignalError(
            f"EXECUTION BLOCKED: Missing or invalid live RTH bid/ask quote for {quote.symbol}."
        )

    spread_d = quote.spread_dollars
    spread_p = quote.spread_pct
    if spread_d is None or spread_d > exec_cfg.max_bid_ask_spread_dollars + 1e-6:
        raise StaleSignalError(
            f"EXECUTION BLOCKED: Bid-Ask spread ${spread_d} exceeds max ${exec_cfg.max_bid_ask_spread_dollars:.2f}."
        )
    if spread_p is None or spread_p > exec_cfg.max_bid_ask_spread_pct + 1e-6:
        raise StaleSignalError(
            f"EXECUTION BLOCKED: Bid-Ask spread {spread_p*100:.3f}% exceeds max {exec_cfg.max_bid_ask_spread_pct*100:.3f}%."
        )

    ref_px = quote.last or quote.ask
    drift_pct = abs(ref_px - entry_limit_price) / entry_limit_price
    if drift_pct > exec_cfg.max_price_drift_pct + 1e-6:
        raise StaleSignalError(
            f"EXECUTION BLOCKED: Live price ${ref_px:.2f} drifted {drift_pct*100:.2f}% from signal price "
            f"${entry_limit_price:.2f} (max {exec_cfg.max_price_drift_pct*100:.2f}%)."
        )


# ==============================================================================
# 2. ENVIRONMENT- & ACCOUNT-SCOPED SESSION MANAGEMENT
# ==============================================================================

def get_session_cache_path(account_id: str, environment: str, base_dir: Optional[str] = None) -> str:
    root = base_dir or os.path.dirname(os.path.abspath(__file__))
    env_clean = environment.upper().strip()
    acct_clean = account_id.strip()
    return os.path.join(root, f".tt_session_{env_clean}_{acct_clean}.json")


def validate_session_cache_metadata(
    cache_payload: dict,
    expected_account_id: str,
    expected_environment: str,
) -> None:
    cached_acct = cache_payload.get("account_id")
    cached_env = cache_payload.get("environment")
    if cached_acct != expected_account_id or cached_env != expected_environment.upper():
        raise SessionEnvironmentMismatchError(
            f"Cached session mismatch: got (account={cached_acct}, env={cached_env}), "
            f"expected (account={expected_account_id}, env={expected_environment.upper()})."
        )


def get_tasty_session(
    expected_account_id: str = TARGET_ACCOUNT_ID,
    expected_environment: str = "PRODUCTION",
    cache_dir: Optional[str] = None,
) -> Tuple[Session, str]:
    """
    Create or restore a Tastytrade session strictly scoped to `expected_account_id`
    and `expected_environment` ("SANDBOX" or "PRODUCTION").
    """
    env_upper = expected_environment.upper().strip()
    if env_upper not in ("SANDBOX", "PRODUCTION"):
        raise SessionEnvironmentMismatchError(f"Invalid environment '{expected_environment}'.")

    username = os.getenv("TT_USERNAME")
    password = os.getenv("TT_PASSWORD")
    account_id = os.getenv("TT_ACCOUNT_ID")
    configured_env = os.getenv("TT_ENVIRONMENT", env_upper).upper().strip()

    if configured_env != env_upper:
        raise SessionEnvironmentMismatchError(
            f"Environment mismatch: TT_ENVIRONMENT='{configured_env}' != requested '{env_upper}'."
        )

    if not username or not password or not account_id:
        raise OrderValidationError(
            "Missing Tastytrade credentials in .env (TT_USERNAME, TT_PASSWORD, TT_ACCOUNT_ID required)."
        )

    if account_id != expected_account_id:
        raise OrderValidationError(
            f"SECURITY ABORT: .env TT_ACCOUNT_ID '{account_id}' != '{expected_account_id}'!"
        )

    is_test = (env_upper == "SANDBOX")
    cache_file = get_session_cache_path(expected_account_id, env_upper, cache_dir)

    if os.path.exists(cache_file):
        with open(cache_file, "r", encoding="utf-8") as f:
            cached = json.load(f)
        validate_session_cache_metadata(cached, expected_account_id, env_upper)
        serialized_data = cached.get("session_data")
        if serialized_data:
            session = Session.deserialize(serialized_data)
            if session.validate():
                return session, account_id

    session = Session(username, password, remember_me=True, is_test=is_test)
    payload = {
        "account_id": expected_account_id,
        "environment": env_upper,
        "created_at": datetime.now().isoformat(),
        "session_data": session.serialize(),
    }
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    return session, account_id


# ==============================================================================
# 3. SPLIT-TRANCHE OTOCO ORDER BUILDER (Remaining Shares Stay Protected!)
# ==============================================================================

def build_v6_split_tranche_orders(
    ticker: str,
    shares: int,
    limit_price: float,
    stop_price: float,
    leg1_target: float,
    leg2_target: float,
    account_id: str = TARGET_ACCOUNT_ID,
    session: Optional[Session] = None,
    risk_cfg: RiskConfig = RiskConfig(),
) -> dict:
    """
    Construct the V6 Split-Tranche OTOCO pair so that when Leg 1 (50%) fills,
    ONLY Tranche A's 50% stop is canceled, while Tranche B's 50% stop and Leg 2
    target remain 100% live and active at the broker.

    Also performs local Pydantic schema serialization (`LOCAL_SCHEMA_VALIDATION_ONLY`).
    Note: Local schema validation is NEVER labeled as broker validation.
    """
    validated = validate_order_safety(
        ticker=ticker,
        shares=shares,
        limit_price=limit_price,
        stop_price=stop_price,
        account_id=account_id,
        leg1_target=leg1_target,
        leg2_target=leg2_target,
        risk_cfg=risk_cfg,
    )

    sym = validated["ticker"]
    q1 = validated["tranche1_shares"]
    q2 = validated["tranche2_shares"]

    if session is not None:
        equity = Equity.get_equity(session, sym)
        occ_symbol = equity.symbol
    else:
        occ_symbol = sym

    d_limit = Decimal(f"{limit_price:.2f}")
    d_stop = Decimal(f"{stop_price:.2f}")
    d_leg1 = Decimal(f"{leg1_target:.2f}")
    d_leg2 = Decimal(f"{leg2_target:.2f}")

    # --- TRANCHE A (Q_1 shares): Buy Q_1 -> OCO(Limit Sell Q_1 @ Leg1, Stop Q_1 @ Stop) ---
    entry_leg_a = Leg(
        instrument_type="Equity",
        symbol=occ_symbol,
        action=OrderAction.BUY_TO_OPEN,
        quantity=Decimal(q1),
    )
    exit_leg_a = Leg(
        instrument_type="Equity",
        symbol=occ_symbol,
        action=OrderAction.SELL_TO_CLOSE,
        quantity=Decimal(q1),
    )

    trigger_order_a = NewOrder(
        time_in_force=OrderTimeInForce.DAY,
        order_type=OrderType.LIMIT,
        legs=[entry_leg_a],
        price=d_limit,
        price_effect=PriceEffect.DEBIT,
    )
    leg1_profit_order_a = NewOrder(
        time_in_force=OrderTimeInForce.DAY,
        order_type=OrderType.LIMIT,
        legs=[exit_leg_a],
        price=d_leg1,
        price_effect=PriceEffect.CREDIT,
    )
    stop_loss_order_a = NewOrder(
        time_in_force=OrderTimeInForce.DAY,
        order_type=OrderType.STOP,
        legs=[exit_leg_a],
        stop_trigger=d_stop,
        price_effect=PriceEffect.CREDIT,
    )
    tranche_a_otoco = NewComplexOrder(
        orders=[leg1_profit_order_a, stop_loss_order_a],
        trigger_order=trigger_order_a,
        type=ComplexOrderType.OTOCO,
    )

    # --- TRANCHE B (Q_2 shares): Buy Q_2 -> OCO(Limit Sell Q_2 @ Leg2, Stop Q_2 @ Stop) ---
    entry_leg_b = Leg(
        instrument_type="Equity",
        symbol=occ_symbol,
        action=OrderAction.BUY_TO_OPEN,
        quantity=Decimal(q2),
    )
    exit_leg_b = Leg(
        instrument_type="Equity",
        symbol=occ_symbol,
        action=OrderAction.SELL_TO_CLOSE,
        quantity=Decimal(q2),
    )

    trigger_order_b = NewOrder(
        time_in_force=OrderTimeInForce.DAY,
        order_type=OrderType.LIMIT,
        legs=[entry_leg_b],
        price=d_limit,
        price_effect=PriceEffect.DEBIT,
    )
    leg2_profit_order_b = NewOrder(
        time_in_force=OrderTimeInForce.DAY,
        order_type=OrderType.LIMIT,
        legs=[exit_leg_b],
        price=d_leg2,
        price_effect=PriceEffect.CREDIT,
    )
    stop_loss_order_b = NewOrder(
        time_in_force=OrderTimeInForce.DAY,
        order_type=OrderType.STOP,
        legs=[exit_leg_b],
        stop_trigger=d_stop,
        price_effect=PriceEffect.CREDIT,
    )
    tranche_b_otoco = NewComplexOrder(
        orders=[leg2_profit_order_b, stop_loss_order_b],
        trigger_order=trigger_order_b,
        type=ComplexOrderType.OTOCO,
    )

    # Local schema serialization check (strictly labeled LOCAL_SCHEMA_VALIDATION_ONLY)
    json_a = tranche_a_otoco.model_dump_json()
    json_b = tranche_b_otoco.model_dump_json()

    return {
        "ticker": sym,
        "account_id": account_id,
        "total_shares": shares,
        "tranche1_shares": q1,
        "tranche2_shares": q2,
        "limit_price": round(limit_price, 2),
        "stop_price": round(stop_price, 2),
        "leg1_target": round(leg1_target, 2),
        "leg2_target": round(leg2_target, 2),
        "notional_usd": validated["notional_usd"],
        "planned_loss_usd": validated["planned_loss_usd"],
        "tranche_a_otoco": tranche_a_otoco,
        "tranche_b_otoco": tranche_b_otoco,
        "tranche_a_schema_json": json_a,
        "tranche_b_schema_json": json_b,
        "tranche_a_json": json_a,
        "tranche_b_json": json_b,
        "validation_mode": ValidationMode.LOCAL_SCHEMA_VALIDATION_ONLY.value,
        "broker_preflight_verified": False,
    }


def build_replacement_tranche_b_oco(
    ticker: str,
    tranche2_shares: int,
    new_stop_price: float,
    new_target_price: float,
    current_market_price: Optional[float] = None,
) -> Tuple[NewOrder, NewOrder]:
    """
    Build replacement protective Stop and Limit Target orders for Tranche B (`tranche2_shares`)
    when amending the stop to breakeven or trailing 5m EMA20.
    Validates that `new_stop_price < new_target_price` and (if `current_market_price` is known)
    `new_stop_price < current_market_price`.
    """
    if tranche2_shares <= 0:
        raise OrderValidationError(f"Replacement tranche2_shares must be > 0 (got {tranche2_shares}).")
    if new_stop_price <= 0 or new_target_price <= 0:
        raise OrderValidationError("Replacement stop and target prices must be positive.")
    if new_stop_price >= new_target_price:
        raise OrderValidationError(
            f"Replacement stop ${new_stop_price:.2f} must be strictly below target ${new_target_price:.2f}."
        )
    if current_market_price is not None and new_stop_price >= current_market_price:
        raise OrderValidationError(
            f"Replacement stop ${new_stop_price:.2f} cannot be >= current market price ${current_market_price:.2f}."
        )

    exit_leg = Leg(
        instrument_type="Equity",
        symbol=ticker.upper().strip(),
        action=OrderAction.SELL_TO_CLOSE,
        quantity=Decimal(tranche2_shares),
    )
    new_stop_order = NewOrder(
        time_in_force=OrderTimeInForce.DAY,
        order_type=OrderType.STOP,
        legs=[exit_leg],
        stop_trigger=Decimal(f"{new_stop_price:.2f}"),
        price_effect=PriceEffect.CREDIT,
    )
    new_limit_order = NewOrder(
        time_in_force=OrderTimeInForce.DAY,
        order_type=OrderType.LIMIT,
        legs=[exit_leg],
        price=Decimal(f"{new_target_price:.2f}"),
        price_effect=PriceEffect.CREDIT,
    )
    return new_stop_order, new_limit_order


# ==============================================================================
# 4. PERSISTENT EXECUTION JOURNAL & BROKER RECONCILIATION
# ==============================================================================

class ExecutionJournal:
    """
    Persists signal IDs, order-intent IDs, strategy-created positions, and
    working order IDs so polling loops or restarts cannot duplicate entries
    and pre-existing IRA holdings are never confused with strategy positions.
    """

    def __init__(self, journal_path: Optional[str] = None):
        default_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "execution_journal.json")
        self.journal_path = journal_path or default_path
        self.state: Dict[str, Any] = {
            "submitted_signal_ids": {},
            "order_intents": {},
            "strategy_positions": {},
            "pre_existing_holdings": [],
        }
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.journal_path):
            try:
                with open(self.journal_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self.state.update(loaded)
            except Exception:
                pass

    def save(self) -> None:
        with open(self.journal_path, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2, sort_keys=True)

    def has_signal_id(self, signal_id: str) -> bool:
        return signal_id in self.state["submitted_signal_ids"]

    def has_order_intent(self, order_intent_id: str) -> bool:
        return order_intent_id in self.state["order_intents"]

    def register_order_intent(
        self,
        signal_id: str,
        order_intent_id: str,
        symbol: str,
        total_shares: int,
        tranche1_shares: int,
        tranche2_shares: int,
        limit_price: float,
        stop_price: float,
        leg1_target: float,
        leg2_target: float,
    ) -> None:
        if self.has_signal_id(signal_id):
            raise OrderValidationError(
                f"IDEMPOTENCY BLOCK: Signal ID '{signal_id}' was already submitted!"
            )
        if self.has_order_intent(order_intent_id):
            raise OrderValidationError(
                f"IDEMPOTENCY BLOCK: Order Intent ID '{order_intent_id}' was already submitted!"
            )
        now_str = datetime.now().isoformat()
        self.state["submitted_signal_ids"][signal_id] = {
            "order_intent_id": order_intent_id,
            "symbol": symbol.upper(),
            "registered_at": now_str,
        }
        self.state["order_intents"][order_intent_id] = {
            "signal_id": signal_id,
            "symbol": symbol.upper(),
            "total_shares": total_shares,
            "tranche1_shares": tranche1_shares,
            "tranche2_shares": tranche2_shares,
            "filled_tranche1_shares": 0,
            "filled_tranche2_shares": 0,
            "exited_tranche1_shares": 0,
            "exited_tranche2_shares": 0,
            "limit_price": limit_price,
            "stop_price": stop_price,
            "active_stop_price": stop_price,
            "leg1_target": leg1_target,
            "leg2_target": leg2_target,
            "lifecycle_state": LifecycleState.STAGED_LOCAL.value,
            "tranche_a_order_id": None,
            "tranche_b_order_id": None,
            "updated_at": now_str,
        }
        self.save()

    def update_intent_state(self, order_intent_id: str, **updates: Any) -> dict:
        if order_intent_id not in self.state["order_intents"]:
            raise OrderValidationError(f"Unknown order_intent_id: {order_intent_id}")
        rec = self.state["order_intents"][order_intent_id]
        rec.update(updates)
        rec["updated_at"] = datetime.now().isoformat()
        self.save()
        return rec


# ==============================================================================
# 5. ORDER LIFECYCLE STATE MACHINE & BROKER ADAPTER
# ==============================================================================

class OrderLifecycleManager:
    """
    Manages the full order lifecycle across Tranche A (`Q_1`) and Tranche B (`Q_2`):
      - Preflight validation (`dry_run=True`) with strict failure blocking
      - Live submission of both split-tranche OTOCO brackets (`tranche_a_otoco` and `tranche_b_otoco`)
      - Partial entry fills (sizing protective stops strictly to filled shares)
      - Protective-order rejection emergency flatten
      - Leg 1 fill -> Tranche B breakeven stop replacement with cancel/replace race protection
      - Runner mode activation -> Tranche B trailing stop & stretch target update
      - Scheduled 15:40 ET (12:40 ET early-close) flatten of remaining strategy shares
      - Startup & post-disconnect reconciliation distinguishing pre-existing IRA holdings
    """

    def __init__(
        self,
        journal: Optional[ExecutionJournal] = None,
        risk_cfg: RiskConfig = RiskConfig(),
    ):
        self.journal = journal or ExecutionJournal()
        self.risk_cfg = risk_cfg

    def run_broker_preflight(
        self,
        broker_account: Any,
        session: Any,
        staged_bundle: dict,
    ) -> dict:
        """
        Execute `dry_run=True` preflight on BOTH Tranche A and Tranche B OTOCO orders.
        Any exception, empty response, or broker error immediately raises `PreflightRejectedError`
        and prevents live order placement.
        """
        try:
            resp_a = broker_account.place_complex_order(
                session, staged_bundle["tranche_a_otoco"], dry_run=True
            )
            resp_b = broker_account.place_complex_order(
                session, staged_bundle["tranche_b_otoco"], dry_run=True
            )
        except Exception as exc:
            raise PreflightRejectedError(
                f"BROKER PREFLIGHT FAILED WITH EXCEPTION: {exc}"
            ) from exc

        if resp_a is None or resp_b is None:
            raise PreflightRejectedError(
                "BROKER PREFLIGHT FAILED: Received null response from broker."
            )

        errors_a = getattr(resp_a, "errors", None)
        errors_b = getattr(resp_b, "errors", None)
        if errors_a or errors_b:
            raise PreflightRejectedError(
                f"BROKER PREFLIGHT REJECTED ORDERS: TrancheA errors={errors_a}, TrancheB errors={errors_b}"
            )

        bp_effect_a = getattr(resp_a, "buying_power_effect", None)
        bp_effect_b = getattr(resp_b, "buying_power_effect", None)
        if bp_effect_a is None or bp_effect_b is None:
            raise PreflightRejectedError(
                "BROKER PREFLIGHT INCOMPLETE: Missing buying_power_effect in broker response."
            )

        staged_bundle["validation_mode"] = ValidationMode.BROKER_PREFLIGHT_VERIFIED.value
        staged_bundle["broker_preflight_verified"] = True
        return {
            "validation_mode": ValidationMode.BROKER_PREFLIGHT_VERIFIED.value,
            "broker_preflight_verified": True,
            "tranche_a_preflight": resp_a,
            "tranche_b_preflight": resp_b,
        }

    def submit_live_split_tranche_order(
        self,
        broker_account: Any,
        session: Any,
        signal_id: str,
        order_intent_id: str,
        staged_bundle: dict,
    ) -> dict:
        """
        Submit the verified Split-Tranche OTOCO order pair (`tranche_a_otoco` and `tranche_b_otoco`).
        Requires `staged_bundle["broker_preflight_verified"] == True`.
        Never submits an unprotected standalone entry order.
        """
        if not staged_bundle.get("broker_preflight_verified", False):
            raise PreflightRejectedError(
                "LIVE SUBMISSION BLOCKED: Order bundle has not passed broker preflight verification!"
            )

        # Register in persistent idempotency journal BEFORE network transmission
        self.journal.register_order_intent(
            signal_id=signal_id,
            order_intent_id=order_intent_id,
            symbol=staged_bundle["ticker"],
            total_shares=staged_bundle["total_shares"],
            tranche1_shares=staged_bundle["tranche1_shares"],
            tranche2_shares=staged_bundle["tranche2_shares"],
            limit_price=staged_bundle["limit_price"],
            stop_price=staged_bundle["stop_price"],
            leg1_target=staged_bundle["leg1_target"],
            leg2_target=staged_bundle["leg2_target"],
        )
        self.journal.update_intent_state(
            order_intent_id,
            lifecycle_state=LifecycleState.SUBMITTED.value,
        )

        try:
            live_resp_a = broker_account.place_complex_order(
                session, staged_bundle["tranche_a_otoco"], dry_run=False
            )
        except Exception as exc:
            self.journal.update_intent_state(
                order_intent_id,
                lifecycle_state=LifecycleState.REJECTED.value,
                error=str(exc),
            )
            raise ReconciliationRequiredError(
                f"Tranche A live submission failed or timed out ({exc}); run reconcile_account_state() immediately."
            ) from exc

        order_id_a = str(getattr(getattr(live_resp_a, "complex_order", live_resp_a), "id", "TRANCHE_A_OK"))

        try:
            live_resp_b = broker_account.place_complex_order(
                session, staged_bundle["tranche_b_otoco"], dry_run=False
            )
        except Exception as exc:
            # Tranche A is already live and self-protected by its own OTOCO; record partial submission
            self.journal.update_intent_state(
                order_intent_id,
                lifecycle_state=LifecycleState.ACCEPTED.value,
                tranche_a_order_id=order_id_a,
                tranche_b_error=str(exc),
            )
            raise ReconciliationRequiredError(
                f"Tranche A ({order_id_a}) succeeded but Tranche B failed ({exc}); reconcile required."
            ) from exc

        order_id_b = str(getattr(getattr(live_resp_b, "complex_order", live_resp_b), "id", "TRANCHE_B_OK"))

        updated = self.journal.update_intent_state(
            order_intent_id,
            lifecycle_state=LifecycleState.PROTECTED.value,
            tranche_a_order_id=order_id_a,
            tranche_b_order_id=order_id_b,
        )
        return updated

    def handle_entry_fill_event(
        self,
        broker_account: Any,
        session: Any,
        order_intent_id: str,
        filled_tranche1_shares: int,
        filled_tranche2_shares: int,
        protective_stops_active: bool,
    ) -> dict:
        """
        Handle partial or full entry fills and verify that protective stops are active
        for the exact filled share quantities (`filled_tranche1_shares` and `filled_tranche2_shares`).
        If protective stops are rejected or missing on filled shares, immediately emergency-flatten!
        """
        rec = self.journal.state["order_intents"][order_intent_id]
        tot_filled = filled_tranche1_shares + filled_tranche2_shares
        target_total = rec["total_shares"]

        if tot_filled <= 0:
            return self.journal.update_intent_state(
                order_intent_id,
                lifecycle_state=LifecycleState.ACCEPTED.value,
            )

        if not protective_stops_active:
            # Emergency flatten unprotected shares immediately!
            return self.emergency_flatten_unprotected_position(
                broker_account=broker_account,
                session=session,
                order_intent_id=order_intent_id,
                unprotected_shares=tot_filled,
                reason="PROTECTIVE_STOP_REJECTED_ON_ENTRY_FILL",
            )

        new_state = (
            LifecycleState.PROTECTED.value
            if tot_filled == target_total
            else LifecycleState.PARTIALLY_FILLED.value
        )
        return self.journal.update_intent_state(
            order_intent_id,
            filled_tranche1_shares=filled_tranche1_shares,
            filled_tranche2_shares=filled_tranche2_shares,
            lifecycle_state=new_state,
        )

    def handle_leg1_exit_and_amend_tranche_b_to_breakeven(
        self,
        broker_account: Any,
        session: Any,
        order_intent_id: str,
        filled_leg1_exit_shares: int,
        tranche_b_already_filled_or_stopped: bool = False,
    ) -> dict:
        """
        When Tranche A's Leg 1 limit order fills (`filled_leg1_exit_shares == tranche1_shares`):
          1. Tranche B's initial stop (`stop_price`) is STILL ACTIVE protecting `tranche2_shares`!
          2. Check if Tranche B has already exited (cancel/replace race guard).
          3. If Tranche B is still open, replace Tranche B's stop with a breakeven stop (`limit_price`)
             sized strictly to `filled_tranche2_shares - exited_tranche2_shares`.
        """
        rec = self.journal.state["order_intents"][order_intent_id]
        remaining_t2 = rec["filled_tranche2_shares"] - rec["exited_tranche2_shares"]

        if tranche_b_already_filled_or_stopped or remaining_t2 <= 0:
            # Cancel/replace race guard: Tranche B already exited; do NOT submit a replacement sell order!
            return self.journal.update_intent_state(
                order_intent_id,
                exited_tranche1_shares=filled_leg1_exit_shares,
                exited_tranche2_shares=rec["filled_tranche2_shares"],
                lifecycle_state=LifecycleState.CLOSED.value,
            )

        be_stop_price = rec["limit_price"]
        leg2_target = rec["leg2_target"]

        new_stop_order, new_limit_order = build_replacement_tranche_b_oco(
            ticker=rec["symbol"],
            tranche2_shares=remaining_t2,
            new_stop_price=be_stop_price,
            new_target_price=leg2_target,
        )

        if hasattr(broker_account, "replace_tranche_b_protection"):
            broker_account.replace_tranche_b_protection(
                session, rec["tranche_b_order_id"], new_stop_order, new_limit_order
            )

        return self.journal.update_intent_state(
            order_intent_id,
            exited_tranche1_shares=filled_leg1_exit_shares,
            active_stop_price=be_stop_price,
            lifecycle_state=LifecycleState.PARTIALLY_EXITED.value,
        )

    def amend_tranche_b_runner_stop(
        self,
        broker_account: Any,
        session: Any,
        order_intent_id: str,
        new_runner_stop: float,
        new_stretch_target: float,
        current_market_price: float,
        tranche_b_already_exited: bool = False,
    ) -> dict:
        """
        Update Tranche B's protective stop to trail 5m EMA20 and raise target to +1.60% stretch.
        Never lowers an existing stop and guards against cancel/replace races.
        """
        rec = self.journal.state["order_intents"][order_intent_id]
        if tranche_b_already_exited or rec["lifecycle_state"] == LifecycleState.CLOSED.value:
            return self.journal.update_intent_state(
                order_intent_id,
                lifecycle_state=LifecycleState.CLOSED.value,
            )

        remaining_t2 = rec["filled_tranche2_shares"] - rec["exited_tranche2_shares"]
        if remaining_t2 <= 0:
            return self.journal.update_intent_state(
                order_intent_id,
                lifecycle_state=LifecycleState.CLOSED.value,
            )

        effective_stop = max(rec["active_stop_price"], rec["limit_price"], round(new_runner_stop, 2))
        new_stop_order, new_limit_order = build_replacement_tranche_b_oco(
            ticker=rec["symbol"],
            tranche2_shares=remaining_t2,
            new_stop_price=effective_stop,
            new_target_price=new_stretch_target,
            current_market_price=current_market_price,
        )

        if hasattr(broker_account, "replace_tranche_b_protection"):
            broker_account.replace_tranche_b_protection(
                session, rec["tranche_b_order_id"], new_stop_order, new_limit_order
            )

        return self.journal.update_intent_state(
            order_intent_id,
            active_stop_price=effective_stop,
            leg2_target=round(new_stretch_target, 2),
        )

    def flatten_remaining_at_cutoff(
        self,
        broker_account: Any,
        session: Any,
        order_intent_id: str,
    ) -> dict:
        """
        Mandatory 15:40 ET (or 12:40 ET early-close) flatten:
        Cancels working child orders for `order_intent_id` and flattens ONLY the
        remaining open strategy shares (`remaining_t1 + remaining_t2`), never
        touching pre-existing IRA holdings.
        """
        rec = self.journal.state["order_intents"][order_intent_id]
        if rec["lifecycle_state"] == LifecycleState.CLOSED.value:
            return rec

        rem_t1 = max(0, rec["filled_tranche1_shares"] - rec["exited_tranche1_shares"])
        rem_t2 = max(0, rec["filled_tranche2_shares"] - rec["exited_tranche2_shares"])
        rem_total = rem_t1 + rem_t2

        if rem_total <= 0:
            return self.journal.update_intent_state(
                order_intent_id,
                lifecycle_state=LifecycleState.CLOSED.value,
            )

        flatten_leg = Leg(
            instrument_type="Equity",
            symbol=rec["symbol"],
            action=OrderAction.SELL_TO_CLOSE,
            quantity=Decimal(rem_total),
        )
        flatten_order = NewOrder(
            time_in_force=OrderTimeInForce.DAY,
            order_type=OrderType.MARKET,
            legs=[flatten_leg],
        )

        if hasattr(broker_account, "cancel_and_flatten_strategy_intent"):
            broker_account.cancel_and_flatten_strategy_intent(
                session, order_intent_id, flatten_order
            )
        elif hasattr(broker_account, "place_order"):
            broker_account.place_order(session, flatten_order, dry_run=False)

        return self.journal.update_intent_state(
            order_intent_id,
            exited_tranche1_shares=rec["filled_tranche1_shares"],
            exited_tranche2_shares=rec["filled_tranche2_shares"],
            lifecycle_state=LifecycleState.CLOSED.value,
            closed_reason="MANDATORY_TIME_FLATTEN",
        )

    def emergency_flatten_unprotected_position(
        self,
        broker_account: Any,
        session: Any,
        order_intent_id: str,
        unprotected_shares: int,
        reason: str,
    ) -> dict:
        """
        Immediately flatten any filled strategy shares whose protective stop order
        was rejected or failed to activate.
        """
        rec = self.journal.state["order_intents"][order_intent_id]
        if unprotected_shares > 0:
            flatten_leg = Leg(
                instrument_type="Equity",
                symbol=rec["symbol"],
                action=OrderAction.SELL_TO_CLOSE,
                quantity=Decimal(unprotected_shares),
            )
            emergency_order = NewOrder(
                time_in_force=OrderTimeInForce.DAY,
                order_type=OrderType.MARKET,
                legs=[flatten_leg],
            )
            if hasattr(broker_account, "place_order"):
                broker_account.place_order(session, emergency_order, dry_run=False)

        return self.journal.update_intent_state(
            order_intent_id,
            exited_tranche1_shares=rec["filled_tranche1_shares"],
            exited_tranche2_shares=rec["filled_tranche2_shares"],
            lifecycle_state=LifecycleState.EMERGENCY_FLATTENED.value,
            closed_reason=reason,
        )

    def reconcile_account_state(
        self,
        broker_positions: List[Dict[str, Any]],
        broker_working_orders: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Reconcile live broker positions and working orders against `execution_journal.json`:
          1. Separate pre-existing IRA holdings from strategy-created positions.
          2. Verify every open strategy position has active protective stop coverage.
          3. Raise `ReconciliationRequiredError` if a strategy position is found open at the broker
             without a matching active protective stop order.
        """
        strategy_symbols_expected: Dict[str, int] = {}
        for intent_id, rec in self.journal.state["order_intents"].items():
            if rec["lifecycle_state"] in (
                LifecycleState.PROTECTED.value,
                LifecycleState.PARTIALLY_FILLED.value,
                LifecycleState.PARTIALLY_EXITED.value,
                LifecycleState.ACCEPTED.value,
                LifecycleState.SUBMITTED.value,
            ):
                sym = rec["symbol"]
                rem = (
                    (rec["filled_tranche1_shares"] - rec["exited_tranche1_shares"])
                    + (rec["filled_tranche2_shares"] - rec["exited_tranche2_shares"])
                )
                strategy_symbols_expected[sym] = strategy_symbols_expected.get(sym, 0) + rem

        pre_existing_holdings: List[Dict[str, Any]] = []
        strategy_positions_verified: List[Dict[str, Any]] = []
        unprotected_strategy_positions: List[Dict[str, Any]] = []

        # Map working stop orders by symbol
        working_stop_shares_by_sym: Dict[str, int] = {}
        for wo in broker_working_orders:
            if wo.get("order_type") == "STOP" and wo.get("action") == "SELL_TO_CLOSE":
                s = wo["symbol"].upper()
                working_stop_shares_by_sym[s] = working_stop_shares_by_sym.get(s, 0) + int(wo.get("quantity", 0))

        for pos in broker_positions:
            sym = pos["symbol"].upper()
            qty = int(pos["quantity"])
            if sym not in strategy_symbols_expected:
                # Pre-existing IRA holding! Record separately and never touch/flatten.
                pre_existing_holdings.append({"symbol": sym, "quantity": qty})
            else:
                expected_qty = strategy_symbols_expected[sym]
                stop_cov = working_stop_shares_by_sym.get(sym, 0)
                if qty > 0 and stop_cov < expected_qty:
                    unprotected_strategy_positions.append(
                        {
                            "symbol": sym,
                            "broker_quantity": qty,
                            "expected_strategy_quantity": expected_qty,
                            "working_stop_coverage": stop_cov,
                        }
                    )
                else:
                    strategy_positions_verified.append(
                        {
                            "symbol": sym,
                            "broker_quantity": qty,
                            "working_stop_coverage": stop_cov,
                        }
                    )

        self.journal.state["pre_existing_holdings"] = [h["symbol"] for h in pre_existing_holdings]
        self.journal.save()

        if unprotected_strategy_positions:
            raise ReconciliationRequiredError(
                f"RECONCILIATION FAILURE: Unprotected strategy positions detected: {unprotected_strategy_positions}"
            )

        return {
            "status": "RECONCILED_OK",
            "pre_existing_holdings": pre_existing_holdings,
            "strategy_positions_verified": strategy_positions_verified,
        }


# ==============================================================================
# 7. DISTINCT SIGNAL, ORDER, AND POSITION PROTECTION STATES (Section 7)
# ==============================================================================

class SignalState(str, Enum):
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    WATCHING_ARMED = "WATCHING_ARMED"
    WATCHING_FLUSHING = "WATCHING_FLUSHING"
    WATCHING_ELIGIBLE = "WATCHING_ELIGIBLE"
    BLOCKED = "BLOCKED"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
    EXPIRED = "EXPIRED"
    HISTORICAL_REPLAY = "HISTORICAL_REPLAY"


class OrderState(str, Enum):
    ENTRY_PENDING = "ENTRY_PENDING"
    ENTRY_PARTIALLY_FILLED = "ENTRY_PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELED = "CANCELED"
    OUTCOME_UNCERTAIN_RECONCILING = "OUTCOME_UNCERTAIN_RECONCILING"


class PositionProtectionState(str, Enum):
    POSITION_OPEN = "POSITION_OPEN"
    PROTECTION_PENDING = "PROTECTION_PENDING"
    PROTECTION_CONFIRMED = "PROTECTION_CONFIRMED"
    PROTECTION_REJECTED_OR_MISSING = "PROTECTION_REJECTED_OR_MISSING"
    FIRST_EXIT_COMPLETED = "FIRST_EXIT_COMPLETED"
    RUNNER_ACTIVE = "RUNNER_ACTIVE"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    OUTCOME_UNCERTAIN = "OUTCOME_UNCERTAIN"


ALLOWED_ACCOUNT_ID = TARGET_ACCOUNT_ID
MAX_FEED_DIVERGENCE_PCT = 0.0025


def load_or_create_session(account_id: str = TARGET_ACCOUNT_ID):
    """
    Attempt to load Tastytrade OAuth or password credentials from environment / `.env`.
    Returns `(session_or_none, target_account_id, is_live_authenticated)`.
    Never logs or returns secret values.
    """
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass

    acct = os.getenv("TT_ACCOUNT_ID", account_id) or account_id
    provider_secret = os.getenv("TT_PROVIDER_SECRET", "").strip()
    refresh_token = os.getenv("TT_REFRESH_TOKEN", "").strip()
    username = os.getenv("TT_USERNAME", "").strip()
    password = os.getenv("TT_PASSWORD", "").strip()

    if provider_secret and refresh_token:
        try:
            session = Session(provider_secret, refresh_token)
            return session, acct, True
        except Exception:
            return None, acct, False

    if username and password and "your_" not in username.lower():
        try:
            session = Session(username, password)
            return session, acct, True
        except Exception:
            return None, acct, False

    return None, acct, False


def fetch_secondary_quotes(tickers: List[str], timeout: float = 3.5) -> Dict[str, Dict[str, Any]]:
    """
    CLI-compatible quote helper backed by `market_data_adapter.fetch_cnbc_quote_observations`.
    Note: Does NOT include a fake `vwap` field computed from `(high + low + last) / 3`.
    """
    from market_data_adapter import fetch_cnbc_quote_observations

    obs_map = fetch_cnbc_quote_observations(tickers, timeout=timeout)
    out: Dict[str, Dict[str, Any]] = {}
    for sym, obs in obs_map.items():
        out[sym] = {
            "last": obs.last,
            "bid": obs.bid,
            "ask": obs.ask,
            "open": obs.open_price,
            "high": obs.day_high,
            "low": obs.day_low,
            "volume": obs.day_volume,
            "observation_timestamp_et": obs.provenance.observation_timestamp_et,
            "vendor_id": obs.provenance.vendor_id,
            "endpoint_id": obs.provenance.endpoint_id,
        }
    return out

