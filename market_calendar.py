"""
market_calendar.py
==================
NYSE market calendar utility handling regular sessions, market holidays,
early-close sessions (13:00 ET close / 12:40 ET flatten), DST transitions,
and causal prior-completed-session lookups.
"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo
import pandas as pd

try:
    import pandas_market_calendars as mcal
    _NYSE_CAL = mcal.get_calendar("NYSE")
except Exception:
    _NYSE_CAL = None

NY_TZ = ZoneInfo("America/New_York")

# Deterministic NYSE Holiday & Early-Close Fallback / Verification Tables (2025-2027)
NYSE_HOLIDAYS_FALLBACK = {
    date(2025, 1, 1), date(2025, 1, 9), date(2025, 1, 20), date(2025, 2, 17),
    date(2025, 4, 18), date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4),
    date(2025, 9, 1), date(2025, 11, 27), date(2025, 12, 25),
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
}

NYSE_EARLY_CLOSES_FALLBACK = {
    date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24),
    date(2026, 11, 27), date(2026, 12, 24),
    date(2027, 11, 26),
}


@dataclass(frozen=True)
class SessionSchedule:
    session_date: date
    is_trading_day: bool
    is_early_close: bool
    market_open_et: Optional[datetime]
    market_close_et: Optional[datetime]
    entry_start_et: Optional[datetime]
    entry_end_et: Optional[datetime]
    mandatory_flatten_et: Optional[datetime]


class NYSECalendar:
    """
    Deterministic NYSE session calendar with caching.
    """

    def __init__(self):
        self._schedule_cache: Dict[date, SessionSchedule] = {}

    def get_session_schedule(self, d: date) -> SessionSchedule:
        if isinstance(d, datetime):
            d = d.astimezone(NY_TZ).date() if d.tzinfo else d.date()
        if d in self._schedule_cache:
            return self._schedule_cache[d]

        # Check weekend first
        if d.weekday() >= 5:
            sched = SessionSchedule(d, False, False, None, None, None, None, None)
            self._schedule_cache[d] = sched
            return sched

        is_open = True
        is_early = False
        open_dt = datetime.combine(d, time(9, 30), tzinfo=NY_TZ)
        close_dt = datetime.combine(d, time(16, 0), tzinfo=NY_TZ)

        if _NYSE_CAL is not None:
            try:
                s = _NYSE_CAL.schedule(start_date=d.isoformat(), end_date=d.isoformat())
                if s.empty:
                    is_open = False
                else:
                    open_dt = s.iloc[0]["market_open"].tz_convert(NY_TZ).to_pydatetime()
                    close_dt = s.iloc[0]["market_close"].tz_convert(NY_TZ).to_pydatetime()
                    is_early = (close_dt.hour < 16)
            except Exception:
                is_open = d not in NYSE_HOLIDAYS_FALLBACK
                is_early = d in NYSE_EARLY_CLOSES_FALLBACK
                if is_early:
                    close_dt = datetime.combine(d, time(13, 0), tzinfo=NY_TZ)
        else:
            is_open = d not in NYSE_HOLIDAYS_FALLBACK
            is_early = d in NYSE_EARLY_CLOSES_FALLBACK
            if is_early:
                close_dt = datetime.combine(d, time(13, 0), tzinfo=NY_TZ)

        if not is_open:
            sched = SessionSchedule(d, False, False, None, None, None, None, None)
        else:
            entry_start = datetime.combine(d, time(9, 50), tzinfo=NY_TZ)
            if is_early:
                # On 13:00 ET early-close sessions, mandatory flatten is 12:40 ET
                # and entry window ends at 12:00 ET (at least 40m before flatten).
                entry_end = datetime.combine(d, time(12, 0), tzinfo=NY_TZ)
                flatten_dt = datetime.combine(d, time(12, 40), tzinfo=NY_TZ)
            else:
                entry_end = datetime.combine(d, time(13, 0), tzinfo=NY_TZ)
                flatten_dt = datetime.combine(d, time(15, 40), tzinfo=NY_TZ)
            sched = SessionSchedule(
                session_date=d,
                is_trading_day=True,
                is_early_close=is_early,
                market_open_et=open_dt,
                market_close_et=close_dt,
                entry_start_et=entry_start,
                entry_end_et=entry_end,
                mandatory_flatten_et=flatten_dt,
            )

        self._schedule_cache[d] = sched
        return sched

    def is_trading_day(self, d: date) -> bool:
        return self.get_session_schedule(d).is_trading_day

    def is_early_close(self, d: date) -> bool:
        return self.get_session_schedule(d).is_early_close

    def session_label(self, d: date) -> str:
        sched = self.get_session_schedule(d)
        if not sched.is_trading_day:
            if d.weekday() >= 5:
                return f"NYSE CLOSED (Weekend — {d.isoformat()})"
            return f"NYSE CLOSED (Holiday — {d.isoformat()})"
        if sched.is_early_close:
            return f"EARLY CLOSE SESSION ({d.isoformat()}: 09:30–13:00 ET | Flatten 12:40 ET)"
        return f"REGULAR NYSE SESSION ({d.isoformat()}: 09:30–16:00 ET | Entry 09:50–13:00 ET | Flatten 15:40 ET)"

    def prior_completed_session_date(self, as_of: datetime | date) -> date:
        """
        Return the most recent completed NYSE trading session date strictly prior
        to `as_of`'s session date (unless `as_of` is a datetime after today's market close,
        Wait: for intraday trading on session D, the prior completed daily bar is strictly < D).
        """
        if isinstance(as_of, datetime):
            dt_et = as_of.astimezone(NY_TZ) if as_of.tzinfo else as_of.replace(tzinfo=NY_TZ)
            cur = dt_et.date() - timedelta(days=1)
        else:
            cur = as_of - timedelta(days=1)

        for _ in range(15):
            if self.is_trading_day(cur):
                return cur
            cur -= timedelta(days=1)
        return cur

    def trading_days_between(self, start_date: date, end_date: date) -> List[date]:
        days: List[date] = []
        cur = start_date
        while cur <= end_date:
            if self.is_trading_day(cur):
                days.append(cur)
            cur += timedelta(days=1)
        return days


def normalize_to_ny_tz(df: pd.DataFrame) -> pd.DataFrame:
    """
    Ensure a DataFrame with a DatetimeIndex is timezone-aware in America/New_York,
    including when loaded from CSV across EDT (-04:00) / EST (-05:00) DST boundaries.
    """
    if df.empty:
        return df
    out = df.copy()
    if isinstance(out.index, pd.DatetimeIndex):
        idx = out.index
        if idx.tz is None:
            idx = idx.tz_localize(NY_TZ)
        else:
            idx = idx.tz_convert(NY_TZ)
    else:
        try:
            idx = pd.to_datetime(out.index)
            if idx.tz is None:
                idx = idx.tz_localize(NY_TZ)
            else:
                idx = idx.tz_convert(NY_TZ)
        except ValueError:
            idx = pd.to_datetime(out.index, utc=True).tz_convert(NY_TZ)
    out.index = idx
    return out


def filter_regular_session_bars(df: pd.DataFrame) -> pd.DataFrame:
    """
    Filter 5m bars to NYSE Regular Trading Hours (09:30 to 15:55 ET inclusive).
    """
    if df.empty:
        return df
    out = normalize_to_ny_tz(df)
    hm = out.index.hour * 100 + out.index.minute
    return out[(hm >= 930) & (hm <= 1555)].copy()
