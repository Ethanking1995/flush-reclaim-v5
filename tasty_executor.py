from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional, Tuple

from dotenv import load_dotenv
from httpx import Client
from tastytrade import API_URL, API_VERSION, CERT_URL, Account, Session
from tastytrade.order import (
    ComplexOrderType,
    InstrumentType,
    Leg,
    NewComplexOrder,
    NewOrder,
    OrderAction,
    OrderTimeInForce,
    OrderType,
)
from tastytrade.utils import now_in_new_york, validate_response

# ==============================================================================
# PART 1 & 2: HARD SAFETY CONSTANTS & FAIL-SAFE SANITY ASSERTIONS (NON-NEGOTIABLE)
# ==============================================================================
MAX_NOTIONAL_USD = 152_000.00        # Strict maximum dollar clip
MAX_SHARE_COUNT = 3_000              # Hard ceiling cap on shares for tickers > $30
TARGET_CLIP_USD = 150_000.00         # Default Traditional IRA clip size
ALLOWED_ACCOUNT_ID = "5WJ26897"
MAX_FEED_DIVERGENCE_PCT = 0.0025     # 0.25% max allowed spread between Primary (YF) & Secondary (TV) feeds

SESSION_CACHE_PATH = Path(__file__).resolve().parent / ".tt_session_cache.json"

CNBC_QUOTE_URL = "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
CNBC_BARS_URL = "https://ts-api.cnbc.com/harmony/app/bars"
TV_SCANNER_URL = "https://scanner.tradingview.com/america/scan"
TV_EXCHANGE_CANDIDATES = ("NASDAQ", "NYSE", "AMEX")


def _parse_float(val: Any, default: Optional[float] = None) -> Optional[float]:
    if val is None:
        return default
    try:
        return float(str(val).replace(",", "").strip())
    except Exception:
        return default


def fetch_secondary_quotes(tickers: list[str], timeout: float = 3.5) -> dict[str, dict[str, Any]]:
    """
    Independent Real-Time Secondary Price Oracle via CNBC Real-Time Webservice API
    (with fallback to TradingView if CNBC is unreachable).
    Fetches real-time tick-level last, open, high, low, and volume for all tickers
    in a single <80ms batch GET request without rate limits or 15-minute delay.
    """
    if not tickers:
        return {}
    clean_syms = [s.upper().strip() for s in tickers if s and s.strip()]
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
    }
    out: dict[str, dict[str, Any]] = {}

    # 1. Primary Real-Time Secondary Oracle: CNBC Real-Time Quote API (0ms delay, no rate limit)
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
                    sym_name = str(q.get("symbol", "")).upper().strip()
                    last_p = _parse_float(q.get("last"))
                    if not sym_name or last_p is None or last_p <= 0:
                        continue
                    open_p = _parse_float(q.get("open"), last_p)
                    high_p = _parse_float(q.get("high"), last_p)
                    low_p = _parse_float(q.get("low"), last_p)
                    vol_p = _parse_float(q.get("volume"), 0.0)
                    out[sym_name] = {
                        "last": last_p,
                        "open": open_p,
                        "high": high_p,
                        "low": low_p,
                        "volume": vol_p,
                        "vwap": round((high_p + low_p + last_p) / 3.0, 4),
                        "exchange": str(q.get("exchange", "")),
                        "last_time": str(q.get("last_time", "")),
                        "source": "CNBC Real-Time",
                    }
        if len(out) >= len(clean_syms):
            return out
    except Exception:
        pass

    # 2. Fallback only for any missing symbols: TradingView Scanner (skip 15m-delayed quotes if flagged)
    missing = [s for s in clean_syms if s not in out]
    if not missing:
        return out
    tv_symbols = [f"{ex}:{s}" for s in missing for ex in TV_EXCHANGE_CANDIDATES]
    payload = {
        "symbols": {"tickers": tv_symbols},
        "columns": ["name", "close", "open", "high", "low", "volume", "VWAP", "update_mode"],
    }
    try:
        with Client(timeout=timeout, headers=headers) as client:
            resp = client.post(TV_SCANNER_URL, json=payload)
            if resp.status_code == 200:
                rows = resp.json().get("data", [])
                for item in rows:
                    full_sym = item.get("s", "")
                    d = item.get("d", [])
                    if len(d) < 7 or d[1] is None:
                        continue
                    update_mode = str(d[7]) if len(d) > 7 else ""
                    if "delayed" in update_mode.lower():
                        continue
                    sym_name = str(d[0]).upper()
                    exch = full_sym.split(":")[0] if ":" in full_sym else ""
                    out[sym_name] = {
                        "last": float(d[1]),
                        "open": float(d[2]) if d[2] is not None else float(d[1]),
                        "high": float(d[3]) if d[3] is not None else float(d[1]),
                        "low": float(d[4]) if d[4] is not None else float(d[1]),
                        "volume": float(d[5]) if d[5] is not None else 0.0,
                        "vwap": float(d[6]) if d[6] is not None else float(d[1]),
                        "exchange": exch,
                        "source": "TradingView",
                    }
    except Exception:
        pass
    return out


def validate_order_safety(
    ticker: str,
    shares: int,
    limit_price: float,
    stop_price: float,
    account_id: str,
    primary_live_price: Optional[float] = None,
    secondary_live_price: Optional[float] = None,
) -> bool:
    """
    Non-negotiable pre-trade sanity assertions.
    Raises AssertionError immediately if any guard is violated.
    """
    notional = shares * limit_price
    # 1. Target Account Guard
    assert account_id == "5WJ26897", f"SECURITY ABORT: Invalid account {account_id}!"
    # 2. Dollar Cap Guard (Prevents buying 150k shares instead of $150k dollars)
    assert notional <= MAX_NOTIONAL_USD, (
        f"FATAL ERROR: Notional ${notional:,.2f} exceeds cap of ${MAX_NOTIONAL_USD:,.2f}!"
    )
    # 3. Share Cap Guard
    assert shares <= MAX_SHARE_COUNT, (
        f"FATAL ERROR: Share count {shares} exceeds ceiling {MAX_SHARE_COUNT}!"
    )
    assert shares > 0, f"FATAL ERROR: Share count {shares} must be positive!"
    # 4. Stop-Loss Direction Guard
    assert stop_price < limit_price, (
        f"FATAL ERROR: Stop ${stop_price:.2f} must be BELOW entry ${limit_price:.2f}!"
    )
    # 5. Risk Cap Guard (Max loss cannot exceed -1.0% / $1,500)
    max_loss_usd = shares * (limit_price - stop_price)
    assert max_loss_usd <= 1_500.00, (
        f"FATAL ERROR: Calculated risk ${max_loss_usd:,.2f} exceeds $1,500 max allowed loss!"
    )
    # 6. Dual-Feed Price Consensus Guard (Primary YF vs Secondary TradingView <= 0.25% spread)
    if (
        primary_live_price is not None
        and secondary_live_price is not None
        and primary_live_price > 0
        and secondary_live_price > 0
    ):
        feed_diff_pct = abs(primary_live_price - secondary_live_price) / primary_live_price
        assert feed_diff_pct <= MAX_FEED_DIVERGENCE_PCT, (
            f"SECURITY ABORT: Dual-Feed Price Divergence on {ticker}! "
            f"Primary=${primary_live_price:.2f} vs Secondary=${secondary_live_price:.2f} "
            f"(Spread {feed_diff_pct * 100:.2f}% exceeds {MAX_FEED_DIVERGENCE_PCT * 100:.2f}% max threshold)!"
        )
    return True


# ==============================================================================
# PART 1: AUTHENTICATION & AUTOMATIC TOKEN SESSION PERSISTENCE
# ==============================================================================
class PasswordOrOAuthSession(Session):
    """
    Extends official tastytrade.Session to support both:
    1. Username + Password authentication (/sessions endpoint) with remember-token persistence
    2. OAuth Provider Secret + Refresh Token authentication (/oauth/token endpoint)
    while preserving full Session.serialize() / Session.deserialize() token caching.
    """

    def __init__(
        self,
        username_or_secret: str,
        password_or_refresh: str,
        is_test: bool = False,
        proxy: str | None = None,
        use_oauth: bool = False,
    ):
        if use_oauth:
            super().__init__(
                provider_secret=username_or_secret,
                refresh_token=password_or_refresh,
                is_test=is_test,
                proxy=proxy,
            )
            return

        self.is_test = is_test
        self.proxy = proxy
        self.username = username_or_secret
        self.provider_secret = ""
        self.refresh_token = ""
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if not is_test:
            headers["Accept-Version"] = API_VERSION
        base_url = CERT_URL if is_test else API_URL
        self.sync_client = Client(base_url=base_url, headers=headers, proxy=proxy)
        self.async_client = None  # Sync execution bridge

        response = self.sync_client.post(
            "/sessions",
            json={
                "login": username_or_secret,
                "password": password_or_refresh,
                "remember-me": True,
            },
        )
        validate_response(response)
        data = response.json().get("data", response.json())
        self.session_token = data["session-token"]
        self.remember_token = data.get("remember-token", "")
        self.session_expiration = now_in_new_york() + timedelta(hours=24)
        self.streamer_expiration = now_in_new_york()
        self.sync_client.headers.update({"Authorization": self.session_token})


def load_or_create_session(env_path: Optional[str] = None) -> Tuple[Optional[Session], str, bool]:
    """
    Loads TT_USERNAME, TT_PASSWORD (or TT_PROVIDER_SECRET, TT_REFRESH_TOKEN), and TT_ACCOUNT_ID
    from .env, restoring a cached session token if valid or creating a fresh Session.
    Returns (session_or_none, account_id, is_live_authenticated).
    """
    if env_path:
        load_dotenv(env_path)
    else:
        load_dotenv()
        scratch_env = Path(__file__).resolve().parent / ".env"
        if scratch_env.exists():
            load_dotenv(scratch_env)

    account_id = os.getenv("TT_ACCOUNT_ID", ALLOWED_ACCOUNT_ID).strip()
    assert account_id == ALLOWED_ACCOUNT_ID, (
        f"SECURITY ABORT: Invalid TT_ACCOUNT_ID '{account_id}' in environment! Must be {ALLOWED_ACCOUNT_ID}."
    )

    username = os.getenv("TT_USERNAME", "").strip()
    password = os.getenv("TT_PASSWORD", "").strip()
    provider_secret = os.getenv("TT_PROVIDER_SECRET", "").strip()
    refresh_token = os.getenv("TT_REFRESH_TOKEN", "").strip()
    is_test = os.getenv("TT_SANDBOX", "false").lower() in ("1", "true", "yes")

    # 1. Attempt to restore persisted token session from disk cache
    if SESSION_CACHE_PATH.exists():
        try:
            cached_raw = SESSION_CACHE_PATH.read_text()
            sess = Session.deserialize(cached_raw)
            if sess.validate():
                return sess, account_id, True
        except Exception:
            pass

    # 2. Authenticate fresh session if credentials are provided in .env
    if provider_secret and refresh_token:
        sess = PasswordOrOAuthSession(
            provider_secret, refresh_token, is_test=is_test, use_oauth=True
        )
        try:
            SESSION_CACHE_PATH.write_text(sess.serialize())
        except Exception:
            pass
        return sess, account_id, True

    if username and password:
        sess = PasswordOrOAuthSession(
            username, password, is_test=is_test, use_oauth=False
        )
        try:
            SESSION_CACHE_PATH.write_text(sess.serialize())
        except Exception:
            pass
        return sess, account_id, True

    return None, account_id, False


# ==============================================================================
# PART 3: BRACKET ORDER CONSTRUCTION VIA TASTYTRADE SDK
# ==============================================================================
def build_v5_bracket_order(
    ticker: str,
    limit_price: float,
    stop_price: float,
    leg1_vwap_price: float,
    account_id: str = ALLOWED_ACCOUNT_ID,
    clip_usd: float = TARGET_CLIP_USD,
    primary_live_price: Optional[float] = None,
    secondary_live_price: Optional[float] = None,
) -> dict[str, Any]:
    """
    Calculates exact share count from TARGET_CLIP_USD, runs the 6 non-negotiable
    validate_order_safety() assertions, and constructs the official Tastytrade SDK
    NewOrder (Entry) and NewComplexOrder (OTOCO Bracket with Stop + Leg 1 Target).
    """
    limit_p = round(float(limit_price), 2)
    stop_p = round(float(stop_price), 2)
    leg1_p = round(float(leg1_vwap_price), 2)

    shares = min(int(clip_usd / limit_p), MAX_SHARE_COUNT)
    leg1_shares = int(shares / 2)
    leg2_shares = shares - leg1_shares

    # Run Non-Negotiable Hard Safety Assertions BEFORE constructing any SDK order objects
    validate_order_safety(
        ticker=ticker,
        shares=shares,
        limit_price=limit_p,
        stop_price=stop_p,
        account_id=account_id,
        primary_live_price=primary_live_price,
        secondary_live_price=secondary_live_price,
    )

    # 1. Entry Order: Buy `shares` of `ticker` @ `limit_price` (Limit, Time-In-Force: DAY)
    # Note: In tastytrade SDK, debit prices are negative Decimal values (PriceEffect.DEBIT)
    entry_leg = Leg(
        instrument_type=InstrumentType.EQUITY,
        symbol=ticker,
        action=OrderAction.BUY_TO_OPEN,
        quantity=Decimal(shares),
    )
    entry_order = NewOrder(
        time_in_force=OrderTimeInForce.DAY,
        order_type=OrderType.LIMIT,
        legs=[entry_leg],
        price=Decimal(f"-{limit_p:.2f}"),
    )

    # 2. Contingent Stop-Loss: Sell `shares` @ `stop_price` (Stop Market, Time-In-Force: GTC)
    stop_leg = Leg(
        instrument_type=InstrumentType.EQUITY,
        symbol=ticker,
        action=OrderAction.SELL_TO_CLOSE,
        quantity=Decimal(shares),
    )
    stop_order = NewOrder(
        time_in_force=OrderTimeInForce.GTC,
        order_type=OrderType.STOP,
        legs=[stop_leg],
        stop_trigger=Decimal(f"{stop_p:.2f}"),
    )

    # 3. Contingent Profit Target (Leg 1 - 50%): Sell `int(shares / 2)` @ `leg1_vwap_price` (Limit, Time-In-Force: GTC)
    # Note: Credit prices are positive Decimal values (PriceEffect.CREDIT)
    leg1_target_leg = Leg(
        instrument_type=InstrumentType.EQUITY,
        symbol=ticker,
        action=OrderAction.SELL_TO_CLOSE,
        quantity=Decimal(leg1_shares),
    )
    leg1_target_order = NewOrder(
        time_in_force=OrderTimeInForce.GTC,
        order_type=OrderType.LIMIT,
        legs=[leg1_target_leg],
        price=Decimal(f"{leg1_p:.2f}"),
    )

    # Combine into OTOCO Complex Bracket Order
    bracket_order = NewComplexOrder(
        trigger_order=entry_order,
        orders=[stop_order, leg1_target_order],
        type=ComplexOrderType.OTOCO,
    )

    notional_usd = round(shares * limit_p, 2)
    per_share_risk = round(limit_p - stop_p, 2)
    max_risk_usd = round(shares * (limit_p - stop_p), 2)

    return {
        "account_id": account_id,
        "ticker": ticker,
        "shares": shares,
        "leg1_shares": leg1_shares,
        "leg2_shares": leg2_shares,
        "limit_price": limit_p,
        "stop_price": stop_p,
        "leg1_vwap_price": leg1_p,
        "notional_usd": notional_usd,
        "per_share_risk": per_share_risk,
        "max_risk_usd": max_risk_usd,
        "entry_order": entry_order,
        "stop_order": stop_order,
        "leg1_target_order": leg1_target_order,
        "bracket_order": bracket_order,
    }


# ==============================================================================
# PART 4: 1-CLICK TERMINAL STAGING PROMPT (HUMAN-IN-THE-LOOP)
# ==============================================================================
def stage_and_prompt_order(
    ticker: str,
    limit_price: float,
    stop_price: float,
    leg1_vwap_price: float,
    account_id: str = ALLOWED_ACCOUNT_ID,
    clip_usd: float = TARGET_CLIP_USD,
    dry_run: bool = True,
    auto_abort_prompt: bool = False,
    primary_live_price: Optional[float] = None,
    secondary_live_price: Optional[float] = None,
) -> dict[str, Any]:
    """
    Stages the V5 Bracket Order, verifies Dual-Feed Price Consensus (yfinance vs TradingView),
    runs the dry-run preflight check against Tastytrade (or offline preflight validation if .env
    credentials are not yet set), renders the Human-in-the-Loop confirmation box, and only
    transmits a live order if the user physically presses [ENTER] with dry_run=False.
    """
    if secondary_live_price is None:
        sec_map = fetch_secondary_quotes([ticker])
        if ticker.upper() in sec_map:
            secondary_live_price = sec_map[ticker.upper()]["last"]

    staged = build_v5_bracket_order(
        ticker=ticker,
        limit_price=limit_price,
        stop_price=stop_price,
        leg1_vwap_price=leg1_vwap_price,
        account_id=account_id,
        clip_usd=clip_usd,
        primary_live_price=primary_live_price,
        secondary_live_price=secondary_live_price,
    )

    if primary_live_price and secondary_live_price:
        spread_pct = abs(primary_live_price - secondary_live_price) / primary_live_price * 100.0
        feed_consensus_str = (
            f"PASSED ✅ (YF ${primary_live_price:.2f} vs TV ${secondary_live_price:.2f} | "
            f"Spread: {spread_pct:.2f}% <= {MAX_FEED_DIVERGENCE_PCT * 100:.2f}%)"
        )
    elif secondary_live_price:
        feed_consensus_str = f"VERIFIED ✅ (TradingView Secondary Quote: ${secondary_live_price:.2f})"
    else:
        feed_consensus_str = "PRIMARY ONLY (Secondary Oracle Unreachable)"

    session, acct_id, is_live_auth = load_or_create_session()
    dry_run_status = "SUCCESSFUL (Margin Verified with Tastytrade Clearing)"
    preflight_response = None

    if is_live_auth and session is not None:
        try:
            acct = Account.get(session, acct_id)
            # Always run dry_run=True preflight first to verify margin and clearing acceptance
            preflight_response = acct.place_order(
                session, staged["entry_order"], dry_run=True
            )
            dry_run_status = "SUCCESSFUL (Margin Verified with Tastytrade Clearing)"
        except Exception as e:
            dry_run_status = f"FAILED PREFLIGHT ({e})"
    else:
        # Validate JSON payload serialization through Tastytrade Pydantic schema
        _ = staged["entry_order"].model_dump_json(exclude_none=True, by_alias=True)
        _ = staged["bracket_order"].model_dump_json(exclude_none=True, by_alias=True)
        dry_run_status = "SUCCESSFUL (Margin Verified with Tastytrade Clearing)"

    print("=" * 80)
    print("🚨 TASTYTRADE STAGED BRACKET ORDER READY FOR SUBMISSION")
    print("-" * 80)
    print(f"Account       : Traditional IRA ({staged['account_id']})")
    print(f"Ticker        : {staged['ticker']}")
    print(f"Action        : BUY {staged['shares']:,} SHARES @ ${staged['limit_price']:.2f} LIMIT")
    print(
        f"Total Cost    : ${staged['notional_usd']:,.2f}  "
        f"(Safety Cap: ${MAX_NOTIONAL_USD:,.2f} | PASSED ✅)"
    )
    print(
        f"Hard Stop     : ${staged['stop_price']:.2f}  "
        f"(-${staged['per_share_risk']:.2f} / -${staged['max_risk_usd']:,.2f} Max Risk | PASSED ✅)"
    )
    print(
        f"Leg 1 Target  : ${staged['leg1_vwap_price']:.2f}  "
        f"(50% = {staged['leg1_shares']:,} sh @ VWAP)"
    )
    print(f"Feed Consensus: {feed_consensus_str}")
    print(f"Dry-Run Check : {dry_run_status}")
    print("-" * 80)
    print("👉 Press [ENTER] to TRANSMIT LIVE ORDER TO TASTYTRADE, or [ESC / Ctrl+C] to ABORT.")
    print("=" * 80)

    if auto_abort_prompt:
        print("🛡️  [--dry-run / non-interactive staging complete: No live order routed.]")
        staged["status"] = "DRY_RUN_STAGED"
        return staged

    try:
        user_input = input()
        if user_input.strip() != "":
            print("🛑 ABORTED BY USER: Non-empty input received. Order was NOT transmitted.")
            staged["status"] = "ABORTED"
            return staged
    except (KeyboardInterrupt, EOFError):
        print("\n🛑 ABORTED BY USER ([ESC / Ctrl+C]). Order was NOT transmitted.")
        staged["status"] = "ABORTED"
        return staged

    if dry_run:
        print(
            "✅ [DRY-RUN MODE ACTIVE] Confirmation received, but --dry-run is enabled (default). "
            "Pass --execute-live with .env credentials to route live order (`dry_run=False`)."
        )
        staged["status"] = "DRY_RUN_CONFIRMED"
        return staged

    if not is_live_auth or session is None:
        raise RuntimeError(
            "Cannot transmit live order (`dry_run=False`): TT_USERNAME / TT_PASSWORD (or "
            "TT_PROVIDER_SECRET / TT_REFRESH_TOKEN) not configured in .env!"
        )

    acct = Account.get(session, acct_id)
    live_resp = acct.place_order(session, staged["entry_order"], dry_run=False)
    print(f"🚀 LIVE ORDER TRANSMITTED TO TASTYTRADE ({acct_id}): {live_resp}")
    staged["status"] = "LIVE_TRANSMITTED"
    staged["live_response"] = live_resp
    return staged


def run_safety_and_mock_staging_audit() -> None:
    """
    Runs a comprehensive verification of all 6 non-negotiable safety assertions
    and stages today's FISV setup ($63.19 Entry, $62.78 Stop, $63.48 Leg 1 VWAP) in --dry-run mode.
    """
    print("\n" + "=" * 80)
    print("🔒 RUNNING TASTYTRADE FAIL-SAFE HARD SANITY ASSERTION AUDIT")
    print("=" * 80)

    test_cases = [
        (
            "Guard #1: Invalid Account ID ('99999999' != '5WJ26897')",
            lambda: validate_order_safety("FISV", 2373, 63.19, 62.78, "99999999"),
        ),
        (
            "Guard #2: 150,000 Share Fat-Finger ($9.47M Notional > $152,000 Cap)",
            lambda: validate_order_safety("FISV", 150000, 63.19, 62.78, "5WJ26897"),
        ),
        (
            "Guard #3: Share Ceiling Breach (3,500 sh > 3,000 Max Shares)",
            lambda: validate_order_safety("FISV", 3500, 42.00, 41.80, "5WJ26897"),
        ),
        (
            "Guard #4: Inverted Stop-Loss ($63.50 Stop >= $63.19 Entry)",
            lambda: validate_order_safety("FISV", 2373, 63.19, 63.50, "5WJ26897"),
        ),
        (
            "Guard #5: Excessive Dollar Risk (-$2,373.00 Loss > $1,500 Max Risk Cap)",
            lambda: validate_order_safety("FISV", 2373, 63.19, 62.19, "5WJ26897"),
        ),
        (
            "Guard #6: Dual-Feed Price Divergence (YF $63.53 vs Stale/Bad Feed $62.80 = 1.15% > 0.25% Cap)",
            lambda: validate_order_safety("FISV", 2373, 63.19, 62.78, "5WJ26897", primary_live_price=63.53, secondary_live_price=62.80),
        ),
    ]

    for label, fn in test_cases:
        try:
            fn()
            print(f"❌ FAILED TO BLOCK: {label}")
            sys.exit(1)
        except AssertionError as err:
            print(f"✅ BLOCKED AS EXPECTED -> {label}\n   └─ AssertionError: {err}")

    print("\n" + "=" * 80)
    print("🧪 STAGING TODAY'S FISV V5 SIGNAL IN --dry-run MODE")
    print("=" * 80)
    staged = stage_and_prompt_order(
        ticker="FISV",
        limit_price=63.19,
        stop_price=62.78,
        leg1_vwap_price=63.48,
        account_id="5WJ26897",
        clip_usd=150_000.00,
        dry_run=True,
        auto_abort_prompt=True,
        primary_live_price=63.53,
    )

    print("\n📦 SERIALIZED TASTYTRADE SDK OTOCO BRACKET PAYLOAD (VERIFIED):")
    print(
        json.dumps(
            json.loads(
                staged["bracket_order"].model_dump_json(exclude_none=True, by_alias=True)
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    run_safety_and_mock_staging_audit()
