"""
strategy_engine.py
==================
Single shared causal strategy engine consumed by:
  1. Historical replay / portfolio backtest
  2. Paper trading mode
  3. Live HUD / broker execution staging

Preserves the exact V5 economic hypothesis and thresholds (`get_accum_map`,
`build_spy_intraday_map`, `VolMA10_ExOpen`, `is_hl_reclaim`, `is_hammer_reclaim`)
while enforcing all audited V6 correctness fixes:
  - Strictly causal prior-session daily accumulation lookup (1-day lag `< session_date`)
  - Separate display-only diagnostic for developing next-session accumulation (`<= session_date`)
  - Explicitly named daily regime policies: `core_v5_dislocated_sma5` vs `scanner_hl10_atr_compression`
  - Causal ToD_MedVol & Midday_MedVol with ZERO `.bfill()` and ZERO same-day median fallbacks
  - Explicit SPY state: `HEALTHY`, `WATERFALL_BLOCKED`, or `UNKNOWN_BLOCKED` (missing/stale SPY blocks entry)
  - Identical Higher-Low and Hammer reclaim logic across replay, paper, and HUD
  - Causal intrabar order sequencing:
      1) Evaluate previously active resting orders first (gap-aware stops, prior-bar VWAP Leg 1,
         prior-bar-activated runner target, conservative stop-first ambiguous bar policy)
      2) Check scheduled session flatten (15:40 ET regular / 12:40 ET early close; never flatten on prefix end)
      3) Update completed-bar features, runner activation, and validated trailing stops for subsequent bars
      4) Evaluate new entry signals on completed bars inside the entry window (without requiring >=30 future bars)
"""

from __future__ import annotations
from dataclasses import dataclass, asdict
from datetime import date, datetime, time, timedelta
import hashlib
import math
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd

from config_schema import (
    StrategyConfig,
    ExecutionConfig,
    RiskConfig,
)
from market_calendar import NYSECalendar, NY_TZ, normalize_to_ny_tz
from earnings_oracle import EarningsOracle, EarningsCheckResult


# ==============================================================================
# DATA STRUCTURES FOR SIGNALS, ORDERS, POSITIONS & TRADES
# ==============================================================================

@dataclass
class OrderPlan:
    symbol: str
    signal_id: str
    session_date: str
    bar_start_ts: str
    bar_end_ts: str
    feature_available_ts: str
    decision_ts: str
    entry_price: float
    flush_low: float
    stop_price: float
    stop_distance_pct: float
    stop_policy_applied: str
    total_shares: int
    tranche1_shares: int
    tranche2_shares: int
    notional_usd: float
    planned_risk_usd: float
    leg1_initial_target: float
    leg2_base_target: float
    leg2_stretch_target: float
    climax_vol_ratio: float
    reclaim_type: str  # "Higher-Low Reclaim" or "Hammer Reclaim"
    why_diagnosis: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OpenPositionState:
    symbol: str
    signal_id: str
    session_date: str
    entry_bar_start_ts: str
    entry_bar_end_ts: str
    order_submit_ts: str
    entry_fill_ts: str
    entry_signal_price: float
    entry_fill_price: float
    flush_low: float
    initial_stop_price: float
    active_stop_price: float
    total_shares: int
    tranche1_shares: int
    tranche2_shares: int
    remaining_shares: int
    leg1_filled: bool
    leg1_fill_ts: Optional[str]
    leg1_fill_price: Optional[float]
    runner_mode_active: bool
    runner_activated_ts: Optional[str]
    prev_bar_vwap: float
    active_leg1_target: float
    active_leg2_target: float
    last_mark_price: float
    last_mark_ts: str
    unrealized_pnl_usd: float
    realized_leg1_pnl_usd: float
    ambiguous_bar_count: int
    climax_vol_ratio: float
    reclaim_type: str
    order_plan: OrderPlan

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CompletedTradeRecord:
    symbol: str
    signal_id: str
    session_date: str
    entry_bar_start_ts: str
    entry_bar_end_ts: str
    feature_available_ts: str
    decision_ts: str
    order_submit_ts: str
    entry_fill_ts: str
    exit_bar_start_ts: str
    exit_fill_ts: str
    reclaim_type: str
    entry_signal_price: float
    entry_fill_price: float
    flush_low: float
    initial_stop_price: float
    stop_distance_pct: float
    total_shares: int
    tranche1_shares: int
    tranche2_shares: int
    notional_usd: float
    planned_risk_usd: float
    leg1_filled: bool
    leg1_fill_ts: Optional[str]
    leg1_fill_price: Optional[float]
    leg2_fill_ts: str
    leg2_fill_price: float
    exit_reason: str
    runner_mode_used: bool
    ambiguous_bar_count: int
    gross_return_pct: float
    net_return_pct: float
    net_pnl_usd: float
    climax_vol_ratio: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SessionEvaluationResult:
    symbol: str
    session_date: str
    as_of_ts: Optional[str]
    status: str
    is_executable_now: bool
    prior_completed_session_date: Optional[str]
    prior_session_accum_ok: bool
    developing_next_session_accum: bool
    tod_history_sufficient: bool
    earnings_check: EarningsCheckResult
    spy_state: str  # "HEALTHY", "WATERFALL_BLOCKED", "UNKNOWN_BLOCKED", "FILTER_DISABLED"
    spy_ok: bool
    spy_3bar_ret: Optional[float]
    spy_vwap_dist: Optional[float]
    current_price: Optional[float]
    current_vwap: Optional[float]
    morning_high: Optional[float]
    flush_low: Optional[float]
    max_flush_drop_pct: Optional[float]
    max_vwap_discount_pct: Optional[float]
    climax_vol_ratio: Optional[float]
    climax_bar_ts: Optional[str]
    latest_bar_start_ts: Optional[str]
    latest_bar_end_ts: Optional[str]
    active_signal_plan: Optional[OrderPlan]
    open_position: Optional[OpenPositionState]
    completed_trades: List[CompletedTradeRecord]
    rejection_reason: Optional[str] = None


# ==============================================================================
# 1. DAILY ACCUMULATION REGIME (Strictly Lagged Prior Completed Session)
# ==============================================================================

def compute_daily_regime_table(
    df_1d: pd.DataFrame,
    cfg: StrategyConfig = StrategyConfig(),
    df_spy_1d: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """
    Compute both explicitly named daily accumulation regime proxies on raw 1d OHLCV bars:
      1. `core_v5_dislocated_sma5`: Exact V5 `get_accum_map` proxy (Dislocated >=12% off 60d high,
         Close > rising SMA5, and 5d UpVol >= 55% or AfternoonRampAccum relative to SPY).
      2. `scanner_hl10_atr_compression`: Dynamic scanner proxy (HL_10d >= 6, ATR_Pct <= 4.5%, Vol_10d/20d >= 1.0).

    Causal lookup (`lookup_causal_daily_regime`) strictly selects `Session_Date < session_date`
    for prior completed session eligibility, and `Session_Date <= session_date` only for the
    separate display-only `developing_next_session_accum` diagnostic.
    """
    if df_1d.empty:
        return pd.DataFrame()

    df = normalize_to_ny_tz(df_1d).sort_index()
    out = pd.DataFrame(index=df.index)
    out["Open"] = df["Open"].astype(float)
    out["High"] = df["High"].astype(float)
    out["Low"] = df["Low"].astype(float)
    out["Close"] = df["Close"].astype(float)
    out["Volume"] = df["Volume"].astype(float)
    out["Session_Date"] = out.index.date

    # --- Proxy A: Core V5 Dislocated + Rising SMA5 + UpVol / AfternoonRamp ---
    high60 = out["High"].rolling(60, min_periods=20).max()
    drawdown60 = (out["Low"].rolling(35, min_periods=5).min() - high60) / high60.replace(0, np.nan)
    dislocated = drawdown60 <= -0.12

    sma5 = out["Close"].rolling(5).mean()
    sma5_rising = sma5 > sma5.shift(2)
    green_day = out["Close"] > out["Open"]
    up_vol5 = (out["Volume"] * green_day).rolling(5).sum()
    tot_vol5 = out["Volume"].rolling(5).sum().replace(0, np.nan)
    vol_accum = (up_vol5 / tot_vol5) >= 0.55

    base_v5_accum = (dislocated & (out["Close"] > sma5) & sma5_rising & vol_accum).fillna(False)

    if df_spy_1d is not None and not df_spy_1d.empty:
        s = normalize_to_ny_tz(df_spy_1d).sort_index()
        s_date = s.index.date
        spy_ret1 = s["Close"].astype(float).pct_change(fill_method=None)
        spy_ret5 = s["Close"].astype(float).pct_change(5, fill_method=None)
        map_ret1 = dict(zip(s_date, spy_ret1))
        map_ret5 = dict(zip(s_date, spy_ret5))
        d_spy_ret1 = pd.Series([map_ret1.get(d, np.nan) for d in out["Session_Date"]], index=out.index)
        d_spy_ret5 = pd.Series([map_ret5.get(d, np.nan) for d in out["Session_Date"]], index=out.index)

        ret1 = out["Close"].pct_change(fill_method=None)
        ret5 = out["Close"].pct_change(5, fill_method=None)
        rng = (out["High"] - out["Low"]).replace(0, np.nan)
        clv = ((out["Close"] - out["Low"]) / rng).fillna(0.5)
        clv3 = clv.rolling(3, min_periods=2).mean()
        bloodbath_div = ((ret1 > 0) & (d_spy_ret1 <= -0.010)).rolling(5, min_periods=1).max() > 0
        afternoon_ramp = (
            dislocated
            & (out["Close"] > sma5)
            & sma5_rising
            & (ret5 > d_spy_ret5 + 0.02)
            & (clv3 >= 0.60)
            & bloodbath_div
        ).fillna(False)
        base_v5_accum = base_v5_accum | afternoon_ramp

    out["Is_Accum_Core_V5"] = base_v5_accum.astype(bool)

    # --- Proxy B: Scanner Higher-Lows + Tight ATR + Volume Ratio ---
    out["Higher_Low"] = (out["Low"] >= out["Low"].shift(1) * 0.998).astype(int)
    out["HL_Count_10d"] = out["Higher_Low"].rolling(
        cfg.accum_lookback_days, min_periods=cfg.accum_lookback_days
    ).sum()

    tr0 = (out["High"] - out["Low"]).abs()
    tr1 = (out["High"] - out["Close"].shift(1)).abs()
    tr2 = (out["Low"] - out["Close"].shift(1)).abs()
    tr = pd.concat([tr0, tr1, tr2], axis=1).max(axis=1)
    out["ATR_14d"] = tr.rolling(cfg.accum_atr_period, min_periods=cfg.accum_atr_period).mean()
    out["ATR_Pct"] = out["ATR_14d"] / out["Close"].replace(0, np.nan)

    out["Vol_Short"] = out["Volume"].rolling(
        cfg.accum_vol_short_window, min_periods=cfg.accum_vol_short_window
    ).mean()
    out["Vol_Long"] = out["Volume"].rolling(
        cfg.accum_vol_long_window, min_periods=cfg.accum_vol_long_window
    ).mean()
    out["Vol_Ratio"] = out["Vol_Short"] / out["Vol_Long"].replace(0, np.nan)

    out["Is_Accum_Scanner"] = (
        (out["HL_Count_10d"] >= cfg.accum_min_higher_lows)
        & (out["ATR_Pct"] <= cfg.accum_max_atr_pct)
        & (out["Vol_Ratio"] >= cfg.accum_min_vol_ratio)
    ).fillna(False).astype(bool)

    if cfg.daily_regime_policy == "core_v5_dislocated_sma5":
        out["Is_Accum"] = out["Is_Accum_Core_V5"]
    elif cfg.daily_regime_policy == "scanner_hl10_atr_compression":
        out["Is_Accum"] = out["Is_Accum_Scanner"]
    else:
        raise ValueError(f"Unknown daily_regime_policy: {cfg.daily_regime_policy}")

    return out


def lookup_causal_daily_regime(
    daily_table: pd.DataFrame,
    session_date: date,
) -> Tuple[Optional[date], bool, bool]:
    """
    Given a target intraday `session_date`, return:
      1. `prior_session_date`: latest daily bar date strictly `< session_date`
      2. `prior_session_accum_ok`: whether that prior completed daily bar was in Accumulation
      3. `developing_next_session_accum`: whether the latest bar `<= session_date` is developing Accumulation (display-only diagnostic)
    """
    if daily_table.empty:
        return None, False, False

    prior_rows = daily_table[daily_table["Session_Date"] < session_date]
    if prior_rows.empty:
        prior_dt = None
        prior_accum = False
    else:
        last_prior = prior_rows.iloc[-1]
        prior_dt = last_prior["Session_Date"]
        prior_accum = bool(last_prior["Is_Accum"])

    up_to_today = daily_table[daily_table["Session_Date"] <= session_date]
    dev_accum = bool(up_to_today.iloc[-1]["Is_Accum"]) if not up_to_today.empty else False

    return prior_dt, prior_accum, dev_accum


# ==============================================================================
# 2. CAUSAL 5-MINUTE FEATURE PREPARATION (NO .bfill(), NO FUTURE LEAKAGE)
# ==============================================================================

def compute_causal_5m_features(
    df_5m: pd.DataFrame,
    cfg: StrategyConfig = StrategyConfig(),
    calendar: Optional[NYSECalendar] = None,
) -> pd.DataFrame:
    """
    Compute strictly causal 5-minute intraday features from raw 5m bars.
    - Never uses `.bfill()` on `ToD_MedVol` or `Midday_MedVol`.
    - Never uses same-day `day_df['Volume'].median()` as a fallback for `Midday_MedVol`.
    - Never forward-fills missing Volume.
    - Marks `ToD_History_Sufficient` False when fewer than `cfg.min_tod_history_sessions`
      prior sessions exist for a given 5-minute slot.
    """
    if df_5m.empty:
        return pd.DataFrame()

    cal = calendar or NYSECalendar()
    df = normalize_to_ny_tz(df_5m).sort_index()
    df = df.between_time("09:30", "15:55").copy()
    if df.empty:
        return df

    df["Open"] = df["Open"].astype(float)
    df["High"] = df["High"].astype(float)
    df["Low"] = df["Low"].astype(float)
    df["Close"] = df["Close"].astype(float)
    df["Volume"] = df["Volume"].astype(float)

    df["Date"] = df.index.date
    df["Time_Str"] = df.index.strftime("%H:%M")

    # Drop any bars after an early-close session's market close
    valid_mask = []
    for ts, d in zip(df.index, df["Date"]):
        sched = cal.get_session_schedule(d)
        if not sched.is_trading_day or sched.market_close_et is None:
            valid_mask.append(False)
        else:
            valid_mask.append(ts < sched.market_close_et)
    df = df[valid_mask].copy()
    if df.empty:
        return df

    # 1. Causal Time-of-Day Median Volume: strictly shifted by 1 session, no .bfill()
    df["ToD_Prior_Count"] = df.groupby("Time_Str")["Volume"].transform(
        lambda x: x.shift(1).rolling(cfg.tod_rolling_sessions, min_periods=1).count()
    )
    df["ToD_MedVol"] = df.groupby("Time_Str")["Volume"].transform(
        lambda x: x.shift(1).rolling(
            cfg.tod_rolling_sessions,
            min_periods=cfg.min_tod_history_sessions,
        ).median()
    )

    # 2. Causal Midday Median Volume (10:30-14:30 from prior completed sessions, no .bfill(), no same-day fallback)
    all_dates = pd.Index(sorted(df["Date"].unique()))
    midday_slice = df.between_time("10:30", "14:30")
    if not midday_slice.empty:
        daily_midday_med = midday_slice.groupby("Date")["Volume"].median().reindex(all_dates)
        causal_midday_baseline = daily_midday_med.shift(1).rolling(
            cfg.tod_rolling_sessions,
            min_periods=cfg.min_tod_history_sessions,
        ).median()
        df["Midday_MedVol"] = df["Date"].map(causal_midday_baseline)
    else:
        df["Midday_MedVol"] = np.nan

    df["ToD_History_Sufficient"] = (
        (df["ToD_Prior_Count"] >= cfg.min_tod_history_sessions)
        & df["ToD_MedVol"].notna()
        & (df["ToD_MedVol"] > 0)
        & df["Midday_MedVol"].notna()
        & (df["Midday_MedVol"] > 0)
    )

    df["Vol_ToD_Ratio"] = np.where(
        df["ToD_History_Sufficient"],
        df["Volume"] / np.minimum(df["ToD_MedVol"], df["Midday_MedVol"]),
        np.nan,
    )

    # 3. Intraday VolMA10_ExOpen per session (excluding 09:30 and 09:35 opening auction bars, causal within day)
    vol_ex_open = df["Volume"].where(~df["Time_Str"].isin(["09:30", "09:35"]), np.nan)
    df["VolMA10_ExOpen"] = (
        vol_ex_open.groupby(df["Date"])
        .transform(lambda s: s.rolling(10, min_periods=1).mean())
        .fillna(df["Volume"])
    )

    # 4. Intraday cumulative VWAP, Morning_High, Open_Bar_High, and session EMAs (per session Date, matching V5)
    df["Typ_Price"] = (df["High"] + df["Low"] + df["Close"]) / 3.0
    df["TP_Vol"] = df["Typ_Price"] * df["Volume"]
    cum_tp_vol = df.groupby("Date")["TP_Vol"].cumsum()
    cum_vol = df.groupby("Date")["Volume"].cumsum()
    df["VWAP"] = np.where(cum_vol > 0, cum_tp_vol / cum_vol, df["Close"])
    df["Morning_High"] = df.groupby("Date")["High"].cummax()
    df["Open_Bar_High"] = df.groupby("Date")["High"].transform("first")

    df["EMA12"] = df.groupby("Date")["Close"].transform(lambda s: s.ewm(span=12, adjust=False).mean())
    df["EMA20"] = df.groupby("Date")["Close"].transform(lambda s: s.ewm(span=20, adjust=False).mean())

    return df


def compute_causal_spy_features(df_spy_5m: pd.DataFrame) -> pd.DataFrame:
    """
    Compute causal SPY 5-minute waterfall features matching V5 `build_spy_intraday_map`:
      - `SPY_VWAP`, `SPY_MorningHigh`, `SPY_DropFromHigh`, `SPY_VWAP_Dist`, `SPY_RedBar`, `SPY_3Bar_Ret`
    Missing observations are never filled with healthy values.
    """
    if df_spy_5m is None or df_spy_5m.empty:
        return pd.DataFrame()

    s = normalize_to_ny_tz(df_spy_5m).sort_index()
    s = s.between_time("09:30", "15:55").copy()
    if s.empty:
        return s

    s["Open"] = s["Open"].astype(float)
    s["High"] = s["High"].astype(float)
    s["Low"] = s["Low"].astype(float)
    s["Close"] = s["Close"].astype(float)
    s["Volume"] = s["Volume"].astype(float)
    s["Date"] = s.index.date

    s["Typ"] = (s["High"] + s["Low"] + s["Close"]) / 3.0
    s["TPV"] = s["Typ"] * s["Volume"]
    cum_tpv = s.groupby("Date")["TPV"].cumsum()
    cum_vol = s.groupby("Date")["Volume"].cumsum()
    s["SPY_VWAP"] = np.where(cum_vol > 0, cum_tpv / cum_vol, s["Close"])
    s["SPY_MorningHigh"] = s.groupby("Date")["High"].cummax()
    s["SPY_DropFromHigh"] = (s["Low"] - s["SPY_MorningHigh"]) / s["SPY_MorningHigh"].replace(0, np.nan)
    s["SPY_VWAP_Dist"] = (s["Close"] - s["SPY_VWAP"]) / s["SPY_VWAP"].replace(0, np.nan)
    s["SPY_RedBar"] = s["Close"] < s["Open"]
    s["SPY_3Bar_Ret"] = s.groupby("Date")["Close"].pct_change(3, fill_method=None).fillna(0.0)
    return s


def lookup_spy_state_at_bar(
    spy_df: pd.DataFrame,
    bar_start_ts: pd.Timestamp,
    cfg: StrategyConfig,
) -> Tuple[str, bool, Optional[float], Optional[float]]:
    """
    Evaluate SPY waterfall filter at `bar_start_ts` matching V5 `build_spy_intraday_map`.
    If SPY data is missing or stale (>10 minutes old or from a prior session),
    returns `("UNKNOWN_BLOCKED", False, None, None)` when filter is enabled.
    """
    if not cfg.use_spy_waterfall_filter:
        return "FILTER_DISABLED", True, None, None

    if spy_df is None or spy_df.empty:
        return "UNKNOWN_BLOCKED", False, None, None

    same_day = spy_df[(spy_df["Date"] == bar_start_ts.date()) & (spy_df.index <= bar_start_ts)]
    if same_day.empty:
        return "UNKNOWN_BLOCKED", False, None, None

    last_spy_ts = same_day.index[-1]
    staleness_sec = (bar_start_ts - last_spy_ts).total_seconds()
    if staleness_sec > 600:
        return "UNKNOWN_BLOCKED", False, None, None

    last_row = same_day.iloc[-1]
    if pd.isna(last_row["Close"]) or pd.isna(last_row["SPY_VWAP"]) or pd.isna(last_row["SPY_DropFromHigh"]):
        return "UNKNOWN_BLOCKED", False, None, None

    drop_high = float(last_row["SPY_DropFromHigh"])
    vdist = float(last_row["SPY_VWAP_Dist"])
    ret3 = float(last_row["SPY_3Bar_Ret"])
    red_bar = bool(last_row["SPY_RedBar"])

    is_waterfall = (
        drop_high <= cfg.spy_drop_from_high_limit
        and vdist <= cfg.spy_vwap_dist_limit
        and (red_bar or ret3 < 0 or drop_high <= cfg.spy_deep_crash_limit)
    )
    if is_waterfall:
        return "WATERFALL_BLOCKED", False, ret3, vdist
    return "HEALTHY", True, ret3, vdist


# ==============================================================================
# 3. STOP PRICE & POSITION SIZING WITH EXPLICIT RISK / NOTIONAL RECONCILIATION
# ==============================================================================

def compute_stop_and_shares(
    symbol: str,
    entry_price: float,
    flush_low: float,
    cfg: StrategyConfig,
    risk_cfg: RiskConfig,
    clip_override_usd: Optional[float] = None,
) -> Tuple[Optional[float], float, str, int, int, int, Optional[str]]:
    """
    Reconcile:
      - V5 structural stop formula: `min(flush_low * (1 - 0.0005), max(flush_low * (1 - 0.0035), entry_price * (1 - 0.0065)))`
      - Advertised max stop distance: `0.65%` below `entry_price`
      - Stop policy:
          * `"strict_cap_reject"`: rejects any setup where `stop_price` is > 0.65% below `entry_price`
          * `"structural_risk_sized"`: keeps `stop_price` at the structural level below `flush_low` (up to 1.25% max)
            and scales down `total_shares` so planned dollar risk `shares * (entry - stop)` never exceeds `$975` (`0.65% * $150k`) or `$1,500`
          * `"legacy_v5_capped"`: places stop at `stop_price` and sizes shares by notional/3,000-cap without rejecting > 0.65%
    """
    if entry_price <= 0 or flush_low <= 0:
        return None, 0.0, cfg.stop_policy, 0, 0, 0, "INVALID_PRICE"

    structural_stop = min(
        flush_low * (1.0 - cfg.flush_stop_buffer_pct),
        max(
            flush_low * (1.0 - cfg.min_stop_pct),
            entry_price * (1.0 - cfg.max_stop_pct),
        ),
    )
    stop_price = round(structural_stop, 4)
    stop_dist_pct = (entry_price - stop_price) / entry_price

    if cfg.stop_policy == "strict_cap_reject":
        if stop_dist_pct > cfg.max_stop_pct + 1e-6:
            return (
                stop_price,
                stop_dist_pct,
                "strict_cap_reject",
                0,
                0,
                0,
                f"REJECTED_STOP_EXCEEDS_CAP ({stop_dist_pct*100:.2f}% > {cfg.max_stop_pct*100:.2f}%)",
            )
    elif cfg.stop_policy == "structural_risk_sized":
        if stop_dist_pct > cfg.max_structural_stop_pct + 1e-6:
            return (
                stop_price,
                stop_dist_pct,
                "structural_risk_sized",
                0,
                0,
                0,
                f"REJECTED_STRUCTURAL_STOP_TOO_WIDE ({stop_dist_pct*100:.2f}% > {cfg.max_structural_stop_pct*100:.2f}%)",
            )
    elif cfg.stop_policy == "legacy_v5_capped":
        pass
    else:
        raise ValueError(f"Unknown stop_policy: {cfg.stop_policy}")

    target_clip = clip_override_usd if clip_override_usd is not None else risk_cfg.target_clip_usd
    effective_clip = min(target_clip, risk_cfg.max_notional_per_order_usd)

    shares_by_notional = int(math.floor(effective_clip / entry_price))
    shares_capped = min(shares_by_notional, risk_cfg.max_shares_per_order)

    per_share_risk = max(entry_price - stop_price, 0.01)
    max_risk_dollars = risk_cfg.max_planned_stop_loss_usd
    if cfg.stop_policy == "structural_risk_sized":
        max_risk_dollars = min(max_risk_dollars, effective_clip * cfg.max_stop_pct)

    shares_by_risk = int(math.floor(max_risk_dollars / per_share_risk))
    total_shares = min(shares_capped, shares_by_risk)

    if total_shares < 2:
        return stop_price, stop_dist_pct, cfg.stop_policy, 0, 0, 0, "REJECTED_ZERO_SHARES"

    tranche1_shares = total_shares // 2
    tranche2_shares = total_shares - tranche1_shares
    return stop_price, stop_dist_pct, cfg.stop_policy, total_shares, tranche1_shares, tranche2_shares, None


def make_signal_id(symbol: str, bar_start_ts: pd.Timestamp, entry_price: float) -> str:
    raw = f"{symbol.upper()}|{bar_start_ts.isoformat()}|{entry_price:.4f}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ==============================================================================
# 4. CORE CAUSAL SESSION STATE MACHINE
# ==============================================================================

def evaluate_session_causal(
    symbol: str,
    session_date: date,
    df_1d: pd.DataFrame,
    df_5m: pd.DataFrame,
    df_spy_5m: Optional[pd.DataFrame],
    cfg: StrategyConfig = StrategyConfig(),
    exec_cfg: ExecutionConfig = ExecutionConfig(),
    risk_cfg: RiskConfig = RiskConfig(),
    earnings_oracle: Optional[EarningsOracle] = None,
    calendar: Optional[NYSECalendar] = None,
    as_of_ts: Optional[pd.Timestamp] = None,
    clip_override_usd: Optional[float] = None,
    df_spy_1d: Optional[pd.DataFrame] = None,
    precomputed_daily_table: Optional[pd.DataFrame] = None,
    precomputed_feat_5m: Optional[pd.DataFrame] = None,
    precomputed_spy_feat: Optional[pd.DataFrame] = None,
) -> SessionEvaluationResult:
    """
    Run the single shared causal state machine for `symbol` on `session_date`
    using only data available up to `as_of_ts` (if provided).
    """
    sym = symbol.upper()
    cal = calendar or NYSECalendar()
    oracle = earnings_oracle or EarningsOracle(
        blackout_days=cfg.er_blackout_calendar_days,
        unknown_policy=cfg.er_unknown_policy,
    )

    d1_input = normalize_to_ny_tz(df_1d) if not df_1d.empty else df_1d
    d5_input = normalize_to_ny_tz(df_5m) if not df_5m.empty else df_5m
    spy_input = normalize_to_ny_tz(df_spy_5m) if (df_spy_5m is not None and not df_spy_5m.empty) else pd.DataFrame()
    spy_1d_input = normalize_to_ny_tz(df_spy_1d) if (df_spy_1d is not None and not df_spy_1d.empty) else pd.DataFrame()

    if as_of_ts is not None:
        if as_of_ts.tzinfo is None:
            as_of_ts = as_of_ts.tz_localize(NY_TZ)
        else:
            as_of_ts = as_of_ts.tz_convert(NY_TZ)
        if not d5_input.empty:
            d5_input = d5_input[d5_input.index <= as_of_ts]
        if not spy_input.empty:
            spy_input = spy_input[spy_input.index <= as_of_ts]
        if not d1_input.empty:
            d1_input = d1_input[d1_input.index.date <= as_of_ts.date()]
        if not spy_1d_input.empty:
            spy_1d_input = spy_1d_input[spy_1d_input.index.date <= as_of_ts.date()]

    # 1. Daily Accumulation Regime (strictly prior completed session < session_date)
    daily_table = (
        precomputed_daily_table[
            precomputed_daily_table["Session_Date"] <= (as_of_ts.date() if as_of_ts is not None else session_date)
        ]
        if precomputed_daily_table is not None
        else compute_daily_regime_table(d1_input, cfg, spy_1d_input)
    )
    prior_dt, prior_accum_ok, dev_next_accum = lookup_causal_daily_regime(daily_table, session_date)
    regime_gate_ok = prior_accum_ok if cfg.use_accum_regime_filter else True

    # 2. Earnings Blackout Check
    er_check = oracle.check_blackout(sym, session_date, use_filter=cfg.use_er_blackout_filter)
    er_gate_ok = not er_check.is_blocked

    # 3. Compute causal 5m features
    if precomputed_feat_5m is not None:
        feat_5m = precomputed_feat_5m[precomputed_feat_5m.index <= as_of_ts] if as_of_ts is not None else precomputed_feat_5m
    else:
        feat_5m = compute_causal_5m_features(d5_input, cfg, cal)

    if precomputed_spy_feat is not None:
        spy_feat = precomputed_spy_feat[precomputed_spy_feat.index <= as_of_ts] if as_of_ts is not None else precomputed_spy_feat
    else:
        spy_feat = compute_causal_spy_features(spy_input) if not spy_input.empty else pd.DataFrame()

    day_df = feat_5m[feat_5m["Date"] == session_date].copy() if not feat_5m.empty else pd.DataFrame()
    sched = cal.get_session_schedule(session_date)

    if day_df.empty or not sched.is_trading_day:
        return SessionEvaluationResult(
            symbol=sym,
            session_date=session_date.isoformat(),
            as_of_ts=as_of_ts.isoformat() if as_of_ts is not None else None,
            status="NO_DATA",
            is_executable_now=False,
            prior_completed_session_date=prior_dt.isoformat() if prior_dt else None,
            prior_session_accum_ok=prior_accum_ok,
            developing_next_session_accum=dev_next_accum,
            tod_history_sufficient=False,
            earnings_check=er_check,
            spy_state="UNKNOWN_BLOCKED" if cfg.use_spy_waterfall_filter else "FILTER_DISABLED",
            spy_ok=not cfg.use_spy_waterfall_filter,
            spy_3bar_ret=None,
            spy_vwap_dist=None,
            current_price=None,
            current_vwap=None,
            morning_high=None,
            flush_low=None,
            max_flush_drop_pct=None,
            max_vwap_discount_pct=None,
            climax_vol_ratio=None,
            climax_bar_ts=None,
            latest_bar_start_ts=None,
            latest_bar_end_ts=None,
            active_signal_plan=None,
            open_position=None,
            completed_trades=[],
        )

    entry_slippage = exec_cfg.entry_slippage_bps / 10000.0
    exit_slippage = exec_cfg.exit_slippage_bps / 10000.0
    limit_exit_slippage = exec_cfg.limit_exit_slippage_bps / 10000.0

    open_pos: Optional[OpenPositionState] = None
    pending_entry_plan: Optional[OrderPlan] = None
    completed_trades: List[CompletedTradeRecord] = []
    traded_today = False

    in_flush = False
    flush_low = float("inf")
    flush_time: Optional[pd.Timestamp] = None
    max_flush_drop_pct = 0.0
    max_vwap_discount_pct = 0.0
    flush_vol_climax = False
    climax_vol_ratio = 0.0
    climax_bar_ts: Optional[str] = None
    tod_sufficient_any = bool(day_df["ToD_History_Sufficient"].any())

    last_signal_plan_on_latest_bar: Optional[OrderPlan] = None
    last_rejection_reason: Optional[str] = None
    last_spy_state = "HEALTHY"
    last_spy_ok = True
    last_spy_ret3: Optional[float] = None
    last_spy_vdist: Optional[float] = None

    bars = list(day_df.iterrows())
    open_bar_high = float(bars[0][1]["Open_Bar_High"])

    for i, (bar_ts, row) in enumerate(bars):
        bar_start_ts: pd.Timestamp = bar_ts
        bar_end_ts: pd.Timestamp = bar_start_ts + pd.Timedelta(minutes=5)
        is_final_bar_in_slice = (i == len(bars) - 1)
        last_signal_plan_on_latest_bar = None

        c_open = float(row["Open"])
        c_high = float(row["High"])
        c_low = float(row["Low"])
        c_close = float(row["Close"])
        c_vol = float(row["Volume"])
        c_vwap = float(row["VWAP"])
        c_mhigh = float(row["Morning_High"])
        c_ema12 = float(row["EMA12"])
        c_ema20 = float(row["EMA20"])
        c_vol_ma10_ex = float(row["VolMA10_ExOpen"])
        c_tod_ok = bool(row["ToD_History_Sufficient"])
        c_tod_med = float(row["ToD_MedVol"]) if c_tod_ok else np.nan
        c_midday_med = float(row["Midday_MedVol"]) if c_tod_ok else np.nan

        # ------------------------------------------------------------------
        # STEP 0: Fill pending 1-bar delayed entry at bar Open (if configured)
        # ------------------------------------------------------------------
        if pending_entry_plan is not None and open_pos is None:
            fill_px = round(c_open * (1.0 + entry_slippage), 4)
            prev_vwap_init = float(bars[i - 1][1]["VWAP"]) if i > 0 else c_vwap
            leg1_t = round(max(prev_vwap_init, fill_px * (1.0 + cfg.leg1_min_profit_pct)), 4)
            leg2_t = round(fill_px * (1.0 + cfg.leg2_base_target_pct), 4)
            open_pos = OpenPositionState(
                symbol=sym,
                signal_id=pending_entry_plan.signal_id,
                session_date=session_date.isoformat(),
                entry_bar_start_ts=pending_entry_plan.bar_start_ts,
                entry_bar_end_ts=pending_entry_plan.bar_end_ts,
                order_submit_ts=pending_entry_plan.decision_ts,
                entry_fill_ts=bar_start_ts.isoformat(),
                entry_signal_price=pending_entry_plan.entry_price,
                entry_fill_price=fill_px,
                flush_low=pending_entry_plan.flush_low,
                initial_stop_price=pending_entry_plan.stop_price,
                active_stop_price=pending_entry_plan.stop_price,
                total_shares=pending_entry_plan.total_shares,
                tranche1_shares=pending_entry_plan.tranche1_shares,
                tranche2_shares=pending_entry_plan.tranche2_shares,
                remaining_shares=pending_entry_plan.total_shares,
                leg1_filled=False,
                leg1_fill_ts=None,
                leg1_fill_price=None,
                runner_mode_active=False,
                runner_activated_ts=None,
                prev_bar_vwap=prev_vwap_init,
                active_leg1_target=leg1_t,
                active_leg2_target=leg2_t,
                last_mark_price=c_close,
                last_mark_ts=bar_end_ts.isoformat(),
                unrealized_pnl_usd=0.0,
                realized_leg1_pnl_usd=0.0,
                ambiguous_bar_count=0,
                climax_vol_ratio=pending_entry_plan.climax_vol_ratio,
                reclaim_type=pending_entry_plan.reclaim_type,
                order_plan=pending_entry_plan,
            )
            pending_entry_plan = None

        # ------------------------------------------------------------------
        # STEP 1: Evaluate previously active resting orders on bar `i` OHLC
        # ------------------------------------------------------------------
        if open_pos is not None:
            # Causal Leg 1 target during bar `i` uses `open_pos.prev_bar_vwap` (known at start of bar `i`)
            open_pos.active_leg1_target = round(
                max(open_pos.prev_bar_vwap, open_pos.entry_signal_price * (1.0 + cfg.leg1_min_profit_pct)),
                4,
            )
            # Leg 2 resting target during bar `i` depends ONLY on whether runner_mode_active was ALREADY True at start of bar `i`
            open_pos.active_leg2_target = round(
                open_pos.entry_signal_price * (
                    1.0 + (cfg.leg2_stretch_target_pct if open_pos.runner_mode_active else cfg.leg2_base_target_pct)
                ),
                4,
            )

            stop_px = open_pos.active_stop_price
            stop_hit = (c_low <= stop_px)

            if not open_pos.leg1_filled:
                leg1_hit = (c_high >= open_pos.active_leg1_target)
                if stop_hit and leg1_hit:
                    open_pos.ambiguous_bar_count += 1
                    if exec_cfg.ambiguous_bar_policy == "stop_first":
                        raw_stop_fill = min(c_open, stop_px)
                        fill_px = round(raw_stop_fill * (1.0 - exit_slippage), 4)
                        completed_trades.append(
                            _finalize_trade(
                                open_pos=open_pos,
                                exit_bar_start_ts=bar_start_ts.isoformat(),
                                exit_fill_ts=bar_end_ts.isoformat(),
                                leg2_fill_price=fill_px,
                                exit_reason="StopLoss(AmbiguousBarStopFirst)",
                            )
                        )
                        open_pos = None
                elif stop_hit:
                    raw_stop_fill = min(c_open, stop_px)
                    fill_px = round(raw_stop_fill * (1.0 - exit_slippage), 4)
                    completed_trades.append(
                        _finalize_trade(
                            open_pos=open_pos,
                            exit_bar_start_ts=bar_start_ts.isoformat(),
                            exit_fill_ts=bar_end_ts.isoformat(),
                            leg2_fill_price=fill_px,
                            exit_reason="StopLoss",
                        )
                    )
                    open_pos = None
                elif leg1_hit:
                    l1_fill_px = round(
                        max(c_open, open_pos.active_leg1_target) * (1.0 - limit_exit_slippage),
                        4,
                    )
                    open_pos.leg1_filled = True
                    open_pos.leg1_fill_ts = bar_end_ts.isoformat()
                    open_pos.leg1_fill_price = l1_fill_px
                    open_pos.remaining_shares = open_pos.tranche2_shares
                    open_pos.realized_leg1_pnl_usd = (
                        (l1_fill_px - open_pos.entry_fill_price) * open_pos.tranche1_shares
                    )
                    open_pos.active_stop_price = max(open_pos.active_stop_price, open_pos.entry_signal_price)

                    # Did Tranche 2 also hit its resting Leg 2 target or reverse through breakeven on this bar?
                    leg2_hit_same_bar = (c_high >= open_pos.active_leg2_target)
                    reversed_below_be_after_high = (c_close <= open_pos.entry_signal_price)

                    if leg2_hit_same_bar and reversed_below_be_after_high:
                        open_pos.ambiguous_bar_count += 1
                        fill_px = round(open_pos.entry_signal_price * (1.0 - exit_slippage), 4)
                        completed_trades.append(
                            _finalize_trade(
                                open_pos=open_pos,
                                exit_bar_start_ts=bar_start_ts.isoformat(),
                                exit_fill_ts=bar_end_ts.isoformat(),
                                leg2_fill_price=fill_px,
                                exit_reason="Leg1+Breakeven(AmbiguousBar)",
                            )
                        )
                        open_pos = None
                    elif reversed_below_be_after_high:
                        fill_px = round(open_pos.entry_signal_price * (1.0 - exit_slippage), 4)
                        completed_trades.append(
                            _finalize_trade(
                                open_pos=open_pos,
                                exit_bar_start_ts=bar_start_ts.isoformat(),
                                exit_fill_ts=bar_end_ts.isoformat(),
                                leg2_fill_price=fill_px,
                                exit_reason="Leg1+Breakeven",
                            )
                        )
                        open_pos = None
                    elif leg2_hit_same_bar:
                        # Note: A resting +0.85% target that fills during the candle cannot be retroactively canceled!
                        l2_fill_px = round(
                            max(c_open, open_pos.active_leg2_target) * (1.0 - limit_exit_slippage),
                            4,
                        )
                        completed_trades.append(
                            _finalize_trade(
                                open_pos=open_pos,
                                exit_bar_start_ts=bar_start_ts.isoformat(),
                                exit_fill_ts=bar_end_ts.isoformat(),
                                leg2_fill_price=l2_fill_px,
                                exit_reason="Leg1+Target2(0.85%)",
                            )
                        )
                        open_pos = None

            else:
                # Leg 1 was already filled on a prior bar `< i`; only Tranche 2 is open
                leg2_hit = (c_high >= open_pos.active_leg2_target)
                if stop_hit and leg2_hit:
                    open_pos.ambiguous_bar_count += 1
                    raw_stop_fill = min(c_open, stop_px)
                    fill_px = round(raw_stop_fill * (1.0 - exit_slippage), 4)
                    reason = "Leg1+RunnerTrailStop(AmbiguousBar)" if open_pos.runner_mode_active else "Leg1+Breakeven(AmbiguousBar)"
                    completed_trades.append(
                        _finalize_trade(
                            open_pos=open_pos,
                            exit_bar_start_ts=bar_start_ts.isoformat(),
                            exit_fill_ts=bar_end_ts.isoformat(),
                            leg2_fill_price=fill_px,
                            exit_reason=reason,
                        )
                    )
                    open_pos = None
                elif stop_hit:
                    raw_stop_fill = min(c_open, stop_px)
                    fill_px = round(raw_stop_fill * (1.0 - exit_slippage), 4)
                    reason = "Leg1+RunnerTrailStop" if (open_pos.runner_mode_active and stop_px > open_pos.entry_signal_price) else "Leg1+Breakeven"
                    completed_trades.append(
                        _finalize_trade(
                            open_pos=open_pos,
                            exit_bar_start_ts=bar_start_ts.isoformat(),
                            exit_fill_ts=bar_end_ts.isoformat(),
                            leg2_fill_price=fill_px,
                            exit_reason=reason,
                        )
                    )
                    open_pos = None
                elif leg2_hit:
                    l2_fill_px = round(
                        max(c_open, open_pos.active_leg2_target) * (1.0 - limit_exit_slippage),
                        4,
                    )
                    reason = "Leg1+RunnerStretch(1.6%)" if open_pos.runner_mode_active else "Leg1+Target2(0.85%)"
                    completed_trades.append(
                        _finalize_trade(
                            open_pos=open_pos,
                            exit_bar_start_ts=bar_start_ts.isoformat(),
                            exit_fill_ts=bar_end_ts.isoformat(),
                            leg2_fill_price=l2_fill_px,
                            exit_reason=reason,
                        )
                    )
                    open_pos = None

        # ------------------------------------------------------------------
        # STEP 2: Mandatory Time Flatten (15:40 ET regular / 12:40 ET early close)
        # Never flatten just because `is_final_bar_in_slice` is True!
        # ------------------------------------------------------------------
        if open_pos is not None and sched.mandatory_flatten_et is not None:
            if bar_start_ts >= sched.mandatory_flatten_et:
                fill_px = round(c_close * (1.0 - exit_slippage), 4)
                flatten_label = "EndOfDay(12:40_EarlyClose)" if sched.is_early_close else "EndOfDay(15:40)"
                if not open_pos.leg1_filled:
                    completed_trades.append(
                        _finalize_trade(
                            open_pos=open_pos,
                            exit_bar_start_ts=bar_start_ts.isoformat(),
                            exit_fill_ts=bar_end_ts.isoformat(),
                            leg2_fill_price=fill_px,
                            exit_reason=flatten_label,
                        )
                    )
                else:
                    completed_trades.append(
                        _finalize_trade(
                            open_pos=open_pos,
                            exit_bar_start_ts=bar_start_ts.isoformat(),
                            exit_fill_ts=bar_end_ts.isoformat(),
                            leg2_fill_price=fill_px,
                            exit_reason=f"Leg1+{flatten_label}",
                        )
                    )
                open_pos = None

        # ------------------------------------------------------------------
        # STEP 3: Completed-Bar Feature Update & Stop/Runner Amendments for Bar `i+1`
        # ------------------------------------------------------------------
        if open_pos is not None:
            open_pos.prev_bar_vwap = c_vwap
            open_pos.last_mark_price = c_close
            open_pos.last_mark_ts = bar_end_ts.isoformat()

            # Runner activation check at bar close (takes effect on subsequent bars `i+1` onward)
            if (
                not open_pos.runner_mode_active
                and c_close >= open_pos.entry_signal_price * (1.0 + cfg.runner_activation_profit_pct)
                and c_close > c_ema12
            ):
                open_pos.runner_mode_active = True
                open_pos.runner_activated_ts = bar_end_ts.isoformat()

            if open_pos.leg1_filled and open_pos.runner_mode_active:
                ema_candidate = round(c_ema20 * (1.0 - cfg.runner_ema20_buffer_pct), 4)
                max_safe_stop = round(c_close * (1.0 - cfg.runner_close_safety_buffer_pct), 4)
                validated_candidate = min(ema_candidate, max_safe_stop)
                open_pos.active_stop_price = max(
                    open_pos.active_stop_price,
                    open_pos.entry_signal_price,
                    validated_candidate,
                )

            open_pos.unrealized_pnl_usd = round(
                (c_close - open_pos.entry_fill_price) * open_pos.remaining_shares,
                2,
            )
            continue

        # ------------------------------------------------------------------
        # STEP 4: Entry Signal Evaluation on Completed Bar `i` (Exact V5 Logic + Causal Guards)
        # ------------------------------------------------------------------
        in_entry_window = (
            sched.entry_start_et is not None
            and sched.entry_end_et is not None
            and (sched.entry_start_et <= bar_start_ts <= sched.entry_end_et)
        )

        last_spy_state, last_spy_ok, last_spy_ret3, last_spy_vdist = lookup_spy_state_at_bar(
            spy_feat, bar_start_ts, cfg
        )

        if i < 1:
            continue

        drop_from_high = (c_low - c_mhigh) / c_mhigh if c_mhigh > 0 else 0.0
        below_vwap = (c_low - c_vwap) / c_vwap if c_vwap > 0 else 0.0
        max_flush_drop_pct = min(max_flush_drop_pct, drop_from_high)
        max_vwap_discount_pct = min(max_vwap_discount_pct, below_vwap)

        if in_entry_window and not traded_today:
            # If already in_flush and Close reclaims VWAP before triggering, reset flush state (exact V5 line 669)
            if in_flush and c_close >= c_vwap:
                in_flush = False
                flush_low = float("inf")
                flush_vol_climax = False
                flush_time = None
                climax_vol_ratio = 0.0
                climax_bar_ts = None

            post_open_rally = c_mhigh > open_bar_high * 1.001
            vol_mult_ex = c_vol / c_vol_ma10_ex if c_vol_ma10_ex > 0 else 0.0

            if c_tod_ok and pd.notna(c_tod_med) and pd.notna(c_midday_med):
                tod_ref = min(c_tod_med, c_midday_med)
                vol_mult_tod = c_vol / tod_ref if tod_ref > 0 else 0.0
                is_vol_climax = (
                    (c_vol >= cfg.vol_ma10_ex_open_mult * c_vol_ma10_ex)
                    or (c_vol >= cfg.tod_climax_mult * tod_ref and c_vol >= 1.0 * c_vol_ma10_ex)
                    or (
                        post_open_rally
                        and drop_from_high <= cfg.deep_flush_drop_pct
                        and below_vwap <= cfg.deep_vwap_discount_pct
                        and c_vol >= cfg.tod_climax_mult * tod_ref
                    )
                )
            else:
                # If ToD history is insufficient (< min_tod_history_sessions), mark insufficient history and do not trigger
                tod_ref = np.nan
                vol_mult_tod = 0.0
                is_vol_climax = False

            if (drop_from_high <= cfg.min_flush_drop_pct) and (below_vwap <= cfg.min_vwap_discount_pct):
                if (not in_flush) or (c_low < flush_low):
                    in_flush = True
                    flush_low = c_low
                    flush_time = bar_start_ts
                if is_vol_climax:
                    flush_vol_climax = True
                    cur_mult = max(vol_mult_ex, vol_mult_tod)
                    if cur_mult >= climax_vol_ratio:
                        climax_vol_ratio = cur_mult
                        climax_bar_ts = bar_start_ts.strftime("%H:%M")
            elif in_flush and (c_low < flush_low):
                flush_low = c_low
                flush_time = bar_start_ts
                if is_vol_climax:
                    flush_vol_climax = True
                    cur_mult = max(vol_mult_ex, vol_mult_tod)
                    if cur_mult >= climax_vol_ratio:
                        climax_vol_ratio = cur_mult
                        climax_bar_ts = bar_start_ts.strftime("%H:%M")

            if in_flush and flush_vol_climax and flush_time is not None:
                prev_close = float(bars[i - 1][1]["Close"])
                reclaim_bounce = (c_close - flush_low) / flush_low if flush_low > 0 else 0.0
                max_bounce = cfg.hl_max_bounce_rally_pct if post_open_rally else cfg.hl_max_bounce_base_pct
                close_below_vwap = (c_close - c_vwap) / c_vwap if c_vwap > 0 else 0.0

                is_hl_reclaim = (
                    (bar_start_ts > flush_time)
                    and (c_low > flush_low)
                    and (c_close > c_open)
                    and (c_close > prev_close)
                    and (cfg.hl_min_bounce_pct <= reclaim_bounce <= max_bounce)
                    and (close_below_vwap <= cfg.hl_max_close_vs_vwap_pct)
                )

                bar_rng = c_high - c_low
                is_hammer_reclaim = (
                    post_open_rally
                    and (bar_start_ts == flush_time)
                    and (below_vwap <= cfg.hammer_max_below_vwap_pct)
                    and (c_close > c_open)
                    and (bar_rng > 0)
                    and (((c_close - c_low) / bar_rng) >= cfg.hammer_min_close_pos_pct)
                    and (cfg.hammer_min_bounce_pct <= reclaim_bounce <= cfg.hammer_max_bounce_pct)
                    and (c_close < c_vwap)
                )

                if is_hl_reclaim or is_hammer_reclaim:
                    reclaim_type = "Higher-Low Reclaim" if is_hl_reclaim else "Hammer Reclaim"

                    if not regime_gate_ok:
                        last_rejection_reason = "BLOCKED_BY_ACCUM_REGIME_FILTER"
                        in_flush = False
                        flush_vol_climax = False
                        continue
                    if not er_gate_ok:
                        last_rejection_reason = f"BLOCKED_BY_EARNINGS_FILTER ({er_check.display_label})"
                        in_flush = False
                        flush_vol_climax = False
                        continue
                    if not last_spy_ok:
                        last_rejection_reason = f"BLOCKED_BY_SPY_FILTER ({last_spy_state})"
                        continue

                    stop_px, stop_dist_pct, policy_used, tot_sh, t1_sh, t2_sh, reject_reason = compute_stop_and_shares(
                        symbol=sym,
                        entry_price=c_close,
                        flush_low=flush_low,
                        cfg=cfg,
                        risk_cfg=risk_cfg,
                        clip_override_usd=clip_override_usd,
                    )
                    if reject_reason is not None or stop_px is None or tot_sh < 2:
                        last_rejection_reason = reject_reason or "REJECTED_STOP_CAP"
                        in_flush = False
                        flush_vol_climax = False
                        continue

                    sig_id = make_signal_id(sym, bar_start_ts, c_close)
                    leg1_init_t = round(max(c_vwap, c_close * (1.0 + cfg.leg1_min_profit_pct)), 4)
                    leg2_base_t = round(c_close * (1.0 + cfg.leg2_base_target_pct), 4)
                    leg2_stretch_t = round(c_close * (1.0 + cfg.leg2_stretch_target_pct), 4)
                    notional = round(tot_sh * c_close, 2)
                    planned_risk = round(tot_sh * (c_close - stop_px), 2)

                    why_text = (
                        f"{sym} flushed {max_flush_drop_pct*100:.2f}% from ${c_mhigh:.2f} morning high "
                        f"to ${flush_low:.2f} ({max_vwap_discount_pct*100:.2f}% vs ${c_vwap:.2f} VWAP) "
                        f"with a {climax_vol_ratio:.1f}x causal Volume Climax at {climax_bar_ts or bar_start_ts.strftime('%H:%M')} ET "
                        f"and printed a {reclaim_type} bar at {bar_start_ts.strftime('%H:%M')} ET @ ${c_close:.2f}."
                    )

                    plan = OrderPlan(
                        symbol=sym,
                        signal_id=sig_id,
                        session_date=session_date.isoformat(),
                        bar_start_ts=bar_start_ts.isoformat(),
                        bar_end_ts=bar_end_ts.isoformat(),
                        feature_available_ts=bar_end_ts.isoformat(),
                        decision_ts=bar_end_ts.isoformat(),
                        entry_price=round(c_close, 4),
                        flush_low=round(flush_low, 4),
                        stop_price=stop_px,
                        stop_distance_pct=round(stop_dist_pct, 6),
                        stop_policy_applied=policy_used,
                        total_shares=tot_sh,
                        tranche1_shares=t1_sh,
                        tranche2_shares=t2_sh,
                        notional_usd=notional,
                        planned_risk_usd=planned_risk,
                        leg1_initial_target=leg1_init_t,
                        leg2_base_target=leg2_base_t,
                        leg2_stretch_target=leg2_stretch_t,
                        climax_vol_ratio=round(climax_vol_ratio, 2),
                        reclaim_type=reclaim_type,
                        why_diagnosis=why_text,
                    )

                    traded_today = True
                    if is_final_bar_in_slice:
                        last_signal_plan_on_latest_bar = plan

                    if exec_cfg.entry_delay_bars > 0:
                        pending_entry_plan = plan
                    else:
                        fill_px = round(c_close * (1.0 + entry_slippage), 4)
                        open_pos = OpenPositionState(
                            symbol=sym,
                            signal_id=sig_id,
                            session_date=session_date.isoformat(),
                            entry_bar_start_ts=bar_start_ts.isoformat(),
                            entry_bar_end_ts=bar_end_ts.isoformat(),
                            order_submit_ts=bar_end_ts.isoformat(),
                            entry_fill_ts=bar_end_ts.isoformat(),
                            entry_signal_price=round(c_close, 4),
                            entry_fill_price=fill_px,
                            flush_low=round(flush_low, 4),
                            initial_stop_price=stop_px,
                            active_stop_price=stop_px,
                            total_shares=tot_sh,
                            tranche1_shares=t1_sh,
                            tranche2_shares=t2_sh,
                            remaining_shares=tot_sh,
                            leg1_filled=False,
                            leg1_fill_ts=None,
                            leg1_fill_price=None,
                            runner_mode_active=False,
                            runner_activated_ts=None,
                            prev_bar_vwap=c_vwap,
                            active_leg1_target=leg1_init_t,
                            active_leg2_target=leg2_base_t,
                            last_mark_price=c_close,
                            last_mark_ts=bar_end_ts.isoformat(),
                            unrealized_pnl_usd=round((c_close - fill_px) * tot_sh, 2),
                            realized_leg1_pnl_usd=0.0,
                            ambiguous_bar_count=0,
                            climax_vol_ratio=round(climax_vol_ratio, 2),
                            reclaim_type=reclaim_type,
                            order_plan=plan,
                        )

    last_bar_ts, last_row = bars[-1]
    last_bar_end_ts = last_bar_ts + pd.Timedelta(minutes=5)

    is_exec_now = bool(
        last_signal_plan_on_latest_bar is not None
        and regime_gate_ok
        and er_gate_ok
        and last_spy_ok
    )

    if is_exec_now:
        status = "ENTRY_SIGNAL_NOW"
    elif open_pos is not None:
        status = "IN_POSITION"
    elif completed_trades:
        status = "TRADED_COMPLETED"
    elif not tod_sufficient_any:
        status = "INSUFFICIENT_HISTORY"
    elif not regime_gate_ok:
        status = "BLOCKED_REGIME"
    elif not er_gate_ok:
        status = "BLOCKED_EARNINGS"
    elif not last_spy_ok:
        status = "BLOCKED_SPY"
    elif last_rejection_reason is not None:
        status = "REJECTED_STOP_CAP"
    elif in_flush:
        status = "ARMED_FLUSHING"
    else:
        status = "WATCHING_NO_FLUSH"

    return SessionEvaluationResult(
        symbol=sym,
        session_date=session_date.isoformat(),
        as_of_ts=as_of_ts.isoformat() if as_of_ts is not None else last_bar_end_ts.isoformat(),
        status=status,
        is_executable_now=is_exec_now,
        prior_completed_session_date=prior_dt.isoformat() if prior_dt else None,
        prior_session_accum_ok=prior_accum_ok,
        developing_next_session_accum=dev_next_accum,
        tod_history_sufficient=tod_sufficient_any,
        earnings_check=er_check,
        spy_state=last_spy_state,
        spy_ok=last_spy_ok,
        spy_3bar_ret=last_spy_ret3,
        spy_vwap_dist=last_spy_vdist,
        current_price=round(float(last_row["Close"]), 4),
        current_vwap=round(float(last_row["VWAP"]), 4),
        morning_high=round(float(last_row["Morning_High"]), 4),
        flush_low=round(flush_low, 4) if flush_low != float("inf") else round(float(day_df["Low"].min()), 4),
        max_flush_drop_pct=round(max_flush_drop_pct, 6),
        max_vwap_discount_pct=round(max_vwap_discount_pct, 6),
        climax_vol_ratio=round(climax_vol_ratio, 2) if climax_vol_ratio > 0 else None,
        climax_bar_ts=climax_bar_ts,
        latest_bar_start_ts=last_bar_ts.isoformat(),
        latest_bar_end_ts=last_bar_end_ts.isoformat(),
        active_signal_plan=last_signal_plan_on_latest_bar,
        open_position=open_pos,
        completed_trades=completed_trades,
        rejection_reason=last_rejection_reason,
    )


def _finalize_trade(
    open_pos: OpenPositionState,
    exit_bar_start_ts: str,
    exit_fill_ts: str,
    leg2_fill_price: float,
    exit_reason: str,
) -> CompletedTradeRecord:
    """
    Compute exact share-weighted PnL and net/gross returns across Tranche 1 and Tranche 2.
    """
    entry_fill = open_pos.entry_fill_price
    entry_sig = open_pos.entry_signal_price
    tot_sh = open_pos.total_shares
    t1_sh = open_pos.tranche1_shares
    t2_sh = open_pos.tranche2_shares

    if open_pos.leg1_filled and open_pos.leg1_fill_price is not None:
        l1_px = open_pos.leg1_fill_price
        pnl_usd = (l1_px - entry_fill) * t1_sh + (leg2_fill_price - entry_fill) * t2_sh
        avg_exit_px = (l1_px * t1_sh + leg2_fill_price * t2_sh) / tot_sh
    else:
        pnl_usd = (leg2_fill_price - entry_fill) * tot_sh
        avg_exit_px = leg2_fill_price

    notional = entry_fill * tot_sh
    net_ret_pct = (pnl_usd / notional) * 100.0 if notional > 0 else 0.0
    gross_ret_pct = ((avg_exit_px - entry_sig) / entry_sig) * 100.0 if entry_sig > 0 else 0.0
    stop_dist_pct = ((entry_sig - open_pos.initial_stop_price) / entry_sig) * 100.0 if entry_sig > 0 else 0.0

    return CompletedTradeRecord(
        symbol=open_pos.symbol,
        signal_id=open_pos.signal_id,
        session_date=open_pos.session_date,
        entry_bar_start_ts=open_pos.entry_bar_start_ts,
        entry_bar_end_ts=open_pos.entry_bar_end_ts,
        feature_available_ts=open_pos.entry_bar_end_ts,
        decision_ts=open_pos.order_plan.decision_ts,
        order_submit_ts=open_pos.order_submit_ts,
        entry_fill_ts=open_pos.entry_fill_ts,
        exit_bar_start_ts=exit_bar_start_ts,
        exit_fill_ts=exit_fill_ts,
        reclaim_type=open_pos.reclaim_type,
        entry_signal_price=entry_sig,
        entry_fill_price=entry_fill,
        flush_low=open_pos.flush_low,
        initial_stop_price=open_pos.initial_stop_price,
        stop_distance_pct=round(stop_dist_pct, 4),
        total_shares=tot_sh,
        tranche1_shares=t1_sh,
        tranche2_shares=t2_sh,
        notional_usd=round(notional, 2),
        planned_risk_usd=open_pos.order_plan.planned_risk_usd,
        leg1_filled=open_pos.leg1_filled,
        leg1_fill_ts=open_pos.leg1_fill_ts,
        leg1_fill_price=open_pos.leg1_fill_price,
        leg2_fill_ts=exit_fill_ts,
        leg2_fill_price=leg2_fill_price,
        exit_reason=exit_reason,
        runner_mode_used=open_pos.runner_mode_active,
        ambiguous_bar_count=open_pos.ambiguous_bar_count,
        gross_return_pct=round(gross_ret_pct, 4),
        net_return_pct=round(net_ret_pct, 4),
        net_pnl_usd=round(pnl_usd, 2),
        climax_vol_ratio=open_pos.climax_vol_ratio,
    )
