import React, { useMemo, useState } from 'react';
import {
  WholeMarketAuditStrategy,
  WholeMarketAuditSummary,
  WholeMarketCandidate,
  WholeMarketScanPayload,
} from '../types';

interface WholeMarketAuditViewProps {
  scan: WholeMarketScanPayload | null;
  audit: WholeMarketAuditSummary | null;
  bookCapital: number;
  onChangeBookCapital: (cap: number) => void;
  onRefreshScan: () => void;
  isRefreshing: boolean;
  autoPoll: boolean;
  onToggleAutoPoll: () => void;
  onCopyNotify?: (msg: string) => void;
}

type SubMode = 'SCANNER' | 'AUDIT';
type SleeveFilter = 'ALL' | 'S1' | 'S2' | 'S3' | 'S4' | 'S5';

const STATUS_LABELS: Record<WholeMarketCandidate['status'], string> = {
  SIGNAL_ACTIVE: 'SIGNAL ACTIVE',
  MORNING_COHORT_LIVE: 'MORNING LIVE',
  FORMING_IN_WINDOW: 'FORMING 15:55',
  PRE_3PM_WATCH: '3PM WATCH',
  NEAR_TRIGGER_WATCH: 'NEAR RADAR',
};

function formatAdtv(usd: number): string {
  if (usd >= 1e9) return `$${(usd / 1e9).toFixed(2)}B`;
  return `$${(usd / 1e6).toFixed(1)}M`;
}

export const WholeMarketAuditView: React.FC<WholeMarketAuditViewProps> = ({
  scan,
  audit,
  bookCapital,
  onChangeBookCapital,
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
  const [selectedAuditSleeve, setSelectedAuditSleeve] = useState<'S1' | 'S2' | 'S3' | 'S4' | 'S5'>('S1');

  const strategyMetaById = useMemo(() => {
    const map: Record<string, WholeMarketAuditStrategy> = {};
    if (audit?.strategies) {
      for (const s of audit.strategies) {
        map[s.id] = s;
      }
    }
    return map;
  }, [audit]);

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
        c.guild.toUpperCase().includes(q)
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
        (sleeveFilter === 'ALL' || c.sleeve_id === sleeveFilter)
    );
    if (activeCohort.length === 0) {
      if (onCopyNotify) {
        onCopyNotify('No active top-cohort signals to copy right now.');
      }
      return;
    }
    const joined = activeCohort.map((c) => c.copy_ticket).join('\n\n---\n\n');
    copyText(
      joined,
      `Copied ${activeCohort.length} multi-leg cohort order tickets to clipboard.`
    );
  };

  // Build SVG polyline paths for the 1Y compounded equity curve
  const chartGeometry = useMemo(() => {
    const pts = audit?.equity_curve || [];
    if (pts.length < 2) return null;
    const W = 860;
    const H = 220;
    const padL = 48;
    const padR = 18;
    const padT = 16;
    const padB = 26;
    const allVals: number[] = [];
    for (const p of pts) {
      allVals.push(p.port, p.s1, p.s2, p.s3, p.s4, p.s5);
    }
    const minV = Math.min(96, ...allVals);
    const maxV = Math.max(115, ...allVals);
    const spanV = Math.max(1, maxV - minV);
    const xAt = (i: number) =>
      padL + (i / (pts.length - 1)) * (W - padL - padR);
    const yAt = (v: number) =>
      padT + (1 - (v - minV) / spanV) * (H - padT - padB);

    const makePath = (getter: (p: (typeof pts)[number]) => number) =>
      pts
        .map((p, idx) => `${idx === 0 ? 'M' : 'L'}${xAt(idx).toFixed(1)},${yAt(getter(p)).toFixed(1)}`)
        .join(' ');

    const yTicks = [100, 115, 130, 145, 160].filter(
      (v) => v >= minV - 2 && v <= maxV + 5
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
      portPath: makePath((p) => p.port),
      s1Path: makePath((p) => p.s1),
      s2Path: makePath((p) => p.s2),
      s3Path: makePath((p) => p.s3),
      s4Path: makePath((p) => p.s4),
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

  return (
    <div className="wm-root">
      {/* ==================== TOP COMMAND & CLOCK HEADER ==================== */}
      <header className="wm-header">
        <div className="wm-title-block">
          <div className="wm-title-row">
            <h2>Whole-Market Multi-Strategy Scanner &amp; 10,526-Spec Audit</h2>
            {scan && (
              <span className="wm-latency-pill tabular">
                {scan.quotes_received.toLocaleString()} live US quotes ·{' '}
                {scan.scan_latency_ms.toFixed(0)}ms ({scan.feed_mode === 'CNBC_REALTIME_BATCH_REST' ? 'CNBC Live' : 'Fallback'})
              </span>
            )}
          </div>
          <p className="wm-subtitle tabular">
            Audited Universe: <strong>{scan?.total_audited_universe?.toLocaleString() || '4,688'}</strong> US Equities, ADRs, Biotechs &amp; ETPs · Causal t-1 Baseline:{' '}
            <strong>{scan?.as_of_prior_session || '2026-11-10'}</strong> · Updated:{' '}
            <strong>{scan?.fetched_at_et || 'Loading...'}</strong>
          </p>
        </div>

        <div className="wm-controls-row">
          {/* Sub-View Mode Switcher */}
          <div className="wm-subnav" role="tablist" aria-label="Whole Market Sub-Views">
            <button
              type="button"
              role="tab"
              aria-selected={subMode === 'SCANNER'}
              className={`wm-subnav-btn ${subMode === 'SCANNER' ? 'active' : ''}`}
              onClick={() => setSubMode('SCANNER')}
            >
              📡 Live 5-Sleeve Scanner ({scan?.total_active_or_forming ?? 0})
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={subMode === 'AUDIT'}
              className={`wm-subnav-btn ${subMode === 'AUDIT' ? 'active' : ''}`}
              onClick={() => setSubMode('AUDIT')}
            >
              📊 10,526-Strategy Audit &amp; Graveyard
            </button>
          </div>

          {/* Book Capital Sizer */}
          <div className="wm-cap-group tabular">
            <span className="wm-cap-label">Book Capital:</span>
            {[50000, 100000, 250000, 500000].map((cap) => (
              <button
                key={cap}
                type="button"
                className={`wm-cap-btn ${bookCapital === cap ? 'active' : ''}`}
                onClick={() => onChangeBookCapital(cap)}
              >
                ${cap / 1000}k
              </button>
            ))}
          </div>

          <button
            type="button"
            className={`btn-ghost-xs ${autoPoll ? 'active-pill' : ''}`}
            onClick={onToggleAutoPoll}
          >
            {autoPoll ? '⏱ Auto-Scan: ON (20s)' : '⏸ Auto-Scan: OFF'}
          </button>

          <button
            type="button"
            className="btn-primary-xs"
            disabled={isRefreshing}
            onClick={onRefreshScan}
          >
            {isRefreshing ? 'Scanning ~4,000 Tickers...' : '⟳ Scan Whole Market Now'}
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
              <span className="wm-spy-lbl">SPY Live:</span>{' '}
              <strong>${scan.spy.last.toFixed(2)}</strong>{' '}
              <span className={scan.spy.day_chg_pct >= 0 ? 'text-emerald' : 'text-rose'}>
                ({scan.spy.day_chg_pct >= 0 ? '+' : ''}
                {scan.spy.day_chg_pct.toFixed(2)}% Day ·{' '}
                {scan.spy.intraday_chg_pct >= 0 ? '+' : ''}
                {scan.spy.intraday_chg_pct.toFixed(2)}% vs Open)
              </span>
            </div>
            <div className="wm-spy-sub">
              Open ${scan.spy.open.toFixed(2)} · Range ${scan.spy.low.toFixed(2)}–$
              {scan.spy.high.toFixed(2)} · 15:00 Anchor:{' '}
              <strong>
                {scan.spy.p1500 ? `$${scan.spy.p1500.toFixed(2)} (Locked)` : 'Locks @ 15:00 ET'}
              </strong>
            </div>
          </div>
        </section>
      )}

      {/* ========================================================================
          SUB-VIEW 1: LIVE MULTI-STRATEGY WHOLE-MARKET SCANNER (S1 - S5)
         ======================================================================== */}
      {subMode === 'SCANNER' && (
        <>
          {/* 6 Sleeve Selector Cards (ALL + S1..S5) */}
          <section className="wm-sleeve-strip" aria-label="Strategy Sleeve Filters">
            <button
              type="button"
              className={`wm-sleeve-card ${sleeveFilter === 'ALL' ? 'selected' : ''}`}
              onClick={() => setSleeveFilter('ALL')}
            >
              <div className="wm-sc-top">
                <span className="wm-sc-id">ALL 5 SLEEVES</span>
                <span className="wm-sc-wt tabular">100% Book</span>
              </div>
              <div className="wm-sc-name">Combined Prop Portfolio</div>
              <div className="wm-sc-metrics tabular">
                <span className="text-emerald">OOS Sharpe 5.50</span>
                <span>·</span>
                <span>{scan?.total_active_or_forming ?? 0} Active/Forming</span>
              </div>
              <div className="wm-sc-bells tabular">09:30 Open &amp; 15:55 Close Bells</div>
            </button>

            {(scan?.sleeves || []).map((sl) => {
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
                      {sl.id} · {sl.direction}
                    </span>
                    <span className="wm-sc-wt tabular">
                      {(sl.weight * 100).toFixed(0)}% ($
                      {(sl.sleeve_capital_usd / 1000).toFixed(0)}k)
                    </span>
                  </div>
                  <div className="wm-sc-name">{sl.name}</div>
                  <div className="wm-sc-metrics tabular">
                    <strong className={sl.qualifying_count > 0 ? 'text-emerald' : ''}>
                      {sl.qualifying_count} Live/Forming
                    </strong>
                    <span>· {sl.near_count} Radar</span>
                    {sl.today_cohort_mtm_bps !== null && (
                      <span
                        className={
                          sl.today_cohort_mtm_bps >= 0 ? 'text-emerald' : 'text-rose'
                        }
                      >
                        · {sl.today_cohort_mtm_bps >= 0 ? '+' : ''}
                        {sl.today_cohort_mtm_bps.toFixed(0)} bps MTM
                      </span>
                    )}
                  </div>
                  <div className="wm-sc-bells tabular">
                    {sl.entry_bell} → {sl.exit_bell} · Sh {sl.audited_sharpe.toFixed(2)}
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
                placeholder="Filter ticker, company, industry, or guild (e.g. SOXS, Biotech, Semis)..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
              />
              <label className="wm-checkbox-lbl">
                <input
                  type="checkbox"
                  checked={includeNear}
                  onChange={(e) => setIncludeNear(e.target.checked)}
                />
                <span>Include Closest Near-Trigger Radar Candidates ({scan?.near_candidates?.length ?? 0})</span>
              </label>
            </div>
            <div className="wm-filter-right">
              <button
                type="button"
                className="btn-secondary-xs"
                onClick={handleCopyAllCohortTickets}
              >
                📋 Copy All Top-Cohort Tickets
              </button>
            </div>
          </div>

          {/* Main Scanner Desk Split Grid */}
          <div className="wm-desk-grid">
            {/* LEFT: Live Multi-Strategy Candidates Table */}
            <section className="wm-table-panel">
              <div className="panel-header">
                <h2>
                  Live Signals &amp; Forming Setups ({visibleCandidates.length})
                </h2>
                <span className="panel-subtitle tabular">
                  Ranked by Sleeve Priority · 0.5% ADTV Cap Enforced
                </span>
              </div>

              {visibleCandidates.length === 0 ? (
                <div className="wm-empty-box">
                  No qualifying or near-trigger candidates match the current filter. Enable{' '}
                  <strong>Include Closest Near-Trigger Radar Candidates</strong> or select{' '}
                  <strong>ALL 5 SLEEVES</strong>.
                </div>
              ) : (
                <div className="wm-table-scroll">
                  <table className="wm-table tabular">
                    <thead>
                      <tr>
                        <th>Sleeve</th>
                        <th>Status</th>
                        <th>Symbol / Guild</th>
                        <th>Last / Day %</th>
                        <th>Primary Signal Metric</th>
                        <th>Secondary Gate</th>
                        <th>Clip &amp; SPY Hedge</th>
                        <th>RT Cost</th>
                        <th>Today MTM</th>
                      </tr>
                    </thead>
                    <tbody>
                      {visibleCandidates.map((c) => {
                        const isSelected = selectedCandidate?.id === c.id;
                        const isNear =
                          c.status === 'NEAR_TRIGGER_WATCH' ||
                          c.status === 'PRE_3PM_WATCH';
                        return (
                          <tr
                            key={c.id}
                            className={`wm-row ${isSelected ? 'selected' : ''} ${
                              isNear ? 'wm-row-near' : 'wm-row-live'
                            }`}
                            onClick={() => setSelectedCandId(c.id)}
                          >
                            <td>
                              <span
                                className={`wm-sleeve-pill sleeve-${c.sleeve_id.toLowerCase()}`}
                              >
                                {c.sleeve_id} · {c.direction}
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
                              {c.catalyst_warning && (
                                <div className="wm-cat-warn">⚠️ &gt;8% Gap Check News</div>
                              )}
                            </td>
                            <td>
                              <div className="wm-sym-cell">
                                <strong>{c.symbol}</strong>
                                <span className="wm-adtv">{formatAdtv(c.adtv20_usd)}</span>
                              </div>
                              <div className="wm-ind-sub">
                                {c.guild} · {c.industry || c.name}
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
                            <td className="wm-sec-cell">{c.secondary_label}</td>
                            <td>
                              <div>
                                <strong>
                                  {c.direction === 'SHORT' ? 'SHORT' : 'BUY'}{' '}
                                  {c.shares.toLocaleString()} sh
                                </strong>{' '}
                                (${c.notional_usd.toLocaleString(undefined, { maximumFractionDigits: 0 })})
                              </div>
                              <div className="wm-hedge-sub">
                                {c.spy_hedge_shares > 0
                                  ? `${c.hedge_side} ${c.spy_hedge_shares} SPY (β=${c.beta_spy.toFixed(2)})`
                                  : 'Unhedged Sleeve'}
                              </div>
                            </td>
                            <td>{c.rt_cost_bps.toFixed(1)} bps</td>
                            <td>
                              {c.live_mtm_bps !== null ? (
                                <div>
                                  <strong
                                    className={
                                      c.live_mtm_bps >= 0 ? 'text-emerald' : 'text-rose'
                                    }
                                  >
                                    {c.live_mtm_bps >= 0 ? '+' : ''}
                                    {c.live_mtm_bps.toFixed(0)} bps
                                  </strong>
                                  {c.live_mtm_usd !== null && (
                                    <div className="wm-muted-sm">
                                      ({c.live_mtm_usd >= 0 ? '+' : ''}$
                                      {c.live_mtm_usd.toFixed(0)})
                                      {c.stopped_out ? ' · Stopped' : ''}
                                    </div>
                                  )}
                                </div>
                              ) : (
                                <span className="wm-muted-sm">15:55 Entry</span>
                              )}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </section>

            {/* RIGHT: Selected Signal Inspector & Multi-Leg Order Ticket */}
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

                  {selectedCandidate.catalyst_warning && (
                    <div className="wm-catalyst-alert">
                      <strong>⚠️ Large Overnight Gap ({selectedCandidate.gap_pct.toFixed(1)}%):</strong>{' '}
                      Verify {selectedCandidate.symbol} did not report earnings or M&amp;A
                      before shorting S1! True earnings PEAD shocks should be skipped.
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
                  <div className="wm-section-lbl">Causal Strategy Gate Checklist</div>
                  <div className="wm-gates-list tabular">
                    {selectedCandidate.gates.map((g, i) => (
                      <div
                        key={i}
                        className={`wm-gate-item ${g.pass ? 'gate-pass' : 'gate-watch'}`}
                      >
                        <div className="wm-gate-top">
                          <strong>
                            {g.pass ? '✅' : '⏳'} {g.name}
                          </strong>
                          <span className="wm-gate-req">{g.required}</span>
                        </div>
                        <div className="wm-gate-obs">{g.observed}</div>
                      </div>
                    ))}
                  </div>

                  {/* Multi-Leg Position & Hedge Sizer */}
                  <div className="wm-section-lbl">
                    Multi-Leg Order Plan (${(bookCapital / 1000).toFixed(0)}k Book)
                  </div>
                  <div className="wm-plan-grid tabular">
                    <div>
                      <span>Leg 1 (Primary):</span>
                      <strong
                        className={
                          selectedCandidate.direction === 'SHORT'
                            ? 'text-rose'
                            : 'text-emerald'
                        }
                      >
                        {selectedCandidate.direction === 'SHORT' ? 'SHORT' : 'BUY'}{' '}
                        {selectedCandidate.shares.toLocaleString()} sh @ $
                        {selectedCandidate.ref_price.toFixed(2)}
                      </strong>
                    </div>
                    <div>
                      <span>Leg 2 (SPY Hedge):</span>
                      <strong>
                        {selectedCandidate.spy_hedge_shares > 0
                          ? `${selectedCandidate.hedge_side} ${selectedCandidate.spy_hedge_shares} sh SPY`
                          : 'None (Unhedged)'}
                      </strong>
                    </div>
                    <div>
                      <span>Target Notional:</span>
                      <strong>
                        ${selectedCandidate.notional_usd.toLocaleString()} (ADTV{' '}
                        {formatAdtv(selectedCandidate.adtv20_usd)})
                      </strong>
                    </div>
                    <div>
                      <span>Catastrophe Stop:</span>
                      <strong className="text-rose">
                        ${selectedCandidate.stop_price.toFixed(2)}
                      </strong>
                    </div>
                    <div>
                      <span>Entry Window:</span>
                      <strong>{selectedCandidate.entry_bell}</strong>
                    </div>
                    <div>
                      <span>Mandatory Exit:</span>
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
                      onClick={() =>
                        copyText(
                          selectedCandidate.copy_ticket,
                          `Copied ${selectedCandidate.sleeve_id} multi-leg ticket for ${selectedCandidate.symbol}`
                        )
                      }
                    >
                      📋 Copy Multi-Leg Order Ticket
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
                  Select a candidate row on the left to inspect its causal gates and multi-leg
                  SPY-hedged order ticket.
                </div>
              )}
            </aside>
          </div>
        </>
      )}

      {/* ========================================================================
          SUB-VIEW 2: 10,526-STRATEGY EMPIRICAL AUDIT, PORTFOLIO PROOF & GRAVEYARD
         ======================================================================== */}
      {subMode === 'AUDIT' && audit && (
        <div className="wm-audit-container">
          {/* Portfolio Performance Windows */}
          <section className="wm-kpi-grid tabular">
            {audit.portfolio_windows.map((w, idx) => (
              <div key={idx} className="wm-kpi-card">
                <div className="wm-kpi-label">{w.label}</div>
                <div className="wm-kpi-main">
                  <span className="wm-kpi-ret text-emerald">
                    +{w.compounded_return_pct.toFixed(1)}%
                  </span>
                  <span className="wm-kpi-sh">Sharpe {w.sharpe.toFixed(2)}</span>
                </div>
                <div className="wm-kpi-sub">
                  t = +{w.daily_t_stat.toFixed(2)} · Max DD {w.max_drawdown_pct.toFixed(2)}% ·{' '}
                  {w.winning_months_pct.toFixed(0)}% Winning Months ({w.days}d)
                </div>
              </div>
            ))}
            <div className="wm-kpi-card">
              <div className="wm-kpi-label">Multiple-Testing &amp; Orthogonality Proof</div>
              <div className="wm-kpi-main">
                <span className="wm-kpi-ret">
                  DSR {audit.overview.deflated_sharpe_ratio.toFixed(3)}
                </span>
                <span className="wm-kpi-sh">
                  Mean ρ = +{audit.overview.mean_pairwise_corr.toFixed(3)}
                </span>
              </div>
              <div className="wm-kpi-sub">
                {audit.overview.surviving_specs} Survivors /{' '}
                {audit.overview.total_specs_tested.toLocaleString()} Tested (p{' '}
                {audit.overview.deflated_sharpe_pvalue}) ·{' '}
                {(audit.overview.daily_stock_days / 1e6).toFixed(2)}M Stock-Days
              </div>
            </div>
          </section>

          {/* Compounded Equity Curve SVG */}
          {chartGeometry && (
            <section className="wm-audit-card">
              <div className="panel-header">
                <h2>
                  Trailing 1-Year Net Compounded Equity Curve (Base 100 · After Full Reg NMS
                  Spread, Impact &amp; Borrow)
                </h2>
                <div className="wm-legend tabular">
                  <span className="leg-port">━ Combined 5-Sleeve Book (+56.1%)</span>
                  <span className="leg-s1">━ S1 Gap Short</span>
                  <span className="leg-s2">━ S2 10b-18 MOC</span>
                  <span className="leg-s3">━ S3 Lev-ETP</span>
                  <span className="leg-s4">━ S4 Biotech</span>
                  <span className="leg-s5">━ S5 S&amp;P 500</span>
                </div>
              </div>
              <div className="wm-svg-wrap">
                <svg
                  viewBox={`0 0 ${chartGeometry.W} ${chartGeometry.H}`}
                  className="wm-eq-svg"
                  role="img"
                  aria-label="5-Strategy Net Compounded Equity Curve"
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
                  <path d={chartGeometry.s1Path} fill="none" stroke="#f43f5e" strokeWidth="1.3" opacity="0.75" />
                  <path d={chartGeometry.s2Path} fill="none" stroke="#38bdf8" strokeWidth="1.3" opacity="0.75" />
                  <path d={chartGeometry.s3Path} fill="none" stroke="#a855f7" strokeWidth="1.3" opacity="0.75" />
                  <path d={chartGeometry.s4Path} fill="none" stroke="#f59e0b" strokeWidth="1.3" opacity="0.75" />
                  <path d={chartGeometry.s5Path} fill="none" stroke="#14b8a6" strokeWidth="1.3" opacity="0.75" />
                  <path d={chartGeometry.portPath} fill="none" stroke="#10b981" strokeWidth="2.8" />
                  <text x={chartGeometry.padL} y={chartGeometry.H - 6} fill="#64748b" fontSize="10">
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

          {/* Top 5 Surviving Strategies Leaderboard */}
          <section className="wm-audit-card">
            <div className="panel-header">
              <h2>
                The 5 Surviving Whole-Market Strategies (Out of 10,526 Tested — Click Any Row
                to Inspect Trades &amp; Placebo Proof)
              </h2>
            </div>
            <div className="wm-table-scroll">
              <table className="wm-table tabular">
                <thead>
                  <tr>
                    <th>Sleeve</th>
                    <th>Strategy &amp; Clock Window</th>
                    <th>Wt</th>
                    <th>Trades (Days)</th>
                    <th>Gross / Cost / Net</th>
                    <th>Win %</th>
                    <th>Train t / OOS t / Full t</th>
                    <th>Sharpe</th>
                    <th>Ex-Top3 Sym</th>
                    <th>Ex-Top3 Day</th>
                    <th>2x Cost Sh</th>
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
                      <td>{s.weight_pct}%</td>
                      <td>
                        {s.trades.toLocaleString()} ({s.active_days}d)
                      </td>
                      <td>
                        +{s.gross_bps.toFixed(1)} / -{s.cost_bps.toFixed(1)} /{' '}
                        <strong className="text-emerald">+{s.net_bps.toFixed(1)} bps</strong>
                      </td>
                      <td>{s.win_rate_pct.toFixed(1)}%</td>
                      <td>
                        +{s.train_t.toFixed(2)} / <strong>+{s.oos_t.toFixed(2)}</strong> / +
                        {s.full_t.toFixed(2)}
                      </td>
                      <td>
                        <strong className="text-emerald">{s.sharpe.toFixed(2)}</strong>
                      </td>
                      <td>{s.ex_top3_tickers_sharpe.toFixed(2)}</td>
                      <td>{s.ex_top3_days_sharpe.toFixed(2)}</td>
                      <td>{s.friction_2x_sharpe.toFixed(2)}</td>
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
                    <strong>Execution &amp; Hedge:</strong> {activeAuditStrat.hedge_rule}
                  </p>
                  <p className="wm-drill-p">
                    <strong>Structural Counterparty:</strong> {activeAuditStrat.structural_why}
                  </p>
                  <div className="wm-placebo-callout">
                    <strong>Placebo / Falsification Check:</strong>{' '}
                    {activeAuditStrat.placebo_note}
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

          {/* 5x5 Correlation Matrix + SLS Case Study */}
          <div className="wm-two-col">
            <section className="wm-audit-card">
              <div className="panel-header">
                <h2>5x5 Daily Net Return Correlation Matrix (Mean Pairwise ρ = +0.024)</h2>
              </div>
              <table className="wm-mini-table tabular">
                <thead>
                  <tr>
                    <th>Sleeve</th>
                    {(['S1', 'S2', 'S3', 'S4', 'S5'] as const).map((k) => (
                      <th key={k}>{k}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {(['S1', 'S2', 'S3', 'S4', 'S5'] as const).map((rk) => (
                    <tr key={rk}>
                      <td>
                        <strong>{rk}</strong>
                      </td>
                      {(['S1', 'S2', 'S3', 'S4', 'S5'] as const).map((ck) => {
                        const v = audit.correlation_matrix[rk]?.[ck] ?? 0;
                        return (
                          <td
                            key={ck}
                            style={{
                              color:
                                rk === ck
                                  ? '#94a3b8'
                                  : Math.abs(v) < 0.08
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

          {/* Graveyard of Rejected Strategy Families */}
          <section className="wm-audit-card">
            <div className="panel-header">
              <h2>
                The Strategy Graveyard — Major Hypotheses Falsified Across 10,521 Rejected
                Specs
              </h2>
            </div>
            <div className="wm-table-scroll">
              <table className="wm-table tabular">
                <thead>
                  <tr>
                    <th>Falsified Strategy Family</th>
                    <th>Specs Tested</th>
                    <th>Gross bps</th>
                    <th>Net bps</th>
                    <th>OOS t-stat</th>
                    <th>Empirical Post-Mortem (Why It Died)</th>
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
