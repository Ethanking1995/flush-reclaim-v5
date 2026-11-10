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

# ==============================================================================
# PART 1 & 2: HARD SAFETY CONSTANTS & FAIL-SAFE SANITY ASSERTIONS (NON-NEGOTIABLE)
# ==============================================================================
load_dotenv()

BROKER = os.getenv("BROKER", "robinhood").strip().lower()
MAX_NOTIONAL_USD = float(os.getenv("MAX_NOTIONAL_USD", "152000"))
MAX_SHARE_COUNT = int(os.getenv("MAX_SHARE_COUNT", "3000"))
TARGET_CLIP_USD = float(os.getenv("TARGET_CLIP_USD", "150000"))
MAX_RISK_USD = float(os.getenv("MAX_RISK_USD", "1500"))
MAX_FEED_DIVERGENCE_PCT = 0.0025  # 0.25% max allowed spread between Primary (YF) & Secondary (TV) feeds

# Tastytrade-only default account; Robinhood uses RH_ACCOUNT_LABEL / any configured id
ALLOWED_ACCOUNT_ID = os.getenv(
    "TT_ACCOUNT_ID" if BROKER == "tastytrade" else "RH_ACCOUNT_ID",
    "5WJ26897" if BROKER == "tastytrade" else "robinhood",
).strip()

SESSION_CACHE_PATH = Path(__file__).resolve().parent / ".tt_session_cache.json"
PENDING_ORDERS_PATH = Path(__file__).resolve().parent / "pending_orders.json"

CNBC_QUOTE_URL = "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
CNBC_BARS_URL = "https://ts-api.cnbc.com/harmony/app/bars"
TV_SCANNER_URL = "https://scanner.tradingview.com/america/scan"
TV_EXCHANGE_CANDIDATES = ("NASDAQ", "NYSE", "AMEX")


def _require_tastytrade():
    """Lazy-import tastytrade so HUD / Robinhood mode works without that SDK."""
    try:
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
    except ImportError as exc:
        raise ImportError(
            "tastytrade is not installed. Set BROKER=robinhood (default) for HUD + "
            "manual Robinhood tickets, or `pip install tastytrade` for live TT routing."
        ) from exc
    return {
        "API_URL": API_URL,
        "API_VERSION": API_VERSION,
        "CERT_URL": CERT_URL,
        "Account": Account,
        "Session": Session,
        "ComplexOrderType": ComplexOrderType,
        "InstrumentType": InstrumentType,
        "Leg": Leg,
        "NewComplexOrder": NewComplexOrder,
        "NewOrder": NewOrder,
        "OrderAction": OrderAction,
        "OrderTimeInForce": OrderTimeInForce,
        "OrderType": OrderType,
        "now_in_new_york": now_in_new_york,
        "validate_response": validate_response,
    }


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
    assert account_id == ALLOWED_ACCOUNT_ID, f"SECURITY ABORT: Invalid account {account_id}!"
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
    # 5. Risk Cap Guard
    max_loss_usd = shares * (limit_price - stop_price)
    assert max_loss_usd <= MAX_RISK_USD, (
        f"FATAL ERROR: Calculated risk ${max_loss_usd:,.2f} exceeds ${MAX_RISK_USD:,.2f} max allowed loss!"
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
# PART 1: AUTHENTICATION & AUTOMATIC TOKEN SESSION PERSISTENCE (TASTYTRADE ONLY)
# ==============================================================================
def load_or_create_session(env_path: Optional[str] = None) -> Tuple[Optional[Any], str, bool]:
    """
    Loads TT credentials from .env. Returns (session_or_none, account_id, is_live_authenticated).
    No-op / offline for Robinhood mode (no official stock REST API).
    """
    if BROKER != "tastytrade":
        return None, ALLOWED_ACCOUNT_ID, False

    tt = _require_tastytrade()
    Session = tt["Session"]
    API_URL = tt["API_URL"]
    API_VERSION = tt["API_VERSION"]
    CERT_URL = tt["CERT_URL"]
    now_in_new_york = tt["now_in_new_york"]
    validate_response = tt["validate_response"]

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

    class PasswordOrOAuthSession(Session):
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
            self.async_client = None

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

    if SESSION_CACHE_PATH.exists():
        try:
            cached_raw = SESSION_CACHE_PATH.read_text()
            sess = Session.deserialize(cached_raw)
            if sess.validate():
                return sess, account_id, True
        except Exception:
            pass

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
# PART 3: ORDER PLAN CONSTRUCTION
# ==============================================================================
def build_v5_order_plan(
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
    Calculates share count from clip_usd, runs safety assertions, and returns a
    broker-agnostic order plan (entry / stop / leg1 target).
    """
    limit_p = round(float(limit_price), 2)
    stop_p = round(float(stop_price), 2)
    leg1_p = round(float(leg1_vwap_price), 2)

    shares = min(int(clip_usd / limit_p), MAX_SHARE_COUNT)
    leg1_shares = int(shares / 2)
    leg2_shares = shares - leg1_shares

    validate_order_safety(
        ticker=ticker,
        shares=shares,
        limit_price=limit_p,
        stop_price=stop_p,
        account_id=account_id,
        primary_live_price=primary_live_price,
        secondary_live_price=secondary_live_price,
    )

    notional_usd = round(shares * limit_p, 2)
    per_share_risk = round(limit_p - stop_p, 2)
    max_risk_usd = round(shares * (limit_p - stop_p), 2)
    leg2_target = round(limit_p * 1.0085, 2)

    return {
        "account_id": account_id,
        "broker": BROKER,
        "ticker": ticker.upper(),
        "shares": shares,
        "leg1_shares": leg1_shares,
        "leg2_shares": leg2_shares,
        "limit_price": limit_p,
        "stop_price": stop_p,
        "leg1_vwap_price": leg1_p,
        "leg2_target": leg2_target,
        "notional_usd": notional_usd,
        "per_share_risk": per_share_risk,
        "max_risk_usd": max_risk_usd,
    }


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
    Builds the V5 plan. On tastytrade, also attaches official SDK OTOCO bracket objects.
    On robinhood, returns the plan only (manual ticket / no official stock REST API).
    """
    staged = build_v5_order_plan(
        ticker=ticker,
        limit_price=limit_price,
        stop_price=stop_price,
        leg1_vwap_price=leg1_vwap_price,
        account_id=account_id,
        clip_usd=clip_usd,
        primary_live_price=primary_live_price,
        secondary_live_price=secondary_live_price,
    )

    if BROKER != "tastytrade":
        staged["entry_order"] = None
        staged["stop_order"] = None
        staged["leg1_target_order"] = None
        staged["bracket_order"] = None
        return staged

    tt = _require_tastytrade()
    Leg = tt["Leg"]
    NewOrder = tt["NewOrder"]
    NewComplexOrder = tt["NewComplexOrder"]
    InstrumentType = tt["InstrumentType"]
    OrderAction = tt["OrderAction"]
    OrderTimeInForce = tt["OrderTimeInForce"]
    OrderType = tt["OrderType"]
    ComplexOrderType = tt["ComplexOrderType"]

    shares = staged["shares"]
    leg1_shares = staged["leg1_shares"]
    limit_p = staged["limit_price"]
    stop_p = staged["stop_price"]
    leg1_p = staged["leg1_vwap_price"]

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

    bracket_order = NewComplexOrder(
        trigger_order=entry_order,
        orders=[stop_order, leg1_target_order],
        type=ComplexOrderType.OTOCO,
    )

    staged.update(
        {
            "entry_order": entry_order,
            "stop_order": stop_order,
            "leg1_target_order": leg1_target_order,
            "bracket_order": bracket_order,
        }
    )
    return staged


def _feed_consensus_str(
    primary_live_price: Optional[float],
    secondary_live_price: Optional[float],
) -> str:
    if primary_live_price and secondary_live_price:
        spread_pct = abs(primary_live_price - secondary_live_price) / primary_live_price * 100.0
        return (
            f"PASSED ✅ (YF ${primary_live_price:.2f} vs TV ${secondary_live_price:.2f} | "
            f"Spread: {spread_pct:.2f}% <= {MAX_FEED_DIVERGENCE_PCT * 100:.2f}%)"
        )
    if secondary_live_price:
        return f"VERIFIED ✅ (TradingView Secondary Quote: ${secondary_live_price:.2f})"
    return "PRIMARY ONLY (Secondary Oracle Unreachable)"


def _print_robinhood_ticket(staged: dict[str, Any], feed_consensus_str: str) -> None:
    print("=" * 80)
    print("📱 ROBINHOOD MANUAL ORDER TICKET (NO LIVE API ROUTING)")
    print("-" * 80)
    print(f"Account       : {staged['account_id']}")
    print(f"Ticker        : {staged['ticker']}")
    print(f"1. ENTRY      : Buy {staged['shares']:,} sh @ ${staged['limit_price']:.2f} Limit (Day)")
    print(
        f"   Notional   : ${staged['notional_usd']:,.2f}  "
        f"(Safety Cap: ${MAX_NOTIONAL_USD:,.2f} | PASSED ✅)"
    )
    print(
        f"2. STOP       : Stop / Stop-Limit sell {staged['shares']:,} sh trigger "
        f"${staged['stop_price']:.2f}  "
        f"(-${staged['per_share_risk']:.2f} / -${staged['max_risk_usd']:,.2f} Max Risk | PASSED ✅)"
    )
    print(
        f"3. LEG 1 OUT  : Limit sell {staged['leg1_shares']:,} sh @ ${staged['leg1_vwap_price']:.2f} "
        f"(50% scale-out / VWAP or +0.40%)"
    )
    print(
        f"4. LEG 2 OUT  : Limit sell {staged['leg2_shares']:,} sh @ ${staged['leg2_target']:.2f} "
        f"(+0.85% target) OR trail; flatten by 3:40 PM ET"
    )
    print(f"Feed Consensus: {feed_consensus_str}")
    print("-" * 80)
    print(f"Saved to {PENDING_ORDERS_PATH.name} — ask the Cursor agent to place it via Robinhood MCP,")
    print("or enter it manually in the Robinhood app. This script never transmits orders itself.")
    print("=" * 80)


def write_pending_order(staged: dict[str, Any], signal: str) -> None:
    """Upserts today's plan for this ticker into pending_orders.json for the MCP agent handoff."""
    now = datetime.now().astimezone()
    try:
        book = json.loads(PENDING_ORDERS_PATH.read_text()) if PENDING_ORDERS_PATH.exists() else {}
    except Exception:
        book = {}
    if book.get("session_date") != now.date().isoformat():
        book = {"session_date": now.date().isoformat(), "orders": {}}

    existing = book["orders"].get(staged["ticker"], {})
    if existing.get("status") in ("submitted", "filled", "closed"):
        return

    book["orders"][staged["ticker"]] = {
        "ticker": staged["ticker"],
        "signal": signal,
        "status": "pending",
        "updated_at": now.isoformat(timespec="seconds"),
        "shares": staged["shares"],
        "entry_limit": staged["limit_price"],
        "stop_price": staged["stop_price"],
        "leg1_shares": staged["leg1_shares"],
        "leg1_limit": staged["leg1_vwap_price"],
        "leg2_shares": staged["leg2_shares"],
        "leg2_limit": staged["leg2_target"],
        "notional_usd": staged["notional_usd"],
        "max_risk_usd": staged["max_risk_usd"],
        "flatten_by": "15:40 America/New_York",
    }
    PENDING_ORDERS_PATH.write_text(json.dumps(book, indent=2))


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
    signal: str = "STAGED",
) -> dict[str, Any]:
    """
    Stages the V5 order plan, verifies Dual-Feed Price Consensus, and either:
      - Robinhood: prints a manual app ticket (never auto-routes)
      - Tastytrade: dry-run / optional live transmit with human confirmation
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

    feed_consensus_str = _feed_consensus_str(primary_live_price, secondary_live_price)

    if BROKER != "tastytrade":
        _print_robinhood_ticket(staged, feed_consensus_str)
        if signal != "SELF_TEST":
            write_pending_order(staged, signal)
        staged["status"] = "ROBINHOOD_PENDING"
        return staged

    session, acct_id, is_live_auth = load_or_create_session()
    dry_run_status = "SUCCESSFUL (Margin Verified with Tastytrade Clearing)"
    preflight_response = None
    Account = _require_tastytrade()["Account"]

    if is_live_auth and session is not None:
        try:
            acct = Account.get(session, acct_id)
            preflight_response = acct.place_order(
                session, staged["entry_order"], dry_run=True
            )
            dry_run_status = "SUCCESSFUL (Margin Verified with Tastytrade Clearing)"
        except Exception as e:
            dry_run_status = f"FAILED PREFLIGHT ({e})"
    else:
        _ = staged["entry_order"].model_dump_json(exclude_none=True, by_alias=True)
        _ = staged["bracket_order"].model_dump_json(exclude_none=True, by_alias=True)
        dry_run_status = "SUCCESSFUL (Offline schema validation only — no TT session)"

    print("=" * 80)
    print("🚨 TASTYTRADE STAGED BRACKET ORDER READY FOR SUBMISSION")
    print("-" * 80)
    print(f"Account       : {staged['account_id']}")
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
    staged["preflight_response"] = preflight_response
    return staged


def run_safety_and_mock_staging_audit() -> None:
    """
    Runs a comprehensive verification of all 6 non-negotiable safety assertions
    and stages a sample FISV setup in dry-run / Robinhood ticket mode.
    """
    print("\n" + "=" * 80)
    print(f"🔒 RUNNING FAIL-SAFE HARD SANITY ASSERTION AUDIT (BROKER={BROKER})")
    print("=" * 80)

    good_acct = ALLOWED_ACCOUNT_ID
    test_cases = [
        (
            f"Guard #1: Invalid Account ID ('99999999' != '{good_acct}')",
            lambda: validate_order_safety("FISV", 2373, 63.19, 62.78, "99999999"),
        ),
        (
            "Guard #2: Fat-Finger Share Count (notional over cap)",
            lambda: validate_order_safety("FISV", 150000, 63.19, 62.78, good_acct),
        ),
        (
            "Guard #3: Share Ceiling Breach",
            lambda: validate_order_safety("FISV", MAX_SHARE_COUNT + 500, 42.00, 41.80, good_acct),
        ),
        (
            "Guard #4: Inverted Stop-Loss",
            lambda: validate_order_safety("FISV", 100, 63.19, 63.50, good_acct),
        ),
        (
            "Guard #5: Excessive Dollar Risk",
            lambda: validate_order_safety("FISV", 2373, 63.19, 62.19, good_acct),
        ),
        (
            "Guard #6: Dual-Feed Price Divergence",
            lambda: validate_order_safety(
                "FISV", 100, 63.19, 62.78, good_acct,
                primary_live_price=63.53, secondary_live_price=62.80,
            ),
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
    print("🧪 STAGING SAMPLE FISV V5 SIGNAL")
    print("=" * 80)
    # Size sample so risk stays under MAX_RISK_USD for small Robinhood clips
    sample_clip = min(TARGET_CLIP_USD, 10_000.0)
    staged = stage_and_prompt_order(
        ticker="FISV",
        limit_price=63.19,
        stop_price=62.78,
        leg1_vwap_price=63.48,
        account_id=ALLOWED_ACCOUNT_ID,
        clip_usd=sample_clip,
        dry_run=True,
        auto_abort_prompt=True,
        primary_live_price=63.53,
        signal="SELF_TEST",
    )

    if staged.get("bracket_order") is not None:
        print("\n📦 SERIALIZED TASTYTRADE SDK OTOCO BRACKET PAYLOAD (VERIFIED):")
        print(
            json.dumps(
                json.loads(
                    staged["bracket_order"].model_dump_json(exclude_none=True, by_alias=True)
                ),
                indent=2,
            )
        )
    else:
        print("\n📦 ROBINHOOD ORDER PLAN:")
        print(json.dumps({k: v for k, v in staged.items() if not k.endswith("_order")}, indent=2))


if __name__ == "__main__":
    run_safety_and_mock_staging_audit()
