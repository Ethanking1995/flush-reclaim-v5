import warnings
warnings.filterwarnings("ignore")

from typing import Tuple
import yfinance as yf
import pandas as pd
import numpy as np

pd.set_option("display.max_rows", 200)
pd.set_option("display.max_columns", 25)
pd.set_option("display.width", 1000)

TARGETS = ["FISV", "PLNT", "EEFT"]
BENCHMARKS = ["QQQ", "SPY", "NVDA", "XLK", "XLF", "IWD"]
ALL_SYMS = TARGETS + BENCHMARKS


def calc_beta(target_ret: pd.Series, bench_ret: pd.Series) -> float:
    valid = pd.concat([target_ret, bench_ret], axis=1).dropna()
    if len(valid) < 3:
        return np.nan
    cov = np.cov(valid.iloc[:, 0], valid.iloc[:, 1], ddof=1)[0, 1]
    var = np.var(valid.iloc[:, 1], ddof=1)
    return float(cov / var) if var > 0 else np.nan


def build_conditional_matrix(ret_df: pd.DataFrame, window_label: str) -> pd.DataFrame:
    rows = []
    for t in TARGETS:
        r_t = ret_df[t]
        r_qqq = ret_df["QQQ"]
        r_spy = ret_df["SPY"]
        r_nvda = ret_df["NVDA"]
        r_xlf = ret_df["XLF"]
        r_iwd = ret_df["IWD"]

        beta_qqq = calc_beta(r_t, r_qqq)
        beta_spy = calc_beta(r_t, r_spy)
        beta_xlf = calc_beta(r_t, r_xlf)
        beta_iwd = calc_beta(r_t, r_iwd)

        up_mask = r_qqq > 0
        dn_mask = r_qqq < 0
        dn_10_mask = r_qqq <= -0.010
        dn_15_mask = r_qqq <= -0.015
        dn_25_mask = r_qqq <= -0.025

        beta_up_qqq = calc_beta(r_t[up_mask], r_qqq[up_mask])
        beta_dn_qqq = calc_beta(r_t[dn_mask], r_qqq[dn_mask])
        beta_dn15_qqq = calc_beta(r_t[dn_15_mask], r_qqq[dn_15_mask])
        beta_dn25_qqq = calc_beta(r_t[dn_25_mask], r_qqq[dn_25_mask])

        # Conditional stats on QQQ <= -1.0% days
        n_dn10 = int(dn_10_mask.sum())
        win_dn10 = float((r_t[dn_10_mask] > 0).mean() * 100.0) if n_dn10 > 0 else np.nan
        avg_ret_dn10 = float(r_t[dn_10_mask].mean() * 100.0) if n_dn10 > 0 else np.nan
        exp_ret_qqq_dn10 = float((beta_qqq * r_qqq[dn_10_mask]).mean() * 100.0) if n_dn10 > 0 else np.nan
        alpha_vs_qqq_dn10 = avg_ret_dn10 - exp_ret_qqq_dn10

        # Factor-confounding check: Alpha vs XLF and IWD on QQQ <= -1.0% days
        avg_xlf_dn10 = float(r_xlf[dn_10_mask].mean() * 100.0) if n_dn10 > 0 else np.nan
        avg_iwd_dn10 = float(r_iwd[dn_10_mask].mean() * 100.0) if n_dn10 > 0 else np.nan
        spread_vs_xlf_dn10 = avg_ret_dn10 - avg_xlf_dn10
        spread_vs_iwd_dn10 = avg_ret_dn10 - avg_iwd_dn10

        # Conditional stats on QQQ < 0 (All Red QQQ Days)
        n_dn = int(dn_mask.sum())
        win_dn = float((r_t[dn_mask] > 0).mean() * 100.0) if n_dn > 0 else np.nan
        avg_ret_dn = float(r_t[dn_mask].mean() * 100.0) if n_dn > 0 else np.nan

        rows.append({
            "Window": window_label,
            "Ticker": t,
            "N_Total": len(r_t.dropna()),
            "Beta_QQQ": round(beta_qqq, 2),
            "Beta_SPY": round(beta_spy, 2),
            "Beta+_QQQ": round(beta_up_qqq, 2),
            "Beta-_QQQ (<0%)": round(beta_dn_qqq, 2),
            "Asym (B-/B)": round(beta_dn_qqq / beta_qqq, 2) if beta_qqq and beta_qqq != 0 else np.nan,
            "Beta-- (<= -1.5%)": round(beta_dn15_qqq, 2) if pd.notna(beta_dn15_qqq) else np.nan,
            "Beta--- (<= -2.5%)": round(beta_dn25_qqq, 2) if pd.notna(beta_dn25_qqq) else np.nan,
            "N (QQQ<0)": n_dn,
            "Green% (QQQ<0)": round(win_dn, 1),
            "N (QQQ<=-1%)": n_dn10,
            "Green% (QQQ<=-1%)": round(win_dn10, 1),
            "AvgRet% (QQQ<=-1%)": round(avg_ret_dn10, 2),
            "ExpRet% (Beta*QQQ)": round(exp_ret_qqq_dn10, 2),
            "Alpha_vs_QQQ%": round(alpha_vs_qqq_dn10, 2),
            "Excess_vs_XLF%": round(spread_vs_xlf_dn10, 2),
            "Excess_vs_IWD%": round(spread_vs_iwd_dn10, 2),
        })
    return pd.DataFrame(rows)


def build_top10_qqq_drawdowns(
    close_df: pd.DataFrame,
    low_df: pd.DataFrame,
    open_df: pd.DataFrame,
    vol_df: pd.DataFrame,
    ret_df: pd.DataFrame,
    top_n: int = 10,
) -> pd.DataFrame:
    vol_ma20 = vol_df["FISV"].rolling(20, min_periods=5).mean().shift(1)
    fisv_rvol = vol_df["FISV"] / vol_ma20
    fisv_low_to_close = (close_df["FISV"] - low_df["FISV"]) / low_df["FISV"] * 100.0
    fisv_open_to_close = (close_df["FISV"] - open_df["FISV"]) / open_df["FISV"] * 100.0

    worst_dates = ret_df["QQQ"].nsmallest(top_n).index
    rows = []
    for dt in worst_dates:
        rows.append({
            "Date": dt.strftime("%Y-%m-%d"),
            "QQQ_Ret%": round(ret_df.loc[dt, "QQQ"] * 100.0, 2),
            "NVDA_Ret%": round(ret_df.loc[dt, "NVDA"] * 100.0, 2),
            "SPY_Ret%": round(ret_df.loc[dt, "SPY"] * 100.0, 2),
            "XLF_Ret%": round(ret_df.loc[dt, "XLF"] * 100.0, 2),
            "IWD_Ret%": round(ret_df.loc[dt, "IWD"] * 100.0, 2),
            "FISV_Ret%": round(ret_df.loc[dt, "FISV"] * 100.0, 2),
            "FISV_Intraday(O->C)%": round(fisv_open_to_close.loc[dt], 2),
            "FISV_Vol/20dAvg": f"{fisv_rvol.loc[dt]:.2f}x",
            "FISV_Low->Close_Bounce%": round(fisv_low_to_close.loc[dt], 2),
            "PLNT_Ret%": round(ret_df.loc[dt, "PLNT"] * 100.0, 2),
            "EEFT_Ret%": round(ret_df.loc[dt, "EEFT"] * 100.0, 2),
        })
    return pd.DataFrame(rows)


def analyze_rolling_correlations(ret_df_6m: pd.DataFrame) -> pd.DataFrame:
    roll20 = pd.DataFrame(index=ret_df_6m.index)
    for b in ["QQQ", "NVDA", "SPY", "XLK", "XLF", "IWD"]:
        roll20[f"Corr(FISV,{b})"] = ret_df_6m["FISV"].rolling(20).corr(ret_df_6m[b])

    # Summarize by month from June 2026 to Nov 2026
    roll20["Month"] = roll20.index.strftime("%Y-%m")
    monthly_summary = roll20.groupby("Month").mean().dropna(how="all").round(3)
    return roll20, monthly_summary


def analyze_intraday_5m_divergence() -> Tuple[pd.DataFrame, pd.DataFrame]:
    data_5m = {}
    for sym in ["FISV", "PLNT", "EEFT", "QQQ", "SPY", "NVDA", "XLF", "IWD"]:
        df = yf.Ticker(sym).history(period="60d", interval="5m")
        df["Date"] = df.index.date
        df["TimeStr"] = df.index.strftime("%H:%M")
        data_5m[sym] = df

    qqq_5m = data_5m["QQQ"]
    fisv_5m = data_5m["FISV"]

    # Compute daily intraday Open -> Close and High -> Low on QQQ across the 60 trading days
    day_stats = []
    common_dates = sorted(list(set(qqq_5m["Date"].unique()).intersection(set(fisv_5m["Date"].unique()))))

    for dt in common_dates:
        q_day = qqq_5m[qqq_5m["Date"] == dt].copy()
        f_day = fisv_5m[fisv_5m["Date"] == dt].copy()
        s_day = data_5m["SPY"][data_5m["SPY"]["Date"] == dt].copy()
        n_day = data_5m["NVDA"][data_5m["NVDA"]["Date"] == dt].copy()
        x_day = data_5m["XLF"][data_5m["XLF"]["Date"] == dt].copy()
        if len(q_day) < 40 or len(f_day) < 40:
            continue

        q_open, q_close, q_high, q_low = q_day["Open"].iloc[0], q_day["Close"].iloc[-1], q_day["High"].max(), q_day["Low"].min()
        q_intraday_ret = (q_close - q_open) / q_open * 100.0
        q_mhigh_to_low = (q_low - q_day["High"].iloc[:12].max()) / q_day["High"].iloc[:12].max() * 100.0

        f_open, f_close = f_day["Open"].iloc[0], f_day["Close"].iloc[-1]
        f_intraday_ret = (f_close - f_open) / f_open * 100.0

        # Align 5m returns
        merged = pd.DataFrame({
            "F_Close": f_day["Close"],
            "F_Low": f_day["Low"],
            "F_Vol": f_day["Volume"],
            "Q_Close": q_day["Close"],
            "Q_Low": q_day["Low"],
            "S_Close": s_day["Close"] if not s_day.empty else np.nan,
            "N_Close": n_day["Close"] if not n_day.empty else np.nan,
            "X_Close": x_day["Close"] if not x_day.empty else np.nan,
        }).dropna(subset=["F_Close", "Q_Close"])

        merged["F_Ret5m"] = merged["F_Close"].pct_change()
        merged["Q_Ret5m"] = merged["Q_Close"].pct_change()
        merged["TimeHM"] = merged.index.hour * 100 + merged.index.minute

        corr_full = merged["F_Ret5m"].corr(merged["Q_Ret5m"])
        morn = merged[(merged["TimeHM"] >= 1000) & (merged["TimeHM"] <= 1130)]
        aft = merged[(merged["TimeHM"] >= 1300) & (merged["TimeHM"] <= 1540)]

        corr_morn = morn["F_Ret5m"].corr(morn["Q_Ret5m"]) if len(morn) > 3 else np.nan
        corr_aft = aft["F_Ret5m"].corr(aft["Q_Ret5m"]) if len(aft) > 3 else np.nan

        f_morn_ret = (morn["F_Close"].iloc[-1] - morn["F_Close"].iloc[0]) / morn["F_Close"].iloc[0] * 100.0 if len(morn) > 1 else np.nan
        q_morn_ret = (morn["Q_Close"].iloc[-1] - morn["Q_Close"].iloc[0]) / morn["Q_Close"].iloc[0] * 100.0 if len(morn) > 1 else np.nan

        f_aft_ret = (aft["F_Close"].iloc[-1] - aft["F_Close"].iloc[0]) / aft["F_Close"].iloc[0] * 100.0 if len(aft) > 1 else np.nan
        q_aft_ret = (aft["Q_Close"].iloc[-1] - aft["Q_Close"].iloc[0]) / aft["Q_Close"].iloc[0] * 100.0 if len(aft) > 1 else np.nan
        x_aft_ret = (aft["X_Close"].iloc[-1] - aft["X_Close"].iloc[0]) / aft["X_Close"].iloc[0] * 100.0 if len(aft) > 1 and pd.notna(aft["X_Close"].iloc[0]) else np.nan

        # Did FISV set its morning low BEFORE QQQ set its day low (Morning Absorption Decouple)?
        f_low_time = merged["F_Low"].idxmin()
        q_low_time = merged["Q_Low"].idxmin()
        decoupled_early = (f_low_time < q_low_time) and (f_low_time.hour * 100 + f_low_time.minute <= 1200)

        day_stats.append({
            "Date": str(dt),
            "QQQ_O->C%": round(q_intraday_ret, 2),
            "QQQ_Drop%": round(q_mhigh_to_low, 2),
            "FISV_O->C%": round(f_intraday_ret, 2),
            "5mCorr_Day": round(corr_full, 2),
            "5mCorr_10:00-11:30": round(corr_morn, 2),
            "QQQ_10:00-11:30%": round(q_morn_ret, 2),
            "FISV_10:00-11:30%": round(f_morn_ret, 2),
            "QQQ_13:00-15:40%": round(q_aft_ret, 2),
            "FISV_13:00-15:40%": round(f_aft_ret, 2),
            "XLF_13:00-15:40%": round(x_aft_ret, 2),
            "FISV_LowTime": f_low_time.strftime("%H:%M"),
            "QQQ_LowTime": q_low_time.strftime("%H:%M"),
            "Morn_Decouple": "YES" if decoupled_early else "NO",
        })

    df_days = pd.DataFrame(day_stats)
    top5_trend_down = df_days.nsmallest(10, "QQQ_O->C%")
    return df_days, top5_trend_down


if __name__ == "__main__":
    print("Fetching 2-Year Daily and 60-Day 5-Minute Data for TARGETS & BENCHMARKS...")
    hist_2y = {}
    for sym in ALL_SYMS:
        df = yf.Ticker(sym).history(period="2y", interval="1d")
        df[["Open", "High", "Low", "Close", "Volume"]] = df[["Open", "High", "Low", "Close", "Volume"]].ffill()
        df.index = df.index.tz_localize(None).normalize()
        hist_2y[sym] = df

    close_2y = pd.DataFrame({s: hist_2y[s]["Close"] for s in ALL_SYMS}).dropna()
    open_2y = pd.DataFrame({s: hist_2y[s]["Open"] for s in ALL_SYMS}).reindex(close_2y.index)
    low_2y = pd.DataFrame({s: hist_2y[s]["Low"] for s in ALL_SYMS}).reindex(close_2y.index)
    vol_2y = pd.DataFrame({s: hist_2y[s]["Volume"] for s in ALL_SYMS}).reindex(close_2y.index)
    ret_2y = close_2y.pct_change().dropna()

    # 6-Month Slice (May 2026 - Nov 2026) & Post-Crash Accumulation Slice (Aug 15, 2026 - Nov 9, 2026)
    ret_6m = ret_2y.loc["2026-05-09":]
    ret_post_aug = ret_2y.loc["2026-08-15":]
    ret_oct_nov = ret_2y.loc["2026-10-01":]

    print("\n" + "=" * 140)
    print("1. CONDITIONAL RETURN & DOWNSIDE DISSOCIATION MATRIX (6M, POST-AUG ACCUM, OCT-NOV AI ROTATION, & 2Y)")
    print("=" * 140)
    mat_all = pd.concat([
        build_conditional_matrix(ret_6m, "Last 6M (May-Nov 26)"),
        build_conditional_matrix(ret_post_aug, "Last 60d (Aug 15-Nov 9)"),
        build_conditional_matrix(ret_oct_nov, "Oct-Nov 2026 (Recent)"),
        build_conditional_matrix(ret_2y, "Full 2Y (Nov 24-Nov 26)"),
    ], ignore_index=True)
    print(mat_all.to_string(index=False))

    print("\n" + "=" * 140)
    print("2A. TOP 10 WORST QQQ SELLOFF DAYS — LAST 6 MONTHS (MAY 2026 – NOV 2026)")
    print("=" * 140)
    t10_6m = build_top10_qqq_drawdowns(
        close_2y.loc[ret_6m.index], low_2y.loc[ret_6m.index], open_2y.loc[ret_6m.index], vol_2y.loc[ret_6m.index], ret_6m, top_n=10
    )
    print(t10_6m.to_string(index=False))

    print("\n" + "=" * 140)
    print("2B. TOP 10 WORST QQQ SELLOFF DAYS — FULL 2-YEAR HISTORY (NOV 2024 – NOV 2026)")
    print("=" * 140)
    t10_2y = build_top10_qqq_drawdowns(
        close_2y.loc[ret_2y.index], low_2y.loc[ret_2y.index], open_2y.loc[ret_2y.index], vol_2y.loc[ret_2y.index], ret_2y, top_n=10
    )
    print(t10_2y.to_string(index=False))

    print("\n" + "=" * 140)
    print("3. ROLLING 20-DAY CORRELATION REGIME SHIFT (MONTHLY AVERAGES & RECENT WEEKLY TRAJECTORY)")
    print("=" * 140)
    roll20, monthly_corr = analyze_rolling_correlations(ret_2y.loc["2026-04-01":])
    print("--- Monthly Average of 20-Day Rolling Daily Return Correlation with FISV ---")
    print(monthly_corr.to_string())
    print("\n--- Last 15 Trading Days: 20-Day Rolling Correlation Trajectory ---")
    print(roll20.drop(columns=["Month"]).tail(15).round(3).to_string())

    print("\n" + "=" * 140)
    print("4. INTRADAY ABSORPTION DIVERGENCE (5-MINUTE TAPE PROBE ON WORST QQQ INTRADAY TREND-DOWN DAYS)")
    print("=" * 140)
    df_intraday_all, top_trend_down = analyze_intraday_5m_divergence()
    print(top_trend_down.to_string(index=False))

    q_dn10_intraday = df_intraday_all[df_intraday_all["QQQ_O->C%"] <= -1.0]
    q_red_intraday = df_intraday_all[df_intraday_all["QQQ_O->C%"] < 0.0]
    print("\n--- Afternoon Rebalancing Flow Test (13:00 -> 15:40 EST) ---")
    print(f"Days with QQQ Intraday <= -1.0% (N={len(q_dn10_intraday)}):")
    print(f"  Avg QQQ 13:00-15:40 Return  : {q_dn10_intraday['QQQ_13:00-15:40%'].mean():+.2f}%")
    print(f"  Avg XLF 13:00-15:40 Return  : {q_dn10_intraday['XLF_13:00-15:40%'].mean():+.2f}%")
    print(f"  Avg FISV 13:00-15:40 Return : {q_dn10_intraday['FISV_13:00-15:40%'].mean():+.2f}% (Green Afternoon Rate: {(q_dn10_intraday['FISV_13:00-15:40%'] > 0).mean()*100:.1f}%)")
    print(f"  Morning Decouple Rate       : {(q_dn10_intraday['Morn_Decouple'] == 'YES').mean()*100:.1f}% (FISV bottoms before 12:00 PM while QQQ makes lower afternoon lows)")

    print(f"\nDays with QQQ Intraday < 0.0% (All Red Tech Days, N={len(q_red_intraday)}):")
    print(f"  Avg QQQ 13:00-15:40 Return  : {q_red_intraday['QQQ_13:00-15:40%'].mean():+.2f}%")
    print(f"  Avg FISV 13:00-15:40 Return : {q_red_intraday['FISV_13:00-15:40%'].mean():+.2f}% (Green Afternoon Rate: {(q_red_intraday['FISV_13:00-15:40%'] > 0).mean()*100:.1f}%)")
