"""
tests/test_audited_v6.py
========================
Comprehensive offline regression test suite for Flush-Reclaim Audited V6.
Runs completely offline using synthetic fixtures and mocked broker interfaces
(no network calls, no real broker credentials required).
"""

from __future__ import annotations
from datetime import date, datetime, time, timedelta
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

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
    ModeConflictError,
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
from portfolio_simulator import run_portfolio_simulation
from tasty_executor import (
    LifecycleState,
    ValidationMode,
    QuoteSnapshot,
    AccountRiskSnapshot,
    ExecutionJournal,
    OrderLifecycleManager,
    validate_order_safety,
    validate_account_risk_limits,
    revalidate_before_submission,
    validate_session_cache_metadata,
    build_v6_split_tranche_orders,
)
from universe_scanner import (
    EXPECTED_SCANNER_COLUMNS,
    evaluate_quote_spread,
    rank_and_filter_candidates,
)
from test_regimes import (
    parse_cli_args,
    render_trader_hud,
)


def make_synthetic_daily_df(
    num_days: int = 65,
    end_date: date = date(2026, 11, 9),
    make_today_accum_only: bool = False,
) -> pd.DataFrame:
    """
    Create synthetic daily OHLCV bars in America/New_York satisfying both
    `core_v5_dislocated_sma5` and `scanner_hl10_atr_compression` (unless `make_today_accum_only=True`,
    in which case the prior completed session is NOT in accumulation while today's developing bar IS).
    """
    cal = NYSECalendar()
    days = []
    cur = end_date
    while len(days) < num_days:
        if cal.is_trading_day(cur):
            days.append(cur)
        cur -= timedelta(days=1)
    days.reverse()

    rows = []
    idx = []
    for i, d in enumerate(days):
        ts = datetime.combine(d, time(0, 0), tzinfo=NY_TZ)
        idx.append(ts)
        offset_from_end = len(days) - 1 - i

        if i < 20:
            # Earlier 60d peak around 130.0 so stock is >=12% dislocated off 60d high
            low = 128.0
            open_p = 128.5
            high = 130.0
            close = 129.0
            vol = 1_500_000.0
        elif make_today_accum_only:
            if offset_from_end == 0:
                # Today's developing bar flips BOTH proxies into accumulation:
                # 6th higher low in 10d, strong green close > rising SMA5, UpVol5 >= 55%
                low = 101.00
                open_p = 101.10
                high = 103.50
                close = 103.40
                vol = 5_000_000.0
            elif offset_from_end == 1:
                # Yesterday (prior completed session): 5th higher low, but red close below SMA5 -> NOT in accum!
                low = 100.50
                open_p = 101.20
                high = 101.30
                close = 100.55
                vol = 1_500_000.0
            elif 2 <= offset_from_end <= 6:
                # 4 higher lows on days -5..-2 above day -6 base, green days
                low = 100.00 + (6 - offset_from_end) * 0.10
                open_p = low + 0.05
                high = low + 0.90
                close = low + 0.80
                vol = 1_500_000.0
            else:
                # Earlier days (-7 and older): falling lows so 10d HL count on yesterday is 5 (<6)
                low = 100.00 + (offset_from_end - 6) * 0.35
                open_p = low + 0.10
                high = low + 0.80
                close = low + 0.30
                vol = 1_200_000.0
        else:
            # Steady higher lows, green candles, rising SMA5, >=12% below 130 peak -> in accumulation!
            low = 95.0 + (i - 20) * 0.15
            open_p = low + 0.10
            high = low + 1.10
            close = low + 0.85
            vol = 1_500_000.0 + (i * 20_000.0)

        rows.append({"Open": open_p, "High": high, "Low": low, "Close": close, "Volume": vol})

    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx))


def make_synthetic_5m_dataset(
    num_prior_sessions: int = 12,
    target_date: date = date(2026, 11, 9),
    include_flush_reclaim_on_target: bool = True,
    reclaim_style: str = "hl",  # "hl" or "hammer"
    next_bar_gap_down_open: float | None = None,
    ambiguous_stop_and_target_bar: bool = False,
    flush_low_override: float | None = None,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Build a multi-session 5m dataset (plus healthy SPY 5m dataset) ending on `target_date`.
    """
    cal = NYSECalendar()
    sessions = []
    cur = target_date - timedelta(days=1)
    while len(sessions) < num_prior_sessions:
        if cal.is_trading_day(cur):
            sessions.append(cur)
        cur -= timedelta(days=1)
    sessions.reverse()
    sessions.append(target_date)

    stock_rows = []
    stock_idx = []
    spy_rows = []
    spy_idx = []

    for sess_d in sessions:
        is_target = (sess_d == target_date)
        sched = cal.get_session_schedule(sess_d)
        t_cur = datetime.combine(sess_d, time(9, 30), tzinfo=NY_TZ)
        t_end = (sched.market_close_et - timedelta(minutes=5)) if (sched.market_close_et is not None) else datetime.combine(sess_d, time(15, 55), tzinfo=NY_TZ)

        while t_cur <= t_end:
            hhmm = t_cur.strftime("%H:%M")
            spy_idx.append(t_cur)
            spy_rows.append({"Open": 580.0, "High": 580.5, "Low": 579.8, "Close": 580.2, "Volume": 500_000.0})

            if not is_target or not include_flush_reclaim_on_target:
                stock_idx.append(t_cur)
                stock_rows.append({"Open": 100.0, "High": 100.3, "Low": 99.8, "Close": 100.1, "Volume": 50_000.0})
            else:
                f_low = flush_low_override if flush_low_override is not None else 99.85
                if hhmm == "09:30":
                    o, h, l, c, v = 100.70, 101.00, 100.60, 100.85, 80_000.0
                elif hhmm in ("09:35", "09:40", "09:45", "09:50"):
                    # Post-open rally: High 101.25 > 101.00 * 1.001
                    o, h, l, c, v = 100.90, 101.25, 100.85, 101.10, 60_000.0
                elif hhmm == "09:55":
                    if reclaim_style == "hl":
                        # Volume climax flush bar (180k >= 3.6x ToD median)
                        o, h, l, c, v = 100.90, 100.95, f_low, f_low + 0.05, 180_000.0
                    else:
                        # Preliminary dip before 10:00 hammer climax bar
                        o, h, l, c, v = 100.90, 100.95, 100.20, 100.25, 60_000.0
                elif hhmm == "10:00":
                    if reclaim_style == "hl":
                        # Higher-Low Reclaim bar at 10:00 ET:
                        # Low > f_low, Close > Open, Close > prev_Close, bounce in [0.15%, 0.80%], Close < VWAP
                        c_target = round(f_low * 1.0035, 2)
                        o = round(f_low + 0.08, 2)
                        l = round(f_low + 0.05, 2)
                        h = round(c_target + 0.05, 2)
                        c = c_target
                        v = 90_000.0
                    else:
                        # Hammer Reclaim bar at 10:00 ET (`ts == flush_time`, `post_open_rally`, `below_vwap <= -0.65%`,
                        # `(Close - Low)/(High - Low) >= 0.75`, `0.0040 <= reclaim_bounce <= 0.0080`)
                        l = f_low
                        c = round(f_low * 1.0055, 2)
                        o = round(f_low * 1.0045, 2)
                        h = round(c + 0.05, 2)
                        v = 180_000.0
                elif hhmm == "10:05":
                    if next_bar_gap_down_open is not None:
                        o = next_bar_gap_down_open
                        h = next_bar_gap_down_open + 0.50
                        l = next_bar_gap_down_open - 1.00
                        c = next_bar_gap_down_open + 0.20
                        v = 100_000.0
                    elif ambiguous_stop_and_target_bar:
                        o, h, l, c, v = 100.20, 101.20, 99.10, 100.10, 100_000.0
                    else:
                        o, h, l, c, v = 100.20, 100.45, 100.15, 100.40, 50_000.0
                else:
                    o, h, l, c, v = 100.40, 100.55, 100.30, 100.45, 50_000.0
                stock_idx.append(t_cur)
                stock_rows.append({"Open": o, "High": h, "Low": l, "Close": c, "Volume": v})

            t_cur += timedelta(minutes=5)

    return (
        pd.DataFrame(stock_rows, index=pd.DatetimeIndex(stock_idx)),
        pd.DataFrame(spy_rows, index=pd.DatetimeIndex(spy_idx)),
    )


class TestAuditedV6Suite(unittest.TestCase):

    # ------------------------------------------------------------------
    # TEST 1: Truncation Invariance Across All Features & Decisions
    # ------------------------------------------------------------------
    def test_01_truncation_invariance_and_no_first_session_leakage(self):
        d1 = make_synthetic_daily_df(65, date(2026, 11, 9))
        d5, spy5 = make_synthetic_5m_dataset(12, date(2026, 11, 9))
        cfg = StrategyConfig(er_unknown_policy="allow")
        exec_cfg = ExecutionConfig()
        risk_cfg = RiskConfig()
        oracle = EarningsOracle(unknown_policy="allow")

        full_feat = compute_causal_5m_features(d5, cfg)

        # Verify first session in 5m history has NaN for ToD_MedVol and Midday_MedVol (no .bfill() leakage!)
        first_date = sorted(full_feat["Date"].unique())[0]
        first_day_rows = full_feat[full_feat["Date"] == first_date]
        self.assertTrue(first_day_rows["ToD_MedVol"].isna().all(), "First session ToD_MedVol must be NaN (no .bfill())")
        self.assertTrue(first_day_rows["Midday_MedVol"].isna().all(), "First session Midday_MedVol must be NaN (no .bfill())")
        self.assertFalse(first_day_rows["ToD_History_Sufficient"].any())

        # Truncate raw data at 10:00 ET (signal bar), 10:30 ET, and 12:15 ET
        for cutoff_hhmm in ((10, 0), (10, 30), (12, 15)):
            cutoff_ts = pd.Timestamp(datetime.combine(date(2026, 11, 9), time(*cutoff_hhmm), tzinfo=NY_TZ))
            d5_trunc = d5[d5.index <= cutoff_ts].copy()
            d1_trunc = d1[d1.index.date <= cutoff_ts.date()].copy()
            spy_trunc = spy5[spy5.index <= cutoff_ts].copy()

            trunc_feat = compute_causal_5m_features(d5_trunc, cfg)
            full_slice = full_feat[full_feat.index <= cutoff_ts]

            for col in ("ToD_MedVol", "Vol_ToD_Ratio", "Midday_MedVol", "VolMA10_ExOpen", "VWAP", "Morning_High", "EMA12", "EMA20"):
                np.testing.assert_allclose(
                    trunc_feat[col].values,
                    full_slice[col].values,
                    rtol=1e-12,
                    atol=1e-12,
                    err_msg=f"Feature {col} failed truncation invariance at {cutoff_ts}",
                )

            res_from_full = evaluate_session_causal(
                symbol="FISV",
                session_date=date(2026, 11, 9),
                df_1d=d1,
                df_5m=d5,
                df_spy_5m=spy5,
                cfg=cfg,
                exec_cfg=exec_cfg,
                risk_cfg=risk_cfg,
                earnings_oracle=oracle,
                as_of_ts=cutoff_ts,
            )
            res_from_trunc = evaluate_session_causal(
                symbol="FISV",
                session_date=date(2026, 11, 9),
                df_1d=d1_trunc,
                df_5m=d5_trunc,
                df_spy_5m=spy_trunc,
                cfg=cfg,
                exec_cfg=exec_cfg,
                risk_cfg=risk_cfg,
                earnings_oracle=oracle,
            )
            self.assertEqual(res_from_full.status, res_from_trunc.status)
            self.assertEqual(res_from_full.is_executable_now, res_from_trunc.is_executable_now)
            self.assertEqual(len(res_from_full.completed_trades), len(res_from_trunc.completed_trades))
            if res_from_full.open_position is not None:
                self.assertIsNotNone(res_from_trunc.open_position)
                self.assertEqual(
                    res_from_full.open_position.entry_fill_price,
                    res_from_trunc.open_position.entry_fill_price,
                )

    # ------------------------------------------------------------------
    # TEST 2: Prior-Day vs Current-Day Developing Accumulation Separation
    # ------------------------------------------------------------------
    def test_02_prior_day_vs_current_day_accum_separation(self):
        d1 = make_synthetic_daily_df(65, date(2026, 11, 9), make_today_accum_only=True)
        d5, spy5 = make_synthetic_5m_dataset(12, date(2026, 11, 9))
        oracle = EarningsOracle(unknown_policy="allow")

        for policy in ("core_v5_dislocated_sma5", "scanner_hl10_atr_compression"):
            cfg = StrategyConfig(use_accum_regime_filter=True, daily_regime_policy=policy, er_unknown_policy="allow")
            table = compute_daily_regime_table(d1, cfg)
            prior_dt, prior_accum_ok, dev_next_accum = lookup_causal_daily_regime(table, date(2026, 11, 9))

            self.assertEqual(prior_dt, date(2026, 11, 6))
            self.assertFalse(prior_accum_ok, f"Prior session must NOT be in accumulation under {policy}")
            self.assertTrue(dev_next_accum, f"Developing current session IS in accumulation under {policy}")

            res = evaluate_session_causal(
                symbol="FISV",
                session_date=date(2026, 11, 9),
                df_1d=d1,
                df_5m=d5,
                df_spy_5m=spy5,
                cfg=cfg,
                earnings_oracle=oracle,
            )
            self.assertFalse(res.prior_session_accum_ok)
            self.assertTrue(res.developing_next_session_accum)
            self.assertEqual(len(res.completed_trades), 0)
            self.assertIsNone(res.open_position)

    # ------------------------------------------------------------------
    # TEST 3: Higher-Low and Hammer Reclaim Parity Across Replay & HUD
    # ------------------------------------------------------------------
    def test_03_hl_and_hammer_reclaim_parity(self):
        d1 = make_synthetic_daily_df(65, date(2026, 11, 9))
        oracle = EarningsOracle(unknown_policy="allow")
        cfg = StrategyConfig(er_unknown_policy="allow")
        cutoff_1000 = pd.Timestamp(datetime(2026, 11, 9, 10, 0, tzinfo=NY_TZ))

        for style, expected_label in (("hl", "Higher-Low Reclaim"), ("hammer", "Hammer Reclaim")):
            d5, spy5 = make_synthetic_5m_dataset(12, date(2026, 11, 9), reclaim_style=style)
            res = evaluate_session_causal(
                symbol="FISV",
                session_date=date(2026, 11, 9),
                df_1d=d1,
                df_5m=d5,
                df_spy_5m=spy5,
                cfg=cfg,
                earnings_oracle=oracle,
                as_of_ts=cutoff_1000,
            )
            self.assertEqual(res.status, "ENTRY_SIGNAL_NOW")
            self.assertTrue(res.is_executable_now)
            self.assertIsNotNone(res.active_signal_plan)
            self.assertEqual(res.active_signal_plan.reclaim_type, expected_label)

            # Verify HUD returns exact same executable signal
            hud_exec = render_trader_hud([res], mode="monitor")
            self.assertEqual(len(hud_exec), 1)
            self.assertEqual(hud_exec[0].active_signal_plan.reclaim_type, expected_label)

    # ------------------------------------------------------------------
    # TEST 4: Partial-Session Evaluation (< 30 Bars) Does Not Force Close
    # ------------------------------------------------------------------
    def test_04_partial_session_keeps_position_open(self):
        d1 = make_synthetic_daily_df(65, date(2026, 11, 9))
        d5, spy5 = make_synthetic_5m_dataset(12, date(2026, 11, 9))
        cfg = StrategyConfig(er_unknown_policy="allow")
        oracle = EarningsOracle(unknown_policy="allow")

        cutoff_1015 = pd.Timestamp(datetime(2026, 11, 9, 10, 15, tzinfo=NY_TZ))
        res = evaluate_session_causal(
            symbol="FISV",
            session_date=date(2026, 11, 9),
            df_1d=d1,
            df_5m=d5,
            df_spy_5m=spy5,
            cfg=cfg,
            earnings_oracle=oracle,
            as_of_ts=cutoff_1015,
        )
        self.assertEqual(res.status, "IN_POSITION")
        self.assertIsNotNone(res.open_position)
        self.assertEqual(len(res.completed_trades), 0, "Partial morning slice must not close open position!")

        res_full = evaluate_session_causal(
            symbol="FISV",
            session_date=date(2026, 11, 9),
            df_1d=d1,
            df_5m=d5,
            df_spy_5m=spy5,
            cfg=cfg,
            earnings_oracle=oracle,
        )
        self.assertEqual(res_full.status, "TRADED_COMPLETED")
        self.assertIsNone(res_full.open_position)
        self.assertEqual(len(res_full.completed_trades), 1)
        self.assertIn("15:40", res_full.completed_trades[0].exit_reason)

    # ------------------------------------------------------------------
    # TEST 5: Gap-Through-Stop Loss Calculation (Exact Prompt Example)
    # ------------------------------------------------------------------
    def test_05_gap_through_stop_loss_calculation(self):
        """
        Prompt requirement:
        If a position enters at 100.50 with initial stop 99.84675 (-0.65%) and the next bar
        has Open 98.00, High 98.50, Low 97.00, Close 98.20, a stop-market model must fill
        around 98.00 minus slippage (roughly -2.5%), not at 99.84675 (-0.65%).
        """
        d1 = make_synthetic_daily_df(65, date(2026, 11, 9))
        d5, spy5 = make_synthetic_5m_dataset(
            12,
            date(2026, 11, 9),
            flush_low_override=99.90,
            next_bar_gap_down_open=98.00,
        )
        ts_1000 = pd.Timestamp(datetime(2026, 11, 9, 10, 0, tzinfo=NY_TZ))
        d5.loc[ts_1000, "Open"] = 99.95
        d5.loc[ts_1000, "Low"] = 99.95
        d5.loc[ts_1000, "High"] = 100.55
        d5.loc[ts_1000, "Close"] = 100.50

        cfg = StrategyConfig(er_unknown_policy="allow", stop_policy="strict_cap_reject")
        exec_cfg = ExecutionConfig(entry_slippage_bps=1.5, exit_slippage_bps=1.5)
        oracle = EarningsOracle(unknown_policy="allow")

        res = evaluate_session_causal(
            symbol="FISV",
            session_date=date(2026, 11, 9),
            df_1d=d1,
            df_5m=d5,
            df_spy_5m=spy5,
            cfg=cfg,
            exec_cfg=exec_cfg,
            earnings_oracle=oracle,
        )
        self.assertEqual(len(res.completed_trades), 1)
        tr = res.completed_trades[0]
        self.assertAlmostEqual(tr.leg2_fill_price, round(98.00 * (1.0 - 0.00015), 4), places=4)
        self.assertLess(tr.net_return_pct, -2.40)

    # ------------------------------------------------------------------
    # TEST 6: Causal Leg 1 VWAP Target Uses Prior Bar VWAP
    # ------------------------------------------------------------------
    def test_06_causal_leg1_vwap_uses_prior_bar(self):
        d1 = make_synthetic_daily_df(65, date(2026, 11, 9))
        d5, spy5 = make_synthetic_5m_dataset(12, date(2026, 11, 9))
        ts_1005 = pd.Timestamp(datetime(2026, 11, 9, 10, 5, tzinfo=NY_TZ))
        d5.loc[ts_1005, "Open"] = 100.22
        d5.loc[ts_1005, "Low"] = 100.21
        d5.loc[ts_1005, "High"] = 100.78
        d5.loc[ts_1005, "Close"] = 100.72

        cfg = StrategyConfig(er_unknown_policy="allow")
        oracle = EarningsOracle(unknown_policy="allow")
        res = evaluate_session_causal(
            symbol="FISV",
            session_date=date(2026, 11, 9),
            df_1d=d1,
            df_5m=d5,
            df_spy_5m=spy5,
            cfg=cfg,
            earnings_oracle=oracle,
            as_of_ts=ts_1005,
        )
        self.assertIsNotNone(res.open_position)
        self.assertTrue(res.open_position.leg1_filled, "Leg 1 must fill against prior completed bar's VWAP")

    # ------------------------------------------------------------------
    # TEST 7: Deterministic Ambiguous-Bar Handling (Stop Triggers First)
    # ------------------------------------------------------------------
    def test_07_ambiguous_bar_stop_first(self):
        d1 = make_synthetic_daily_df(65, date(2026, 11, 9))
        d5, spy5 = make_synthetic_5m_dataset(
            12,
            date(2026, 11, 9),
            ambiguous_stop_and_target_bar=True,
        )
        cfg = StrategyConfig(er_unknown_policy="allow")
        oracle = EarningsOracle(unknown_policy="allow")
        res = evaluate_session_causal(
            symbol="FISV",
            session_date=date(2026, 11, 9),
            df_1d=d1,
            df_5m=d5,
            df_spy_5m=spy5,
            cfg=cfg,
            earnings_oracle=oracle,
        )
        self.assertEqual(len(res.completed_trades), 1)
        tr = res.completed_trades[0]
        self.assertGreaterEqual(tr.ambiguous_bar_count, 1)
        self.assertIn("AmbiguousBarStopFirst", tr.exit_reason)
        self.assertLess(tr.net_return_pct, 0.0)

    # ------------------------------------------------------------------
    # TEST 8: Stop-Cap (0.65%) & Sizing Reconciliation (Prompt Example)
    # ------------------------------------------------------------------
    def test_08_stop_cap_reconciliation_and_sizing_parity(self):
        """
        Prompt example: flush_low = 100.00, entry = 100.80 -> stop = 99.95 (-0.843%).
        Verify:
          1) `strict_cap_reject` rejects the trade because 0.843% > 0.65%.
          2) `structural_risk_sized` keeps stop at 99.95 and scales shares down so planned risk <= $975.
          3) Backtest sizing matches OrderPlan / validate_order_safety sizing.
        """
        risk_cfg = RiskConfig(target_clip_usd=150_000.0)

        cfg_strict = StrategyConfig(stop_policy="strict_cap_reject")
        stop_px, dist_pct, pol, tot_sh, t1, t2, rej = compute_stop_and_shares(
            "FISV", 100.80, 100.00, cfg_strict, risk_cfg
        )
        self.assertAlmostEqual(stop_px, 99.95, places=2)
        self.assertAlmostEqual(dist_pct * 100.0, 0.8433, places=3)
        self.assertEqual(tot_sh, 0)
        self.assertIsNotNone(rej)
        self.assertIn("REJECTED_STOP_EXCEEDS_CAP", rej)

        cfg_sized = StrategyConfig(stop_policy="structural_risk_sized")
        stop_px2, dist_pct2, pol2, tot_sh2, t1_2, t2_2, rej2 = compute_stop_and_shares(
            "FISV", 100.80, 100.00, cfg_sized, risk_cfg
        )
        self.assertIsNone(rej2)
        self.assertGreater(tot_sh2, 0)
        self.assertEqual(t1_2 + t2_2, tot_sh2)
        planned_risk_usd = tot_sh2 * (100.80 - stop_px2)
        self.assertLessEqual(planned_risk_usd, 150_000.0 * 0.0065 + 0.01)

        # Verify order planner and broker safety validator accept exact sized output
        validated = validate_order_safety(
            ticker="FISV",
            shares=tot_sh2,
            limit_price=100.80,
            stop_price=stop_px2,
            account_id="5WJ26897",
            risk_cfg=risk_cfg,
        )
        self.assertEqual(validated["shares"], tot_sh2)
        self.assertEqual(validated["tranche1_shares"], t1_2)
        self.assertEqual(validated["tranche2_shares"], t2_2)

    # ------------------------------------------------------------------
    # TEST 9: Missing SPY & Unknown-Earnings Blocking
    # ------------------------------------------------------------------
    def test_09_missing_spy_and_unknown_earnings_block(self):
        d1 = make_synthetic_daily_df(65, date(2026, 11, 9))
        d5, spy5 = make_synthetic_5m_dataset(12, date(2026, 11, 9))

        cfg = StrategyConfig(use_spy_waterfall_filter=True, er_unknown_policy="allow")
        oracle_allow = EarningsOracle(unknown_policy="allow")
        res_no_spy = evaluate_session_causal(
            symbol="FISV",
            session_date=date(2026, 11, 9),
            df_1d=d1,
            df_5m=d5,
            df_spy_5m=pd.DataFrame(),
            cfg=cfg,
            earnings_oracle=oracle_allow,
        )
        self.assertEqual(res_no_spy.spy_state, "UNKNOWN_BLOCKED")
        self.assertFalse(res_no_spy.spy_ok)
        self.assertEqual(len(res_no_spy.completed_trades), 0)

        oracle_strict = EarningsOracle(registry={}, unknown_policy="block")
        er_res = oracle_strict.check_blackout("UNKN", date(2026, 11, 9), use_filter=True)
        self.assertEqual(er_res.status, "UNKNOWN")
        self.assertTrue(er_res.is_blocked)
        self.assertNotIn(">14d", er_res.display_label)

    # ------------------------------------------------------------------
    # TEST 10: Broker Preflight Failure Produces Zero Live-Submission Calls
    # ------------------------------------------------------------------
    def test_10_broker_preflight_failure_blocks_live_submission(self):
        bundle = build_v6_split_tranche_orders(
            ticker="FISV",
            shares=2000,
            limit_price=63.00,
            stop_price=62.65,
            leg1_target=63.30,
            leg2_target=63.55,
            account_id="5WJ26897",
        )
        self.assertEqual(bundle["validation_mode"], ValidationMode.LOCAL_SCHEMA_VALIDATION_ONLY.value)
        self.assertFalse(bundle["broker_preflight_verified"])

        with tempfile.TemporaryDirectory() as tmpdir:
            journal = ExecutionJournal(os.path.join(tmpdir, "journal.json"))
            mgr = OrderLifecycleManager(journal=journal)
            mock_acct = MagicMock()
            mock_sess = MagicMock()

            with self.assertRaises(PreflightRejectedError):
                mgr.submit_live_split_tranche_order(
                    mock_acct, mock_sess, "sig_1", "intent_1", bundle
                )
            # Zero live submission calls occurred
            self.assertEqual(mock_acct.place_complex_order.call_count, 0)

            mock_acct.place_complex_order.side_effect = RuntimeError("Connection reset by peer")
            with self.assertRaises(PreflightRejectedError):
                mgr.run_broker_preflight(mock_acct, mock_sess, bundle)
            self.assertFalse(bundle["broker_preflight_verified"])

            err_resp = MagicMock()
            err_resp.errors = ["Insufficient buying power"]
            err_resp.buying_power_effect = MagicMock()
            mock_acct.place_complex_order.side_effect = None
            mock_acct.place_complex_order.return_value = err_resp
            with self.assertRaises(PreflightRejectedError):
                mgr.run_broker_preflight(mock_acct, mock_sess, bundle)
            self.assertFalse(bundle["broker_preflight_verified"])

    # ------------------------------------------------------------------
    # TEST 11: Split-Tranche Lifecycle Through Leg 1 Exit, Runner & 15:40 Flatten
    # ------------------------------------------------------------------
    def test_11_split_tranche_protective_lifecycle(self):
        bundle = build_v6_split_tranche_orders(
            ticker="FISV",
            shares=2373,
            limit_price=63.19,
            stop_price=62.78,
            leg1_target=63.48,
            leg2_target=63.73,
            account_id="5WJ26897",
        )
        self.assertEqual(bundle["tranche1_shares"], 1186)
        self.assertEqual(bundle["tranche2_shares"], 1187)
        self.assertEqual(bundle["tranche1_shares"] + bundle["tranche2_shares"], 2373)

        with tempfile.TemporaryDirectory() as tmpdir:
            journal = ExecutionJournal(os.path.join(tmpdir, "journal.json"))
            mgr = OrderLifecycleManager(journal=journal)
            mock_acct = MagicMock()
            mock_sess = MagicMock()

            ok_resp = MagicMock()
            ok_resp.errors = []
            ok_resp.buying_power_effect = MagicMock()
            ok_resp.complex_order.id = "ORD_100"
            mock_acct.place_complex_order.return_value = ok_resp

            pf = mgr.run_broker_preflight(mock_acct, mock_sess, bundle)
            self.assertTrue(pf["broker_preflight_verified"])
            self.assertEqual(bundle["validation_mode"], ValidationMode.BROKER_PREFLIGHT_VERIFIED.value)

            state = mgr.submit_live_split_tranche_order(
                mock_acct, mock_sess, "sig_fisv_1", "intent_fisv_1", bundle
            )
            self.assertEqual(state["lifecycle_state"], LifecycleState.PROTECTED.value)

            state = mgr.handle_entry_fill_event(
                mock_acct, mock_sess, "intent_fisv_1", 1186, 1187, protective_stops_active=True
            )
            self.assertEqual(state["filled_tranche1_shares"], 1186)
            self.assertEqual(state["filled_tranche2_shares"], 1187)

            state = mgr.handle_leg1_exit_and_amend_tranche_b_to_breakeven(
                mock_acct, mock_sess, "intent_fisv_1", filled_leg1_exit_shares=1186
            )
            self.assertEqual(state["lifecycle_state"], LifecycleState.PARTIALLY_EXITED.value)
            self.assertAlmostEqual(state["active_stop_price"], 63.19, places=2)

            state = mgr.amend_tranche_b_runner_stop(
                mock_acct,
                mock_sess,
                "intent_fisv_1",
                new_runner_stop=63.55,
                new_stretch_target=64.20,
                current_market_price=63.80,
            )
            self.assertAlmostEqual(state["active_stop_price"], 63.55, places=2)
            self.assertAlmostEqual(state["leg2_target"], 64.20, places=2)

            state = mgr.flatten_remaining_at_cutoff(mock_acct, mock_sess, "intent_fisv_1")
            self.assertEqual(state["lifecycle_state"], LifecycleState.CLOSED.value)
            self.assertEqual(state["exited_tranche2_shares"], 1187)

    # ------------------------------------------------------------------
    # TEST 12: Blocked Execution for ARMED_FLUSHING, TRADED_COMPLETED, Stale & Out-of-Session
    # ------------------------------------------------------------------
    def test_12_armed_historical_stale_and_out_of_session_blocked(self):
        d1 = make_synthetic_daily_df(65, date(2026, 11, 9))
        d5, spy5 = make_synthetic_5m_dataset(12, date(2026, 11, 9))
        cfg = StrategyConfig(er_unknown_policy="allow")
        oracle = EarningsOracle(unknown_policy="allow")

        ts_0955 = pd.Timestamp(datetime(2026, 11, 9, 9, 55, tzinfo=NY_TZ))
        res_armed = evaluate_session_causal(
            "FISV", date(2026, 11, 9), d1, d5, spy5, cfg=cfg, earnings_oracle=oracle, as_of_ts=ts_0955
        )
        self.assertEqual(res_armed.status, "ARMED_FLUSHING")
        self.assertFalse(res_armed.is_executable_now)

        ts_1550 = pd.Timestamp(datetime(2026, 11, 9, 15, 50, tzinfo=NY_TZ))
        res_done = evaluate_session_causal(
            "FISV", date(2026, 11, 9), d1, d5, spy5, cfg=cfg, earnings_oracle=oracle, as_of_ts=ts_1550
        )
        self.assertEqual(res_done.status, "TRADED_COMPLETED")
        self.assertFalse(res_done.is_executable_now)

        exec_list = render_trader_hud([res_armed, res_done], mode="monitor")
        self.assertEqual(len(exec_list), 0)

        q = QuoteSnapshot("FISV", 63.18, 63.20, 63.19, "2026-11-09 10:01:00 EST", is_live_rth=True)
        for bad_status in ("ARMED_FLUSHING", "TRADED_COMPLETED", "IN_POSITION"):
            with self.assertRaises(StaleSignalError):
                revalidate_before_submission(
                    signal_bar_end_ts=datetime(2026, 11, 9, 10, 0, tzinfo=NY_TZ),
                    now_et=datetime(2026, 11, 9, 10, 1, tzinfo=NY_TZ),
                    entry_limit_price=63.19,
                    quote=q,
                    spy_state="HEALTHY",
                    accum_ok=True,
                    er_blocked=False,
                    signal_status=bad_status,
                )

        # Out-of-window submission (e.g., 13:30 ET after 13:00 ET entry cutoff) must fail
        with self.assertRaises(StaleSignalError):
            revalidate_before_submission(
                signal_bar_end_ts=datetime(2026, 11, 9, 13, 30, tzinfo=NY_TZ),
                now_et=datetime(2026, 11, 9, 13, 31, tzinfo=NY_TZ),
                entry_limit_price=63.19,
                quote=q,
                spy_state="HEALTHY",
                accum_ok=True,
                er_blocked=False,
                signal_status="ENTRY_SIGNAL_NOW",
            )

        # Stale signal (> 360 seconds after bar end) must fail
        with self.assertRaises(StaleSignalError):
            revalidate_before_submission(
                signal_bar_end_ts=datetime(2026, 11, 9, 10, 0, tzinfo=NY_TZ),
                now_et=datetime(2026, 11, 9, 10, 10, tzinfo=NY_TZ),
                entry_limit_price=63.19,
                quote=q,
                spy_state="HEALTHY",
                accum_ok=True,
                er_blocked=False,
                signal_status="ENTRY_SIGNAL_NOW",
            )

    # ------------------------------------------------------------------
    # TEST 13: Idempotency Duplicate Prevention & Startup Reconciliation
    # ------------------------------------------------------------------
    def test_13_idempotency_and_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            journal = ExecutionJournal(os.path.join(tmpdir, "journal.json"))
            journal.register_order_intent(
                signal_id="sig_abc",
                order_intent_id="intent_abc",
                symbol="FISV",
                total_shares=2000,
                tranche1_shares=1000,
                tranche2_shares=1000,
                limit_price=63.00,
                stop_price=62.60,
                leg1_target=63.30,
                leg2_target=63.55,
            )
            with self.assertRaises(OrderValidationError):
                journal.register_order_intent(
                    signal_id="sig_abc",
                    order_intent_id="intent_xyz",
                    symbol="FISV",
                    total_shares=2000,
                    tranche1_shares=1000,
                    tranche2_shares=1000,
                    limit_price=63.00,
                    stop_price=62.60,
                    leg1_target=63.30,
                    leg2_target=63.55,
                )

            journal.update_intent_state(
                "intent_abc",
                filled_tranche1_shares=1000,
                filled_tranche2_shares=1000,
                lifecycle_state=LifecycleState.PROTECTED.value,
            )
            mgr = OrderLifecycleManager(journal=journal)

            rec_ok = mgr.reconcile_account_state(
                broker_positions=[
                    {"symbol": "AAPL", "quantity": 500},
                    {"symbol": "FISV", "quantity": 2000},
                ],
                broker_working_orders=[
                    {"symbol": "FISV", "order_type": "STOP", "action": "SELL_TO_CLOSE", "quantity": 1000},
                    {"symbol": "FISV", "order_type": "STOP", "action": "SELL_TO_CLOSE", "quantity": 1000},
                ],
            )
            self.assertEqual(rec_ok["status"], "RECONCILED_OK")
            self.assertEqual(rec_ok["pre_existing_holdings"], [{"symbol": "AAPL", "quantity": 500}])

            with self.assertRaises(ReconciliationRequiredError):
                mgr.reconcile_account_state(
                    broker_positions=[{"symbol": "FISV", "quantity": 2000}],
                    broker_working_orders=[
                        {"symbol": "FISV", "order_type": "STOP", "action": "SELL_TO_CLOSE", "quantity": 1000},
                    ],
                )

    # ------------------------------------------------------------------
    # TEST 14: Validation Enforcement Under `python -O` & Environment Scope
    # ------------------------------------------------------------------
    def test_14_validation_exceptions_and_session_environment_scope(self):
        with self.assertRaises(OrderValidationError):
            validate_order_safety("FISV", 1000, 63.0, 62.6, "WRONG_ACCT")
        with self.assertRaises(OrderValidationError):
            validate_order_safety("FISV", 2500, 63.0, 62.6, "5WJ26897")
        with self.assertRaises(OrderValidationError):
            validate_order_safety("NU", 3500, 25.0, 24.85, "5WJ26897")
        with self.assertRaises(OrderValidationError):
            validate_order_safety("FISV", 1000, 63.0, 63.10, "5WJ26897")

        with self.assertRaises(SessionEnvironmentMismatchError):
            validate_session_cache_metadata(
                {"account_id": "5WJ26897", "environment": "SANDBOX"},
                expected_account_id="5WJ26897",
                expected_environment="PRODUCTION",
            )

        snap_loss = AccountRiskSnapshot(
            account_id="5WJ26897",
            environment="PRODUCTION",
            available_buying_power_usd=200_000.0,
            aggregate_strategy_exposure_usd=0.0,
            symbol_strategy_exposure_usd={},
            daily_realized_loss_usd=2_600.0,
            daily_realized_pnl_usd=-2_600.0,
            open_strategy_positions_count=0,
        )
        with self.assertRaises(RiskLimitExceededError):
            validate_account_risk_limits("FISV", 100_000.0, 500.0, snap_loss)

    # ------------------------------------------------------------------
    # TEST 15: Scanner Zero-Survivors, Off-Hours Spread, CLI Conflicts & Early-Close Calendar
    # ------------------------------------------------------------------
    def test_15_scanner_zero_survivors_off_hours_cli_and_early_close_calendar(self):
        empty_df = rank_and_filter_candidates([])
        self.assertTrue(empty_df.empty)
        self.assertEqual(list(empty_df.columns), EXPECTED_SCANNER_COLUMNS)

        ok_off, _, status_off, _ = evaluate_quote_spread(None, None, 63.0, is_rth=False)
        self.assertTrue(ok_off)
        self.assertEqual(status_off, "UNVERIFIED_OFF_HOURS")

        ok_rth, _, status_rth, _ = evaluate_quote_spread(None, None, 63.0, is_rth=True)
        self.assertFalse(ok_rth)
        self.assertEqual(status_rth, "MISSING_RTH_QUOTE")

        with self.assertRaises(ModeConflictError):
            parse_cli_args(["--dry-run", "--execute-live"])
        with self.assertRaises(ModeConflictError):
            parse_cli_args(["--mode", "replay", "--execute-live"])

        # Holiday & Early-Close NYSE Calendar verification
        cal = NYSECalendar()
        thanksgiving = cal.get_session_schedule(date(2026, 11, 26))
        self.assertFalse(thanksgiving.is_trading_day)

        black_friday = cal.get_session_schedule(date(2026, 11, 27))
        self.assertTrue(black_friday.is_trading_day)
        self.assertTrue(black_friday.is_early_close)
        self.assertEqual(black_friday.mandatory_flatten_et.hour, 12)
        self.assertEqual(black_friday.mandatory_flatten_et.minute, 40)


if __name__ == "__main__":
    unittest.main()
