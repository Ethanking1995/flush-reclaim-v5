from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytz
import yfinance as yf

import test_regimes as legacy_tr

REPO_ROOT = Path(__file__).resolve().parent
BASELINE_DIR = REPO_ROOT / "baseline_data"
RAW_DIR = BASELINE_DIR / "raw_ohlcv"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def freeze_baseline() -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    est_tz = pytz.timezone("US/Eastern")
    freeze_ts = datetime.now(est_tz).strftime("%Y-%m-%d %H:%M:%S %Z")

    tickers_to_save = ["SPY", "FISV", "PLNT", "NU", "CNC", "EEFT", "GPN", "VIAV", "CHTR", "MGM", "MOS", "RDDT"]
    files_manifest = {}

    data_5m = {}
    data_1d = {}

    for sym in tickers_to_save:
        df_1d = yf.Ticker(sym).history(period="1y", interval="1d")
        df_5m = yf.Ticker(sym).history(period="60d", interval="5m")
        data_1d[sym] = df_1d
        data_5m[sym] = df_5m

        p_1d = RAW_DIR / f"{sym}_1d.csv.gz"
        p_5m = RAW_DIR / f"{sym}_5m.csv.gz"
        df_1d.to_csv(p_1d, compression="gzip")
        df_5m.to_csv(p_5m, compression="gzip")

        files_manifest[f"{sym}_1d.csv.gz"] = {
            "symbol": sym,
            "interval": "1d",
            "rows": int(len(df_1d)),
            "start_ts": str(df_1d.index[0]) if not df_1d.empty else None,
            "end_ts": str(df_1d.index[-1]) if not df_1d.empty else None,
            "timezone": str(df_1d.index.tz) if not df_1d.empty else None,
            "sha256": sha256_file(p_1d),
        }
        files_manifest[f"{sym}_5m.csv.gz"] = {
            "symbol": sym,
            "interval": "5m",
            "rows": int(len(df_5m)),
            "start_ts": str(df_5m.index[0]) if not df_5m.empty else None,
            "end_ts": str(df_5m.index[-1]) if not df_5m.empty else None,
            "timezone": str(df_5m.index.tz) if not df_5m.empty else None,
            "sha256": sha256_file(p_5m),
        }

    spy_1d = data_1d["SPY"]
    spy_5m = data_5m["SPY"]
    spy_w_map, _ = legacy_tr.build_spy_intraday_map(spy_5m)

    # Capture legacy V1-V5 benchmark metrics and trade records on Core-3 and Expanded-6
    legacy_metrics = []
    for t in legacy_tr.core_tickers:
        v1 = legacy_tr.run_old_5m_sim(t, data_5m[t], data_1d[t], mode="ex_ante_accum")
        v2 = legacy_tr.run_v2_5m_sim(t, data_5m[t], data_1d[t], mode="ex_ante_accum")
        v3 = legacy_tr.run_v3_audit_tuned(t, data_5m[t], data_1d[t], spy_1d, mode="ex_ante_accum")
        v4 = legacy_tr.run_v4_production(t, data_5m[t], data_1d[t], spy_1d, mode="ex_ante_accum")
        v5 = legacy_tr.run_v5_production(
            t, data_5m[t], data_1d[t], spy_1d, spy_5m=spy_5m, spy_waterfall_map=spy_w_map, mode="ex_ante_accum"
        )
        legacy_metrics.append(legacy_tr.get_metrics(v1, f"{t} [V1 - Old Baseline]"))
        legacy_metrics.append(legacy_tr.get_metrics(v2, f"{t} [V2 - 4 Fixes (Pre-Audit)]"))
        legacy_metrics.append(legacy_tr.get_metrics(v3, f"{t} [V3 - Audit-Tuned]"))
        legacy_metrics.append(legacy_tr.get_metrics(v4, f"{t} [V4 - Prod + EMA12 Runner]"))
        legacy_metrics.append(legacy_tr.get_metrics(v5, f"{t} [V5 - Final (NoPreER+SPYGate)]"))

    for mode_label, mode_key in [
        ("Accum Regime", "ex_ante_accum"),
        ("No Tailwind (Dist)", "non_accum"),
        ("All 60d (Unfiltered)", "all"),
    ]:
        p1 = pd.concat([legacy_tr.run_old_5m_sim(t, data_5m[t], data_1d[t], mode=mode_key) for t in legacy_tr.core_tickers])
        p2 = pd.concat([legacy_tr.run_v2_5m_sim(t, data_5m[t], data_1d[t], mode=mode_key) for t in legacy_tr.core_tickers])
        p3 = pd.concat(
            [legacy_tr.run_v3_audit_tuned(t, data_5m[t], data_1d[t], spy_1d, mode=mode_key) for t in legacy_tr.core_tickers]
        )
        p4 = pd.concat(
            [legacy_tr.run_v4_production(t, data_5m[t], data_1d[t], spy_1d, mode=mode_key) for t in legacy_tr.core_tickers]
        )
        p5 = pd.concat(
            [
                legacy_tr.run_v5_production(
                    t, data_5m[t], data_1d[t], spy_1d, spy_5m=spy_5m, spy_waterfall_map=spy_w_map, mode=mode_key
                )
                for t in legacy_tr.core_tickers
            ]
        )
        legacy_metrics.append(legacy_tr.get_metrics(p1, f"CORE-3 POOL [V1 Old - {mode_label}]"))
        legacy_metrics.append(legacy_tr.get_metrics(p2, f"CORE-3 POOL [V2 Pre-Audit - {mode_label}]"))
        legacy_metrics.append(legacy_tr.get_metrics(p3, f"CORE-3 POOL [V3 Audit-Tuned - {mode_label}]"))
        legacy_metrics.append(legacy_tr.get_metrics(p4, f"CORE-3 POOL [V4 Prod Runner - {mode_label}]"))
        legacy_metrics.append(legacy_tr.get_metrics(p5, f"CORE-3 POOL [V5 Final Engine - {mode_label}]"))

    for t in legacy_tr.all_tickers:
        v5_t = legacy_tr.run_v5_production(
            t, data_5m[t], data_1d[t], spy_1d, spy_5m=spy_5m, spy_waterfall_map=spy_w_map, mode="ex_ante_accum"
        )
        legacy_metrics.append(legacy_tr.get_metrics(v5_t, f"{t} [Expanded-6 V5 - Accum]"))

    v5_dfs = [
        legacy_tr.run_v5_production(
            t, data_5m[t], data_1d[t], spy_1d, spy_5m=spy_5m, spy_waterfall_map=spy_w_map, mode="ex_ante_accum"
        )
        for t in legacy_tr.all_tickers
    ]
    v5_accum = pd.concat([d for d in v5_dfs if not d.empty], ignore_index=True)
    legacy_metrics.append(legacy_tr.get_metrics(v5_accum, "EXPANDED-6 POOL [V5 Final Engine - Accum]"))

    # Serialize legacy trade records
    legacy_trades = []
    for _, row in v5_accum.sort_values(["Ticker", "Date"]).iterrows():
        rec = {}
        for k, v in row.items():
            if pd.isna(v):
                rec[k] = None
            elif isinstance(v, (pd.Timestamp, datetime)):
                rec[k] = str(v)
            elif hasattr(v, "isoformat"):
                rec[k] = v.isoformat()
            elif isinstance(v, (np.floating, float)):
                rec[k] = round(float(v), 6)
            elif isinstance(v, (np.integer, int)):
                rec[k] = int(v)
            else:
                rec[k] = str(v)
        legacy_trades.append(rec)

    # Clean NaN/Inf in legacy_metrics for strict JSON serialization
    cleaned_metrics = []
    for m in legacy_metrics:
        cm = {}
        for k, v in m.items():
            if isinstance(v, (float, np.floating)):
                if np.isnan(v):
                    cm[k] = None
                elif np.isinf(v):
                    cm[k] = "INF"
                else:
                    cm[k] = float(v)
            elif isinstance(v, (int, np.integer)):
                cm[k] = int(v)
            else:
                cm[k] = v
        cleaned_metrics.append(cm)

    manifest_payload = {
        "review_baseline_commit": "8bd8ad6ea73faf62e2a70b0c9ae3f7194f7e90cd",
        "frozen_at_est": freeze_ts,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "data_provider": "Yahoo Finance (yfinance==1.2.0) + TradingView Scanner API",
        "exchange_timezone": "America/New_York",
        "regular_session_hours_et": "09:30-16:00",
        "early_close_session_hours_et": "09:30-13:00",
        "entry_window_et": "09:50-13:00",
        "mandatory_flatten_et": "15:40 (regular) / 12:40 (early_close)",
        "universes": {
            "core_tickers": legacy_tr.core_tickers,
            "expanded_6_tickers": legacy_tr.all_tickers,
            "hud_tickers": legacy_tr.hud_tickers,
            "dynamic_snapshot_tickers": ["VIAV", "CHTR", "MGM", "MOS", "FISV", "RDDT"],
        },
        "legacy_earnings_dates": legacy_tr.EARNINGS_DATES,
        "files": files_manifest,
    }

    (BASELINE_DIR / "manifest.json").write_text(json.dumps(manifest_payload, indent=2))
    (BASELINE_DIR / "legacy_v5_benchmark.json").write_text(
        json.dumps(
            {
                "baseline_commit": "8bd8ad6ea73faf62e2a70b0c9ae3f7194f7e90cd",
                "metrics": cleaned_metrics,
                "expanded_6_v5_accum_trades": legacy_trades,
            },
            indent=2,
        )
    )
    print(f"✅ Frozen {len(files_manifest)} raw OHLCV files and legacy V5 benchmark ({len(legacy_trades)} trades).")


if __name__ == "__main__":
    freeze_baseline()
