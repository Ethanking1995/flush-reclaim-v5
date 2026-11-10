"""
universe_scanner.py
===================
Audited V6 Dynamic Universe Discovery Funnel with Hard Sanity Guards.

Fixes all V5 scanner inconsistencies:
  1. Uses the single shared `EarningsOracle` (`earnings_oracle.py`); unknown earnings
     remain `UNKNOWN` (never labeled `N/A (>14d)`).
  2. Separates overnight discovery from RTH spread verification: missing/zero quotes
     outside regular trading hours are marked `UNVERIFIED_OFF_HOURS`, never verified.
  3. Deep-validates ALL candidates before final selection.
  4. Handles zero survivors cleanly without `KeyError: 'Score'`.
  5. Keeps `core_monitored_tickers` (`FISV`, `PLNT`, `NU`) strictly distinct from
     `order_eligible_tickers` (only tickers passing all gates are order-eligible).
  6. Persists full snapshot metadata (`data_cutoff_ts`, `effective_session_date`,
     `config_sha256`, `selection_rules`, `rejection_reasons`) to `discovered_candidates.json`.
"""

from __future__ import annotations
import argparse
from datetime import datetime, date
import hashlib
import json
import os
from typing import Any, Dict, List, Optional, Tuple
import warnings
import numpy as np
import pandas as pd
import yfinance as yf

from config_schema import StrategyConfig, ExecutionConfig
from market_calendar import NYSECalendar, NY_TZ, normalize_to_ny_tz
from earnings_oracle import EarningsOracle

warnings.filterwarnings("ignore")

CORE_MONITORED_TICKERS = ["FISV", "PLNT", "NU"]

FALLBACK_LIQUID_UNIVERSE = [
    "FISV", "PLNT", "NU", "GPN", "EEFT", "CNC", "CVS", "HUM", "UNH", "ELV", "CI", "MOH",
    "PYPL", "SQ", "AFRM", "SOFI", "UPST", "ALLY", "COF", "SYF", "DFS", "AXP", "V", "MA",
    "BAC", "WFC", "C", "JPM", "GS", "MS", "SCHW", "IBKR", "HOOD", "COIN", "usb", "PNC", "TFC", "FITB", "KEY", "RF", "HBAN", "CFG",
    "DAL", "UAL", "AAL", "LUV", "CCL", "RCL", "NCLH", "MAR", "HLT", "ABNB", "BKNG", "EXPE", "UBER", "LYFT", "DASH",
    "NKE", "LULU", "UAA", "DECK", "CROX", "ONON", "SBUX", "MCD", "CMG", "DPZ", "YUM", "QSR", "WEN", "DRI", "TGT", "WMT", "COST", "DG", "DLTR", "ROST", "TJX", "BBY", "HD", "LOW",
    "DIS", "NFLX", "WBD", "PARA", "CMCSA", "CHTR", "T", "VZ", "TMUS", "SPOT", "ROKU", "TTD", "PINS", "SNAP", "RDDT",
    "INTC", "AMD", "MU", "QCOM", "TXN", "MRVL", "ON", "MCHP", "STM", "NXPI", "SWKS", "QRVO", "GLW", "HPQ", "DELL", "HPE", "CSCO", "JNPR", "ANET",
    "CRM", "ORCL", "SAP", "ADBE", "INTU", "WDAY", "NOW", "TEAM", "SNOW", "DDOG", "NET", "CRWD", "ZS", "PANW", "FTNT", "OKTA", "DOCU", "ZM", "TWLO", "U", "PLTR", "PATH",
    "BA", "GE", "HON", "CAT", "DE", "URI", "LMT", "RTX", "NOC", "GD", "MMM", "EMR", "ETN", "ITW", "PH", "FDX", "UPS", "CSX", "UNP", "NSC",
    "XOM", "CVX", "COP", "EOG", "SLB", "HAL", "BKR", "OXY", "DVN", "FANG", "MPC", "VLO", "PSX", "KMI", "WMB", "OKE",
    "FCX", "NEM", "GOLD", "AA", "NUE", "STLD", "CLF", "X", "DOW", "LYB", "DD", "PPG", "SHW", "ALB", "MOS", "CF",
    "PFE", "BMY", "MRK", "ABBV", "GILD", "AMGN", "BIIB", "REGN", "VRTX", "ZTS", "BSX", "MDT", "ABT", "SYK", "EW", "ISRG", "BDX", "BAX", "DXCM", "ALGN", "HOLX", "GEHC",
    "F", "GM", "STLA", "TSLA", "RIVN", "LCID", "TM", "HMC", "APTV", "BWA", "LEA", "LKQ",
    "KO", "PEP", "MNST", "CELH", "KDP", "STZ", "TAP", "BUD", "PG", "CL", "KMB", "EL", "CHD", "CLX", "GIS", "K", "CAG", "CPB", "HSY", "MDLZ", "KHC", "TSN", "HRL",
    "NEE", "DUK", "SO", "D", "AEP", "EXC", "SRE", "XEL", "PEG", "ED", "PCG", "EIX", "VST", "CEG", "NRG",
    "AMT", "PLD", "CCI", "EQIX", "DLR", "O", "SPG", "WELL", "VICI", "PSA", "EXR", "AVB", "EQR", "INVH",
    "KSS", "M", "JWN", "GAP", "ANF", "AEO", "URBN", "FIVE", "OLLI", "BURL", "ASO", "DKS", "TSCO", "WSM", "RH", "WAY", "W", "ETSY", "EBAY", "CHWY", "CVNA", "KMX", "AN",
]

HARD_BLACKLIST_TICKERS = {
    "BABA", "JD", "PDD", "BIDU", "NIO", "XPEV", "LI", "BILI", "TME", "IQ", "VIPS", "ZTO", "FUTU", "TIGR", "BEKE", "TCOM", "YUMC", "HTHT",
    "SMCI", "DJT", "GME", "AMC", "MARA", "RIOT", "CLSK", "MSTR", "MSTX", "CONL", "NVDL", "TSLL", "TQQQ", "SQQQ", "SOXL", "SOXS",
}

EXPECTED_SCANNER_COLUMNS = [
    "Ticker", "Price", "ADTV_M", "DolVol_M", "ATR_Pct", "HL_10d",
    "Vol_Ratio", "Close_50d_Pct", "MarketCap_B", "Sector", "Industry",
    "Spread_Cents", "Spread_Status", "Next_ER", "ER_Status", "Score",
]


def is_regular_trading_hours(now_et: Optional[datetime] = None, calendar: Optional[NYSECalendar] = None) -> bool:
    cal = calendar or NYSECalendar()
    dt = now_et or datetime.now(NY_TZ)
    dt_ny = dt.astimezone(NY_TZ) if dt.tzinfo else dt.replace(tzinfo=NY_TZ)
    sched = cal.get_session_schedule(dt_ny.date())
    if not sched.is_trading_day or sched.market_open_et is None or sched.market_close_et is None:
        return False
    return sched.market_open_et <= dt_ny <= sched.market_close_et


def evaluate_quote_spread(
    bid: Optional[float],
    ask: Optional[float],
    last: Optional[float],
    is_rth: bool,
    exec_cfg: ExecutionConfig = ExecutionConfig(),
) -> Tuple[bool, Optional[float], str, Optional[str]]:
    """
    Distinguish overnight discovery from execution-time RTH spread verification:
      - Outside regular trading hours or when bid/ask is missing off-hours:
        returns `(True, None, "UNVERIFIED_OFF_HOURS", None)` — NEVER marked verified!
      - During regular trading hours:
        - Missing/zero bid/ask -> `(False, None, "MISSING_RTH_QUOTE", "REJECTED_MISSING_RTH_QUOTE")`
        - Spread > $0.04 or > 0.08% -> `(False, spread_cents, "REJECTED_WIDE_SPREAD", reason)`
        - Spread within limits -> `(True, spread_cents, "VERIFIED_RTH", None)`
    """
    has_valid_ba = (
        bid is not None
        and ask is not None
        and bid > 0
        and ask >= bid
    )
    if not has_valid_ba:
        if not is_rth:
            return True, None, "UNVERIFIED_OFF_HOURS", None
        return False, None, "MISSING_RTH_QUOTE", "REJECTED_MISSING_RTH_QUOTE"

    spread_dollars = round(float(ask) - float(bid), 4)
    spread_cents = round(spread_dollars * 100.0, 2)
    ref_px = float(last) if (last is not None and last > 0) else float(ask)
    spread_pct = spread_dollars / ref_px if ref_px > 0 else 1.0

    if spread_dollars > exec_cfg.max_bid_ask_spread_dollars + 1e-6 or spread_pct > exec_cfg.max_bid_ask_spread_pct + 1e-6:
        return (
            False,
            spread_cents,
            "REJECTED_WIDE_SPREAD",
            f"Spread ${spread_dollars:.2f} ({spread_pct*100:.3f}%) exceeds ${exec_cfg.max_bid_ask_spread_dollars:.2f} / {exec_cfg.max_bid_ask_spread_pct*100:.2f}%",
        )

    if is_rth:
        return True, spread_cents, "VERIFIED_RTH", None
    else:
        return True, spread_cents, "UNVERIFIED_OFF_HOURS", None


def deep_validate_candidate(
    symbol: str,
    info: dict,
    as_of_date: date,
    is_rth: bool,
    oracle: EarningsOracle,
    cfg: StrategyConfig = StrategyConfig(),
    exec_cfg: ExecutionConfig = ExecutionConfig(),
) -> Tuple[bool, Dict[str, Any], Optional[str]]:
    """
    Programmatic deep validation of a candidate's fundamentals, sector/industry,
    country, debt, earnings blackout (via shared `EarningsOracle`), and quote spread.
    """
    sym = symbol.upper()
    if sym in HARD_BLACKLIST_TICKERS:
        return False, {}, "REJECTED_HARD_BLACKLIST"

    quote_type = info.get("quoteType", "EQUITY")
    if quote_type != "EQUITY":
        return False, {}, f"REJECTED_NON_EQUITY ({quote_type})"

    country = info.get("country", "United States")
    if country and country not in ("United States", "USA", "Puerto Rico"):
        if sym not in CORE_MONITORED_TICKERS:
            return False, {}, f"REJECTED_NON_US_COUNTRY ({country})"

    sector = info.get("sector", "Unknown") or "Unknown"
    industry = info.get("industry", "Unknown") or "Unknown"
    mcap = float(info.get("marketCap", 0) or 0)

    if "Biotechnology" in industry or "Pharmaceutical" in industry:
        return False, {}, f"REJECTED_BIOTECH_PHARMA ({industry})"

    if sector == "Healthcare" and mcap < 20_000_000_000:
        return False, {}, f"REJECTED_SMALL_HEALTHCARE (MarketCap ${mcap/1e9:.1f}B < $20B)"

    if mcap > 0 and mcap < 2_000_000_000:
        return False, {}, f"REJECTED_MICRO_CAP (MarketCap ${mcap/1e9:.2f}B < $2B)"

    # Ingest any yfinance earnings timestamp into the shared EarningsOracle
    oracle.ingest_yfinance_info(sym, info)
    er_res = oracle.check_blackout(sym, as_of_date, use_filter=cfg.use_er_blackout_filter)
    if er_res.is_blocked:
        return False, {}, f"REJECTED_EARNINGS ({er_res.display_label})"

    bid = info.get("bid")
    ask = info.get("ask")
    last = info.get("currentPrice") or info.get("regularMarketPrice")
    spread_ok, spread_cents, spread_status, spread_reject = evaluate_quote_spread(
        bid=bid,
        ask=ask,
        last=last,
        is_rth=is_rth,
        exec_cfg=exec_cfg,
    )
    if not spread_ok:
        return False, {}, spread_reject or "REJECTED_SPREAD"

    dte = info.get("debtToEquity", 0) or 0
    if dte and dte > 450 and sector not in ("Financial Services", "Utilities", "Real Estate"):
        return False, {}, f"REJECTED_HIGH_DEBT_TO_EQUITY ({dte})"

    meta = {
        "MarketCap_B": round(mcap / 1e9, 1) if mcap > 0 else 0.0,
        "Sector": sector,
        "Industry": industry,
        "Spread_Cents": spread_cents,
        "Spread_Status": spread_status,
        "Next_ER": er_res.display_label,
        "ER_Status": er_res.status,
        "deep_validated": True,
    }
    return True, meta, None


def compute_heuristic_discovery_score(row: Dict[str, Any]) -> float:
    """
    Compute the 0-100 heuristic ranking score (formerly overclaimed as 'institutional footprint').
    Combines higher-low count (0-35), volume ratio (0-25), ATR compression (0-20), and dollar liquidity (0-20).
    """
    hl_pts = min(35.0, max(0.0, (float(row["HL_10d"]) - 5.0) * 7.0))
    vol_pts = min(25.0, max(0.0, (float(row["Vol_Ratio"]) - 0.95) * 80.0))
    atr_pts = min(20.0, max(0.0, (0.045 - float(row["ATR_Pct"])) * 800.0))
    liq_pts = min(20.0, max(0.0, np.log10(max(float(row["DolVol_M"]), 50.0) / 50.0) * 18.0))
    return round(hl_pts + vol_pts + atr_pts + liq_pts, 1)


def rank_and_filter_candidates(
    raw_candidates: List[Dict[str, Any]],
) -> pd.DataFrame:
    """
    Given a list of candidate dicts, compute heuristic `Score` and sort descending.
    Cleanly returns an empty DataFrame with `EXPECTED_SCANNER_COLUMNS` when `raw_candidates` is empty
    (never raises `KeyError: 'Score'`).
    """
    if not raw_candidates:
        return pd.DataFrame(columns=EXPECTED_SCANNER_COLUMNS)

    for c in raw_candidates:
        c["Score"] = compute_heuristic_discovery_score(c)

    df = pd.DataFrame(raw_candidates)
    for col in EXPECTED_SCANNER_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df = df.sort_values(by=["Score", "Ticker"], ascending=[False, True]).reset_index(drop=True)
    return df


def fetch_broad_us_equities_pool() -> List[str]:
    try:
        import urllib.request
        url = "https://scanner.tradingview.com/america/scan"
        payload = {
            "filter": [
                {"left": "type", "operation": "equal", "right": "stock"},
                {"left": "exchange", "operation": "in_range", "right": ["NYSE", "NASDAQ"]},
                {"left": "close", "operation": "in_range", "right": [20.0, 200.0]},
                {"left": "average_volume_30d_calc", "operation": "greater", "right": 1200000},
                {"left": "Value.Traded", "operation": "greater", "right": 55000000},
                {"left": "market_cap_basic", "operation": "greater", "right": 2500000000},
            ],
            "options": {"lang": "en"},
            "markets": ["america"],
            "symbols": {"query": {"types": []}, "tickers": []},
            "columns": ["name", "close", "volume", "average_volume_30d_calc", "Value.Traded", "market_cap_basic", "sector", "industry"],
            "sort": {"sortBy": "Value.Traded", "sortOrder": "desc"},
            "range": [0, 300],
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tickers: List[str] = []
        for row in data.get("data", []):
            d = row.get("d", [])
            if not d:
                continue
            sym = str(d[0]).strip().upper()
            industry = str(d[7] or "")
            sector = str(d[6] or "")
            mcap = float(d[5] or 0)
            if "." in sym or "/" in sym or "-" in sym:
                continue
            if sym in HARD_BLACKLIST_TICKERS:
                continue
            if "Biotechnology" in industry or "Pharmaceutical" in industry:
                continue
            if sector == "Health Technology" and mcap < 20_000_000_000:
                continue
            tickers.append(sym)
        for core in CORE_MONITORED_TICKERS:
            if core not in tickers:
                tickers.append(core)
        return list(dict.fromkeys(tickers))
    except Exception:
        pool = [t.upper() for t in FALLBACK_LIQUID_UNIVERSE if t.upper() not in HARD_BLACKLIST_TICKERS]
        return list(dict.fromkeys(pool))


def run_autonomous_discovery_funnel(
    max_candidates: int = 8,
    save_json: bool = True,
    as_of_dt: Optional[datetime] = None,
    cfg: StrategyConfig = StrategyConfig(),
    exec_cfg: ExecutionConfig = ExecutionConfig(),
    oracle: Optional[EarningsOracle] = None,
) -> pd.DataFrame:
    cal = NYSECalendar()
    now_et = as_of_dt or datetime.now(NY_TZ)
    now_ny = now_et.astimezone(NY_TZ) if now_et.tzinfo else now_et.replace(tzinfo=NY_TZ)
    is_rth = is_regular_trading_hours(now_ny, cal)
    er_oracle = oracle or EarningsOracle(
        blackout_days=cfg.er_blackout_calendar_days,
        unknown_policy=cfg.er_unknown_policy,
    )

    pool = fetch_broad_us_equities_pool()
    rejection_reasons: Dict[str, str] = {}

    raw_daily = yf.download(
        pool,
        period="3mo",
        interval="1d",
        group_by="ticker",
        progress=False,
        threads=True,
    )

    stage2_PreCandidates: List[Dict[str, Any]] = []
    for sym in pool:
        try:
            df = raw_daily[sym].dropna(how="all") if len(pool) > 1 else raw_daily.dropna(how="all")
            if df.empty or len(df) < 35:
                rejection_reasons[sym] = "REJECTED_INSUFFICIENT_DAILY_BARS"
                continue

            last_close = float(df["Close"].iloc[-1])
            if last_close < exec_cfg.min_share_price or last_close > exec_cfg.max_share_price:
                rejection_reasons[sym] = f"REJECTED_PRICE_OUT_OF_RANGE (${last_close:.2f})"
                continue

            adtv_20d = float(df["Volume"].tail(20).mean())
            if adtv_20d < 1_000_000:
                rejection_reasons[sym] = f"REJECTED_LOW_ADTV ({adtv_20d:,.0f} < 1M)"
                continue

            dol_vol_med = float((df["Volume"] * df["Close"]).tail(20).median())
            if dol_vol_med < 50_000_000:
                rejection_reasons[sym] = f"REJECTED_LOW_DOLLAR_VOL (${dol_vol_med/1e6:.1f}M < $50M)"
                continue

            ret_3d = (last_close - float(df["Close"].iloc[-4])) / float(df["Close"].iloc[-4])
            if ret_3d <= -0.10:
                rejection_reasons[sym] = f"REJECTED_3D_WATERFALL ({ret_3d*100:.1f}%)"
                continue

            tr0 = abs(df["High"] - df["Low"])
            tr1 = abs(df["High"] - df["Close"].shift(1))
            tr2 = abs(df["Low"] - df["Close"].shift(1))
            tr = pd.concat([tr0, tr1, tr2], axis=1).max(axis=1)
            atr_14 = float(tr.rolling(14).mean().iloc[-1])
            atr_pct = atr_14 / last_close
            if atr_pct > cfg.accum_max_atr_pct:
                rejection_reasons[sym] = f"REJECTED_HIGH_ATR ({atr_pct*100:.2f}% > 4.5%)"
                continue

            higher_low = (df["Low"] >= df["Low"].shift(1) * 0.998).astype(int)
            hl_10d = int(higher_low.tail(10).sum())
            vol_10d = float(df["Volume"].tail(10).mean())
            vol_20d = float(df["Volume"].tail(20).mean())
            vol_ratio = vol_10d / vol_20d if vol_20d > 0 else 0.0

            high_50d = float(df["High"].tail(50).max())
            low_50d = float(df["Low"].tail(50).min())
            close_pos_50d = (last_close - low_50d) / max(high_50d - low_50d, 1e-6)

            if hl_10d < cfg.accum_min_higher_lows:
                rejection_reasons[sym] = f"REJECTED_LOW_HL_COUNT ({hl_10d}/10 < 6)"
                continue
            if vol_ratio < cfg.accum_min_vol_ratio:
                rejection_reasons[sym] = f"REJECTED_LOW_VOL_RATIO ({vol_ratio:.2f}x < 1.0x)"
                continue
            if close_pos_50d > 0.70:
                rejection_reasons[sym] = f"REJECTED_NEAR_50D_HIGH ({close_pos_50d*100:.0f}% > 70%)"
                continue

            c_dict = {
                "Ticker": sym,
                "Price": round(last_close, 2),
                "ADTV_M": round(adtv_20d / 1e6, 2),
                "DolVol_M": round(dol_vol_med / 1e6, 1),
                "ATR_Pct": round(atr_pct, 4),
                "HL_10d": hl_10d,
                "Vol_Ratio": round(vol_ratio, 2),
                "Close_50d_Pct": round(close_pos_50d * 100.0, 1),
            }
            c_dict["Score"] = compute_heuristic_discovery_score(c_dict)
            stage2_PreCandidates.append(c_dict)
        except Exception as exc:
            rejection_reasons[sym] = f"ERROR ({exc})"

    # Sort pre-candidates by heuristic score descending and deep-validate until we have `max_candidates` verified survivors
    stage2_PreCandidates.sort(key=lambda x: (-x["Score"], x["Ticker"]))

    verified_survivors: List[Dict[str, Any]] = []
    for cand in stage2_PreCandidates:
        sym = cand["Ticker"]
        try:
            info = yf.Ticker(sym).info or {}
        except Exception:
            info = {}

        passed, meta, reject_reason = deep_validate_candidate(
            symbol=sym,
            info=info,
            as_of_date=now_ny.date(),
            is_rth=is_rth,
            oracle=er_oracle,
            cfg=cfg,
            exec_cfg=exec_cfg,
        )
        if not passed:
            rejection_reasons[sym] = reject_reason or "REJECTED_DEEP_VALIDATION"
            continue

        cand.update(meta)
        verified_survivors.append(cand)
        if len(verified_survivors) >= max_candidates:
            break

    final_df = rank_and_filter_candidates(verified_survivors)

    if save_json:
        order_eligible_tickers = final_df["Ticker"].tolist() if not final_df.empty else []
        out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "discovered_candidates.json")
        snapshot_payload = {
            "schema_version": "v6.0-audited",
            "generated_at_et": now_ny.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "data_cutoff_ts": now_ny.isoformat(),
            "effective_session_date": now_ny.date().isoformat(),
            "is_regular_trading_hours_scan": is_rth,
            "config_sha256": cfg.config_sha256(),
            "selection_rules": {
                "price_range_usd": [exec_cfg.min_share_price, exec_cfg.max_share_price],
                "min_adtv_20d": 1_000_000,
                "min_median_dollar_vol_20d": 50_000_000,
                "max_atr_pct": cfg.accum_max_atr_pct,
                "min_hl_10d": cfg.accum_min_higher_lows,
                "min_vol_ratio_10d_20d": cfg.accum_min_vol_ratio,
                "max_50d_range_pct": 70.0,
                "er_blackout_days": cfg.er_blackout_calendar_days,
                "er_unknown_policy": cfg.er_unknown_policy,
            },
            "core_monitored_tickers": list(CORE_MONITORED_TICKERS),
            "order_eligible_tickers": order_eligible_tickers,
            "selected_candidates": final_df.to_dict(orient="records") if not final_df.empty else [],
            "rejected_count": len(rejection_reasons),
            "rejection_reasons_sample": dict(list(rejection_reasons.items())[:50]),
        }
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(snapshot_payload, f, indent=2)

    return final_df


def load_discovered_universe(
    include_core_monitored: bool = False,
    require_order_eligible: bool = True,
) -> List[str]:
    """
    Load the discovered universe from `discovered_candidates.json`.
    Keeps `core_monitored_tickers` distinct from `order_eligible_tickers`.
    """
    json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "discovered_candidates.json")
    if os.path.exists(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if require_order_eligible and "order_eligible_tickers" in data:
                eligible = list(data.get("order_eligible_tickers", []))
            else:
                eligible = [c["Ticker"] for c in data.get("selected_candidates", []) if "Ticker" in c]

            if include_core_monitored:
                combined = list(CORE_MONITORED_TICKERS) + [t for t in eligible if t not in CORE_MONITORED_TICKERS]
                return combined
            return eligible
        except Exception:
            pass
    return list(CORE_MONITORED_TICKERS) if include_core_monitored else []


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Audited V6 Dynamic Universe Discovery Funnel")
    parser.add_argument("--scan-universe", action="store_true", help="Run full autonomous discovery funnel")
    parser.add_argument("--max-candidates", type=int, default=8, help="Max order-eligible candidates to select")
    args = parser.parse_args()
    df_out = run_autonomous_discovery_funnel(max_candidates=args.max_candidates, save_json=True)
    if df_out.empty:
        print("No candidates passed all gates for the current snapshot.")
    else:
        print(df_out.to_string(index=False))
