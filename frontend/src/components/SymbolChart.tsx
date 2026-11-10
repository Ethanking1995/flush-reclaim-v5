import React, { useEffect, useRef, useState } from 'react';
import {
  createChart,
  ColorType,
  CrosshairMode,
  IChartApi,
  ISeriesApi,
  IPriceLine,
  LineStyle,
  Time,
} from 'lightweight-charts';
import { SymbolChartPayload } from '../types';

interface SymbolChartProps {
  chartData: SymbolChartPayload | null;
  symbol: string;
}

export const SymbolChart: React.FC<SymbolChartProps> = ({ chartData, symbol }) => {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartApiRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<'Histogram'> | null>(null);
  const vwapSeriesRef = useRef<ISeriesApi<'Line'> | null>(null);
  const ema12SeriesRef = useRef<ISeriesApi<'Line'> | null>(null);
  const ema20SeriesRef = useRef<ISeriesApi<'Line'> | null>(null);
  const priceLinesRef = useRef<IPriceLine[]>([]);
  const lastSymbolRef = useRef<string>('');

  const [showEma12, setShowEma12] = useState<boolean>(true);
  const [showEma20, setShowEma20] = useState<boolean>(true);
  const [showProposedLevels, setShowProposedLevels] = useState<boolean>(true);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    const initWidth = Math.max(
      260,
      Math.min(el.clientWidth || 640, window.innerWidth - 24)
    );
    const chart = createChart(el, {
      width: initWidth,
      height: 330,
      layout: {
        background: { type: ColorType.Solid, color: '#0b0f17' },
        textColor: '#94a3b8',
        fontFamily: "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace",
        fontSize: 11,
      },
      grid: {
        vertLines: { color: '#161f30' },
        horzLines: { color: '#161f30' },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
      },
      rightPriceScale: {
        borderColor: '#1e293b',
        scaleMargins: { top: 0.08, bottom: 0.24 },
      },
      timeScale: {
        borderColor: '#1e293b',
        timeVisible: true,
        secondsVisible: false,
      },
    });

    const candleSeries = chart.addCandlestickSeries({
      upColor: '#10b981',
      downColor: '#f43f5e',
      borderUpColor: '#10b981',
      borderDownColor: '#f43f5e',
      wickUpColor: '#10b981',
      wickDownColor: '#f43f5e',
    });

    const volumeSeries = chart.addHistogramSeries({
      priceFormat: { type: 'volume' },
      priceScaleId: '',
    });
    volumeSeries.priceScale().applyOptions({
      scaleMargins: { top: 0.80, bottom: 0.0 },
    });

    const vwapSeries = chart.addLineSeries({
      color: '#38bdf8',
      lineWidth: 2,
      title: 'Session VWAP',
      crosshairMarkerVisible: true,
    });

    const ema12Series = chart.addLineSeries({
      color: '#a855f7',
      lineWidth: 1,
      lineStyle: LineStyle.Solid,
      title: 'EMA12',
    });

    const ema20Series = chart.addLineSeries({
      color: '#f59e0b',
      lineWidth: 1,
      lineStyle: LineStyle.Dotted,
      title: 'EMA20 (Trail)',
    });

    chartApiRef.current = chart;
    candleSeriesRef.current = candleSeries;
    volumeSeriesRef.current = volumeSeries;
    vwapSeriesRef.current = vwapSeries;
    ema12SeriesRef.current = ema12Series;
    ema20SeriesRef.current = ema20Series;

    const handleResize = () => {
      if (containerRef.current && chartApiRef.current) {
        chartApiRef.current.applyOptions({
          width: containerRef.current.clientWidth,
        });
      }
    };
    window.addEventListener('resize', handleResize);

    return () => {
      window.removeEventListener('resize', handleResize);
      chart.remove();
      chartApiRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (
      !chartData ||
      !candleSeriesRef.current ||
      !volumeSeriesRef.current ||
      !vwapSeriesRef.current ||
      !ema12SeriesRef.current ||
      !ema20SeriesRef.current ||
      !chartApiRef.current
    ) {
      return;
    }

    const candles = chartData.bars.map((b) => {
      const isUp = b.close >= b.open;
      // Visually distinguish forming (incomplete) 5m candle from completed candles
      if (!b.is_completed) {
        return {
          time: b.time as Time,
          open: b.open,
          high: b.high,
          low: b.low,
          close: b.close,
          color: isUp ? 'rgba(16, 185, 129, 0.35)' : 'rgba(244, 63, 94, 0.35)',
          borderColor: '#facc15',
          wickColor: '#facc15',
        };
      }
      return {
        time: b.time as Time,
        open: b.open,
        high: b.high,
        low: b.low,
        close: b.close,
      };
    });

    const volumes = chartData.bars.map((b) => ({
      time: b.time as Time,
      value: b.volume,
      color: !b.is_completed
        ? 'rgba(250, 204, 21, 0.35)'
        : b.close >= b.open
        ? 'rgba(16, 185, 129, 0.38)'
        : 'rgba(244, 63, 94, 0.38)',
    }));

    const vwapData = chartData.bars.map((b) => ({
      time: b.time as Time,
      value: b.vwap,
    }));

    const ema12Data = showEma12
      ? chartData.bars.map((b) => ({
          time: b.time as Time,
          value: b.ema12,
        }))
      : [];

    const ema20Data = showEma20
      ? chartData.bars.map((b) => ({
          time: b.time as Time,
          value: b.ema20,
        }))
      : [];

    candleSeriesRef.current.setData(candles);
    volumeSeriesRef.current.setData(volumes);
    vwapSeriesRef.current.setData(vwapData);
    ema12SeriesRef.current.setData(ema12Data);
    ema20SeriesRef.current.setData(ema20Data);

    // Markers (Flush Low, Volume Climax, Confirmed Reclaim)
    const seriesMarkers = (chartData.markers || [])
      .slice()
      .sort((a, b) => a.time - b.time)
      .map((m) => ({
        time: m.time as Time,
        position:
          m.kind === 'FLUSH_LOW' || m.kind === 'CONFIRMED_RECLAIM'
            ? ('belowBar' as const)
            : ('aboveBar' as const),
        color: m.color || '#38bdf8',
        shape:
          m.kind === 'CONFIRMED_RECLAIM'
            ? ('arrowUp' as const)
            : m.kind === 'FLUSH_LOW'
            ? ('circle' as const)
            : ('square' as const),
        text: m.label,
      }));
    candleSeriesRef.current.setMarkers(seriesMarkers);

    // Clear previous price lines
    for (const pl of priceLinesRef.current) {
      try {
        candleSeriesRef.current.removePriceLine(pl);
      } catch {
        // ignore
      }
    }
    priceLinesRef.current = [];

    // Add Proposed Plan vs Broker Working / Manual User-Reported levels
    for (const lvl of chartData.levels || []) {
      if (lvl.source === 'PROPOSED_PLAN' && !showProposedLevels) {
        continue;
      }
      let color = '#94a3b8';
      let style = LineStyle.Dashed;
      let lineWidth: 1 | 2 = 1;

      if (lvl.source === 'PROPOSED_PLAN') {
        style = LineStyle.Dashed;
        lineWidth = 1;
        if (lvl.kind === 'ENTRY') color = '#38bdf8';
        else if (lvl.kind === 'STOP') color = '#f43f5e';
        else if (lvl.kind === 'LEG1_TARGET') color = '#10b981';
        else if (lvl.kind === 'LEG2_TARGET') color = '#34d399';
      } else if (lvl.source === 'BROKER_CONFIRMED_ORDER') {
        style = LineStyle.Solid;
        lineWidth = 2;
        color = lvl.kind.includes('STOP') ? '#ef4444' : '#22c55e';
      } else if (lvl.source === 'MANUAL_USER_REPORTED') {
        style = LineStyle.Dotted;
        lineWidth = 2;
        color = '#f59e0b';
      }

      const pl = candleSeriesRef.current.createPriceLine({
        price: lvl.price,
        color,
        lineWidth,
        lineStyle: style,
        axisLabelVisible: true,
        title: lvl.label,
      });
      priceLinesRef.current.push(pl);
    }

    // Preserve user zoom unless symbol changed
    if (lastSymbolRef.current !== symbol) {
      chartApiRef.current.timeScale().fitContent();
      lastSymbolRef.current = symbol;
    }
  }, [chartData, symbol, showEma12, showEma20, showProposedLevels]);

  const handleResetZoom = () => {
    if (chartApiRef.current) {
      chartApiRef.current.timeScale().fitContent();
    }
  };

  const latestBar =
    chartData && chartData.bars.length > 0
      ? chartData.bars[chartData.bars.length - 1]
      : null;

  return (
    <div className="chart-card">
      <div className="chart-toolbar">
        <div className="chart-title-group">
          <span className="chart-symbol-badge">{symbol} · 5m RTH</span>
          {chartData?.last_completed_bar_close_et && (
            <span className="chart-bar-meta tabular">
              Completed Close: <strong>{chartData.last_completed_bar_close_et}</strong>
            </span>
          )}
          {chartData?.forming_bar_open_et && (
            <span className="chart-forming-badge tabular" title="Current 5m bar is still forming; strategy triggers only on completed 5m bar closes">
              Forming Bar ({chartData.forming_bar_open_et})
            </span>
          )}
        </div>
        <div className="chart-controls">
          <label className="chart-toggle">
            <input
              type="checkbox"
              checked={showProposedLevels}
              onChange={(e) => setShowProposedLevels(e.target.checked)}
            />
            <span>[Proposed Plan] Levels</span>
          </label>
          <label className="chart-toggle">
            <input
              type="checkbox"
              checked={showEma12}
              onChange={(e) => setShowEma12(e.target.checked)}
            />
            <span style={{ color: '#c084fc' }}>EMA12</span>
          </label>
          <label className="chart-toggle">
            <input
              type="checkbox"
              checked={showEma20}
              onChange={(e) => setShowEma20(e.target.checked)}
            />
            <span style={{ color: '#fbbf24' }}>EMA20</span>
          </label>
          <button type="button" className="btn-ghost-xs" onClick={handleResetZoom}>
            Reset view
          </button>
        </div>
      </div>

      {latestBar && (
        <div className="chart-ohlcv-strip tabular">
          <span>O: <strong>${latestBar.open.toFixed(2)}</strong></span>
          <span>H: <strong>${latestBar.high.toFixed(2)}</strong></span>
          <span>L: <strong>${latestBar.low.toFixed(2)}</strong></span>
          <span>C: <strong>${latestBar.close.toFixed(2)}</strong></span>
          <span>Vol: <strong>{latestBar.volume.toLocaleString()}</strong></span>
          <span style={{ color: '#38bdf8' }}>
            Bar Session VWAP: <strong>${latestBar.vwap.toFixed(2)}</strong>
          </span>
          <span style={{ color: '#c084fc' }}>
            EMA12: <strong>${latestBar.ema12.toFixed(2)}</strong>
          </span>
          <span style={{ color: '#fbbf24' }}>
            EMA20: <strong>${latestBar.ema20.toFixed(2)}</strong>
          </span>
        </div>
      )}

      <div ref={containerRef} className="chart-canvas-container" />

      <div className="chart-footer">
        <div className="chart-legend">
          <span className="legend-item">
            <span className="legend-swatch swatch-dashed-cyan" /> [Proposed Plan] Entry/Targets (Dashed)
          </span>
          <span className="legend-item">
            <span className="legend-swatch swatch-solid-green" /> [Broker Working] Confirmed Order (Solid)
          </span>
          <span className="legend-item">
            <span className="legend-swatch swatch-dotted-amber" /> [User-Reported Manual] Unreconciled Note (Dotted)
          </span>
        </div>
        <a
          href="https://www.tradingview.com/lightweight-charts/"
          target="_blank"
          rel="noopener noreferrer"
          className="tv-attribution"
        >
          Charts by TradingView Lightweight Charts
        </a>
      </div>
    </div>
  );
};
