"""
test_regimes.py
===============
Audited V6 Intraday Flush-Reclaim Evaluation CLI, Replay Runner, and Live HUD.

Consumes the single shared causal engine (`strategy_engine.py`), account-level
portfolio simulator (`portfolio_simulator.py`), unified earnings oracle (`earnings_oracle.py`),
NYSE market calendar (`market_calendar.py`), and audited broker adapter (`tasty_executor.py`).
"""

from __future__ import annotations
import argparse
from datetime import datetime, date
import gzip
import json
import os
import sys
import time
from typing import Dict, List, Optional, Tuple
import pandas as pd
import yfinance as yf

from config_schema import (
    StrategyConfig,
    ExecutionConfig,
    RiskConfig,
    ModeConflictError,
    OrderValidationError,
    PreflightRejectedError,
    StaleSignalError,
)
from market_calendar import NYSECalendar, NY_TZ, normalize_to_ny_tz
from earnings_oracle import EarningsOracle
from strategy_engine import (
    SessionEvaluationResult,
    evaluate_session_causal,
)
from portfolio_simulator import (
    PortfolioSimulationResult,
    run_portfolio_simulation,
)
from tasty_executor import (
    QuoteSnapshot,
    AccountRiskSnapshot,
    OrderLifecycleManager,
    ExecutionJournal,
    build_v6_split_tranche_orders,
    get_tasty_session,
    revalidate_before_submission,
    validate_account_risk_limits,
)
from universe_scanner import (
    CORE_MONITORED_TICKERS,
    run_autonomous_discovery_funnel,
    load_discovered_universe,
)


# ==============================================================================
# 1. UNAMBIGUOUS CLI ARGUMENT PARSER & MODE VALIDATION
# ==============================================================================

VALID_MODES = ("replay", "scan", "monitor", "paper", "broker-preview", "execute-live")


def parse_cli_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Flush-Reclaim Audited V6 Engine (Replay, Scanner, Monitor HUD, Paper, Broker Preview, Live Execution)"
    )
    parser.add_argument(
        "--mode",
        choices=VALID_MODES,
        default=None,
        help="Explicit operating mode: replay | scan | monitor | paper | broker-preview | execute-live",
    )
    parser.add_argument("--watch", "--monitor", dest="watch", action="store_true", help="Continuous read-only HUD monitor loop")
    parser.add_argument("--scan-universe", action="store_true", help="Run dynamic universe discovery funnel")
    parser.add_argument("--dynamic-universe", action="store_true", help="Include order-eligible tickers from discovered_candidates.json")
    parser.add_argument("--dry-run", action="store_true", help="Broker preview / preflight mode only (no live transmission)")
    parser.add_argument("--execute-live", action="store_true", help="Enable human-confirmed live order transmission")
    parser.add_argument("--clip", type=float, default=150_000.0, help="Target dollar clip per trade (default: 150000)")
    parser.add_argument("--interval", type=int, default=60, help="Refresh interval in seconds for monitor mode")
    parser.add_argument(
        "--stop-policy",
        choices=("strict_cap_reject", "structural_risk_sized", "legacy_v5_capped"),
        default="strict_cap_reject",
        help="Stop placement & sizing policy when structural stop exceeds 0.65%%",
    )
    parser.add_argument("--baseline-dir", type=str, default=None, help="Directory containing frozen baseline_data")
    parser.add_argument("--live-data", action="store_true", help="Fetch live Yahoo Finance data instead of frozen baseline_data in replay mode")

    args = parser.parse_args(argv)

    # Detect conflicting flags immediately and fail with ModeConflictError
    if args.dry_run and args.execute_live:
        raise ModeConflictError(
            "CONFLICTING CLI FLAGS: Cannot specify both `--dry-run` and `--execute-live`."
        )

    if args.scan_universe and (args.dry_run or args.execute_live):
        raise ModeConflictError(
            "CONFLICTING CLI FLAGS: `--scan-universe` cannot be combined with `--dry-run` or `--execute-live`."
        )

    if args.mode is not None:
        if args.mode == "execute-live" and args.dry_run:
            raise ModeConflictError("CONFLICTING CLI FLAGS: `--mode execute-live` conflicts with `--dry-run`.")
        if args.mode == "broker-preview" and args.execute_live:
            raise ModeConflictError("CONFLICTING CLI FLAGS: `--mode broker-preview` conflicts with `--execute-live`.")
        if args.mode in ("replay", "scan", "monitor", "paper") and (args.dry_run or args.execute_live):
            raise ModeConflictError(
                f"CONFLICTING CLI FLAGS: `--mode {args.mode}` cannot be combined with broker execution flags."
            )
    else:
        # Infer unambiguous mode when `--mode` is omitted
        if args.scan_universe:
            args.mode = "scan"
        elif args.execute_live:
            args.mode = "execute-live"
        elif args.dry_run:
            args.mode = "broker-preview"
        elif args.watch:
            args.mode = "monitor"
        else:
            args.mode = "replay"

    return args


# ==============================================================================
# 2. FROZEN BASELINE & LIVE MARKET DATA LOADERS
# ==============================================================================

def load_frozen_baseline_data(
    symbols: List[str],
    baseline_dir: Optional[str] = None,
) -> Tuple[Dict[str, pd.DataFrame], Dict[str, pd.DataFrame], pd.DataFrame]:
    root = baseline_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "baseline_data", "raw_ohlcv")
    data_1d: Dict[str, pd.DataFrame] = {}
    data_5m: Dict[str, pd.DataFrame] = {}

    all_syms = list(dict.fromkeys([s.upper() for s in symbols] + ["SPY"]))
    for sym in all_syms:
        p1 = os.path.join(root, f"{sym}_1d.csv.gz")
        p5 = os.path.join(root, f"{sym}_5m.csv.gz")
        if os.path.exists(p1):
            df1 = pd.read_csv(p1, index_col=0, parse_dates=True)
            data_1d[sym] = normalize_to_ny_tz(df1)
        if os.path.exists(p5):
            df5 = pd.read_csv(p5, index_col=0, parse_dates=True)
            data_5m[sym] = normalize_to_ny_tz(df5)

    spy_5m = data_5m.get("SPY", pd.DataFrame())
    return data_1d, data_5m, spy_5m


def fetch_live_market_data(
    symbols: List[str],
) -> Tuple[Dict[str, pd.DataFrame], Dict[str, pd.DataFrame], pd.DataFrame]:
    data_1d: Dict[str, pd.DataFrame] = {}
    data_5m: Dict[str, pd.DataFrame] = {}
    all_syms = list(dict.fromkeys([s.upper() for s in symbols] + ["SPY"]))
    for sym in all_syms:
        t = yf.Ticker(sym)
        d1 = t.history(period="1y", interval="1d")
        d5 = t.history(period="60d", interval="5m")
        if not d1.empty:
            data_1d[sym] = normalize_to_ny_tz(d1)
        if not d5.empty:
            data_5m[sym] = normalize_to_ny_tz(d5)
    return data_1d, data_5m, data_5m.get("SPY", pd.DataFrame())


# ==============================================================================
# 3. CAUSAL TRADER HUD (SHARED ENGINE, NO OVERCLAIMS, STRICT EXECUTION GATING)
# ==============================================================================

def evaluate_hud_universe(
    symbols: List[str],
    data_1d: Dict[str, pd.DataFrame],
    data_5m: Dict[str, pd.DataFrame],
    spy_5m: pd.DataFrame,
    cfg: StrategyConfig,
    exec_cfg: ExecutionConfig,
    risk_cfg: RiskConfig,
    as_of_ts: Optional[pd.Timestamp] = None,
    clip_usd: float = 150_000.0,
) -> List[SessionEvaluationResult]:
    cal = NYSECalendar()
    oracle = EarningsOracle(
        blackout_days=cfg.er_blackout_calendar_days,
        unknown_policy=cfg.er_unknown_policy,
    )
    results: List[SessionEvaluationResult] = []
    for sym in symbols:
        s_up = sym.upper()
        d1 = data_1d.get(s_up, pd.DataFrame())
        d5 = data_5m.get(s_up, pd.DataFrame())
        if d5.empty:
            continue
        if as_of_ts is not None:
            target_date = as_of_ts.astimezone(NY_TZ).date()
        else:
            target_date = d5.index[-1].date()

        res = evaluate_session_causal(
            symbol=s_up,
            session_date=target_date,
            df_1d=d1,
            df_5m=d5,
            df_spy_5m=spy_5m,
            cfg=cfg,
            exec_cfg=exec_cfg,
            risk_cfg=risk_cfg,
            earnings_oracle=oracle,
            calendar=cal,
            as_of_ts=as_of_ts,
            clip_override_usd=clip_usd,
        )
        results.append(res)
    return results


def render_trader_hud(
    results: List[SessionEvaluationResult],
    mode: str = "monitor",
    clip_usd: float = 150_000.0,
) -> List[SessionEvaluationResult]:
    """
    Render the Audited V6 Trader HUD from `SessionEvaluationResult` objects produced
    by `strategy_engine.evaluate_session_causal`.
    Returns ONLY the results where `res.is_executable_now == True` (`ENTRY_SIGNAL_NOW`).
    `ARMED_FLUSHING` and `TRADED_COMPLETED` are NEVER returned as executable!
    """
    now_str = datetime.now(NY_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    print("\n" + "╔" + "═" * 124 + "╗")
    print(
        f"║ 📊 AUDITED V6 FLUSH-RECLAIM HUD | Mode: {mode.upper():<14} | Clip: ${clip_usd:>9,.0f} | As-Of: {now_str:<23} ║"
    )
    print("╠" + "═" * 124 + "╣")

    if results:
        spy_r = results[0]
        spy_str = (
            f"{spy_r.spy_state} (3-Bar: {(spy_r.spy_3bar_ret or 0)*100:+.2f}%, VWAP Dist: {(spy_r.spy_vwap_dist or 0)*100:+.2f}%)"
            if spy_r.spy_3bar_ret is not None
            else f"{spy_r.spy_state} (Missing/Stale SPY blocks new entries)"
        )
        print(f"║ 🌐 SPY INTRADAY FILTER : {spy_str:<98} ║")
        print("╠" + "═" * 124 + "╣")

    print(
        f"║ {'TICKER':<6} │ {'LAST':>8} │ {'VWAP':>8} │ {'PRIOR ACCUM':<13} │ {'NEXT-DAY DEV':<13} │ {'14D ER FILTER':<24} │ {'FLUSH%':>7} │ {'CLIMAX':>6} │ {'STATUS':<18} ║"
    )
    print("╟" + "─" * 124 + "╢")

    executable_now: List[SessionEvaluationResult] = []
    for r in results:
        px_s = f"${r.current_price:.2f}" if r.current_price is not None else "N/A"
        vw_s = f"${r.current_vwap:.2f}" if r.current_vwap is not None else "N/A"
        p_acc = "YES (Prior)" if r.prior_session_accum_ok else "NO (Prior)"
        n_acc = "DEV-YES" if r.developing_next_session_accum else "DEV-NO"
        er_lbl = r.earnings_check.display_label[:24]
        fl_s = f"{(r.max_flush_drop_pct or 0)*100:.2f}%" if r.max_flush_drop_pct is not None else "N/A"
        cx_s = f"{r.climax_vol_ratio:.1f}x" if r.climax_vol_ratio is not None else "N/A"
        print(
            f"║ {r.symbol:<6} │ {px_s:>8} │ {vw_s:>8} │ {p_acc:<13} │ {n_acc:<13} │ {er_lbl:<24} │ {fl_s:>7} │ {cx_s:>6} │ {r.status:<18} ║"
        )
        if r.is_executable_now and r.status == "ENTRY_SIGNAL_NOW" and r.active_signal_plan is not None:
            executable_now.append(r)

    print("╚" + "═" * 124 + "╝")

    # Print detailed order plans ONLY for active signals or open positions
    for r in results:
        plan = r.active_signal_plan or (r.open_position.order_plan if r.open_position else None)
        if plan is not None:
            print(f"\n┌─── 📌 {r.symbol} ORDER PLAN [{r.status}] (Signal ID: {plan.signal_id}) " + "─" * 55)
            print(f"│ 🧠 WHY DIAGNOSIS : {plan.why_diagnosis}")
            print(
                f"│ 💼 SPLIT-TRANCHE IRA ORDER PLAN (${plan.notional_usd:,.2f} Notional = {plan.total_shares:,} Shares = "
                f"Tranche A {plan.tranche1_shares:,} sh + Tranche B {plan.tranche2_shares:,} sh):"
            )
            print(
                f"│    1. ENTRY (LIMIT)        : Buy {plan.total_shares:,} sh @ ${plan.entry_price:.2f} "
                f"(Tranche A: {plan.tranche1_shares:,} sh OTOCO | Tranche B: {plan.tranche2_shares:,} sh OTOCO)"
            )
            print(
                f"│    2. PLANNED INITIAL STOP : Stop @ ${plan.stop_price:.2f} [-{plan.stop_distance_pct*100:.2f}% / "
                f"-${plan.planned_risk_usd:,.2f} planned risk; gap/slippage can exceed planned stop]"
            )
            print(
                f"│    3. TRANCHE A LEG 1 (50%): Limit Sell {plan.tranche1_shares:,} sh @ ${plan.leg1_initial_target:.2f} "
                f"(prior-bar VWAP / +0.40% min) -> Tranche B ({plan.tranche2_shares:,} sh) stays protected & moves to BE"
            )
            print(
                f"│    4. TRANCHE B LEG 2 (50%): Limit Sell {plan.tranche2_shares:,} sh @ ${plan.leg2_base_target:.2f} (+0.85%) "
                f"OR if bar closes >= +0.75% & > EMA12, trail 5m EMA20 to ${plan.leg2_stretch_target:.2f} (+1.60%) | Flatten 15:40 ET"
            )
            print("└" + "─" * 124)

    return executable_now


# ==============================================================================
# 4. REPLAY & ABLATION COMPARISON RUNNER
# ==============================================================================

def run_replay_comparison(
    baseline_dir: Optional[str] = None,
    use_live_data: bool = False,
    clip_usd: float = 150_000.0,
) -> Dict[str, PortfolioSimulationResult]:
    core_3 = ["FISV", "PLNT", "NU"]
    expanded_6 = ["FISV", "PLNT", "NU", "CNC", "EEFT", "GPN"]

    if use_live_data:
        d1, d5, spy5 = fetch_live_market_data(expanded_6)
    else:
        d1, d5, spy5 = load_frozen_baseline_data(expanded_6, baseline_dir)

    risk_cfg = RiskConfig(target_clip_usd=clip_usd)
    exec_base = ExecutionConfig(entry_slippage_bps=1.5, exit_slippage_bps=1.5, entry_delay_bars=0)
    exec_high_slip = ExecutionConfig(entry_slippage_bps=5.0, exit_slippage_bps=5.0, entry_delay_bars=0)
    exec_delayed = ExecutionConfig(entry_slippage_bps=1.5, exit_slippage_bps=1.5, entry_delay_bars=1)

    configs = [
        ("1. V6 Audited [No Filters (Raw Signal)]", core_3, StrategyConfig(use_accum_regime_filter=False, use_er_blackout_filter=False, use_spy_waterfall_filter=False, stop_policy="strict_cap_reject"), exec_base),
        ("2. V6 Audited [+Accum Regime Only]", core_3, StrategyConfig(use_accum_regime_filter=True, use_er_blackout_filter=False, use_spy_waterfall_filter=False, stop_policy="strict_cap_reject"), exec_base),
        ("3. V6 Audited [+Accum +14d ER Filter]", core_3, StrategyConfig(use_accum_regime_filter=True, use_er_blackout_filter=True, use_spy_waterfall_filter=False, stop_policy="strict_cap_reject"), exec_base),
        ("4. V6 Audited Core-3 [All Filters + Strict 0.65% Stop Cap]", core_3, StrategyConfig(use_accum_regime_filter=True, use_er_blackout_filter=True, use_spy_waterfall_filter=True, stop_policy="strict_cap_reject"), exec_base),
        ("5. V6 Audited Core-3 [All Filters + Structural Risk-Sized Stop]", core_3, StrategyConfig(use_accum_regime_filter=True, use_er_blackout_filter=True, use_spy_waterfall_filter=True, stop_policy="structural_risk_sized"), exec_base),
        ("6. V6 Audited Core-3 [All Filters + Legacy V5 Uncapped Stop]", core_3, StrategyConfig(use_accum_regime_filter=True, use_er_blackout_filter=True, use_spy_waterfall_filter=True, stop_policy="legacy_v5_capped"), exec_base),
        ("7. V6 Audited Core-3 [Sensitivity: 5.0 bps Slippage/Side]", core_3, StrategyConfig(use_accum_regime_filter=True, use_er_blackout_filter=True, use_spy_waterfall_filter=True, stop_policy="strict_cap_reject"), exec_high_slip),
        ("8. V6 Audited Core-3 [Sensitivity: +1 Bar Open Entry Delay]", core_3, StrategyConfig(use_accum_regime_filter=True, use_er_blackout_filter=True, use_spy_waterfall_filter=True, stop_policy="strict_cap_reject"), exec_delayed),
        ("9. V6 Audited Expanded-6 [All Filters + Strict 0.65% Stop Cap]", expanded_6, StrategyConfig(use_accum_regime_filter=True, use_er_blackout_filter=True, use_spy_waterfall_filter=True, stop_policy="strict_cap_reject"), exec_base),
        ("10. V6 Audited Expanded-6 [All Filters + Structural Risk-Sized]", expanded_6, StrategyConfig(use_accum_regime_filter=True, use_er_blackout_filter=True, use_spy_waterfall_filter=True, stop_policy="structural_risk_sized"), exec_base),
    ]

    results_map: Dict[str, PortfolioSimulationResult] = {}
    rows_for_display = []

    for label, syms, scfg, ecfg in configs:
        sim = run_portfolio_simulation(
            label=label,
            symbols=syms,
            data_1d_by_symbol=d1,
            data_5m_by_symbol=d5,
            df_spy_5m=spy5,
            cfg=scfg,
            exec_cfg=ecfg,
            risk_cfg=risk_cfg,
        )
        results_map[label] = sim
        s = sim.summary
        rows_for_display.append(
            {
                "Configuration": s.label,
                "Days": s.total_trading_days,
                "N_Exec": s.executed_trades_count,
                "N_Skip": s.skipped_by_capital_count,
                "Ambig": s.ambiguous_bars_affected_trades,
                "WR%": f"{s.win_rate_pct:.1f}%",
                "AvgNet%": f"{s.avg_trade_net_return_pct:+.3f}%",
                "NetPnL$": f"${s.total_net_pnl_usd:+,.0f}",
                "PortRet%": f"{s.total_portfolio_return_pct:+.2f}%",
                "MaxDD%": f"{s.max_drawdown_pct:.2f}%",
                "DailySharpe": f"{s.daily_portfolio_sharpe:.2f}",
                "LegacyTradeSharpe": f"{s.legacy_trade_level_sharpe_comparison_only:.2f}",
            }
        )

    df_disp = pd.DataFrame(rows_for_display)
    print("\n" + "=" * 145)
    print("📊 AUDITED V6 CAUSAL PORTFOLIO BACKTEST & ABLATION MATRIX (Account Equity: $300,000 | Clip: $150,000)")
    print("=" * 145)
    print(df_disp.to_string(index=False))
    print("=" * 145)
    return results_map


# ==============================================================================
# 5. MAIN ENTRYPOINT
# ==============================================================================

def main(argv: Optional[List[str]] = None) -> int:
    args = parse_cli_args(argv)
    cfg = StrategyConfig(stop_policy=args.stop_policy)
    exec_cfg = ExecutionConfig()
    risk_cfg = RiskConfig(target_clip_usd=args.clip)

    if args.mode == "scan":
        df_scan = run_autonomous_discovery_funnel(save_json=True, cfg=cfg, exec_cfg=exec_cfg)
        if df_scan.empty:
            print("No candidates passed all discovery gates.")
        else:
            print(df_scan.to_string(index=False))
        return 0

    if args.mode == "replay":
        run_replay_comparison(
            baseline_dir=args.baseline_dir,
            use_live_data=args.live_data,
            clip_usd=args.clip,
        )
        return 0

    # Monitor / Paper / Broker-Preview / Execute-Live modes
    symbols = load_discovered_universe(
        include_core_monitored=True,
        require_order_eligible=True,
    ) if args.dynamic_universe else ["FISV", "PLNT", "EEFT", "CNC", "GPN", "NU"]

    if args.live_data or args.mode in ("monitor", "paper", "broker-preview", "execute-live"):
        d1, d5, spy5 = load_live_or_fallback(symbols, args.baseline_dir)
    else:
        d1, d5, spy5 = load_frozen_baseline_data(symbols, args.baseline_dir)

    hud_results = evaluate_hud_universe(
        symbols=symbols,
        data_1d=d1,
        data_5m=d5,
        spy_5m=spy5,
        cfg=cfg,
        exec_cfg=exec_cfg,
        risk_cfg=risk_cfg,
        clip_usd=args.clip,
    )
    executable_signals = render_trader_hud(hud_results, mode=args.mode, clip_usd=args.clip)

    if args.mode in ("broker-preview", "execute-live"):
        if not executable_signals:
            print(
                f"\n🛡️  [{args.mode.upper()}] No fresh ENTRY_SIGNAL_NOW on the latest completed 5m bar. "
                f"(ARMED_FLUSHING and historical TRADED_COMPLETED sessions are blocked from execution)."
            )
            return 0

        for sig_res in executable_signals:
            plan = sig_res.active_signal_plan
            if plan is None:
                continue
            bundle = build_v6_split_tranche_orders(
                ticker=plan.symbol,
                shares=plan.total_shares,
                limit_price=plan.entry_price,
                stop_price=plan.stop_price,
                leg1_target=plan.leg1_initial_target,
                leg2_target=plan.leg2_base_target,
                account_id=risk_cfg.target_account_id,
                risk_cfg=risk_cfg,
            )
            print(
                f"\n📋 Local Schema Validation ({bundle['validation_mode']}): "
                f"Tranche A ({bundle['tranche1_shares']} sh OTOCO) + Tranche B ({bundle['tranche2_shares']} sh OTOCO) staged."
            )

    return 0


def load_live_or_fallback(
    symbols: List[str],
    baseline_dir: Optional[str] = None,
) -> Tuple[Dict[str, pd.DataFrame], Dict[str, pd.DataFrame], pd.DataFrame]:
    try:
        d1, d5, spy5 = fetch_live_market_data(symbols)
        if d5 and not spy5.empty:
            return d1, d5, spy5
    except Exception:
        pass
    return load_frozen_baseline_data(symbols, baseline_dir)


if __name__ == "__main__":
    sys.exit(main())
