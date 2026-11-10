"""
earnings_oracle.py
==================
Single shared earnings oracle used by historical replay, universe_scanner, and live HUD.
Tracks earnings date, source, observation timestamp, and status (CONFIRMED | ESTIMATED | UNKNOWN).
Never formats unknown earnings as ">14d".
"""

from __future__ import annotations
from dataclasses import dataclass, asdict
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple
import pandas as pd


@dataclass(frozen=True)
class EarningsEvent:
    symbol: str
    earnings_date: Optional[date]
    status: str  # "CONFIRMED", "ESTIMATED", or "UNKNOWN"
    source: str
    observed_at_et: str
    schedule_provenance: str = "RECONSTRUCTED_POST_HOC"  # or "POINT_IN_TIME_SNAPSHOT"
    notes: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.earnings_date is not None:
            d["earnings_date"] = self.earnings_date.isoformat()
        return d


@dataclass(frozen=True)
class EarningsCheckResult:
    symbol: str
    as_of_date: date
    status: str  # "CONFIRMED", "ESTIMATED", "UNKNOWN"
    next_earnings_date: Optional[date]
    days_until_earnings: Optional[int]
    is_blocked: bool
    display_label: str
    source: str
    observed_at_et: str
    schedule_provenance: str


# Documented reconciliation of the legacy V5 conflicting maps:
# - FISV: test_regimes had 2027-02-04, universe_scanner had 2027-02-09 -> Reconciled to 2027-02-04 (ESTIMATED)
# - EEFT: test_regimes had 2027-02-11, universe_scanner had 2027-02-10 -> Reconciled to 2027-02-10 (ESTIMATED)
# - GPN : test_regimes had 2026-11-05, universe_scanner had 2026-11-02 -> Reconciled to 2026-11-05 (CONFIRMED Q3 release)
RECONCILED_EARNINGS_REGISTRY: Dict[str, List[EarningsEvent]] = {
    "FISV": [
        EarningsEvent("FISV", date(2026, 11, 3), "CONFIRMED", "reconciled_registry", "2026-10-01 08:00:00 EST", "RECONSTRUCTED_POST_HOC", "Q3 2026 release"),
        EarningsEvent("FISV", date(2027, 2, 4), "ESTIMATED", "reconciled_registry (was 2027-02-04 in test_regimes vs 2027-02-09 in scanner)", "2026-11-04 08:00:00 EST", "RECONSTRUCTED_POST_HOC", "Q4 2026 estimated"),
    ],
    "PLNT": [
        EarningsEvent("PLNT", date(2026, 11, 5), "CONFIRMED", "reconciled_registry", "2026-10-01 08:00:00 EST", "RECONSTRUCTED_POST_HOC", "Q3 2026 release"),
        EarningsEvent("PLNT", date(2027, 2, 23), "ESTIMATED", "reconciled_registry", "2026-11-06 08:00:00 EST", "RECONSTRUCTED_POST_HOC", "Q4 2026 estimated"),
    ],
    "NU": [
        EarningsEvent("NU", date(2026, 8, 13), "CONFIRMED", "reconciled_registry", "2026-07-15 08:00:00 EST", "RECONSTRUCTED_POST_HOC", "Q2 2026 release"),
        EarningsEvent("NU", date(2026, 11, 12), "CONFIRMED", "reconciled_registry", "2026-10-01 08:00:00 EST", "RECONSTRUCTED_POST_HOC", "Q3 2026 release"),
        EarningsEvent("NU", date(2027, 2, 18), "ESTIMATED", "reconciled_registry", "2026-11-09 08:00:00 EST", "RECONSTRUCTED_POST_HOC", "Q4 2026 estimated"),
    ],
    "CNC": [
        EarningsEvent("CNC", date(2026, 10, 27), "CONFIRMED", "reconciled_registry", "2026-09-25 08:00:00 EST", "RECONSTRUCTED_POST_HOC", "Q3 2026 release"),
        EarningsEvent("CNC", date(2027, 2, 2), "ESTIMATED", "reconciled_registry", "2026-10-28 08:00:00 EST", "RECONSTRUCTED_POST_HOC", "Q4 2026 estimated"),
    ],
    "EEFT": [
        EarningsEvent("EEFT", date(2026, 10, 27), "CONFIRMED", "reconciled_registry", "2026-09-25 08:00:00 EST", "RECONSTRUCTED_POST_HOC", "Q3 2026 release"),
        EarningsEvent("EEFT", date(2027, 2, 10), "ESTIMATED", "reconciled_registry (was 2027-02-11 in test_regimes vs 2027-02-10 in scanner)", "2026-10-28 08:00:00 EST", "RECONSTRUCTED_POST_HOC", "Q4 2026 estimated"),
    ],
    "GPN": [
        EarningsEvent("GPN", date(2026, 11, 5), "CONFIRMED", "reconciled_registry (was 2026-11-05 in test_regimes vs 2026-11-02 in scanner)", "2026-10-01 08:00:00 EST", "RECONSTRUCTED_POST_HOC", "Q3 2026 release"),
        EarningsEvent("GPN", date(2027, 2, 10), "ESTIMATED", "reconciled_registry", "2026-11-06 08:00:00 EST", "RECONSTRUCTED_POST_HOC", "Q4 2026 estimated"),
    ],
}


class EarningsOracle:
    """
    Unified earnings schedule lookup.
    """

    def __init__(
        self,
        registry: Optional[Dict[str, List[EarningsEvent]]] = None,
        blackout_days: int = 14,
        unknown_policy: str = "block",
    ):
        self.registry: Dict[str, List[EarningsEvent]] = {}
        base = registry if registry is not None else RECONCILED_EARNINGS_REGISTRY
        for sym, events in base.items():
            self.registry[sym.upper()] = sorted(
                events, key=lambda e: e.earnings_date or date.max
            )
        self.blackout_days = blackout_days
        if unknown_policy not in ("block", "allow"):
            raise ValueError(f"Invalid unknown_policy: {unknown_policy}")
        self.unknown_policy = unknown_policy

    def register_event(self, event: EarningsEvent) -> None:
        sym = event.symbol.upper()
        existing = self.registry.get(sym, [])
        existing.append(event)
        self.registry[sym] = sorted(
            existing, key=lambda e: e.earnings_date or date.max
        )

    def ingest_yfinance_info(
        self,
        symbol: str,
        info: dict,
        observed_at_et: Optional[str] = None,
    ) -> Optional[EarningsEvent]:
        """
        Parse earnings timestamp from a yfinance `.info` dict if present.
        Never fabricates '>14d' when missing; records UNKNOWN if absent.
        """
        sym = symbol.upper()
        obs = observed_at_et or datetime.now().strftime("%Y-%m-%d %H:%M:%S EST")
        er_ts = info.get("earningsTimestamp") or info.get("earningsTimestampStart")
        if er_ts:
            try:
                er_dt = pd.to_datetime(er_ts, unit="s").date()
                is_est = bool(info.get("isEarningsDateEstimate", False))
                status = "ESTIMATED" if is_est else "CONFIRMED"
                ev = EarningsEvent(
                    symbol=sym,
                    earnings_date=er_dt,
                    status=status,
                    source="yfinance.info",
                    observed_at_et=obs,
                    schedule_provenance="POINT_IN_TIME_SNAPSHOT",
                )
                self.register_event(ev)
                return ev
            except Exception:
                pass
        return None

    def check_blackout(
        self,
        symbol: str,
        as_of_date: date | datetime,
        use_filter: bool = True,
    ) -> EarningsCheckResult:
        if isinstance(as_of_date, datetime):
            as_of_date = as_of_date.date()

        sym = symbol.upper()
        events = self.registry.get(sym, [])
        upcoming = [
            e for e in events
            if e.earnings_date is not None and e.earnings_date >= as_of_date
        ]

        if not upcoming:
            # Unknown upcoming earnings! Never label as ">14d".
            blocked = bool(use_filter and self.unknown_policy == "block")
            return EarningsCheckResult(
                symbol=sym,
                as_of_date=as_of_date,
                status="UNKNOWN",
                next_earnings_date=None,
                days_until_earnings=None,
                is_blocked=blocked,
                display_label="UNKNOWN (blocked)" if blocked else "UNKNOWN (unverified)",
                source="none",
                observed_at_et="N/A",
                schedule_provenance="UNKNOWN",
            )

        next_ev = upcoming[0]
        if next_ev.earnings_date is None:
            raise ValueError(f"Expected non-null earnings_date for {sym}")
        days_to_er = (next_ev.earnings_date - as_of_date).days
        in_blackout = 0 <= days_to_er <= self.blackout_days
        blocked = bool(use_filter and in_blackout)
        label = f"{next_ev.earnings_date.isoformat()} ({days_to_er}d, {next_ev.status})"
        return EarningsCheckResult(
            symbol=sym,
            as_of_date=as_of_date,
            status=next_ev.status,
            next_earnings_date=next_ev.earnings_date,
            days_until_earnings=days_to_er,
            is_blocked=blocked,
            display_label=label,
            source=next_ev.source,
            observed_at_et=next_ev.observed_at_et,
            schedule_provenance=next_ev.schedule_provenance,
        )
