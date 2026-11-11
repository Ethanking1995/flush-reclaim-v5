"""
backend/whole_market_scanner.py
===============================
Real-time Multi-Strategy Whole-Market Scanner & 10,526-Spec Empirical Audit Engine.

Continuously scans ~4,000 liquid US equities, ADRs, cash-burning biotechs, and
2x/3x leveraged ETPs via parallel batched CNBC real-time quotes (~0.8s full-market
sweep) against strictly causal prior-session (`t-1`) baselines to surface live
signals, forming closing setups, position + SPY beta-hedge sizing, Reg NMS friction
estimates, and multi-leg order tickets across the Top 5 Surviving Strategies:

  - S1 (25% wt | 09:30 -> 16:00): Bounded Non-PEAD Weak-Trend Gap-Up Short (+SPY Hedge)
  - S2 (25% wt | 15:55 -> Next 10:00): SEC Rule 10b-18 Buyback-Shutoff MOC Vacuum (-SPY Hedge)
  - S3 (20% wt | 15:55 -> Next 09:30): 2x/3x Leveraged ETP Constant-Leverage 15:55 Washout
  - S4 (10% wt | 09:30 -> 16:00): Biotech $5+ Bounded Non-News Stop-Run Gap Reversal (-SPY Hedge)
  - S5 (20% wt | 15:55 -> Close+3d): S&P 500 1-of-N Narrow-Industry Capitulation Shock
"""

from __future__ import annotations

import concurrent.futures
import datetime
import gzip
import json
import math
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx

NY_TZ = ZoneInfo("America/New_York")

CNBC_QUOTE_URL = "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol.htm"
CNBC_BARS_URL = "https://ts-api.cnbc.com/harmony/app/bars"
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

SLEEVE_SPECS: Dict[str, Dict[str, Any]] = {
    "S1": {
        "id": "S1",
        "name": "Bounded Non-PEAD Weak-Trend Gap-Up Short",
        "short_name": "S1 · Weak-Trend Gap Short",
        "weight": 0.25,
        "max_positions": 8,
        "direction": "SHORT",
        "hedge_mode": "BUY_BETA_SPY",
        "entry_bell": "09:30 ET Open",
        "exit_bell": "15:55–16:00 ET Close",
        "window_kind": "MORNING_OPEN",
        "audited_sharpe": 3.21,
        "audited_t": 4.39,
        "audited_net_bps": 62.7,
    },
    "S2": {
        "id": "S2",
        "name": "SEC Rule 10b-18 Buyback-Shutoff MOC Vacuum",
        "short_name": "S2 · Rule 10b-18 MOC",
        "weight": 0.25,
        "max_positions": 5,
        "direction": "LONG",
        "hedge_mode": "SHORT_1X_SPY",
        "entry_bell": "15:55 ET Close",
        "exit_bell": "Next Day 10:00 AM ET",
        "window_kind": "CLOSING_1555",
        "audited_sharpe": 2.45,
        "audited_t": 2.53,
        "audited_net_bps": 170.2,
    },
    "S3": {
        "id": "S3",
        "name": "2x/3x Leveraged ETP Constant-Leverage 15:55 Washout",
        "short_name": "S3 · Lev-ETP Washout",
        "weight": 0.20,
        "max_positions": 6,
        "direction": "LONG",
        "hedge_mode": "NONE",
        "entry_bell": "15:55 ET Close",
        "exit_bell": "Next Day 09:30–09:35 ET Open",
        "window_kind": "CLOSING_1555",
        "audited_sharpe": 1.95,
        "audited_t": 2.65,
        "audited_net_bps": 30.8,
    },
    "S4": {
        "id": "S4",
        "name": "Biotech $5+ Bounded Non-News Stop-Run Reversal",
        "short_name": "S4 · Biotech Stop-Run",
        "weight": 0.10,
        "max_positions": 6,
        "direction": "LONG",
        "hedge_mode": "SHORT_BETA_SPY",
        "entry_bell": "09:30 ET Open",
        "exit_bell": "15:55–16:00 ET Close",
        "window_kind": "MORNING_OPEN",
        "audited_sharpe": 2.23,
        "audited_t": 3.07,
        "audited_net_bps": 172.6,
    },
    "S5": {
        "id": "S5",
        "name": "S&P 500 1-of-N Narrow-Industry Capitulation",
        "short_name": "S5 · S&P 500 3d Cap",
        "weight": 0.20,
        "max_positions": 5,
        "direction": "LONG",
        "hedge_mode": "COHORT_3D_UNHEDGED",
        "entry_bell": "15:55 ET Close",
        "exit_bell": "Close + 3 Trading Days",
        "window_kind": "CLOSING_1555",
        "audited_sharpe": 2.23,
        "audited_t": 3.06,
        "audited_net_bps": 92.8,
    },
}


def _safe_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return None if (math.isnan(val) or math.isinf(val)) else float(val)
    s = str(val).strip().replace(",", "").replace("%", "").replace("+", "")
    if not s or s.upper() in ("UNCH", "N/A", "NULL", "NONE", "--"):
        return 0.0 if s.upper() == "UNCH" else None
    try:
        out = float(s)
        return None if (math.isnan(out) or math.isinf(out)) else out
    except ValueError:
        return None


def _expected_cum_vol_fraction(minutes_since_open: float) -> float:
    """
    Empirical US equity intraday U-shaped cumulative volume curve from 09:30 (0m) to 16:00 (390m).
    Prevents false low-RVOL readings mid-session when evaluating S5 capitulation pace.
    """
    if minutes_since_open <= 0:
        return 0.06
    if minutes_since_open >= 390:
        return 1.00
    x = minutes_since_open / 390.0
    # Morning burst (~22% in first hour) + midday trough + closing auction surge (~24% in last hour)
    frac = 0.22 * math.pow(x, 0.55) + 0.48 * x + 0.30 * math.pow(x, 2.8)
    return max(0.06, min(1.0, frac))


def _estimate_rt_friction_bps(
    price: float,
    adtv20_usd: float,
    clip_usd: float,
    hedged: bool,
    is_short: bool,
    is_htb: bool,
) -> float:
    px = max(price, 0.50)
    adtv = max(adtv20_usd, 100_000.0)
    eff_clip = max(2_500.0, min(clip_usd, 0.01 * adtv))
    tick_1way_bps = (0.01 / px) * 10000.0
    half_spread_bps = max(2.5, 0.65 * tick_1way_bps + 1.5)
    impact_1way_bps = 110.0 * math.sqrt(eff_clip / adtv)
    rt_bps = 2.0 * (half_spread_bps + impact_1way_bps)
    if hedged:
        rt_bps += 3.5
    if is_short:
        rt_bps += 55.0 if is_htb else 3.0
    return round(rt_bps, 1)


def _classify_market_window(now_et: datetime.datetime) -> Dict[str, str]:
    wd = now_et.weekday()
    hm = now_et.hour * 60 + now_et.minute
    if wd >= 5:
        return {
            "phase": "WEEKEND",
            "badge": "WEEKEND STANDBY",
            "active_bells": "End-of-Week Closing Tape",
            "guidance": "Markets closed. Showing Friday closing triggers for S2/S3/S5 and baseline watchlists for Monday 09:30 ET.",
        }
    if hm < 9 * 60 + 30:
        return {
            "phase": "PREMARKET",
            "badge": "PREMARKET · 09:30 PREP",
            "active_bells": "S1 & S4 Fire @ 09:30:00 ET",
            "guidance": "Premarket preparation window: monitor S1 Weak-Trend Gap-Ups and S4 Biotech Stop-Run Gaps ahead of the 09:30:00 ET opening bell.",
        }
    if hm < 10 * 60 + 15:
        return {
            "phase": "MORNING_WINDOW",
            "badge": "MORNING BELL ACTIVE (S1 & S4)",
            "active_bells": "S1 & S4 Entry | S3 Exit 09:30 | S2 Exit 10:00",
            "guidance": "Morning execution window live: S1 Gap-Up Shorts & S4 Biotech Stop-Run Longs active from 09:30 Open; unwind S3 @ 09:30–09:35 and S2 @ 10:00 AM.",
        }
    if hm < 15 * 60:
        return {
            "phase": "MIDDAY_RTH",
            "badge": "MIDDAY TRACKING · 15:55 PREP",
            "active_bells": "S1 & S4 Live MTM | S3 & S5 Forming | S2 Anchor @ 15:00",
            "guidance": "Tracking live beta-hedged intraday P&L on today's 09:30 S1 & S4 cohorts while monitoring forming S3 Lev-ETP washouts & S5 S&P 500 shocks ahead of 15:55 ET.",
        }
    if hm < 15 * 60 + 45:
        return {
            "phase": "POST_3PM_ANCHOR",
            "badge": "3:00 PM ANCHOR LOCKED (S2 LIVE)",
            "active_bells": "S2, S3 & S5 Forming -> Execute 15:50–15:55 ET",
            "guidance": "15:00 ET anchor locked! Tracking live final-hour Rule 10b-18 buyback shutoff drops (S2), Lev-ETP rebalance dumps (S3), and S&P 500 capitulations (S5).",
        }
    if hm <= 16 * 60 + 2:
        return {
            "phase": "CLOSING_BELL_ACTIVE",
            "badge": "CLOSING EXECUTION BELL (15:50–16:00)",
            "active_bells": "EXECUTE S2, S3, S5 NOW | FLATTEN S1 & S4",
            "guidance": "CLOSING EXECUTION WINDOW LIVE: Enter qualifying S2 MOC Vacuums, S3 Lev-ETP Washouts & S5 S&P 500 Capitulations; flatten intraday S1 & S4 positions into 16:00.",
        }
    return {
        "phase": "AFTER_HOURS",
        "badge": "POST-CLOSE REVIEW",
        "active_bells": "Session Closed · Overnight S2/S3/S5 Active",
        "guidance": "RTH session complete. Displaying final closing triggers for S2/S3/S5 and full-day realized intraday marks for today's S1 & S4 morning cohorts.",
    }


class WholeMarketScannerService:
    """
    Background & on-demand whole-market scanner across ~4,000 US securities.
    """

    def __init__(
        self,
        baseline_dir: Optional[str] = None,
        poll_interval_sec: float = 25.0,
    ) -> None:
        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self._baseline_dir = baseline_dir or os.path.join(root_dir, "baseline_data")
        self._poll_interval_sec = poll_interval_sec
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._baseline_meta: Dict[str, Any] = {}
        self._tickers: List[Dict[str, Any]] = []
        self._by_symbol: Dict[str, Dict[str, Any]] = {}
        self._audit_summary: Dict[str, Any] = {}

        # Daily 15:00 ET anchor cache: {session_date_str: {symbol: price_at_1500}}
        self._p1500_cache_date: Optional[str] = None
        self._p1500_prices: Dict[str, float] = {}

        self._last_scan_raw: Optional[Dict[str, Any]] = None
        self._last_scan_monotonic: float = 0.0

        self._load_static_assets()

    def _load_static_assets(self) -> None:
        gz_path = os.path.join(self._baseline_dir, "whole_market_scanner_baseline.json.gz")
        audit_path = os.path.join(self._baseline_dir, "whole_market_audit_summary.json")

        if os.path.isfile(gz_path):
            with gzip.open(gz_path, "rt", encoding="utf-8") as gz:
                data = json.load(gz)
            self._baseline_meta = {
                "as_of_prior_session": data.get("as_of_prior_session", "2026-11-10"),
                "total_audited_universe": data.get("total_audited_universe", 4688),
                "live_scanner_universe_count": data.get("live_scanner_universe_count", 0),
                "guild_counts_full_universe": data.get("guild_counts_full_universe", {}),
            }
            self._tickers = data.get("tickers", [])
            self._by_symbol = {t["symbol"]: t for t in self._tickers}

        if os.path.isfile(audit_path):
            with open(audit_path, "r", encoding="utf-8") as f:
                self._audit_summary = json.load(f)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="whole-market-scanner",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.refresh_quotes()
            except Exception:
                pass
            self._stop_event.wait(self._poll_interval_sec)

    def get_audit_summary(self) -> Dict[str, Any]:
        return self._audit_summary

    def _fetch_cnbc_quotes_batched(self, symbols: List[str]) -> Tuple[Dict[str, Dict[str, Any]], str]:
        """
        Fetches real-time quotes for ~4,000 US symbols in parallel 135-ticker batches.
        Falls back cleanly to deterministic synthetic quotes if network is offline/blocked.
        """
        batch_size = 135
        batches = [symbols[i : i + batch_size] for i in range(0, len(symbols), batch_size)]
        quote_map: Dict[str, Dict[str, Any]] = {}

        def _fetch_one(batch: List[str]) -> List[Dict[str, Any]]:
            params = {
                "symbols": "|".join(batch),
                "requestMethod": "itv",
                "noform": "1",
                "partnerId": "2",
                "fund": "1",
                "exthrs": "1",
                "output": "json",
                "events": "1",
            }
            with httpx.Client(timeout=7.5, headers=HTTP_HEADERS) as client:
                resp = client.get(CNBC_QUOTE_URL, params=params)
                if resp.status_code != 200:
                    return []
                items = resp.json().get("FormattedQuoteResult", {}).get("FormattedQuote", [])
                if isinstance(items, dict):
                    return [items]
                return items if isinstance(items, list) else []

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=14) as pool:
                for batch_items in pool.map(_fetch_one, batches):
                    for q in batch_items:
                        sym = str(q.get("symbol", "")).strip().upper()
                        if sym:
                            quote_map[sym] = q
        except Exception:
            quote_map = {}

        if len(quote_map) >= max(20, int(0.15 * len(symbols))):
            return quote_map, "CNBC_REALTIME_BATCH_REST"

        # Deterministic offline fallback (used during offline unit tests or disconnected Wi-Fi)
        synth: Dict[str, Dict[str, Any]] = {}
        for idx, sym in enumerate(symbols):
            b = self._by_symbol.get(sym)
            if not b:
                continue
            pc = float(b["prev_close"])
            sig = float(b["sigma20"])
            # Create deterministic dispersion so every sleeve has realistic test coverage offline
            if sym == "SPY":
                op, last, hi, lo = pc * 1.002, pc * 1.005, pc * 1.007, pc * 0.999
            elif b.get("s1") and idx % 97 == 0:
                op = min(pc * (1.0 + 1.85 * sig), float(b["sma20"]) * 0.98)
                last = op * 0.986
                hi, lo = op * 1.005, last * 0.995
            elif b.get("s3") and idx % 19 == 0:
                op = pc * (1.0 - 0.8 * sig)
                last = pc * (1.0 - 1.9 * sig)
                hi, lo = op * 1.002, last * 0.997
            elif b.get("s4") and idx % 41 == 0:
                op = max(5.25, pc * (1.0 - 1.8 * sig))
                last = op * 1.031
                hi, lo = last * 1.01, op * 0.992
            elif b.get("s5") and idx % 83 == 0:
                op = pc * (1.0 - 0.9 * sig)
                last = pc * (1.0 - 2.9 * sig)
                hi, lo = op, last * 0.996
            else:
                op, last, hi, lo = pc * 1.001, pc * 1.002, pc * 1.008, pc * 0.995
            synth[sym] = {
                "symbol": sym,
                "open": f"{op:.4f}",
                "high": f"{hi:.4f}",
                "low": f"{lo:.4f}",
                "last": f"{last:.4f}",
                "previous_day_closing": f"{pc:.4f}",
                "volume": str(int(float(b["vol20_sh"]) * (3.1 if (b.get("s5") and idx % 83 == 0) else 0.85))),
            }
        return synth, "OFFLINE_DETERMINISTIC_FALLBACK"

    def _backfill_1500_anchor_if_needed(
        self,
        now_et: datetime.datetime,
        quote_map: Dict[str, Dict[str, Any]],
        feed_mode: str,
    ) -> None:
        """
        Locks the 15:00 ET price anchor for SPY and S2 Large/Mega-Cap buyback issuers.
        If the server booted after 15:02 ET during live RTH, fetches exact 15:00 ET 5m bar closes
        for SPY and top S2 candidates so Rule 10b-18 final-hour relative drops are exact.
        """
        today_str = now_et.strftime("%Y-%m-%d")
        hm = now_et.hour * 60 + now_et.minute
        if self._p1500_cache_date != today_str:
            self._p1500_cache_date = today_str
            self._p1500_prices = {}

        if hm < 15 * 60 or self._p1500_prices:
            return

        # If right around 15:00–15:04 ET, current live quote IS the 15:00 anchor!
        if hm <= 15 * 60 + 4 or feed_mode != "CNBC_REALTIME_BATCH_REST":
            for sym, q in quote_map.items():
                px = _safe_float(q.get("last"))
                if px and px > 0:
                    self._p1500_prices[sym] = px
            return

        # Server started after 15:04 ET: fetch exact 15:00 bar close for SPY + S2 Large/Mega-Cap universe
        s2_syms = ["SPY"] + [t["symbol"] for t in self._tickers if t.get("s2")]
        ymd = now_et.strftime("%Y%m%d")
        start_s = f"{ymd}093000"
        end_s = f"{ymd}150500"

        def _fetch_1500_bar(sym: str) -> Tuple[str, Optional[float]]:
            url = f"{CNBC_BARS_URL}/{sym}/5M/{start_s}/{end_s}/adjusted/EST5EDT.json"
            try:
                with httpx.Client(timeout=5.0, headers=HTTP_HEADERS) as client:
                    r = client.get(url)
                    if r.status_code != 200:
                        return sym, None
                    bars = r.json().get("barData", {}).get("priceBars", []) or []
                    for b in reversed(bars):
                        tt = str(b.get("tradeTime", ""))
                        if len(tt) >= 12 and "0930" <= tt[8:12] <= "1500":
                            c = _safe_float(b.get("close"))
                            if c and c > 0:
                                return sym, c
            except Exception:
                pass
            return sym, None

        # Only fetch 5m bars for SPY + S2 stocks whose full-day move could plausibly qualify (fast ~0.7s!)
        priority_syms = ["SPY"]
        for sym in s2_syms[1:]:
            q = quote_map.get(sym)
            if not q:
                continue
            op = _safe_float(q.get("open"))
            last = _safe_float(q.get("last"))
            if op and last and (last / op - 1.0) <= 0.005:
                priority_syms.append(sym)

        with concurrent.futures.ThreadPoolExecutor(max_workers=14) as pool:
            for sym, px1500 in pool.map(_fetch_1500_bar, priority_syms[:220]):
                if px1500 and px1500 > 0:
                    self._p1500_prices[sym] = px1500

        # For remaining S2 stocks that rallied all day (> +0.5%), approximate with current/open midpoint
        for sym in s2_syms:
            if sym not in self._p1500_prices:
                q = quote_map.get(sym, {})
                op = _safe_float(q.get("open"))
                last = _safe_float(q.get("last"))
                if op and last:
                    self._p1500_prices[sym] = 0.35 * op + 0.65 * last

    def refresh_quotes(self) -> Dict[str, Any]:
        t0 = time.perf_counter()
        now_et = datetime.datetime.now(NY_TZ)
        symbols = [t["symbol"] for t in self._tickers]
        if "SPY" not in self._by_symbol:
            symbols.append("SPY")

        quote_map, feed_mode = self._fetch_cnbc_quotes_batched(symbols)
        self._backfill_1500_anchor_if_needed(now_et, quote_map, feed_mode)
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)

        raw_state = {
            "fetched_at_et": now_et.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "now_et": now_et,
            "quote_map": quote_map,
            "feed_mode": feed_mode,
            "scan_latency_ms": elapsed_ms,
            "quotes_received": len(quote_map),
        }
        with self._lock:
            self._last_scan_raw = raw_state
            self._last_scan_monotonic = time.monotonic()
        return raw_state

    def get_scan_payload(
        self,
        book_capital_usd: float = 100_000.0,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        book_cap = max(10_000.0, min(float(book_capital_usd or 100_000.0), 25_000_000.0))
        with self._lock:
            raw = self._last_scan_raw
            age = time.monotonic() - self._last_scan_monotonic if raw else 9999.0

        if force_refresh or raw is None or age > 30.0:
            raw = self.refresh_quotes()

        now_et: datetime.datetime = raw["now_et"]
        quote_map: Dict[str, Dict[str, Any]] = raw["quote_map"]
        feed_mode: str = raw["feed_mode"]
        win_info = _classify_market_window(now_et)
        hm = now_et.hour * 60 + now_et.minute
        mins_since_open = max(0.0, min(390.0, float(hm - (9 * 60 + 30))))
        cum_vol_frac = _expected_cum_vol_fraction(mins_since_open)
        is_closing_window = (15 * 60 + 45) <= hm <= (16 * 60 + 5)
        is_post_3pm = hm >= 15 * 60

        # SPY live benchmark
        spy_base = self._by_symbol.get("SPY", {"prev_close": 749.92})
        spy_q = quote_map.get("SPY", {})
        spy_prev = _safe_float(spy_q.get("previous_day_closing")) or float(spy_base["prev_close"])
        spy_last = _safe_float(spy_q.get("last")) or spy_prev
        spy_open = _safe_float(spy_q.get("open")) or spy_last
        spy_high = _safe_float(spy_q.get("high")) or max(spy_open, spy_last)
        spy_low = _safe_float(spy_q.get("low")) or min(spy_open, spy_last)
        spy_day_ret = (spy_last / spy_prev - 1.0) if spy_prev > 0 else 0.0
        spy_intraday_ret = (spy_last / spy_open - 1.0) if spy_open > 0 else 0.0
        spy_p1500 = self._p1500_prices.get("SPY")

        # Pass 1: Cross-sectional narrow-industry aggregates (matching mkt_step4c_lock_exact_top5.py!)
        ind_cc_sum: Dict[str, float] = {}
        ind_cc_cnt: Dict[str, int] = {}
        ind_gap_sum: Dict[str, float] = {}
        ind_gap_cnt: Dict[str, int] = {}
        parsed_quotes: Dict[str, Dict[str, float]] = {}

        for t in self._tickers:
            sym = t["symbol"]
            q = quote_map.get(sym)
            if not q:
                continue
            last = _safe_float(q.get("last"))
            pclose = _safe_float(q.get("previous_day_closing")) or float(t["prev_close"])
            if not last or last <= 0 or pclose <= 0:
                continue
            # Reject unadjusted corporate splits (> 65% discrepancy vs stored causal prev_close)
            if abs(pclose / float(t["prev_close"]) - 1.0) > 0.35:
                continue
            op = _safe_float(q.get("open")) or last
            hi = _safe_float(q.get("high")) or max(op, last)
            lo = _safe_float(q.get("low")) or min(op, last)
            vol = _safe_float(q.get("volume")) or 0.0

            ret_cc = last / pclose - 1.0
            ret_gap = op / pclose - 1.0
            if abs(ret_cc) >= 0.85 or abs(ret_gap) >= 0.75:
                continue

            parsed_quotes[sym] = {
                "open": op,
                "high": max(hi, op, last),
                "low": min(lo, op, last),
                "last": last,
                "prev_close": pclose,
                "volume": vol,
                "ret_cc": ret_cc,
                "ret_gap": ret_gap,
            }
            ind = t.get("industry", "")
            if ind:
                ind_cc_sum[ind] = ind_cc_sum.get(ind, 0.0) + ret_cc
                ind_cc_cnt[ind] = ind_cc_cnt.get(ind, 0) + 1
                ind_gap_sum[ind] = ind_gap_sum.get(ind, 0.0) + ret_gap
                ind_gap_cnt[ind] = ind_gap_cnt.get(ind, 0) + 1

        # Pass 2: Evaluate all 5 strategy sleeves
        s1_qual: List[Dict[str, Any]] = []
        s1_near: List[Dict[str, Any]] = []
        s2_qual: List[Dict[str, Any]] = []
        s2_near: List[Dict[str, Any]] = []
        s3_qual: List[Dict[str, Any]] = []
        s3_near: List[Dict[str, Any]] = []
        s4_qual: List[Dict[str, Any]] = []
        s4_near: List[Dict[str, Any]] = []
        s5_qual: List[Dict[str, Any]] = []
        s5_near: List[Dict[str, Any]] = []

        for t in self._tickers:
            sym = t["symbol"]
            pq = parsed_quotes.get(sym)
            if not pq:
                continue
            op = pq["open"]
            hi = pq["high"]
            lo = pq["low"]
            last = pq["last"]
            pclose = pq["prev_close"]
            vol = pq["volume"]
            ret_cc = pq["ret_cc"]
            ret_gap = pq["ret_gap"]
            rng = hi - lo
            ibs = ((last - lo) / rng) if rng > 1e-6 else 0.5

            sig20 = float(t["sigma20"])
            sma20 = float(t["sma20"])
            adtv20 = float(t["adtv20_usd"])
            vol20_sh = max(float(t["vol20_sh"]), 1.0)
            prev_rvol = float(t["prev_rvol"])
            beta = float(t["beta_spy"])
            ind = t.get("industry", "")
            guild = t.get("guild", "")
            mcap = float(t.get("market_cap", 0.0))

            z_gap = ret_gap / sig20
            z1d_c = ret_cc / sig20
            pace_rvol = vol / (vol20_sh * cum_vol_frac)
            raw_rvol = vol / vol20_sh
            eff_rvol = raw_rvol if hm >= (15 * 60 + 40) else max(raw_rvol, pace_rvol)

            # Peer-isolated industry residuals
            n_ind = ind_cc_cnt.get(ind, 0)
            if n_ind >= 6:
                peer_cc = (ind_cc_sum[ind] - ret_cc) / float(n_ind - 1)
                ind_resid_z = (ret_cc - peer_cc) / sig20
                peer_gap = (ind_gap_sum[ind] - ret_gap) / float(n_ind - 1)
                ind_gap_z = (ret_gap - peer_gap) / sig20
            else:
                peer_cc = None
                ind_resid_z = None
                peer_gap = None
                ind_gap_z = None

            # ---------------- S1: Weak-Trend Gap-Up Short (09:30 -> 16:00) ----------------
            if t.get("s1") and op >= 15.0 and adtv20 >= 10_000_000:
                weak_trend = op < sma20
                in_z_band = 1.30 <= z_gap <= 2.80
                catalyst_warn = ret_gap >= 0.08
                if weak_trend and in_z_band:
                    # Live MTM from 09:30 Open -> Last (honoring +6% hard stop on short leg)
                    capped_stock_ret = min(hi / op - 1.0, 0.06) if hi >= op * 1.06 else (last / op - 1.0)
                    stopped_out = hi >= op * 1.06
                    hedged_ret = -capped_stock_ret + beta * spy_intraday_ret
                    status = (
                        "SIGNAL_ACTIVE"
                        if win_info["phase"] in ("PREMARKET", "MORNING_WINDOW")
                        else "MORNING_COHORT_LIVE"
                    )
                    s1_qual.append(
                        self._build_candidate(
                            sleeve_id="S1",
                            status=status,
                            sort_score=z_gap,
                            t=t,
                            pq=pq,
                            ref_price=op,
                            stop_price=round(op * 1.06, 2),
                            primary_label=f"z_gap {z_gap:+.2f}σ ({ret_gap*100:+.1f}%)",
                            secondary_label=f"Open ${op:.2f} < SMA20 ${sma20:.2f}",
                            metric_value=round(z_gap, 3),
                            ibs=ibs,
                            eff_rvol=eff_rvol,
                            book_cap=book_cap,
                            spy_last=spy_last,
                            live_mtm_bps=round(hedged_ret * 10000.0, 1),
                            catalyst_warning=catalyst_warn,
                            stopped_out=stopped_out,
                            gates=[
                                {
                                    "name": "Profitable ETB Common Stock",
                                    "pass": True,
                                    "observed": f"OCF > $0 · ADTV ${adtv20/1e6:.1f}M",
                                    "required": "OCF_TTM > 0 & ADTV ≥ $10M",
                                },
                                {
                                    "name": "Weak Daily Trend (Open < SMA20)",
                                    "pass": True,
                                    "observed": f"Open ${op:.2f} vs SMA20 ${sma20:.2f} ({(op/sma20-1)*100:+.1f}%)",
                                    "required": "Open < SMA20",
                                },
                                {
                                    "name": "Bounded Non-PEAD Gap (+1.3σ..+2.8σ)",
                                    "pass": True,
                                    "observed": f"{z_gap:+.2f}σ (Gap {ret_gap*100:+.2f}%)"
                                    + (" · ⚠️ >8% Catalyst Check!" if catalyst_warn else ""),
                                    "required": "+1.30σ ≤ z_gap ≤ +2.80σ",
                                },
                            ],
                        )
                    )
                elif weak_trend and 0.90 <= z_gap < 1.30:
                    s1_near.append((z_gap, t, pq, ibs, eff_rvol))

            # ---------------- S2: Rule 10b-18 MOC Vacuum (15:55 -> Next 10:00) ----------------
            if t.get("s2") and op >= 20.0 and adtv20 >= 20_000_000:
                p1500 = self._p1500_prices.get(sym)
                if is_post_3pm and p1500 and spy_p1500 and p1500 > 0 and spy_p1500 > 0:
                    r_pre_rel = (p1500 / op - 1.0) - (spy_p1500 / spy_open - 1.0)
                    r_last_hr_rel = (last / p1500 - 1.0) - (spy_last / spy_p1500 - 1.0)
                    quiet_pre = abs(r_pre_rel) <= 0.015
                    slammed_close = r_last_hr_rel <= -0.015
                    if quiet_pre and slammed_close:
                        status = "SIGNAL_ACTIVE" if is_closing_window else "FORMING_IN_WINDOW"
                        s2_qual.append(
                            self._build_candidate(
                                sleeve_id="S2",
                                status=status,
                                sort_score=-r_last_hr_rel,
                                t=t,
                                pq=pq,
                                ref_price=last,
                                stop_price=round(last * 0.94, 2),
                                primary_label=f"15:00→Now {r_last_hr_rel*100:+.2f}% vs SPY",
                                secondary_label=f"Pre-3PM {r_pre_rel*100:+.2f}% vs SPY · 10b-18 Shutoff",
                                metric_value=round(r_last_hr_rel * 100.0, 2),
                                ibs=ibs,
                                eff_rvol=eff_rvol,
                                book_cap=book_cap,
                                spy_last=spy_last,
                                live_mtm_bps=None,
                                gates=[
                                    {
                                        "name": "Large/Mega-Cap Buyback Issuer",
                                        "pass": True,
                                        "observed": f"{guild} (${mcap/1e9:.1f}B) · OCF > $0",
                                        "required": "MCap ≥ $10B & OCF_TTM > 0",
                                    },
                                    {
                                        "name": "Quiet Tape Before 15:00 ET",
                                        "pass": True,
                                        "observed": f"09:30→15:00 vs SPY: {r_pre_rel*100:+.2f}%",
                                        "required": "|r_0930_1500 − SPY| ≤ 1.50%",
                                    },
                                    {
                                        "name": "Rule 10b-18 Shutoff MOC Vacuum",
                                        "pass": True,
                                        "observed": f"15:00→Now vs SPY: {r_last_hr_rel*100:+.2f}%",
                                        "required": "r_1500_1555 − SPY ≤ -1.50%",
                                    },
                                ],
                            )
                        )
                    elif quiet_pre and r_last_hr_rel <= -0.006:
                        s2_near.append((-r_last_hr_rel, t, pq, ibs, eff_rvol, r_pre_rel, r_last_hr_rel))
                else:
                    # Pre-15:00 ET monitoring: track Large/Mega-Caps lagging SPY intraday ahead of 3 PM lock
                    r_intraday_rel = (last / op - 1.0) - spy_intraday_ret
                    if -0.025 <= r_intraday_rel <= -0.008 and ibs <= 0.25:
                        s2_near.append((-r_intraday_rel, t, pq, ibs, eff_rvol, r_intraday_rel, r_intraday_rel))

            # ---------------- S3: 2x/3x Lev-ETP 15:55 Washout (15:55 -> Next 09:30) ----------------
            if t.get("s3") and adtv20 >= 15_000_000:
                if z1d_c <= -1.50 and ibs <= 0.15:
                    status = "SIGNAL_ACTIVE" if is_closing_window else "FORMING_IN_WINDOW"
                    s3_qual.append(
                        self._build_candidate(
                            sleeve_id="S3",
                            status=status,
                            sort_score=-z1d_c,
                            t=t,
                            pq=pq,
                            ref_price=last,
                            stop_price=round(last * 0.92, 2),
                            primary_label=f"z1d {z1d_c:+.2f}σ ({ret_cc*100:+.2f}%)",
                            secondary_label=f"IBS {ibs:.2f} (Bottom {ibs*100:.0f}% of Range)",
                            metric_value=round(z1d_c, 3),
                            ibs=ibs,
                            eff_rvol=eff_rvol,
                            book_cap=book_cap,
                            spy_last=spy_last,
                            live_mtm_bps=None,
                            gates=[
                                {
                                    "name": "Liquid 2x/3x Leveraged ETP",
                                    "pass": True,
                                    "observed": f"LEV_ETF · ADTV ${adtv20/1e6:.1f}M",
                                    "required": "Guild = LEV_ETF & ADTV ≥ $15M",
                                },
                                {
                                    "name": "Constant-Leverage Down-Shock",
                                    "pass": True,
                                    "observed": f"z1d_c = {z1d_c:+.2f}σ ({ret_cc*100:+.2f}%)",
                                    "required": "z1d_c ≤ -1.50σ",
                                },
                                {
                                    "name": "Pinned at Lows Into 15:55 Rebalance",
                                    "pass": True,
                                    "observed": f"IBS = {ibs:.3f} (Low ${lo:.2f} / High ${hi:.2f})",
                                    "required": "IBS ≤ 0.15",
                                },
                            ],
                        )
                    )
                elif z1d_c <= -1.05 and ibs <= 0.28:
                    s3_near.append((-z1d_c, t, pq, ibs, eff_rvol))

            # ---------------- S4: Biotech $5+ Bounded Stop-Run Gap (09:30 -> 16:00) ----------------
            if t.get("s4") and op >= 5.0 and adtv20 >= 2_000_000 and ind_gap_z is not None:
                quiet_prior = prev_rvol <= 1.50
                bounded_gap = -3.00 <= ind_gap_z <= -1.20
                if quiet_prior and bounded_gap:
                    stopped_out = lo <= op * 0.90
                    capped_ret = -0.10 if stopped_out else (last / op - 1.0)
                    hedged_ret = capped_ret - beta * spy_intraday_ret
                    status = (
                        "SIGNAL_ACTIVE"
                        if win_info["phase"] in ("PREMARKET", "MORNING_WINDOW")
                        else "MORNING_COHORT_LIVE"
                    )
                    s4_qual.append(
                        self._build_candidate(
                            sleeve_id="S4",
                            status=status,
                            sort_score=adtv20,
                            t=t,
                            pq=pq,
                            ref_price=op,
                            stop_price=round(op * 0.90, 2),
                            primary_label=f"ind_gap_z {ind_gap_z:+.2f}σ ({ret_gap*100:+.1f}%)",
                            secondary_label=f"Prev RVOL {prev_rvol:.2f}x · Open ${op:.2f}",
                            metric_value=round(ind_gap_z, 3),
                            ibs=ibs,
                            eff_rvol=eff_rvol,
                            book_cap=book_cap,
                            spy_last=spy_last,
                            live_mtm_bps=round(hedged_ret * 10000.0, 1),
                            stopped_out=stopped_out,
                            gates=[
                                {
                                    "name": "Institutional $5+ Cash-Burning Biotech",
                                    "pass": True,
                                    "observed": f"Open ${op:.2f} · ADTV ${adtv20/1e6:.1f}M",
                                    "required": "BIOTECH_BURNER, Open ≥ $5, ADTV ≥ $2M",
                                },
                                {
                                    "name": "Quiet Prior Session (No Active Offering)",
                                    "pass": True,
                                    "observed": f"Prior-Day RVOL = {prev_rvol:.2f}x",
                                    "required": "prev_rvol ≤ 1.50x",
                                },
                                {
                                    "name": "Bounded Industry Gap (-3.0σ..-1.2σ)",
                                    "pass": True,
                                    "observed": f"ind_gap_z = {ind_gap_z:+.2f}σ (Peer Gap {peer_gap*100:+.1f}%)",
                                    "required": "-3.00σ ≤ ind_gap_z ≤ -1.20σ",
                                },
                            ],
                        )
                    )
                elif quiet_prior and -3.60 <= ind_gap_z <= -0.20:
                    s4_near.append((-ind_gap_z, t, pq, ibs, eff_rvol, ind_gap_z))

            # ---------------- S5: S&P 500 1-of-N Narrow-Industry Cap (15:55 -> Close+3d) ----------------
            if t.get("s5") and last >= 20.0 and adtv20 >= 25_000_000 and ind_resid_z is not None:
                if ind_resid_z <= -2.50 and eff_rvol >= 2.50 and ibs <= 0.25:
                    status = "SIGNAL_ACTIVE" if is_closing_window else "FORMING_IN_WINDOW"
                    s5_qual.append(
                        self._build_candidate(
                            sleeve_id="S5",
                            status=status,
                            sort_score=-ind_resid_z,
                            t=t,
                            pq=pq,
                            ref_price=last,
                            stop_price=round(last * 0.92, 2),
                            primary_label=f"ind_resid_z {ind_resid_z:+.2f}σ (Peers {peer_cc*100:+.1f}%)",
                            secondary_label=f"Pace RVOL {eff_rvol:.2f}x · IBS {ibs:.2f}",
                            metric_value=round(ind_resid_z, 3),
                            ibs=ibs,
                            eff_rvol=eff_rvol,
                            book_cap=book_cap,
                            spy_last=spy_last,
                            live_mtm_bps=None,
                            gates=[
                                {
                                    "name": "S&P 500 Constituent ($20+ / $25M+ ADTV)",
                                    "pass": True,
                                    "observed": f"S&P 500 · {ind} ({n_ind} peers)",
                                    "required": "in_sp500 = True & ≥ 6 Industry Peers",
                                },
                                {
                                    "name": "1-of-N Isolated Industry Shock",
                                    "pass": True,
                                    "observed": f"Stock {ret_cc*100:+.2f}% vs Peers {peer_cc*100:+.2f}% ({ind_resid_z:+.2f}σ)",
                                    "required": "ind_resid_z ≤ -2.50σ",
                                },
                                {
                                    "name": "Capitulation Volume & Dead-Low Close",
                                    "pass": True,
                                    "observed": f"Pace RVOL {eff_rvol:.2f}x · IBS {ibs:.2f}",
                                    "required": "RVOL ≥ 2.50x & IBS ≤ 0.25",
                                },
                            ],
                        )
                    )
                elif ind_resid_z <= -1.55 and ibs <= 0.40:
                    s5_near.append((-ind_resid_z, t, pq, ibs, eff_rvol, ind_resid_z, peer_cc or 0.0))

        # Rank & cap primary cohort slots exactly per audited rules
        s1_qual.sort(key=lambda c: c["sort_score"], reverse=True)
        for rank_i, c in enumerate(s1_qual, 1):
            c["cohort_rank"] = rank_i
            c["in_top_cohort"] = rank_i <= SLEEVE_SPECS["S1"]["max_positions"]

        s2_qual.sort(key=lambda c: c["sort_score"], reverse=True)
        for rank_i, c in enumerate(s2_qual, 1):
            c["cohort_rank"] = rank_i
            c["in_top_cohort"] = rank_i <= SLEEVE_SPECS["S2"]["max_positions"]

        s3_qual.sort(key=lambda c: c["sort_score"], reverse=True)
        for rank_i, c in enumerate(s3_qual, 1):
            c["cohort_rank"] = rank_i
            c["in_top_cohort"] = rank_i <= SLEEVE_SPECS["S3"]["max_positions"]

        s4_qual.sort(key=lambda c: c["sort_score"], reverse=True)
        for rank_i, c in enumerate(s4_qual, 1):
            c["cohort_rank"] = rank_i
            c["in_top_cohort"] = rank_i <= SLEEVE_SPECS["S4"]["max_positions"]

        s5_qual.sort(key=lambda c: c["sort_score"], reverse=True)
        for rank_i, c in enumerate(s5_qual, 1):
            c["cohort_rank"] = rank_i
            c["in_top_cohort"] = rank_i <= SLEEVE_SPECS["S5"]["max_positions"]

        # Build top near-trigger watchlist items so every sleeve displays closest radar setups
        near_candidates: List[Dict[str, Any]] = []
        near_candidates.extend(self._format_near_s1(s1_near, book_cap, spy_last))
        near_candidates.extend(self._format_near_s2(s2_near, book_cap, spy_last, is_post_3pm))
        near_candidates.extend(self._format_near_s3(s3_near, book_cap, spy_last))
        near_candidates.extend(self._format_near_s4(s4_near, book_cap, spy_last))
        near_candidates.extend(self._format_near_s5(s5_near, book_cap, spy_last))

        all_qualifying = s1_qual + s2_qual + s3_qual + s4_qual + s5_qual

        sleeve_summaries = []
        by_sleeve_qual = {"S1": s1_qual, "S2": s2_qual, "S3": s3_qual, "S4": s4_qual, "S5": s5_qual}
        by_sleeve_near = {
            "S1": [c for c in near_candidates if c["sleeve_id"] == "S1"],
            "S2": [c for c in near_candidates if c["sleeve_id"] == "S2"],
            "S3": [c for c in near_candidates if c["sleeve_id"] == "S3"],
            "S4": [c for c in near_candidates if c["sleeve_id"] == "S4"],
            "S5": [c for c in near_candidates if c["sleeve_id"] == "S5"],
        }
        for sid in ("S1", "S2", "S3", "S4", "S5"):
            sp = SLEEVE_SPECS[sid]
            q_list = by_sleeve_qual[sid]
            top_list = [c for c in q_list if c["in_top_cohort"]]
            mtm_vals = [c["live_mtm_bps"] for c in top_list if c["live_mtm_bps"] is not None]
            sleeve_summaries.append({
                **sp,
                "sleeve_capital_usd": round(book_cap * sp["weight"], 0),
                "qualifying_count": len(q_list),
                "cohort_selected_count": len(top_list),
                "near_count": len(by_sleeve_near[sid]),
                "today_cohort_mtm_bps": round(sum(mtm_vals) / len(mtm_vals), 1) if mtm_vals else None,
            })

        return {
            "ok": True,
            "fetched_at_et": raw["fetched_at_et"],
            "feed_mode": feed_mode,
            "scan_latency_ms": raw["scan_latency_ms"],
            "as_of_prior_session": self._baseline_meta.get("as_of_prior_session", "2026-11-10"),
            "total_audited_universe": self._baseline_meta.get("total_audited_universe", 4688),
            "live_scanner_universe_count": len(self._tickers),
            "quotes_received": raw["quotes_received"],
            "book_capital_usd": book_cap,
            "market_window": win_info,
            "p1500_anchor_locked": bool(is_post_3pm and len(self._p1500_prices) > 0),
            "spy": {
                "symbol": "SPY",
                "prev_close": round(spy_prev, 2),
                "open": round(spy_open, 2),
                "high": round(spy_high, 2),
                "low": round(spy_low, 2),
                "last": round(spy_last, 2),
                "p1500": round(spy_p1500, 2) if spy_p1500 else None,
                "day_chg_pct": round(spy_day_ret * 100.0, 2),
                "intraday_chg_pct": round(spy_intraday_ret * 100.0, 2),
            },
            "sleeves": sleeve_summaries,
            "candidates": all_qualifying,
            "near_candidates": near_candidates,
            "total_active_or_forming": len(all_qualifying),
        }

    def _build_candidate(
        self,
        sleeve_id: str,
        status: str,
        sort_score: float,
        t: Dict[str, Any],
        pq: Dict[str, float],
        ref_price: float,
        stop_price: float,
        primary_label: str,
        secondary_label: str,
        metric_value: float,
        ibs: float,
        eff_rvol: float,
        book_cap: float,
        spy_last: float,
        live_mtm_bps: Optional[float],
        gates: List[Dict[str, Any]],
        catalyst_warning: bool = False,
        stopped_out: bool = False,
    ) -> Dict[str, Any]:
        sp = SLEEVE_SPECS[sleeve_id]
        sym = t["symbol"]
        adtv20 = float(t["adtv20_usd"])
        beta = float(t["beta_spy"])
        guild = str(t.get("guild", ""))
        mcap = float(t.get("market_cap", 0.0))

        sleeve_cap = book_cap * float(sp["weight"])
        if sleeve_id == "S5":
            # 1/3 daily rolling 3-day cohort
            raw_slot_usd = (sleeve_cap / 3.0) / float(sp["max_positions"])
        else:
            raw_slot_usd = sleeve_cap / float(sp["max_positions"])

        # Enforce strict 0.5% ADTV liquidity cap
        adtv_cap_usd = 0.005 * adtv20
        target_notional_usd = max(1_000.0, min(raw_slot_usd, adtv_cap_usd))
        shares = max(1, int(target_notional_usd // max(ref_price, 0.50)))
        actual_notional_usd = round(shares * ref_price, 2)

        hedge_mode = sp["hedge_mode"]
        if hedge_mode == "BUY_BETA_SPY":
            hedge_side = "BUY"
            hedge_beta_used = beta
            spy_shares = max(1, round((actual_notional_usd * beta) / max(spy_last, 1.0)))
        elif hedge_mode == "SHORT_1X_SPY":
            hedge_side = "SHORT"
            hedge_beta_used = 1.0
            spy_shares = max(1, round(actual_notional_usd / max(spy_last, 1.0)))
        elif hedge_mode == "SHORT_BETA_SPY":
            hedge_side = "SHORT"
            hedge_beta_used = beta
            spy_shares = max(1, round((actual_notional_usd * beta) / max(spy_last, 1.0)))
        else:
            hedge_side = "NONE"
            hedge_beta_used = 0.0
            spy_shares = 0

        is_short = sp["direction"] == "SHORT"
        is_htb = (
            ref_price < 12.0
            or mcap < 750_000_000
            or guild in ("BIOTECH_BURNER", "MICRO_CAP", "CEF", "PREFERRED")
        )
        rt_cost_bps = _estimate_rt_friction_bps(
            price=ref_price,
            adtv20_usd=adtv20,
            clip_usd=actual_notional_usd,
            hedged=(spy_shares > 0),
            is_short=is_short,
            is_htb=is_htb,
        )
        live_mtm_usd = (
            round(actual_notional_usd * (live_mtm_bps / 10000.0), 2)
            if live_mtm_bps is not None
            else None
        )

        # Build crisp copyable multi-leg ticket
        leg1_verb = "SELL SHORT" if is_short else "BUY"
        hedge_line = (
            f"LEG 2 (SPY HEDGE): {hedge_side} {spy_shares} sh SPY @ ~${spy_last:.2f} (β={hedge_beta_used:.2f})"
            if spy_shares > 0
            else "LEG 2 (HEDGE): NONE (Unhedged Sleeve)"
        )
        ticket_text = (
            f"[{sleeve_id} — {sp['name']}]\n"
            f"LEG 1 (PRIMARY): {leg1_verb} {shares} sh {sym} @ ~${ref_price:.2f} (${actual_notional_usd:,.0f})\n"
            f"{hedge_line}\n"
            f"HARD STOP: ${stop_price:.2f} | ENTRY: {sp['entry_bell']} | EXIT: {sp['exit_bell']}\n"
            f"SIGNAL: {primary_label} | {secondary_label} | Est. RT Friction: {rt_cost_bps:.1f} bps"
        )

        return {
            "id": f"{sleeve_id}-{sym}",
            "sleeve_id": sleeve_id,
            "sleeve_short_name": sp["short_name"],
            "direction": sp["direction"],
            "status": status,
            "sort_score": float(sort_score),
            "cohort_rank": 0,
            "in_top_cohort": True,
            "symbol": sym,
            "name": t.get("name", sym),
            "guild": guild,
            "sector": t.get("sector", ""),
            "industry": t.get("industry", ""),
            "adtv20_usd": round(adtv20, 0),
            "prev_close": round(pq["prev_close"], 2),
            "open": round(pq["open"], 2),
            "high": round(pq["high"], 2),
            "low": round(pq["low"], 2),
            "last": round(pq["last"], 2),
            "day_chg_pct": round(pq["ret_cc"] * 100.0, 2),
            "gap_pct": round(pq["ret_gap"] * 100.0, 2),
            "ibs": round(ibs, 3),
            "pace_rvol": round(eff_rvol, 2),
            "sigma20_pct": round(float(t["sigma20"]) * 100.0, 2),
            "beta_spy": round(beta, 2),
            "primary_label": primary_label,
            "secondary_label": secondary_label,
            "metric_value": metric_value,
            "ref_price": round(ref_price, 2),
            "stop_price": stop_price,
            "shares": shares,
            "notional_usd": actual_notional_usd,
            "hedge_side": hedge_side,
            "spy_hedge_shares": spy_shares,
            "rt_cost_bps": rt_cost_bps,
            "live_mtm_bps": live_mtm_bps,
            "live_mtm_usd": live_mtm_usd,
            "catalyst_warning": catalyst_warning,
            "stopped_out": stopped_out,
            "entry_bell": sp["entry_bell"],
            "exit_bell": sp["exit_bell"],
            "gates": gates,
            "copy_ticket": ticket_text,
        }

    def _format_near_s1(
        self, items: List[Tuple[Any, ...]], book_cap: float, spy_last: float
    ) -> List[Dict[str, Any]]:
        items.sort(key=lambda x: x[0], reverse=True)
        out = []
        for z_gap, t, pq, ibs, rvol in items[:5]:
            op = pq["open"]
            sma20 = float(t["sma20"])
            out.append(
                self._build_candidate(
                    sleeve_id="S1",
                    status="NEAR_TRIGGER_WATCH",
                    sort_score=z_gap,
                    t=t,
                    pq=pq,
                    ref_price=op,
                    stop_price=round(op * 1.06, 2),
                    primary_label=f"z_gap {z_gap:+.2f}σ (Needs ≥ +1.30σ)",
                    secondary_label=f"Open ${op:.2f} < SMA20 ${sma20:.2f}",
                    metric_value=round(z_gap, 3),
                    ibs=ibs,
                    eff_rvol=rvol,
                    book_cap=book_cap,
                    spy_last=spy_last,
                    live_mtm_bps=None,
                    gates=[
                        {
                            "name": "Weak Daily Trend (Open < SMA20)",
                            "pass": True,
                            "observed": f"Open ${op:.2f} < SMA20 ${sma20:.2f}",
                            "required": "Open < SMA20",
                        },
                        {
                            "name": "Bounded Gap (+1.3σ..+2.8σ)",
                            "pass": False,
                            "observed": f"z_gap = {z_gap:+.2f}σ",
                            "required": "+1.30σ ≤ z_gap ≤ +2.80σ",
                        },
                    ],
                )
            )
        return out

    def _format_near_s2(
        self,
        items: List[Tuple[Any, ...]],
        book_cap: float,
        spy_last: float,
        is_post_3pm: bool,
    ) -> List[Dict[str, Any]]:
        items.sort(key=lambda x: x[0], reverse=True)
        out = []
        for score, t, pq, ibs, rvol, r_pre, r_last in items[:5]:
            lbl1 = (
                f"15:00→Now {r_last*100:+.2f}% vs SPY"
                if is_post_3pm
                else f"Intraday {r_last*100:+.2f}% vs SPY (3PM Watch)"
            )
            lbl2 = (
                f"Pre-3PM {r_pre*100:+.2f}% · Needs ≤ -1.50%"
                if is_post_3pm
                else f"Awaiting 15:00 ET Anchor Lock · IBS {ibs:.2f}"
            )
            out.append(
                self._build_candidate(
                    sleeve_id="S2",
                    status="PRE_3PM_WATCH" if not is_post_3pm else "NEAR_TRIGGER_WATCH",
                    sort_score=score,
                    t=t,
                    pq=pq,
                    ref_price=pq["last"],
                    stop_price=round(pq["last"] * 0.94, 2),
                    primary_label=lbl1,
                    secondary_label=lbl2,
                    metric_value=round(r_last * 100.0, 2),
                    ibs=ibs,
                    eff_rvol=rvol,
                    book_cap=book_cap,
                    spy_last=spy_last,
                    live_mtm_bps=None,
                    gates=[
                        {
                            "name": "Large/Mega-Cap Buyback Issuer",
                            "pass": True,
                            "observed": f"{t.get('guild')} · OCF > $0",
                            "required": "MCap ≥ $10B & OCF_TTM > 0",
                        },
                        {
                            "name": "15:00→15:55 Final-Hour Drop ≤ -1.50%",
                            "pass": False,
                            "observed": lbl1,
                            "required": "Post-15:00 Rel Drop ≤ -1.50%",
                        },
                    ],
                )
            )
        return out

    def _format_near_s3(
        self, items: List[Tuple[Any, ...]], book_cap: float, spy_last: float
    ) -> List[Dict[str, Any]]:
        items.sort(key=lambda x: x[0], reverse=True)
        out = []
        for score, t, pq, ibs, rvol in items[:5]:
            z1d = -score
            out.append(
                self._build_candidate(
                    sleeve_id="S3",
                    status="NEAR_TRIGGER_WATCH",
                    sort_score=score,
                    t=t,
                    pq=pq,
                    ref_price=pq["last"],
                    stop_price=round(pq["last"] * 0.92, 2),
                    primary_label=f"z1d {z1d:+.2f}σ (Needs ≤ -1.50σ)",
                    secondary_label=f"IBS {ibs:.2f} (Needs ≤ 0.15)",
                    metric_value=round(z1d, 3),
                    ibs=ibs,
                    eff_rvol=rvol,
                    book_cap=book_cap,
                    spy_last=spy_last,
                    live_mtm_bps=None,
                    gates=[
                        {
                            "name": "Constant-Leverage Shock ≤ -1.50σ",
                            "pass": z1d <= -1.50,
                            "observed": f"{z1d:+.2f}σ",
                            "required": "z1d_c ≤ -1.50σ",
                        },
                        {
                            "name": "Pinned Near Session Low (IBS ≤ 0.15)",
                            "pass": ibs <= 0.15,
                            "observed": f"IBS = {ibs:.2f}",
                            "required": "IBS ≤ 0.15",
                        },
                    ],
                )
            )
        return out

    def _format_near_s4(
        self, items: List[Tuple[Any, ...]], book_cap: float, spy_last: float
    ) -> List[Dict[str, Any]]:
        items.sort(key=lambda x: x[0], reverse=True)
        out = []
        for score, t, pq, ibs, rvol, ind_gap_z in items[:5]:
            op = pq["open"]
            out.append(
                self._build_candidate(
                    sleeve_id="S4",
                    status="NEAR_TRIGGER_WATCH",
                    sort_score=score,
                    t=t,
                    pq=pq,
                    ref_price=op,
                    stop_price=round(op * 0.90, 2),
                    primary_label=f"ind_gap_z {ind_gap_z:+.2f}σ (Band -3.0..-1.2σ)",
                    secondary_label=f"Prev RVOL {float(t['prev_rvol']):.2f}x · Open ${op:.2f}",
                    metric_value=round(ind_gap_z, 3),
                    ibs=ibs,
                    eff_rvol=rvol,
                    book_cap=book_cap,
                    spy_last=spy_last,
                    live_mtm_bps=None,
                    gates=[
                        {
                            "name": "Quiet Prior RVOL ≤ 1.50x",
                            "pass": True,
                            "observed": f"{float(t['prev_rvol']):.2f}x",
                            "required": "prev_rvol ≤ 1.50x",
                        },
                        {
                            "name": "Bounded Biotech Stop-Run Gap",
                            "pass": -3.0 <= ind_gap_z <= -1.2,
                            "observed": f"ind_gap_z = {ind_gap_z:+.2f}σ",
                            "required": "-3.00σ ≤ ind_gap_z ≤ -1.20σ",
                        },
                    ],
                )
            )
        return out

    def _format_near_s5(
        self, items: List[Tuple[Any, ...]], book_cap: float, spy_last: float
    ) -> List[Dict[str, Any]]:
        items.sort(key=lambda x: x[0], reverse=True)
        out = []
        for score, t, pq, ibs, rvol, ind_z, peer_cc in items[:5]:
            out.append(
                self._build_candidate(
                    sleeve_id="S5",
                    status="NEAR_TRIGGER_WATCH",
                    sort_score=score,
                    t=t,
                    pq=pq,
                    ref_price=pq["last"],
                    stop_price=round(pq["last"] * 0.92, 2),
                    primary_label=f"ind_resid_z {ind_z:+.2f}σ (Peers {peer_cc*100:+.1f}%)",
                    secondary_label=f"Pace RVOL {rvol:.2f}x (≥2.5x) · IBS {ibs:.2f} (≤0.25)",
                    metric_value=round(ind_z, 3),
                    ibs=ibs,
                    eff_rvol=rvol,
                    book_cap=book_cap,
                    spy_last=spy_last,
                    live_mtm_bps=None,
                    gates=[
                        {
                            "name": "1-of-N Narrow-Industry Isolation ≤ -2.50σ",
                            "pass": ind_z <= -2.50,
                            "observed": f"{ind_z:+.2f}σ",
                            "required": "ind_resid_z ≤ -2.50σ",
                        },
                        {
                            "name": "Capitulation RVOL ≥ 2.5x & IBS ≤ 0.25",
                            "pass": (rvol >= 2.50 and ibs <= 0.25),
                            "observed": f"RVOL {rvol:.2f}x · IBS {ibs:.2f}",
                            "required": "RVOL ≥ 2.5x & IBS ≤ 0.25",
                        },
                    ],
                )
            )
        return out
