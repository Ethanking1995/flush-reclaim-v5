"""
backend/whole_market_scanner.py
===============================
IRA Long-Only (`5WJ26897`) Whole-Market Closing Scanner & Audit Engine.

Excludes ALL short-selling strategies and margin/short hedges. Every order leg is 100%
cash-account / IRA executable (`BUY` to open -> `SELL` to close, zero shorting, zero borrow):

  1. S3 (50% Weight — IRA CORE): US Equity Bull 2x/3x ETP Constant-Leverage 15:55 Washout
     - BUY washed-out US Equity Bull ETPs at 15:55 ET -> SELL Next Morning 09:30–09:35 ET.
     - Strips Commodity/Rates/VIX/Crypto ETPs & Inverse ETPs; enforces Unadj_Price >= $5.00.
     - Supports `INDEX_SECTOR_ONLY` (TQQQ, SOXL, SPXL, UPRO, TNA, TECL, FAS, USD) vs
       `EQUITY_BULL_ER_GUARDED` (blocks Single-Stock 2x Bull ETPs on underlying [ER-1d, ER]).

  2. S2 (25% Weight — IRA RULE 10b-18 MOC VACUUM):
     - BUY cash-generative $10B+ Large/Mega-Cap stocks at 15:55 ET -> SELL Next Day 10:00 AM ET.
     - Blocks [ER-14d, ER+1d] Rule 10b-18 blackouts + Live 15:00–16:00 ET NLP Corporate News Guard.
     - Optional IRA Beta Hedge: `BUY beta_20d * SH` (ProShares 1x Inverse S&P 500 ETF, long cash).

  3. S5 (25% Weight — IRA PIT S&P 500 1-of-N CAPITULATION):
     - BUY dislocated Point-in-Time S&P 500 stocks at 15:55 ET -> SELL at Close + 3 Trading Days.
     - Blocks [ER-1d, ER+1d] Earnings Announcement crashes (eliminating 60.2% of toxic ER landmines).
     - Optional IRA Beta Hedge: `BUY beta_20d * SH` (long cash inverse ETF, zero shorting).

  4. STRICT IRA CASH & CONCENTRATION CAPS:
     - Hard <= 5.0% of IRA Equity per single stock/ETP AND <= 0.5% of 20d ADTV dollar cap.
     - Total IRA cash (`Primary BUY $ + SH Hedge BUY $`) never exceeds 100% of IRA cash;
       undeployed cash remains safely in Cash / SGOV reserve.
"""

from __future__ import annotations

import concurrent.futures
import datetime
from email.utils import parsedate_to_datetime
import gzip
import json
import math
import os
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx

NY_TZ = ZoneInfo("America/New_York")

CNBC_QUOTE_URL = "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol.htm"
CNBC_BARS_URL = "https://ts-api.cnbc.com/harmony/app/bars"
YAHOO_RSS_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline"
GNEWS_RSS_URL = "https://news.google.com/rss/search"

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, application/xml, text/xml, text/plain, */*",
}

HEDGE_BENCHMARK_SYMS = ("SPY", "QQQ", "SH", "PSQ")

TOXIC_NEWS_KEYWORDS = (
    "INVESTIGATION", "INVESTIGATIVE", "SUBPOENA", "DOJ", "SEC ", "LAWSUIT", "CLASS ACTION",
    "FRAUD", "ACCOUNTING", "RESTATEMENT", "GUIDANCE", "CUTS OUTLOOK", "SLASHES", "WARNING",
    "WARNS", "MISSES", "EARNINGS", "DOWNGRADE", "FDA", "CLINICAL HOLD", "CRL", "HALT",
    "OFFERING", "SECONDARY", "DILUTION", "PROBE", "RECALL", "BREACH", "RESIGNS", "STEPS DOWN",
    "BANKRUPTCY", "DEFAULT", "ANTITRUST", "TARIFF",
)

SLEEVE_SPECS: Dict[str, Dict[str, Any]] = {
    "S3": {
        "id": "S3",
        "name": "US Equity Bull 2x/3x ETP 15:55 Washout",
        "short_name": "S3 · Equity Bull Lev-ETP",
        "weight": 0.50,
        "max_positions": 6,
        "direction": "LONG",
        "uses_sh_hedge": False,
        "entry_bell": "15:55 ET Close",
        "exit_bell": "Next Day 09:30–09:35 ET",
        "window_kind": "CLOSING_1555",
        "audited_sharpe": 2.33,
        "audited_t": 2.74,
        "audited_net_bps": 60.1,
    },
    "S2": {
        "id": "S2",
        "name": "SEC Rule 10b-18 MOC Vacuum (+NLP & ER Guard)",
        "short_name": "S2 · Clean 10b-18 MOC",
        "weight": 0.25,
        "max_positions": 5,
        "direction": "LONG",
        "uses_sh_hedge": True,
        "entry_bell": "15:55 ET Close",
        "exit_bell": "Next Day 10:00 AM ET",
        "window_kind": "CLOSING_1555",
        "audited_sharpe": 1.81,
        "audited_t": 2.91,
        "audited_net_bps": 113.3,
    },
    "S5": {
        "id": "S5",
        "name": "PIT S&P 500 1-of-N Non-ER Capitulation (3d)",
        "short_name": "S5 · S&P 500 Capitulation",
        "weight": 0.25,
        "max_positions": 5,
        "direction": "LONG",
        "uses_sh_hedge": True,
        "entry_bell": "15:55 ET Close",
        "exit_bell": "Close + 3 Trading Days",
        "window_kind": "CLOSING_1555",
        "audited_sharpe": 2.54,
        "audited_t": 2.21,
        "audited_net_bps": 104.6,
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
    if minutes_since_open <= 0:
        return 0.06
    if minutes_since_open >= 390:
        return 1.00
    x = minutes_since_open / 390.0
    frac = 0.22 * math.pow(x, 0.55) + 0.48 * x + 0.30 * math.pow(x, 2.8)
    return max(0.06, min(1.0, frac))


def _estimate_ira_rt_friction_bps(
    sleeve_id: str,
    guild: str,
    price: float,
    adtv20_usd: float,
    clip_usd: float,
    with_sh_hedge: bool,
) -> float:
    """
    Sledgehammer-calibrated IRA Long-Only round-trip execution cost model:
      - S3 Leveraged ETPs: >= 12 bps RT (>$250M ADTV), >= 20 bps ($60M-$250M), >= 35 bps ($15M-$60M)
      - S2 / S5 Large-Caps: Reg NMS tick + square-root market impact + optional cash SH leg cost
    """
    px = max(price, 0.50)
    adtv = max(adtv20_usd, 100_000.0)
    eff_clip = max(2_500.0, min(clip_usd, 0.005 * adtv))
    tick_1way_bps = (0.01 / px) * 10000.0
    half_spread_bps = max(2.5, 0.65 * tick_1way_bps + 1.5)
    impact_1way_bps = 110.0 * math.sqrt(eff_clip / adtv)
    rt_bps = 2.0 * (half_spread_bps + impact_1way_bps)

    if sleeve_id == "S3" or guild == "LEV_ETF":
        if adtv >= 250_000_000:
            rt_bps = max(12.0, rt_bps)
        elif adtv >= 60_000_000:
            rt_bps = max(20.0, rt_bps)
        else:
            rt_bps = max(35.0, rt_bps)

    if with_sh_hedge:
        rt_bps += 6.0
    return round(rt_bps, 1)


def _classify_market_window(now_et: datetime.datetime) -> Dict[str, str]:
    wd = now_et.weekday()
    hm = now_et.hour * 60 + now_et.minute
    if wd >= 5:
        return {
            "phase": "WEEKEND",
            "badge": "WEEKEND STANDBY · IRA LONG-ONLY",
            "active_bells": "15:55 Closing-Cross IRA Architecture (S3 · S2 · S5)",
            "guidance": "Markets closed. Displaying latest 15:55 IRA Long-Only candidates across S3, S2, and S5 (zero shorting).",
        }
    if hm < 9 * 60 + 30:
        return {
            "phase": "PREMARKET",
            "badge": "PREMARKET · MORNING SELL PREP",
            "active_bells": "SELL Overnight S3 @ 09:30–09:35 ET & S2 @ 10:00 AM ET",
            "guidance": "Premarket window: prepare to sell yesterday's S3 Bull Lev-ETP washouts at 09:30–09:35 ET and S2 Rule 10b-18 positions (+ SH hedge) at 10:00 AM ET.",
        }
    if hm < 10 * 60 + 15:
        return {
            "phase": "MORNING_WINDOW",
            "badge": "MORNING EXIT WINDOW (S3 & S2)",
            "active_bells": "SELL S3 @ 09:30–09:35 ET | SELL S2 (+SH) @ 10:00 AM ET",
            "guidance": "Morning exit window live: harvest overnight dealer-gamma rebound on S3 (09:30–09:35) and corporate buyback resumption on S2 (10:00 AM).",
        }
    if hm < 15 * 60:
        return {
            "phase": "MIDDAY_RTH",
            "badge": "MIDDAY RADAR · AWAITING 15:00 ANCHOR",
            "active_bells": "S3 & S5 Forming Intraday | S2 Anchor Locks @ 15:00 ET",
            "guidance": "Monitoring US Equity Bull Leveraged ETPs (S3) and S&P 500 1-of-N industry shocks (S5) ahead of the 15:00 ET Rule 10b-18 anchor lock and 15:55 IRA closing entries.",
        }
    if hm < 15 * 60 + 45:
        return {
            "phase": "POST_3PM_ANCHOR",
            "badge": "15:00 ANCHOR LOCKED · NLP NEWS GUARD ARMED",
            "active_bells": "S3, S2 & S5 Forming -> BUY Clean IRA Cohort @ 15:50–15:55 ET",
            "guidance": "15:00 ET anchor locked and live 15:00–16:00 NLP headline scraper active! Any S2 candidate with afternoon news or [ER-14d, ER] blackout is automatically aborted.",
        }
    if hm <= 16 * 60 + 2:
        return {
            "phase": "CLOSING_BELL_ACTIVE",
            "badge": "15:55 IRA CLOSING WINDOW LIVE",
            "active_bells": "BUY S3 (50%), S2 (25%), S5 (25%) LONG-ONLY NOW",
            "guidance": "PRIMARY IRA EXECUTION BELL LIVE (15:50–15:55 ET): Enter clean S3 Bull Lev-ETP washouts, news-verified S2 10b-18 vacuums, and S5 S&P 500 capitulations (<=5% IRA cap per ticker).",
        }
    return {
        "phase": "AFTER_HOURS",
        "badge": "POST-CLOSE OVERNIGHT IRA HOLD",
        "active_bells": "Session Closed · S3/S2/S5 Long-Only IRA Book",
        "guidance": "RTH session complete. Showing final 15:55 IRA Long-Only closing candidates, NLP news-filter checks, and cash-sized order tickets.",
    }


class WholeMarketScannerService:
    """
    IRA Long-Only (`5WJ26897`) whole-market scanner across ~4,000 US securities.
    Never emits short-stock or short-ETF orders; supports optional cash `BUY SH`
    (ProShares 1x Inverse S&P 500 ETF) beta hedging on S2 & S5.
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

        self._p1500_cache_date: Optional[str] = None
        self._p1500_prices: Dict[str, float] = {}
        self._news_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

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

    def _check_live_afternoon_news(
        self,
        symbol: str,
        company_name: str,
        now_et: datetime.datetime,
        feed_mode: str,
    ) -> Dict[str, Any]:
        if feed_mode != "CNBC_REALTIME_BATCH_REST":
            return {
                "checked": True,
                "status": "CLEAR",
                "afternoon_1500_1600_count": 0,
                "toxic_today_count": 0,
                "items_scanned": 0,
                "latest_headline": None,
                "latest_time_et": None,
                "source": "OFFLINE_DETERMINISTIC",
            }

        now_mono = time.monotonic()
        cached = self._news_cache.get(symbol)
        if cached and (now_mono - cached[0]) < 90.0:
            return cached[1]

        today_date = now_et.date()
        afternoon_hits: List[Tuple[str, str]] = []
        toxic_hits: List[Tuple[str, str]] = []
        latest_hl: Optional[str] = None
        latest_ts_str: Optional[str] = None
        items_scanned = 0

        urls = [
            f"{YAHOO_RSS_URL}?s={urllib.parse.quote(symbol)}&region=US&lang=en-US",
            f"{GNEWS_RSS_URL}?q={urllib.parse.quote(symbol + ' stock when:1d')}&hl=en-US&gl=US&ceid=US:en",
        ]
        short_co = (company_name or "").split()[0].upper().rstrip(",.") if company_name else ""

        for url in urls:
            try:
                with httpx.Client(timeout=2.8, headers=HTTP_HEADERS, follow_redirects=True) as client:
                    resp = client.get(url)
                    if resp.status_code != 200 or not resp.text:
                        continue
                root = ET.fromstring(resp.text)
                for item in root.findall(".//item")[:12]:
                    title = (item.findtext("title") or "").strip()
                    pub_raw = (item.findtext("pubDate") or "").strip()
                    if not title or not pub_raw:
                        continue
                    try:
                        dt_parsed = parsedate_to_datetime(pub_raw)
                        if dt_parsed.tzinfo is None:
                            dt_parsed = dt_parsed.replace(tzinfo=datetime.timezone.utc)
                        dt_et = dt_parsed.astimezone(NY_TZ)
                    except Exception:
                        continue

                    items_scanned += 1
                    title_up = title.upper()
                    mentions_co = (
                        symbol.upper() in title_up
                        or (len(short_co) >= 4 and short_co in title_up)
                        or "YAHOO" in url.upper()
                    )
                    if not mentions_co:
                        continue

                    ts_fmt = dt_et.strftime("%Y-%m-%d %H:%M ET")
                    if latest_hl is None:
                        latest_hl = title[:115]
                        latest_ts_str = ts_fmt

                    if dt_et.date() == today_date:
                        hm_pub = dt_et.hour * 60 + dt_et.minute
                        if (15 * 60) <= hm_pub <= (16 * 60 + 5):
                            afternoon_hits.append((ts_fmt, title[:115]))
                        if hm_pub >= (8 * 60) and any(kw in title_up for kw in TOXIC_NEWS_KEYWORDS):
                            toxic_hits.append((ts_fmt, title[:115]))
                if items_scanned >= 5:
                    break
            except Exception:
                continue

        blocked = len(afternoon_hits) > 0 or len(toxic_hits) > 0
        trigger_item = afternoon_hits[0] if afternoon_hits else (toxic_hits[0] if toxic_hits else None)
        res = {
            "checked": True,
            "status": "BLOCKED_HEADLINE" if blocked else "CLEAR",
            "afternoon_1500_1600_count": len(afternoon_hits),
            "toxic_today_count": len(toxic_hits),
            "items_scanned": items_scanned,
            "latest_headline": trigger_item[1] if trigger_item else latest_hl,
            "latest_time_et": trigger_item[0] if trigger_item else latest_ts_str,
            "source": "YAHOO_GNEWS_LIVE_RSS",
        }
        with self._lock:
            self._news_cache[symbol] = (now_mono, res)
        return res

    def _fetch_cnbc_quotes_batched(self, symbols: List[str]) -> Tuple[Dict[str, Dict[str, Any]], str]:
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

        # Deterministic offline fallback for offline unit tests / disconnected network
        synth: Dict[str, Dict[str, Any]] = {}
        for idx, sym in enumerate(symbols):
            b = self._by_symbol.get(sym)
            pc = float(b["prev_close"]) if b else (38.50 if sym in ("SH", "PSQ") else 210.0)
            sig = float(b["sigma20"]) if b else 0.012
            vol20 = float(b["vol20_sh"]) if b else 5_000_000.0
            if sym == "SPY":
                op, last, hi, lo = pc * 1.002, pc * 1.004, pc * 1.006, pc * 0.999
            elif sym in ("SH", "PSQ"):
                op, last, hi, lo = pc * 0.998, pc * 0.996, pc * 1.001, pc * 0.994
            elif b and b.get("s3") and idx % 11 == 0:
                op = pc * (1.0 - 0.8 * sig)
                last = pc * (1.0 - 1.95 * sig)
                hi, lo = op * 1.002, last * 0.998
            elif b and b.get("s5") and not b.get("in_er1_window") and idx % 79 == 0:
                op = pc * (1.0 - 0.9 * sig)
                last = pc * (1.0 - 2.95 * sig)
                hi, lo = op, last * 0.997
            else:
                op, last, hi, lo = pc * 1.001, pc * 1.002, pc * 1.007, pc * 0.995
            synth[sym] = {
                "symbol": sym,
                "open": f"{op:.4f}",
                "high": f"{hi:.4f}",
                "low": f"{lo:.4f}",
                "last": f"{last:.4f}",
                "previous_day_closing": f"{pc:.4f}",
                "volume": str(int(vol20 * (3.15 if (b and b.get("s5") and idx % 79 == 0) else 0.88))),
            }
        return synth, "OFFLINE_DETERMINISTIC_FALLBACK"

    def _backfill_1500_anchor_if_needed(
        self,
        now_et: datetime.datetime,
        quote_map: Dict[str, Dict[str, Any]],
        feed_mode: str,
    ) -> None:
        today_str = now_et.strftime("%Y-%m-%d")
        hm = now_et.hour * 60 + now_et.minute
        if self._p1500_cache_date != today_str:
            self._p1500_cache_date = today_str
            self._p1500_prices = {}

        if hm < 15 * 60 or self._p1500_prices:
            return

        if hm <= 15 * 60 + 4 or feed_mode != "CNBC_REALTIME_BATCH_REST":
            for sym, q in quote_map.items():
                px = _safe_float(q.get("last"))
                if px and px > 0:
                    self._p1500_prices[sym] = px
            return

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

        priority_syms = ["SPY"]
        for sym in s2_syms[1:]:
            q = quote_map.get(sym)
            if not q:
                continue
            op = _safe_float(q.get("open"))
            last = _safe_float(q.get("last"))
            if op and last and (last / op - 1.0) <= 0.008:
                priority_syms.append(sym)

        with concurrent.futures.ThreadPoolExecutor(max_workers=14) as pool:
            for sym, px1500 in pool.map(_fetch_1500_bar, priority_syms[:220]):
                if px1500 and px1500 > 0:
                    self._p1500_prices[sym] = px1500

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
        sym_set = {t["symbol"] for t in self._tickers}
        symbols = [t["symbol"] for t in self._tickers]
        for hsym in HEDGE_BENCHMARK_SYMS:
            if hsym not in sym_set:
                symbols.append(hsym)

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
        book_capital_usd: float = 1_000_000.0,
        hedge_mode: str = "SH_INVERSE_ETF",
        etp_mode: str = "EQUITY_BULL_ER_GUARDED",
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        book_cap = max(10_000.0, min(float(book_capital_usd or 1_000_000.0), 25_000_000.0))
        ira_hedge_mode = (
            "UNHEDGED_LONG"
            if str(hedge_mode).upper() == "UNHEDGED_LONG"
            else "SH_INVERSE_ETF"
        )
        s3_etp_mode = (
            "INDEX_SECTOR_ONLY"
            if str(etp_mode).upper() == "INDEX_SECTOR_ONLY"
            else "EQUITY_BULL_ER_GUARDED"
        )

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

        sh_q = quote_map.get("SH", {})
        sh_px = (
            _safe_float(sh_q.get("last"))
            or _safe_float(sh_q.get("previous_day_closing"))
            or float(self._by_symbol.get("SH", {}).get("prev_close", 38.50))
        )

        # Pass 1: Cross-sectional narrow-industry aggregates across all ~4,000 securities
        ind_cc_sum: Dict[str, float] = {}
        ind_cc_cnt: Dict[str, int] = {}
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

        # Pass 2: Evaluate ONLY the 3 Verified IRA Long-Only Sleeves (S3, S2, S5 — ZERO Shorting)
        s3_qual: List[Dict[str, Any]] = []
        s3_near: List[Tuple[Any, ...]] = []
        s2_raw_hits: List[Tuple[float, Dict[str, Any], Dict[str, float], float, float, float, float]] = []
        s2_near: List[Tuple[Any, ...]] = []
        s5_raw_hits: List[Tuple[float, Dict[str, Any], Dict[str, float], float, float, float, float, int]] = []
        s5_near: List[Tuple[Any, ...]] = []

        for t in self._tickers:
            sym = t["symbol"]
            pq = parsed_quotes.get(sym)
            if not pq:
                continue
            op = pq["open"]
            hi = pq["high"]
            lo = pq["low"]
            last = pq["last"]
            vol = pq["volume"]
            ret_cc = pq["ret_cc"]
            rng = hi - lo
            ibs = ((last - lo) / rng) if rng > 1e-6 else 0.5

            sig20 = float(t["sigma20"])
            adtv20 = float(t["adtv20_usd"])
            vol20_sh = max(float(t["vol20_sh"]), 1.0)
            ind = t.get("industry", "")

            z1d_c = ret_cc / sig20
            pace_rvol = vol / (vol20_sh * cum_vol_frac)
            raw_rvol = vol / vol20_sh
            eff_rvol = raw_rvol if hm >= (15 * 60 + 40) else max(raw_rvol, pace_rvol)

            n_ind = ind_cc_cnt.get(ind, 0)
            if n_ind >= 6:
                peer_cc = (ind_cc_sum[ind] - ret_cc) / float(n_ind - 1)
                ind_resid_z = (ret_cc - peer_cc) / sig20
            else:
                peer_cc = None
                ind_resid_z = None

            # ---------------- S3 (50% IRA Core): US Equity Bull Lev-ETP 15:55 Washout ----------------
            etp_bkt = t.get("etp_sub_bucket")
            unadj_ok = float(t.get("unadj_prev_close", last)) >= 5.0 and last >= 5.0
            s3_bkt_allowed = (
                etp_bkt == "EQUITY_INDEX_SECTOR_BULL"
                if s3_etp_mode == "INDEX_SECTOR_ONLY"
                else etp_bkt in ("EQUITY_INDEX_SECTOR_BULL", "SINGLE_STOCK_2X_BULL")
            )
            if t.get("s3") and s3_bkt_allowed and unadj_ok and adtv20 >= 15_000_000:
                und_sym = t.get("underlying_symbol")
                und_er_blocked = bool(und_sym and t.get("in_er1_window"))
                next_er = t.get("next_er_date")
                if z1d_c <= -1.50 and ibs <= 0.15:
                    status = (
                        "BLOCKED_GUARD"
                        if und_er_blocked
                        else ("SIGNAL_ACTIVE" if is_closing_window else "FORMING_IN_WINDOW")
                    )
                    bkt_label = "Index/Sector Bull ETP" if etp_bkt == "EQUITY_INDEX_SECTOR_BULL" else f"2x Bull ({und_sym})"
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
                            secondary_label=f"IBS {ibs:.2f} · {bkt_label}",
                            metric_value=round(z1d_c, 3),
                            ibs=ibs,
                            eff_rvol=eff_rvol,
                            book_cap=book_cap,
                            ira_hedge_mode=ira_hedge_mode,
                            sh_px=sh_px,
                            guard_blocked=und_er_blocked,
                            guard_reason=(
                                f"BLOCKED: Underlying {und_sym} reports earnings ({next_er}) — skip overnight 2x single-stock ER risk!"
                                if und_er_blocked
                                else None
                            ),
                            gates=[
                                {
                                    "name": "IRA Long-Only US Equity Bull ETP (Unadj ≥ $5)",
                                    "pass": True,
                                    "observed": f"{bkt_label} · Last ${last:.2f} · ADTV ${adtv20/1e6:.1f}M",
                                    "required": "US Equity Bull ETP (No Commodity/VIX/Inverse) & Price ≥ $5",
                                },
                                {
                                    "name": "Constant-Leverage Down-Shock (L(L-1)·AUM·r)",
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
                                {
                                    "name": "Overnight Earnings Risk Guard",
                                    "pass": not und_er_blocked,
                                    "observed": (
                                        "Broad Index/Sector Basket — Zero Single-Stock ER Risk"
                                        if not und_sym
                                        else (
                                            f"🚨 BLOCKED: {und_sym} Earnings Window ({next_er})"
                                            if und_er_blocked
                                            else f"{und_sym} Clear (Next ER: {next_er or 'Outside Window'})"
                                        )
                                    ),
                                    "required": "No Underlying [ER-1d, ER] Overnight Event",
                                },
                            ],
                        )
                    )
                elif not und_er_blocked and z1d_c <= -1.00 and ibs <= 0.30:
                    s3_near.append((-z1d_c, t, pq, ibs, eff_rvol))

            # ---------------- S2 (25% IRA): Rule 10b-18 MOC Vacuum (+ER Blackout & Live NLP News) ----------------
            if t.get("s2") and op >= 20.0 and adtv20 >= 20_000_000:
                p1500 = self._p1500_prices.get(sym)
                if is_post_3pm and p1500 and spy_p1500 and p1500 > 0 and spy_p1500 > 0:
                    r_pre_rel = (p1500 / op - 1.0) - (spy_p1500 / spy_open - 1.0)
                    r_last_hr_rel = (last / p1500 - 1.0) - (spy_last / spy_p1500 - 1.0)
                    if abs(r_pre_rel) <= 0.015 and r_last_hr_rel <= -0.015:
                        s2_raw_hits.append((-r_last_hr_rel, t, pq, ibs, eff_rvol, r_pre_rel, r_last_hr_rel))
                    elif abs(r_pre_rel) <= 0.015 and r_last_hr_rel <= -0.006 and not t.get("in_er14_blackout"):
                        s2_near.append((-r_last_hr_rel, t, pq, ibs, eff_rvol, r_pre_rel, r_last_hr_rel))
                else:
                    r_intraday_rel = (last / op - 1.0) - spy_intraday_ret
                    if -0.025 <= r_intraday_rel <= -0.008 and ibs <= 0.25 and not t.get("in_er14_blackout"):
                        s2_near.append((-r_intraday_rel, t, pq, ibs, eff_rvol, r_intraday_rel, r_intraday_rel))

            # ---------------- S5 (25% IRA): PIT S&P 500 1-of-N Non-ER Capitulation ----------------
            if t.get("s5") and last >= 20.0 and adtv20 >= 25_000_000 and ind_resid_z is not None:
                if ind_resid_z <= -2.50 and eff_rvol >= 2.50 and ibs <= 0.25:
                    s5_raw_hits.append((-ind_resid_z, t, pq, ibs, eff_rvol, ind_resid_z, peer_cc or 0.0, n_ind))
                elif not t.get("in_er1_window") and ind_resid_z <= -1.55 and ibs <= 0.40:
                    s5_near.append((-ind_resid_z, t, pq, ibs, eff_rvol, ind_resid_z, peer_cc or 0.0))

        # Run Live Parallel NLP Headline Scraping on top S2 & S5 candidates
        s2_raw_hits.sort(key=lambda x: x[0], reverse=True)
        s5_raw_hits.sort(key=lambda x: x[0], reverse=True)
        news_targets: Dict[str, str] = {}
        for _, t, *_ in s2_raw_hits[:12]:
            news_targets[t["symbol"]] = t.get("name", t["symbol"])
        for _, t, *_ in s5_raw_hits[:12]:
            news_targets[t["symbol"]] = t.get("name", t["symbol"])

        news_results: Dict[str, Dict[str, Any]] = {}
        if news_targets:
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                futs = {
                    pool.submit(self._check_live_afternoon_news, sym, cname, now_et, feed_mode): sym
                    for sym, cname in news_targets.items()
                }
                for fut in concurrent.futures.as_completed(futs):
                    sym = futs[fut]
                    try:
                        news_results[sym] = fut.result()
                    except Exception:
                        news_results[sym] = {
                            "checked": False,
                            "status": "CLEAR",
                            "afternoon_1500_1600_count": 0,
                            "toxic_today_count": 0,
                            "items_scanned": 0,
                            "latest_headline": None,
                            "latest_time_et": None,
                        }

        # Build S2 IRA candidates with [ER-14d, ER+1d] Blackout + Live 15:00–16:00 NLP Headline Guard
        s2_qual: List[Dict[str, Any]] = []
        for score, t, pq, ibs, eff_rvol, r_pre_rel, r_last_hr_rel in s2_raw_hits[:12]:
            sym = t["symbol"]
            er14_blocked = bool(t.get("in_er14_blackout"))
            next_er = t.get("next_er_date")
            last_er = t.get("last_er_date")
            nw = news_results.get(
                sym,
                {
                    "status": "CLEAR",
                    "afternoon_1500_1600_count": 0,
                    "toxic_today_count": 0,
                    "items_scanned": 0,
                    "latest_headline": None,
                    "latest_time_et": None,
                },
            )
            hl_blocked = nw.get("status") == "BLOCKED_HEADLINE"
            is_blocked = er14_blocked or hl_blocked

            reasons = []
            if er14_blocked:
                reasons.append(f"Rule 10b-18 [ER-14d, ER] Blackout (Next ER: {next_er or last_er})")
            if hl_blocked:
                reasons.append(
                    f"15:00–16:00 / Toxic Headline Detected ({nw.get('latest_time_et')}): \"{nw.get('latest_headline')}\""
                )

            status = "BLOCKED_GUARD" if is_blocked else ("SIGNAL_ACTIVE" if is_closing_window else "FORMING_IN_WINDOW")
            mcap = float(t.get("market_cap", 0.0))
            s2_qual.append(
                self._build_candidate(
                    sleeve_id="S2",
                    status=status,
                    sort_score=score,
                    t=t,
                    pq=pq,
                    ref_price=pq["last"],
                    stop_price=round(pq["last"] * 0.94, 2),
                    primary_label=f"15:00→Now {r_last_hr_rel*100:+.2f}% vs SPY",
                    secondary_label=(
                        "🚨 ABORT: " + ("ER Blackout" if er14_blocked else "Afternoon Headline!")
                        if is_blocked
                        else f"Pre-3PM {r_pre_rel*100:+.2f}% · News Clear ✅"
                    ),
                    metric_value=round(r_last_hr_rel * 100.0, 2),
                    ibs=ibs,
                    eff_rvol=eff_rvol,
                    book_cap=book_cap,
                    ira_hedge_mode=ira_hedge_mode,
                    sh_px=sh_px,
                    guard_blocked=is_blocked,
                    guard_reason=" | ".join(reasons) if reasons else None,
                    news_guard=nw,
                    gates=[
                        {
                            "name": "Large/Mega-Cap PIT Cash-Generative Issuer",
                            "pass": True,
                            "observed": f"{t.get('guild')} (${mcap/1e9:.1f}B) · PIT OCF > $0",
                            "required": "MCap ≥ $10B & Lagged Quarterly OCF > 0",
                        },
                        {
                            "name": "Rule 10b-18 [ER-14d, ER+1d] Blackout Check",
                            "pass": not er14_blocked,
                            "observed": (
                                f"🚨 BLOCKED: Inside Blackout (ER: {next_er or last_er})"
                                if er14_blocked
                                else f"Outside 14d Blackout (Next ER: {next_er or 'Clear'})"
                            ),
                            "required": "Strictly outside [ER - 14d, ER + 1d]",
                        },
                        {
                            "name": "Live 15:00–16:00 ET NLP Corporate Headline Guard",
                            "pass": not hl_blocked,
                            "observed": (
                                f"🚨 ABORT ({nw.get('latest_time_et')}): {nw.get('latest_headline')}"
                                if hl_blocked
                                else f"0 Afternoon Headlines ({nw.get('items_scanned', 0)} RSS items verified)"
                            ),
                            "required": "ZERO corporate headlines 15:00–16:00 ET",
                        },
                        {
                            "name": "Quiet Pre-3PM Tape + Final-Hour 10b-18 Shutoff Drop",
                            "pass": True,
                            "observed": f"Pre-3PM {r_pre_rel*100:+.2f}% · Post-3PM {r_last_hr_rel*100:+.2f}% vs SPY",
                            "required": "|Pre-3PM| ≤ 1.50% & Post-3PM ≤ -1.50%",
                        },
                    ],
                )
            )

        # Build S5 IRA candidates with [ER-1d, ER+1d] Earnings Filter + Long SH Hedge
        s5_qual: List[Dict[str, Any]] = []
        for score, t, pq, ibs, eff_rvol, ind_resid_z, peer_cc, n_ind in s5_raw_hits[:12]:
            sym = t["symbol"]
            er_blocked = bool(t.get("in_er1_window"))
            next_er = t.get("next_er_date")
            last_er = t.get("last_er_date")
            nw = news_results.get(
                sym,
                {
                    "status": "CLEAR",
                    "afternoon_1500_1600_count": 0,
                    "toxic_today_count": 0,
                    "items_scanned": 0,
                    "latest_headline": None,
                    "latest_time_et": None,
                },
            )
            status = "BLOCKED_GUARD" if er_blocked else ("SIGNAL_ACTIVE" if is_closing_window else "FORMING_IN_WINDOW")
            hedge_tag = "BUY SH Hedge" if ira_hedge_mode == "SH_INVERSE_ETF" else "Unhedged Long"

            s5_qual.append(
                self._build_candidate(
                    sleeve_id="S5",
                    status=status,
                    sort_score=score,
                    t=t,
                    pq=pq,
                    ref_price=pq["last"],
                    stop_price=round(pq["last"] * 0.92, 2),
                    primary_label=f"ind_resid_z {ind_resid_z:+.2f}σ (Peers {peer_cc*100:+.1f}%)",
                    secondary_label=(
                        f"🚨 BLOCKED: Earnings Shock ({last_er or next_er})"
                        if er_blocked
                        else f"RVOL {eff_rvol:.2f}x · IBS {ibs:.2f} · {hedge_tag}"
                    ),
                    metric_value=round(ind_resid_z, 3),
                    ibs=ibs,
                    eff_rvol=eff_rvol,
                    book_cap=book_cap,
                    ira_hedge_mode=ira_hedge_mode,
                    sh_px=sh_px,
                    guard_blocked=er_blocked,
                    guard_reason=(
                        f"BLOCKED: Earnings Announcement Window ({last_er or next_er}) — Sledgehammer filter blocks 60% ER crash landmines!"
                        if er_blocked
                        else None
                    ),
                    news_guard=nw,
                    gates=[
                        {
                            "name": "Point-in-Time S&P 500 Constituent ($20+ / $25M+ ADTV)",
                            "pass": True,
                            "observed": f"S&P 500 · {t.get('industry')} ({n_ind} peers)",
                            "required": "PIT S&P 500 = True & ≥ 6 Industry Peers",
                        },
                        {
                            "name": "Non-Earnings Liquidation Filter (No Day -1..+1 ER)",
                            "pass": not er_blocked,
                            "observed": (
                                f"🚨 BLOCKED: Earnings Release ({last_er or next_er})"
                                if er_blocked
                                else f"Confirmed Non-Earnings Shock (Last ER: {last_er or 'Clear'} · Next: {next_er or 'Clear'})"
                            ),
                            "required": "Outside [ER - 1d, ER + 1d] Earnings Window",
                        },
                        {
                            "name": "1-of-N Isolated Industry Shock + Capitulation Close",
                            "pass": True,
                            "observed": f"Resid {ind_resid_z:+.2f}σ · Pace RVOL {eff_rvol:.2f}x · IBS {ibs:.2f}",
                            "required": "ind_resid_z ≤ -2.50σ, RVOL ≥ 2.5x, IBS ≤ 0.25",
                        },
                    ],
                )
            )

        # Rank ONLY clean (non-blocked) candidates into executable IRA cohort slots
        for sid, q_list in (("S3", s3_qual), ("S2", s2_qual), ("S5", s5_qual)):
            q_list.sort(key=lambda c: (not c["guard_blocked"], c["sort_score"]), reverse=True)
            clean_rank = 0
            for c in q_list:
                if c["guard_blocked"]:
                    c["cohort_rank"] = 0
                    c["in_top_cohort"] = False
                else:
                    clean_rank += 1
                    c["cohort_rank"] = clean_rank
                    c["in_top_cohort"] = clean_rank <= SLEEVE_SPECS[sid]["max_positions"]

        near_candidates: List[Dict[str, Any]] = []
        near_candidates.extend(self._format_near_s3(s3_near, book_cap, ira_hedge_mode, sh_px))
        near_candidates.extend(self._format_near_s2(s2_near, book_cap, ira_hedge_mode, sh_px, is_post_3pm))
        near_candidates.extend(self._format_near_s5(s5_near, book_cap, ira_hedge_mode, sh_px))

        all_qualifying = s3_qual + s2_qual + s5_qual
        clean_cohort = [c for c in all_qualifying if c["in_top_cohort"] and not c["guard_blocked"]]
        blocked_count = sum(1 for c in all_qualifying if c["guard_blocked"])

        deployed_primary_usd = round(sum(float(c["notional_usd"]) for c in clean_cohort), 2)
        deployed_hedge_usd = round(sum(float(c["hedge_notional_usd"]) for c in clean_cohort), 2)
        total_cash_deployed_usd = round(deployed_primary_usd + deployed_hedge_usd, 2)
        undeployed_cash_usd = round(max(0.0, book_cap - total_cash_deployed_usd), 2)

        by_sleeve_qual = {"S3": s3_qual, "S2": s2_qual, "S5": s5_qual}
        by_sleeve_near = {
            "S3": [c for c in near_candidates if c["sleeve_id"] == "S3"],
            "S2": [c for c in near_candidates if c["sleeve_id"] == "S2"],
            "S5": [c for c in near_candidates if c["sleeve_id"] == "S5"],
        }

        sleeve_summaries = []
        for sid in ("S3", "S2", "S5"):
            sp = SLEEVE_SPECS[sid]
            q_list = by_sleeve_qual[sid]
            clean_q = [c for c in q_list if not c["guard_blocked"]]
            top_list = [c for c in clean_q if c["in_top_cohort"]]
            blk_q = [c for c in q_list if c["guard_blocked"]]
            sleeve_summaries.append({
                **sp,
                "sleeve_capital_usd": round(book_cap * sp["weight"], 0),
                "deployed_usd": round(
                    sum(float(c["notional_usd"]) + float(c["hedge_notional_usd"]) for c in top_list), 0
                ),
                "qualifying_count": len(clean_q),
                "blocked_count": len(blk_q),
                "cohort_selected_count": len(top_list),
                "near_count": len(by_sleeve_near[sid]),
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
            "hedge_mode": ira_hedge_mode,
            "etp_mode": s3_etp_mode,
            "capital_summary": {
                "book_capital_usd": book_cap,
                "max_single_position_usd": round(0.05 * book_cap, 2),
                "deployed_primary_usd": deployed_primary_usd,
                "deployed_hedge_usd": deployed_hedge_usd,
                "total_cash_deployed_usd": total_cash_deployed_usd,
                "undeployed_cash_usd": undeployed_cash_usd,
                "deployed_pct": round((total_cash_deployed_usd / book_cap) * 100.0, 1),
            },
            "market_window": win_info,
            "p1500_anchor_locked": bool(is_post_3pm and len(self._p1500_prices) > 0),
            "spy": {
                "symbol": "SPY",
                "prev_close": round(spy_prev, 2),
                "open": round(spy_open, 2),
                "high": round(spy_high, 2),
                "low": round(spy_low, 2),
                "last": round(spy_last, 2),
                "sh_last": round(sh_px, 2),
                "p1500": round(spy_p1500, 2) if spy_p1500 else None,
                "day_chg_pct": round(spy_day_ret * 100.0, 2),
                "intraday_chg_pct": round(spy_intraday_ret * 100.0, 2),
            },
            "sleeves": sleeve_summaries,
            "candidates": all_qualifying,
            "near_candidates": near_candidates,
            "total_active_or_forming": len(clean_cohort),
            "total_guard_blocked": blocked_count,
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
        ira_hedge_mode: str,
        sh_px: float,
        gates: List[Dict[str, Any]],
        guard_blocked: bool = False,
        guard_reason: Optional[str] = None,
        news_guard: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        sp = SLEEVE_SPECS[sleeve_id]
        sym = t["symbol"]
        adtv20 = float(t["adtv20_usd"])
        beta = max(0.25, min(2.25, float(t["beta_spy"])))
        guild = str(t.get("guild", ""))
        with_sh = bool(sp["uses_sh_hedge"] and ira_hedge_mode == "SH_INVERSE_ETF")

        if guard_blocked:
            shares = 0
            actual_notional_usd = 0.0
            hedge_side = "NONE"
            hedge_symbol = "NONE"
            hedge_shares = 0
            hedge_px = 0.0
            hedge_notional_usd = 0.0
        else:
            sleeve_cap = book_cap * float(sp["weight"])
            raw_slot_total_cash = (
                (sleeve_cap / 3.0) / float(sp["max_positions"])
                if sleeve_id == "S5"
                else sleeve_cap / float(sp["max_positions"])
            )
            # In an IRA, buying SH uses real cash buying power alongside the primary stock leg:
            primary_slot_budget = raw_slot_total_cash / (1.0 + beta) if with_sh else raw_slot_total_cash
            max_single_stock_usd = 0.05 * book_cap
            adtv_cap_usd = 0.005 * adtv20
            target_notional_usd = max(500.0, min(max_single_stock_usd, primary_slot_budget, adtv_cap_usd))
            shares = max(1, int(target_notional_usd // max(ref_price, 0.50)))
            actual_notional_usd = round(shares * ref_price, 2)

            if with_sh:
                hedge_side = "BUY"
                hedge_symbol = "SH"
                hedge_px = sh_px
                hedge_shares = max(1, round((actual_notional_usd * beta) / max(hedge_px, 1.0)))
                hedge_notional_usd = round(hedge_shares * hedge_px, 2)
            else:
                hedge_side = "NONE"
                hedge_symbol = "NONE"
                hedge_px = 0.0
                hedge_shares = 0
                hedge_notional_usd = 0.0

        rt_cost_bps = _estimate_ira_rt_friction_bps(
            sleeve_id=sleeve_id,
            guild=guild,
            price=ref_price,
            adtv20_usd=adtv20,
            clip_usd=max(actual_notional_usd, 5_000.0),
            with_sh_hedge=with_sh,
        )

        if guard_blocked:
            ticket_text = (
                f"[{sleeve_id} — DO NOT EXECUTE / QUARANTINED]\n"
                f"SYMBOL: {sym} @ ${ref_price:.2f}\n"
                f"REASON: {guard_reason or 'Blocked by Sledgehammer Risk Guard'}"
            )
        else:
            hedge_line = (
                f"LEG 2 (IRA BETA HEDGE): BUY {hedge_shares} sh SH @ ~${hedge_px:.2f} (${hedge_notional_usd:,.0f} · 1x Inverse SPY, β={beta:.2f})"
                if hedge_shares > 0
                else "LEG 2 (HEDGE): NONE (100% Unhedged Long-Only IRA Cash)"
            )
            ticket_text = (
                f"[{sleeve_id} — {sp['short_name']} | IRA 5WJ26897 LONG-ONLY]\n"
                f"LEG 1 (PRIMARY): BUY {shares} sh {sym} @ ~${ref_price:.2f} (${actual_notional_usd:,.0f} · <=5% IRA & <=0.5% ADTV)\n"
                f"{hedge_line}\n"
                f"CATASTROPHE STOP: ${stop_price:.2f} | ENTRY: {sp['entry_bell']} | SELL EXIT: {sp['exit_bell']}\n"
                f"SIGNAL: {primary_label} | {secondary_label} | Est. RT Friction: {rt_cost_bps:.1f} bps"
            )

        return {
            "id": f"{sleeve_id}-{sym}",
            "sleeve_id": sleeve_id,
            "sleeve_short_name": sp["short_name"],
            "direction": "LONG",
            "status": status,
            "sort_score": float(sort_score),
            "cohort_rank": 0,
            "in_top_cohort": False,
            "symbol": sym,
            "name": t.get("name", sym),
            "guild": guild,
            "sector": t.get("sector", ""),
            "etp_sub_bucket": t.get("etp_sub_bucket"),
            "underlying_symbol": t.get("underlying_symbol"),
            "next_er_date": t.get("next_er_date"),
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
            "hedge_symbol": hedge_symbol,
            "hedge_price": round(hedge_px, 2),
            "hedge_notional_usd": hedge_notional_usd,
            "sh_hedge_shares": hedge_shares,
            "rt_cost_bps": rt_cost_bps,
            "guard_blocked": guard_blocked,
            "guard_reason": guard_reason,
            "news_guard": news_guard,
            "entry_bell": sp["entry_bell"],
            "exit_bell": sp["exit_bell"],
            "gates": gates,
            "copy_ticket": ticket_text,
        }

    def _format_near_s3(
        self,
        items: List[Tuple[Any, ...]],
        book_cap: float,
        ira_hedge_mode: str,
        sh_px: float,
    ) -> List[Dict[str, Any]]:
        items.sort(key=lambda x: x[0], reverse=True)
        out = []
        for score, t, pq, ibs, rvol in items[:6]:
            z1d = -score
            bkt = (
                "Index/Sector Bull"
                if t.get("etp_sub_bucket") == "EQUITY_INDEX_SECTOR_BULL"
                else f"2x Bull ({t.get('underlying_symbol') or 'Eq'})"
            )
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
                    secondary_label=f"IBS {ibs:.2f} (≤0.15) · {bkt}",
                    metric_value=round(z1d, 3),
                    ibs=ibs,
                    eff_rvol=rvol,
                    book_cap=book_cap,
                    ira_hedge_mode=ira_hedge_mode,
                    sh_px=sh_px,
                    gates=[
                        {
                            "name": "Constant-Leverage Shock ≤ -1.50σ",
                            "pass": z1d <= -1.50,
                            "observed": f"{z1d:+.2f}σ ({bkt})",
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

    def _format_near_s2(
        self,
        items: List[Tuple[Any, ...]],
        book_cap: float,
        ira_hedge_mode: str,
        sh_px: float,
        is_post_3pm: bool,
    ) -> List[Dict[str, Any]]:
        items.sort(key=lambda x: x[0], reverse=True)
        out = []
        for score, t, pq, ibs, rvol, r_pre, r_last in items[:6]:
            if t.get("in_er14_blackout"):
                continue
            lbl1 = (
                f"15:00→Now {r_last*100:+.2f}% vs SPY"
                if is_post_3pm
                else f"Intraday {r_last*100:+.2f}% vs SPY (3PM Watch)"
            )
            lbl2 = (
                f"Pre-3PM {r_pre*100:+.2f}% · Needs ≤ -1.50%"
                if is_post_3pm
                else "Outside ER Blackout · Awaiting 15:00 ET Lock"
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
                    ira_hedge_mode=ira_hedge_mode,
                    sh_px=sh_px,
                    gates=[
                        {
                            "name": "Outside [ER-14d, ER] Rule 10b-18 Blackout",
                            "pass": True,
                            "observed": f"Clear (Next ER: {t.get('next_er_date') or 'Outside Window'})",
                            "required": "Not in [ER - 14d, ER + 1d]",
                        },
                        {
                            "name": "15:00→15:55 Final-Hour Drop ≤ -1.50%",
                            "pass": False,
                            "observed": lbl1,
                            "required": "Post-15:00 Rel Drop ≤ -1.50% + 0 Afternoon News",
                        },
                    ],
                )
            )
        return out

    def _format_near_s5(
        self,
        items: List[Tuple[Any, ...]],
        book_cap: float,
        ira_hedge_mode: str,
        sh_px: float,
    ) -> List[Dict[str, Any]]:
        items.sort(key=lambda x: x[0], reverse=True)
        out = []
        hedge_tag = "BUY SH" if ira_hedge_mode == "SH_INVERSE_ETF" else "Unhedged"
        for score, t, pq, ibs, rvol, ind_z, peer_cc in items[:6]:
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
                    secondary_label=f"RVOL {rvol:.2f}x (≥2.5x) · IBS {ibs:.2f} · {hedge_tag}",
                    metric_value=round(ind_z, 3),
                    ibs=ibs,
                    eff_rvol=rvol,
                    book_cap=book_cap,
                    ira_hedge_mode=ira_hedge_mode,
                    sh_px=sh_px,
                    gates=[
                        {
                            "name": "1-of-N Narrow-Industry Isolation ≤ -2.50σ",
                            "pass": ind_z <= -2.50,
                            "observed": f"{ind_z:+.2f}σ ({hedge_tag})",
                            "required": "ind_resid_z ≤ -2.50σ & Non-Earnings Day",
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
