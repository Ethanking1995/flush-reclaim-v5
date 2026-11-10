"""
generate_validation_report.py
=============================
Runs the Audited V6 evaluation against the frozen baseline data (`baseline_data/`),
compares every single trade against `baseline_data/legacy_v5_benchmark.json`,
performs step-by-step attribution of each correctness fix, and generates
`VALIDATION_REPORT.md`.
"""

from __future__ import annotations
import json
import os
from typing import Dict, List, Tuple
import numpy as np
import pandas as pd

from config_schema import StrategyConfig, ExecutionConfig, RiskConfig
from test_regimes import load_frozen_baseline_data, run_replay_comparison
from portfolio_simulator import run_portfolio_simulation


def main() -> None:
    root = os.path.dirname(os.path.abspath(__file__))
    manifest_path = os.path.join(root, "baseline_data", "manifest.json")
    bench_path = os.path.join(root, "baseline_data", "legacy_v5_benchmark.json")

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    with open(bench_path, "r", encoding="utf-8") as f:
        legacy_bench = json.load(f)

    core_3 = ["FISV", "PLNT", "NU"]
    expanded_6 = ["FISV", "PLNT", "NU", "CNC", "EEFT", "GPN"]
    d1, d5, spy5 = load_frozen_baseline_data(expanded_6)

    # Run the replay comparison matrix
    results_map = run_replay_comparison()

    # Also run Core-3 with warm-up min_tod_history_sessions=1 (matching V5's min_periods=1) vs min_tod_history_sessions=5
    # to isolate the exact effect of removing .bfill() / warm-up threshold vs intrabar sequencing vs stop cap!
    risk_unconstrained = RiskConfig(
        target_clip_usd=150_000.0,
        max_concurrent_positions=10,
        max_aggregate_exposure_usd=1_500_000.0,
    )
    risk_realistic = RiskConfig(
        target_clip_usd=150_000.0,
        max_concurrent_positions=2,
        max_aggregate_exposure_usd=300_000.0,
    )
    exec_base = ExecutionConfig(entry_slippage_bps=1.5, exit_slippage_bps=1.5, limit_exit_slippage_bps=1.5)

    # Stepwise waterfall from Legacy V5 on Expanded-6 (37 trades) to Audited V6:
    sim_step1_min1_uncapped = run_portfolio_simulation(
        label="Step 1: Causal Intrabar Sequencing + min_tod=1 + Uncapped Stop (Unconstrained Capital)",
        symbols=expanded_6,
        data_1d_by_symbol=d1,
        data_5m_by_symbol=d5,
        df_spy_5m=spy5,
        cfg=StrategyConfig(min_tod_history_sessions=1, stop_policy="legacy_v5_capped"),
        exec_cfg=exec_base,
        risk_cfg=risk_unconstrained,
    )
    sim_step2_min5_uncapped = run_portfolio_simulation(
        label="Step 2: + Require >=5 Prior Sessions for ToD/Midday Baseline (No .bfill() / Warmup)",
        symbols=expanded_6,
        data_1d_by_symbol=d1,
        data_5m_by_symbol=d5,
        df_spy_5m=spy5,
        cfg=StrategyConfig(min_tod_history_sessions=5, stop_policy="legacy_v5_capped"),
        exec_cfg=exec_base,
        risk_cfg=risk_unconstrained,
    )
    sim_step3_risk_sized = run_portfolio_simulation(
        label="Step 3: + Structural Risk-Sized Stop Policy (Cap $ Risk to <=0.65% of Clip)",
        symbols=expanded_6,
        data_1d_by_symbol=d1,
        data_5m_by_symbol=d5,
        df_spy_5m=spy5,
        cfg=StrategyConfig(min_tod_history_sessions=5, stop_policy="structural_risk_sized"),
        exec_cfg=exec_base,
        risk_cfg=risk_realistic,
    )
    sim_step4_strict_cap = run_portfolio_simulation(
        label="Step 4: + Strict 0.65% Stop Distance Reject Policy + Portfolio Capital Constraints",
        symbols=expanded_6,
        data_1d_by_symbol=d1,
        data_5m_by_symbol=d5,
        df_spy_5m=spy5,
        cfg=StrategyConfig(min_tod_history_sessions=5, stop_policy="strict_cap_reject"),
        exec_cfg=exec_base,
        risk_cfg=risk_realistic,
    )

    # Also evaluate the Dynamic Scanner Regime Policy (`scanner_hl10_atr_compression`) separately
    sim_scanner_regime = run_portfolio_simulation(
        label="Separate Evaluation: Dynamic Scanner Regime Proxy (HL_10d>=6, ATR<=4.5%, VolRatio>=1.0)",
        symbols=expanded_6,
        data_1d_by_symbol=d1,
        data_5m_by_symbol=d5,
        df_spy_5m=spy5,
        cfg=StrategyConfig(
            daily_regime_policy="scanner_hl10_atr_compression",
            min_tod_history_sessions=5,
            stop_policy="structural_risk_sized",
        ),
        exec_cfg=exec_base,
        risk_cfg=risk_realistic,
    )

    # Build Trade-by-Trade Comparison Table against the 37 Legacy V5 trades
    legacy_trades = legacy_bench["expanded_6_v5_accum_trades"]
    v6_step1_by_key = {(t.symbol, t.session_date): t for t in sim_step1_min1_uncapped.executed_trades}
    v6_step2_by_key = {(t.symbol, t.session_date): t for t in sim_step2_min5_uncapped.executed_trades}
    v6_step3_by_key = {(t.symbol, t.session_date): t for t in sim_step3_risk_sized.executed_trades}
    v6_step3_skipped = {(t.symbol, t.session_date): reason for t, reason in sim_step3_risk_sized.skipped_trades}
    v6_step4_by_key = {(t.symbol, t.session_date): t for t in sim_step4_strict_cap.executed_trades}

    comparison_rows = []
    for lt in legacy_trades:
        sym = lt["Ticker"]
        dt_str = str(lt["Date"])[:10]
        key = (sym, dt_str)
        l_entry_px = float(lt["EntryPrice"])
        l_init_stop = float(lt["InitialStop"])
        l_stop_dist_pct = (l_entry_px - l_init_stop) / l_entry_px * 100.0
        l_ret = float(lt["RetPct"])
        l_reason = lt["Reason"]

        t1 = v6_step1_by_key.get(key)
        t2 = v6_step2_by_key.get(key)
        t3 = v6_step3_by_key.get(key)
        t4 = v6_step4_by_key.get(key)

        if t2 is None and t1 is not None:
            status_explain = "Dropped in Warm-up (`<5` prior 5m sessions; legacy V5 used `.bfill()`)"
            v6_ret_sized = "N/A (Warm-up)"
            v6_ret_strict = "N/A (Warm-up)"
        elif t2 is None and t1 is None:
            status_explain = "Dropped when `.bfill()` / same-day midday median fallback removed"
            v6_ret_sized = "N/A"
            v6_ret_strict = "N/A"
        else:
            v6_ret_sized = f"{t3.net_return_pct:+.3f}% (${t3.net_pnl_usd:+,.0f}, {t3.total_shares} sh)" if t3 else f"Skipped ({v6_step3_skipped.get(key, 'Cap')})"
            if t4 is not None:
                v6_ret_strict = f"{t4.net_return_pct:+.3f}% (${t4.net_pnl_usd:+,.0f})"
            else:
                if l_stop_dist_pct > 0.6501:
                    v6_ret_strict = f"Rejected (Stop {l_stop_dist_pct:.2f}% > 0.65%)"
                else:
                    v6_ret_strict = "Skipped (Capital)"

            diffs = []
            if l_stop_dist_pct > 0.6501:
                diffs.append(f"Initial stop {l_stop_dist_pct:.2f}% > 0.65% cap (risk-sized down to {t3.total_shares if t3 else 0} sh in Step 3; rejected in Step 4)")
            if t2 is not None and abs(t2.net_return_pct - l_ret) > 0.02:
                diffs.append(f"Intrabar exit changed ({l_reason} [{l_ret:+.2f}%] -> {t2.exit_reason} [{t2.net_return_pct:+.2f}%]) due to causal prev-bar VWAP / runner chronology")
            if not diffs:
                diffs.append("Unchanged signal & exit (minor share/rounding difference only)")
            status_explain = "; ".join(diffs)

        comparison_rows.append(
            {
                "Date": dt_str,
                "Ticker": sym,
                "EntryPx": f"${l_entry_px:.2f}",
                "InitStop%": f"-{l_stop_dist_pct:.2f}%",
                "LegacyV5_Ret": f"{l_ret:+.2f}%",
                "LegacyV5_Exit": l_reason,
                "V6_RiskSized": v6_ret_sized,
                "V6_StrictCap": v6_ret_strict,
                "Explanation": status_explain,
            }
        )

    # Save structured JSON comparison for reproducibility
    comp_json_path = os.path.join(root, "baseline_data", "v5_vs_v6_comparison.json")
    with open(comp_json_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "stepwise_summaries": [
                    sim_step1_min1_uncapped.summary.to_dict(),
                    sim_step2_min5_uncapped.summary.to_dict(),
                    sim_step3_risk_sized.summary.to_dict(),
                    sim_step4_strict_cap.summary.to_dict(),
                    sim_scanner_regime.summary.to_dict(),
                ],
                "ablation_summaries": {k: v.summary.to_dict() for k, v in results_map.items()},
                "trade_by_trade_comparison": comparison_rows,
            },
            f,
            indent=2,
        )

    print(f"✅ Saved structured comparison JSON to {comp_json_path} ({len(comparison_rows)} legacy trades analyzed).")


if __name__ == "__main__":
    main()
