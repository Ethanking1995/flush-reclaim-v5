"""
portfolio_simulator.py
======================
Account-level multi-asset portfolio simulator and ablation/sensitivity runner.
Replaces trade-level sqrt(252) Sharpe with true daily portfolio return accounting
across all NYSE trading days (including no-trade days and open-position mark-to-market),
enforcing integer shares, cash/buying-power reservation, concurrent position limits,
daily loss limits, and deterministic tie-breaking for simultaneous signals.
"""

from __future__ import annotations
from dataclasses import dataclass, asdict
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple
import math
import numpy as np
import pandas as pd

from config_schema import StrategyConfig, ExecutionConfig, RiskConfig
from market_calendar import NYSECalendar, normalize_to_ny_tz
from earnings_oracle import EarningsOracle
from strategy_engine import (
    CompletedTradeRecord,
    OpenPositionState,
    SessionEvaluationResult,
    compute_daily_regime_table,
    compute_causal_5m_features,
    compute_causal_spy_features,
    evaluate_session_causal,
)


@dataclass
class DailyPortfolioRecord:
    session_date: str
    starting_equity_usd: float
    realized_pnl_usd: float
    unrealized_pnl_change_usd: float
    total_daily_pnl_usd: float
    ending_equity_usd: float
    daily_return_pct: float
    trades_entered: int
    trades_closed: int
    peak_concurrent_positions: int
    peak_reserved_buying_power_usd: float
    skipped_signals_count: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PortfolioMetricsSummary:
    label: str
    universe_symbols: List[str]
    total_trading_days: int
    active_trade_days: int
    no_trade_days: int
    executed_trades_count: int
    skipped_by_capital_count: int
    ambiguous_bars_affected_trades: int
    win_rate_pct: float
    avg_trade_net_return_pct: float
    sum_trade_net_return_pct: float
    total_net_pnl_usd: float
    total_portfolio_return_pct: float
    profit_factor: float | str
    worst_trade_net_return_pct: float
    worst_day_portfolio_return_pct: float
    max_drawdown_pct: float
    daily_portfolio_sharpe: float
    legacy_trade_level_sharpe_comparison_only: float
    config_sha256: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PortfolioSimulationResult:
    summary: PortfolioMetricsSummary
    executed_trades: List[CompletedTradeRecord]
    skipped_trades: List[ Tuple[CompletedTradeRecord, str] ]
    daily_equity_curve: List[DailyPortfolioRecord]
    open_positions_at_end: List[OpenPositionState]


def run_portfolio_simulation(
    label: str,
    symbols: List[str],
    data_1d_by_symbol: Dict[str, pd.DataFrame],
    data_5m_by_symbol: Dict[str, pd.DataFrame],
    df_spy_5m: Optional[pd.DataFrame],
    cfg: StrategyConfig = StrategyConfig(),
    exec_cfg: ExecutionConfig = ExecutionConfig(),
    risk_cfg: RiskConfig = RiskConfig(),
    earnings_oracle: Optional[EarningsOracle] = None,
    calendar: Optional[NYSECalendar] = None,
) -> PortfolioSimulationResult:
    """
    Simulate an account-level portfolio across `symbols` over all trading dates
    present in the 5-minute dataset after warm-up (`min_tod_history_sessions`).
    """
    cal = calendar or NYSECalendar()
    oracle = earnings_oracle or EarningsOracle(
        blackout_days=cfg.er_blackout_calendar_days,
        unknown_policy=cfg.er_unknown_policy,
    )

    # Precompute causal tables per symbol
    daily_tables: Dict[str, pd.DataFrame] = {}
    feat_5m_tables: Dict[str, pd.DataFrame] = {}
    all_5m_dates_set = set()

    spy_1d = data_1d_by_symbol.get("SPY", pd.DataFrame())
    for sym in symbols:
        s_up = sym.upper()
        d1 = data_1d_by_symbol.get(s_up, pd.DataFrame())
        d5 = data_5m_by_symbol.get(s_up, pd.DataFrame())
        daily_tables[s_up] = compute_daily_regime_table(d1, cfg, spy_1d) if not d1.empty else pd.DataFrame()
        f5 = compute_causal_5m_features(d5, cfg, cal) if not d5.empty else pd.DataFrame()
        feat_5m_tables[s_up] = f5
        if not f5.empty:
            for d in f5["Date"].unique():
                all_5m_dates_set.add(d)

    spy_feat = compute_causal_spy_features(df_spy_5m) if (df_spy_5m is not None and not df_spy_5m.empty) else pd.DataFrame()
    all_session_dates = sorted(all_5m_dates_set)

    # Only include trading days after the initial ToD warm-up window (min_tod_history_sessions)
    if len(all_session_dates) > cfg.min_tod_history_sessions:
        eval_dates = all_session_dates[cfg.min_tod_history_sessions:]
    else:
        eval_dates = all_session_dates

    current_equity = risk_cfg.initial_account_equity_usd
    prev_unrealized_pnl = 0.0

    executed_trades: List[CompletedTradeRecord] = []
    skipped_trades: List[Tuple[CompletedTradeRecord, str]] = []
    daily_records: List[DailyPortfolioRecord] = []
    open_positions_end: List[OpenPositionState] = []

    for sess_date in eval_dates:
        candidate_trades: List[CompletedTradeRecord] = []
        session_open_positions: List[OpenPositionState] = []

        for sym in symbols:
            s_up = sym.upper()
            f5 = feat_5m_tables.get(s_up, pd.DataFrame())
            if f5.empty:
                continue
            res = evaluate_session_causal(
                symbol=s_up,
                session_date=sess_date,
                df_1d=data_1d_by_symbol.get(s_up, pd.DataFrame()),
                df_5m=data_5m_by_symbol.get(s_up, pd.DataFrame()),
                df_spy_5m=df_spy_5m,
                cfg=cfg,
                exec_cfg=exec_cfg,
                risk_cfg=risk_cfg,
                earnings_oracle=oracle,
                calendar=cal,
                precomputed_daily_table=daily_tables.get(s_up),
                precomputed_feat_5m=f5,
                precomputed_spy_feat=spy_feat,
            )
            for tr in res.completed_trades:
                candidate_trades.append(tr)
            if res.open_position is not None:
                session_open_positions.append(res.open_position)

        # Deterministic tie-breaking for simultaneous or overlapping candidate trades:
        # Sort by:
        #   1) entry_fill_ts ascending (chronological order)
        #   2) climax_vol_ratio descending (strongest institutional volume climax first)
        #   3) symbol alphabetical (deterministic tie-breaker)
        candidate_trades.sort(
            key=lambda t: (
                pd.Timestamp(t.entry_fill_ts),
                -float(t.climax_vol_ratio),
                t.symbol,
            )
        )

        # Enforce concurrent positions, buying power reservation, per-symbol exposure, and daily realized loss limit
        accepted_today: List[CompletedTradeRecord] = []
        skipped_today = 0
        peak_concurrent = 0
        peak_bp_used = 0.0
        realized_pnl_so_far = 0.0

        for cand in candidate_trades:
            cand_entry_ts = pd.Timestamp(cand.entry_fill_ts)

            # Check which previously accepted trades today have already exited before `cand_entry_ts`
            realized_pnl_so_far = sum(
                t.net_pnl_usd for t in accepted_today if pd.Timestamp(t.exit_fill_ts) <= cand_entry_ts
            )
            if realized_pnl_so_far <= -risk_cfg.max_daily_realized_loss_usd:
                skipped_trades.append((cand, "SKIPPED_DAILY_LOSS_LIMIT"))
                skipped_today += 1
                continue

            # Determine active open trades at `cand_entry_ts`
            active_now = [
                t for t in accepted_today
                if pd.Timestamp(t.entry_fill_ts) <= cand_entry_ts < pd.Timestamp(t.exit_fill_ts)
            ]

            if len(active_now) >= risk_cfg.max_concurrent_positions:
                skipped_trades.append((cand, "SKIPPED_MAX_CONCURRENT_POSITIONS"))
                skipped_today += 1
                continue

            # Calculate currently reserved buying power at `cand_entry_ts`
            bp_reserved_now = 0.0
            symbol_exposure_now = 0.0
            for t in active_now:
                if t.leg1_filled and t.leg1_fill_ts is not None and pd.Timestamp(t.leg1_fill_ts) <= cand_entry_ts:
                    pos_exp = t.tranche2_shares * t.entry_fill_price
                else:
                    pos_exp = t.notional_usd
                bp_reserved_now += pos_exp
                if t.symbol == cand.symbol:
                    symbol_exposure_now += pos_exp

            if symbol_exposure_now + cand.notional_usd > risk_cfg.max_symbol_exposure_usd:
                skipped_trades.append((cand, "SKIPPED_SYMBOL_EXPOSURE_LIMIT"))
                skipped_today += 1
                continue

            max_bp = min(current_equity + realized_pnl_so_far, risk_cfg.max_aggregate_exposure_usd)
            if bp_reserved_now + cand.notional_usd > max_bp:
                skipped_trades.append((cand, "SKIPPED_INSUFFICIENT_BUYING_POWER"))
                skipped_today += 1
                continue

            accepted_today.append(cand)
            executed_trades.append(cand)
            peak_concurrent = max(peak_concurrent, len(active_now) + 1)
            peak_bp_used = max(peak_bp_used, bp_reserved_now + cand.notional_usd)

        day_realized_pnl = round(sum(t.net_pnl_usd for t in accepted_today), 2)
        day_unrealized_pnl = round(sum(p.unrealized_pnl_usd for p in session_open_positions), 2)
        unrealized_change = round(day_unrealized_pnl - prev_unrealized_pnl, 2)
        total_day_pnl = round(day_realized_pnl + unrealized_change, 2)

        start_eq = current_equity
        end_eq = round(start_eq + total_day_pnl, 2)
        day_ret_pct = (total_day_pnl / start_eq) * 100.0 if start_eq > 0 else 0.0

        daily_records.append(
            DailyPortfolioRecord(
                session_date=sess_date.isoformat(),
                starting_equity_usd=round(start_eq, 2),
                realized_pnl_usd=day_realized_pnl,
                unrealized_pnl_change_usd=unrealized_change,
                total_daily_pnl_usd=total_day_pnl,
                ending_equity_usd=end_eq,
                daily_return_pct=round(day_ret_pct, 6),
                trades_entered=len(accepted_today) + len(session_open_positions),
                trades_closed=len(accepted_today),
                peak_concurrent_positions=peak_concurrent,
                peak_reserved_buying_power_usd=round(peak_bp_used, 2),
                skipped_signals_count=skipped_today,
            )
        )

        current_equity = end_eq
        prev_unrealized_pnl = day_unrealized_pnl
        open_positions_end = session_open_positions

    summary = _compute_summary_metrics(
        label=label,
        symbols=symbols,
        executed_trades=executed_trades,
        skipped_trades=skipped_trades,
        daily_records=daily_records,
        initial_equity=risk_cfg.initial_account_equity_usd,
        cfg=cfg,
    )

    return PortfolioSimulationResult(
        summary=summary,
        executed_trades=executed_trades,
        skipped_trades=skipped_trades,
        daily_equity_curve=daily_records,
        open_positions_at_end=open_positions_end,
    )


def _compute_summary_metrics(
    label: str,
    symbols: List[str],
    executed_trades: List[CompletedTradeRecord],
    skipped_trades: List[Tuple[CompletedTradeRecord, str]],
    daily_records: List[DailyPortfolioRecord],
    initial_equity: float,
    cfg: StrategyConfig,
) -> PortfolioMetricsSummary:
    total_days = len(daily_records)
    active_days = sum(1 for d in daily_records if d.trades_entered > 0 or d.trades_closed > 0)
    no_trade_days = total_days - active_days

    n_trades = len(executed_trades)
    ambig_trades = sum(1 for t in executed_trades if t.ambiguous_bar_count > 0)

    if n_trades > 0:
        trade_rets = np.array([t.net_return_pct for t in executed_trades], dtype=float)
        wins = trade_rets[trade_rets > 0]
        losses = trade_rets[trade_rets <= 0]
        wr_pct = (len(wins) / n_trades) * 100.0
        avg_ret = float(np.mean(trade_rets))
        sum_ret = float(np.sum(trade_rets))
        worst_trade = float(np.min(trade_rets))
        total_pnl = float(sum(t.net_pnl_usd for t in executed_trades))

        gross_win_usd = sum(t.net_pnl_usd for t in executed_trades if t.net_pnl_usd > 0)
        gross_loss_usd = abs(sum(t.net_pnl_usd for t in executed_trades if t.net_pnl_usd < 0))
        pf: float | str = round(gross_win_usd / gross_loss_usd, 2) if gross_loss_usd > 0 else "INF"

        # Legacy trade-level sqrt(252) Sharpe (reported ONLY for explicit comparison against daily portfolio Sharpe)
        t_std = float(np.std(trade_rets, ddof=1)) if n_trades > 1 else 0.0
        legacy_sharpe = round((avg_ret / t_std) * math.sqrt(252), 2) if t_std > 1e-9 else 0.0
    else:
        wr_pct = 0.0
        avg_ret = 0.0
        sum_ret = 0.0
        worst_trade = 0.0
        total_pnl = 0.0
        pf = 0.0
        legacy_sharpe = 0.0

    if total_days > 0:
        daily_rets = np.array([d.daily_return_pct for d in daily_records], dtype=float)
        d_mean = float(np.mean(daily_rets))
        d_std = float(np.std(daily_rets, ddof=1)) if total_days > 1 else 0.0
        daily_sharpe = round((d_mean / d_std) * math.sqrt(252), 2) if d_std > 1e-9 else 0.0
        worst_day = float(np.min(daily_rets))

        # Max drawdown on daily equity curve
        eq_series = np.array([initial_equity] + [d.ending_equity_usd for d in daily_records], dtype=float)
        running_peak = np.maximum.accumulate(eq_series)
        drawdowns = (eq_series - running_peak) / np.where(running_peak > 0, running_peak, 1.0)
        max_dd_pct = float(abs(np.min(drawdowns)) * 100.0)
        tot_port_ret_pct = ((eq_series[-1] - initial_equity) / initial_equity) * 100.0 if initial_equity > 0 else 0.0
    else:
        daily_sharpe = 0.0
        worst_day = 0.0
        max_dd_pct = 0.0
        tot_port_ret_pct = 0.0

    return PortfolioMetricsSummary(
        label=label,
        universe_symbols=list(symbols),
        total_trading_days=total_days,
        active_trade_days=active_days,
        no_trade_days=no_trade_days,
        executed_trades_count=n_trades,
        skipped_by_capital_count=len(skipped_trades),
        ambiguous_bars_affected_trades=ambig_trades,
        win_rate_pct=round(wr_pct, 2),
        avg_trade_net_return_pct=round(avg_ret, 4),
        sum_trade_net_return_pct=round(sum_ret, 2),
        total_net_pnl_usd=round(total_pnl, 2),
        total_portfolio_return_pct=round(tot_port_ret_pct, 2),
        profit_factor=pf,
        worst_trade_net_return_pct=round(worst_trade, 2),
        worst_day_portfolio_return_pct=round(worst_day, 4),
        max_drawdown_pct=round(max_dd_pct, 2),
        daily_portfolio_sharpe=daily_sharpe,
        legacy_trade_level_sharpe_comparison_only=legacy_sharpe,
        config_sha256=cfg.config_sha256(),
    )
