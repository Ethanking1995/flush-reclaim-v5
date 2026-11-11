import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  BrokerPreview,
  CandidateCategory,
  CandidateRow,
  DashboardSnapshot,
  OrderPlan,
  PositionRecord,
  SymbolChartPayload,
} from './types';
import { SymbolChart } from './components/SymbolChart';

type NavTab = 'TRADE' | 'JOURNAL' | 'SETTINGS';

const CATEGORY_LABELS: Record<CandidateCategory, string> = {
  READY_FOR_REVIEW: 'Ready for review',
  WATCHING: 'Watching',
  BLOCKED: 'Blocked',
  DATA_UNAVAILABLE: 'Data unavailable',
};

const CATEGORY_ORDER: CandidateCategory[] = [
  'READY_FOR_REVIEW',
  'WATCHING',
  'BLOCKED',
  'DATA_UNAVAILABLE',
];

export const App: React.FC = () => {
  const initialParams = useMemo(
    () => new URLSearchParams(window.location.search),
    []
  );
  const [snapshot, setSnapshot] = useState<DashboardSnapshot | null>(null);
  const [selectedSymbol, setSelectedSymbol] = useState<string>(
    (initialParams.get('symbol') || 'FISV').toUpperCase()
  );
  const [chartData, setChartData] = useState<SymbolChartPayload | null>(null);
  const [activeTab, setActiveTab] = useState<NavTab>(
    ((initialParams.get('tab') || 'TRADE').toUpperCase() as NavTab) || 'TRADE'
  );
  const [showDiagnostics, setShowDiagnostics] = useState<boolean>(
    initialParams.get('diag') === '1'
  );
  const [mobileTicketOpen, setMobileTicketOpen] = useState<boolean>(
    initialParams.get('drawer') === 'open'
  );
  const [sseConnected, setSseConnected] = useState<boolean>(false);
  const [bannerNotice, setBannerNotice] = useState<{
    kind: 'info' | 'success' | 'warning' | 'error';
    text: string;
  } | null>(null);

  // In-flight local draft state for Order Review Ticket (never overwritten by background refreshes)
  const [draftSymbol, setDraftSymbol] = useState<string>('');
  const [draftAllocation, setDraftAllocation] = useState<string>('150000');
  const [draftLimitPrice, setDraftLimitPrice] = useState<string>('62.72');
  const [isDraftDirty, setIsDraftDirty] = useState<boolean>(false);
  const [unlockManualOverride, setUnlockManualOverride] = useState<boolean>(false);
  const [draftManualStop, setDraftManualStop] = useState<string>('');
  const [draftManualLeg1, setDraftManualLeg1] = useState<string>('');
  const [draftManualLeg2, setDraftManualLeg2] = useState<string>('');

  // Confirmation modal & simulation options
  const [confirmModalOpen, setConfirmModalOpen] = useState<boolean>(false);
  const [modalConfirmChecked, setModalConfirmChecked] = useState<boolean>(false);
  const [simulatePreviewError, setSimulatePreviewError] = useState<boolean>(false);
  const [simulateTimeout, setSimulateTimeout] = useState<boolean>(false);
  const [simulatePartialFill, setSimulatePartialFill] = useState<boolean>(false);
  const [busyAction, setBusyAction] = useState<string | null>(null);

  // Journal filter state
  const [journalFilter, setJournalFilter] = useState<string>('ALL');
  const [manualSym, setManualSym] = useState<string>('FISV');
  const [manualEntry, setManualEntry] = useState<string>('62.72');
  const [manualStop, setManualStop] = useState<string>('63.50');
  const [manualLeg1, setManualLeg1] = useState<string>('63.18');
  const [manualPnl, setManualPnl] = useState<string>('3000');
  const [manualNotes, setManualNotes] = useState<string>(
    'User-reported trade note (unreconciled with broker).'
  );

  const selectedSymbolRef = useRef<string>(selectedSymbol);
  selectedSymbolRef.current = selectedSymbol;

  const fetchSnapshot = useCallback(async () => {
    try {
      const res = await fetch('/api/snapshot');
      if (res.ok) {
        const data: DashboardSnapshot = await res.json();
        setSnapshot(data);
      }
    } catch {
      // handled by SSE reconnect / status indicator
    }
  }, []);

  const fetchChart = useCallback(async (sym: string) => {
    try {
      const res = await fetch(`/api/chart/${encodeURIComponent(sym)}`);
      if (res.ok) {
        const data: SymbolChartPayload = await res.json();
        setChartData(data);
      }
    } catch {
      // ignore transient error
    }
  }, []);

  // Connect to SSE stream with automatic snapshot recovery on connect/reconnect
  useEffect(() => {
    fetchSnapshot();
    if (initialParams.get('nosse') === '1') {
      setSseConnected(true);
      return;
    }
    let es: EventSource | null = null;
    let reconnectTimer: number | null = null;

    const connectSse = () => {
      es = new EventSource('/api/stream');
      es.onopen = () => {
        setSseConnected(true);
      };
      es.onmessage = (evt) => {
        try {
          const payload = JSON.parse(evt.data);
          if (payload.snapshot) {
            setSnapshot(payload.snapshot);
          } else {
            fetchSnapshot();
          }
          fetchChart(selectedSymbolRef.current);
        } catch {
          // ignore malformed frame
        }
      };
      es.onerror = () => {
        setSseConnected(false);
        if (es) {
          es.close();
        }
        reconnectTimer = window.setTimeout(() => {
          fetchSnapshot();
          connectSse();
        }, 3000);
      };
    };

    connectSse();
    return () => {
      if (es) es.close();
      if (reconnectTimer) window.clearTimeout(reconnectTimer);
    };
  }, [fetchSnapshot, fetchChart]);

  // Fetch chart whenever selectedSymbol changes
  useEffect(() => {
    if (selectedSymbol) {
      fetchChart(selectedSymbol);
    }
  }, [selectedSymbol, fetchChart]);

  const selectedCandidate: CandidateRow | null = useMemo(() => {
    if (!snapshot) return null;
    return (
      snapshot.candidates.find((c) => c.symbol === selectedSymbol) ||
      snapshot.candidates[0] ||
      null
    );
  }, [snapshot, selectedSymbol]);

  const currentPlan: OrderPlan | null = useMemo(() => {
    if (!snapshot || !selectedCandidate) return null;
    return snapshot.order_plans_by_symbol[selectedCandidate.symbol] || null;
  }, [snapshot, selectedCandidate]);

  const activePreview: BrokerPreview | null = useMemo(() => {
    if (!snapshot || !currentPlan || !currentPlan.active_preview_id) return null;
    return snapshot.broker_previews_by_id[currentPlan.active_preview_id] || null;
  }, [snapshot, currentPlan]);

  // Sync draft fields ONLY when switching symbols or when not dirty
  useEffect(() => {
    if (!currentPlan) return;
    if (draftSymbol !== currentPlan.symbol) {
      setDraftSymbol(currentPlan.symbol);
      setDraftAllocation(String(currentPlan.allocation_usd));
      setDraftLimitPrice(currentPlan.limit_price.toFixed(2));
      setDraftManualStop(currentPlan.stop_price.toFixed(2));
      setDraftManualLeg1(currentPlan.leg1_target.toFixed(2));
      setDraftManualLeg2(currentPlan.leg2_target.toFixed(2));
      setUnlockManualOverride(Boolean(currentPlan.is_manual_override));
      setIsDraftDirty(false);
    } else if (!isDraftDirty) {
      setDraftAllocation(String(currentPlan.allocation_usd));
      setDraftLimitPrice(currentPlan.limit_price.toFixed(2));
      setDraftManualStop(currentPlan.stop_price.toFixed(2));
      setDraftManualLeg1(currentPlan.leg1_target.toFixed(2));
      setDraftManualLeg2(currentPlan.leg2_target.toFixed(2));
    }
  }, [currentPlan, draftSymbol, isDraftDirty]);

  // Live client-side draft preview math while typing (before saving to backend)
  const liveDraftPreviewMath = useMemo(() => {
    const alloc = parseFloat(draftAllocation) || 0;
    const lim = parseFloat(draftLimitPrice) || 0;
    const stp = unlockManualOverride
      ? parseFloat(draftManualStop) || 0
      : currentPlan?.stop_price || 0;
    if (alloc <= 0 || lim <= 0) {
      return {
        shares: 0,
        q1: 0,
        q2: 0,
        notional: 0,
        riskUsd: 0,
        riskPct: 0,
      };
    }
    const maxCap = Number(snapshot?.settings?.max_share_cap || 3000);
    const shares = Math.min(Math.max(1, Math.floor(alloc / lim)), maxCap);
    const q1 = Math.floor(shares / 2);
    const q2 = shares - q1;
    const notional = shares * lim;
    const riskUsd = shares * Math.max(0, lim - stp);
    const riskPct = lim > 0 ? (Math.abs(lim - stp) / lim) * 100 : 0;
    return { shares, q1, q2, notional, riskUsd, riskPct };
  }, [
    draftAllocation,
    draftLimitPrice,
    draftManualStop,
    unlockManualOverride,
    currentPlan,
    snapshot,
  ]);

  const postApi = async (path: string, body: Record<string, unknown> = {}) => {
    const res = await fetch(path, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-FlushReclaim-Confirm': 'true',
      },
      body: JSON.stringify(body),
    });
    const rawText = await res.text();
    let data: Record<string, any> = {};
    if (rawText) {
      try {
        data = JSON.parse(rawText);
      } catch {
        throw new Error(
          res.ok
            ? 'Received invalid JSON response from local server.'
            : `Server error (${res.status}): ${rawText.slice(0, 180)}`
        );
      }
    }
    if (!res.ok) {
      throw new Error(String(data.detail || `Request failed (${res.status})`));
    }
    if (data.snapshot) {
      setSnapshot(data.snapshot);
    }
    return data;
  };

  const handleSelectSymbol = (sym: string, openDrawerOnMobile = false) => {
    setSelectedSymbol(sym);
    if (openDrawerOnMobile) {
      setMobileTicketOpen(true);
    }
  };

  const handleSavePlanEdits = async (e?: React.FormEvent) => {
    if (e) e.preventDefault();
    if (!currentPlan) return;
    setBusyAction('SAVE_PLAN');
    try {
      const payload: Record<string, unknown> = {
        plan_id: currentPlan.plan_id,
        symbol: currentPlan.symbol,
        allocation_usd: parseFloat(draftAllocation),
        limit_price: parseFloat(draftLimitPrice),
      };
      if (unlockManualOverride) {
        payload.manual_stop_override = parseFloat(draftManualStop);
        payload.manual_leg1_override = parseFloat(draftManualLeg1);
        payload.manual_leg2_override = parseFloat(draftManualLeg2);
      }
      const res = await postApi('/api/plans/update', payload);
      setIsDraftDirty(false);
      setBannerNotice({
        kind: 'info',
        text: `Updated ${currentPlan.symbol} Order Plan to v${res.plan.plan_version} (${res.plan.shares.toLocaleString()} sh @ $${res.plan.limit_price.toFixed(2)}). Prior previews invalidated.`,
      });
      fetchChart(currentPlan.symbol);
    } catch (err: unknown) {
      setBannerNotice({
        kind: 'error',
        text: err instanceof Error ? err.message : 'Failed to update order plan.',
      });
    } finally {
      setBusyAction(null);
    }
  };

  const handleRunPreview = async () => {
    if (!currentPlan) return;
    if (isDraftDirty) {
      await handleSavePlanEdits();
    }
    setBusyAction('PREVIEW');
    try {
      const latestPlan =
        snapshot?.order_plans_by_symbol[currentPlan.symbol] || currentPlan;
      const res = await postApi('/api/previews/create', {
        plan_id: latestPlan.plan_id,
        plan_version: latestPlan.plan_version,
        simulate_broker_error: simulatePreviewError,
      });
      if (res.preview.status === 'FAILED_REJECTED') {
        setBannerNotice({
          kind: 'error',
          text: res.preview.message,
        });
      } else {
        setBannerNotice({
          kind: 'success',
          text: `Preview verified (${res.preview.preview_id}) for ${latestPlan.symbol} Plan v${latestPlan.plan_version} [${res.preview.validation_mode}].`,
        });
      }
    } catch (err: unknown) {
      setBannerNotice({
        kind: 'error',
        text: err instanceof Error ? err.message : 'Preview failed.',
      });
    } finally {
      setBusyAction(null);
    }
  };

  const handleConfirmSubmitOrder = async () => {
    if (!currentPlan || !activePreview) return;
    setBusyAction('SUBMIT');
    try {
      const idemKey = `ui-${currentPlan.plan_id}-v${currentPlan.plan_version}-${activePreview.preview_id}`;
      const res = await postApi('/api/orders/submit', {
        plan_id: currentPlan.plan_id,
        plan_version: currentPlan.plan_version,
        preview_id: activePreview.preview_id,
        idempotency_key: idemKey,
        confirmed_by_user: modalConfirmChecked,
        simulate_timeout: simulateTimeout,
        simulate_partial_fill_qty: simulatePartialFill
          ? Math.max(2, Math.floor(currentPlan.shares * 0.5))
          : null,
      });
      setConfirmModalOpen(false);
      setModalConfirmChecked(false);
      const intentState = res.result?.intent?.order_state;
      if (intentState === 'OUTCOME_UNCERTAIN_RECONCILING') {
        setBannerNotice({
          kind: 'warning',
          text: `Order submission timed out on ${currentPlan.symbol} — entered OUTCOME_UNCERTAIN_RECONCILING. Retry is blocked until account reconciliation completes.`,
        });
      } else {
        setBannerNotice({
          kind: 'success',
          text: `Order submitted & protected for ${currentPlan.symbol}: ${res.result?.intent?.filled_qty?.toLocaleString() || currentPlan.shares.toLocaleString()} sh (${intentState}).`,
        });
      }
      fetchChart(currentPlan.symbol);
    } catch (err: unknown) {
      setBannerNotice({
        kind: 'error',
        text: err instanceof Error ? err.message : 'Order submission failed.',
      });
    } finally {
      setBusyAction(null);
    }
  };

  const handleCopyText = async (text: string, label: string, symbol: string) => {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // Fallback for headless/restricted clipboard
    }
    await postApi('/api/handoff/open', { symbol, action: label });
    setBannerNotice({
      kind: 'info',
      text: `Copied ${label} for ${symbol} to clipboard (no broker order submitted).`,
    });
  };

  const handleOpenTastytrade = async (plan: OrderPlan) => {
    const res = await postApi('/api/handoff/open', {
      symbol: plan.symbol,
      action: 'OPEN_TASTYTRADE',
    });
    const url =
      res.handoff_result?.handoff?.destination_url ||
      plan.broker_handoff_link.destination_url;
    window.open(url, '_blank', 'noopener,noreferrer');
    setBannerNotice({
      kind: 'info',
      text: `Opened Tastytrade web platform for ${plan.symbol}. Note: Tastytrade web URLs do not prefill OTOCO brackets and this action never marks an order submitted or filled.`,
    });
  };

  const handleUpdateSettings = async (updates: Record<string, unknown>) => {
    try {
      await postApi('/api/settings', updates);
      if (updates.fixture_mode) {
        fetchChart(selectedSymbol);
      }
    } catch (err: unknown) {
      setBannerNotice({
        kind: 'error',
        text: err instanceof Error ? err.message : 'Failed to update settings.',
      });
    }
  };

  const handleLeg1Exit = async (positionId: string, symbol: string) => {
    try {
      await postApi(`/api/positions/${encodeURIComponent(positionId)}/leg1-exit`);
      setBannerNotice({
        kind: 'success',
        text: `${symbol}: Leg 1 (50%) exited at VWAP target; Tranche B stop moved to breakeven and confirmed covering remaining shares.`,
      });
      fetchChart(symbol);
    } catch (err: unknown) {
      setBannerNotice({
        kind: 'error',
        text: err instanceof Error ? err.message : 'Leg 1 transition failed.',
      });
    }
  };

  const handleReconcile = async (simulateUnprotected = false) => {
    try {
      await postApi('/api/reconcile', { simulate_unprotected: simulateUnprotected });
      setBannerNotice({
        kind: simulateUnprotected ? 'error' : 'success',
        text: simulateUnprotected
          ? 'Simulated unprotected strategy position — new submissions blocked until reconciled.'
          : 'Broker & local state reconciliation completed cleanly.',
      });
    } catch (err: unknown) {
      setBannerNotice({
        kind: 'error',
        text: err instanceof Error ? err.message : 'Reconciliation failed.',
      });
    }
  };

  const handleAddManualTrade = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await postApi('/api/positions/manual', {
        symbol: manualSym,
        reported_entry_price: parseFloat(manualEntry),
        reported_stop_price: parseFloat(manualStop) || null,
        reported_leg1_exit_price: parseFloat(manualLeg1) || null,
        reported_approx_pnl_usd: parseFloat(manualPnl) || null,
        notes: manualNotes,
      });
      setBannerNotice({
        kind: 'info',
        text: `Recorded user-reported manual trade note for ${manualSym.toUpperCase()} (labeled MANUAL_USER_REPORTED / UNRECONCILED).`,
      });
    } catch (err: unknown) {
      setBannerNotice({
        kind: 'error',
        text: err instanceof Error ? err.message : 'Failed to save manual trade note.',
      });
    }
  };

  if (!snapshot) {
    return (
      <div className="loading-shell">
        <div className="loading-card">
          <h2>Flush &amp; Reclaim V5</h2>
          <p>Connecting to local strategy engine &amp; scanner worker...</p>
        </div>
      </div>
    );
  }

  const canSubmitCurrentPlan = Boolean(
    currentPlan &&
      currentPlan.is_executable &&
      !isDraftDirty &&
      activePreview &&
      activePreview.is_valid_for_submission &&
      activePreview.plan_version === currentPlan.plan_version &&
      snapshot.worker_health.reconciliation_healthy &&
      !snapshot.pause_new_entries
  );

  const strategyManagedPositions = snapshot.positions.filter(
    (p) => p.is_strategy_managed
  );
  const isolatedHoldings = snapshot.positions.filter(
    (p) => !p.is_strategy_managed && p.source_mode !== 'MANUAL_USER_REPORTED'
  );
  const manualUserReportedPositions = snapshot.positions.filter(
    (p) => p.source_mode === 'MANUAL_USER_REPORTED'
  );

  return (
    <div className="app-shell">
      {/* ==================== TOP HEADER BAR ==================== */}
      <header className="top-header">
        <div className="brand-and-nav">
          <div className="brand-block">
            <span className="brand-dot" />
            <div>
              <h1 className="brand-title">FLUSH &amp; RECLAIM V5</h1>
              <span className="brand-subtitle tabular">
                Personal Execution Workstation · {snapshot.server_time_et}
              </span>
            </div>
          </div>

          <nav className="primary-nav" aria-label="Primary Navigation">
            <button
              type="button"
              className={`nav-tab ${activeTab === 'TRADE' ? 'active' : ''}`}
              onClick={() => setActiveTab('TRADE')}
            >
              Trade
            </button>
            <button
              type="button"
              className={`nav-tab ${activeTab === 'JOURNAL' ? 'active' : ''}`}
              onClick={() => setActiveTab('JOURNAL')}
            >
              Journal ({snapshot.events.length})
            </button>
            <button
              type="button"
              className={`nav-tab ${activeTab === 'SETTINGS' ? 'active' : ''}`}
              onClick={() => setActiveTab('SETTINGS')}
            >
              Settings
            </button>
          </nav>
        </div>

        <div className="header-status-strip">
          {/* Operating Mode Badge */}
          <span
            className={`status-pill mode-pill mode-${snapshot.operating_mode.toLowerCase()}`}
            title="Current Operating Mode"
          >
            Mode: <strong>{snapshot.operating_mode}</strong>
          </span>

          {/* Fixture / Data Source Quick Switcher */}
          <div className="fixture-switcher">
            <label htmlFor="fixture-select" className="sr-only">
              Data / Fixture Mode
            </label>
            <select
              id="fixture-select"
              className="fixture-select"
              value={snapshot.fixture_mode}
              onChange={(e) =>
                handleUpdateSettings({ fixture_mode: e.target.value })
              }
            >
              <option value="demo_ready">Fixture: Ready Signal (FISV Confirmed)</option>
              <option value="demo_acceptance">
                Fixture: CLI Screenshot Acceptance (Watching/Blocked)
              </option>
              <option value="demo_stale">Fixture: Stale Data Emergency Block</option>
              <option value="live">Live Market Feed (CNBC RTH Polling)</option>
            </select>
          </div>

          {/* SPY Macro Gate Pill */}
          <span
            className={`status-pill tabular ${
              snapshot.spy_gate.gate_status === 'PASS'
                ? 'pill-pass'
                : 'pill-warn'
            }`}
          >
            SPY: <strong>${snapshot.spy_gate.last.toFixed(2)}</strong> (
            {snapshot.spy_gate.vs_vwap_pct >= 0 ? '+' : ''}
            {snapshot.spy_gate.vs_vwap_pct.toFixed(2)}% vs VWAP) ·{' '}
            {snapshot.spy_gate.gate_status}
          </span>

          {/* Broker Connection Pill */}
          <span
            className={`status-pill tabular ${
              snapshot.broker_status.connected ? 'pill-pass' : 'pill-neutral'
            }`}
          >
            Broker: <strong>{snapshot.broker_status.masked_account_id}</strong> ·{' '}
            {snapshot.broker_status.connected ? 'OAuth Connected' : 'Local Ticket Mode'}
          </span>

          {/* Pause New Entries Operator Toggle */}
          <button
            type="button"
            className={`btn-toggle-pause ${
              snapshot.pause_new_entries ? 'paused' : 'active'
            }`}
            onClick={() =>
              handleUpdateSettings({
                pause_new_entries: !snapshot.pause_new_entries,
              })
            }
          >
            {snapshot.pause_new_entries
              ? '⏸ New Entries PAUSED'
              : '🟢 Entries Enabled'}
          </button>

          {/* Technical Diagnostics Disclosure Button */}
          <button
            type="button"
            className="btn-ghost-xs"
            onClick={() => setShowDiagnostics((v) => !v)}
          >
            {showDiagnostics ? 'Hide Diagnostics' : 'Diagnostics'}
          </button>
        </div>
      </header>

      {/* ==================== EXPANDABLE DIAGNOSTICS BAR ==================== */}
      {showDiagnostics && (
        <section className="diagnostics-drawer tabular">
          <div className="diag-grid">
            <div>
              <span className="diag-label">Session Schedule:</span>{' '}
              <strong>{snapshot.session_window_label}</strong>
            </div>
            <div>
              <span className="diag-label">Worker Heartbeat:</span>{' '}
              <strong>
                {snapshot.worker_health.last_heartbeat_et} (Cycle #
                {snapshot.worker_health.scan_cycle_count})
              </strong>{' '}
              · SSE: {sseConnected ? 'Connected' : 'Reconnecting'}
            </div>
            <div>
              <span className="diag-label">Reconciliation Health:</span>{' '}
              <strong
                style={{
                  color: snapshot.worker_health.reconciliation_healthy
                    ? '#10b981'
                    : '#f43f5e',
                }}
              >
                {snapshot.worker_health.reconciliation_healthy
                  ? 'HEALTHY'
                  : `REQUIRED (${snapshot.worker_health.reconciliation_reason})`}
              </strong>
            </div>
            <div>
              <span className="diag-label">Worker Persistence:</span>{' '}
              <span>{snapshot.worker_health.persistence_note}</span>
            </div>
          </div>
          <div className="diag-actions">
            <button
              type="button"
              className="btn-secondary-xs"
              onClick={() => postApi('/api/scan')}
            >
              Force Scan Cycle
            </button>
            <button
              type="button"
              className="btn-secondary-xs"
              onClick={() => handleReconcile(false)}
            >
              Run Account Reconciliation
            </button>
            <label className="diag-checkbox">
              <input
                type="checkbox"
                checked={simulatePreviewError}
                onChange={(e) => setSimulatePreviewError(e.target.checked)}
              />
              <span>Simulate Preview Reject</span>
            </label>
            <label className="diag-checkbox">
              <input
                type="checkbox"
                checked={simulateTimeout}
                onChange={(e) => setSimulateTimeout(e.target.checked)}
              />
              <span>Simulate Submit Timeout</span>
            </label>
            <label className="diag-checkbox">
              <input
                type="checkbox"
                checked={simulatePartialFill}
                onChange={(e) => setSimulatePartialFill(e.target.checked)}
              />
              <span>Simulate 50% Partial Fill</span>
            </label>
          </div>
        </section>
      )}

      {/* ==================== ALERT / RECONCILIATION BANNERS ==================== */}
      {!snapshot.worker_health.reconciliation_healthy && (
        <div className="critical-reconcile-banner">
          <div>
            <strong>⚠️ RECONCILIATION REQUIRED BEFORE NEW ORDERS:</strong>{' '}
            {snapshot.worker_health.reconciliation_reason}
          </div>
          <button
            type="button"
            className="btn-primary-xs"
            onClick={() => handleReconcile(false)}
          >
            Reconcile &amp; Verify Protective Stops Now
          </button>
        </div>
      )}

      {bannerNotice && (
        <div className={`notice-banner notice-${bannerNotice.kind}`}>
          <span>{bannerNotice.text}</span>
          <button
            type="button"
            className="btn-close-notice"
            onClick={() => setBannerNotice(null)}
          >
            ✕
          </button>
        </div>
      )}

      {/* ==================== MAIN CONTENT AREA ==================== */}
      {activeTab === 'TRADE' && (
        <main className="trade-workspace">
          {/* ---------- LEFT COLUMN: CANDIDATE LIST ---------- */}
          <aside className="candidate-sidebar" aria-label="Monitored Universe">
            <div className="panel-header">
              <h2>Universe Candidates ({snapshot.candidates.length})</h2>
              <span className="panel-subtitle tabular">
                5m Bar-Close Evaluation
              </span>
            </div>

            <div className="candidate-groups">
              {CATEGORY_ORDER.map((cat) => {
                const rows = snapshot.grouped_candidates[cat] || [];
                return (
                  <div key={cat} className="candidate-group">
                    <div className={`group-header group-${cat.toLowerCase()}`}>
                      <span>{CATEGORY_LABELS[cat]}</span>
                      <span className="group-count tabular">{rows.length}</span>
                    </div>

                    {rows.length === 0 ? (
                      <div className="empty-group-row">None right now</div>
                    ) : (
                      rows.map((row) => {
                        const isSelected =
                          selectedCandidate?.symbol === row.symbol;
                        return (
                          <div
                            key={row.symbol}
                            role="button"
                            tabIndex={0}
                            className={`candidate-row ${
                              isSelected ? 'selected' : ''
                            } state-${row.signal_state.toLowerCase()}`}
                            onClick={() => handleSelectSymbol(row.symbol, false)}
                            onKeyDown={(e) => {
                              if (e.key === 'Enter' || e.key === ' ') {
                                handleSelectSymbol(row.symbol, false);
                              }
                            }}
                          >
                            <div className="cand-row-top">
                              <span className="cand-symbol">{row.symbol}</span>
                              <span className="cand-price tabular">
                                ${row.last_price.toFixed(2)}
                              </span>
                            </div>

                            <div className="cand-row-mid">
                              <span
                                className={`cand-badge badge-${row.category.toLowerCase()}`}
                              >
                                {row.primary_status_badge}
                              </span>
                              {row.signal_time_et && (
                                <span className="cand-sig-time tabular">
                                  {row.signal_time_et}
                                  {row.signal_age_seconds !== null
                                    ? ` · ${Math.round(row.signal_age_seconds)}s`
                                    : ''}
                                </span>
                              )}
                            </div>

                            <div className="cand-reason">{row.concise_reason}</div>

                            {/* Only READY_FOR_REVIEW rows get the Review trade button; Watching/Blocked never show Buy/Submit */}
                            {row.is_actionable &&
                              row.signal_state === 'READY_FOR_REVIEW' && (
                                <div className="cand-action-row">
                                  <button
                                    type="button"
                                    className="btn-review-trade"
                                    onClick={(e) => {
                                      e.stopPropagation();
                                      handleSelectSymbol(row.symbol, true);
                                    }}
                                  >
                                    Review trade →
                                  </button>
                                </div>
                              )}
                          </div>
                        );
                      })
                    )}
                  </div>
                );
              })}
            </div>
          </aside>

          {/* ---------- CENTER COLUMN: CHART, EXPLANATION, GATES & POSITIONS ---------- */}
          <section className="center-stage">
            {selectedCandidate && (
              <>
                <div className="symbol-summary-header">
                  <div className="sym-title-block">
                    <div className="sym-headline">
                      <h2>{selectedCandidate.symbol}</h2>
                      <span
                        className={`cand-badge badge-${selectedCandidate.category.toLowerCase()}`}
                      >
                        {selectedCandidate.primary_status_badge}
                      </span>
                      <span className="sym-last-price tabular">
                        ${selectedCandidate.last_price.toFixed(2)}
                      </span>
                    </div>
                    <div className="sym-metrics-strip tabular">
                      <span>
                        Bar Session VWAP:{' '}
                        <strong>${selectedCandidate.session_vwap.toFixed(2)}</strong> (
                        {selectedCandidate.vs_vwap_pct >= 0 ? '+' : ''}
                        {selectedCandidate.vs_vwap_pct.toFixed(2)}%)
                      </span>
                      <span>
                        Morning High:{' '}
                        <strong>${selectedCandidate.morning_high.toFixed(2)}</strong>
                      </span>
                      <span>
                        Flush Low:{' '}
                        <strong>${selectedCandidate.flush_low.toFixed(2)}</strong> (
                        {selectedCandidate.flush_pct.toFixed(2)}%)
                      </span>
                      <span>
                        Vol Climax:{' '}
                        <strong>{selectedCandidate.volume_climax_mult.toFixed(1)}x</strong>
                      </span>
                      <span>
                        Quote Age:{' '}
                        <strong>{selectedCandidate.provenance.age_seconds.toFixed(1)}s</strong>
                      </span>
                    </div>
                  </div>

                  <div className="mobile-ticket-trigger">
                    <button
                      type="button"
                      className="btn-primary-sm"
                      onClick={() => setMobileTicketOpen(true)}
                    >
                      {selectedCandidate.is_actionable
                        ? 'Review Order Ticket'
                        : 'Inspect Order Plan'}
                    </button>
                  </div>
                </div>

                {/* 5m Candlestick Chart */}
                <SymbolChart
                  chartData={chartData}
                  symbol={selectedCandidate.symbol}
                />

                {/* If a Strategy-Managed Position is Open, surface Positions & Active Protection right below Chart */}
                {strategyManagedPositions.length > 0 && (
                  <div className="positions-panel" style={{ borderColor: '#10b981' }}>
                    <div className="panel-header">
                      <h3>🟢 Active Strategy Positions &amp; Isolated Holdings</h3>
                      <span className="panel-subtitle tabular">
                        Split-Tranche Stop Protection Active · Isolated from SGOV &amp; Manual Notes
                      </span>
                    </div>

                    <div className="pos-section">
                      <h4 className="pos-section-title">
                        Strategy-Managed Positions ({strategyManagedPositions.length})
                      </h4>
                      <div className="pos-table-wrap">
                        <table className="pos-table tabular">
                          <thead>
                            <tr>
                              <th>Symbol / Mode</th>
                              <th>Filled / Rem</th>
                              <th>Entry / Last</th>
                              <th>Realized / Unrealized</th>
                              <th>Active Stop (Covered Qty)</th>
                              <th>Leg 1 / Leg 2 / Runner</th>
                              <th>Actions</th>
                            </tr>
                          </thead>
                          <tbody>
                            {strategyManagedPositions.map((pos: PositionRecord) => (
                              <tr key={pos.position_id}>
                                <td>
                                  <strong>{pos.symbol}</strong>{' '}
                                  <span className="badge-source">{pos.source_mode}</span>
                                </td>
                                <td>
                                  {pos.filled_qty?.toLocaleString()} sh /{' '}
                                  <strong>{pos.remaining_qty?.toLocaleString()} sh</strong>
                                </td>
                                <td>
                                  ${pos.avg_fill_price.toFixed(2)} → $
                                  {pos.current_price?.toFixed(2)}
                                </td>
                                <td>
                                  <span className="pnl-pos">
                                    +${(pos.realized_pnl_usd || 0).toFixed(2)}
                                  </span>{' '}
                                  /{' '}
                                  <span
                                    className={
                                      (pos.unrealized_pnl_usd || 0) >= 0
                                        ? 'pnl-pos'
                                        : 'pnl-neg'
                                    }
                                  >
                                    ${(pos.unrealized_pnl_usd || 0).toFixed(2)}
                                  </span>
                                </td>
                                <td>
                                  <span className="stop-pill">
                                    Stop ${pos.working_stop_price?.toFixed(2)} (
                                    {pos.working_stop_qty?.toLocaleString()} sh)
                                  </span>
                                </td>
                                <td>
                                  L1 ${pos.leg1_target_price?.toFixed(2)} · L2 $
                                  {pos.leg2_target_price?.toFixed(2)} ·{' '}
                                  <span className="runner-tag">{pos.runner_state}</span>
                                </td>
                                <td>
                                  {pos.leg1_exited_qty === 0 && (
                                    <button
                                      type="button"
                                      className="btn-secondary-xs"
                                      onClick={() =>
                                        handleLeg1Exit(pos.position_id, pos.symbol)
                                      }
                                    >
                                      Simulate Leg 1 Fill → BE Stop
                                    </button>
                                  )}
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </div>

                    <div className="pos-section">
                      <div className="manual-cards-grid">
                        {manualUserReportedPositions.map((mpos) => (
                          <div key={mpos.position_id} className="manual-pos-card tabular">
                            <div className="manual-card-header">
                              <strong>{mpos.symbol} (User-Reported Workflow Record)</strong>
                              <span className="badge-unreconciled">
                                {mpos.reconciliation_badge || 'UNRECONCILED_USER_REPORTED'}
                              </span>
                            </div>
                            <div className="manual-metrics">
                              <span>Entry: <strong>${mpos.avg_fill_price.toFixed(2)}</strong></span>
                              <span>50% Scale: <strong>${mpos.leg1_target_price?.toFixed(2)}</strong></span>
                              <span>Stop: <strong>${mpos.working_stop_price?.toFixed(2)}</strong></span>
                              <span>Reported Profit: <strong className="pnl-pos">~${(mpos.reported_approx_pnl_usd || 0).toLocaleString()}</strong></span>
                            </div>
                          </div>
                        ))}
                        {isolatedHoldings.map((hpos) => (
                          <div key={hpos.position_id} className="manual-pos-card isolated-card tabular">
                            <div className="manual-card-header">
                              <strong>{hpos.symbol} (Pre-Existing IRA Holding)</strong>
                              <span className="badge-isolated">PRE_EXISTING_HOLDING_ISOLATED</span>
                            </div>
                            <div className="manual-metrics">
                              <span>Qty: <strong>{hpos.filled_qty?.toLocaleString()} sh</strong></span>
                              <span>Avg Cost: <strong>${hpos.avg_fill_price.toFixed(2)}</strong></span>
                              <span>Strategy Managed: <strong>No (Excluded from Flatten)</strong></span>
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  </div>
                )}

                {/* Plain-language Why Now / What is Missing + 6-Gate Evidence */}
                <div className="explanation-card">
                  <div className="why-header">
                    <h3>
                      {selectedCandidate.is_actionable
                        ? '✅ Why Actionable Now (Confirmed 5m Bar Close)'
                        : '🔍 Setup Diagnosis — Why Waiting or Blocked'}
                    </h3>
                    <span className="tabular meta-ts">
                      Evaluated: {selectedCandidate.decision_time_et}
                    </span>
                  </div>
                  <p className="why-body-text">
                    {selectedCandidate.why_now_or_missing}
                  </p>

                  {/* 6-Gate Evidence Grid */}
                  <div className="gates-grid">
                    {selectedCandidate.gates.map((g) => (
                      <div
                        key={g.gate_id}
                        className={`gate-card gate-${g.status.toLowerCase()}`}
                      >
                        <div className="gate-card-top">
                          <span className="gate-name">{g.gate_name}</span>
                          <span className={`gate-status-pill pill-${g.status.toLowerCase()}`}>
                            {g.status}
                          </span>
                        </div>
                        <div className="gate-observed tabular">
                          Observed: <strong>{g.observed_value}</strong>
                        </div>
                        <div className="gate-threshold tabular">
                          Required: {g.required_threshold || g.threshold}
                        </div>
                      </div>
                    ))}
                  </div>

                  {/* Secondary Disclosure for Reference Trigger & Feed Provenance */}
                  <details className="provenance-disclosure">
                    <summary>
                      Reference Trigger Price &amp; Data Provenance Details (
                      {selectedCandidate.consistency_check.provider_relationship})
                    </summary>
                    <div className="disclosure-body tabular">
                      <div>
                        <strong>Reference Trigger Price (Informational Only):</strong>{' '}
                        {selectedCandidate.reference_trigger_price
                          ? `$${selectedCandidate.reference_trigger_price.toFixed(2)} (as of ${selectedCandidate.reference_trigger_timestamp_et})`
                          : 'N/A'}{' '}
                        — {selectedCandidate.reference_trigger_meaning}
                      </div>
                      <div>
                        <strong>Feed Consistency &amp; Provenance:</strong>{' '}
                        {selectedCandidate.consistency_check.explanation} [Relation:{' '}
                        <code>
                          {selectedCandidate.consistency_check.provider_relationship}
                        </code>{' '}
                        | Basis:{' '}
                        <code>{selectedCandidate.consistency_check.comparison_basis}</code>]
                      </div>
                    </div>
                  </details>
                </div>

                {/* Positions & Active Orders Panel */}
                <div className="positions-panel">
                  <div className="panel-header">
                    <h3>Positions &amp; Active Protection</h3>
                    <span className="panel-subtitle tabular">
                      Strategy Positions Isolated from Pre-Existing Holdings &amp; Manual Notes
                    </span>
                  </div>

                  {/* 1. Strategy-Managed Positions */}
                  <div className="pos-section">
                    <h4 className="pos-section-title">
                      Strategy-Managed Positions ({strategyManagedPositions.length})
                    </h4>
                    {strategyManagedPositions.length === 0 ? (
                      <div className="pos-empty">
                        No open strategy-managed positions right now.
                      </div>
                    ) : (
                      <div className="pos-table-wrap">
                        <table className="pos-table tabular">
                          <thead>
                            <tr>
                              <th>Symbol / Mode</th>
                              <th>Filled / Rem</th>
                              <th>Entry / Last</th>
                              <th>Realized / Unrealized</th>
                              <th>Active Stop (Covered Qty)</th>
                              <th>Leg 1 / Leg 2 / Runner</th>
                              <th>Actions</th>
                            </tr>
                          </thead>
                          <tbody>
                            {strategyManagedPositions.map((pos: PositionRecord) => (
                              <tr key={pos.position_id}>
                                <td>
                                  <strong>{pos.symbol}</strong>{' '}
                                  <span className="badge-source">{pos.source_mode}</span>
                                </td>
                                <td>
                                  {pos.filled_qty?.toLocaleString()} sh /{' '}
                                  <strong>{pos.remaining_qty?.toLocaleString()} sh</strong>
                                </td>
                                <td>
                                  ${pos.avg_fill_price.toFixed(2)} → $
                                  {pos.current_price?.toFixed(2)}
                                </td>
                                <td>
                                  <span className="pnl-pos">
                                    +${(pos.realized_pnl_usd || 0).toFixed(2)}
                                  </span>{' '}
                                  /{' '}
                                  <span
                                    className={
                                      (pos.unrealized_pnl_usd || 0) >= 0
                                        ? 'pnl-pos'
                                        : 'pnl-neg'
                                    }
                                  >
                                    ${(pos.unrealized_pnl_usd || 0).toFixed(2)}
                                  </span>
                                </td>
                                <td>
                                  <span className="stop-pill">
                                    Stop ${pos.working_stop_price?.toFixed(2)} (
                                    {pos.working_stop_qty?.toLocaleString()} sh)
                                  </span>
                                </td>
                                <td>
                                  L1 ${pos.leg1_target_price?.toFixed(2)} · L2 $
                                  {pos.leg2_target_price?.toFixed(2)} ·{' '}
                                  <span className="runner-tag">{pos.runner_state}</span>
                                </td>
                                <td>
                                  {pos.leg1_exited_qty === 0 && (
                                    <button
                                      type="button"
                                      className="btn-secondary-xs"
                                      onClick={() =>
                                        handleLeg1Exit(pos.position_id, pos.symbol)
                                      }
                                    >
                                      Simulate Leg 1 Fill → BE Stop
                                    </button>
                                  )}
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </div>

                  {/* 2. User-Reported Manual Trade Records (Unreconciled) */}
                  <div className="pos-section">
                    <h4 className="pos-section-title">
                      User-Reported Manual Workflow Records (Unreconciled · Excluded from Verified P&amp;L)
                    </h4>
                    <div className="manual-cards-grid">
                      {manualUserReportedPositions.map((mpos) => (
                        <div key={mpos.position_id} className="manual-pos-card tabular">
                          <div className="manual-card-header">
                            <strong>{mpos.symbol}</strong>
                            <span className="badge-unreconciled">
                              {mpos.reconciliation_badge || 'UNRECONCILED_USER_REPORTED'}
                            </span>
                          </div>
                          <div className="manual-metrics">
                            <span>
                              Reported Entry: <strong>${mpos.avg_fill_price.toFixed(2)}</strong>
                            </span>
                            <span>
                              50% Scale Exit:{' '}
                              <strong>
                                {mpos.leg1_target_price
                                  ? `$${mpos.leg1_target_price.toFixed(2)}`
                                  : 'Unverified'}
                              </strong>
                            </span>
                            <span>
                              Reported Stop:{' '}
                              <strong>
                                {mpos.working_stop_price
                                  ? `$${mpos.working_stop_price.toFixed(2)}`
                                  : 'Unverified'}
                              </strong>
                            </span>
                            <span>
                              Reported Profit:{' '}
                              <strong className="pnl-pos">
                                ~${(mpos.reported_approx_pnl_usd || 0).toLocaleString()} (User-Reported)
                              </strong>
                            </span>
                            <span>
                              Verified Qty / Timestamps:{' '}
                              <em>Unverified (awaiting broker reconciliation)</em>
                            </span>
                          </div>
                          <p className="manual-notes">{mpos.notes}</p>
                        </div>
                      ))}

                      {/* 3. Pre-Existing Non-Strategy Holdings */}
                      {isolatedHoldings.map((hpos) => (
                        <div key={hpos.position_id} className="manual-pos-card isolated-card tabular">
                          <div className="manual-card-header">
                            <strong>{hpos.symbol} (Pre-Existing IRA Holding)</strong>
                            <span className="badge-isolated">
                              PRE_EXISTING_HOLDING_ISOLATED
                            </span>
                          </div>
                          <div className="manual-metrics">
                            <span>
                              Qty: <strong>{hpos.filled_qty?.toLocaleString()} sh</strong>
                            </span>
                            <span>
                              Avg Cost: <strong>${hpos.avg_fill_price.toFixed(2)}</strong>
                            </span>
                            <span>
                              Strategy Managed: <strong>No (Never Adopted or Flattened)</strong>
                            </span>
                          </div>
                          <p className="manual-notes">{hpos.notes}</p>
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              </>
            )}
          </section>

          {/* ---------- RIGHT COLUMN: PREFILLED ORDER-REVIEW PANEL ---------- */}
          <aside
            className={`order-ticket-panel ${
              mobileTicketOpen ? 'mobile-open' : ''
            }`}
            aria-label="Order Review Ticket"
          >
            <div className="ticket-mobile-close-bar">
              <span>Order Review Ticket</span>
              <button
                type="button"
                className="btn-ghost-xs"
                onClick={() => setMobileTicketOpen(false)}
              >
                Close ✕
              </button>
            </div>

            {currentPlan && selectedCandidate ? (
              <div className="ticket-content">
                <div className="ticket-header">
                  <div>
                    <div className="ticket-symbol-row">
                      <h2>{currentPlan.symbol}</h2>
                      <span className="side-badge-bto">BUY TO OPEN (SHARES)</span>
                      <span className="plan-ver-badge tabular">
                        Plan v{currentPlan.plan_version}
                      </span>
                    </div>
                    <div className="ticket-submeta tabular">
                      Strategy: {currentPlan.strategy_version} · Bar Close:{' '}
                      {currentPlan.signal_bar_close_et}
                    </div>
                  </div>
                </div>

                {/* Actionability Banner inside Ticket */}
                <div
                  className={`ticket-status-box ${
                    currentPlan.is_executable ? 'status-ready' : 'status-blocked'
                  }`}
                >
                  {currentPlan.is_executable ? (
                    <>
                      <strong>🟢 Actionable Setup Ready for Review</strong>
                      <p>
                        Review the prefilled Split-Tranche OTOCO parameters below. No
                        order is sent until you run Preview and confirm submission.
                      </p>
                    </>
                  ) : (
                    <>
                      <strong>🔒 Submission Disabled ({selectedCandidate.primary_status_badge})</strong>
                      <p>{currentPlan.execution_blocked_reason}</p>
                    </>
                  )}
                </div>

                {/* Editable Sizing & Price Form */}
                <form className="ticket-form" onSubmit={handleSavePlanEdits}>
                  <div className="form-row-2col">
                    <div className="form-field">
                      <label htmlFor="alloc-input">Dollar Allocation ($ USD)</label>
                      <input
                        id="alloc-input"
                        type="number"
                        step="1000"
                        min="1000"
                        max="152000"
                        className="tabular-input"
                        value={draftAllocation}
                        onChange={(e) => {
                          setDraftAllocation(e.target.value);
                          setIsDraftDirty(true);
                        }}
                      />
                    </div>
                    <div className="form-field">
                      <label htmlFor="limit-input">Entry Limit Price ($)</label>
                      <input
                        id="limit-input"
                        type="number"
                        step="0.01"
                        min="1"
                        className="tabular-input"
                        value={draftLimitPrice}
                        onChange={(e) => {
                          setDraftLimitPrice(e.target.value);
                          setIsDraftDirty(true);
                        }}
                      />
                    </div>
                  </div>

                  {/* Computed Whole Shares & Split-Tranche OTOCO Breakdown */}
                  <div className="split-tranche-card tabular">
                    <div className="tranche-headline">
                      <span>Total Whole Shares:</span>
                      <strong className="shares-big">
                        {liveDraftPreviewMath.shares.toLocaleString()} SH
                      </strong>
                    </div>
                    <div className="tranche-split-row">
                      <span>
                        Tranche A (Leg 1 · 50%):{' '}
                        <strong>{liveDraftPreviewMath.q1.toLocaleString()} sh</strong>
                      </span>
                      <span>
                        Tranche B (Leg 2 · 50%):{' '}
                        <strong>{liveDraftPreviewMath.q2.toLocaleString()} sh</strong>
                      </span>
                    </div>
                    <div className="tranche-note">
                      Split-Tranche OTOCO ensures {liveDraftPreviewMath.q2.toLocaleString()}{' '}
                      runner shares retain active stop protection when Leg 1 fills.
                    </div>
                  </div>

                  {/* Prominent Planned Risk Box */}
                  <div className="planned-risk-highlight tabular">
                    <div className="risk-col">
                      <span className="risk-label">PLANNED STOP RISK ($)</span>
                      <strong className="risk-value-usd">
                        -${liveDraftPreviewMath.riskUsd.toFixed(2)}
                      </strong>
                    </div>
                    <div className="risk-col">
                      <span className="risk-label">STOP DISTANCE (%)</span>
                      <strong className="risk-value-pct">
                        -{liveDraftPreviewMath.riskPct.toFixed(2)}%
                      </strong>
                    </div>
                    <div className="risk-col">
                      <span className="risk-label">EST. NOTIONAL</span>
                      <strong className="notional-value">
                        ${liveDraftPreviewMath.notional.toLocaleString(undefined, {
                          minimumFractionDigits: 2,
                          maximumFractionDigits: 2,
                        })}
                      </strong>
                    </div>
                  </div>

                  {/* Strategy-Derived Exit Levels (Read-only by default, unlockable) */}
                  <div className="exit-levels-section">
                    <div className="exit-section-header">
                      <span>Protective Stop &amp; Profit Targets (STC · DAY)</span>
                      <label className="override-toggle">
                        <input
                          type="checkbox"
                          checked={unlockManualOverride}
                          onChange={(e) => {
                            setUnlockManualOverride(e.target.checked);
                            setIsDraftDirty(true);
                          }}
                        />
                        <span>Unlock Manual Override</span>
                      </label>
                    </div>

                    <div className="exit-levels-grid tabular">
                      <div className="exit-field">
                        <label>Hard Stop (STC Stop Market)</label>
                        <input
                          type="number"
                          step="0.01"
                          disabled={!unlockManualOverride}
                          value={
                            unlockManualOverride
                              ? draftManualStop
                              : currentPlan.stop_price.toFixed(2)
                          }
                          onChange={(e) => {
                            setDraftManualStop(e.target.value);
                            setIsDraftDirty(true);
                          }}
                        />
                      </div>
                      <div className="exit-field">
                        <label>Leg 1 Target (50% @ VWAP)</label>
                        <input
                          type="number"
                          step="0.01"
                          disabled={!unlockManualOverride}
                          value={
                            unlockManualOverride
                              ? draftManualLeg1
                              : currentPlan.leg1_target.toFixed(2)
                          }
                          onChange={(e) => {
                            setDraftManualLeg1(e.target.value);
                            setIsDraftDirty(true);
                          }}
                        />
                      </div>
                      <div className="exit-field">
                        <label>Leg 2 Target (+0.85% / Trail)</label>
                        <input
                          type="number"
                          step="0.01"
                          disabled={!unlockManualOverride}
                          value={
                            unlockManualOverride
                              ? draftManualLeg2
                              : currentPlan.leg2_target.toFixed(2)
                          }
                          onChange={(e) => {
                            setDraftManualLeg2(e.target.value);
                            setIsDraftDirty(true);
                          }}
                        />
                      </div>
                    </div>

                    <div className="runner-rule-summary tabular">
                      <div>
                        <strong>Post-Leg 1 Rule:</strong> Move Tranche B stop to{' '}
                        <strong>${parseFloat(draftLimitPrice || '0').toFixed(2)} BE</strong>;
                        trail 5m EMA20 up to{' '}
                        <strong>${currentPlan.runner_stretch_target.toFixed(2)}</strong> (+1.60%).
                      </div>
                      <div>
                        <strong>Mandatory Time Exit:</strong> Flatten remaining strategy
                        shares at <strong>{currentPlan.flatten_cutoff_et}</strong>.
                      </div>
                    </div>
                  </div>

                  {isDraftDirty && (
                    <button
                      type="submit"
                      className="btn-apply-edits"
                      disabled={busyAction === 'SAVE_PLAN'}
                    >
                      {busyAction === 'SAVE_PLAN'
                        ? 'Updating Plan...'
                        : 'Apply Edits & Revalidate Order Plan (Invalidates Old Preview)'}
                    </button>
                  )}
                </form>

                {/* Account & Preview Validity Metadata */}
                <div className="account-meta-box tabular">
                  <div className="meta-line">
                    <span>Target Account:</span>
                    <strong>{currentPlan.masked_account_id}</strong>
                  </div>
                  <div className="meta-line">
                    <span>Available Buying Power:</span>
                    <strong>
                      ${currentPlan.available_buying_power_usd.toLocaleString()} (as of{' '}
                      {currentPlan.buying_power_timestamp_et.split(' ')[1] ||
                        currentPlan.buying_power_timestamp_et}
                      )
                    </strong>
                  </div>
                  <div className="meta-line">
                    <span>Preview Status:</span>
                    {activePreview ? (
                      <strong
                        className={
                          activePreview.is_valid_for_submission
                            ? 'text-emerald'
                            : 'text-rose'
                        }
                      >
                        {activePreview.status} · {activePreview.validation_mode} (Exp:{' '}
                        {activePreview.expires_at_et.split(' ')[1] ||
                          activePreview.expires_at_et}
                        )
                      </strong>
                    ) : (
                      <span className="text-amber">
                        {isDraftDirty
                          ? 'Draft edited — apply edits & run preview'
                          : 'Not previewed yet'}
                      </span>
                    )}
                  </div>
                </div>

                {/* ==================== DISTINCT TASTYTRADE ACTIONS ==================== */}
                <div className="ticket-actions-stack">
                  <div className="handoff-buttons-row">
                    <button
                      type="button"
                      className="btn-utility"
                      onClick={() =>
                        handleCopyText(
                          currentPlan.symbol,
                          'COPY_TICKER',
                          currentPlan.symbol
                        )
                      }
                    >
                      Copy ticker
                    </button>
                    <button
                      type="button"
                      className="btn-utility"
                      onClick={() =>
                        handleCopyText(
                          currentPlan.copy_ticket_text,
                          'COPY_TICKET',
                          currentPlan.symbol
                        )
                      }
                    >
                      Copy ticket
                    </button>
                    <button
                      type="button"
                      className="btn-utility"
                      onClick={() => handleOpenTastytrade(currentPlan)}
                    >
                      Open Tastytrade ↗
                    </button>
                  </div>

                  <button
                    type="button"
                    className="btn-preview-broker"
                    disabled={
                      !currentPlan.is_executable ||
                      busyAction === 'PREVIEW' ||
                      snapshot.pause_new_entries ||
                      !snapshot.worker_health.reconciliation_healthy
                    }
                    onClick={handleRunPreview}
                  >
                    {busyAction === 'PREVIEW'
                      ? 'Running Bound Preview...'
                      : `Preview with Tastytrade (Plan v${currentPlan.plan_version})`}
                  </button>

                  <button
                    type="button"
                    className="btn-submit-broker"
                    disabled={!canSubmitCurrentPlan || busyAction === 'SUBMIT'}
                    onClick={() => {
                      setModalConfirmChecked(false);
                      setConfirmModalOpen(true);
                    }}
                  >
                    Submit to Tastytrade (Requires Confirmation)
                  </button>
                </div>
              </div>
            ) : (
              <div className="ticket-empty">
                Select a symbol to inspect its order plan.
              </div>
            )}
          </aside>
        </main>
      )}

      {/* ==================== JOURNAL DESTINATION ==================== */}
      {activeTab === 'JOURNAL' && (
        <main className="secondary-workspace">
          <div className="journal-grid">
            <section className="journal-main-card">
              <div className="panel-header">
                <h2>Durable Execution &amp; Signal Journal</h2>
                <div className="filter-pills">
                  {['ALL', 'LIVE_BROKER', 'PAPER', 'MANUAL_USER_REPORTED'].map(
                    (f) => (
                      <button
                        key={f}
                        type="button"
                        className={`btn-ghost-xs ${
                          journalFilter === f ? 'active-pill' : ''
                        }`}
                        onClick={() => setJournalFilter(f)}
                      >
                        {f}
                      </button>
                    )
                  )}
                </div>
              </div>

              <h3>Order Intents ({snapshot.order_intents.length})</h3>
              {snapshot.order_intents.length === 0 ? (
                <p className="muted-note">
                  No order intents submitted in this session yet.
                </p>
              ) : (
                <div className="pos-table-wrap">
                  <table className="pos-table tabular">
                    <thead>
                      <tr>
                        <th>Intent ID / Ext ID</th>
                        <th>Symbol</th>
                        <th>Source Mode</th>
                        <th>Requested / Filled</th>
                        <th>Limit / Stop</th>
                        <th>Order State</th>
                        <th>Protection State</th>
                        <th>Created (ET)</th>
                      </tr>
                    </thead>
                    <tbody>
                      {snapshot.order_intents
                        .filter(
                          (i) =>
                            journalFilter === 'ALL' ||
                            i.source_mode === journalFilter
                        )
                        .map((intent) => (
                          <tr key={intent.intent_id}>
                            <td>
                              <code>{intent.intent_id}</code>
                              <br />
                              <small>{intent.external_identifier}</small>
                            </td>
                            <td>
                              <strong>{intent.symbol}</strong>
                            </td>
                            <td>
                              <span className="badge-source">
                                {intent.source_mode}
                              </span>
                            </td>
                            <td>
                              {intent.shares.toLocaleString()} /{' '}
                              <strong>
                                {(
                                  intent.filled_qty ?? intent.shares
                                ).toLocaleString()}
                              </strong>
                            </td>
                            <td>
                              ${intent.limit_price.toFixed(2)} / Stop $
                              {intent.stop_price.toFixed(2)}
                            </td>
                            <td>
                              <strong>{intent.order_state}</strong>
                            </td>
                            <td>{intent.protection_state}</td>
                            <td>{intent.created_at_et}</td>
                          </tr>
                        ))}
                    </tbody>
                  </table>
                </div>
              )}

              <h3 style={{ marginTop: '1.5rem' }}>
                Audit Event Log ({snapshot.events.length})
              </h3>
              <div className="events-list tabular">
                {snapshot.events
                  .filter(
                    (ev) =>
                      journalFilter === 'ALL' ||
                      ev.source_mode.includes(journalFilter)
                  )
                  .map((ev) => (
                    <div
                      key={ev.event_id}
                      className={`event-row sev-${ev.severity.toLowerCase()}`}
                    >
                      <div className="event-top">
                        <span className="event-type">{ev.event_type}</span>
                        {ev.symbol && (
                          <span className="event-sym">{ev.symbol}</span>
                        )}
                        <span className="badge-source">{ev.source_mode}</span>
                        <span className="event-time">{ev.timestamp_et}</span>
                      </div>
                      <div className="event-summary">{ev.summary}</div>
                    </div>
                  ))}
              </div>
            </section>

            {/* Manual User-Reported Trade Entry Form */}
            <aside className="journal-side-card">
              <h3>Record User-Reported Manual Trade</h3>
              <p className="muted-note">
                Manual notes are explicitly labeled{' '}
                <code>MANUAL_USER_REPORTED</code> /{' '}
                <code>UNRECONCILED_USER_REPORTED</code> and are never combined
                with broker-verified P&amp;L.
              </p>
              <form className="manual-form" onSubmit={handleAddManualTrade}>
                <div className="form-field">
                  <label>Symbol</label>
                  <input
                    type="text"
                    value={manualSym}
                    onChange={(e) => setManualSym(e.target.value.toUpperCase())}
                  />
                </div>
                <div className="form-field">
                  <label>Reported Entry Price ($)</label>
                  <input
                    type="number"
                    step="0.01"
                    value={manualEntry}
                    onChange={(e) => setManualEntry(e.target.value)}
                  />
                </div>
                <div className="form-field">
                  <label>Reported Stop Price ($)</label>
                  <input
                    type="number"
                    step="0.01"
                    value={manualStop}
                    onChange={(e) => setManualStop(e.target.value)}
                  />
                </div>
                <div className="form-field">
                  <label>Reported Leg 1 Scale Price ($)</label>
                  <input
                    type="number"
                    step="0.01"
                    value={manualLeg1}
                    onChange={(e) => setManualLeg1(e.target.value)}
                  />
                </div>
                <div className="form-field">
                  <label>Reported Approx. Profit ($ USD)</label>
                  <input
                    type="number"
                    step="10"
                    value={manualPnl}
                    onChange={(e) => setManualPnl(e.target.value)}
                  />
                </div>
                <div className="form-field">
                  <label>Notes</label>
                  <textarea
                    rows={3}
                    value={manualNotes}
                    onChange={(e) => setManualNotes(e.target.value)}
                  />
                </div>
                <button type="submit" className="btn-preview-broker">
                  Save Unreconciled Manual Record
                </button>
              </form>
            </aside>
          </div>
        </main>
      )}

      {/* ==================== SETTINGS DESTINATION ==================== */}
      {activeTab === 'SETTINGS' && (
        <main className="secondary-workspace">
          <div className="settings-grid">
            <section className="settings-card">
              <h2>Operating Mode &amp; Safety Controls</h2>
              <p className="muted-note">
                Strategy parameters remain locked to audited V5 thresholds during UI
                operation. Configure execution boundaries and sizing caps below.
              </p>

              <div className="settings-form-group">
                <label>Operating Mode</label>
                <select
                  value={snapshot.operating_mode}
                  onChange={(e) =>
                    handleUpdateSettings({ operating_mode: e.target.value })
                  }
                >
                  <option value="LIVE_DATA_TRADING_DISABLED">
                    LIVE_DATA_TRADING_DISABLED (Scanner + Local Ticket + Copy/Open)
                  </option>
                  <option value="DEMO_REPLAY">
                    DEMO_REPLAY (Deterministic Replay / No Live Execution)
                  </option>
                  <option value="PAPER_TRADING">
                    PAPER_TRADING (Simulated Execution &amp; Position Lifecycle)
                  </option>
                  <option value="BROKER_PREVIEW">
                    BROKER_PREVIEW (Dry-Run Validation Only)
                  </option>
                  <option value="LIVE_SUBMISSION_ENABLED">
                    LIVE_SUBMISSION_ENABLED (Requires Live Feed + OAuth Credentials)
                  </option>
                </select>
              </div>

              <div className="settings-form-group">
                <label>Fixture / Data Mode</label>
                <select
                  value={snapshot.fixture_mode}
                  onChange={(e) =>
                    handleUpdateSettings({ fixture_mode: e.target.value })
                  }
                >
                  <option value="demo_ready">
                    demo_ready (FISV Confirmed Ready + PLNT Watching + EEFT Divergent)
                  </option>
                  <option value="demo_acceptance">
                    demo_acceptance (CLI Screenshot Acceptance Fixture)
                  </option>
                  <option value="demo_stale">
                    demo_stale (Stale Data Block Verification)
                  </option>
                  <option value="live">
                    live (Live CNBC RTH Bars &amp; Quotes)
                  </option>
                </select>
              </div>

              <div className="settings-form-group">
                <label className="chart-toggle">
                  <input
                    type="checkbox"
                    checked={Boolean(snapshot.settings.sound_alerts_enabled)}
                    onChange={(e) =>
                      handleUpdateSettings({
                        sound_alerts_enabled: e.target.checked,
                      })
                    }
                  />
                  <span>Enable Sound Alerts on New Confirmed Signals (Default: Off)</span>
                </label>
              </div>
            </section>

            <section className="settings-card">
              <h2>Tastytrade Broker Connection &amp; Security</h2>
              <div className="broker-info-box tabular">
                <div>
                  <strong>Target Account (Allowlisted):</strong>{' '}
                  {snapshot.broker_status.masked_account_id}
                </div>
                <div>
                  <strong>Environment:</strong> {snapshot.broker_status.environment}
                </div>
                <div>
                  <strong>Connection Status:</strong>{' '}
                  {snapshot.broker_status.status_label}
                </div>
              </div>
              <p className="muted-note" style={{ marginTop: '0.75rem' }}>
                {snapshot.broker_status.setup_instructions}
              </p>
              <div className="security-checklist">
                <div>✅ Credentials loaded exclusively from backend environment (never exposed to browser JS).</div>
                <div>✅ Server bound to loopback <code>127.0.0.1</code> with CSRF/Origin validation on all POST routes.</div>
                <div>✅ GET requests can never preview, submit, or modify broker orders.</div>
              </div>
            </section>
          </div>
        </main>
      )}

      {/* ==================== FINAL ORDER CONFIRMATION MODAL ==================== */}
      {confirmModalOpen && currentPlan && activePreview && (
        <div className="modal-backdrop" role="dialog" aria-modal="true">
          <div className="confirm-modal">
            <div className="modal-header">
              <h2>Confirm Split-Tranche OTOCO Order Submission</h2>
              <button
                type="button"
                className="btn-close-notice"
                onClick={() => setConfirmModalOpen(false)}
              >
                ✕
              </button>
            </div>

            <div className="modal-body tabular">
              <div className="modal-summary-grid">
                <div>
                  <span>Account / Env:</span>
                  <strong>
                    {currentPlan.masked_account_id} ({activePreview.environment})
                  </strong>
                </div>
                <div>
                  <span>Operating Mode:</span>
                  <strong>{snapshot.operating_mode}</strong>
                </div>
                <div>
                  <span>Symbol &amp; Side:</span>
                  <strong>
                    {currentPlan.symbol} · BUY TO OPEN (SHARES)
                  </strong>
                </div>
                <div>
                  <span>Total Quantity:</span>
                  <strong>
                    {currentPlan.shares.toLocaleString()} sh (Tranche A:{' '}
                    {currentPlan.leg1_shares.toLocaleString()} sh + Tranche B:{' '}
                    {currentPlan.leg2_shares.toLocaleString()} sh)
                  </strong>
                </div>
                <div>
                  <span>Entry Limit Price:</span>
                  <strong>${currentPlan.limit_price.toFixed(2)} (DAY)</strong>
                </div>
                <div>
                  <span>Protective Stop Price:</span>
                  <strong className="text-rose">
                    ${currentPlan.stop_price.toFixed(2)} Stop Market (Covers{' '}
                    {currentPlan.shares.toLocaleString()} sh)
                  </strong>
                </div>
                <div>
                  <span>Profit Targets:</span>
                  <strong className="text-emerald">
                    Leg 1: ${currentPlan.leg1_target.toFixed(2)} · Leg 2: $
                    {currentPlan.leg2_target.toFixed(2)}
                  </strong>
                </div>
                <div>
                  <span>Estimated Notional:</span>
                  <strong>
                    ${currentPlan.notional_usd.toLocaleString(undefined, {
                      minimumFractionDigits: 2,
                    })}
                  </strong>
                </div>
                <div className="modal-risk-banner">
                  <span>MAX PLANNED STOP RISK:</span>
                  <strong>
                    -${currentPlan.planned_risk_usd.toFixed(2)} (-
                    {currentPlan.planned_risk_pct.toFixed(2)}%)
                  </strong>
                </div>
              </div>

              <label className="modal-confirm-checkbox">
                <input
                  type="checkbox"
                  checked={modalConfirmChecked}
                  onChange={(e) => setModalConfirmChecked(e.target.checked)}
                />
                <span>
                  I have verified the quantities, limit price, protective stop, and
                  planned dollar risk for <strong>{currentPlan.symbol}</strong>.
                </span>
              </label>
            </div>

            <div className="modal-footer">
              <button
                type="button"
                className="btn-utility"
                onClick={() => setConfirmModalOpen(false)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn-submit-broker"
                disabled={!modalConfirmChecked || busyAction === 'SUBMIT'}
                onClick={handleConfirmSubmitOrder}
              >
                {busyAction === 'SUBMIT'
                  ? 'Transmitting Order...'
                  : `Transmit Confirmed Order (${currentPlan.shares.toLocaleString()} sh)`}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
