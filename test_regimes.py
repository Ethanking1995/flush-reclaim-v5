import os
import sys
import time
from datetime import datetime
import pytz
import yfinance as yf
import pandas as pd
import numpy as np

pd.set_option('future.no_silent_downcasting', True)
pd.set_option('display.max_rows', 250)
pd.set_option('display.max_columns', 20)
pd.set_option('display.width', 1000)

core_tickers = ['FISV', 'PLNT', 'NU']
all_tickers = ['FISV', 'PLNT', 'NU', 'CNC', 'EEFT', 'GPN']
hud_tickers = ['FISV', 'PLNT', 'EEFT', 'CNC', 'GPN', 'NU']

# Known Q3/Q4 2026 & Q1 2027 Corporate Earnings Dates (14-Day Pre-ER Rule 10b-18 Blackout Filter)
EARNINGS_DATES = {
    'FISV': ['2026-11-03', '2027-02-04'],
    'PLNT': ['2026-11-05', '2027-02-23'],
    'NU':   ['2026-08-13', '2026-11-12', '2027-02-18'],
    'CNC':  ['2026-10-27', '2027-02-02'],
    'EEFT': ['2026-10-27', '2027-02-11'],
    'GPN':  ['2026-11-05', '2027-02-10'],
}


def check_er_blackout(ticker, dt, blackout_days=14, extra_er_dates=None):
    """Returns (is_blackout, er_date_str) if dt is within [ER_Date - blackout_days, ER_Date]."""
    dt_ts = pd.Timestamp(dt)
    dates = list(EARNINGS_DATES.get(ticker, []))
    if extra_er_dates:
        for d_str in extra_er_dates:
            if d_str not in dates:
                dates.append(d_str)
    for er_str in dates:
        er_ts = pd.Timestamp(er_str)
        diff_days = (er_ts - dt_ts).days
        if 0 <= diff_days <= blackout_days:
            return True, er_str
    return False, None


def build_spy_intraday_map(spy_5m):
    """Precomputes SPY intraday VWAP, MorningHigh, drawdown, and active waterfall crash gate per 5m bar."""
    if spy_5m is None or spy_5m.empty:
        return {}, None
    s = spy_5m.copy()
    s['Date'] = s.index.date
    waterfall_map = {}
    last_spy_state = None
    for dt, sdf in s.groupby('Date'):
        sdf = sdf.copy()
        tp = (sdf['High'] + sdf['Low'] + sdf['Close']) / 3.0
        sdf['VWAP'] = (tp * sdf['Volume']).cumsum() / sdf['Volume'].cumsum()
        sdf['MorningHigh'] = sdf['High'].cummax()
        sdf['DropFromHigh'] = (sdf['Low'] - sdf['MorningHigh']) / sdf['MorningHigh']
        sdf['CloseDropFromHigh'] = (sdf['Close'] - sdf['MorningHigh']) / sdf['MorningHigh']
        sdf['BelowVWAP'] = (sdf['Close'] - sdf['VWAP']) / sdf['VWAP']
        sdf['RedBar'] = sdf['Close'] < sdf['Open']
        sdf['Ret3'] = sdf['Close'].pct_change(3, fill_method=None).fillna(0.0)
        for ts, row in sdf.iterrows():
            # Active SPY waterfall crash: down <= -1.00% from SPY MorningHigh AND <= -0.35% below SPY VWAP
            # and either current 5m bar is red, 3-bar momentum is negative, or SPY is in a deep <= -2.0% crash
            is_waterfall = (
                row['DropFromHigh'] <= -0.0100
                and row['BelowVWAP'] <= -0.0035
                and (bool(row['RedBar']) or row['Ret3'] < 0 or row['DropFromHigh'] <= -0.0200)
            )
            waterfall_map[ts] = is_waterfall
        last_row = sdf.iloc[-1]
        last_spy_state = {
            'Date': dt,
            'Last': float(last_row['Close']),
            'VWAP': float(last_row['VWAP']),
            'MorningHigh': float(last_row['MorningHigh']),
            'DrawdownPct': float((last_row['Close'] - last_row['MorningHigh']) / last_row['MorningHigh'] * 100.0),
            'MaxIntradayDropPct': float((sdf['Low'].min() - last_row['MorningHigh']) / last_row['MorningHigh'] * 100.0),
            'VsVWAPPct': float(last_row['BelowVWAP'] * 100.0),
            'RedBar': bool(last_row['RedBar']),
            'IsWaterfall': bool(waterfall_map[sdf.index[-1]]),
        }
    return waterfall_map, last_spy_state


# ==============================================================================
# 1. INSTITUTIONAL ACCUMULATION DETECTOR (Incorporating Review Agent Tells #1-#3)
# ==============================================================================
def get_accum_map(df_1d, spy_df=None, use_v3_tells=False):
    d = df_1d.copy()
    d[['Open', 'High', 'Low', 'Close', 'Volume']] = d[['Open', 'High', 'Low', 'Close', 'Volume']].ffill()
    d['Date'] = d.index.date
    d['High60'] = d['High'].rolling(60, min_periods=20).max()
    d['Drawdown60'] = (d['Low'].rolling(35, min_periods=5).min() - d['High60']) / d['High60']
    d['Dislocated'] = d['Drawdown60'] <= -0.12

    d['SMA5'] = d['Close'].rolling(5).mean()
    d['SMA5_Rising'] = d['SMA5'] > d['SMA5'].shift(2)
    d['GreenDay'] = d['Close'] > d['Open']
    d['UpVol5'] = (d['Volume'] * d['GreenDay']).rolling(5).sum()
    d['TotVol5'] = d['Volume'].rolling(5).sum()
    d['VolAccum'] = (d['UpVol5'] / d['TotVol5']) >= 0.55

    base_accum = d['Dislocated'] & (d['Close'] > d['SMA5']) & d['SMA5_Rising'] & d['VolAccum']

    if use_v3_tells and spy_df is not None:
        s = spy_df.copy()
        s[['Close']] = s[['Close']].ffill()
        s['Date'] = s.index.date
        s['SPY_Ret1'] = s['Close'].pct_change(fill_method=None)
        s['SPY_Ret5'] = s['Close'].pct_change(5, fill_method=None)
        d['SPY_Ret1'] = d['Date'].map(dict(zip(s['Date'], s['SPY_Ret1'])))
        d['SPY_Ret5'] = d['Date'].map(dict(zip(s['Date'], s['SPY_Ret5'])))

        d['Ret1'] = d['Close'].pct_change(fill_method=None)
        d['Ret5'] = d['Close'].pct_change(5, fill_method=None)
        # Tell #2: Green on heavy volume during market bloodbath OR strong 5d relative strength + afternoon ramp (Tell #3: CLV)
        rng = (d['High'] - d['Low']).replace(0, np.nan)
        d['CLV'] = ((d['Close'] - d['Low']) / rng).fillna(0.5)
        d['CLV3'] = d['CLV'].rolling(3, min_periods=2).mean()
        d['BloodbathDivergence'] = ((d['Ret1'] > 0) & (d['SPY_Ret1'] <= -0.010)).rolling(5, min_periods=1).max() > 0
        d['AfternoonRampAccum'] = (
            d['Dislocated']
            & (d['Close'] > d['SMA5'])
            & d['SMA5_Rising']
            & (d['Ret5'] > d['SPY_Ret5'] + 0.02)
            & (d['CLV3'] >= 0.60)
            & d['BloodbathDivergence']
        )
        base_accum = base_accum | d['AfternoonRampAccum']

    d['ExAnteAccum'] = base_accum.shift(1).fillna(False).astype(bool)
    return dict(zip(d['Date'], d['ExAnteAccum'])), bool(base_accum.iloc[-1]) if len(base_accum) > 0 else False


# ==============================================================================
# 2. V1 (OLD BASELINE) & V2 (PRE-AUDIT 4 FIXES) FOR COMPARISON
# ==============================================================================
def run_old_5m_sim(ticker, df_5m, df_1d, mode='ex_ante_accum'):
    df = df_5m.copy()
    df['Date'] = df.index.date
    accum_map, _ = get_accum_map(df_1d, use_v3_tells=False)

    trades = []
    for dt, day_df in df.groupby('Date'):
        if len(day_df) < 30:
            continue
        if mode == 'ex_ante_accum' and not accum_map.get(dt, False):
            continue
        if mode == 'non_accum' and accum_map.get(dt, False):
            continue

        day_df = day_df.copy()
        typ_price = (day_df['High'] + day_df['Low'] + day_df['Close']) / 3.0
        day_df['VWAP'] = (typ_price * day_df['Volume']).cumsum() / day_df['Volume'].cumsum()
        day_df['VolMA10'] = day_df['Volume'].rolling(10, min_periods=1).mean()
        day_df['MorningHigh'] = day_df['High'].cummax()

        in_flush, flush_low, flush_vol_spike, flush_time = False, np.inf, False, None
        entry_price, entry_time = None, None

        bars = list(day_df.iterrows())
        for i in range(1, len(bars)):
            ts, row = bars[i]
            prev_ts, prev_row = bars[i - 1]
            t_hm = ts.hour * 100 + ts.minute
            is_last_bar = (i == len(bars) - 1)

            if entry_price is None:
                if 935 <= t_hm <= 1300 and not is_last_bar:
                    drop_from_high = (row['Low'] - row['MorningHigh']) / row['MorningHigh']
                    below_vwap = (row['Low'] - row['VWAP']) / row['VWAP']
                    if drop_from_high <= -0.0065 and below_vwap <= -0.002:
                        in_flush = True
                        if row['Low'] < flush_low:
                            flush_low = row['Low']
                            flush_time = ts
                        if row['Volume'] >= 1.2 * row['VolMA10']:
                            flush_vol_spike = True
                    if in_flush and flush_vol_spike and ts > flush_time:
                        reclaim_bounce = (row['Close'] - flush_low) / flush_low
                        if row['Close'] > row['Open'] and row['Close'] > prev_row['Close'] and reclaim_bounce >= 0.002:
                            entry_price, entry_time = row['Close'], ts
            else:
                ret_high = (row['High'] - entry_price) / entry_price * 100
                ret_close = (row['Close'] - entry_price) / entry_price * 100
                if ret_high >= 0.70:
                    trades.append({'Ticker': ticker, 'Date': dt, 'EntryTime': entry_time, 'ExitTime': ts, 'EntryPrice': entry_price, 'ExitPrice': entry_price * 1.0070, 'RetPct': 0.70, 'Reason': 'Target (+0.70%)'})
                    break
                if t_hm >= 1530 and not is_last_bar and row['Close'] > entry_price:
                    trades.append({'Ticker': ticker, 'Date': dt, 'EntryTime': entry_time, 'ExitTime': ts, 'EntryPrice': entry_price, 'ExitPrice': row['Close'], 'RetPct': ret_close, 'Reason': '3:30PM Buyback Window'})
                    break
                if is_last_bar:
                    trades.append({'Ticker': ticker, 'Date': dt, 'EntryTime': entry_time, 'ExitTime': ts, 'EntryPrice': entry_price, 'ExitPrice': row['Close'], 'RetPct': ret_close, 'Reason': 'EOD Market Flatten'})
                    break
    return pd.DataFrame(trades)


def run_v2_5m_sim(ticker, df_5m, df_1d, mode='ex_ante_accum'):
    df = df_5m.copy()
    df['Date'] = df.index.date
    accum_map, _ = get_accum_map(df_1d, use_v3_tells=False)

    trades = []
    for dt, day_df in df.groupby('Date'):
        if len(day_df) < 30:
            continue
        if mode == 'ex_ante_accum' and not accum_map.get(dt, False):
            continue
        if mode == 'non_accum' and accum_map.get(dt, False):
            continue

        day_df = day_df.copy()
        typ_price = (day_df['High'] + day_df['Low'] + day_df['Close']) / 3.0
        day_df['VWAP'] = (typ_price * day_df['Volume']).cumsum() / day_df['Volume'].cumsum()
        day_df['VolMA10'] = day_df['Volume'].rolling(10, min_periods=1).mean()
        day_df['MorningHigh'] = day_df['High'].cummax()

        in_flush, flush_low, flush_vol_climax, flush_time = False, np.inf, False, None
        entry_price, entry_time, stop_price, scaled_vwap, leg1_ret = None, None, None, False, 0.0

        bars = list(day_df.iterrows())
        for i in range(1, len(bars)):
            ts, row = bars[i]
            prev_ts, prev_row = bars[i - 1]
            t_hm = ts.hour * 100 + ts.minute
            is_last_bar = (i == len(bars) - 1)

            if entry_price is None:
                if 950 <= t_hm <= 1300 and not is_last_bar:
                    drop_from_high = (row['Low'] - row['MorningHigh']) / row['MorningHigh']
                    below_vwap = (row['Low'] - row['VWAP']) / row['VWAP']
                    if in_flush and row['Close'] >= row['VWAP']:
                        in_flush, flush_low, flush_vol_climax, flush_time = False, np.inf, False, None
                    if drop_from_high <= -0.011 and below_vwap <= -0.0035:
                        if not in_flush or row['Low'] < flush_low:
                            in_flush, flush_low, flush_time = True, row['Low'], ts
                        if row['Volume'] >= 1.8 * row['VolMA10']:
                            flush_vol_climax = True
                    elif in_flush and row['Low'] < flush_low:
                        flush_low, flush_time = row['Low'], ts
                        if row['Volume'] >= 1.8 * row['VolMA10']:
                            flush_vol_climax = True
                    if in_flush and flush_vol_climax and ts > flush_time:
                        reclaim_bounce = (row['Close'] - flush_low) / flush_low
                        if row['Low'] > flush_low and row['Close'] > row['Open'] and row['Close'] > prev_row['Close'] and 0.0015 <= reclaim_bounce <= 0.0065 and row['Close'] < row['VWAP']:
                            entry_price, entry_time, stop_price = row['Close'], ts, flush_low * (1.0 - 0.0045)
            else:
                if row['Low'] <= stop_price:
                    stop_ret = (stop_price - entry_price) / entry_price * 100
                    net_ret = (0.5 * leg1_ret + 0.5 * stop_ret - 0.03) if scaled_vwap else (stop_ret - 0.03)
                    reason = '50% VWAP + 50% BE Stop' if scaled_vwap else 'Hard Stop (-0.45% < FlushLow)'
                    exit_p = entry_price * (1 + (0.5 * leg1_ret + 0.5 * stop_ret) / 100) if scaled_vwap else stop_price
                    trades.append({'Ticker': ticker, 'Date': dt, 'EntryTime': entry_time, 'ExitTime': ts, 'EntryPrice': entry_price, 'ExitPrice': exit_p, 'RetPct': net_ret, 'Reason': reason})
                    break
                if not scaled_vwap and row['High'] >= row['VWAP'] and row['VWAP'] > entry_price:
                    scaled_vwap = True
                    vwap_fill = max(entry_price, min(row['High'], row['VWAP']))
                    leg1_ret = (vwap_fill - entry_price) / entry_price * 100
                    stop_price = max(stop_price, entry_price)
                if (row['High'] - entry_price) / entry_price * 100 >= 0.85:
                    if not scaled_vwap:
                        vwap_fill = max(entry_price, min(entry_price * 1.0085, row['VWAP']))
                        leg1_ret = (vwap_fill - entry_price) / entry_price * 100
                    net_ret = 0.5 * leg1_ret + 0.5 * 0.85 - 0.03
                    exit_p = entry_price * (1 + (0.5 * leg1_ret + 0.5 * 0.85) / 100)
                    trades.append({'Ticker': ticker, 'Date': dt, 'EntryTime': entry_time, 'ExitTime': ts, 'EntryPrice': entry_price, 'ExitPrice': exit_p, 'RetPct': net_ret, 'Reason': '50% VWAP + 50% Target(+0.85%)'})
                    break
                if t_hm >= 1540 or is_last_bar:
                    ret_close = (row['Close'] - entry_price) / entry_price * 100
                    net_ret = (0.5 * leg1_ret + 0.5 * ret_close - 0.03) if scaled_vwap else (ret_close - 0.03)
                    reason = '50% VWAP + 50% 3:40PM Flatten' if scaled_vwap else '3:40PM Pre-10b18 Flatten'
                    exit_p = entry_price * (1 + (0.5 * leg1_ret + 0.5 * ret_close) / 100) if scaled_vwap else row['Close']
                    trades.append({'Ticker': ticker, 'Date': dt, 'EntryTime': entry_time, 'ExitTime': ts, 'EntryPrice': entry_price, 'ExitPrice': exit_p, 'RetPct': net_ret, 'Reason': reason})
                    break
    return pd.DataFrame(trades)


# ==============================================================================
# 3. V3 (AUDIT-TUNED ENGINE: FIXES BUG A, BUG B, AND REAL-WORLD ACCUM TELLS)
# ==============================================================================
def run_v3_audit_tuned(ticker, df_5m, df_1d, spy_df, mode='ex_ante_accum'):
    df = df_5m.copy()
    df['Date'] = df.index.date
    accum_map, _ = get_accum_map(df_1d, spy_df=spy_df, use_v3_tells=True)

    midday_bars = df[(df.index.hour * 100 + df.index.minute >= 1030) & (df.index.hour * 100 + df.index.minute <= 1430)]
    midday_med = midday_bars.groupby('Date')['Volume'].median().rolling(15, min_periods=1).median().shift(1).bfill()
    midday_vol_map = midday_med.to_dict()

    trades = []
    for dt, day_df in df.groupby('Date'):
        if len(day_df) < 30:
            continue
        if mode == 'ex_ante_accum' and not accum_map.get(dt, False):
            continue
        if mode == 'non_accum' and accum_map.get(dt, False):
            continue

        day_df = day_df.copy()
        typ_price = (day_df['High'] + day_df['Low'] + day_df['Close']) / 3.0
        day_df['CumPV'] = (typ_price * day_df['Volume']).cumsum()
        day_df['CumV'] = day_df['Volume'].cumsum()
        day_df['VWAP'] = day_df['CumPV'] / day_df['CumV']
        day_df['VolMA10'] = day_df['Volume'].rolling(10, min_periods=1).mean()
        day_df['MorningHigh'] = day_df['High'].cummax()
        open_bar_high = day_df['High'].iloc[0]
        ref_midday_vol = midday_vol_map.get(dt, day_df['Volume'].median())

        in_flush, flush_low, flush_vol_climax, flush_time = False, np.inf, False, None
        entry_price, entry_time, stop_price, scaled_leg1, leg1_ret = None, None, None, False, 0.0

        bars = list(day_df.iterrows())
        for i in range(1, len(bars)):
            ts, row = bars[i]
            prev_ts, prev_row = bars[i - 1]
            t_hm = ts.hour * 100 + ts.minute
            is_last_bar = (i == len(bars) - 1)

            if entry_price is None:
                if 950 <= t_hm <= 1300 and not is_last_bar:
                    drop_from_high = (row['Low'] - row['MorningHigh']) / row['MorningHigh']
                    below_vwap = (row['Low'] - row['VWAP']) / row['VWAP']
                    if in_flush and row['Close'] >= row['VWAP']:
                        in_flush, flush_low, flush_vol_climax, flush_time = False, np.inf, False, None

                    post_open_rally = row['MorningHigh'] > open_bar_high * 1.001
                    is_vol_climax = (row['Volume'] >= 1.8 * row['VolMA10']) or (
                        post_open_rally
                        and 1000 <= t_hm <= 1025
                        and drop_from_high <= -0.0150
                        and below_vwap <= -0.0070
                        and row['Volume'] >= 1.5 * ref_midday_vol
                    )

                    if drop_from_high <= -0.011 and below_vwap <= -0.0035:
                        if not in_flush or row['Low'] < flush_low:
                            in_flush, flush_low, flush_time = True, row['Low'], ts
                        if is_vol_climax:
                            flush_vol_climax = True
                    elif in_flush and row['Low'] < flush_low:
                        flush_low, flush_time = row['Low'], ts
                        if is_vol_climax:
                            flush_vol_climax = True

                    if in_flush and flush_vol_climax:
                        reclaim_bounce = (row['Close'] - flush_low) / flush_low
                        max_bounce = 0.0120 if post_open_rally else 0.0065
                        close_below_vwap = (row['Close'] - row['VWAP']) / row['VWAP']
                        is_hl_reclaim = (
                            ts > flush_time
                            and row['Low'] > flush_low
                            and row['Close'] > row['Open']
                            and row['Close'] > prev_row['Close']
                            and 0.0015 <= reclaim_bounce <= max_bounce
                            and close_below_vwap <= -0.0010
                        )
                        bar_rng = row['High'] - row['Low']
                        is_hammer_reclaim = (
                            post_open_rally
                            and ts == flush_time
                            and below_vwap <= -0.0065
                            and row['Close'] > row['Open']
                            and bar_rng > 0
                            and ((row['Close'] - row['Low']) / bar_rng) >= 0.75
                            and 0.0040 <= reclaim_bounce <= 0.0120
                            and row['Close'] < row['VWAP']
                        )
                        if is_hl_reclaim or is_hammer_reclaim:
                            entry_price, entry_time, stop_price = row['Close'], ts, flush_low * (1.0 - 0.0045)
            else:
                if row['Low'] <= stop_price:
                    stop_ret = (stop_price - entry_price) / entry_price * 100
                    if scaled_leg1:
                        net_ret = 0.5 * leg1_ret + 0.5 * stop_ret - 0.03
                        reason = '50% Leg1(>=0.4%) + 50% BE Stop'
                        exit_p = entry_price * (1 + (0.5 * leg1_ret + 0.5 * stop_ret) / 100)
                    else:
                        net_ret = stop_ret - 0.03
                        reason = 'Hard Stop (-0.45% < FlushLow)'
                        exit_p = stop_price
                    trades.append({'Ticker': ticker, 'Date': dt, 'EntryTime': entry_time, 'ExitTime': ts, 'EntryPrice': entry_price, 'ExitPrice': exit_p, 'RetPct': net_ret, 'Reason': reason})
                    break

                leg1_target = max(row['VWAP'], entry_price * 1.0040)
                if not scaled_leg1 and row['High'] >= leg1_target:
                    scaled_leg1 = True
                    leg1_fill = min(row['High'], leg1_target) if row['Open'] <= leg1_target else row['Open']
                    leg1_ret = (leg1_fill - entry_price) / entry_price * 100
                    stop_price = max(stop_price, entry_price)

                ret_high = (row['High'] - entry_price) / entry_price * 100
                if ret_high >= 0.85:
                    if not scaled_leg1:
                        leg1_fill = max(entry_price * 1.0040, min(entry_price * 1.0085, row['VWAP']))
                        leg1_ret = (leg1_fill - entry_price) / entry_price * 100
                    leg2_ret = 0.85
                    net_ret = 0.5 * leg1_ret + 0.5 * leg2_ret - 0.03
                    exit_p = entry_price * (1 + (0.5 * leg1_ret + 0.5 * leg2_ret) / 100)
                    trades.append({'Ticker': ticker, 'Date': dt, 'EntryTime': entry_time, 'ExitTime': ts, 'EntryPrice': entry_price, 'ExitPrice': exit_p, 'RetPct': net_ret, 'Reason': '50% Leg1(>=0.4%) + 50% Target(+0.85%)'})
                    break

                if t_hm >= 1540 or is_last_bar:
                    ret_close = (row['Close'] - entry_price) / entry_price * 100
                    if scaled_leg1:
                        net_ret = 0.5 * leg1_ret + 0.5 * ret_close - 0.03
                        reason = '50% Leg1(>=0.4%) + 50% 3:40PM Flatten'
                        exit_p = entry_price * (1 + (0.5 * leg1_ret + 0.5 * ret_close) / 100)
                    else:
                        net_ret = ret_close - 0.03
                        reason = '3:40PM Pre-10b18 Flatten'
                        exit_p = row['Close']
                    trades.append({'Ticker': ticker, 'Date': dt, 'EntryTime': entry_time, 'ExitTime': ts, 'EntryPrice': entry_price, 'ExitPrice': exit_p, 'RetPct': net_ret, 'Reason': reason})
                    break
    return pd.DataFrame(trades)


# ==============================================================================
# 4. V4 (PRODUCTION NON-OVERFITTED + AFTERNOON RUNNER)
# ==============================================================================
def run_v4_production(ticker, df_5m, df_1d, spy_df, mode='ex_ante_accum'):
    df = df_5m.copy()
    df['Date'] = df.index.date
    df['TimeStr'] = df.index.strftime('%H:%M')
    accum_map, _ = get_accum_map(df_1d, spy_df=spy_df, use_v3_tells=True)

    df['ToD_MedVol'] = df.groupby('TimeStr')['Volume'].transform(
        lambda s: s.rolling(15, min_periods=1).median().shift(1).bfill()
    )
    midday_bars = df[(df.index.hour * 100 + df.index.minute >= 1030) & (df.index.hour * 100 + df.index.minute <= 1430)]
    midday_med = midday_bars.groupby('Date')['Volume'].median().rolling(15, min_periods=1).median().shift(1).bfill()
    midday_vol_map = midday_med.to_dict()

    trades = []
    for dt, day_df in df.groupby('Date'):
        if len(day_df) < 30:
            continue
        if mode == 'ex_ante_accum' and not accum_map.get(dt, False):
            continue
        if mode == 'non_accum' and accum_map.get(dt, False):
            continue

        day_df = day_df.copy()
        typ_price = (day_df['High'] + day_df['Low'] + day_df['Close']) / 3.0
        day_df['CumPV'] = (typ_price * day_df['Volume']).cumsum()
        day_df['CumV'] = day_df['Volume'].cumsum()
        day_df['VWAP'] = day_df['CumPV'] / day_df['CumV']

        day_df['VolMA10'] = day_df['Volume'].rolling(10, min_periods=1).mean()
        day_df['MorningHigh'] = day_df['High'].cummax()
        day_df['EMA12'] = day_df['Close'].ewm(span=12, adjust=False).mean()
        day_df['EMA20'] = day_df['Close'].ewm(span=20, adjust=False).mean()
        open_bar_high = day_df['High'].iloc[0]
        ref_midday_vol = midday_vol_map.get(dt, day_df['Volume'].median())

        in_flush, flush_low, flush_vol_climax, flush_time = False, np.inf, False, None
        entry_price, entry_time, stop_price, scaled_leg1, runner_active, leg1_ret = None, None, None, False, False, 0.0

        bars = list(day_df.iterrows())
        for i in range(1, len(bars)):
            ts, row = bars[i]
            prev_ts, prev_row = bars[i - 1]
            t_hm = ts.hour * 100 + ts.minute
            is_last_bar = (i == len(bars) - 1)

            if entry_price is None:
                if 950 <= t_hm <= 1300 and not is_last_bar:
                    drop_from_high = (row['Low'] - row['MorningHigh']) / row['MorningHigh']
                    below_vwap = (row['Low'] - row['VWAP']) / row['VWAP']

                    if in_flush and row['Close'] >= row['VWAP']:
                        in_flush, flush_low, flush_vol_climax, flush_time = False, np.inf, False, None

                    post_open_rally = row['MorningHigh'] > open_bar_high * 1.001
                    tod_ref = min(row['ToD_MedVol'], ref_midday_vol)
                    is_vol_climax = (row['Volume'] >= 1.8 * row['VolMA10']) or (
                        post_open_rally
                        and drop_from_high <= -0.0150
                        and below_vwap <= -0.0070
                        and row['Volume'] >= 1.65 * tod_ref
                    )

                    if drop_from_high <= -0.011 and below_vwap <= -0.0035:
                        if not in_flush or row['Low'] < flush_low:
                            in_flush, flush_low, flush_time = True, row['Low'], ts
                        if is_vol_climax:
                            flush_vol_climax = True
                    elif in_flush and row['Low'] < flush_low:
                        flush_low, flush_time = row['Low'], ts
                        if is_vol_climax:
                            flush_vol_climax = True

                    if in_flush and flush_vol_climax:
                        reclaim_bounce = (row['Close'] - flush_low) / flush_low
                        max_bounce = 0.0080 if post_open_rally else 0.0065
                        close_below_vwap = (row['Close'] - row['VWAP']) / row['VWAP']
                        is_hl_reclaim = (
                            ts > flush_time
                            and row['Low'] > flush_low
                            and row['Close'] > row['Open']
                            and row['Close'] > prev_row['Close']
                            and 0.0015 <= reclaim_bounce <= max_bounce
                            and close_below_vwap <= -0.0010
                        )
                        bar_rng = row['High'] - row['Low']
                        is_hammer_reclaim = (
                            post_open_rally
                            and ts == flush_time
                            and below_vwap <= -0.0065
                            and row['Close'] > row['Open']
                            and bar_rng > 0
                            and ((row['Close'] - row['Low']) / bar_rng) >= 0.75
                            and 0.0040 <= reclaim_bounce <= 0.0080
                            and row['Close'] < row['VWAP']
                        )
                        if is_hl_reclaim or is_hammer_reclaim:
                            entry_price, entry_time = row['Close'], ts
                            stop_price = min(
                                flush_low * (1.0 - 0.0005),
                                max(flush_low * (1.0 - 0.0035), entry_price * (1.0 - 0.0065))
                            )
            else:
                if row['Low'] <= stop_price:
                    stop_ret = (stop_price - entry_price) / entry_price * 100
                    if scaled_leg1:
                        net_ret = 0.5 * leg1_ret + 0.5 * stop_ret - 0.03
                        reason = '50% Leg1(>=0.4%) + 50% EMA Trail' if runner_active else '50% Leg1(>=0.4%) + 50% BE Stop'
                        exit_p = entry_price * (1 + (0.5 * leg1_ret + 0.5 * stop_ret) / 100)
                    else:
                        net_ret = stop_ret - 0.03
                        reason = 'Tight Stop (<= -0.35%<Low)'
                        exit_p = stop_price
                    trades.append({'Ticker': ticker, 'Date': dt, 'EntryTime': entry_time, 'ExitTime': ts, 'EntryPrice': entry_price, 'ExitPrice': exit_p, 'RetPct': net_ret, 'Reason': reason})
                    break

                leg1_target = max(row['VWAP'], entry_price * 1.0040)
                if not scaled_leg1 and row['High'] >= leg1_target:
                    scaled_leg1 = True
                    leg1_fill = min(row['High'], leg1_target) if row['Open'] <= leg1_target else row['Open']
                    leg1_ret = (leg1_fill - entry_price) / entry_price * 100
                    stop_price = max(stop_price, entry_price)

                ret_high = (row['High'] - entry_price) / entry_price * 100
                if ret_high >= 1.60:
                    if not scaled_leg1:
                        leg1_fill = max(entry_price * 1.0040, min(entry_price * 1.0160, row['VWAP']))
                        leg1_ret = (leg1_fill - entry_price) / entry_price * 100
                    leg2_ret = 1.60
                    net_ret = 0.5 * leg1_ret + 0.5 * leg2_ret - 0.03
                    exit_p = entry_price * (1 + (0.5 * leg1_ret + 0.5 * leg2_ret) / 100)
                    trades.append({'Ticker': ticker, 'Date': dt, 'EntryTime': entry_time, 'ExitTime': ts, 'EntryPrice': entry_price, 'ExitPrice': exit_p, 'RetPct': net_ret, 'Reason': '50% Leg1(>=0.4%) + 50% Stretch(+1.60%)'})
                    break

                if ret_high >= 0.85:
                    if not scaled_leg1:
                        scaled_leg1 = True
                        leg1_fill = max(entry_price * 1.0040, min(entry_price * 1.0085, row['VWAP']))
                        leg1_ret = (leg1_fill - entry_price) / entry_price * 100
                    if row['Close'] >= entry_price * 1.0075 and row['Close'] > row['EMA12']:
                        runner_active = True
                        stop_price = max(stop_price, entry_price * 1.0085, row['EMA20'] * 0.9985)
                    elif not runner_active:
                        leg2_ret = 0.85
                        net_ret = 0.5 * leg1_ret + 0.5 * leg2_ret - 0.03
                        exit_p = entry_price * (1 + (0.5 * leg1_ret + 0.5 * leg2_ret) / 100)
                        trades.append({'Ticker': ticker, 'Date': dt, 'EntryTime': entry_time, 'ExitTime': ts, 'EntryPrice': entry_price, 'ExitPrice': exit_p, 'RetPct': net_ret, 'Reason': '50% Leg1(>=0.4%) + 50% Target(+0.85%)'})
                        break
                elif runner_active:
                    stop_price = max(stop_price, entry_price * 1.0085, row['EMA20'] * 0.9985)

                if t_hm >= 1540 or is_last_bar:
                    ret_close = (row['Close'] - entry_price) / entry_price * 100
                    if scaled_leg1:
                        net_ret = 0.5 * leg1_ret + 0.5 * ret_close - 0.03
                        reason = '50% Leg1(>=0.4%) + 50% 3:40PM Runner' if runner_active else '50% Leg1(>=0.4%) + 50% 3:40PM Flatten'
                        exit_p = entry_price * (1 + (0.5 * leg1_ret + 0.5 * ret_close) / 100)
                    else:
                        net_ret = ret_close - 0.03
                        reason = '3:40PM Pre-10b18 Flatten'
                        exit_p = row['Close']
                    trades.append({'Ticker': ticker, 'Date': dt, 'EntryTime': entry_time, 'ExitTime': ts, 'EntryPrice': entry_price, 'ExitPrice': exit_p, 'RetPct': net_ret, 'Reason': reason})
                    break
    return pd.DataFrame(trades)


# ==============================================================================
# 5. V5 (FINAL PRODUCTION ENGINE: PRE-ER BLACKOUT + SPY WATERFALL + VOLMA10_EXOPEN)
# ==============================================================================
def run_v5_production(ticker, df_5m, df_1d, spy_df, spy_5m=None, spy_waterfall_map=None, mode='ex_ante_accum'):
    df = df_5m.copy()
    df['Date'] = df.index.date
    df['TimeStr'] = df.index.strftime('%H:%M')
    accum_map, _ = get_accum_map(df_1d, spy_df=spy_df, use_v3_tells=True)

    if spy_waterfall_map is None:
        spy_waterfall_map, _ = build_spy_intraday_map(spy_5m)

    # Time-of-Day Relative Volume (15-day rolling median at exact HH:MM, shifted 1d)
    df['ToD_MedVol'] = df.groupby('TimeStr')['Volume'].transform(
        lambda s: s.rolling(15, min_periods=1).median().shift(1).bfill()
    )
    midday_bars = df[(df.index.hour * 100 + df.index.minute >= 1030) & (df.index.hour * 100 + df.index.minute <= 1430)]
    midday_med = midday_bars.groupby('Date')['Volume'].median().rolling(15, min_periods=1).median().shift(1).bfill()
    midday_vol_map = midday_med.to_dict()

    trades = []
    for dt, day_df in df.groupby('Date'):
        if len(day_df) < 30:
            continue
        if mode == 'ex_ante_accum' and not accum_map.get(dt, False):
            continue
        if mode == 'non_accum' and accum_map.get(dt, False):
            continue

        # Upgrade #1: 14-Day Pre-Earnings Corporate Buyback Blackout Filter
        if mode != 'all':
            in_blackout, _ = check_er_blackout(ticker, dt, blackout_days=14)
            if in_blackout:
                continue

        day_df = day_df.copy()
        typ_price = (day_df['High'] + day_df['Low'] + day_df['Close']) / 3.0
        day_df['CumPV'] = (typ_price * day_df['Volume']).cumsum()
        day_df['CumV'] = day_df['Volume'].cumsum()
        day_df['VWAP'] = day_df['CumPV'] / day_df['CumV']

        # Upgrade #3a: Compute VolMA10_ExOpen (excluding 09:30 and 09:35 opening auction bars)
        vol_ex_open = day_df['Volume'].where(~day_df['TimeStr'].isin(['09:30', '09:35']), np.nan)
        day_df['VolMA10_ExOpen'] = vol_ex_open.rolling(10, min_periods=1).mean().fillna(day_df['Volume'])
        day_df['MorningHigh'] = day_df['High'].cummax()
        day_df['EMA12'] = day_df['Close'].ewm(span=12, adjust=False).mean()
        day_df['EMA20'] = day_df['Close'].ewm(span=20, adjust=False).mean()
        open_bar_high = day_df['High'].iloc[0]
        ref_midday_vol = midday_vol_map.get(dt, day_df['Volume'].median())

        in_flush = False
        flush_low = np.inf
        flush_vol_climax = False
        flush_time = None
        flush_vwap = np.nan
        flush_mhigh = np.nan
        climax_mult = 0.0
        climax_time = None

        entry_price = None
        entry_time = None
        stop_price = None
        initial_stop = None
        scaled_leg1 = False
        runner_active = False
        leg1_ret = 0.0
        leg1_fill_price = np.nan
        leg1_fill_time = None

        bars = list(day_df.iterrows())
        for i in range(1, len(bars)):
            ts, row = bars[i]
            prev_ts, prev_row = bars[i - 1]
            t_hm = ts.hour * 100 + ts.minute
            is_last_bar = (i == len(bars) - 1)

            if entry_price is None:
                # Ignore 09:30-09:49 opening noise completely
                if 950 <= t_hm <= 1300 and not is_last_bar:
                    drop_from_high = (row['Low'] - row['MorningHigh']) / row['MorningHigh']
                    below_vwap = (row['Low'] - row['VWAP']) / row['VWAP']

                    if in_flush and row['Close'] >= row['VWAP']:
                        in_flush, flush_low, flush_vol_climax, flush_time = False, np.inf, False, None
                        climax_mult, climax_time = 0.0, None

                    post_open_rally = row['MorningHigh'] > open_bar_high * 1.001
                    tod_ref = min(row['ToD_MedVol'], ref_midday_vol)
                    vol_mult_ex = row['Volume'] / row['VolMA10_ExOpen'] if row['VolMA10_ExOpen'] > 0 else 0.0
                    vol_mult_tod = row['Volume'] / tod_ref if tod_ref > 0 else 0.0

                    is_vol_climax = (
                        (row['Volume'] >= 1.8 * row['VolMA10_ExOpen'])
                        or (row['Volume'] >= 1.65 * tod_ref and row['Volume'] >= 1.0 * row['VolMA10_ExOpen'])
                        or (post_open_rally and drop_from_high <= -0.0150 and below_vwap <= -0.0070 and row['Volume'] >= 1.65 * tod_ref)
                    )

                    if drop_from_high <= -0.011 and below_vwap <= -0.0035:
                        if not in_flush or row['Low'] < flush_low:
                            in_flush = True
                            flush_low = row['Low']
                            flush_time = ts
                            flush_vwap = row['VWAP']
                            flush_mhigh = row['MorningHigh']
                        if is_vol_climax:
                            flush_vol_climax = True
                            cur_mult = max(vol_mult_ex, vol_mult_tod)
                            if cur_mult >= climax_mult:
                                climax_mult = cur_mult
                                climax_time = ts
                    elif in_flush and row['Low'] < flush_low:
                        flush_low = row['Low']
                        flush_time = ts
                        flush_vwap = row['VWAP']
                        flush_mhigh = row['MorningHigh']
                        if is_vol_climax:
                            flush_vol_climax = True
                            cur_mult = max(vol_mult_ex, vol_mult_tod)
                            if cur_mult >= climax_mult:
                                climax_mult = cur_mult
                                climax_time = ts

                    if in_flush and flush_vol_climax:
                        # Upgrade #2: Intraday SPY Waterfall Circuit Breaker
                        if spy_waterfall_map.get(ts, False):
                            continue

                        reclaim_bounce = (row['Close'] - flush_low) / flush_low
                        max_bounce = 0.0080 if post_open_rally else 0.0065
                        close_below_vwap = (row['Close'] - row['VWAP']) / row['VWAP']

                        is_hl_reclaim = (
                            ts > flush_time
                            and row['Low'] > flush_low
                            and row['Close'] > row['Open']
                            and row['Close'] > prev_row['Close']
                            and 0.0015 <= reclaim_bounce <= max_bounce
                            and close_below_vwap <= -0.0010
                        )
                        bar_rng = row['High'] - row['Low']
                        is_hammer_reclaim = (
                            post_open_rally
                            and ts == flush_time
                            and below_vwap <= -0.0065
                            and row['Close'] > row['Open']
                            and bar_rng > 0
                            and ((row['Close'] - row['Low']) / bar_rng) >= 0.75
                            and 0.0040 <= reclaim_bounce <= 0.0080
                            and row['Close'] < row['VWAP']
                        )
                        if is_hl_reclaim or is_hammer_reclaim:
                            entry_price = row['Close']
                            entry_time = ts
                            stop_price = min(
                                flush_low * (1.0 - 0.0005),
                                max(flush_low * (1.0 - 0.0035), entry_price * (1.0 - 0.0065))
                            )
                            initial_stop = stop_price
            else:
                if row['Low'] <= stop_price:
                    stop_ret = (stop_price - entry_price) / entry_price * 100
                    if scaled_leg1:
                        net_ret = 0.5 * leg1_ret + 0.5 * stop_ret - 0.03
                        reason = '50% Leg1(>=0.4%) + 50% EMA Trail' if runner_active else '50% Leg1(>=0.4%) + 50% BE Stop'
                        exit_p = entry_price * (1 + (0.5 * leg1_ret + 0.5 * stop_ret) / 100)
                    else:
                        net_ret = stop_ret - 0.03
                        reason = 'Tight Stop (<= -0.35%<Low)'
                        exit_p = stop_price
                    trades.append({
                        'Ticker': ticker, 'Date': dt, 'EntryTime': entry_time, 'ExitTime': ts,
                        'EntryPrice': entry_price, 'ExitPrice': exit_p,
                        'RetPct': net_ret, 'Reason': reason,
                        'FlushLow': flush_low, 'FlushTime': flush_time, 'FlushMHigh': flush_mhigh,
                        'FlushVWAP': flush_vwap, 'ClimaxMult': climax_mult, 'ClimaxTime': climax_time,
                        'InitialStop': initial_stop, 'Leg1Fill': leg1_fill_price, 'Leg1Time': leg1_fill_time
                    })
                    break

                leg1_target = max(row['VWAP'], entry_price * 1.0040)
                if not scaled_leg1 and row['High'] >= leg1_target:
                    scaled_leg1 = True
                    leg1_fill = min(row['High'], leg1_target) if row['Open'] <= leg1_target else row['Open']
                    leg1_ret = (leg1_fill - entry_price) / entry_price * 100
                    leg1_fill_price = leg1_fill
                    leg1_fill_time = ts
                    stop_price = max(stop_price, entry_price)

                ret_high = (row['High'] - entry_price) / entry_price * 100
                if ret_high >= 1.60:
                    if not scaled_leg1:
                        leg1_fill = max(entry_price * 1.0040, min(entry_price * 1.0160, row['VWAP']))
                        leg1_ret = (leg1_fill - entry_price) / entry_price * 100
                        leg1_fill_price = leg1_fill
                        leg1_fill_time = ts
                    leg2_ret = 1.60
                    net_ret = 0.5 * leg1_ret + 0.5 * leg2_ret - 0.03
                    exit_p = entry_price * (1 + (0.5 * leg1_ret + 0.5 * leg2_ret) / 100)
                    trades.append({
                        'Ticker': ticker, 'Date': dt, 'EntryTime': entry_time, 'ExitTime': ts,
                        'EntryPrice': entry_price, 'ExitPrice': exit_p,
                        'RetPct': net_ret, 'Reason': '50% Leg1(>=0.4%) + 50% Stretch(+1.60%)',
                        'FlushLow': flush_low, 'FlushTime': flush_time, 'FlushMHigh': flush_mhigh,
                        'FlushVWAP': flush_vwap, 'ClimaxMult': climax_mult, 'ClimaxTime': climax_time,
                        'InitialStop': initial_stop, 'Leg1Fill': leg1_fill_price, 'Leg1Time': leg1_fill_time
                    })
                    break

                # Upgrade #3b: Fix Runner Stop Placement so stop is never placed above row['Close'] * 0.9990
                if ret_high >= 0.85:
                    if not scaled_leg1:
                        scaled_leg1 = True
                        leg1_fill = max(entry_price * 1.0040, min(entry_price * 1.0085, row['VWAP']))
                        leg1_ret = (leg1_fill - entry_price) / entry_price * 100
                        leg1_fill_price = leg1_fill
                        leg1_fill_time = ts
                    if row['Close'] >= entry_price * 1.0075 and row['Close'] > row['EMA12']:
                        runner_active = True
                        stop_price = max(
                            stop_price,
                            min(row['Close'] * 0.9990, entry_price * 1.0070),
                            row['EMA20'] * 0.9985
                        )
                    elif not runner_active:
                        leg2_ret = 0.85
                        net_ret = 0.5 * leg1_ret + 0.5 * leg2_ret - 0.03
                        exit_p = entry_price * (1 + (0.5 * leg1_ret + 0.5 * leg2_ret) / 100)
                        trades.append({
                            'Ticker': ticker, 'Date': dt, 'EntryTime': entry_time, 'ExitTime': ts,
                            'EntryPrice': entry_price, 'ExitPrice': exit_p,
                            'RetPct': net_ret, 'Reason': '50% Leg1(>=0.4%) + 50% Target(+0.85%)',
                            'FlushLow': flush_low, 'FlushTime': flush_time, 'FlushMHigh': flush_mhigh,
                            'FlushVWAP': flush_vwap, 'ClimaxMult': climax_mult, 'ClimaxTime': climax_time,
                            'InitialStop': initial_stop, 'Leg1Fill': leg1_fill_price, 'Leg1Time': leg1_fill_time
                        })
                        break
                elif runner_active:
                    stop_price = max(
                        stop_price,
                        min(row['Close'] * 0.9990, entry_price * 1.0070),
                        row['EMA20'] * 0.9985
                    )

                if t_hm >= 1540 or is_last_bar:
                    ret_close = (row['Close'] - entry_price) / entry_price * 100
                    if scaled_leg1:
                        net_ret = 0.5 * leg1_ret + 0.5 * ret_close - 0.03
                        reason = '50% Leg1(>=0.4%) + 50% 3:40PM Runner' if runner_active else '50% Leg1(>=0.4%) + 50% 3:40PM Flatten'
                        exit_p = entry_price * (1 + (0.5 * leg1_ret + 0.5 * ret_close) / 100)
                    else:
                        net_ret = ret_close - 0.03
                        reason = '3:40PM Pre-10b18 Flatten'
                        exit_p = row['Close']
                    trades.append({
                        'Ticker': ticker, 'Date': dt, 'EntryTime': entry_time, 'ExitTime': ts,
                        'EntryPrice': entry_price, 'ExitPrice': exit_p,
                        'RetPct': net_ret, 'Reason': reason,
                        'FlushLow': flush_low, 'FlushTime': flush_time, 'FlushMHigh': flush_mhigh,
                        'FlushVWAP': flush_vwap, 'ClimaxMult': climax_mult, 'ClimaxTime': climax_time,
                        'InitialStop': initial_stop, 'Leg1Fill': leg1_fill_price, 'Leg1Time': leg1_fill_time
                    })
                    break
    return pd.DataFrame(trades)


def get_metrics(df, label):
    if df.empty:
        return {'Strategy / Ticker': label, 'N': 0, 'WR%': 0, 'Avg%': 0, 'Sum%': 0, 'PF': 0, 'Worst%': 0, 'Skew': 0, 'Kurt': 0, 'Sharpe': 0}
    n = len(df)
    wr = (df['RetPct'] > 0).mean() * 100
    avg = df['RetPct'].mean()
    std = df['RetPct'].std()
    skew = df['RetPct'].skew() if n > 2 else np.nan
    kurt = df['RetPct'].kurt() if n > 3 else np.nan
    tot = df['RetPct'].sum()
    pos = df[df['RetPct'] > 0]['RetPct'].sum()
    neg = abs(df[df['RetPct'] < 0]['RetPct'].sum())
    pf = pos / neg if neg > 0 else np.inf
    worst = df['RetPct'].min()
    sharpe = (avg / std) * np.sqrt(252) if std and std > 0 else np.nan
    return {
        'Strategy / Ticker': label, 'N': n, 'WR%': round(wr, 1),
        'Avg%': round(avg, 2), 'Sum%': round(tot, 2), 'PF': round(pf, 2),
        'Worst%': round(worst, 2), 'Skew': round(skew, 2) if pd.notna(skew) else np.nan,
        'Kurt': round(kurt, 2) if pd.notna(kurt) else np.nan,
        'Sharpe': round(sharpe, 2) if pd.notna(sharpe) else np.nan
    }


# ==============================================================================
# 6. V5 TRADER EXECUTION HUD (--live / --watch / --clip)
# ==============================================================================
def get_session_window_banner(now_est, latest_session_date):
    t_hm = now_est.hour * 100 + now_est.minute
    is_weekend = now_est.weekday() >= 5
    if is_weekend or t_hm < 930 or t_hm >= 1600:
        return (
            f"🌙 POST-CLOSE / OFF-HOURS REPLAY (SESSION: {latest_session_date}) — "
            f"NEXT ENTRY WINDOW: 09:50–13:00 EST (09:30–09:49 OPENING NOISE FILTER)"
        )
    if 930 <= t_hm <= 949:
        return "⏳ 09:30–09:49 EST: OPENING NOISE FILTER — DO NOT ENTER YET"
    if 950 <= t_hm <= 1300:
        return "🟢 09:50–13:00 EST: ACTIVE ENTRY WINDOW (SCANNING FOR FLUSH & RECLAIM)"
    if 1300 < t_hm < 1540:
        return "🏃 13:00–15:40 EST: AFTERNOON RUNNER WINDOW (NO NEW ENTRIES — TRAIL WINNERS)"
    return "🛑 15:40–16:00 EST: PRE-10b18 CUTOFF — FLATTEN ALL INTRADAY TRADES NOW"


_HUD_CACHE = {
    '1d': {},
    '5m': {},
    'last_full_fetch': 0.0,
}


def fetch_cnbc_bars(sym, interval='5m', days=5):
    """
    Zero-rate-limit real-time OHLCV bar fetcher via CNBC Harmony TS-API (Akamai CDN).
    Used as primary 30s delta refresher and automatic failover if Yahoo Finance throttles.
    """
    from datetime import timedelta
    from httpx import Client

    est = pytz.timezone('US/Eastern')
    now_est = datetime.now(est)
    start_est = now_est - timedelta(days=days)
    cnbc_int = '1D' if str(interval).lower() in ('1d', 'd') else '5M'
    start_str = start_est.strftime('%Y%m%d000000')
    end_str = (now_est + timedelta(days=1)).strftime('%Y%m%d160000')
    url = f"https://ts-api.cnbc.com/harmony/app/bars/{sym.upper()}/{cnbc_int}/{start_str}/{end_str}/adjusted/EST5EDT.json"
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
                return pd.DataFrame()
            bars = r.json().get('barData', {}).get('priceBars', [])
            if not bars:
                return pd.DataFrame()
            rows = []
            for b in bars:
                tt = str(b.get('tradeTime', ''))
                if len(tt) < 8 or b.get('close') is None:
                    continue
                if cnbc_int == '1D':
                    dt_naive = datetime.strptime(tt[:8], '%Y%m%d')
                else:
                    dt_naive = datetime.strptime(tt[:14], '%Y%m%d%H%M%S')
                dt_tz = est.localize(dt_naive)
                if cnbc_int == '5M':
                    hm = dt_tz.hour * 100 + dt_tz.minute
                    if hm < 930 or hm > 1555:
                        continue
                rows.append({
                    'Datetime': dt_tz,
                    'Open': float(b['open']),
                    'High': float(b['high']),
                    'Low': float(b['low']),
                    'Close': float(b['close']),
                    'Volume': int(float(b.get('volume') or 0)),
                })
            if not rows:
                return pd.DataFrame()
            return pd.DataFrame(rows).set_index('Datetime').sort_index()
    except Exception:
        return pd.DataFrame()


def safe_yf_history(sym, period, interval, retries=1):
    """
    Resilient OHLCV history fetcher that uses CNBC Harmony CDN first (zero rate limits)
    and falls back to yfinance if needed.
    """
    days_map = {'1d': 2, '2d': 3, '5d': 7, '60d': 90, '1y': 370}
    days = days_map.get(str(period).lower(), 60)
    df_cnbc = fetch_cnbc_bars(sym, interval=interval, days=days)
    if df_cnbc is not None and not df_cnbc.empty:
        return df_cnbc

    for attempt in range(retries + 1):
        try:
            df = yf.Ticker(sym).history(period=period, interval=interval)
            if df is not None and not df.empty:
                return df
        except KeyboardInterrupt:
            raise
        except Exception:
            if attempt < retries:
                time.sleep(0.3 * (attempt + 1))
    return pd.DataFrame()


def batch_refresh_hud_deltas(symbols):
    """
    Updates all cached 5m DataFrames using CNBC Harmony CDN 5m bars (zero rate limits),
    with automatic fallback to batched yf.download if needed.
    """
    if not symbols:
        return
    updated_count = 0
    for sym in symbols:
        cached_5m = _HUD_CACHE['5m'].get(sym, pd.DataFrame())
        if cached_5m.empty:
            continue
        sub = fetch_cnbc_bars(sym, interval='5m', days=3)
        if sub is not None and not sub.empty:
            if cached_5m.index.tz is not None and sub.index.tz is not None:
                sub = sub.tz_convert(cached_5m.index.tz)
            combined = pd.concat([cached_5m, sub])
            combined = combined[~combined.index.duplicated(keep='last')].sort_index()
            _HUD_CACHE['5m'][sym] = combined
            updated_count += 1

    if updated_count == len(symbols):
        return

    try:
        batch_df = yf.download(
            tickers=list(symbols),
            period='2d',
            interval='5m',
            group_by='ticker',
            progress=False,
            threads=False,
        )
        if batch_df is None or batch_df.empty:
            return
        for sym in symbols:
            cached_5m = _HUD_CACHE['5m'].get(sym, pd.DataFrame())
            if cached_5m.empty:
                continue
            try:
                sub = batch_df[sym].dropna(how='all') if isinstance(batch_df.columns, pd.MultiIndex) else batch_df.dropna(how='all')
                if sub is not None and not sub.empty:
                    if cached_5m.index.tz is not None and sub.index.tz is not None:
                        sub = sub.tz_convert(cached_5m.index.tz)
                    combined = pd.concat([cached_5m, sub])
                    combined = combined[~combined.index.duplicated(keep='last')].sort_index()
                    _HUD_CACHE['5m'][sym] = combined
            except Exception:
                pass
    except Exception:
        pass


def get_cached_hud_history(sym, need_full=False, skip_individual_delta=False):
    """
    Caches 1y daily and 60d 5m history in memory across --watch cycles, fetching only lightweight
    delta bars on 30-second refreshes so Yahoo Finance never rate-limits or crashes the HUD.
    """
    cached_1d = _HUD_CACHE['1d'].get(sym, pd.DataFrame())
    cached_5m = _HUD_CACHE['5m'].get(sym, pd.DataFrame())

    if need_full or cached_1d.empty:
        df_1d = safe_yf_history(sym, period='1y', interval='1d')
        if not df_1d.empty:
            _HUD_CACHE['1d'][sym] = df_1d
            cached_1d = df_1d

    if need_full or cached_5m.empty:
        df_5m = safe_yf_history(sym, period='60d', interval='5m')
        if not df_5m.empty:
            _HUD_CACHE['5m'][sym] = df_5m
            cached_5m = df_5m
    elif not skip_individual_delta:
        recent_5m = safe_yf_history(sym, period='5d', interval='5m')
        if not recent_5m.empty:
            combined = pd.concat([cached_5m, recent_5m])
            combined = combined[~combined.index.duplicated(keep='last')].sort_index()
            _HUD_CACHE['5m'][sym] = combined
            cached_5m = combined

    return cached_1d.copy(), cached_5m.copy()


def render_trader_hud(tickers_list, clip_dollars=150000.0, clear_screen=False):
    import importlib
    import tasty_executor
    importlib.reload(tasty_executor)
    from tasty_executor import MAX_FEED_DIVERGENCE_PCT, fetch_secondary_quotes

    est_tz = pytz.timezone('US/Eastern')
    now_est = datetime.now(est_tz)

    now_ts = time.time()
    all_hud_syms = ['SPY'] + list(tickers_list)
    have_warm_cache = all(
        (s in _HUD_CACHE['1d'] and not _HUD_CACHE['1d'][s].empty and s in _HUD_CACHE['5m'] and not _HUD_CACHE['5m'][s].empty)
        for s in all_hud_syms
    )
    need_full = (not have_warm_cache) or ((now_ts - _HUD_CACHE['last_full_fetch']) > 1800.0)

    # 1. If cache is warm, refresh all 7 symbols in a SINGLE batched Yahoo HTTP request
    if not need_full:
        batch_refresh_hud_deltas(all_hud_syms)

    # 2. Fetch Independent Secondary Price & VWAP Oracle (TradingView Scanner API) in 1 HTTP POST
    tv_quotes = fetch_secondary_quotes(all_hud_syms)

    spy_1d, spy_5m = get_cached_hud_history('SPY', need_full=need_full, skip_individual_delta=(not need_full))
    if spy_1d.empty or spy_5m.empty:
        print(f"⚠️  [{now_est.strftime('%H:%M:%S EST')}] Transient Yahoo Finance timeout on SPY; retrying next cycle...")
        return
    spy_waterfall_map, spy_state = build_spy_intraday_map(spy_5m)
    latest_session_date = spy_state['Date'] if spy_state else now_est.date()

    # --------------------------------------------------------------------------
    # SECTION A: TOP MACRO & SESSION GATE BAR
    # --------------------------------------------------------------------------
    session_banner = get_session_window_banner(now_est, latest_session_date)
    if spy_state and spy_state['IsWaterfall']:
        spy_gate_str = "🚨 CIRCUIT BREAKER: SPY WATERFALL CRASH — ALL LONGS PAUSED"
    else:
        spy_gate_str = "🟢 SPY TAPE HEALTHY"

    spy_last = spy_state['Last'] if spy_state else 0.0
    spy_vwap = spy_state['VWAP'] if spy_state else 0.0
    spy_dd = spy_state['DrawdownPct'] if spy_state else 0.0
    spy_max_dd = spy_state['MaxIntradayDropPct'] if spy_state else 0.0
    spy_vs_vwap = spy_state['VsVWAPPct'] if spy_state else 0.0

    # --------------------------------------------------------------------------
    # SECTION B: THE 6-STOCK MASTER RADAR TABLE
    # --------------------------------------------------------------------------
    radar_rows = []
    action_cards = []
    feed_sync_count = 0
    feed_total_checked = 0
    max_feed_spread_pct = 0.0

    if 'SPY' in tv_quotes and spy_last > 0:
        spy_tv_p = tv_quotes['SPY']['last']
        spy_spread = abs(spy_last - spy_tv_p) / spy_last * 100.0
        feed_total_checked += 1
        max_feed_spread_pct = max(max_feed_spread_pct, spy_spread)
        if spy_spread <= MAX_FEED_DIVERGENCE_PCT * 100.0:
            feed_sync_count += 1

    for t in tickers_list:
        df_1d, df_5m = get_cached_hud_history(t, need_full=need_full, skip_individual_delta=(not need_full))
        if df_5m.empty or df_1d.empty:
            continue

        df_5m['Date'] = df_5m.index.date
        df_5m['TimeStr'] = df_5m.index.strftime('%H:%M')
        df_5m['ToD_MedVol'] = df_5m.groupby('TimeStr')['Volume'].transform(
            lambda s: s.rolling(15, min_periods=1).median().shift(1).bfill()
        )
        midday_bars = df_5m[(df_5m.index.hour * 100 + df_5m.index.minute >= 1030) & (df_5m.index.hour * 100 + df_5m.index.minute <= 1430)]
        midday_med = midday_bars.groupby('Date')['Volume'].median().rolling(15, min_periods=1).median().shift(1).bfill()
        midday_vol_map = midday_med.to_dict()

        accum_map, latest_accum = get_accum_map(df_1d, spy_df=spy_1d, use_v3_tells=True)
        last_dt = df_5m['Date'].iloc[-1]
        is_accum = accum_map.get(last_dt, False) or latest_accum
        in_blackout, er_str = check_er_blackout(t, last_dt, blackout_days=14)

        if in_blackout:
            er_short = pd.Timestamp(er_str).strftime('%b %d')
            gate_status = f"🚫 ER BLACKOUT ({er_short})"
        elif is_accum:
            gate_status = "🟢 ELIGIBLE (Accum+Open)"
        else:
            gate_status = "⚪ OFF (No Daily Accum)"

        day_df = df_5m[df_5m['Date'] == last_dt].copy()
        typ_price = (day_df['High'] + day_df['Low'] + day_df['Close']) / 3.0
        day_df['VWAP'] = (typ_price * day_df['Volume']).cumsum() / day_df['Volume'].cumsum()
        vol_ex_open = day_df['Volume'].where(~day_df['TimeStr'].isin(['09:30', '09:35']), np.nan)
        day_df['VolMA10_ExOpen'] = vol_ex_open.rolling(10, min_periods=1).mean().fillna(day_df['Volume'])
        day_df['MorningHigh'] = day_df['High'].cummax()
        day_df['EMA12'] = day_df['Close'].ewm(span=12, adjust=False).mean()
        day_df['EMA20'] = day_df['Close'].ewm(span=20, adjust=False).mean()
        open_bar_high = day_df['High'].iloc[0]
        ref_midday_vol = midday_vol_map.get(last_dt, day_df['Volume'].median())

        # Check live intraday state and whether V5 triggered today
        v5_all_trades = run_v5_production(
            t, df_5m[df_5m['Date'].isin(list(df_5m['Date'].unique())[-20:])],
            df_1d, spy_1d, spy_5m=spy_5m, spy_waterfall_map=spy_waterfall_map, mode='ex_ante_accum'
        )
        today_trades = v5_all_trades[v5_all_trades['Date'] == last_dt] if not v5_all_trades.empty else pd.DataFrame()

        in_flush, flush_low, flush_climax, flush_time = False, np.inf, False, None
        flush_vwap, flush_mhigh = np.nan, np.nan
        max_vol_mult = 0.0
        climax_time = None
        live_triggered_bar = False

        bars = list(day_df.iterrows())
        for i in range(1, len(bars)):
            ts, row = bars[i]
            prev_ts, prev_row = bars[i - 1]
            t_hm = ts.hour * 100 + ts.minute
            if 950 <= t_hm <= 1300:
                drop_h = (row['Low'] - row['MorningHigh']) / row['MorningHigh']
                below_v = (row['Low'] - row['VWAP']) / row['VWAP']
                if in_flush and row['Close'] >= row['VWAP']:
                    in_flush, flush_low, flush_climax, flush_time = False, np.inf, False, None

                post_open_rally = row['MorningHigh'] > open_bar_high * 1.001
                tod_ref = min(row['ToD_MedVol'], ref_midday_vol)
                v_mult_ex = row['Volume'] / row['VolMA10_ExOpen'] if row['VolMA10_ExOpen'] > 0 else 0.0
                v_mult_tod = row['Volume'] / tod_ref if tod_ref > 0 else 0.0
                cur_mult = max(v_mult_ex, v_mult_tod)
                if cur_mult > max_vol_mult and drop_h <= -0.008:
                    max_vol_mult = cur_mult

                climax = (
                    (row['Volume'] >= 1.8 * row['VolMA10_ExOpen'])
                    or (row['Volume'] >= 1.65 * tod_ref and row['Volume'] >= 1.0 * row['VolMA10_ExOpen'])
                    or (post_open_rally and drop_h <= -0.0150 and below_v <= -0.0070 and row['Volume'] >= 1.65 * tod_ref)
                )
                if drop_h <= -0.011 and below_v <= -0.0035:
                    if not in_flush or row['Low'] < flush_low:
                        in_flush, flush_low, flush_time = True, row['Low'], ts
                        flush_vwap, flush_mhigh = row['VWAP'], row['MorningHigh']
                    if climax:
                        flush_climax = True
                        climax_time = ts
                        max_vol_mult = max(max_vol_mult, cur_mult)
                elif in_flush and row['Low'] < flush_low:
                    flush_low, flush_time = row['Low'], ts
                    flush_vwap, flush_mhigh = row['VWAP'], row['MorningHigh']
                    if climax:
                        flush_climax = True
                        climax_time = ts
                        max_vol_mult = max(max_vol_mult, cur_mult)

                if i == len(bars) - 1 and in_flush and flush_climax and not in_blackout and is_accum:
                    reclaim_bounce = (row['Close'] - flush_low) / flush_low
                    max_bounce = 0.0080 if post_open_rally else 0.0065
                    close_below_vwap = (row['Close'] - row['VWAP']) / row['VWAP']
                    if (
                        ts > flush_time
                        and row['Low'] > flush_low
                        and row['Close'] > row['Open']
                        and row['Close'] > prev_row['Close']
                        and 0.0015 <= reclaim_bounce <= max_bounce
                        and close_below_vwap <= -0.0010
                        and not spy_waterfall_map.get(ts, False)
                    ):
                        live_triggered_bar = True

        last_row = day_df.iloc[-1]
        cur_p = float(last_row['Close'])
        cur_vwap = float(last_row['VWAP'])
        cur_ema20 = float(last_row['EMA20'])
        vs_vwap_pct = (cur_p - cur_vwap) / cur_vwap * 100.0
        m_high = float(last_row['MorningHigh'])

        # Cross-verify Primary (yfinance) against Secondary Oracle (TradingView Scanner API)
        tv_info = tv_quotes.get(t.upper())
        tv_price = float(tv_info['last']) if tv_info and tv_info.get('last') else None
        if tv_price and cur_p > 0:
            spread_pct = abs(cur_p - tv_price) / cur_p * 100.0
            feed_total_checked += 1
            max_feed_spread_pct = max(max_feed_spread_pct, spread_pct)
            if spread_pct <= MAX_FEED_DIVERGENCE_PCT * 100.0:
                feed_sync_count += 1
                feed_check_str = f"✅ SYNC ({spread_pct:.2f}%)"
            else:
                feed_check_str = f"⚠️ DIV ({spread_pct:.2f}%)"
        else:
            feed_check_str = "⚪ YF ONLY"

        # Morning window (09:50-13:00) flush stats
        morn_df = day_df[(day_df.index.hour * 100 + day_df.index.minute >= 950) & (day_df.index.hour * 100 + day_df.index.minute <= 1300)]
        if not today_trades.empty:
            tr_row = today_trades.iloc[0]
            disp_flush_low = float(tr_row['FlushLow'])
            disp_mhigh = float(tr_row['FlushMHigh'])
            disp_flush_pct = (disp_flush_low - disp_mhigh) / disp_mhigh * 100.0
            c_mult = float(tr_row['ClimaxMult'])
            vol_spike_str = f"✅ {c_mult:.1f}x (Climax)"
            entry_t_str = tr_row['EntryTime'].strftime('%H:%M')
            if live_triggered_bar:
                sig_str = "🚨 BUY TRIGGERED NOW"
            else:
                sig_str = f"🚨 BUY TRIG ({entry_t_str})"
            trig_p = float(tr_row['EntryPrice'])
            stop_p = float(tr_row['InitialStop'])
            leg1_p = float(tr_row['Leg1Fill']) if pd.notna(tr_row['Leg1Fill']) else max(cur_vwap, trig_p * 1.0040)
            leg2_p = trig_p * 1.0085
            action_cards.append({
                'Ticker': t,
                'Mode': 'TRADED_TODAY',
                'TradeRow': tr_row,
                'CurrentPrice': cur_p,
                'SecondaryPrice': tv_price,
                'CurrentVWAP': cur_vwap,
                'CurrentEMA20': cur_ema20,
            })
        else:
            ref_low = float(flush_low) if np.isfinite(flush_low) else float(morn_df['Low'].min() if not morn_df.empty else day_df['Low'].min())
            disp_flush_low = ref_low
            disp_flush_pct = (disp_flush_low - m_high) / m_high * 100.0
            last_hm = last_row.name.hour * 100 + last_row.name.minute
            if 950 <= last_hm <= 1300:
                last_tod_ref = min(float(last_row['ToD_MedVol']), float(ref_midday_vol))
                cur_bar_mult = max(
                    float(last_row['Volume']) / float(last_row['VolMA10_ExOpen']) if last_row['VolMA10_ExOpen'] > 0 else 0.0,
                    float(last_row['Volume']) / last_tod_ref if last_tod_ref > 0 else 0.0,
                )
                last_mult = max(cur_bar_mult, max_vol_mult)
            else:
                last_mult = max_vol_mult
            if flush_climax or last_mult >= 1.80:
                vol_spike_str = f"✅ {last_mult:.1f}x (Climax)"
            else:
                vol_spike_str = f"❌ {last_mult:.1f}x"

            if in_blackout or not is_accum:
                sig_str = "💤 MONITORING"
            elif live_triggered_bar:
                sig_str = "🚨 BUY TRIGGERED NOW"
            elif in_flush and flush_climax:
                sig_str = "🟡 ARMED (Wait Green HL)"
            elif in_flush:
                sig_str = "👀 FLUSHING (Need Vol)"
            else:
                sig_str = "💤 MONITORING"

            trig_p = round(disp_flush_low * 1.0025, 2)
            stop_p = min(disp_flush_low * (1.0 - 0.0005), max(disp_flush_low * (1.0 - 0.0035), trig_p * (1.0 - 0.0065)))
            leg1_p = max(cur_vwap, trig_p * 1.0040)
            leg2_p = trig_p * 1.0085

            if sig_str in ("🚨 BUY TRIGGERED NOW", "🟡 ARMED (Wait Green HL)"):
                action_cards.append({
                    'Ticker': t,
                    'Mode': sig_str,
                    'FlushLow': disp_flush_low,
                    'FlushTime': flush_time,
                    'FlushMHigh': flush_mhigh if pd.notna(flush_mhigh) else m_high,
                    'FlushVWAP': flush_vwap if pd.notna(flush_vwap) else cur_vwap,
                    'ClimaxMult': last_mult,
                    'ClimaxTime': climax_time,
                    'EntryPrice': cur_p if live_triggered_bar else trig_p,
                    'StopPrice': stop_p,
                    'Leg1Price': leg1_p,
                    'CurrentPrice': cur_p,
                    'SecondaryPrice': tv_price,
                    'CurrentVWAP': cur_vwap,
                    'CurrentEMA20': cur_ema20,
                })

        radar_rows.append({
            'Ticker': t,
            'GateStatus': gate_status,
            'Last': f"${cur_p:.2f}",
            'FeedCheck': feed_check_str,
            'VWAP (% vs VWAP)': f"${cur_vwap:.2f} ({vs_vwap_pct:+.2f}%)",
            'MornHigh': f"${m_high:.2f}",
            'FlushLow': f"${disp_flush_low:.2f}",
            'Flush%': f"{disp_flush_pct:+.2f}%",
            'VolSpike': vol_spike_str,
            'SIGNAL': sig_str,
            'BuyHere/Trig': f"${trig_p:.2f}",
            'HardStop': f"${stop_p:.2f}",
            'Leg1 (50%)': f"${leg1_p:.2f}",
            'Leg2 (+0.85%/Trail)': f"${leg2_p:.2f} / EMA20(${cur_ema20:.2f})"
        })

    if need_full:
        _HUD_CACHE['last_full_fetch'] = now_ts
    if clear_screen:
        print("\033[2J\033[H", end="")

    if feed_total_checked > 0:
        oracle_status = (
            f"🟢 DUAL-FEED ORACLE: Primary (CNBC Harmony CDN) + Secondary (CNBC Real-Time Quote API) | "
            f"{feed_sync_count}/{feed_total_checked} Symbols IN SYNC (Max Spread: {max_feed_spread_pct:.2f}% <= {MAX_FEED_DIVERGENCE_PCT * 100:.2f}% Cap)"
        )
    else:
        oracle_status = "🟢 DATA ORACLE: Primary (CNBC Harmony CDN)"

    print("=" * 128)
    print(f"🎯 V5 INSTITUTIONAL FLUSH & RECLAIM — TRADER EXECUTION HUD  |  IRA CLIP: ${clip_dollars:,.0f}  |  {now_est.strftime('%Y-%m-%d %H:%M:%S EST')}")
    print("-" * 128)
    print(f"SESSION GATE : {session_banner}")
    print(
        f"MACRO GATE   : {spy_gate_str}  |  SPY Last: ${spy_last:.2f}  |  SPY VWAP: ${spy_vwap:.2f} ({spy_vs_vwap:+.2f}%)  "
        f"|  SPY Drawdown from High: {spy_dd:+.2f}% (Intraday Low: {spy_max_dd:+.2f}%)"
    )
    print(f"FEED ORACLE  : {oracle_status}")
    print("=" * 128)
    print(pd.DataFrame(radar_rows).to_string(index=False))
    print("=" * 128)

    # --------------------------------------------------------------------------
    # SECTION C: POP-OUT "TRADER ACTION CARDS" (BOXED EXECUTION TICKETS)
    # --------------------------------------------------------------------------
    if not action_cards:
        print("📭 NO ACTIVE OR ARMED EXECUTION TICKETS AT THIS TIMESTAMP.")
    else:
        for card in action_cards:
            t = card['Ticker']
            cur_ema20 = card['CurrentEMA20']
            if card['Mode'] == 'TRADED_TODAY':
                tr_row = card['TradeRow']
                entry_p = float(tr_row['EntryPrice'])
                stop_p = float(tr_row['InitialStop'])
                f_low = float(tr_row['FlushLow'])
                f_mhigh = float(tr_row['FlushMHigh'])
                f_vwap = float(tr_row['FlushVWAP'])
                c_mult = float(tr_row['ClimaxMult'])
                c_time = tr_row['ClimaxTime'].strftime('%H:%M') if pd.notna(tr_row['ClimaxTime']) else tr_row['FlushTime'].strftime('%H:%M')
                e_time = tr_row['EntryTime'].strftime('%H:%M')
                x_time = tr_row['ExitTime'].strftime('%H:%M')
                flush_pct = (f_low - f_mhigh) / f_mhigh * 100.0
                below_vwap_pct = (f_low - f_vwap) / f_vwap * 100.0
                leg1_p = float(tr_row['Leg1Fill']) if pd.notna(tr_row['Leg1Fill']) else max(f_vwap, entry_p * 1.0040)
                status_badge = f"⚡ SESSION SIGNAL EXECUTED ({e_time} EST -> {x_time} EST | {tr_row['Reason']})"
                why_text = (
                    f"{t} flushed {flush_pct:.2f}% from ${f_mhigh:.2f} morning high to ${f_low:.2f} "
                    f"({below_vwap_pct:.2f}% below ${f_vwap:.2f} VWAP) with a {c_mult:.1f}x ToD Volume Climax "
                    f"at {c_time} EST and printed a higher-low green reclaim bar at {e_time} EST @ ${entry_p:.2f}."
                )
            else:
                entry_p = float(card['EntryPrice'])
                stop_p = float(card['StopPrice'])
                f_low = float(card['FlushLow'])
                f_mhigh = float(card['FlushMHigh'])
                f_vwap = float(card['FlushVWAP'])
                c_mult = float(card['ClimaxMult'])
                c_time = card['ClimaxTime'].strftime('%H:%M') if card['ClimaxTime'] is not None else "Morning"
                flush_pct = (f_low - f_mhigh) / f_mhigh * 100.0
                below_vwap_pct = (f_low - f_vwap) / f_vwap * 100.0
                leg1_p = float(card['Leg1Price'])
                status_badge = card['Mode']
                why_text = (
                    f"{t} flushed {flush_pct:.2f}% from ${f_mhigh:.2f} morning high to ${f_low:.2f} "
                    f"({below_vwap_pct:.2f}% below ${f_vwap:.2f} VWAP) with a {c_mult:.1f}x Volume Climax "
                    f"at {c_time} EST; trigger @ ${entry_p:.2f} on higher-low green 5m reclaim bar."
                )

            tot_shares = min(int(clip_dollars // entry_p), 3000)
            leg1_shares = tot_shares // 2
            leg2_shares = tot_shares - leg1_shares

            risk_pct = (stop_p - entry_p) / entry_p * 100.0
            max_dollar_risk = abs(tot_shares * (entry_p - stop_p))

            leg1_pct = (leg1_p - entry_p) / entry_p * 100.0
            leg1_dollar_gain = leg1_shares * (leg1_p - entry_p)

            leg2_t1_p = entry_p * 1.0085
            leg2_stretch_p = entry_p * 1.0160
            total_t1_gain = leg1_dollar_gain + leg2_shares * (leg2_t1_p - entry_p)
            total_stretch_gain = leg1_dollar_gain + leg2_shares * (leg2_stretch_p - entry_p)

            print("┌" + "─" * 126 + "┐")
            header_line = f"│ 🎫 TRADER EXECUTION TICKET: {t}  |  STATUS: {status_badge}"
            print(header_line + " " * max(0, 127 - len(header_line)) + "│")
            print("├" + "─" * 126 + "┤")
            print(f"│ 🧠 WHY DIAGNOSIS : {why_text}")
            print(f"│ 💼 IRA ORDER PLAN (${clip_dollars:,.0f} Clip = {tot_shares:,} Shares):")
            print(f"│    1. ENTRY              : Buy {tot_shares:,} sh @ ${entry_p:.2f} Limit (${tot_shares * entry_p:,.0f} notional)")
            print(f"│    2. HARD STOP          : Stop @ ${stop_p:.2f} [{risk_pct:+.2f}% / -${max_dollar_risk:,.0f} max risk] (<= -0.35% below ${f_low:.2f} FlushLow, capped -0.65%)")
            print(
                f"│    3. LEG 1 EXIT (50%)   : Limit Sell {leg1_shares:,} sh @ ${leg1_p:.2f} (max(VWAP, +0.40%) = +{leg1_pct:.2f}% / +${leg1_dollar_gain:,.0f} locked)"
                f" -> Action: Move Stop on {leg2_shares:,} sh to ${entry_p:.2f} BE"
            )
            print(
                f"│    4. LEG 2 RUNNER (50%) : Target 1 ({leg2_shares:,} sh) @ ${leg2_t1_p:.2f} (+0.85% = +${total_t1_gain:,.0f} total) OR if 5m bar closes strong > EMA12,"
            )
            print(
                f"│                            trail 5m EMA20 (currently ${cur_ema20:.2f}) up to ${leg2_stretch_p:.2f} (+1.60% stretch = +${total_stretch_gain:,.0f} total) | Mandatory Flatten @ 3:40 PM EST"
            )
            if card['Mode'] == 'TRADED_TODAY':
                tr_row = card['TradeRow']
                realized_pct = float(tr_row['RetPct'])
                realized_pnl = tot_shares * entry_p * (realized_pct / 100.0)
                print(
                    f"│    ✅ SESSION OUTCOME    : Exited @ ${float(tr_row['ExitPrice']):.2f} at {tr_row['ExitTime'].strftime('%H:%M')} EST "
                    f"({tr_row['Reason']}) -> Net Return: {realized_pct:+.3f}% (+${realized_pnl:,.0f} on ${clip_dollars:,.0f} clip)"
                )
            print("└" + "─" * 126 + "┘")

            # Part 4: Broker staging — Robinhood manual ticket, or Tastytrade HITL transmit
            if (
                card['Mode'] in ('🚨 BUY TRIGGERED NOW', '🟡 ARMED (Wait Green HL)')
                or '--dry-run' in sys.argv
                or '--stage' in sys.argv
            ):
                from tasty_executor import ALLOWED_ACCOUNT_ID, BROKER, stage_and_prompt_order

                is_dry_run = '--execute-live' not in sys.argv
                non_interactive = (
                    BROKER != 'tastytrade'
                    or '--no-prompt' in sys.argv
                    or (card['Mode'] != '🚨 BUY TRIGGERED NOW' and '--interactive' not in sys.argv)
                )
                print()
                try:
                    stage_and_prompt_order(
                        ticker=t,
                        limit_price=entry_p,
                        stop_price=stop_p,
                        leg1_vwap_price=leg1_p,
                        account_id=ALLOWED_ACCOUNT_ID,
                        clip_usd=clip_dollars,
                        dry_run=is_dry_run,
                        auto_abort_prompt=non_interactive,
                        primary_live_price=card.get('CurrentPrice'),
                        secondary_live_price=card.get('SecondaryPrice'),
                        signal=card['Mode'],
                    )
                except Exception as stage_err:
                    print(f"⚠️  [Order Staging Guard on {t}: {stage_err}]")


def run_live_scanner(tickers_list, watch_mode=False, clip_dollars=150000.0, dynamic_mode=False):
    if dynamic_mode:
        from universe_scanner import get_or_refresh_dynamic_universe
        tickers_list = get_or_refresh_dynamic_universe(core_tickers=['FISV', 'PLNT', 'EEFT'], verbose=True)

    if not watch_mode:
        render_trader_hud(tickers_list, clip_dollars=clip_dollars, clear_screen=False)
        return
    try:
        while True:
            try:
                if dynamic_mode:
                    from universe_scanner import get_or_refresh_dynamic_universe
                    tickers_list = get_or_refresh_dynamic_universe(core_tickers=['FISV', 'PLNT', 'EEFT'], verbose=False)
                render_trader_hud(tickers_list, clip_dollars=clip_dollars, clear_screen=True)
            except KeyboardInterrupt:
                raise
            except Exception as err:
                print(f"\n⚠️  [HUD Refresh Warning: {err} — retrying in 30s...]")
            print("\n[🔄 --watch mode active (smart delta cache enabled): refreshing every 30 seconds. Press Ctrl+C to exit.]")
            time.sleep(30)
    except KeyboardInterrupt:
        print("\nStopped --watch HUD.")


if __name__ == '__main__':
    from dotenv import load_dotenv
    load_dotenv()
    clip_size = float(os.getenv('TARGET_CLIP_USD', '5000'))
    if '--clip' in sys.argv:
        idx = sys.argv.index('--clip')
        if idx + 1 < len(sys.argv):
            clip_size = float(sys.argv[idx + 1])

    if '--live' in sys.argv or '--watch' in sys.argv:
        run_live_scanner(
            hud_tickers,
            watch_mode=('--watch' in sys.argv),
            clip_dollars=clip_size,
            dynamic_mode=('--dynamic' in sys.argv),
        )
        sys.exit(0)

    data_5m = {t: yf.Ticker(t).history(period='60d', interval='5m') for t in all_tickers}
    data_1d = {t: yf.Ticker(t).history(period='1y', interval='1d') for t in all_tickers}
    spy_1d = yf.Ticker('SPY').history(period='1y', interval='1d')
    spy_5m = yf.Ticker('SPY').history(period='60d', interval='5m')
    spy_w_map, _ = build_spy_intraday_map(spy_5m)

    rows = []
    for t in core_tickers:
        v1 = run_old_5m_sim(t, data_5m[t], data_1d[t], mode='ex_ante_accum')
        v2 = run_v2_5m_sim(t, data_5m[t], data_1d[t], mode='ex_ante_accum')
        v3 = run_v3_audit_tuned(t, data_5m[t], data_1d[t], spy_1d, mode='ex_ante_accum')
        v4 = run_v4_production(t, data_5m[t], data_1d[t], spy_1d, mode='ex_ante_accum')
        v5 = run_v5_production(t, data_5m[t], data_1d[t], spy_1d, spy_5m=spy_5m, spy_waterfall_map=spy_w_map, mode='ex_ante_accum')
        rows.append(get_metrics(v1, f"{t} [V1 - Old Baseline]"))
        rows.append(get_metrics(v2, f"{t} [V2 - 4 Fixes (Pre-Audit)]"))
        rows.append(get_metrics(v3, f"{t} [V3 - Audit-Tuned]"))
        rows.append(get_metrics(v4, f"{t} [V4 - Prod + EMA12 Runner]"))
        rows.append(get_metrics(v5, f"{t} [V5 - Final (NoPreER+SPYGate)]"))

    for mode_label, mode_key in [('Accum Regime', 'ex_ante_accum'), ('No Tailwind (Dist)', 'non_accum'), ('All 60d (Unfiltered)', 'all')]:
        p1 = pd.concat([run_old_5m_sim(t, data_5m[t], data_1d[t], mode=mode_key) for t in core_tickers])
        p2 = pd.concat([run_v2_5m_sim(t, data_5m[t], data_1d[t], mode=mode_key) for t in core_tickers])
        p3 = pd.concat([run_v3_audit_tuned(t, data_5m[t], data_1d[t], spy_1d, mode=mode_key) for t in core_tickers])
        p4 = pd.concat([run_v4_production(t, data_5m[t], data_1d[t], spy_1d, mode=mode_key) for t in core_tickers])
        p5 = pd.concat([run_v5_production(t, data_5m[t], data_1d[t], spy_1d, spy_5m=spy_5m, spy_waterfall_map=spy_w_map, mode=mode_key) for t in core_tickers])
        rows.append(get_metrics(p1, f"CORE-3 POOL [V1 Old - {mode_label}]"))
        rows.append(get_metrics(p2, f"CORE-3 POOL [V2 Pre-Audit - {mode_label}]"))
        rows.append(get_metrics(p3, f"CORE-3 POOL [V3 Audit-Tuned - {mode_label}]"))
        rows.append(get_metrics(p4, f"CORE-3 POOL [V4 Prod Runner - {mode_label}]"))
        rows.append(get_metrics(p5, f"CORE-3 POOL [V5 Final Engine - {mode_label}]"))

    print("=== EVOLUTION TABLE (CORE 3: FISV, PLNT, NU): V1 vs V2 vs V3 vs V4 vs V5 ===")
    print(pd.DataFrame(rows).to_string(index=False))

    oos_rows = []
    for t in all_tickers:
        v4_t = run_v4_production(t, data_5m[t], data_1d[t], spy_1d, mode='ex_ante_accum')
        v5_t = run_v5_production(t, data_5m[t], data_1d[t], spy_1d, spy_5m=spy_5m, spy_waterfall_map=spy_w_map, mode='ex_ante_accum')
        oos_rows.append(get_metrics(v4_t, f"{t} [V4 Prod Runner - Accum]"))
        oos_rows.append(get_metrics(v5_t, f"{t} [V5 Final Engine - Accum]"))

    for ver_label, fn in [
        ('V1 Old Baseline', lambda t, m: run_old_5m_sim(t, data_5m[t], data_1d[t], mode=m)),
        ('V2 Pre-Audit', lambda t, m: run_v2_5m_sim(t, data_5m[t], data_1d[t], mode=m)),
        ('V3 Audit-Tuned', lambda t, m: run_v3_audit_tuned(t, data_5m[t], data_1d[t], spy_1d, mode=m)),
        ('V4 Prod Runner', lambda t, m: run_v4_production(t, data_5m[t], data_1d[t], spy_1d, mode=m)),
        ('V5 Final Engine', lambda t, m: run_v5_production(t, data_5m[t], data_1d[t], spy_1d, spy_5m=spy_5m, spy_waterfall_map=spy_w_map, mode=m)),
    ]:
        dfs = [fn(t, 'ex_ante_accum') for t in all_tickers]
        p6_acc = pd.concat([d for d in dfs if not d.empty], ignore_index=True)
        oos_rows.append(get_metrics(p6_acc, f"EXPANDED-6 POOL [{ver_label} - Accum]"))

    print("\n=== EXPANDED 6-TICKER UNIVERSE (FISV, PLNT, NU, CNC, EEFT, GPN): V1 vs V2 vs V3 vs V4 vs V5 ===")
    print(pd.DataFrame(oos_rows).to_string(index=False))

    print("\n=== V5 FINAL PRODUCTION TRADE LOG (Expanded 6-Ticker Accumulation Regime) ===")
    v5_dfs = [run_v5_production(t, data_5m[t], data_1d[t], spy_1d, spy_5m=spy_5m, spy_waterfall_map=spy_w_map, mode='ex_ante_accum') for t in all_tickers]
    v5_accum = pd.concat([d for d in v5_dfs if not d.empty], ignore_index=True)
    display_cols = ['Ticker', 'Date', 'EntryTime', 'ExitTime', 'EntryPrice', 'ExitPrice', 'RetPct', 'Reason']
    v5_disp = v5_accum[display_cols].copy()
    v5_disp['IRA_150k_PnL($)'] = (150000.0 * (v5_disp['RetPct'] / 100.0)).round(0)
    print(v5_disp.sort_values(['Ticker', 'Date']).round(3).to_string(index=False))
