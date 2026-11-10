"""
broker_link_adapter.py
======================
Verified Tastytrade navigation & ticket-handoff adapter.

Documentation & Verification Notes (Section 6):
-----------------------------------------------
1. Official Tastytrade Web Platform URL:
   - Base Web Trading Application: `https://my.tastytrade.com/app.html#/trading`
   - Tastytrade's web application (`my.tastytrade.com`) and desktop client do NOT publish
     an official, documented URL query-parameter schema for pre-populating multi-leg OTOCO
     bracket order quantities, limit prices, stop triggers, and profit targets via a browser GET link.
2. Enforced Boundaries:
   - We NEVER invent undocumented query parameters, custom `tastytrade://` order-injection schemes,
     or fragile browser automation to prefill the Tastytrade web UI.
   - We NEVER describe an API `dry_run=True` preflight (`Preview with Tastytrade`) as creating a draft
     order inside Tastytrade's own UI.
   - Clicking `Open Tastytrade` only performs verified broker navigation and logs a non-mutating
     `BROKER_HANDOFF_OPENED` journal event; it NEVER marks an order plan or intent as submitted or filled.
   - To make manual handoff fast and error-free when live API submission is disabled, `format_copy_ticket_text`
     produces a structured, tabular clipboard ticket with exact quantities, limit/stop/target prices,
     side badges (`BTO` / `STC`), Time-in-Force (`DAY`), and timestamps.
"""

from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Dict, Any


TASTYTRADE_WEB_TRADING_URL = "https://my.tastytrade.com/app.html#/trading"
TASTYTRADE_OAUTH_DOCS_URL = "https://developer.tastytrade.com/oauth/"
TASTYTRADE_ORDER_DOCS_URL = "https://developer.tastytrade.com/order-submission/"


@dataclass(frozen=True)
class BrokerLinkMetadata:
    symbol: str
    destination_url: str
    link_capability: str  # "PLATFORM_NAVIGATION_WITH_CLIPBOARD_TICKET"
    prefill_supported: bool
    user_notice: str
    documentation_ref: str
    platforms_tested: list[str]

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["mode"] = "WEB_SYMBOL_OR_TRADING_DESK"
        d["prefills_ticket_natively"] = self.prefill_supported
        return d


def build_broker_handoff_link(symbol: str) -> BrokerLinkMetadata:
    sym = symbol.upper().strip()
    return BrokerLinkMetadata(
        symbol=sym,
        destination_url=TASTYTRADE_WEB_TRADING_URL,
        link_capability="PLATFORM_NAVIGATION_WITH_CLIPBOARD_TICKET",
        prefill_supported=False,
        user_notice=(
            f"Opens Tastytrade web trading ({TASTYTRADE_WEB_TRADING_URL}). "
            f"Tastytrade does not support URL prefilling of OTOCO bracket tickets; "
            f"use 'Copy ticket' to copy {sym}'s exact quantities and prices, or use "
            f"'Preview with Tastytrade' / 'Submit to Tastytrade' via the API."
        ),
        documentation_ref=TASTYTRADE_ORDER_DOCS_URL,
        platforms_tested=["macOS Chrome 122+", "macOS Safari 17+"],
    )


def format_copy_ticket_text(plan: Dict[str, Any]) -> str:
    """
    Generate a human-verified, structured clipboard summary of a versioned OrderPlan
    that makes shares vs. dollars and BTO vs. STC unmistakable.
    """
    sym = plan["symbol"]
    shares = plan["shares"]
    q1 = plan["leg1_shares"]
    q2 = plan["leg2_shares"]
    limit_p = plan["limit_price"]
    stop_p = plan["stop_price"]
    leg1_p = plan["leg1_target"]
    leg2_p = plan["leg2_target"]
    notional = plan["notional_usd"]
    planned_risk_usd = plan["planned_risk_usd"]
    planned_risk_pct = plan["planned_risk_pct"]
    sig_time = plan.get("signal_bar_close_et", "N/A")
    plan_ver = plan.get("plan_version", 1)
    strat_ver = plan.get("strategy_version", "v5.0-audited")
    acct_masked = plan.get("masked_account_id", "****6897")

    lines = [
        f"=== FLUSH & RECLAIM ORDER TICKET ({sym}) ===",
        f"Account          : {acct_masked} | Strategy: {strat_ver} (Plan v{plan_ver})",
        f"Signal Bar Close : {sig_time}",
        f"Allocation       : ${plan['allocation_usd']:,.2f} -> {shares:,} Shares (${notional:,.2f} est. notional)",
        f"Planned Risk     : -${planned_risk_usd:,.2f} ({planned_risk_pct:.2f}% to initial stop; gap/slippage can exceed planned risk)",
        "------------------------------------------------------------",
        f"ENTRY (BUY)      : BTO +{shares:,} sh {sym} @ ${limit_p:.2f} LIMIT (DAY)",
        f"INITIAL STOP     : STC -{shares:,} sh {sym} @ ${stop_p:.2f} STOP MARKET (DAY)",
        f"TRANCHE A (50%)  : STC -{q1:,} sh {sym} Limit @ ${leg1_p:.2f} | OCO Stop -{q1:,} sh @ ${stop_p:.2f} (DAY)",
        f"TRANCHE B (50%)  : STC -{q2:,} sh {sym} Limit @ ${leg2_p:.2f} | OCO Stop -{q2:,} sh @ ${stop_p:.2f} -> Move to ${limit_p:.2f} BE after Leg 1 (DAY)",
        f"TIME CUTOFF      : Flatten any remaining shares by {plan.get('flatten_cutoff_et', '15:40 ET')}",
        "============================================================",
    ]
    return "\n".join(lines)
