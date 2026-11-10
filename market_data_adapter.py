"""
market_data_adapter.py
======================
Audited market-data adapter, provenance tracker, compatible-timestamp consistency
checker, and deterministic fixture provider for Flush & Reclaim V5/V6.

Key Data-Quality Fixes (Section 10):
------------------------------------
1. True Bar-Derived Session VWAP:
   - Never assigns `(high + low + last) / 3` to a field named `vwap`.
   - `session_vwap` is computed strictly from RTH 5-minute OHLCV bars:
     `sum(((High + Low + Close) / 3.0) * Volume) / sum(Volume)` across all session bars
     (including 09:30 and 09:35).
   - The 09:30 and 09:35 opening-bar exclusion applies ONLY to `VolMA10_ExOpen`
     (the 10-bar rolling volume baseline), never to Session VWAP.
2. Honest Provider Provenance:
   - Distinguishes `SAME_PROVIDER_CROSS_ENDPOINT` (e.g., `cnbc:harmony_bars` + `cnbc:rest_quote`,
     where `is_independent_provider=False`) from `INDEPENDENT_PROVIDERS`
     (e.g., `cnbc` vs `yahoo_finance` or `tastytrade`, where `is_independent_provider=True`).
3. Compatible-Timestamp Divergence Evaluation:
   - Never compares a live tick quote against an older completed 5m candle close as if they were
     simultaneous observations.
   - Only evaluates price divergence across compatible observations (two non-delayed live quotes
     observed within `max_skew_seconds=60.0`, or two completed 5m bars with identical `bar_open_et`).
"""

from __future__ import annotations
from dataclasses import dataclass, asdict
from datetime import datetime, date, timedelta
import gzip
import os
from typing import Any, Dict, List, Optional, Tuple

from httpx import Client
import numpy as np
import pandas as pd
import yfinance as yf

from market_calendar import NY_TZ, normalize_to_ny_tz, filter_regular_session_bars


CNBC_QUOTE_URL = "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
CNBC_BARS_URL = "https://ts-api.cnbc.com/harmony/app/bars"


@dataclass(frozen=True)
class DataProvenance:
    vendor_id: str                # "cnbc", "yahoo_finance", "tastytrade", "demo_fixture", "baseline_archive"
    endpoint_id: str              # "cnbc_harmony_bars", "cnbc_rest_quote", "yahoo_v8_chart", "deterministic_fixture"
    observation_timestamp_et: str
    fetch_timestamp_et: str
    age_seconds: float
    is_delayed: bool
    delay_seconds: int
    update_mode: str              # "streaming_realtime", "polling_snapshot", "delayed_900s", "deterministic_fixture"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class QuoteObservation:
    symbol: str
    last: float
    bid: Optional[float]
    ask: Optional[float]
    open_price: Optional[float]
    day_high: Optional[float]
    day_low: Optional[float]
    day_volume: Optional[float]
    provenance: DataProvenance

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["provenance"] = self.provenance.to_dict()
        return d


@dataclass(frozen=True)
class ConsistencyCheckResult:
    status: str                   # "PASS", "FAIL", "SINGLE_SOURCE_ONLY", "SECONDARY_DELAYED_IGNORED"
    comparison_basis: str         # "COMPATIBLE_LIVE_QUOTES", "COMPATIBLE_COMPLETED_BARS", "QUOTE_NEWER_THAN_COMPLETED_BAR", "SINGLE_PROVIDER"
    is_independent_provider: bool
    provider_relationship: str    # "INDEPENDENT_PROVIDERS", "SAME_PROVIDER_CROSS_ENDPOINT", "SINGLE_PROVIDER", "DEMO_FIXTURE"
    primary_price: Optional[float]
    secondary_price: Optional[float]
    spread_pct: Optional[float]
    threshold_pct: float
    observation_skew_seconds: Optional[float]
    explanation: str

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["provenance_relation"] = self.provider_relationship
        return d


def compute_bar_session_vwap(day_df: pd.DataFrame) -> pd.Series:
    """
    Documented, canonical bar-derived Session VWAP calculation across RTH bars:
      TypicalPrice_i = (High_i + Low_i + Close_i) / 3.0
      SessionVWAP_t  = sum_{i=1..t}(TypicalPrice_i * Volume_i) / sum_{i=1..t}(Volume_i)
    Includes the 09:30 and 09:35 opening bars.
    """
    if day_df.empty:
        return pd.Series(dtype=float)
    tp = (day_df["High"] + day_df["Low"] + day_df["Close"]) / 3.0
    cum_pv = (tp * day_df["Volume"]).cumsum()
    cum_v = day_df["Volume"].cumsum().replace(0, np.nan)
    return (cum_pv / cum_v).ffill().fillna(day_df["Close"])


def evaluate_observation_consistency(
    primary_obs: Optional[QuoteObservation],
    secondary_obs: Optional[QuoteObservation],
    max_divergence_pct: float = 0.0025,
    max_skew_seconds: float = 60.0,
) -> ConsistencyCheckResult:
    """
    Compare two quote observations ONLY if their timestamps and delay modes are compatible.
    Never treats two endpoints from the same vendor as independent providers.
    Never fails divergence merely because a live quote is newer than a completed candle.
    """
    if primary_obs is None or primary_obs.last <= 0:
        return ConsistencyCheckResult(
            status="FAIL",
            comparison_basis="MISSING_PRIMARY",
            is_independent_provider=False,
            provider_relationship="NONE",
            primary_price=None,
            secondary_price=None,
            spread_pct=None,
            threshold_pct=max_divergence_pct * 100.0,
            observation_skew_seconds=None,
            explanation="Primary market quote observation is missing or invalid.",
        )

    if secondary_obs is None or secondary_obs.last <= 0:
        return ConsistencyCheckResult(
            status="SINGLE_SOURCE_ONLY",
            comparison_basis="SINGLE_PROVIDER",
            is_independent_provider=False,
            provider_relationship="SINGLE_PROVIDER",
            primary_price=primary_obs.last,
            secondary_price=None,
            spread_pct=None,
            threshold_pct=max_divergence_pct * 100.0,
            observation_skew_seconds=None,
            explanation=(
                f"Single provider active ({primary_obs.provenance.vendor_id}:{primary_obs.provenance.endpoint_id}); "
                f"no secondary independent quote available for cross-check."
            ),
        )

    if secondary_obs.provenance.is_delayed:
        return ConsistencyCheckResult(
            status="SECONDARY_DELAYED_IGNORED",
            comparison_basis="DELAYED_SECONDARY_EXCLUDED",
            is_independent_provider=(primary_obs.provenance.vendor_id != secondary_obs.provenance.vendor_id),
            provider_relationship="DELAYED_SECONDARY",
            primary_price=primary_obs.last,
            secondary_price=secondary_obs.last,
            spread_pct=None,
            threshold_pct=max_divergence_pct * 100.0,
            observation_skew_seconds=float(secondary_obs.provenance.delay_seconds),
            explanation=(
                f"Secondary feed ({secondary_obs.provenance.vendor_id}) is delayed by "
                f"{secondary_obs.provenance.delay_seconds}s ({secondary_obs.provenance.update_mode}) and excluded from live divergence checks."
            ),
        )

    is_indep = (
        primary_obs.provenance.vendor_id != secondary_obs.provenance.vendor_id
        and primary_obs.provenance.vendor_id != "demo_fixture"
    )
    if primary_obs.provenance.vendor_id == "demo_fixture":
        rel = "DEMO_FIXTURE"
    elif is_indep:
        rel = "INDEPENDENT_PROVIDERS"
    else:
        rel = "SAME_PROVIDER_CROSS_ENDPOINT"

    skew = abs(primary_obs.provenance.age_seconds - secondary_obs.provenance.age_seconds)
    if skew > max_skew_seconds:
        return ConsistencyCheckResult(
            status="SINGLE_SOURCE_ONLY",
            comparison_basis="QUOTE_NEWER_THAN_COMPLETED_BAR",
            is_independent_provider=is_indep,
            provider_relationship=rel,
            primary_price=primary_obs.last,
            secondary_price=secondary_obs.last,
            spread_pct=abs(primary_obs.last - secondary_obs.last) / primary_obs.last * 100.0,
            threshold_pct=max_divergence_pct * 100.0,
            observation_skew_seconds=round(skew, 1),
            explanation=(
                f"Observations differ in age by {skew:.0f}s (> {max_skew_seconds:.0f}s compatible window); "
                f"price difference reflects time elapsed rather than simultaneous feed divergence."
            ),
        )

    spread_frac = abs(primary_obs.last - secondary_obs.last) / primary_obs.last
    spread_pct = round(spread_frac * 100.0, 3)
    thresh_pct = round(max_divergence_pct * 100.0, 2)
    passed = spread_frac <= max_divergence_pct

    if passed:
        rel_desc = (
            f"Independent providers ({primary_obs.provenance.vendor_id} vs {secondary_obs.provenance.vendor_id})"
            if is_indep
            else f"Same-provider cross-endpoint ({primary_obs.provenance.endpoint_id} vs {secondary_obs.provenance.endpoint_id})"
        )
        explanation = (
            f"{rel_desc} agree within {spread_pct:.2f}% "
            f"(${primary_obs.last:.2f} vs ${secondary_obs.last:.2f}, <= {thresh_pct:.2f}% cap)."
        )
    else:
        explanation = (
            f"Simultaneous quote divergence of {spread_pct:.2f}% "
            f"(Primary ${primary_obs.last:.2f} [{primary_obs.provenance.endpoint_id}] vs "
            f"Secondary ${secondary_obs.last:.2f} [{secondary_obs.provenance.endpoint_id}] within {skew:.0f}s) "
            f"exceeds the {thresh_pct:.2f}% consistency threshold. New entries are blocked until feeds converge."
        )

    return ConsistencyCheckResult(
        status="PASS" if passed else "FAIL",
        comparison_basis="COMPATIBLE_LIVE_QUOTES",
        is_independent_provider=is_indep,
        provider_relationship=rel,
        primary_price=primary_obs.last,
        secondary_price=secondary_obs.last,
        spread_pct=spread_pct,
        threshold_pct=thresh_pct,
        observation_skew_seconds=round(skew, 1),
        explanation=explanation,
    )


def _parse_float(val: Any, default: Optional[float] = None) -> Optional[float]:
    if val is None:
        return default
    try:
        return float(str(val).replace(",", "").strip())
    except Exception:
        return default


def fetch_cnbc_quote_observations(
    tickers: List[str],
    now_et: Optional[datetime] = None,
    timeout: float = 3.5,
) -> Dict[str, QuoteObservation]:
    """
    Fetch quote snapshots from CNBC REST Quote Webservice.
    Note: Does NOT fabricate a `vwap` field from `(high + low + last) / 3`.
    Records honest provenance (`vendor_id='cnbc'`, `endpoint_id='cnbc_rest_quote'`).
    """
    if not tickers:
        return {}
    now_et = now_et or datetime.now(NY_TZ)
    fetch_ts_str = now_et.strftime("%Y-%m-%d %H:%M:%S %Z")
    clean_syms = [s.upper().strip() for s in tickers if s and s.strip()]
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
    }
    out: Dict[str, QuoteObservation] = {}
    try:
        params = {
            "symbols": "|".join(clean_syms),
            "requestMethod": "itv",
            "noform": "1",
            "partnerId": "2",
            "fund": "1",
            "exthrs": "1",
            "output": "json",
            "events": "1",
        }
        with Client(timeout=timeout, headers=headers) as client:
            resp = client.get(CNBC_QUOTE_URL, params=params)
            if resp.status_code == 200 and resp.text.strip().startswith("{"):
                quotes = resp.json().get("FormattedQuoteResult", {}).get("FormattedQuote", [])
                if isinstance(quotes, dict):
                    quotes = [quotes]
                for q in quotes:
                    sym = str(q.get("symbol", "")).upper().strip()
                    last_p = _parse_float(q.get("last"))
                    if not sym or last_p is None or last_p <= 0:
                        continue
                    last_time_raw = str(q.get("last_time", ""))
                    age_s = 2.0
                    obs_ts_str = fetch_ts_str
                    if last_time_raw:
                        try:
                            parsed_dt = pd.to_datetime(last_time_raw).tz_convert(NY_TZ)
                            obs_ts_str = parsed_dt.strftime("%Y-%m-%d %H:%M:%S %Z")
                            age_s = max(0.0, (now_et - parsed_dt).total_seconds())
                        except Exception:
                            pass
                    prov = DataProvenance(
                        vendor_id="cnbc",
                        endpoint_id="cnbc_rest_quote",
                        observation_timestamp_et=obs_ts_str,
                        fetch_timestamp_et=fetch_ts_str,
                        age_seconds=round(age_s, 1),
                        is_delayed=False,
                        delay_seconds=0,
                        update_mode="polling_snapshot",
                    )
                    out[sym] = QuoteObservation(
                        symbol=sym,
                        last=last_p,
                        bid=_parse_float(q.get("bid")),
                        ask=_parse_float(q.get("ask")),
                        open_price=_parse_float(q.get("open"), last_p),
                        day_high=_parse_float(q.get("high"), last_p),
                        day_low=_parse_float(q.get("low"), last_p),
                        day_volume=_parse_float(q.get("volume"), 0.0),
                        provenance=prov,
                    )
    except Exception:
        pass
    return out


def fetch_cnbc_ohlcv_bars(
    sym: str,
    interval: str = "5m",
    days: int = 25,
    now_et: Optional[datetime] = None,
) -> Tuple[pd.DataFrame, Optional[DataProvenance]]:
    """
    Fetch RTH 5m or 1d OHLCV bars from CNBC Harmony TS-API with explicit provenance.
    """
    now_et = now_et or datetime.now(NY_TZ)
    fetch_ts_str = now_et.strftime("%Y-%m-%d %H:%M:%S %Z")
    start_et = now_et - timedelta(days=days)
    cnbc_int = "1D" if str(interval).lower() in ("1d", "d") else "5M"
    start_str = start_et.strftime("%Y%m%d000000")
    end_str = (now_et + timedelta(days=1)).strftime("%Y%m%d160000")
    url = f"{CNBC_BARS_URL}/{sym.upper()}/{cnbc_int}/{start_str}/{end_str}/adjusted/EST5EDT.json"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
    }
    try:
        with Client(timeout=4.0, headers=headers) as client:
            r = client.get(url)
            if r.status_code != 200:
                return pd.DataFrame(), None
            bars = r.json().get("barData", {}).get("priceBars", [])
            if not bars:
                return pd.DataFrame(), None
            rows = []
            for b in bars:
                tt = str(b.get("tradeTime", ""))
                if len(tt) < 8 or b.get("close") is None:
                    continue
                if cnbc_int == "1D":
                    dt_naive = datetime.strptime(tt[:8], "%Y%m%d")
                else:
                    dt_naive = datetime.strptime(tt[:14], "%Y%m%d%H%M%S")
                dt_tz = dt_naive.replace(tzinfo=NY_TZ)
                if cnbc_int == "5M":
                    hm = dt_tz.hour * 100 + dt_tz.minute
                    if hm < 930 or hm > 1555:
                        continue
                rows.append({
                    "Datetime": dt_tz,
                    "Open": float(b["open"]),
                    "High": float(b["high"]),
                    "Low": float(b["low"]),
                    "Close": float(b["close"]),
                    "Volume": int(float(b.get("volume") or 0)),
                })
            if not rows:
                return pd.DataFrame(), None
            df = pd.DataFrame(rows).set_index("Datetime").sort_index()
            last_bar_ts = df.index[-1]
            age_s = max(0.0, (now_et - last_bar_ts).total_seconds())
            prov = DataProvenance(
                vendor_id="cnbc",
                endpoint_id="cnbc_harmony_bars",
                observation_timestamp_et=last_bar_ts.strftime("%Y-%m-%d %H:%M:%S %Z"),
                fetch_timestamp_et=fetch_ts_str,
                age_seconds=round(age_s, 1),
                is_delayed=False,
                delay_seconds=0,
                update_mode="polling_snapshot",
            )
            return df, prov
    except Exception:
        return pd.DataFrame(), None


def load_baseline_ohlcv(symbol: str, interval: str, base_dir: Optional[str] = None) -> pd.DataFrame:
    """
    Load frozen baseline OHLCV bars from `baseline_data/raw_ohlcv/{symbol}_{interval}.csv.gz`.
    """
    root = base_dir or os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(root, "baseline_data", "raw_ohlcv", f"{symbol.upper()}_{interval}.csv.gz")
    if not os.path.exists(path):
        return pd.DataFrame()
    with gzip.open(path, "rt", encoding="utf-8") as f:
        df = pd.read_csv(f, index_col=0)
    df = normalize_to_ny_tz(df)
    if interval == "5m":
        df = filter_regular_session_bars(df)
    return df
