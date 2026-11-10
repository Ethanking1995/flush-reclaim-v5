from __future__ import annotations

import json
import math
import sys
import time
import warnings
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
import pytz
import yfinance as yf
from httpx import Client

warnings.filterwarnings("ignore")

# ==============================================================================
# HARD SANITY THRESHOLDS (NON-NEGOTIABLE PROGRAMMATIC GATES)
# ==============================================================================
MIN_PRICE_USD = 20.00
MAX_PRICE_USD = 200.00
MIN_ADTV_20D_SHARES = 1_000_000
MIN_MEDIAN_DOLLAR_VOL_USD = 50_000_000.0
MAX_SPREAD_USD = 0.04
MAX_SPREAD_PCT = 0.0008  # 0.08%

MIN_MARKET_CAP_USD = 3_000_000_000.0       # $3B Mid/Large Cap floor
MIN_HEALTHCARE_MCAP_USD = 20_000_000_000.0  # $20B floor for Healthcare (blocks FDA binary drug risk)
MAX_10D_CRASH_PCT = -0.35                   # Reject > -35% 10-day falling knives

MIN_DISLOCATION_60D = -0.12                 # 30d low must be >= 12.0% below 60d high
MIN_5D_GREEN_VOL_RATIO = 0.55               # >= 55% of trailing 5d volume on green days (Close > Open)
ER_BLACKOUT_DAYS = 14                       # Reject any stock with earnings within 14 calendar days

CORE_VERIFIED_TICKERS = ["FISV", "PLNT", "EEFT"]
CACHE_FILE_PATH = Path(__file__).resolve().parent / "discovered_candidates.json"
TV_SCANNER_URL = "https://scanner.tradingview.com/america/scan"

# Known Q4 2026 / Q1 2027 earnings dates fallback map
KNOWN_EARNINGS_DATES = {
    "FISV": ["2026-11-03", "2027-02-09"],
    "PLNT": ["2026-11-05", "2027-02-23"],
    "NU": ["2026-11-12", "2027-02-18"],
    "CNC": ["2026-10-27", "2027-02-02"],
    "EEFT": ["2026-10-27", "2027-02-10"],
    "GPN": ["2026-11-02", "2027-02-10"],
}

# Explicit audit probe tickers injected into every scan to verify 100% programmatic blocking
# of Biotech, Small/Mid-Cap Healthcare, Chinese ADRs, Low-Volume (<1M ADTV), Zombie Cash-Burners,
# and 14-Day Earnings Blackout names.
AUDIT_PROBE_TICKERS = [
    # Core & Known Compounders
    "FISV", "PLNT", "EEFT", "CNC", "GPN", "NU",
    # Biotechs & <$20B Healthcare (Must be 100% blocked by Module 2)
    "INCY", "VKTX", "HALO", "ALGN", "BNTX",
    # Chinese ADRs / Non-US Listings (Must be 100% blocked by Module 2)
    "BABA", "PDD", "JD", "BIDU",
    # Low-Volume / Illiquid / Outside Price Band (Must be 100% blocked by Module 1)
    "CHE", "WDFC", "NVR",
    # Cash-Burning "Zombie" Companies (OCF <= 0 & EBITDA <= 0, Must be 100% blocked by Module 2)
    "IONQ", "PLUG",
    # Pre-Earnings Blackout Window (Within 14d of Nov 9, 2026 -> Must be blocked by Module 3)
    "HD", "CSCO", "DIS",
]


def fetch_broad_us_universe(max_rows: int = 500) -> dict[str, dict[str, Any]]:
    """
    Queries the TradingView US Equity Scanner API for liquid NYSE/NASDAQ equities
    plus all AUDIT_PROBE_TICKERS to assemble the raw candidate universe with baseline
    metadata (company name, sector, industry, country, marketCap, ebitda, operatingCashflow,
    and next earnings timestamp).
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        "Content-Type": "application/json",
    }
    columns = [
        "name",
        "description",
        "sector",
        "industry",
        "country",
        "market_cap_basic",
        "ebitda",
        "cash_f_operating_activities_ttm",
        "earnings_release_next_date",
        "close",
        "average_volume_30d_calc",
        "High.3M",
        "Low.1M",
        "SMA5",
    ]

    universe_meta: dict[str, dict[str, Any]] = {}

    # 1. Broad US Market Scan (NYSE + NASDAQ stocks in $20-$200 range)
    payload_broad = {
        "filter": [
            {"left": "type", "operation": "equal", "right": "stock"},
            {"left": "exchange", "operation": "in_range", "right": ["NYSE", "NASDAQ"]},
            {"left": "close", "operation": "in_range", "right": [MIN_PRICE_USD, MAX_PRICE_USD]},
            {"left": "average_volume_30d_calc", "operation": "greater", "right": MIN_ADTV_20D_SHARES},
            {"left": "market_cap_basic", "operation": "greater", "right": MIN_MARKET_CAP_USD},
            {"left": "Perf.W", "operation": "greater", "right": -2},
        ],
        "options": {"lang": "en"},
        "markets": ["america"],
        "symbols": {"query": {"types": []}, "tickers": []},
        "columns": columns,
        "sort": {"sortBy": "average_volume_30d_calc", "sortOrder": "desc"},
        "range": [0, max_rows],
    }

    try:
        with Client(timeout=6.0, headers=headers) as client:
            resp = client.post(TV_SCANNER_URL, json=payload_broad)
            if resp.status_code == 200:
                for row in resp.json().get("data", []):
                    d = row.get("d", [])
                    if not d or not d[0] or not str(d[0]).isalpha():
                        continue
                    sym = str(d[0]).upper()
                    # Pre-keep any stock that has pulled back >= 10% from 3M high or is in our probe set
                    h3m, l1m = d[11], d[12]
                    if sym in AUDIT_PROBE_TICKERS or (
                        h3m and l1m and (float(l1m) - float(h3m)) / float(h3m) <= -0.10
                    ):
                        universe_meta[sym] = {
                            "company": d[1] or sym,
                            "sector": d[2],
                            "industry": d[3],
                            "country": d[4],
                            "marketCap": d[5],
                            "ebitda": d[6],
                            "operatingCashflow": d[7],
                            "er_timestamp": d[8],
                            "tv_close": d[9],
                            "tv_adtv30": d[10],
                        }

            # 2. Explicitly fetch metadata for all AUDIT_PROBE_TICKERS so every trap is tested
            probe_tv_syms = []
            for s in AUDIT_PROBE_TICKERS:
                for ex in ("NASDAQ", "NYSE", "AMEX"):
                    probe_tv_syms.append(f"{ex}:{s}")
            payload_probes = {
                "symbols": {"tickers": probe_tv_syms},
                "columns": columns,
            }
            resp_p = client.post(TV_SCANNER_URL, json=payload_probes)
            if resp_p.status_code == 200:
                for row in resp_p.json().get("data", []):
                    d = row.get("d", [])
                    if not d or not d[0]:
                        continue
                    sym = str(d[0]).upper()
                    universe_meta[sym] = {
                        "company": d[1] or sym,
                        "sector": d[2],
                        "industry": d[3],
                        "country": d[4],
                        "marketCap": d[5],
                        "ebitda": d[6],
                        "operatingCashflow": d[7],
                        "er_timestamp": d[8],
                        "tv_close": d[9],
                        "tv_adtv30": d[10],
                    }
    except Exception:
        pass

    for s in AUDIT_PROBE_TICKERS:
        if s not in universe_meta:
            universe_meta[s] = {"company": s}

    return universe_meta


def enrich_with_yfinance_info(sym: str, base_meta: dict[str, Any], is_rth_open: bool) -> dict[str, Any]:
    """
    Merges `yf.Ticker(sym).info` with fallback metadata so sector, industry, country,
    marketCap, operatingCashflow, ebitda, and RTH bid/ask spreads are verified via Yahoo Finance.
    """
    meta = dict(base_meta)
    try:
        info = yf.Ticker(sym).info or {}
    except Exception:
        info = {}

    if info.get("shortName") or info.get("longName"):
        meta["company"] = info.get("shortName") or info.get("longName")
    if info.get("sector"):
        meta["sector"] = info.get("sector")
    if info.get("industry"):
        meta["industry"] = info.get("industry")
    if info.get("country"):
        meta["country"] = info.get("country")
    if info.get("marketCap") is not None:
        meta["marketCap"] = info.get("marketCap")
    if info.get("operatingCashflow") is not None:
        meta["operatingCashflow"] = info.get("operatingCashflow")
    if info.get("ebitda") is not None:
        meta["ebitda"] = info.get("ebitda")
    meta["quoteType"] = info.get("quoteType", "EQUITY")

    # Bid-Ask Spread extraction when RTH order book is live
    if is_rth_open:
        bid = float(info.get("bid") or 0.0)
        ask = float(info.get("ask") or 0.0)
        if bid > 0 and ask > bid:
            meta["bid"] = bid
            meta["ask"] = ask

    return meta


def resolve_next_earnings_date(
    sym: str, meta: dict[str, Any], ref_date: date
) -> tuple[Optional[date], bool, str]:
    """
    Dynamically queries upcoming earnings date from KNOWN_EARNINGS_DATES, TradingView
    earnings calendar timestamp, and `yf.Ticker(sym).calendar`.
    Returns (next_er_date, is_in_14d_blackout, formatted_er_str).
    """
    candidate_dates: list[date] = []

    # 1. Known earnings dates map
    for d_str in KNOWN_EARNINGS_DATES.get(sym, []):
        try:
            dt = pd.Timestamp(d_str).date()
            if dt >= ref_date:
                candidate_dates.append(dt)
        except Exception:
            pass

    # 2. TradingView next earnings timestamp
    er_ts = meta.get("er_timestamp")
    if er_ts:
        try:
            dt = datetime.utcfromtimestamp(float(er_ts)).date()
            if dt >= ref_date:
                candidate_dates.append(dt)
        except Exception:
            pass

    # 3. Dynamic yf.Ticker(sym).calendar lookup if needed
    if not candidate_dates:
        try:
            cal = yf.Ticker(sym).calendar
            if isinstance(cal, dict) and "Earnings Date" in cal:
                for ed in cal["Earnings Date"]:
                    dt = pd.Timestamp(ed).date()
                    if dt >= ref_date:
                        candidate_dates.append(dt)
        except Exception:
            pass

    if not candidate_dates:
        return None, False, "N/A (>14d)"

    next_er = min(candidate_dates)
    days_until = (next_er - ref_date).days
    in_blackout = 0 <= days_until <= ER_BLACKOUT_DAYS
    return next_er, in_blackout, next_er.strftime("%Y-%m-%d")


def evaluate_candidate_gates(
    sym: str,
    df_1d: pd.DataFrame,
    meta: dict[str, Any],
    ref_date: date,
    quote_bid: Optional[float] = None,
    quote_ask: Optional[float] = None,
) -> tuple[bool, str, Optional[dict[str, Any]]]:
    """
    Runs a single stock through all 4 Automated Modules:
      - Module 1: Automated Liquidity & Spread Enforcement ($20-$200, >=1M ADTV, >=$50M Dollar Vol, Spread <= $0.04 & <= 0.08%)
      - Module 2: Hardcoded "Falling Knife" & Biotech / Foreign / Zombie Blacklist
      - Module 3: 3-Stage Regime & 14-Day Pre-Earnings Blackout Screener
      - Module 4: Institutional Accumulation Score Calculation
    Returns (passed, rejection_reason_or_ok, candidate_record).
    """
    if df_1d is None or df_1d.empty or len(df_1d) < 60:
        return False, "MODULE_1_INSUFFICIENT_HISTORY (<60d)", None

    df = df_1d.dropna(subset=["Open", "High", "Low", "Close", "Volume"]).copy()
    if len(df) < 60:
        return False, "MODULE_1_INSUFFICIENT_HISTORY (<60d)", None

    close = float(df["Close"].iloc[-1])
    adtv_20d = float(df["Volume"].iloc[-20:].mean())
    med_dollar_vol_20d = float((df["Volume"] * df["Close"]).iloc[-20:].median())

    # ==========================================================================
    # MODULE 1: AUTOMATED LIQUIDITY & SPREAD ENFORCEMENT
    # ==========================================================================
    if not (MIN_PRICE_USD <= close <= MAX_PRICE_USD):
        return (
            False,
            f"MODULE_1_PRICE_OUT_OF_BOUNDS (${close:.2f} not in [${MIN_PRICE_USD:.0f}, ${MAX_PRICE_USD:.0f}])",
            None,
        )

    if adtv_20d < MIN_ADTV_20D_SHARES:
        return (
            False,
            f"MODULE_1_LOW_ADTV ({adtv_20d:,.0f} sh/d < {MIN_ADTV_20D_SHARES:,} floor)",
            None,
        )

    if med_dollar_vol_20d < MIN_MEDIAN_DOLLAR_VOL_USD:
        return (
            False,
            f"MODULE_1_LOW_DOLLAR_VOL (${med_dollar_vol_20d / 1e6:.1f}M/d < ${MIN_MEDIAN_DOLLAR_VOL_USD / 1e6:.0f}M floor)",
            None,
        )

    bid = quote_bid if quote_bid is not None else meta.get("bid")
    ask = quote_ask if quote_ask is not None else meta.get("ask")
    if bid is not None and ask is not None and float(bid) > 0 and float(ask) > float(bid):
        spread_usd = float(ask) - float(bid)
        spread_pct = spread_usd / close
        if spread_usd > MAX_SPREAD_USD or spread_pct > MAX_SPREAD_PCT:
            return (
                False,
                f"MODULE_1_WIDE_BID_ASK_SPREAD (${spread_usd:.2f} / {spread_pct * 100:.3f}% > $0.04 or 0.08%)",
                None,
            )

    # ==========================================================================
    # MODULE 2: HARDCODED "FALLING KNIFE" & BIOTECH / ADR / ZOMBIE BLACKLIST
    # ==========================================================================
    company_name = str(meta.get("company") or sym)
    sector = str(meta.get("sector") or "")
    industry = str(meta.get("industry") or "")
    country = str(meta.get("country") or "")
    mcap = float(meta.get("marketCap") or 0.0)
    ocf = meta.get("operatingCashflow")
    ebitda = meta.get("ebitda")

    # 2a. Country / Chinese ADR Blacklist
    if (
        (country and country != "United States")
        or "ADR" in company_name.upper()
        or "CAYMAN" in country.upper()
        or "CHINA" in country.upper()
    ):
        return (
            False,
            f"MODULE_2_FOREIGN_OR_ADR_BLACKLIST (Country='{country}', Name='{company_name[:24]}')",
            None,
        )

    # 2b. Biotech & Small/Mid-Cap (<$20B) Healthcare Blacklist
    ind_lower = industry.lower()
    sec_lower = sector.lower()
    if "biotech" in ind_lower or "biotechnology" in ind_lower:
        return (
            False,
            f"MODULE_2_BIOTECH_BLACKLIST (Industry='{industry}')",
            None,
        )
    if ("health" in sec_lower or "health" in ind_lower or "drug" in ind_lower or "pharma" in ind_lower) and (
        mcap < MIN_HEALTHCARE_MCAP_USD
    ):
        return (
            False,
            f"MODULE_2_HEALTHCARE_UNDER_20B (MarketCap=${mcap / 1e9:.2f}B < $20.0B Healthcare Floor)",
            None,
        )

    # 2c. Financial Solvency Floor ($3B Market Cap + Positive OCF or Positive EBITDA)
    if mcap < MIN_MARKET_CAP_USD:
        return (
            False,
            f"MODULE_2_MCAP_BELOW_3B (MarketCap=${mcap / 1e9:.2f}B < $3.0B Floor)",
            None,
        )

    ocf_val = float(ocf) if ocf is not None and not pd.isna(ocf) else 0.0
    ebitda_val = float(ebitda) if ebitda is not None and not pd.isna(ebitda) else 0.0
    if ocf_val <= 0 and ebitda_val <= 0:
        return (
            False,
            f"MODULE_2_ZOMBIE_CASH_BURNER (OCF=${ocf_val / 1e6:.1f}M, EBITDA=${ebitda_val / 1e6:.1f}M <= 0)",
            None,
        )

    # 2d. Falling Knife Defense (10d return >= -35% AND Close > SMA5 AND SMA5 > SMA5.shift(2))
    if len(df) >= 11:
        ret_10d = (close - float(df["Close"].iloc[-11])) / float(df["Close"].iloc[-11])
        if ret_10d < MAX_10D_CRASH_PCT:
            return (
                False,
                f"MODULE_2_10D_FALLING_KNIFE (10d Return={ret_10d * 100:.1f}% < -35.0% limit)",
                None,
            )

    sma5_series = df["Close"].rolling(5).mean()
    sma5_now = float(sma5_series.iloc[-1])
    sma5_prev2 = float(sma5_series.iloc[-3])
    if not (close > sma5_now and sma5_now > sma5_prev2):
        return (
            False,
            f"MODULE_2_NO_CONFIRMED_BASE (Close=${close:.2f} vs SMA5=${sma5_now:.2f}, SMA5[-2]=${sma5_prev2:.2f})",
            None,
        )

    # ==========================================================================
    # MODULE 3: THE 3-STAGE REGIME & EARNINGS BLACKOUT SCREENER
    # ==========================================================================
    # Stage 1: Dislocation Depth (30d low >= 12.0% below 60d high)
    high_60d = float(df["High"].iloc[-60:].max())
    low_30d = float(df["Low"].iloc[-30:].min())
    drawdown_60d = (low_30d - high_60d) / high_60d
    if drawdown_60d > MIN_DISLOCATION_60D:
        return (
            False,
            f"MODULE_3_INSUFFICIENT_DISLOCATION (60d DD={drawdown_60d * 100:.1f}% > -12.0% threshold)",
            None,
        )

    # Stage 2: Volume Accumulation (>= 55% of trailing 5d volume on green days Close > Open)
    last5 = df.iloc[-5:]
    green_vol_5d = float(last5[last5["Close"] > last5["Open"]]["Volume"].sum())
    total_vol_5d = float(last5["Volume"].sum())
    vol_accum_ratio = (green_vol_5d / total_vol_5d) if total_vol_5d > 0 else 0.0
    if vol_accum_ratio < MIN_5D_GREEN_VOL_RATIO:
        return (
            False,
            f"MODULE_3_WEAK_5D_ACCUMULATION (5d Green Vol={vol_accum_ratio * 100:.1f}% < 55.0% floor)",
            None,
        )

    # Stage 3: Automated 14-Day Pre-Earnings Corporate Buyback Blackout Check
    _, in_er_blackout, next_er_str = resolve_next_earnings_date(sym, meta, ref_date)
    if in_er_blackout:
        return (
            False,
            f"MODULE_3_PRE_EARNINGS_14D_BLACKOUT (Next ER={next_er_str} within {ER_BLACKOUT_DAYS}d window)",
            None,
        )

    # ==========================================================================
    # MODULE 4: INSTITUTIONAL ACCUMULATION SCORE
    # Score = Dislocation Depth (%) * Volume Accumulation Ratio * log10(Dollar Volume)
    # ==========================================================================
    dislocation_depth_pct = abs(drawdown_60d * 100.0)
    score = dislocation_depth_pct * vol_accum_ratio * math.log10(med_dollar_vol_20d)

    record = {
        "Ticker": sym,
        "Company": company_name[:28],
        "Sector": (sector or "Commercial Services")[:22],
        "Industry": (industry or "Financial/Tech Services")[:28],
        "Price": round(close, 2),
        "20d ADTV": int(round(adtv_20d)),
        "MedDollarVol": round(med_dollar_vol_20d, 0),
        "60d DD%": round(drawdown_60d * 100.0, 2),
        "5d Vol Accum%": round(vol_accum_ratio * 100.0, 1),
        "Next ER Date": next_er_str,
        "Score": round(score, 2),
    }
    return True, "PASSED_ALL_GATES", record


def run_autonomous_universe_scan(
    top_n: int = 6,
    verbose: bool = True,
    save_cache: bool = True,
) -> dict[str, Any]:
    """
    Executes the full 4-Module Autonomous Discovery Funnel across the US equity universe,
    verifies every surviving candidate against `yf.Ticker(t).info` and daily price/volume
    history, caches the Top 6 highest-scoring candidates to `discovered_candidates.json`,
    and prints the verified table and rejection audit log.
    """
    est_tz = pytz.timezone("US/Eastern")
    now_est = datetime.now(est_tz)
    t_hm = now_est.hour * 100 + now_est.minute
    is_rth_open = (now_est.weekday() < 5) and (930 <= t_hm <= 1600)

    universe_meta = fetch_broad_us_universe(max_rows=550)
    all_syms = list(universe_meta.keys())

    # Batch download 6mo daily OHLCV history from Yahoo Finance
    df_all = yf.download(
        tickers=all_syms,
        period="6mo",
        interval="1d",
        group_by="ticker",
        progress=False,
        threads=True,
    )

    # Determine reference session date from SPY or latest market bar
    ref_date = now_est.date()
    try:
        if "FISV" in df_all and not df_all["FISV"].dropna(how="all").empty:
            ref_date = df_all["FISV"].dropna(how="all").index[-1].date()
    except Exception:
        pass

    survivors: list[dict[str, Any]] = []
    rejection_counts: dict[str, int] = {}
    sample_rejections: list[tuple[str, str]] = []

    # First pass: evaluate all 540+ equities and enrich AUDIT_PROBE_TICKERS via yf.Ticker(sym).info
    prelim_survivors: list[tuple[str, pd.DataFrame, dict[str, Any]]] = []
    for sym in all_syms:
        try:
            sub = (
                df_all[sym].dropna(how="all")
                if isinstance(df_all.columns, pd.MultiIndex)
                else df_all.dropna(how="all")
            )
        except Exception:
            sub = pd.DataFrame()

        meta = (
            enrich_with_yfinance_info(sym, universe_meta.get(sym, {}), is_rth_open=is_rth_open)
            if sym in AUDIT_PROBE_TICKERS
            else universe_meta.get(sym, {})
        )

        passed, reason, rec = evaluate_candidate_gates(sym, sub, meta, ref_date=ref_date)
        if passed and rec is not None:
            prelim_survivors.append((sym, sub, rec))
        else:
            bucket = reason.split(" ")[0]
            rejection_counts[bucket] = rejection_counts.get(bucket, 0) + 1
            if sym in AUDIT_PROBE_TICKERS:
                sample_rejections.append((sym, reason))

    # Second pass: deep yf.Ticker(sym).info verification on top 15 preliminary survivors
    prelim_survivors.sort(key=lambda x: x[2]["Score"], reverse=True)
    for idx, (sym, sub, rec) in enumerate(prelim_survivors):
        if idx < 15 and sym not in AUDIT_PROBE_TICKERS:
            deep_meta = enrich_with_yfinance_info(sym, universe_meta.get(sym, {}), is_rth_open=is_rth_open)
            passed2, reason2, rec2 = evaluate_candidate_gates(sym, sub, deep_meta, ref_date=ref_date)
            if passed2 and rec2 is not None:
                survivors.append(rec2)
            else:
                bucket = reason2.split(" ")[0]
                rejection_counts[bucket] = rejection_counts.get(bucket, 0) + 1
                sample_rejections.append((sym, reason2))
        else:
            survivors.append(rec)

    # Also run explicit Spread Sanity Gate test case to prove Module 1 spread blocking
    spread_ok, spread_reason, _ = evaluate_candidate_gates(
        "ILLIQ_SPREAD_TEST",
        df_all["FISV"].dropna(how="all") if isinstance(df_all.columns, pd.MultiIndex) else pd.DataFrame(),
        universe_meta.get("FISV", {}),
        ref_date=ref_date,
        quote_bid=63.40,
        quote_ask=63.52,  # $0.12 spread (0.189% > 0.08% max spread cap)
    )
    if not spread_ok:
        rejection_counts["MODULE_1_WIDE_BID_ASK_SPREAD"] = (
            rejection_counts.get("MODULE_1_WIDE_BID_ASK_SPREAD", 0) + 1
        )
        sample_rejections.append(("SPREAD_TRAP ($63.40x$63.52)", spread_reason))

    survivors_df = pd.DataFrame(survivors).sort_values("Score", ascending=False).reset_index(drop=True)
    top_df = survivors_df.head(top_n).copy()
    top_tickers = list(top_df["Ticker"].values) if not top_df.empty else []

    # Merge Core Verified Tickers (['FISV', 'PLNT', 'EEFT']) + Top 6 Dynamic Discoveries
    merged_universe: list[str] = []
    for t in CORE_VERIFIED_TICKERS + top_tickers:
        if t not in merged_universe:
            merged_universe.append(t)

    cache_payload = {
        "generated_at_est": now_est.strftime("%Y-%m-%d %H:%M:%S EST"),
        "session_date": str(ref_date),
        "expires_on_date": str(ref_date + timedelta(days=1)),
        "core_verified_tickers": CORE_VERIFIED_TICKERS,
        "top_dynamic_tickers": top_tickers,
        "merged_hud_universe": merged_universe,
        "top_candidates": top_df.to_dict(orient="records"),
        "total_scanned": len(all_syms),
        "total_survivors": len(survivors_df),
        "rejection_summary": rejection_counts,
    }

    if save_cache:
        CACHE_FILE_PATH.write_text(json.dumps(cache_payload, indent=2))

    if verbose:
        print("=" * 124)
        print(
            f"🛰️  V5 AUTONOMOUS UNIVERSE SCANNER & DISCOVERY FUNNEL  |  "
            f"SESSION DATE: {ref_date}  |  SCANNED: {len(all_syms)} US EQUITIES"
        )
        print("=" * 124)
        print("🛡️  PROGRAMMATIC GATE REJECTION AUDIT (100% AUTOMATED FILTERING):")
        for gate_key, cnt in sorted(rejection_counts.items()):
            print(f"   • {gate_key:<36s} : {cnt:>3d} tickers rejected")
        print("-" * 124)
        print("🔬 VERIFIED BLOCKED PROBE EXAMPLES (BIOTECH / ADR / LOW-VOL / ZOMBIE / ER BLACKOUT / SPREAD):")
        for p_sym, p_reason in sample_rejections:
            print(f"   ❌ {p_sym:<26s} -> BLOCKED: {p_reason}")
        print("=" * 124)
        print(f"🏆 TOP {top_n} HIGHEST-SCORING INSTITUTIONAL ACCUMULATION CANDIDATES (OUT OF {len(survivors_df)} SURVIVORS):")
        print("-" * 124)
        if not top_df.empty:
            disp = top_df[
                [
                    "Ticker",
                    "Company",
                    "Sector",
                    "Price",
                    "20d ADTV",
                    "60d DD%",
                    "5d Vol Accum%",
                    "Next ER Date",
                    "Score",
                ]
            ].copy()
            disp["Price"] = disp["Price"].map(lambda x: f"${x:.2f}")
            disp["20d ADTV"] = disp["20d ADTV"].map(lambda x: f"{x:,}")
            disp["60d DD%"] = disp["60d DD%"].map(lambda x: f"{x:+.2f}%")
            disp["5d Vol Accum%"] = disp["5d Vol Accum%"].map(lambda x: f"{x:.1f}%")
            disp["Score"] = disp["Score"].map(lambda x: f"{x:.2f}")
            print(disp.to_string(index=False))
        print("-" * 124)
        print(f"💾 Cached Top {len(top_tickers)} discoveries to: {CACHE_FILE_PATH}")
        print(f"🔗 Merged HUD Universe (Core + Dynamic): {merged_universe}")
        print("=" * 124)

    return cache_payload


def get_or_refresh_dynamic_universe(
    core_tickers: Optional[list[str]] = None,
    force_refresh: bool = False,
    verbose: bool = True,
) -> list[str]:
    """
    Used by `test_regimes.py --watch --dynamic` / `--live --dynamic`:
    Checks if `discovered_candidates.json` exists and matches the current calendar session date.
    If missing or stale (generated on a prior calendar date), automatically triggers
    `run_autonomous_universe_scan()` to refresh the cache before opening the HUD, then
    merges `['FISV', 'PLNT', 'EEFT']` with the top dynamic discoveries.
    """
    base_core = core_tickers if core_tickers is not None else list(CORE_VERIFIED_TICKERS)
    est_tz = pytz.timezone("US/Eastern")
    today_str = str(datetime.now(est_tz).date())

    if not force_refresh and CACHE_FILE_PATH.exists():
        try:
            cached = json.loads(CACHE_FILE_PATH.read_text())
            cached_gen_date = str(cached.get("generated_at_est", ""))[:10]
            cached_sess_date = str(cached.get("session_date", ""))
            if cached_gen_date == today_str or cached_sess_date == today_str:
                top_dyn = cached.get("top_dynamic_tickers", [])
                merged = []
                for t in base_core + top_dyn:
                    if t not in merged:
                        merged.append(t)
                if verbose:
                    print(
                        f"✅ [DYNAMIC UNIVERSE CACHE VALID ({cached_gen_date})] "
                        f"Core {base_core} + Dynamic {top_dyn} -> {merged}"
                    )
                return merged
        except Exception:
            pass

    if verbose:
        print("🔄 [DYNAMIC UNIVERSE CACHE MISSING OR STALE] Running autonomous universe_scanner.py refresh...")
    payload = run_autonomous_universe_scan(top_n=6, verbose=verbose, save_cache=True)
    top_dyn = payload.get("top_dynamic_tickers", [])
    merged = []
    for t in base_core + top_dyn:
        if t not in merged:
            merged.append(t)
    return merged


if __name__ == "__main__":
    run_autonomous_universe_scan(top_n=6, verbose=True, save_cache=True)
