import React, { useMemo, useState } from 'react';
import {
  WholeMarketAuditStrategy,
  WholeMarketAuditSummary,
  WholeMarketCandidate,
  WholeMarketEtpMode,
  WholeMarketHedgeMode,
  WholeMarketScanPayload,
} from '../types';

interface WholeMarketAuditViewProps {
  scan: WholeMarketScanPayload | null;
  audit: WholeMarketAuditSummary | null;
  bookCapital: number;
  onChangeBookCapital: (cap: number) => void;
  hedgeMode: WholeMarketHedgeMode;
  onChangeHedgeMode: (mode: WholeMarketHedgeMode) => void;
  etpMode: WholeMarketEtpMode;
  onChangeEtpMode: (mode: WholeMarketEtpMode) => void;
  onRefreshScan: () => void;
  isRefreshing: boolean;
  autoPoll: boolean;
  onToggleAutoPoll: () => void;
  onCopyNotify?: (msg: string) => void;
}

type SubMode = 'SCANNER' | 'AUDIT';
type SleeveFilter = 'ALL' | 'S3' | 'S2' | 'S5';

const STATUS_LABELS: Record<WholeMarketCandidate['status'], string> = {
  SIGNAL_ACTIVE: '15:55 LIVE',
  FORMING_IN_WINDOW: 'FORMING 15:55',
  PRE_3PM_WATCH: '3PM WATCH',
  NEAR_TRIGGER_WATCH: 'NEAR RADAR',
  BLOCKED_GUARD: 'GUARD BLOCKED',
};

function formatAdtv(usd: number): string {
  if (usd >= 1e9) return `$${(usd / 1e9).toFixed(2)}B`;
  return `$${(usd / 1e6).toFixed(1)}M`;
}

function formatCapBtn(cap: number): string {
  if (cap >= 1_000_000) return `$${(cap / 1_000_000).toFixed(cap % 1_000_000 === 0 ? 0 : 1)}M`;
  return `$${cap / 1000}k`;
}

function formatUsdCompact(usd: number): string {
  if (usd >= 1_000_000) return `$${(usd / 1_000_000).toFixed(2)}M`;
  if (usd >= 10_000) return `$${(usd / 1_000).toFixed(1)}k`;
  return `$${usd.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
}

export const WholeMarketAuditView: React.FC<WholeMarketAuditViewProps> = ({
  scan,
  audit,
  bookCapital,
  onChangeBookCapital,
  hedgeMode,
  onChangeHedgeMode,
  etpMode,
  onChangeEtpMode,
  onRefreshScan,
  isRefreshing,
  autoPoll,
  onToggleAutoPoll,
  onCopyNotify,
}) => {
  const [subMode, setSubMode] = useState<SubMode>('SCANNER');
  const [sleeveFilter, setSleeveFilter] = useState<SleeveFilter>('ALL');
  const [includeNear, setIncludeNear] = useState<boolean>(true);
  const [searchQuery, setSearchQuery] = useState<string>('');
  const [selectedCandId, setSelectedCandId] = useState<string | null>(null);
  const [selectedAuditSleeve, setSelectedAuditSleeve] = useState<'S3' | 'S2' | 'S5'>('S3');

  const strategyMetaById = useMemo(() => {
    const map: Record<string, WholeMarketAuditStrategy> = {};
    if (audit?.strategies) {
      for (const s of audit.strategies) {
        map[s.id] = s;
      }
    }
    return map;
  }, [audit]);

  const displayedSleeves = useMemo(() => {
    return scan?.sleeves || [];
  }, [scan]);

  const visibleCandidates = useMemo(() => {
    if (!scan) return [];
    const baseList = includeNear
      ? [...scan.candidates, ...scan.near_candidates]
      : [...scan.candidates];
    const q = searchQuery.trim().toUpperCase();
    return baseList.filter((c) => {
      if (sleeveFilter !== 'ALL' && c.sleeve_id !== sleeveFilter) return false;
      if (!q) return true;
      return (
        c.symbol.toUpperCase().includes(q) ||
        c.name.toUpperCase().includes(q) ||
        c.industry.toUpperCase().includes(q) ||
        c.guild.toUpperCase().includes(q) ||
        (c.etp_sub_bucket || '').toUpperCase().includes(q)
      );
    });
  }, [scan, includeNear, sleeveFilter, searchQuery]);

  const selectedCandidate: WholeMarketCandidate | null = useMemo(() => {
    if (visibleCandidates.length === 0) return null;
    if (selectedCandId) {
      const found = visibleCandidates.find((c) => c.id === selectedCandId);
      if (found) return found;
    }
    return visibleCandidates[0];
  }, [visibleCandidates, selectedCandId]);

  const copyText = async (text: string, label: string) => {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // ignore headless restriction
    }
    if (onCopyNotify) {
      onCopyNotify(label);
    }
  };

  const handleCopyAllCohortTickets = () => {
    if (!scan) return;
    const activeCohort = scan.candidates.filter(
      (c) =>
        c.in_top_cohort &&
        !c.guard_blocked &&
        (sleeveFilter === 'ALL' || c.sleeve_id === sleeveFilter)
    );
    if (activeCohort.length === 0) {
      if (onCopyNotify) {
        onCopyNotify('No clean executable top-cohort IRA signals right now (check Near-Trigger Radar).');
      }
      return;
    }
    const joined = activeCohort.map((c) => c.copy_ticket).join('\n\n---\n\n');
    copyText(
      joined,
      `Copied ${activeCohort.length} clean IRA 5WJ26897 Long-Only (${
        hedgeMode === 'SH_INVERSE_ETF' ? 'BUY SH Hedged' : 'Unhedged Long'
      }) tickets to clipboard.`
    );
  };

  // Build SVG polyline paths strictly for IRA Long-Only 1Y compounded equity curves
  const chartGeometry = useMemo(() => {
    const pts = audit?.equity_curve || [];
    if (pts.length < 2) return null;
    const W = 860;
    const H = 225;
    const padL = 48;
    const padR = 18;
    const padT = 16;
    const padB = 26;
    const allVals: number[] = [];
    for (const p of pts) {
      allVals.push(p.ira_strict ?? 100, p.s2, p.s3, p.s5);
    }
    const minV = Math.min(92, ...allVals);
    const maxV = Math.max(130, ...allVals);
    const spanV = Math.max(1, maxV - minV);
    const xAt = (i: number) =>
      padL + (i / (pts.length - 1)) * (W - padL - padR);
    const yAt = (v: number) =>
      padT + (1 - (v - minV) / spanV) * (H - padT - padB);

    const makePath = (getter: (p: (typeof pts)[number]) => number) =>
      pts
        .map(
          (p, idx) =>
            `${idx === 0 ? 'M' : 'L'}${xAt(idx).toFixed(1)},${yAt(getter(p)).toFixed(1)}`
        )
        .join(' ');

    const yTicks = [100, 125, 150, 175, 200, 225, 250].filter(
      (v) => v >= minV - 4 && v <= maxV + 10
    );

    return {
      W,
      H,
      padL,
      padR,
      yAt,
      yTicks,
      startDt: pts[0].date,
      midDt: pts[Math.floor(pts.length / 2)].date,
      endDt: pts[pts.length - 1].date,
      iraPath: makePath((p) => p.ira_strict ?? 100),
      s2Path: makePath((p) => p.s2),
      s3Path: makePath((p) => p.s3),
      s5Path: makePath((p) => p.s5),
    };
  }, [audit]);

  const winPhase = scan?.market_window?.phase || 'MIDDAY_RTH';
  const phaseClass =
    winPhase === 'CLOSING_BELL_ACTIVE' || winPhase === 'MORNING_WINDOW'
      ? 'wm-banner-signal'
      : winPhase === 'POST_3PM_ANCHOR' || winPhase === 'PREMARKET'
      ? 'wm-banner-armed'
      : 'wm-banner-watching';

  const activeAuditStrat = useMemo(() => {
    return (
      audit?.strategies?.find((s) => s.id === selectedAuditSleeve) ||
      audit?.strategies?.[0] ||
      null
    );
  }, [audit, selectedAuditSleeve]);

  const sh = audit?.sledgehammer;
  const capSum = scan?.capital_summary;

  return (
    <div className="wm-root">
      {/* ==================== TOP COMMAND & IRA LONG-ONLY EXECUTION HEADER ==================== */}
      <header className="wm-header">
        <div className="wm-title-block">
          <div className="wm-title-row">
            <h2>IRA Market Monitor — 15:55 Closing-Cross Scanner</h2>
            {scan && (
              <span className="wm-latency-pill tabular">
                {scan.quotes_received.toLocaleString()} live US quotes ·{' '}
                {scan.scan_latency_ms.toFixed(0)}ms (
                {scan.feed_mode === 'CNBC_REALTIME_BATCH_REST' ? 'CNBC Live' : 'Deterministic'})
              </span>
            )}
          </div>
          <p className="wm-subtitle tabular">
            IRA <strong>5WJ26897 (100% Long-Only · Zero Shorting)</strong> ·{' '}
            <strong>S3 (50%) + S2 (25%) + S5 (25%)</strong> · Hard{' '}
            <strong>&le;5.0% IRA &amp; &le;0.5% ADTV Cap</strong> · Updated:{' '}
            <strong>{scan?.fetched_at_et || 'Loading...'}</strong>
          </p>
        </div>

        <div className="wm-controls-row">
          {/* Sub-View Mode Switcher */}
          <div className="wm-subnav" role="tablist" aria-label="Market Monitor Sub-Views">
            <button
              type="button"
              role="tab"
              aria-selected={subMode === 'SCANNER'}
              className={`wm-subnav-btn ${subMode === 'SCANNER' ? 'active' : ''}`}
              onClick={() => setSubMode('SCANNER')}
            >
              📡 Live IRA 3-Sleeve Monitor ({scan?.total_active_or_forming ?? 0})
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={subMode === 'AUDIT'}
              className={`wm-subnav-btn ${subMode === 'AUDIT' ? 'active' : ''}`}
              onClick={() => setSubMode('AUDIT')}
            >
              📊 IRA Audit &amp; Sledgehammer Proof
            </button>
          </div>

          {/* IRA Long-Only Beta Hedge Toggle (BUY SH vs Unhedged Long) */}
          <div className="wm-cap-group tabular" role="group" aria-label="IRA Beta Hedge Mode">
            <span className="wm-cap-label">IRA Hedge:</span>
            <button
              type="button"
              className={`wm-cap-btn ${hedgeMode === 'SH_INVERSE_ETF' ? 'active' : ''}`}
              onClick={() => onChangeHedgeMode('SH_INVERSE_ETF')}
              title="100% IRA-Compatible Beta Hedge: Cash BUY of SH (-1x Inverse S&P 500 ETF) on S2 & S5"
            >
              BUY SH (-1x SPY ETF)
            </button>
            <button
              type="button"
              className={`wm-cap-btn ${hedgeMode === 'UNHEDGED_LONG' ? 'active' : ''}`}
              onClick={() => onChangeHedgeMode('UNHEDGED_LONG')}
              title="Pure Unhedged Equity BUY Only (0 inverse ETF shares; single-leg ticket)"
            >
              Unhedged Pure Long
            </button>
          </div>

          {/* S3 ETP Universe Switcher */}
          <div className="wm-cap-group tabular" role="group" aria-label="S3 ETP Universe">
            <span className="wm-cap-label">S3 ETPs:</span>
            <button
              type="button"
              className={`wm-cap-btn ${etpMode === 'EQUITY_BULL_ER_GUARDED' ? 'active' : ''}`}
              onClick={() => onChangeEtpMode('EQUITY_BULL_ER_GUARDED')}
              title="Index/Sector Bull ETPs + Single-Stock 2x Bull ETPs with underlying [ER-1d, ER] blocked"
            >
              All Eq-Bull (ER-Guarded)
            </button>
            <button
              type="button"
              className={`wm-cap-btn ${etpMode === 'INDEX_SECTOR_ONLY' ? 'active' : ''}`}
              onClick={() => onChangeEtpMode('INDEX_SECTOR_ONLY')}
              title="Pure Index & Sector Bull ETPs only (TQQQ, SOXL, SPXL, UPRO, TNA, TECL, FAS, USD)"
            >
              Index/Sector Only
            </button>
          </div>

          {/* IRA Book Capital Sizer */}
          <div className="wm-cap-group tabular">
            <span className="wm-cap-label">IRA Cash:</span>
            {[50000, 100000, 250000, 500000, 1000000].map((cap) => (
              <button
                key={cap}
                type="button"
                className={`wm-cap-btn ${bookCapital === cap ? 'active' : ''}`}
                onClick={() => onChangeBookCapital(cap)}
              >
                {formatCapBtn(cap)}
              </button>
            ))}
          </div>

          <button
            type="button"
            className={`btn-ghost-xs ${autoPoll ? 'active-pill' : ''}`}
            onClick={onToggleAutoPoll}
          >
            {autoPoll ? '⏱ Auto: ON (20s)' : '⏸ Auto: OFF'}
          </button>

          <button
            type="button"
            className="btn-primary-xs"
            disabled={isRefreshing}
            onClick={onRefreshScan}
          >
            {isRefreshing ? 'Scanning US Market...' : '⟳ Scan Market Now'}
          </button>
        </div>
      </header>

      {/* ==================== EXECUTION BELL BANNER ==================== */}
      {scan && (
        <section className={`wm-window-banner ${phaseClass}`}>
          <div className="wm-window-left">
            <span className="wm-window-badge">{scan.market_window.badge}</span>
            <div>
              <div className="wm-window-bells">{scan.market_window.active_bells}</div>
              <div className="wm-window-guidance">{scan.market_window.guidance}</div>
            </div>
          </div>
          <div className="wm-spy-strip tabular">
            <div>
              <span className="wm-spy-lbl">SPY:</span>{' '}
              <strong>${scan.spy.last.toFixed(2)}</strong>{' '}
              <span className={scan.spy.day_chg_pct >= 0 ? 'text-emerald' : 'text-rose'}>
                ({scan.spy.day_chg_pct >= 0 ? '+' : ''}
                {scan.spy.day_chg_pct.toFixed(2)}% Day)
              </span>
              {scan.spy.sh_last ? (
                <>
                  {' · '}
                  <span className="wm-spy-lbl">SH (-1x ETF):</span>{' '}
                  <strong>${scan.spy.sh_last.toFixed(2)}</strong>
                </>
              ) : null}
            </div>
            <div className="wm-spy-sub">
              Open ${scan.spy.open.toFixed(2)} · Range ${scan.spy.low.toFixed(2)}–$
              {scan.spy.high.toFixed(2)} · 15:00 Anchor:{' '}
              <strong>
                {scan.spy.p1500
                  ? `$${scan.spy.p1500.toFixed(2)} (Locked)`
                  : 'Locks @ 15:00 ET'}
              </strong>
            </div>
          </div>
        </section>
      )}

      {/* ========================================================================
          SUB-VIEW 1: LIVE IRA LONG-ONLY 3-SLEEVE MONITOR (S3 / S2 / S5)
         ======================================================================== */}
      {subMode === 'SCANNER' && (
        <>
          {/* IRA Cash Buying-Power & Concentration Strip */}
          {capSum && (
            <section
              className="wm-cap-guard-strip tabular"
              aria-label="IRA Cash Buying Power & Concentration"
            >
              <div className="wm-cg-item">
                <span>Single-Ticker Cap (&le;5.0% IRA):</span>
                <strong>{formatUsdCompact(capSum.max_single_position_usd)} max / stock</strong>
              </div>
              <div className="wm-cg-item">
                <span>Primary Equity BUY $:</span>
                <strong className="text-emerald">
                  {formatUsdCompact(capSum.deployed_primary_usd)}
                </strong>
              </div>
              <div className="wm-cg-item">
                <span>
                  SH Inverse ETF BUY $ ({hedgeMode === 'SH_INVERSE_ETF' ? 'Active' : 'Off'}):
                </span>
                <strong>{formatUsdCompact(capSum.deployed_hedge_usd)}</strong>
              </div>
              <div className="wm-cg-item">
                <span>Total IRA Cash Deployed:</span>
                <strong className="text-emerald">
                  {formatUsdCompact(capSum.total_cash_deployed_usd)} ({capSum.deployed_pct.toFixed(1)}%)
                </strong>
              </div>
              <div className="wm-cg-item">
                <span>Undeployed Cash / SGOV Reserve:</span>
                <strong>
                  {formatUsdCompact(capSum.undeployed_cash_usd)} ·{' '}
                  {scan?.total_guard_blocked ?? 0} Blocked
                </strong>
              </div>
            </section>
          )}

          {/* 4-Card IRA Long-Only Sleeve Selector Strip */}
          <section className="wm-sleeve-strip cols-4" aria-label="IRA Strategy Sleeve Filters">
            <button
              type="button"
              className={`wm-sleeve-card ${sleeveFilter === 'ALL' ? 'selected' : ''}`}
              onClick={() => setSleeveFilter('ALL')}
            >
              <div className="wm-sc-top">
                <span className="wm-sc-id">ALL 3 IRA SLEEVES</span>
                <span className="wm-sc-wt tabular">100% Long-Only</span>
              </div>
              <div className="wm-sc-name">IRA 5WJ26897 Closing-Cross Book</div>
              <div className="wm-sc-metrics tabular">
                <span className="text-emerald">OOS Sh 3.58 · DD -2.45%</span>
                <span>·</span>
                <span>{scan?.total_active_or_forming ?? 0} Clean</span>
              </div>
              <div className="wm-sc-bells tabular">
                50% S3 · 25% S2 · 25% S5 @ 15:55 ET
              </div>
            </button>

            {displayedSleeves.map((sl) => {
              const isSel = sleeveFilter === sl.id;
              return (
                <button
                  key={sl.id}
                  type="button"
                  className={`wm-sleeve-card ${isSel ? 'selected' : ''} ${
                    sl.qualifying_count > 0 ? 'has-live' : ''
                  }`}
                  onClick={() => setSleeveFilter(sl.id)}
                >
                  <div className="wm-sc-top">
                    <span className={`wm-sc-badge sleeve-${sl.id.toLowerCase()}`}>
                      {sl.id} · BUY ONLY
                    </span>
                    <span className="wm-sc-wt tabular">
                      {(sl.weight * 100).toFixed(0)}% ({formatUsdCompact(sl.sleeve_capital_usd)})
                    </span>
                  </div>
                  <div className="wm-sc-name">{sl.name}</div>
                  <div className="wm-sc-metrics tabular">
                    <strong className={sl.qualifying_count > 0 ? 'text-emerald' : ''}>
                      {sl.qualifying_count} Clean
                    </strong>
                    {(sl.blocked_count || 0) > 0 && (
                      <span className="text-amber">· {sl.blocked_count} Blocked</span>
                    )}
                    <span>· {sl.near_count} Radar</span>
                  </div>
                  <div className="wm-sc-bells tabular">
                    {sl.entry_bell} → {sl.exit_bell}
                  </div>
                </button>
              );
            })}
          </section>

          {/* Filter & Batch Action Bar */}
          <div className="wm-filter-bar">
            <div className="wm-filter-left">
              <input
                type="text"
                className="wm-search-input"
                placeholder="Filter ticker, company, industry, or ETP bucket (e.g. SOXL, TQQQ, Semis, Software)..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
              />
              <label className="wm-checkbox-lbl">
                <input
                  type="checkbox"
                  checked={includeNear}
                  onChange={(e) => setIncludeNear(e.target.checked)}
                />
                <span>
                  Include Near-Trigger Radar ({scan?.near_candidates?.length ?? 0})
                </span>
              </label>
            </div>
            <div className="wm-filter-right">
              <button
                type="button"
                className="btn-secondary-xs"
                onClick={handleCopyAllCohortTickets}
              >
                📋 Copy Clean IRA Long-Only Tickets
              </button>
            </div>
          </div>

          {/* Main Scanner Desk Split Grid */}
          <div className="wm-desk-grid">
            {/* LEFT: Live IRA Long-Only Candidates Table */}
            <section className="wm-table-panel">
              <div className="panel-header">
                <h2>
                  IRA Long-Only 15:55 Setups &amp; Risk-Guard Monitor ({visibleCandidates.length})
                </h2>
                <span className="panel-subtitle tabular">
                  BUY Only (Zero Shorting) · &le;5.0% IRA Cap · ER &amp; 15:00–16:00 RSS Guard
                </span>
              </div>

              {visibleCandidates.length === 0 ? (
                <div className="wm-empty-box">
                  No qualifying or near-trigger IRA candidates match the current filter. Enable{' '}
                  <strong>Include Near-Trigger Radar</strong> or select{' '}
                  <strong>ALL 3 IRA SLEEVES</strong>.
                </div>
              ) : (
                <div className="wm-table-scroll">
                  <table className="wm-table tabular">
                    <thead>
                      <tr>
                        <th>Sleeve</th>
                        <th>Status</th>
                        <th>Symbol / Sub-Bucket</th>
                        <th>Last / Day %</th>
                        <th>Primary Signal Metric</th>
                        <th>ER &amp; NLP News Guard</th>
                        <th>IRA BUY Clip &amp; SH Hedge</th>
                        <th>RT Cost</th>
                      </tr>
                    </thead>
                    <tbody>
                      {visibleCandidates.map((c) => {
                        const isSelected = selectedCandidate?.id === c.id;
                        const isNear =
                          c.status === 'NEAR_TRIGGER_WATCH' ||
                          c.status === 'PRE_3PM_WATCH';
                        const isBlocked =
                          Boolean(c.guard_blocked) || c.status === 'BLOCKED_GUARD';
                        return (
                          <tr
                            key={c.id}
                            className={`wm-row ${isSelected ? 'selected' : ''} ${
                              isBlocked
                                ? 'wm-row-blocked'
                                : isNear
                                ? 'wm-row-near'
                                : 'wm-row-live'
                            }`}
                            onClick={() => setSelectedCandId(c.id)}
                          >
                            <td>
                              <span
                                className={`wm-sleeve-pill sleeve-${c.sleeve_id.toLowerCase()}`}
                              >
                                {c.sleeve_id}
                              </span>
                            </td>
                            <td>
                              <span className={`wm-status-pill st-${c.status.toLowerCase()}`}>
                                {STATUS_LABELS[c.status]}
                              </span>
                              {c.cohort_rank > 0 && (
                                <span className="wm-rank-tag">
                                  #{c.cohort_rank}
                                  {!c.in_top_cohort ? ' (Overflow)' : ''}
                                </span>
                              )}
                            </td>
                            <td>
                              <div className="wm-sym-cell">
                                <strong>{c.symbol}</strong>
                                <span className="wm-adtv">{formatAdtv(c.adtv20_usd)}</span>
                              </div>
                              <div className="wm-ind-sub">
                                {c.etp_sub_bucket
                                  ? `${c.etp_sub_bucket}${
                                      c.underlying_symbol ? ` (${c.underlying_symbol})` : ''
                                    }`
                                  : `${c.guild} · ${c.industry || c.name}`}
                              </div>
                            </td>
                            <td>
                              <div>
                                <strong>${c.last.toFixed(2)}</strong>{' '}
                                <span className="wm-muted-sm">(Op ${c.open.toFixed(2)})</span>
                              </div>
                              <div
                                className={
                                  c.day_chg_pct >= 0 ? 'text-emerald' : 'text-rose'
                                }
                              >
                                {c.day_chg_pct >= 0 ? '+' : ''}
                                {c.day_chg_pct.toFixed(2)}%
                              </div>
                            </td>
                            <td className="wm-metric-cell">
                              <strong>{c.primary_label}</strong>
                            </td>
                            <td className="wm-sec-cell">
                              <span className={isBlocked ? 'text-rose' : ''}>
                                {c.secondary_label}
                              </span>
                            </td>
                            <td>
                              {isBlocked ? (
                                <span className="text-rose">
                                  <strong>$0 (BLOCKED)</strong>
                                </span>
                              ) : (
                                <>
                                  <div>
                                    <strong className="text-emerald">
                                      BUY {c.shares.toLocaleString()} sh
                                    </strong>{' '}
                                    ($
                                    {c.notional_usd.toLocaleString(undefined, {
                                      maximumFractionDigits: 0,
                                    })}
                                    )
                                  </div>
                                  <div className="wm-hedge-sub">
                                    {c.sh_hedge_shares > 0 && c.hedge_symbol !== 'NONE'
                                      ? `BUY ${c.sh_hedge_shares.toLocaleString()} ${
                                          c.hedge_symbol
                                        } ($${c.hedge_notional_usd.toLocaleString(undefined, {
                                          maximumFractionDigits: 0,
                                        })})`
                                      : 'Long-Only Unhedged'}
                                  </div>
                                </>
                              )}
                            </td>
                            <td>{c.rt_cost_bps.toFixed(1)} bps</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </section>

            {/* RIGHT: Selected IRA Signal Inspector & Long-Only Order Ticket */}
            <aside className="wm-inspector-panel">
              {selectedCandidate ? (
                <div className="wm-inspector-body">
                  <div className="wm-insp-head">
                    <div>
                      <span
                        className={`wm-sleeve-pill sleeve-${selectedCandidate.sleeve_id.toLowerCase()}`}
                      >
                        {selectedCandidate.sleeve_short_name}
                      </span>
                      <h3 className="wm-insp-sym">
                        {selectedCandidate.symbol}{' '}
                        <span className="wm-insp-name">{selectedCandidate.name}</span>
                      </h3>
                    </div>
                    <span
                      className={`wm-status-pill st-${selectedCandidate.status.toLowerCase()}`}
                    >
                      {STATUS_LABELS[selectedCandidate.status]}
                    </span>
                  </div>

                  {/* Sledgehammer Risk-Guard Quarantine Alert */}
                  {(selectedCandidate.guard_blocked ||
                    selectedCandidate.status === 'BLOCKED_GUARD') && (
                    <div className="wm-quarantine-alert">
                      <strong>🚨 DO NOT EXECUTE — QUARANTINED BY SLEDGEHAMMER GUARD:</strong>
                      <div>
                        {selectedCandidate.guard_reason ||
                          'Blocked by Earnings Blackout or 15:00–16:00 Corporate News.'}
                      </div>
                    </div>
                  )}

                  {/* Live NLP Headline Scraper Status Badge */}
                  {selectedCandidate.news_guard && (
                    <div
                      className={`wm-news-pill tabular ${
                        selectedCandidate.news_guard.status === 'BLOCKED_HEADLINE'
                          ? 'news-blocked'
                          : 'news-clear'
                      }`}
                    >
                      <div>
                        <strong>Live 15:00–16:00 NLP News Guard:</strong>{' '}
                        {selectedCandidate.news_guard.status === 'BLOCKED_HEADLINE'
                          ? '🚨 HEADLINE DETECTED — ABORT'
                          : `✅ CLEAR (${selectedCandidate.news_guard.items_scanned} RSS items verified)`}
                      </div>
                      {selectedCandidate.news_guard.latest_headline && (
                        <div className="wm-news-hl">
                          Latest ({selectedCandidate.news_guard.latest_time_et}): &ldquo;
                          {selectedCandidate.news_guard.latest_headline}&rdquo;
                        </div>
                      )}
                    </div>
                  )}

                  {/* Structural Counterparty Box */}
                  {strategyMetaById[selectedCandidate.sleeve_id] && (
                    <div className="wm-why-box">
                      <div className="wm-why-title">
                        Why This Trade Exists (Counterparty Mechanics)
                      </div>
                      <p>{strategyMetaById[selectedCandidate.sleeve_id].structural_why}</p>
                    </div>
                  )}

                  {/* Gate Checklist */}
                  <div className="wm-section-lbl">IRA Causal Gate Checklist</div>
                  <div className="wm-gates-list tabular">
                    {selectedCandidate.gates.map((g, i) => (
                      <div
                        key={i}
                        className={`wm-gate-item ${g.pass ? 'gate-pass' : 'gate-fail'}`}
                      >
                        <div className="wm-gate-top">
                          <strong>
                            {g.pass ? '✅' : '🛑'} {g.name}
                          </strong>
                          <span className="wm-gate-req">{g.required}</span>
                        </div>
                        <div className="wm-gate-obs">{g.observed}</div>
                      </div>
                    ))}
                  </div>

                  {/* IRA Long-Only Position & SH Hedge Sizer */}
                  <div className="wm-section-lbl">
                    IRA 5WJ26897 Cash Order Plan ({formatCapBtn(bookCapital)} ·{' '}
                    {hedgeMode === 'SH_INVERSE_ETF' ? 'BUY SH Hedged' : 'Unhedged Long'})
                  </div>
                  <div className="wm-plan-grid tabular">
                    <div>
                      <span>Leg 1 (Primary Equity):</span>
                      <strong
                        className={
                          selectedCandidate.shares === 0 ? 'text-rose' : 'text-emerald'
                        }
                      >
                        {selectedCandidate.shares === 0
                          ? 'BLOCKED ($0)'
                          : `BUY ${selectedCandidate.shares.toLocaleString()} sh @ $${selectedCandidate.ref_price.toFixed(
                              2
                            )}`}
                      </strong>
                    </div>
                    <div>
                      <span>Leg 2 (IRA Inverse ETF Hedge):</span>
                      <strong>
                        {selectedCandidate.sh_hedge_shares > 0 &&
                        selectedCandidate.hedge_symbol !== 'NONE'
                          ? `BUY ${selectedCandidate.sh_hedge_shares.toLocaleString()} sh ${
                              selectedCandidate.hedge_symbol
                            } (~$${selectedCandidate.hedge_price.toFixed(2)})`
                          : 'None (100% Unhedged Long)'}
                      </strong>
                    </div>
                    <div>
                      <span>Total IRA Cash Used:</span>
                      <strong>
                        $
                        {(
                          selectedCandidate.notional_usd +
                          selectedCandidate.hedge_notional_usd
                        ).toLocaleString()}{' '}
                        (ADTV {formatAdtv(selectedCandidate.adtv20_usd)})
                      </strong>
                    </div>
                    <div>
                      <span>Catastrophe Stop:</span>
                      <strong className="text-rose">
                        ${selectedCandidate.stop_price.toFixed(2)}
                      </strong>
                    </div>
                    <div>
                      <span>BUY Entry Window:</span>
                      <strong>{selectedCandidate.entry_bell}</strong>
                    </div>
                    <div>
                      <span>Mandatory SELL Exit:</span>
                      <strong>{selectedCandidate.exit_bell}</strong>
                    </div>
                  </div>

                  <pre className="wm-ticket-pre tabular">
                    {selectedCandidate.copy_ticket}
                  </pre>

                  <div className="wm-ticket-actions">
                    <button
                      type="button"
                      className="btn-primary-sm"
                      disabled={Boolean(selectedCandidate.guard_blocked)}
                      onClick={() =>
                        copyText(
                          selectedCandidate.copy_ticket,
                          `Copied IRA 5WJ26897 ${selectedCandidate.sleeve_id} ticket for ${selectedCandidate.symbol}`
                        )
                      }
                    >
                      {selectedCandidate.guard_blocked
                        ? '🛑 Ticket Quarantined'
                        : '📋 Copy IRA Long-Only Ticket'}
                    </button>
                    <button
                      type="button"
                      className="btn-utility"
                      onClick={() =>
                        window.open(
                          `https://my.tastytrade.com/app.html#/trade/equity?symbol=${encodeURIComponent(
                            selectedCandidate.symbol
                          )}`,
                          '_blank',
                          'noopener,noreferrer'
                        )
                      }
                    >
                      Open Tastytrade ↗
                    </button>
                  </div>
                </div>
              ) : (
                <div className="wm-empty-box">
                  Select a candidate row on the left to inspect its IRA causal gates and
                  long-only order ticket.
                </div>
              )}
            </aside>
          </div>
        </>
      )}

      {/* ========================================================================
          SUB-VIEW 2: IRA LONG-ONLY SLEDGEHAMMER AUDIT, DSR & 10,526 GRAVEYARD
         ======================================================================== */}
      {subMode === 'AUDIT' && audit && (
        <div className="wm-audit-container">
          {/* Honest Post-Sledgehammer IRA Portfolio & DSR KPI Cards */}
          <section className="wm-kpi-grid tabular">
            {sh ? (
              <>
                <div className="wm-kpi-card">
                  <div className="wm-kpi-label">
                    IRA 5WJ26897 &le;5% Cap · Index-Only S3 + SH (2026 OOS)
                  </div>
                  <div className="wm-kpi-main">
                    <span className="wm-kpi-ret text-emerald">
                      +{sh.portfolios.ira_index_s3_oos.return_pct.toFixed(2)}%
                    </span>
                    <span className="wm-kpi-sh">
                      Sharpe {sh.portfolios.ira_index_s3_oos.sharpe.toFixed(2)}
                    </span>
                  </div>
                  <div className="wm-kpi-sub">
                    t = +{sh.portfolios.ira_index_s3_oos.daily_t.toFixed(2)} · Max DD{' '}
                    {sh.portfolios.ira_index_s3_oos.max_dd_pct.toFixed(2)}% ·{' '}
                    {sh.portfolios.ira_index_s3_oos.win_months_pct.toFixed(0)}% Win Mo ·{' '}
                    {sh.portfolios.ira_index_s3_oos.avg_deployed_pct}% Avg Deployed (Idle=0%)
                  </div>
                </div>

                <div className="wm-kpi-card">
                  <div className="wm-kpi-label">
                    IRA 5WJ26897 &le;5% Cap · Index-Only S3 + SH (Trailing 1Y)
                  </div>
                  <div className="wm-kpi-main">
                    <span className="wm-kpi-ret text-emerald">
                      +{sh.portfolios.ira_index_s3_1y.return_pct.toFixed(2)}%
                    </span>
                    <span className="wm-kpi-sh">
                      Sharpe {sh.portfolios.ira_index_s3_1y.sharpe.toFixed(2)}
                    </span>
                  </div>
                  <div className="wm-kpi-sub">
                    t = +{sh.portfolios.ira_index_s3_1y.daily_t.toFixed(2)} · Max DD{' '}
                    {sh.portfolios.ira_index_s3_1y.max_dd_pct.toFixed(2)}% ·{' '}
                    {sh.portfolios.ira_index_s3_1y.win_months_pct.toFixed(1)}% Win Months
                  </div>
                </div>

                <div className="wm-kpi-card">
                  <div className="wm-kpi-label">
                    IRA 5WJ26897 &le;5% Cap · All Eq-Bull S3 + SH (2026 OOS)
                  </div>
                  <div className="wm-kpi-main">
                    <span className="wm-kpi-ret text-emerald">
                      +{sh.portfolios.ira_eqbull_s3_oos.return_pct.toFixed(2)}%
                    </span>
                    <span className="wm-kpi-sh">
                      Sharpe {sh.portfolios.ira_eqbull_s3_oos.sharpe.toFixed(2)}
                    </span>
                  </div>
                  <div className="wm-kpi-sub">
                    t = +{sh.portfolios.ira_eqbull_s3_oos.daily_t.toFixed(2)} · Max DD{' '}
                    {sh.portfolios.ira_eqbull_s3_oos.max_dd_pct.toFixed(2)}% · 1Y: +
                    {sh.portfolios.ira_eqbull_s3_1y.return_pct.toFixed(1)}% (Sh{' '}
                    {sh.portfolios.ira_eqbull_s3_1y.sharpe.toFixed(2)})
                  </div>
                </div>

                <div className="wm-kpi-card">
                  <div className="wm-kpi-label">
                    Bailey &amp; López de Prado Deflated Sharpe (DSR)
                  </div>
                  <div className="wm-kpi-main">
                    <span className="wm-kpi-ret">
                      DSR {sh.portfolios.ira_index_s3_oos.dsr_meff.toFixed(3)}
                    </span>
                    <span className="wm-kpi-sh">
                      SR₀ = {sh.trial_stats.sr0_hurdle_ann_meff.toFixed(2)}
                    </span>
                  </div>
                  <div className="wm-kpi-sub">
                    M_eff = {sh.trial_stats.m_eff_trials} (p ={' '}
                    {sh.portfolios.ira_index_s3_oos.pval_meff.toFixed(3)}) · Raw N ={' '}
                    {sh.trial_stats.n_raw_trials.toLocaleString()} (SR₀ ={' '}
                    {sh.trial_stats.sr0_hurdle_ann_10k.toFixed(2)})
                  </div>
                </div>
              </>
            ) : (
              audit.portfolio_windows.map((w, idx) => (
                <div key={idx} className="wm-kpi-card">
                  <div className="wm-kpi-label">{w.label}</div>
                  <div className="wm-kpi-main">
                    <span className="wm-kpi-ret text-emerald">
                      +{w.compounded_return_pct.toFixed(1)}%
                    </span>
                    <span className="wm-kpi-sh">Sharpe {w.sharpe.toFixed(2)}</span>
                  </div>
                  <div className="wm-kpi-sub">
                    t = +{w.daily_t_stat.toFixed(2)} · Max DD {w.max_drawdown_pct.toFixed(2)}%
                  </div>
                </div>
              ))
            )}
          </section>

          {/* PRINCIPAL QUANT SLEDGEHAMMER BEFORE-VS-AFTER SURVIVAL TABLE (LONG-ONLY CANDIDATES) */}
          {sh && (
            <section className="wm-audit-card">
              <div className="panel-header">
                <h2>
                  Principal Quant Sledgehammer Torture Test — Long-Only Survival Table (Zero
                  Short-Selling Strategies Allowed in IRA)
                </h2>
                <span className="panel-subtitle tabular">
                  09:35 Execution · Tiered Spreads · Unadj &ge; $5 · SEC 8-K &amp; ER Blackouts ·
                  PIT S&amp;P 500 · BUY SH Hedge
                </span>
              </div>
              <div className="wm-table-scroll">
                <table className="wm-table tabular">
                  <thead>
                    <tr>
                      <th>Sleeve</th>
                      <th>Long-Only Strategy Hypothesis</th>
                      <th>Before (Naive) Net / t</th>
                      <th>@ 09:31 / Alt Variant</th>
                      <th>After IRA Sledgehammer Net / t</th>
                      <th>Net Change</th>
                      <th>IRA Verdict</th>
                      <th>Forensic Root Cause</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sh.survival_table.map((r) => {
                      const dead =
                        r.verdict.includes('DEAD') || r.verdict.includes('REJECTED');
                      return (
                        <tr key={r.id} className={dead ? 'wm-row-blocked' : ''}>
                          <td>
                            <span className={`wm-sleeve-pill sleeve-${r.id.toLowerCase()}`}>
                              {r.id}
                            </span>
                          </td>
                          <td>
                            <strong>{r.name}</strong>
                          </td>
                          <td>
                            +{r.before_net_bps.toFixed(1)} bps (t = +{r.before_t.toFixed(2)})
                          </td>
                          <td>
                            {r.after_0931_net_bps >= 0 ? '+' : ''}
                            {r.after_0931_net_bps.toFixed(1)} bps
                          </td>
                          <td className={dead ? 'text-rose' : 'text-emerald'}>
                            <strong>
                              {r.after_net_bps >= 0 ? '+' : ''}
                              {r.after_net_bps.toFixed(1)} bps (t ={' '}
                              {r.after_t >= 0 ? '+' : ''}
                              {r.after_t.toFixed(2)})
                            </strong>
                          </td>
                          <td className={r.decay_pct < -50 ? 'text-rose' : 'text-emerald'}>
                            {r.decay_pct >= 0 ? '+' : ''}
                            {r.decay_pct.toFixed(1)}%
                          </td>
                          <td>
                            <span
                              className={`wm-status-pill ${
                                dead ? 'st-blocked_guard' : 'st-signal_active'
                              }`}
                            >
                              {r.verdict}
                            </span>
                          </td>
                          <td className="wm-sec-cell">{r.cause}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </section>
          )}

          {/* S3 SUB-UNIVERSE FORENSICS + IRA PORTFOLIO DSR BREAKDOWN */}
          {sh && (
            <div className="wm-two-col">
              <section className="wm-audit-card">
                <div className="panel-header">
                  <h2>S3 Leveraged ETP Sub-Universe Forensics (Unadj &ge; $5 + Tiered Spreads)</h2>
                </div>
                <table className="wm-mini-table tabular">
                  <thead>
                    <tr>
                      <th>ETP Sub-Universe</th>
                      <th>Trades</th>
                      <th>Gross</th>
                      <th>Net bps</th>
                      <th>Win %</th>
                      <th>t-stat</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr>
                      <td>
                        <strong>Index/Sector Bull Only (TQQQ/SOXL/SPXL/TNA)</strong>
                      </td>
                      <td>{sh.s3_sub_universes.index_sector_bull.n}</td>
                      <td>+{sh.s3_sub_universes.index_sector_bull.gross.toFixed(1)}</td>
                      <td className="text-emerald">
                        <strong>+{sh.s3_sub_universes.index_sector_bull.net.toFixed(1)}</strong>
                      </td>
                      <td>{sh.s3_sub_universes.index_sector_bull.wr.toFixed(1)}%</td>
                      <td className="text-emerald">
                        +{sh.s3_sub_universes.index_sector_bull.t.toFixed(2)}
                      </td>
                    </tr>
                    <tr>
                      <td>
                        <strong>All US Equity Bull (Underlying ER Blocked)</strong>
                      </td>
                      <td>{sh.s3_sub_universes.all_us_equity_bull_no_er.n}</td>
                      <td>+{sh.s3_sub_universes.all_us_equity_bull_no_er.gross.toFixed(1)}</td>
                      <td className="text-emerald">
                        <strong>
                          +{sh.s3_sub_universes.all_us_equity_bull_no_er.net.toFixed(1)}
                        </strong>
                      </td>
                      <td>{sh.s3_sub_universes.all_us_equity_bull_no_er.wr.toFixed(1)}%</td>
                      <td className="text-emerald">
                        +{sh.s3_sub_universes.all_us_equity_bull_no_er.t.toFixed(2)}
                      </td>
                    </tr>
                    <tr>
                      <td>Single-Stock 2x Bull Only (Underlying ER Blocked)</td>
                      <td>{sh.s3_sub_universes.single_stock_bull_no_er.n}</td>
                      <td>+{sh.s3_sub_universes.single_stock_bull_no_er.gross.toFixed(1)}</td>
                      <td className="text-emerald">
                        +{sh.s3_sub_universes.single_stock_bull_no_er.net.toFixed(1)}
                      </td>
                      <td>{sh.s3_sub_universes.single_stock_bull_no_er.wr.toFixed(1)}%</td>
                      <td>+{sh.s3_sub_universes.single_stock_bull_no_er.t.toFixed(2)}</td>
                    </tr>
                    <tr className="wm-row-blocked">
                      <td>
                        <strong>Commodity / Rates / VIX / Crypto (STRIPPED)</strong>
                      </td>
                      <td>{sh.s3_sub_universes.commodity_rates_vix_rejected.n}</td>
                      <td>+{sh.s3_sub_universes.commodity_rates_vix_rejected.gross.toFixed(1)}</td>
                      <td className="text-rose">
                        <strong>
                          {sh.s3_sub_universes.commodity_rates_vix_rejected.net.toFixed(1)}
                        </strong>
                      </td>
                      <td>{sh.s3_sub_universes.commodity_rates_vix_rejected.wr.toFixed(1)}%</td>
                      <td className="text-rose">
                        {sh.s3_sub_universes.commodity_rates_vix_rejected.t.toFixed(2)}
                      </td>
                    </tr>
                  </tbody>
                </table>
                <p className="wm-sls-verdict" style={{ marginTop: '0.55rem' }}>
                  <strong>09:30 vs 09:35 SELL Check (CNBC 5m Sample):</strong> 15:55 BUY → 09:30
                  Open SELL captures{' '}
                  <strong>+{sh.s3_sub_universes.exit_0930_gross_bps.toFixed(1)} bps</strong> gross,
                  and delaying exit to 09:35 5m close still retains{' '}
                  <strong>+{sh.s3_sub_universes.exit_0935_gross_bps.toFixed(1)} bps</strong> gross.
                </p>
              </section>

              <section className="wm-audit-card">
                <div className="panel-header">
                  <h2>
                    IRA 5WJ26897 $1,000,000 Sizing (&le;5% Cap · Idle Cash = 0%) &amp; Deflated
                    Sharpe
                  </h2>
                </div>
                <table className="wm-mini-table tabular">
                  <thead>
                    <tr>
                      <th>IRA Long-Only Simulation</th>
                      <th>Net Ret</th>
                      <th>Sharpe</th>
                      <th>Max DD</th>
                      <th>DSR (M=480)</th>
                      <th>DSR (10.5k)</th>
                    </tr>
                  </thead>
                  <tbody>
                    {[
                      ['IRA 5WJ26897 · Index S3 + SH (2026 OOS)', sh.portfolios.ira_index_s3_oos],
                      ['IRA 5WJ26897 · Index S3 + SH (1Y)', sh.portfolios.ira_index_s3_1y],
                      ['IRA 5WJ26897 · EqBull S3 + SH (2026 OOS)', sh.portfolios.ira_eqbull_s3_oos],
                      ['IRA 5WJ26897 · EqBull S3 + SH (1Y)', sh.portfolios.ira_eqbull_s3_1y],
                      ['Sleeve S3 · Index/Sector Bull (1Y)', sh.portfolios.sleeve_dsr.S3_IndexSectorBull],
                      ['Sleeve S2 · Clean Rule 10b-18 (1Y)', sh.portfolios.sleeve_dsr.S2_Clean10b18],
                    ]
                      .filter(([, st]) => Boolean(st))
                      .map(([lbl, st]: any, idx) => (
                        <tr key={idx}>
                          <td>
                            <strong>{lbl}</strong>
                          </td>
                          <td className="text-emerald">+{st.return_pct.toFixed(2)}%</td>
                          <td>
                            <strong>{st.sharpe.toFixed(2)}</strong>
                          </td>
                          <td>{st.max_dd_pct.toFixed(2)}%</td>
                          <td>{st.dsr_meff.toFixed(3)}</td>
                          <td>{st.dsr_10k.toFixed(3)}</td>
                        </tr>
                      ))}
                  </tbody>
                </table>
                <p className="wm-sls-verdict" style={{ marginTop: '0.55rem' }}>
                  <strong>Honest Statistical Takeaway:</strong> With T=215–266 days and fat tails
                  (kurtosis ~15), clearing Bailey &amp; López de Prado&apos;s{' '}
                  <code>SR₀ = 3.37</code> (<code>M_eff = 480</code>) yields{' '}
                  <code>DSR = 0.582</code> on the 2026 OOS IRA Long-Only book — 100% executable
                  without margin or shorting.
                </p>
              </section>
            </div>
          )}

          {/* Compounded IRA Equity Curve SVG */}
          {chartGeometry && (
            <section className="wm-audit-card">
              <div className="panel-header">
                <h2>
                  Trailing 1-Year IRA Long-Only Net Compounded Equity Curves (Base 100 · &le;5% Cap)
                </h2>
                <div className="wm-legend tabular">
                  <span className="leg-port">━ IRA 5WJ26897 &le;5% Cap Book (+25.8%)</span>
                  <span className="leg-s3">┄ S3 Equity Bull ETP Sleeve</span>
                  <span className="leg-s2">┄ S2 Rule 10b-18 Sleeve</span>
                  <span className="leg-s5">┄ S5 PIT S&amp;P 500 Sleeve</span>
                </div>
              </div>
              <div className="wm-svg-wrap">
                <svg
                  viewBox={`0 0 ${chartGeometry.W} ${chartGeometry.H}`}
                  className="wm-eq-svg"
                  role="img"
                  aria-label="IRA Long-Only Net Compounded Equity Curves"
                >
                  {chartGeometry.yTicks.map((tv) => (
                    <g key={tv}>
                      <line
                        x1={chartGeometry.padL}
                        x2={chartGeometry.W - chartGeometry.padR}
                        y1={chartGeometry.yAt(tv)}
                        y2={chartGeometry.yAt(tv)}
                        stroke="#1e293b"
                        strokeDasharray="3 3"
                      />
                      <text
                        x={chartGeometry.padL - 8}
                        y={chartGeometry.yAt(tv) + 4}
                        textAnchor="end"
                        fill="#94a3b8"
                        fontSize="10"
                      >
                        {tv}
                      </text>
                    </g>
                  ))}
                  <path
                    d={chartGeometry.s3Path}
                    fill="none"
                    stroke="#a855f7"
                    strokeWidth="1.3"
                    strokeDasharray="4 3"
                    opacity="0.7"
                  />
                  <path
                    d={chartGeometry.s2Path}
                    fill="none"
                    stroke="#38bdf8"
                    strokeWidth="1.3"
                    strokeDasharray="4 3"
                    opacity="0.7"
                  />
                  <path
                    d={chartGeometry.s5Path}
                    fill="none"
                    stroke="#14b8a6"
                    strokeWidth="1.3"
                    strokeDasharray="4 3"
                    opacity="0.7"
                  />
                  <path
                    d={chartGeometry.iraPath}
                    fill="none"
                    stroke="#10b981"
                    strokeWidth="2.8"
                  />
                  <text
                    x={chartGeometry.padL}
                    y={chartGeometry.H - 6}
                    fill="#64748b"
                    fontSize="10"
                  >
                    {chartGeometry.startDt}
                  </text>
                  <text
                    x={chartGeometry.W / 2}
                    y={chartGeometry.H - 6}
                    textAnchor="middle"
                    fill="#64748b"
                    fontSize="10"
                  >
                    {chartGeometry.midDt}
                  </text>
                  <text
                    x={chartGeometry.W - chartGeometry.padR}
                    y={chartGeometry.H - 6}
                    textAnchor="end"
                    fill="#64748b"
                    fontSize="10"
                  >
                    {chartGeometry.endDt}
                  </text>
                </svg>
              </div>
            </section>
          )}

          {/* Surviving 3 IRA Long-Only Sleeves Leaderboard */}
          <section className="wm-audit-card">
            <div className="panel-header">
              <h2>
                IRA 5WJ26897 Surviving Long-Only Strategy Ledger (3 Active 15:55 Sleeves — Click Any
                Row)
              </h2>
            </div>
            <div className="wm-table-scroll">
              <table className="wm-table tabular">
                <thead>
                  <tr>
                    <th>Sleeve</th>
                    <th>IRA Long-Only Strategy &amp; Clock Window</th>
                    <th>IRA Wt</th>
                    <th>Trades (Days)</th>
                    <th>Gross / Cost / Net</th>
                    <th>Win %</th>
                    <th>Train t / OOS t / Full t</th>
                    <th>Audited Sharpe</th>
                  </tr>
                </thead>
                <tbody>
                  {audit.strategies.map((s) => (
                    <tr
                      key={s.id}
                      className={`wm-row ${selectedAuditSleeve === s.id ? 'selected' : ''}`}
                      onClick={() => setSelectedAuditSleeve(s.id)}
                    >
                      <td>
                        <span className={`wm-sleeve-pill sleeve-${s.id.toLowerCase()}`}>
                          {s.id}
                        </span>
                      </td>
                      <td>
                        <strong>{s.name}</strong>
                        <div className="wm-ind-sub">{s.clock_window}</div>
                      </td>
                      <td>
                        <strong>{s.weight_pct}%</strong>
                      </td>
                      <td>
                        {s.trades.toLocaleString()} ({s.active_days}d)
                      </td>
                      <td>
                        +{s.gross_bps.toFixed(1)} / -{s.cost_bps.toFixed(1)} /{' '}
                        <strong className="text-emerald">+{s.net_bps.toFixed(1)} bps</strong>
                      </td>
                      <td>{s.win_rate_pct.toFixed(1)}%</td>
                      <td>
                        +{s.train_t.toFixed(2)} /{' '}
                        <strong className="text-emerald">+{s.oos_t.toFixed(2)}</strong> / +
                        {s.full_t.toFixed(2)}
                      </td>
                      <td>
                        <strong className="text-emerald">{s.sharpe.toFixed(2)}</strong>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {activeAuditStrat && (
              <div className="wm-strat-drilldown">
                <div className="wm-drill-left">
                  <h3>
                    {activeAuditStrat.id}: {activeAuditStrat.name}
                  </h3>
                  <p className="wm-drill-p">
                    <strong>Universe:</strong> {activeAuditStrat.universe_rule}
                  </p>
                  <p className="wm-drill-p">
                    <strong>Trigger:</strong> {activeAuditStrat.signal_rule}
                  </p>
                  <p className="wm-drill-p">
                    <strong>IRA Execution &amp; Hedge:</strong> {activeAuditStrat.hedge_rule}
                  </p>
                  <p className="wm-drill-p">
                    <strong>Structural Counterparty:</strong> {activeAuditStrat.structural_why}
                  </p>
                  <div className="wm-placebo-callout">
                    <strong>Sledgehammer Audit Verdict:</strong> {activeAuditStrat.placebo_note}
                  </div>
                </div>
                <div className="wm-drill-right">
                  <h4>Recent Verified Historical Trades ({activeAuditStrat.id})</h4>
                  <table className="wm-mini-table tabular">
                    <thead>
                      <tr>
                        <th>Date</th>
                        <th>Symbol</th>
                        <th>Entry $</th>
                        <th>Metric</th>
                        <th>Gross</th>
                        <th>Net bps</th>
                      </tr>
                    </thead>
                    <tbody>
                      {activeAuditStrat.recent_trades.map((tr, idx) => (
                        <tr key={idx}>
                          <td>{tr.date}</td>
                          <td>
                            <strong>{tr.symbol}</strong>
                          </td>
                          <td>${tr.entry_px.toFixed(2)}</td>
                          <td>{tr.metric}</td>
                          <td>
                            {tr.gross_bps >= 0 ? '+' : ''}
                            {tr.gross_bps.toFixed(1)}
                          </td>
                          <td className={tr.net_bps >= 0 ? 'text-emerald' : 'text-rose'}>
                            <strong>
                              {tr.net_bps >= 0 ? '+' : ''}
                              {tr.net_bps.toFixed(1)}
                            </strong>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </section>

          {/* 3x3 Surviving Sleeve Correlation Matrix + SLS Case Study */}
          <div className="wm-two-col">
            <section className="wm-audit-card">
              <div className="panel-header">
                <h2>
                  Surviving 3-Sleeve Daily Net Return Correlation Matrix (Orthogonal Closing
                  Engines)
                </h2>
              </div>
              {sh?.corr_3x3 ? (
                <table className="wm-mini-table tabular">
                  <thead>
                    <tr>
                      <th>Surviving IRA Sleeve</th>
                      {Object.keys(sh.corr_3x3).map((k) => (
                        <th key={k}>{k}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {Object.keys(sh.corr_3x3).map((rk) => (
                      <tr key={rk}>
                        <td>
                          <strong>{rk}</strong>
                        </td>
                        {Object.keys(sh.corr_3x3).map((ck) => {
                          const v = sh.corr_3x3[rk]?.[ck] ?? 0;
                          return (
                            <td
                              key={ck}
                              style={{
                                color:
                                  rk === ck
                                    ? '#94a3b8'
                                    : Math.abs(v) < 0.18
                                    ? '#34d399'
                                    : '#fde68a',
                              }}
                            >
                              {v >= 0 ? '+' : ''}
                              {v.toFixed(3)}
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : null}
            </section>

            <section className="wm-audit-card">
              <div className="panel-header">
                <h2>
                  Forensic Case Study: {audit.sls_case_study.symbol} (
                    {audit.sls_case_study.company})
                </h2>
              </div>
              <div className="wm-sls-stats tabular">
                <div>
                  <span>SEC Filing:</span>
                  <strong>{audit.sls_case_study.filing}</strong>
                </div>
                <div>
                  <span>Trailing OCF:</span>
                  <strong className="text-rose">-$28.4M / yr</strong>
                </div>
                <div>
                  <span>09:30 Open Reclaim Rate:</span>
                  <strong className="text-amber">
                    {audit.sls_case_study.reclaim_open_win_rate_pct}% (Optical Illusion)
                  </strong>
                </div>
                <div>
                  <span>Net Expectancy (225d 5m):</span>
                  <strong className="text-rose">
                    {audit.sls_case_study.capped_target_net_bps} to{' '}
                    {audit.sls_case_study.hold_to_close_net_bps} bps/trade
                  </strong>
                </div>
                <div>
                  <span>1,533-Stock Habit Mining:</span>
                  <strong className="text-rose">
                    +{audit.sls_case_study.broad_habit_train_gross_bps} bps Train →{' '}
                    {audit.sls_case_study.broad_habit_oos_net_bps} bps OOS
                  </strong>
                </div>
              </div>
              <p className="wm-sls-verdict">{audit.sls_case_study.verdict}</p>
            </section>
          </div>

          {/* Graveyard of Rejected & Non-IRA Strategy Families */}
          <section className="wm-audit-card">
            <div className="panel-header">
              <h2>
                The Strategy Graveyard — Hypotheses Falsified or Disqualified Under IRA Long-Only
                Rules
              </h2>
            </div>
            <div className="wm-table-scroll">
              <table className="wm-table tabular">
                <thead>
                  <tr>
                    <th>Falsified / Disqualified Strategy Family</th>
                    <th>Specs Tested</th>
                    <th>Gross bps</th>
                    <th>Net bps</th>
                    <th>OOS t-stat</th>
                    <th>Empirical / IRA Post-Mortem</th>
                  </tr>
                </thead>
                <tbody>
                  {audit.graveyard.map((g, i) => (
                    <tr key={i} className="wm-row">
                      <td>
                        <strong>{g.family}</strong>
                      </td>
                      <td>{g.specs_tested.toLocaleString()}</td>
                      <td>
                        {g.gross_bps >= 0 ? '+' : ''}
                        {g.gross_bps.toFixed(1)} bps
                      </td>
                      <td className="text-rose">
                        <strong>
                          {g.net_bps >= 0 ? '+' : ''}
                          {g.net_bps.toFixed(1)} bps
                        </strong>
                      </td>
                      <td className="text-rose">{g.oos_t.toFixed(2)}</td>
                      <td className="wm-sec-cell">{g.why_failed}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </div>
      )}
    </div>
  );
};
