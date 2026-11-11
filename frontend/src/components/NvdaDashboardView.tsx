import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { NvdaChartBar, NvdaMonitorState, NvdaSetupCard } from '../types';

interface NvdaDashboardViewProps {
  state: NvdaMonitorState | null;
  audioEnabled: boolean;
  onToggleAudio: () => void;
  onTestChime: () => void;
  onRefresh: () => void;
  isRefreshing: boolean;
}

function fmtPct(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '--';
  return `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`;
}

const SetupCardView: React.FC<{ card: NvdaSetupCard }> = ({ card }) => {
  const st = card.status;
  const isTriggered = st === 'TRIGGERED';
  const isArmed = st.includes('ARMED') || st.includes('FORMING');
  const badgeCls = isTriggered
    ? 'nvda-badge-PASS'
    : st === 'WATCHING'
    ? 'nvda-badge-CLOSED'
    : 'nvda-badge-ARMED';
  const p = card.plan;

  return (
    <div
      className={`nvda-card nvda-setup-card ${
        isTriggered ? 'triggered' : isArmed ? 'armed' : ''
      }`}
    >
      <div className="nvda-card-header">
        <div>
          <div className="nvda-card-title">{card.name}</div>
          <div className="nvda-card-sub">Window: {card.window}</div>
        </div>
        <span className={`nvda-gate-badge ${badgeCls} tabular`}>{st}</span>
      </div>

      <ul className="nvda-checklist">
        {card.checklist.map((c, idx) => (
          <li key={idx} className={`nvda-check-item ${c.done ? 'done' : ''}`}>
            <div className="nvda-check-left">
              <span className={`nvda-check-icon ${c.done ? 'nvda-pos' : 'nvda-warn-txt'}`}>
                {c.done ? '✓' : '○'}
              </span>
              <span>{c.label}</span>
            </div>
            <span className="nvda-check-detail tabular">{c.detail}</span>
          </li>
        ))}
      </ul>

      <div className="nvda-plan-box">
        <div
          className="nvda-plan-box-label"
          style={{
            color: p.is_confirmed ? '#34d399' : 'var(--text-secondary)',
          }}
        >
          {p.is_confirmed
            ? 'CONFIRMED SIGNAL PARAMETERS'
            : 'PROJECTED EXECUTION PARAMETERS (WAITING FOR TRIGGER)'}
        </div>
        <div className="nvda-plan-grid tabular">
          <div className="nvda-plan-kv">
            <div className="k">{p.is_confirmed ? 'Signal Px' : 'Est Entry'}</div>
            <div className="v">${p.entry.toFixed(2)}</div>
          </div>
          <div className="nvda-plan-kv">
            <div className="k">Hard Stop</div>
            <div className="v nvda-neg">${p.hard_stop.toFixed(2)}</div>
          </div>
          <div className="nvda-plan-kv">
            <div className="k">Tranche 1 (50%)</div>
            <div className="v nvda-pos">${p.target_1.toFixed(2)}</div>
          </div>
          <div className="nvda-plan-kv">
            <div className="k">Tranche 2 (50%)</div>
            <div className="v nvda-pos">${p.target_2.toFixed(2)}</div>
          </div>
        </div>
        <div className="nvda-plan-note">
          Tight structural stop ref:{' '}
          <span className="tabular" style={{ color: 'var(--text-primary)' }}>
            ${p.tight_stop.toFixed(2)}
          </span>{' '}
          &bull; {p.swing_note}
        </div>
      </div>
    </div>
  );
};

export const NvdaDashboardView: React.FC<NvdaDashboardViewProps> = ({
  state,
  audioEnabled,
  onToggleAudio,
  onTestChime,
  onRefresh,
  isRefreshing,
}) => {
  const [notionalStr, setNotionalStr] = useState<string>('100000');
  const [activePreset, setActivePreset] = useState<number | null>(100000);
  const [setupMode, setSetupMode] = useState<'AUTO' | 'A' | 'B'>('AUTO');
  const [copiedFlag, setCopiedFlag] = useState<string | null>(null);
  const [hoverBar, setHoverBar] = useState<NvdaChartBar | null>(null);

  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);

  const drawChart = useCallback(() => {
    if (!state || !state.ok || !state.levels) return;
    const canvas = canvasRef.current;
    const wrap = wrapRef.current;
    if (!canvas || !wrap) return;

    const rect = wrap.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return;

    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.floor(rect.width * dpr);
    canvas.height = Math.floor(rect.height * dpr);
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.scale(dpr, dpr);

    const W = rect.width;
    const H = rect.height;
    ctx.clearRect(0, 0, W, H);

    const bars = state.chart_bars || [];
    if (!bars.length) return;

    const lv = state.levels;
    const padL = 10;
    const padR = 96;
    const padT = 14;
    const padB = 24;
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;

    const minP = Math.min(lv.hard_stop - 0.4, ...bars.map((b) => b.l));
    const maxP = Math.max(lv.pm_high + 0.5, ...bars.map((b) => b.h));
    const rangeP = Math.max(1.0, maxP - minP);
    const yOf = (p: number) => padT + ((maxP - p) / rangeP) * plotH;
    const stepX = plotW / Math.max(bars.length, 1);
    const xOf = (i: number) => padL + (i + 0.5) * stepX;

    // Premarket vs RTH shading
    const firstRthIdx = bars.findIndex((b) => b.session === 'RTH');
    if (firstRthIdx === -1) {
      ctx.fillStyle = 'rgba(30, 41, 59, 0.26)';
      ctx.fillRect(padL, padT, plotW, plotH);
    } else if (firstRthIdx > 0) {
      const splitX = padL + firstRthIdx * stepX;
      ctx.fillStyle = 'rgba(30, 41, 59, 0.28)';
      ctx.fillRect(padL, padT, splitX - padL, plotH);
      ctx.strokeStyle = '#475569';
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(splitX, padT);
      ctx.lineTo(splitX, padT + plotH);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = '#94a3b8';
      ctx.font = 'bold 10px Inter, -apple-system, sans-serif';
      ctx.fillText('09:30 BELL', splitX + 4, padT + 11);
    }

    // Setup A Confluence Flush Zone ($170.90 - $172.20)
    const yZTop = yOf(lv.flush_zone_high);
    const yZBot = yOf(lv.flush_zone_low);
    ctx.fillStyle = 'rgba(16, 185, 129, 0.13)';
    ctx.fillRect(padL, yZTop, plotW, Math.max(2, yZBot - yZTop));
    ctx.strokeStyle = 'rgba(16, 185, 129, 0.45)';
    ctx.lineWidth = 1;
    ctx.strokeRect(padL, yZTop, plotW, Math.max(2, yZBot - yZTop));

    // Horizontal Reference Lines
    const hLines = [
      {
        p: lv.pm_high,
        color: '#06b6d4',
        label: `PM Hi ${lv.pm_high.toFixed(2)}`,
        dash: [4, 3],
      },
      {
        p: lv.pm_vwap,
        color: '#facc15',
        label: `PM VWAP ${lv.pm_vwap.toFixed(2)}`,
        dash: [5, 3],
      },
      {
        p: lv.flush_zone_high,
        color: '#10b981',
        label: `Zone ${lv.flush_zone_high.toFixed(2)}`,
        dash: [2, 3],
      },
      {
        p: lv.prev_close,
        color: '#64748b',
        label: `Cls ${lv.prev_close.toFixed(2)}`,
        dash: [3, 3],
      },
      {
        p: lv.hard_stop,
        color: '#ef4444',
        label: `Stop ${lv.hard_stop.toFixed(2)}`,
        dash: [] as number[],
      },
    ];
    ctx.font = '10.5px SF Mono, Menlo, monospace';
    hLines.forEach((hl) => {
      if (hl.p < minP || hl.p > maxP) return;
      const y = yOf(hl.p);
      ctx.strokeStyle = hl.color;
      ctx.lineWidth = hl.dash.length ? 1 : 1.4;
      ctx.setLineDash(hl.dash);
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(padL + plotW, y);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = hl.color;
      ctx.fillText(hl.label, padL + plotW + 4, y + 3);
    });

    // Candlesticks
    const bodyW = Math.max(2, Math.min(9, stepX * 0.68));
    bars.forEach((b, i) => {
      const x = xOf(i);
      const up = b.c >= b.o;
      const color = up ? '#10b981' : '#ef4444';
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.1;
      ctx.beginPath();
      ctx.moveTo(x, yOf(b.h));
      ctx.lineTo(x, yOf(b.l));
      ctx.stroke();

      const yTop = yOf(Math.max(b.o, b.c));
      const yBot = yOf(Math.min(b.o, b.c));
      const bh = Math.max(1.5, yBot - yTop);
      ctx.fillStyle = b.forming
        ? up
          ? 'rgba(16,185,129,0.45)'
          : 'rgba(239,68,68,0.45)'
        : color;
      ctx.fillRect(x - bodyW / 2, yTop, bodyW, bh);
      if (b.forming) {
        ctx.strokeRect(x - bodyW / 2, yTop, bodyW, bh);
      }
    });

    // Running RTH VWAP Curve
    ctx.strokeStyle = '#a855f7';
    ctx.lineWidth = 1.8;
    ctx.beginPath();
    let started = false;
    bars.forEach((b, i) => {
      if (b.rth_vwap !== null && b.rth_vwap !== undefined) {
        const x = xOf(i);
        const y = yOf(b.rth_vwap);
        if (!started) {
          ctx.moveTo(x, y);
          started = true;
        } else {
          ctx.lineTo(x, y);
        }
      }
    });
    if (started) ctx.stroke();

    // Confirmed Signal Arrows
    (state.signals || []).forEach((sig) => {
      const idx = bars.findIndex((b) => b.epoch_ms === sig.bar_epoch_ms);
      if (idx >= 0) {
        const x = xOf(idx);
        const y = yOf(bars[idx].l) + 14;
        ctx.fillStyle = '#34d399';
        ctx.beginPath();
        ctx.moveTo(x, y - 10);
        ctx.lineTo(x - 6, y);
        ctx.lineTo(x + 6, y);
        ctx.closePath();
        ctx.fill();
        ctx.font = 'bold 10px SF Mono, monospace';
        ctx.fillText(sig.setup === 'SETUP_A' ? 'BUY A' : 'BUY B', x - 16, y + 11);
      }
    });

    // Time X-Axis Labels
    ctx.fillStyle = '#64748b';
    ctx.font = '10px SF Mono, Menlo, monospace';
    const labelStride = Math.max(1, Math.floor(bars.length / 8));
    bars.forEach((b, i) => {
      if (i % labelStride === 0 || i === bars.length - 1) {
        ctx.fillText(b.ts, xOf(i) - 14, H - 6);
      }
    });
  }, [state]);

  useEffect(() => {
    drawChart();
    const wrap = wrapRef.current;
    if (!wrap) return;
    const ro = new ResizeObserver(() => drawChart());
    ro.observe(wrap);
    return () => ro.disconnect();
  }, [drawChart]);

  const handleMouseMove = (e: React.MouseEvent<HTMLCanvasElement>) => {
    if (!state?.chart_bars?.length || !wrapRef.current) return;
    const rect = wrapRef.current.getBoundingClientRect();
    const padL = 10;
    const padR = 96;
    const plotW = rect.width - padL - padR;
    const relX = e.clientX - rect.left - padL;
    if (relX < 0 || relX > plotW) {
      setHoverBar(null);
      return;
    }
    const idx = Math.min(
      state.chart_bars.length - 1,
      Math.max(0, Math.floor((relX / plotW) * state.chart_bars.length))
    );
    setHoverBar(state.chart_bars[idx]);
  };

  const sizerCalc = useMemo(() => {
    if (!state || !state.ok || !state.setup_a || !state.setup_b) return null;
    const notional = Math.max(1000, parseFloat(notionalStr) || 100000);
    let chosen = state.setup_a;
    if (setupMode === 'B') {
      chosen = state.setup_b;
    } else if (setupMode === 'AUTO') {
      if (
        state.setup_b.status === 'TRIGGERED' &&
        state.setup_a.status !== 'TRIGGERED'
      ) {
        chosen = state.setup_b;
      } else if (
        state.setup_b.status.includes('FORMING') &&
        state.setup_a.status === 'WATCHING'
      ) {
        chosen = state.setup_b;
      }
    }
    const p = chosen.plan;
    const shares = Math.max(1, Math.floor(notional / p.entry));
    const t1Shares = Math.floor(shares / 2);
    const t2Shares = shares - t1Shares;
    const deployed = shares * p.entry;

    const riskHard = shares * Math.max(0.01, p.entry - p.hard_stop);
    const riskTight = shares * Math.max(0.01, p.entry - p.tight_stop);
    const gainT1 = t1Shares * (p.target_1 - p.entry);
    const gainT2 = t2Shares * (p.target_2 - p.entry);
    const totalBlendedGain = gainT1 + gainT2;
    const rMultHard = totalBlendedGain / riskHard;
    const rMultTight = totalBlendedGain / riskTight;

    const copyText = [
      `NVDA Trade Plan (${chosen.name}) — ${
        p.is_confirmed ? 'CONFIRMED SIGNAL' : 'STANDBY / PROJECTED'
      }`,
      `Buy ${shares} sh NVDA Limit @ $${p.limit_cap.toFixed(
        2
      )} (Ref $${p.entry.toFixed(2)}, Deployed $${deployed.toLocaleString(
        undefined,
        { maximumFractionDigits: 0 }
      )})`,
      `Hard Stop: ${shares} sh @ $${p.hard_stop.toFixed(
        2
      )} (-$${riskHard.toFixed(0)} | Tight Stop $${p.tight_stop.toFixed(
        2
      )} = -$${riskTight.toFixed(0)})`,
      `Tranche 1 Sell (50% = ${t1Shares} sh): Limit @ $${p.target_1.toFixed(
        2
      )} (+$${gainT1.toFixed(0)})`,
      `Tranche 2 Sell (50% = ${t2Shares} sh): Limit @ $${p.target_2.toFixed(
        2
      )} (+$${gainT2.toFixed(0)}) or hold T-4->T0 into Tue Nov 17 close`,
    ].join('\n');

    return {
      chosen,
      p,
      shares,
      t1Shares,
      t2Shares,
      deployed,
      riskHard,
      riskTight,
      gainT1,
      gainT2,
      totalBlendedGain,
      rMultHard,
      rMultTight,
      copyText,
    };
  }, [state, notionalStr, setupMode]);

  const handleCopy = async (text: string, flag: string) => {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // ignore clipboard restriction in headless environments
    }
    setCopiedFlag(flag);
    window.setTimeout(() => setCopiedFlag(null), 1600);
  };

  if (!state || !state.ok || !state.nvda || !state.banner || !state.levels) {
    return (
      <main className="nvda-workspace">
        <div className="nvda-card" style={{ padding: '2rem', textAlign: 'center' }}>
          <h2 style={{ fontSize: '16px', marginBottom: '0.5rem' }}>
            NVDA Signal Monitor — Initializing Live Feed
          </h2>
          <p style={{ color: 'var(--text-secondary)', marginBottom: '1rem' }}>
            {state?.message ||
              state?.last_error ||
              'Fetching CNBC Harmony 5M extended-hours & RTH bars for NVDA, CRWV, SOXL, and semi peers...'}
          </p>
          <button type="button" className="nvda-btn" onClick={onRefresh}>
            ↻ Retry Feed Poll
          </button>
        </div>
      </main>
    );
  }

  const nv = state.nvda;
  const lv = state.levels;
  const banner = state.banner;
  const activeBar =
    hoverBar ||
    (state.chart_bars && state.chart_bars.length > 0
      ? state.chart_bars[state.chart_bars.length - 1]
      : null);

  return (
    <main className="nvda-workspace">
      {/* Sub-header Playbook Controls */}
      <div className="nvda-topbar">
        <div className="nvda-brand-row">
          <span className="nvda-live-dot" />
          <h2 className="nvda-view-title">
            NVDA Signal Monitor{' '}
            <span className="nvda-view-sub">
              | CRWV &amp; SOXL Tailwind Playbook
            </span>
          </h2>
          <span className="nvda-phase-pill tabular">{state.market_phase}</span>
        </div>

        <div className="nvda-controls">
          <span className="nvda-clock tabular">{state.updated_at_et}</span>
          <button
            type="button"
            className={`nvda-btn ${audioEnabled ? 'active' : ''}`}
            onClick={onToggleAudio}
          >
            {audioEnabled
              ? '🔔 Alerts ARMED (Audio + Desktop)'
              : '🔔 Enable Audio + Desktop Alerts'}
          </button>
          <button type="button" className="nvda-btn" onClick={onTestChime}>
            🔊 Test Chime
          </button>
          <button
            type="button"
            className="nvda-btn"
            onClick={onRefresh}
            disabled={isRefreshing}
          >
            {isRefreshing ? '↻ Refreshing...' : '↻ Refresh Now'}
          </button>
          <button
            type="button"
            className="nvda-btn"
            onClick={() =>
              window.open(
                'https://my.tastytrade.com/app.html#/trading',
                '_blank',
                'noopener,noreferrer'
              )
            }
          >
            ↗ Open Tastytrade
          </button>
        </div>
      </div>

      {/* Master Signal Banner */}
      <section className={`nvda-banner ${banner.severity}`}>
        <div className="nvda-banner-body">
          <div className="nvda-banner-title">{banner.headline}</div>
          <div className="nvda-banner-sub">{banner.subtext}</div>
        </div>
        <div className="nvda-hero-quote tabular">
          <div className="nvda-hero-price">${nv.last.toFixed(2)}</div>
          <div
            className={`nvda-hero-chg ${
              nv.chg_pct >= 0 ? 'nvda-pos' : 'nvda-neg'
            }`}
          >
            {fmtPct(nv.chg_pct)} vs ${nv.prev_close.toFixed(2)} |{' '}
            {fmtPct(nv.vs_pm_vwap_pct)} vs PM_VWAP
          </div>
          <div className="nvda-timer-sub">
            {state.market_phase === 'PREMARKET'
              ? `09:30 Bell in ${Math.floor((state.sec_to_bell || 0) / 60)}m ${String(
                  (state.sec_to_bell || 0) % 60
                ).padStart(2, '0')}s`
              : `Next 5m bar close in ${state.sec_to_next_5m_close ?? 0}s`}
          </div>
        </div>
      </section>

      {/* 9-Ticker Peer Tailwind Strip */}
      <section className="nvda-peer-strip" aria-label="Semiconductor & Macro Peers">
        {(state.peers || []).map((p) => {
          const hi =
            p.symbol === 'NVDA' || p.symbol === 'CRWV' || p.symbol === 'SOXL'
              ? 'highlight'
              : '';
          return (
            <div key={p.symbol} className={`nvda-peer-card ${hi} tabular`}>
              <div className="nvda-peer-top">
                <span>{p.symbol}</span>
                <span className={p.chg_pct >= 0 ? 'nvda-pos' : 'nvda-neg'}>
                  {fmtPct(p.chg_pct)}
                </span>
              </div>
              <div className="nvda-peer-px">${p.last.toFixed(2)}</div>
              <div className="nvda-peer-sub">
                PM VWAP ${p.pm_vwap ? p.pm_vwap.toFixed(2) : '--'} (
                {fmtPct(p.vs_pm_vwap_pct)})
              </div>
            </div>
          );
        })}
      </section>

      {/* Chart + Live Tailwind Gates */}
      <div className="nvda-grid-main">
        <div className="nvda-card">
          <div className="nvda-card-header">
            <div className="nvda-card-title">
              NVDA 5-Minute Extended + RTH Chart &amp; Key Levels
            </div>
            <div className="nvda-card-sub tabular">
              {state.session_date} | Last 5m Bar {nv.last_bar_ts} ET | PM Range $
              {lv.pm_low.toFixed(2)}–${lv.pm_high.toFixed(2)}
            </div>
          </div>

          {activeBar && (
            <div className="nvda-ohlcv-strip tabular">
              <span>
                <strong>{activeBar.ts} ET</strong> ({activeBar.session}
                {activeBar.forming ? ' · FORMING' : ''})
              </span>
              <span>O: ${activeBar.o.toFixed(2)}</span>
              <span>H: ${activeBar.h.toFixed(2)}</span>
              <span>L: ${activeBar.l.toFixed(2)}</span>
              <span>C: ${activeBar.c.toFixed(2)}</span>
              <span>Vol: {activeBar.v.toLocaleString()}</span>
              {activeBar.rth_vwap !== null && (
                <span style={{ color: '#c084fc' }}>
                  RTH VWAP: ${activeBar.rth_vwap.toFixed(2)}
                </span>
              )}
            </div>
          )}

          <div className="nvda-chart-wrap" ref={wrapRef}>
            <canvas
              ref={canvasRef}
              className="nvda-chart-canvas"
              onMouseMove={handleMouseMove}
              onMouseLeave={() => setHoverBar(null)}
            />
          </div>

          <div className="nvda-legend-row">
            <div className="nvda-leg-item">
              <span className="nvda-leg-swatch" style={{ background: '#facc15' }} />
              PM VWAP (T1)
            </div>
            <div className="nvda-leg-item">
              <span className="nvda-leg-swatch" style={{ background: '#a855f7' }} />
              Live RTH VWAP
            </div>
            <div className="nvda-leg-item">
              <span className="nvda-leg-swatch" style={{ background: '#06b6d4' }} />
              PM High (T2)
            </div>
            <div className="nvda-leg-item">
              <span
                className="nvda-leg-swatch"
                style={{ background: '#10b981', height: '8px', opacity: 0.45 }}
              />
              Setup A Flush Zone ($170.90–$172.20)
            </div>
            <div className="nvda-leg-item">
              <span className="nvda-leg-swatch" style={{ background: '#ef4444' }} />
              Hard Stop ($168.40)
            </div>
          </div>
        </div>

        <div className="nvda-card">
          <div className="nvda-card-header">
            <div className="nvda-card-title">Live Tailwind &amp; Safety Gates</div>
            <div className="nvda-card-sub tabular">
              PM Vol {nv.pm_vol_m.toFixed(1)}M | RTH Vol {nv.rth_vol_m.toFixed(1)}M
            </div>
          </div>

          <div className="nvda-gates-list">
            {(state.gates || []).map((g) => (
              <div key={g.id} className="nvda-gate-row">
                <div>
                  <div className="nvda-gate-title">
                    {g.id}: {g.name}
                  </div>
                  <div className="nvda-gate-note">{g.note}</div>
                  <div className="nvda-gate-req tabular">
                    Now: {g.current} &bull; Req: {g.threshold}
                  </div>
                </div>
                <span className={`nvda-gate-badge nvda-badge-${g.state} tabular`}>
                  {g.state}
                </span>
              </div>
            ))}
          </div>

          <div className="nvda-ladder-section">
            <div className="nvda-ladder-heading">LOCKED REFERENCE LADDER</div>
            <div className="nvda-ladder-grid tabular">
              <div>
                Target 2 (Nov 9 Hi):{' '}
                <strong className="nvda-pos">${lv.target_180.toFixed(2)}</strong>
              </div>
              <div>
                PM High (T2):{' '}
                <strong style={{ color: '#06b6d4' }}>
                  ${lv.pm_high.toFixed(2)}
                </strong>
              </div>
              <div>
                PM VWAP (T1):{' '}
                <strong style={{ color: '#facc15' }}>
                  ${lv.pm_vwap.toFixed(2)}
                </strong>
              </div>
              <div>
                Live RTH VWAP:{' '}
                <strong style={{ color: '#a855f7' }}>
                  {lv.rth_vwap ? `$${lv.rth_vwap.toFixed(2)}` : 'Wait 09:30'}
                </strong>
              </div>
              <div>
                Flush Zone:{' '}
                <strong className="nvda-pos">
                  ${lv.flush_zone_low.toFixed(2)}–${lv.flush_zone_high.toFixed(2)}
                </strong>
              </div>
              <div>
                50% Gap / PM Lo:{' '}
                <strong>
                  ${lv.gap_fill_50.toFixed(2)} / ${lv.pm_low.toFixed(2)}
                </strong>
              </div>
              <div>
                Prev Close: <strong>${lv.prev_close.toFixed(2)}</strong>
              </div>
              <div>
                Hard Stop Guard:{' '}
                <strong className="nvda-neg">${lv.hard_stop.toFixed(2)}</strong>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Setup A & Setup B Cards */}
      {state.setup_a && state.setup_b && (
        <div className="nvda-grid-setups">
          <SetupCardView card={state.setup_a} />
          <SetupCardView card={state.setup_b} />
        </div>
      )}

      {/* Position Sizer & Event Log */}
      <div className="nvda-grid-bottom">
        <div className="nvda-card">
          <div className="nvda-card-header">
            <div className="nvda-card-title">
              Position Sizer &amp; Execution Order Ticket
            </div>
            <div style={{ display: 'flex', gap: '0.45rem' }}>
              <button
                type="button"
                className="nvda-btn"
                onClick={() => handleCopy('NVDA', 'TICKER')}
              >
                {copiedFlag === 'TICKER' ? '✓ Ticker Copied' : 'Copy Ticker'}
              </button>
              <button
                type="button"
                className="nvda-btn"
                onClick={() =>
                  sizerCalc && handleCopy(sizerCalc.copyText, 'PLAN')
                }
              >
                {copiedFlag === 'PLAN' ? '✓ Copied!' : '📋 Copy Order Plan'}
              </button>
            </div>
          </div>

          <div className="nvda-sizer-controls">
            <span style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
              Capital:
            </span>
            {[25000, 50000, 100000, 150000].map((amt) => (
              <button
                key={amt}
                type="button"
                className={`nvda-btn ${activePreset === amt ? 'active' : ''}`}
                onClick={() => {
                  setActivePreset(amt);
                  setNotionalStr(String(amt));
                }}
              >
                ${amt / 1000}k
              </button>
            ))}
            <input
              type="number"
              className="nvda-num-input tabular"
              value={notionalStr}
              step={5000}
              aria-label="Capital Allocation USD"
              onChange={(e) => {
                setActivePreset(null);
                setNotionalStr(e.target.value);
              }}
            />
            <select
              className="nvda-num-input"
              style={{ width: 'auto' }}
              aria-label="Setup Selection"
              value={setupMode}
              onChange={(e) =>
                setSetupMode(e.target.value as 'AUTO' | 'A' | 'B')
              }
            >
              <option value="AUTO">Auto (Active / Setup A)</option>
              <option value="A">Setup A (Flush-Reclaim)</option>
              <option value="B">Setup B (VWAP Breakout)</option>
            </select>
          </div>

          {sizerCalc && (
            <table className="nvda-ticket-table tabular">
              <thead>
                <tr>
                  <th>Leg</th>
                  <th>Action / Shares</th>
                  <th>Price</th>
                  <th>$ Impact</th>
                  <th>Notes</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>
                    <strong>ENTRY</strong>
                  </td>
                  <td>
                    BUY <strong>{sizerCalc.shares}</strong> sh
                  </td>
                  <td>
                    <strong>${sizerCalc.p.entry.toFixed(2)}</strong> (Lmt $
                    {sizerCalc.p.limit_cap.toFixed(2)})
                  </td>
                  <td>
                    $
                    {sizerCalc.deployed.toLocaleString(undefined, {
                      maximumFractionDigits: 0,
                    })}
                  </td>
                  <td>
                    {sizerCalc.p.is_confirmed ? (
                      <span className="nvda-pos">SIGNAL ACTIVE</span>
                    ) : (
                      <span className="nvda-warn-txt">WAIT FOR SIGNAL</span>
                    )}
                  </td>
                </tr>
                <tr>
                  <td>
                    <strong>HARD STOP</strong>
                  </td>
                  <td className="nvda-neg">STOP {sizerCalc.shares} sh</td>
                  <td className="nvda-neg">
                    ${sizerCalc.p.hard_stop.toFixed(2)}
                  </td>
                  <td className="nvda-neg">
                    -$
                    {sizerCalc.riskHard.toLocaleString(undefined, {
                      maximumFractionDigits: 0,
                    })}{' '}
                    (-{((sizerCalc.riskHard / sizerCalc.deployed) * 100).toFixed(2)}
                    %)
                  </td>
                  <td>
                    Tight stop ${sizerCalc.p.tight_stop.toFixed(2)} (-$
                    {sizerCalc.riskTight.toFixed(0)})
                  </td>
                </tr>
                <tr>
                  <td>
                    <strong>TRANCHE 1</strong>
                  </td>
                  <td className="nvda-pos">
                    SELL {sizerCalc.t1Shares} sh (50%)
                  </td>
                  <td className="nvda-pos">
                    ${sizerCalc.p.target_1.toFixed(2)}
                  </td>
                  <td className="nvda-pos">
                    +$
                    {sizerCalc.gainT1.toLocaleString(undefined, {
                      maximumFractionDigits: 0,
                    })}
                  </td>
                  <td>Move stop to breakeven on fill</td>
                </tr>
                <tr>
                  <td>
                    <strong>TRANCHE 2</strong>
                  </td>
                  <td className="nvda-pos">
                    SELL {sizerCalc.t2Shares} sh (50%)
                  </td>
                  <td className="nvda-pos">
                    ${sizerCalc.p.target_2.toFixed(2)}
                  </td>
                  <td className="nvda-pos">
                    +$
                    {sizerCalc.gainT2.toLocaleString(undefined, {
                      maximumFractionDigits: 0,
                    })}
                  </td>
                  <td>
                    Blended +${sizerCalc.totalBlendedGain.toFixed(0)} (
                    {sizerCalc.rMultHard.toFixed(2)}R hard /{' '}
                    {sizerCalc.rMultTight.toFixed(2)}R tight)
                  </td>
                </tr>
              </tbody>
            </table>
          )}
        </div>

        <div className="nvda-card">
          <div className="nvda-card-header">
            <div className="nvda-card-title">
              Real-Time Signal &amp; Condition Log
            </div>
            <span className="nvda-card-sub tabular">
              5s poll / causal 5m replay
            </span>
          </div>
          <div className="nvda-event-list">
            {(state.events || []).map((ev) => (
              <div key={ev.key} className={`nvda-ev-item ${ev.level}`}>
                <div className="nvda-ev-top">
                  <span>{ev.title}</span>
                  <span className="nvda-ev-ts tabular">{ev.ts}</span>
                </div>
                <div className="nvda-ev-detail">{ev.detail}</div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </main>
  );
};
