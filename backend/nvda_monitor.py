"""
backend/nvda_monitor.py
=======================
Real-time telemetry and causal 5-minute signal monitor service for the
NVDA Oversold + CRWV/SOXL Tailwind Playbook, integrated natively into the
Flush & Reclaim V5 FastAPI backend.

Monitors:
  - Premarket Guard (< 09:30 ET): Blocks chasing premarket below PM_VWAP.
  - Peer Tailwind & Safety Gates:
      * Gate T1: SOXL > $62.50 (+11.8%+ vs Nov 10 close $55.88)
      * Gate T2: CRWV > $53.50 (holding above premarket low / +9.7%+ vs $48.77 close)
      * Gate T3: NVDA > $168.40 (Hard Stop / Nov 10 $168.90 gap-integrity guard)
      * Gate T4: NYSE RTH Open (>= 09:30:00 ET, confirmed on completed 5m RTH candles)
  - Setup A (Primary, 09:30-10:45 ET / 11:30 ET ext):
      * Probe into $170.90-$172.20 confluence flush zone while holding > $168.40
      * Completed 5m RTH green higher-low reclaim candle (Close > Open and Low > Flush_Low)
  - Setup B (Strength Reclaim, >= 09:45 ET if NVDA refuses to flush):
      * Two consecutive completed 5m RTH bars close above max($175.50, PM_VWAP, RTH_VWAP)
"""

from __future__ import annotations

import concurrent.futures
import datetime
import math
import threading
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx

NY_TZ = ZoneInfo("America/New_York")

CNBC_BARS_URL = "https://ts-api.cnbc.com/harmony/app/bars"
CNBC_QUOTE_URL = "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol.htm"
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

SYMBOLS = ["NVDA", "CRWV", "SOXL", "SMH", "MU", "AMD", "AVGO", "QQQ", "SPY"]

# Strategy Constants
HARD_STOP_NVDA = 168.40
SETUP_A_ZONE_LOW = 170.90
SETUP_A_ZONE_HIGH = 172.20
SOXL_GATE_MIN = 62.50
SOXL_GATE_WARN = 60.00
CRWV_GATE_MIN = 53.50
CRWV_GATE_WARN = 51.50
SETUP_B_MIN_HURDLE = 175.50
SETUP_B_FALLBACK_STOP = 173.40
SETUP_B_TARGET_2 = 180.00


def _safe_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return None if math.isnan(val) else float(val)
    s = str(val).strip().replace(",", "").replace("%", "").replace("+", "")
    if not s or s.upper() in ("UNCH", "N/A", "NULL", "NONE", "--"):
        return 0.0 if s.upper() == "UNCH" else None
    try:
        return float(s)
    except ValueError:
        return None


def _compute_vwap(bars: List[Dict[str, Any]]) -> Optional[float]:
    if not bars:
        return None
    pv = 0.0
    vv = 0.0
    for b in bars:
        vol = max(float(b["v"]), 0.0)
        typ = (float(b["h"]) + float(b["l"]) + float(b["c"])) / 3.0
        pv += typ * vol
        vv += vol
    if vv <= 0:
        return round(sum((b["h"] + b["l"] + b["c"]) / 3.0 for b in bars) / len(bars), 4)
    return round(pv / vv, 4)


def _lookup_bar_close_at_or_before(
    bars: List[Dict[str, Any]], target_epoch_ms: int
) -> Optional[float]:
    val: Optional[float] = None
    for b in bars:
        if b["epoch_ms"] <= target_epoch_ms:
            val = float(b["c"])
        else:
            break
    return val


def _fetch_symbol_5m(
    client: httpx.Client, symbol: str, start_str: str, end_str: str
) -> List[Dict[str, Any]]:
    url = f"{CNBC_BARS_URL}/{symbol}/5M/{start_str}/{end_str}/adjusted/EST5EDT.json"
    resp = client.get(url)
    if resp.status_code != 200:
        return []
    raw_bars = resp.json().get("barData", {}).get("priceBars", []) or []
    parsed: List[Dict[str, Any]] = []
    for b in raw_bars:
        tt = str(b.get("tradeTime", ""))
        if len(tt) < 14:
            continue
        o = _safe_float(b.get("open"))
        h = _safe_float(b.get("high"))
        l = _safe_float(b.get("low"))
        c = _safe_float(b.get("close"))
        v = _safe_float(b.get("volume")) or 0.0
        if o is None or h is None or l is None or c is None:
            continue
        dt_et = datetime.datetime.strptime(tt[:14], "%Y%m%d%H%M%S").replace(tzinfo=NY_TZ)
        parsed.append(
            {
                "dt": dt_et,
                "date": dt_et.date(),
                "hm": dt_et.hour * 100 + dt_et.minute,
                "ts_label": dt_et.strftime("%H:%M"),
                "epoch_ms": int(dt_et.timestamp() * 1000),
                "o": round(o, 4),
                "h": round(h, 4),
                "l": round(l, 4),
                "c": round(c, 4),
                "v": float(v),
            }
        )
    parsed.sort(key=lambda x: x["epoch_ms"])
    return parsed


def _fetch_live_quotes(
    client: httpx.Client, symbols: List[str]
) -> Dict[str, Dict[str, Any]]:
    params = {
        "symbols": "|".join(symbols),
        "requestMethod": "itv",
        "noform": "1",
        "partnerId": "2",
        "fund": "1",
        "exthrs": "1",
        "output": "json",
        "events": "1",
    }
    out: Dict[str, Dict[str, Any]] = {}
    try:
        resp = client.get(CNBC_QUOTE_URL, params=params)
        if resp.status_code != 200 or not resp.text.strip():
            return out
        qlist = resp.json().get("FormattedQuoteResult", {}).get("FormattedQuote", [])
        if isinstance(qlist, dict):
            qlist = [qlist]
        for q in qlist:
            sym = str(q.get("symbol", "")).upper().strip()
            if not sym:
                continue
            rth_last = _safe_float(q.get("last"))
            prev_close = _safe_float(q.get("previous_day_closing"))
            ext = q.get("ExtendedMktQuote") or {}
            ext_last = _safe_float(ext.get("last")) if isinstance(ext, dict) else None
            cur_mkt = str(q.get("curmktstatus", "")).upper()
            active_last = (
                rth_last
                if cur_mkt == "REG_MKT" and rth_last is not None
                else (ext_last or rth_last)
            )
            out[sym] = {
                "active_last": active_last,
                "rth_last": rth_last,
                "ext_last": ext_last,
                "prev_close": prev_close,
                "mkt_status": cur_mkt,
            }
    except Exception:
        return out
    return out


class NvdaSignalMonitorService:
    """Thread-safe background & on-demand poller for the NVDA Signal Monitor view."""

    def __init__(self, poll_interval_sec: float = 5.0) -> None:
        self.poll_interval_sec = poll_interval_sec
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._event_log: List[Dict[str, Any]] = []
        self._seen_event_keys: set[str] = set()
        self._latest_state: Dict[str, Any] = {
            "ok": False,
            "status": "INITIALIZING",
            "message": "Booting NVDA live market telemetry...",
        }

    def _log_event(
        self, key: str, ts_str: str, level: str, title: str, detail: str
    ) -> None:
        if key in self._seen_event_keys:
            return
        self._seen_event_keys.add(key)
        self._event_log.insert(
            0,
            {
                "key": key,
                "ts": ts_str,
                "level": level,
                "title": title,
                "detail": detail,
            },
        )
        del self._event_log[60:]

    def _poll_once(self) -> Dict[str, Any]:
        now_et = datetime.datetime.now(NY_TZ)
        today_date = now_et.date()
        start_dt = today_date - datetime.timedelta(days=5)
        end_dt = today_date + datetime.timedelta(days=1)
        start_str = start_dt.strftime("%Y%m%d") + "040000"
        end_str = end_dt.strftime("%Y%m%d") + "200000"

        bars_by_sym: Dict[str, List[Dict[str, Any]]] = {}
        with httpx.Client(timeout=6.5, headers=HTTP_HEADERS) as client:
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
                fut_bars = {
                    pool.submit(_fetch_symbol_5m, client, sym, start_str, end_str): sym
                    for sym in SYMBOLS
                }
                fut_quotes = pool.submit(_fetch_live_quotes, client, SYMBOLS)
                for fut, sym in fut_bars.items():
                    try:
                        bars_by_sym[sym] = fut.result()
                    except Exception:
                        bars_by_sym[sym] = []
                quotes_by_sym = fut_quotes.result()

        nvda_all = bars_by_sym.get("NVDA", [])
        if not nvda_all:
            raise RuntimeError("CNBC 5M feed returned empty bars for NVDA.")

        available_dates = sorted({b["date"] for b in nvda_all})
        session_date = (
            today_date if today_date in available_dates else available_dates[-1]
        )
        prior_rth_dates = [
            d
            for d in available_dates
            if d < session_date
            and any(930 <= b["hm"] <= 1555 for b in nvda_all if b["date"] == d)
        ]
        prev_session_date = prior_rth_dates[-1] if prior_rth_dates else None

        peers: Dict[str, Dict[str, Any]] = {}
        for sym in SYMBOLS:
            sbars = bars_by_sym.get(sym, [])
            qinfo = quotes_by_sym.get(sym, {})
            prev_rth = [
                b
                for b in sbars
                if prev_session_date is not None
                and b["date"] == prev_session_date
                and 930 <= b["hm"] <= 1555
            ]
            prev_close = (
                float(prev_rth[-1]["c"])
                if prev_rth
                else (qinfo.get("prev_close") or 0.0)
            )

            pm_bars = [
                b for b in sbars if b["date"] == session_date and 400 <= b["hm"] < 930
            ]
            rth_bars = [
                b for b in sbars if b["date"] == session_date and 930 <= b["hm"] <= 1555
            ]
            session_bars = pm_bars + rth_bars

            bar_last = float(session_bars[-1]["c"]) if session_bars else prev_close
            q_last = qinfo.get("active_last")
            if (
                q_last is not None
                and bar_last > 0
                and abs(q_last / bar_last - 1.0) < 0.05
            ):
                live_px = float(q_last)
            else:
                live_px = bar_last

            pm_vwap = _compute_vwap(pm_bars)
            rth_vwap = _compute_vwap(rth_bars)
            pm_hi = max((float(b["h"]) for b in pm_bars), default=None)
            pm_lo = min((float(b["l"]) for b in pm_bars), default=None)
            rth_hi = max((float(b["h"]) for b in rth_bars), default=None)
            rth_lo = min((float(b["l"]) for b in rth_bars), default=None)
            if (
                rth_bars
                and now_et.date() == session_date
                and 930 <= (now_et.hour * 100 + now_et.minute) < 1600
            ):
                rth_hi = max(rth_hi, live_px) if rth_hi is not None else live_px
                rth_lo = min(rth_lo, live_px) if rth_lo is not None else live_px
            elif (
                pm_bars
                and now_et.date() == session_date
                and (now_et.hour * 100 + now_et.minute) < 930
            ):
                pm_hi = max(pm_hi, live_px) if pm_hi is not None else live_px
                pm_lo = min(pm_lo, live_px) if pm_lo is not None else live_px

            pm_vol = sum(float(b["v"]) for b in pm_bars)
            rth_vol = sum(float(b["v"]) for b in rth_bars)
            chg_pct = (
                ((live_px / prev_close) - 1.0) * 100.0 if prev_close > 0 else 0.0
            )
            vs_pm_vwap_pct = (
                ((live_px / pm_vwap) - 1.0) * 100.0 if pm_vwap else None
            )

            peers[sym] = {
                "symbol": sym,
                "prev_close": round(prev_close, 2),
                "last": round(live_px, 2),
                "chg_pct": round(chg_pct, 2),
                "pm_high": round(pm_hi, 2) if pm_hi is not None else None,
                "pm_low": round(pm_lo, 2) if pm_lo is not None else None,
                "pm_vwap": round(pm_vwap, 2) if pm_vwap is not None else None,
                "vs_pm_vwap_pct": (
                    round(vs_pm_vwap_pct, 2) if vs_pm_vwap_pct is not None else None
                ),
                "rth_high": round(rth_hi, 2) if rth_hi is not None else None,
                "rth_low": round(rth_lo, 2) if rth_lo is not None else None,
                "rth_vwap": round(rth_vwap, 2) if rth_vwap is not None else None,
                "pm_vol_m": round(pm_vol / 1e6, 2),
                "rth_vol_m": round(rth_vol / 1e6, 2),
                "last_bar_ts": (
                    session_bars[-1]["ts_label"] if session_bars else "--:--"
                ),
            }

        nvda_peer = peers["NVDA"]
        soxl_peer = peers["SOXL"]
        crwv_peer = peers["CRWV"]

        nvda_pm_bars = [
            b for b in nvda_all if b["date"] == session_date and 400 <= b["hm"] < 930
        ]
        nvda_rth_raw = [
            b for b in nvda_all if b["date"] == session_date and 930 <= b["hm"] <= 1555
        ]
        soxl_session_bars = [
            b for b in bars_by_sym.get("SOXL", []) if b["date"] == session_date
        ]
        crwv_session_bars = [
            b for b in bars_by_sym.get("CRWV", []) if b["date"] == session_date
        ]

        now_epoch_ms = int(now_et.timestamp() * 1000)
        five_min_ms = 5 * 60 * 1000

        nvda_live_px = nvda_peer["last"]
        nvda_prev_close = nvda_peer["prev_close"] or 168.90
        pm_high = nvda_peer["pm_high"] or 178.36
        pm_low = nvda_peer["pm_low"] or 171.25
        pm_vwap = nvda_peer["pm_vwap"] or 175.47
        pm_last_anchor = (
            float(nvda_pm_bars[-1]["c"]) if nvda_pm_bars else nvda_live_px
        )

        completed_rth: List[Dict[str, Any]] = []
        forming_rth: Optional[Dict[str, Any]] = None
        for b in nvda_rth_raw:
            b_copy = dict(b)
            if b_copy["epoch_ms"] + five_min_ms <= now_epoch_ms:
                completed_rth.append(b_copy)
            elif b_copy["epoch_ms"] <= now_epoch_ms < b_copy["epoch_ms"] + five_min_ms:
                b_copy["c"] = nvda_live_px
                b_copy["h"] = max(float(b_copy["h"]), nvda_live_px)
                b_copy["l"] = min(float(b_copy["l"]), nvda_live_px)
                forming_rth = b_copy

        is_premarket = (
            session_date == now_et.date()
            and (now_et.hour * 100 + now_et.minute) < 930
        )
        is_rth_open = (
            session_date == now_et.date()
            and 930 <= (now_et.hour * 100 + now_et.minute) < 1600
        )
        is_after_hours = (
            session_date == now_et.date()
            and (now_et.hour * 100 + now_et.minute) >= 1600
        )

        rth_open = float(nvda_rth_raw[0]["o"]) if nvda_rth_raw else None
        gap_ref_px = rth_open if rth_open is not None else pm_last_anchor
        gap_fill_50 = round(0.5 * (nvda_prev_close + gap_ref_px), 2)
        s4_open_flush_px = round(
            (rth_open if rth_open is not None else pm_last_anchor) * 0.9875, 2
        )

        bell_dt = datetime.datetime.combine(
            session_date, datetime.time(9, 30, 0), tzinfo=NY_TZ
        )
        sec_to_bell = max(0, int((bell_dt - now_et).total_seconds()))
        sec_into_5m = (now_et.minute % 5) * 60 + now_et.second
        sec_to_next_5m_close = max(0, 300 - sec_into_5m)

        soxl_live = soxl_peer["last"]
        crwv_live = crwv_peer["last"]
        rth_all_so_far = completed_rth + ([forming_rth] if forming_rth else [])
        rth_low_live = min((float(b["l"]) for b in rth_all_so_far), default=None)
        rth_high_live = max((float(b["h"]) for b in rth_all_so_far), default=None)
        rth_vwap_live = _compute_vwap(rth_all_so_far)

        hard_stop_breached = (nvda_live_px <= HARD_STOP_NVDA) or (
            rth_low_live is not None and rth_low_live <= HARD_STOP_NVDA
        )

        gate_t1_state = (
            "PASS"
            if soxl_live >= SOXL_GATE_MIN
            else ("WARN" if soxl_live >= SOXL_GATE_WARN else "FAIL")
        )
        gate_t2_state = (
            "PASS"
            if crwv_live >= CRWV_GATE_MIN
            else ("WARN" if crwv_live >= CRWV_GATE_WARN else "FAIL")
        )
        gate_t3_state = "FAIL" if hard_stop_breached else "PASS"
        gate_t4_state = (
            "STANDBY"
            if is_premarket
            else ("PASS" if (is_rth_open or completed_rth) else "CLOSED")
        )

        gates = [
            {
                "id": "T4",
                "name": "Premarket Guard / RTH Bell",
                "state": gate_t4_state,
                "current": f"{'PREMARKET' if is_premarket else ('RTH LIVE' if is_rth_open else 'POST-RTH')} ({now_et.strftime('%H:%M:%S')})",
                "threshold": ">= 09:30:00 ET (5m Close)",
                "note": (
                    f"Do NOT buy ${nvda_live_px:.2f} premarket (94-100% morning dip rate). Bell in {sec_to_bell // 60}m {sec_to_bell % 60:02d}s."
                    if is_premarket
                    else f"{len(completed_rth)} completed 5m RTH bars | next 5m close in {sec_to_next_5m_close}s"
                ),
            },
            {
                "id": "T1",
                "name": "SOXL Semi Tailwind",
                "state": gate_t1_state,
                "current": f"${soxl_live:.2f} ({soxl_peer['chg_pct']:+.2f}%)",
                "threshold": f"> ${SOXL_GATE_MIN:.2f} (PM_VWAP ${soxl_peer['pm_vwap'] or 0:.2f})",
                "note": "Confirms broad 3x semiconductor rally remains intact.",
            },
            {
                "id": "T2",
                "name": "CRWV Q3 Backlog Catalyst",
                "state": gate_t2_state,
                "current": f"${crwv_live:.2f} ({crwv_peer['chg_pct']:+.2f}%)",
                "threshold": f"> ${CRWV_GATE_MIN:.2f} (PM Low)",
                "note": "Confirms CoreWeave $132.5B backlog / $37B-$39B CapEx bid is holding.",
            },
            {
                "id": "T3",
                "name": "NVDA Gap-Integrity Guard",
                "state": gate_t3_state,
                "current": f"${nvda_live_px:.2f} (RTH Low {f'${rth_low_live:.2f}' if rth_low_live else 'N/A'})",
                "threshold": f"> ${HARD_STOP_NVDA:.2f} (Prev Close ${nvda_prev_close:.2f})",
                "note": "Full gap-fill below $168.40 invalidates the oversold bounce.",
            },
        ]

        running_bars: List[Dict[str, Any]] = []
        setup_a_probed = False
        setup_a_flush_low: Optional[float] = None
        setup_a_signal: Optional[Dict[str, Any]] = None

        setup_b_consec_count = 0
        setup_b_signal: Optional[Dict[str, Any]] = None

        for b in completed_rth:
            running_bars.append(b)
            rv = _compute_vwap(running_bars) or b["c"]
            b["rth_vwap"] = rv

            bar_low = float(b["l"])
            bar_close = float(b["c"])
            bar_open = float(b["o"])
            bar_hm = int(b["hm"])
            soxl_at_bar = (
                _lookup_bar_close_at_or_before(soxl_session_bars, b["epoch_ms"])
                or soxl_live
            )
            crwv_at_bar = (
                _lookup_bar_close_at_or_before(crwv_session_bars, b["epoch_ms"])
                or crwv_live
            )
            min_low_thru_bar = min(float(x["l"]) for x in running_bars)

            if min_low_thru_bar <= HARD_STOP_NVDA:
                self._log_event(
                    f"invalid_{session_date}",
                    b["ts_label"],
                    "DANGER",
                    "HARD STOP / INVALIDATION ($168.40 BREACHED)",
                    f"NVDA 5m bar {b['ts_label']} traded down to ${bar_low:.2f} (<= $168.40). Stand down.",
                )
                break

            if bar_hm <= 1130:
                if (
                    setup_a_probed
                    and setup_a_signal is None
                    and setup_a_flush_low is not None
                    and bar_close > bar_open
                    and bar_low > setup_a_flush_low
                    and bar_close < (pm_vwap - 0.15)
                    and soxl_at_bar >= SOXL_GATE_MIN
                    and crwv_at_bar >= CRWV_GATE_MIN
                ):
                    confirm_dt = b["dt"] + datetime.timedelta(minutes=5)
                    setup_a_signal = {
                        "setup": "SETUP_A",
                        "bar_open_ts": b["ts_label"],
                        "confirmed_at": confirm_dt.strftime("%H:%M:%S ET"),
                        "bar_epoch_ms": b["epoch_ms"],
                        "entry_price": round(bar_close, 2),
                        "flush_low": round(setup_a_flush_low, 2),
                        "hard_stop": HARD_STOP_NVDA,
                        "tight_stop": round(
                            max(HARD_STOP_NVDA, setup_a_flush_low - 0.25), 2
                        ),
                        "target_1": round(pm_vwap, 2),
                        "target_2": round(pm_high, 2),
                        "soxl_at_signal": round(soxl_at_bar, 2),
                        "crwv_at_signal": round(crwv_at_bar, 2),
                        "in_primary_window": bar_hm <= 1045,
                    }
                    self._log_event(
                        f"sig_a_{session_date}_{b['ts_label']}",
                        confirm_dt.strftime("%H:%M:%S"),
                        "SIGNAL",
                        f"SIGNAL FLASH — SETUP A RECLAIM @ ${bar_close:.2f}",
                        (
                            f"5m bar {b['ts_label']} closed green (${bar_open:.2f} -> ${bar_close:.2f}) with higher low "
                            f"${bar_low:.2f} above flush low ${setup_a_flush_low:.2f} | SOXL ${soxl_at_bar:.2f}, CRWV ${crwv_at_bar:.2f}."
                        ),
                    )

                if bar_low <= SETUP_A_ZONE_HIGH and bar_hm <= 1115:
                    if not setup_a_probed:
                        setup_a_probed = True
                        setup_a_flush_low = bar_low
                        self._log_event(
                            f"arm_a_{session_date}_{b['ts_label']}",
                            b["ts_label"],
                            "ARMED",
                            f"SETUP A ARMED — NVDA Probed Flush Zone (${bar_low:.2f})",
                            f"NVDA entered $170.90-$172.20 confluence zone on {b['ts_label']} bar. Waiting for 5m green higher-low reclaim.",
                        )
                    elif setup_a_flush_low is None or bar_low < setup_a_flush_low:
                        setup_a_flush_low = bar_low

            if 945 <= bar_hm <= 1430:
                hurdle = max(SETUP_B_MIN_HURDLE, pm_vwap, rv)
                if bar_close > hurdle:
                    setup_b_consec_count += 1
                    if setup_b_consec_count == 1 and setup_b_signal is None:
                        confirm_dt = b["dt"] + datetime.timedelta(minutes=5)
                        self._log_event(
                            f"arm_b1_{session_date}_{b['ts_label']}",
                            confirm_dt.strftime("%H:%M:%S"),
                            "ARMED",
                            f"SETUP B BAR 1/2 CLOSED ABOVE VWAP (${bar_close:.2f} > ${hurdle:.2f})",
                            "1st 5m RTH bar closed above PM_VWAP & RTH_VWAP. Need 1 more consecutive 5m close above hurdle.",
                        )
                    elif (
                        setup_b_consec_count >= 2
                        and setup_b_signal is None
                        and bar_close >= bar_open
                        and soxl_at_bar >= SOXL_GATE_MIN
                        and crwv_at_bar >= CRWV_GATE_MIN
                    ):
                        confirm_dt = b["dt"] + datetime.timedelta(minutes=5)
                        rth_morning_low = min(float(x["l"]) for x in running_bars)
                        b_stop = round(
                            min(
                                bar_close - 0.75,
                                max(SETUP_B_FALLBACK_STOP, rth_morning_low),
                            ),
                            2,
                        )
                        setup_b_signal = {
                            "setup": "SETUP_B",
                            "bar_open_ts": b["ts_label"],
                            "confirmed_at": confirm_dt.strftime("%H:%M:%S ET"),
                            "bar_epoch_ms": b["epoch_ms"],
                            "entry_price": round(bar_close, 2),
                            "hurdle_cleared": round(hurdle, 2),
                            "hard_stop": b_stop,
                            "tight_stop": b_stop,
                            "target_1": round(pm_high, 2),
                            "target_2": SETUP_B_TARGET_2,
                            "soxl_at_signal": round(soxl_at_bar, 2),
                            "crwv_at_signal": round(crwv_at_bar, 2),
                        }
                        self._log_event(
                            f"sig_b_{session_date}_{b['ts_label']}",
                            confirm_dt.strftime("%H:%M:%S"),
                            "SIGNAL",
                            f"SIGNAL FLASH — SETUP B VWAP RECLAIM @ ${bar_close:.2f}",
                            (
                                f"2nd consecutive 5m RTH bar ({b['ts_label']}) closed at ${bar_close:.2f} above "
                                f"PM_VWAP (${pm_vwap:.2f}) & RTH_VWAP (${rv:.2f}) | Stop ${b_stop:.2f}."
                            ),
                        )
                else:
                    setup_b_consec_count = 0

        if forming_rth is not None:
            forming_rth["rth_vwap"] = rth_vwap_live or forming_rth["c"]

        forming_in_flush_zone = (
            forming_rth is not None
            and float(forming_rth["l"]) <= SETUP_A_ZONE_HIGH
            and not hard_stop_breached
        )
        if forming_in_flush_zone and not setup_a_probed:
            self._log_event(
                f"live_probe_a_{session_date}_{forming_rth['ts_label']}",
                now_et.strftime("%H:%M:%S"),
                "ARMED",
                f"LIVE INTRA-BAR PROBE INTO FLUSH ZONE (${float(forming_rth['l']):.2f})",
                f"NVDA dipped <= ${SETUP_A_ZONE_HIGH:.2f} on live {forming_rth['ts_label']} candle. Watch next 5m green higher-low close!",
            )

        effective_flush_low = setup_a_flush_low
        if forming_rth is not None and float(forming_rth["l"]) <= SETUP_A_ZONE_HIGH:
            effective_flush_low = (
                min(effective_flush_low, float(forming_rth["l"]))
                if effective_flush_low is not None
                else float(forming_rth["l"])
            )

        forming_a_reclaim_preview = (
            setup_a_probed
            and setup_a_signal is None
            and forming_rth is not None
            and setup_a_flush_low is not None
            and float(forming_rth["c"]) > float(forming_rth["o"])
            and float(forming_rth["l"]) > setup_a_flush_low
            and gate_t1_state == "PASS"
            and gate_t2_state == "PASS"
            and not hard_stop_breached
        )

        setup_b_live_hurdle = round(
            max(SETUP_B_MIN_HURDLE, pm_vwap, rth_vwap_live or pm_vwap), 2
        )
        forming_b_bar2_preview = (
            setup_b_consec_count >= 1
            and setup_b_signal is None
            and forming_rth is not None
            and int(forming_rth["hm"]) >= 950
            and float(forming_rth["c"]) > setup_b_live_hurdle
            and float(forming_rth["c"]) >= float(forming_rth["o"])
            and gate_t1_state == "PASS"
            and gate_t2_state == "PASS"
            and not hard_stop_breached
        )

        active_signal = setup_a_signal or setup_b_signal
        if hard_stop_breached:
            banner = {
                "code": "INVALIDATED",
                "severity": "danger",
                "headline": f"STAND DOWN — NVDA BREACHED ${HARD_STOP_NVDA:.2f} HARD GUARD",
                "subtext": (
                    f"NVDA traded down to ${min(nvda_live_px, rth_low_live or nvda_live_px):.2f}, filling prior "
                    f"${nvda_prev_close:.2f} close. Oversold gap-and-hold thesis is invalidated."
                ),
            }
        elif active_signal is not None:
            sig_name = (
                "SETUP A (RTH FLUSH & HIGHER-LOW RECLAIM)"
                if active_signal["setup"] == "SETUP_A"
                else "SETUP B (2-BAR PM_VWAP + RTH_VWAP BREAKOUT)"
            )
            pnl_vs_entry = (
                (nvda_live_px / active_signal["entry_price"]) - 1.0
            ) * 100.0
            banner = {
                "code": f"SIGNAL_FLASH_{active_signal['setup']}",
                "severity": "signal",
                "headline": f"SIGNAL FLASH — {sig_name} CONFIRMED @ ${active_signal['entry_price']:.2f}",
                "subtext": (
                    f"Confirmed at {active_signal['confirmed_at']} on {active_signal['bar_open_ts']} 5m bar | "
                    f"Live ${nvda_live_px:.2f} ({pnl_vs_entry:+.2f}% vs signal) | "
                    f"Hard Stop ${active_signal['hard_stop']:.2f} | T1 ${active_signal['target_1']:.2f} | T2 ${active_signal['target_2']:.2f}"
                ),
            }
        elif is_premarket:
            banner = {
                "code": "PREMARKET_STANDBY",
                "severity": "standby",
                "headline": f"PREMARKET GUARD ACTIVE — DO NOT CHASE ${nvda_live_px:.2f} BEFORE 09:30 BELL",
                "subtext": (
                    f"NVDA is {'below' if nvda_live_px < pm_vwap else 'near'} PM_VWAP (${pm_vwap:.2f}). "
                    f"94%-100% of historical analogs dip after the bell. Waiting {sec_to_bell // 60}m {sec_to_bell % 60:02d}s "
                    f"for RTH flush into ${SETUP_A_ZONE_LOW:.2f}-${SETUP_A_ZONE_HIGH:.2f} (or post-09:45 VWAP reclaim)."
                ),
            }
        elif forming_a_reclaim_preview and forming_rth is not None:
            banner = {
                "code": "SETUP_A_FORMING_TRIGGER",
                "severity": "armed",
                "headline": f"GET READY — SETUP A GREEN HIGHER-LOW CANDLE FORMING (${nvda_live_px:.2f})",
                "subtext": (
                    f"Flush low ${setup_a_flush_low:.2f} is in! Current 5m bar holds higher low (${float(forming_rth['l']):.2f}) "
                    f"and is green. Signal confirms if still green + gates hold at 5m close in {sec_to_next_5m_close}s!"
                ),
            }
        elif forming_b_bar2_preview:
            banner = {
                "code": "SETUP_B_FORMING_TRIGGER",
                "severity": "armed",
                "headline": f"GET READY — SETUP B BAR 2/2 HOLDING ABOVE ${setup_b_live_hurdle:.2f} (${nvda_live_px:.2f})",
                "subtext": (
                    f"Bar 1 already closed above PM_VWAP & RTH_VWAP. Current 5m bar confirms Setup B breakout "
                    f"if it closes > ${setup_b_live_hurdle:.2f} in {sec_to_next_5m_close}s!"
                ),
            }
        elif (setup_a_probed or forming_in_flush_zone) and effective_flush_low is not None:
            banner = {
                "code": "SETUP_A_ARMED",
                "severity": "armed",
                "headline": f"SETUP A ARMED — NVDA IN FLUSH ZONE (LOW ${effective_flush_low:.2f})",
                "subtext": (
                    f"Confluence zone (${SETUP_A_ZONE_LOW:.2f}-${SETUP_A_ZONE_HIGH:.2f}) reached! Do NOT catch falling knife—waiting "
                    f"for a completed 5m green higher-low candle above ${effective_flush_low:.2f} (next close in {sec_to_next_5m_close}s)."
                ),
            }
        elif setup_b_consec_count == 1:
            banner = {
                "code": "SETUP_B_ARMED_BAR1",
                "severity": "armed",
                "headline": f"SETUP B ARMED (1/2 BARS) — WATCHING FOR 2ND 5M CLOSE > ${setup_b_live_hurdle:.2f}",
                "subtext": (
                    f"1st 5m RTH candle closed above PM_VWAP (${pm_vwap:.2f}) & RTH_VWAP (${rth_vwap_live or 0:.2f}). "
                    f"Next 5m bar close in {sec_to_next_5m_close}s."
                ),
            }
        else:
            banner = {
                "code": "RTH_WATCHING",
                "severity": "watching",
                "headline": f"RTH MONITORING — WAITING FOR FLUSH <= ${SETUP_A_ZONE_HIGH:.2f} OR VWAP RECLAIM > ${setup_b_live_hurdle:.2f}",
                "subtext": (
                    f"NVDA Live ${nvda_live_px:.2f} | RTH Range ${rth_low_live or nvda_live_px:.2f}-${rth_high_live or nvda_live_px:.2f} | "
                    f"PM_VWAP ${pm_vwap:.2f} | RTH_VWAP ${rth_vwap_live or nvda_live_px:.2f} | Next 5m close in {sec_to_next_5m_close}s."
                ),
            }

        self._log_event(
            f"boot_{session_date}",
            now_et.strftime("%H:%M:%S"),
            "INFO",
            "NVDA Signal Monitor Online",
            (
                f"Locked anchors: Prev Close ${nvda_prev_close:.2f} | PM Low ${pm_low:.2f} | PM VWAP ${pm_vwap:.2f} | "
                f"PM High ${pm_high:.2f} | 50% Gap-Fill ${gap_fill_50:.2f}."
            ),
        )

        setup_a_entry_est = (
            setup_a_signal["entry_price"]
            if setup_a_signal
            else round(
                nvda_live_px
                if forming_a_reclaim_preview
                else max(
                    SETUP_A_ZONE_LOW + 0.55,
                    min(SETUP_A_ZONE_HIGH, gap_fill_50 + 0.35),
                ),
                2,
            )
        )
        setup_a_tight_stop_est = (
            setup_a_signal["tight_stop"]
            if setup_a_signal
            else round(
                (effective_flush_low - 0.25) if effective_flush_low else (pm_low - 0.35),
                2,
            )
        )
        setup_a_card = {
            "name": "Setup A — RTH Morning Shakeout & 5m Higher-Low Reclaim",
            "window": "09:30 – 10:45 AM ET (tracked thru 11:30 AM)",
            "status": (
                "TRIGGERED"
                if setup_a_signal
                else (
                    "FORMING RECLAIM"
                    if forming_a_reclaim_preview
                    else (
                        "ARMED IN ZONE"
                        if (setup_a_probed or forming_in_flush_zone)
                        else "WATCHING"
                    )
                )
            ),
            "signal": setup_a_signal,
            "checklist": [
                {
                    "label": "1. RTH Open >= 09:30 ET (No Premarket Chasing)",
                    "done": not is_premarket,
                    "detail": (
                        "LIVE"
                        if not is_premarket
                        else f"Wait {sec_to_bell // 60}m {sec_to_bell % 60:02d}s"
                    ),
                },
                {
                    "label": f"2. NVDA RTH Low Probes ${SETUP_A_ZONE_LOW:.2f}–${SETUP_A_ZONE_HIGH:.2f} (and > ${HARD_STOP_NVDA:.2f})",
                    "done": bool(setup_a_probed or forming_in_flush_zone),
                    "detail": (
                        f"Flush Low ${effective_flush_low:.2f}"
                        if effective_flush_low is not None
                        else (
                            f"RTH Low ${rth_low_live:.2f} (need <= ${SETUP_A_ZONE_HIGH:.2f}; S4 -1.25% = ${s4_open_flush_px:.2f})"
                            if rth_low_live is not None
                            else f"PM Low ${pm_low:.2f} | 50% Gap ${gap_fill_50:.2f}"
                        )
                    ),
                },
                {
                    "label": "3. Completed 5m Green Higher-Low Reclaim Candle",
                    "done": setup_a_signal is not None,
                    "detail": (
                        f"Confirmed @ ${setup_a_signal['entry_price']:.2f} ({setup_a_signal['bar_open_ts']} bar)"
                        if setup_a_signal
                        else (
                            f"FORMING NOW (${nvda_live_px:.2f}, {sec_to_next_5m_close}s left)"
                            if forming_a_reclaim_preview
                            else "Awaiting flush probe + 5m green higher-low close"
                        )
                    ),
                },
                {
                    "label": f"4. Tailwinds Hold (SOXL > ${SOXL_GATE_MIN:.2f} & CRWV > ${CRWV_GATE_MIN:.2f})",
                    "done": gate_t1_state == "PASS" and gate_t2_state == "PASS",
                    "detail": f"SOXL ${soxl_live:.2f} | CRWV ${crwv_live:.2f}",
                },
            ],
            "plan": {
                "is_confirmed": setup_a_signal is not None,
                "entry": setup_a_entry_est,
                "limit_cap": round(setup_a_entry_est + 0.10, 2),
                "hard_stop": HARD_STOP_NVDA,
                "tight_stop": setup_a_tight_stop_est,
                "target_1": round(pm_vwap, 2),
                "target_2": round(pm_high, 2),
                "swing_note": "Optional Tranche 2 hold T-4 -> T0 into Tue Nov 17 pre-earnings close (exit before 4:20 PM AMC).",
            },
        }

        setup_b_entry_est = (
            setup_b_signal["entry_price"]
            if setup_b_signal
            else round(
                max(
                    nvda_live_px if forming_b_bar2_preview else 0.0,
                    setup_b_live_hurdle + 0.15,
                ),
                2,
            )
        )
        setup_b_stop_est = (
            setup_b_signal["hard_stop"]
            if setup_b_signal
            else round(
                min(
                    setup_b_entry_est - 0.75,
                    max(
                        SETUP_B_FALLBACK_STOP,
                        rth_low_live
                        if rth_low_live is not None
                        else SETUP_B_FALLBACK_STOP,
                    ),
                ),
                2,
            )
        )
        setup_b_card = {
            "name": "Setup B — Post-09:45 PM_VWAP + RTH_VWAP Strength Reclaim",
            "window": ">= 09:45 AM ET (if NVDA refuses to flush deeply)",
            "status": (
                "TRIGGERED"
                if setup_b_signal
                else (
                    "FORMING BAR 2/2"
                    if forming_b_bar2_preview
                    else ("ARMED (BAR 1/2)" if setup_b_consec_count >= 1 else "WATCHING")
                )
            ),
            "signal": setup_b_signal,
            "checklist": [
                {
                    "label": "1. Opening Shakeout Window >= 09:45 AM ET",
                    "done": not is_premarket
                    and (now_et.hour * 100 + now_et.minute) >= 945,
                    "detail": f"Current {now_et.strftime('%H:%M ET')}",
                },
                {
                    "label": f"2. Bar 1/2 Closes Above PM_VWAP & RTH_VWAP (> ${setup_b_live_hurdle:.2f})",
                    "done": setup_b_consec_count >= 1 or setup_b_signal is not None,
                    "detail": f"Consecutive closes above hurdle: {2 if setup_b_signal else setup_b_consec_count}/2",
                },
                {
                    "label": f"3. Bar 2/2 Green Follow-Through Close (> ${setup_b_live_hurdle:.2f})",
                    "done": setup_b_signal is not None,
                    "detail": (
                        f"Confirmed @ ${setup_b_signal['entry_price']:.2f} ({setup_b_signal['bar_open_ts']} bar)"
                        if setup_b_signal
                        else (
                            f"FORMING BAR 2/2 (${nvda_live_px:.2f}, {sec_to_next_5m_close}s left)"
                            if forming_b_bar2_preview
                            else "Waiting for 2 consecutive 5m closes"
                        )
                    ),
                },
                {
                    "label": f"4. Tailwinds Hold (SOXL > ${SOXL_GATE_MIN:.2f} & CRWV > ${CRWV_GATE_MIN:.2f})",
                    "done": gate_t1_state == "PASS" and gate_t2_state == "PASS",
                    "detail": f"SOXL ${soxl_live:.2f} | CRWV ${crwv_live:.2f}",
                },
            ],
            "plan": {
                "is_confirmed": setup_b_signal is not None,
                "entry": setup_b_entry_est,
                "limit_cap": round(setup_b_entry_est + 0.10, 2),
                "hard_stop": setup_b_stop_est,
                "tight_stop": setup_b_stop_est,
                "target_1": round(pm_high, 2),
                "target_2": SETUP_B_TARGET_2,
                "swing_note": "Momentum reclaim toward PM High ($178.36) and Nov 9 breakdown high ($179.92/$180.00).",
            },
        }

        chart_bars: List[Dict[str, Any]] = []
        for b in nvda_pm_bars:
            chart_bars.append(
                {
                    "ts": b["ts_label"],
                    "epoch_ms": b["epoch_ms"],
                    "session": "PM",
                    "o": round(float(b["o"]), 2),
                    "h": round(float(b["h"]), 2),
                    "l": round(float(b["l"]), 2),
                    "c": round(float(b["c"]), 2),
                    "v": int(b["v"]),
                    "rth_vwap": None,
                    "forming": False,
                }
            )
        for b in completed_rth:
            chart_bars.append(
                {
                    "ts": b["ts_label"],
                    "epoch_ms": b["epoch_ms"],
                    "session": "RTH",
                    "o": round(float(b["o"]), 2),
                    "h": round(float(b["h"]), 2),
                    "l": round(float(b["l"]), 2),
                    "c": round(float(b["c"]), 2),
                    "v": int(b["v"]),
                    "rth_vwap": (
                        round(float(b["rth_vwap"]), 2) if b.get("rth_vwap") else None
                    ),
                    "forming": False,
                }
            )
        if forming_rth is not None:
            chart_bars.append(
                {
                    "ts": forming_rth["ts_label"],
                    "epoch_ms": forming_rth["epoch_ms"],
                    "session": "RTH",
                    "o": round(float(forming_rth["o"]), 2),
                    "h": round(float(forming_rth["h"]), 2),
                    "l": round(float(forming_rth["l"]), 2),
                    "c": round(float(forming_rth["c"]), 2),
                    "v": int(forming_rth["v"]),
                    "rth_vwap": (
                        round(float(forming_rth["rth_vwap"]), 2)
                        if forming_rth.get("rth_vwap")
                        else None
                    ),
                    "forming": True,
                }
            )

        return {
            "ok": True,
            "updated_at_et": now_et.strftime("%Y-%m-%d %H:%M:%S ET"),
            "session_date": str(session_date),
            "prev_session_date": (
                str(prev_session_date) if prev_session_date else "2026-11-10"
            ),
            "market_phase": (
                "PREMARKET"
                if is_premarket
                else (
                    "RTH"
                    if is_rth_open
                    else ("AFTER_HOURS" if is_after_hours else "HISTORICAL")
                )
            ),
            "sec_to_bell": sec_to_bell,
            "sec_to_next_5m_close": sec_to_next_5m_close,
            "banner": banner,
            "levels": {
                "prev_close": round(nvda_prev_close, 2),
                "hard_stop": HARD_STOP_NVDA,
                "flush_zone_low": SETUP_A_ZONE_LOW,
                "flush_zone_high": SETUP_A_ZONE_HIGH,
                "pm_low": round(pm_low, 2),
                "gap_fill_50": gap_fill_50,
                "s4_open_flush": s4_open_flush_px,
                "rth_open": round(rth_open, 2) if rth_open is not None else None,
                "rth_low": round(rth_low_live, 2) if rth_low_live is not None else None,
                "rth_high": (
                    round(rth_high_live, 2) if rth_high_live is not None else None
                ),
                "rth_vwap": (
                    round(rth_vwap_live, 2) if rth_vwap_live is not None else None
                ),
                "pm_vwap": round(pm_vwap, 2),
                "setup_b_hurdle": setup_b_live_hurdle,
                "pm_high": round(pm_high, 2),
                "target_180": SETUP_B_TARGET_2,
            },
            "nvda": nvda_peer,
            "gates": gates,
            "setup_a": setup_a_card,
            "setup_b": setup_b_card,
            "peers": [peers[s] for s in SYMBOLS if s in peers],
            "chart_bars": chart_bars,
            "signals": [s for s in (setup_a_signal, setup_b_signal) if s is not None],
            "events": list(self._event_log[:25]),
        }

    def poll_now(self) -> Dict[str, Any]:
        try:
            snap = self._poll_once()
            with self._lock:
                self._latest_state = snap
                return dict(self._latest_state)
        except Exception as exc:
            now_str = datetime.datetime.now(NY_TZ).strftime("%H:%M:%S ET")
            with self._lock:
                self._latest_state["last_error"] = f"{now_str}: {exc}"
                if not self._latest_state.get("ok"):
                    self._latest_state["status"] = "FEED_RETRYING"
                    self._latest_state["message"] = f"Feed retry ({exc})"
                return dict(self._latest_state)

    def get_state(self, lazy_prime: bool = True) -> Dict[str, Any]:
        with self._lock:
            ready = bool(self._latest_state.get("ok"))
            cached = dict(self._latest_state)
        if not ready and lazy_prime:
            return self.poll_now()
        return cached

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            self.poll_now()
            self._stop_event.wait(self.poll_interval_sec)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="nvda-signal-poller", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.5)
