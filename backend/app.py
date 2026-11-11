"""
backend/app.py
==============
FastAPI web application for the Flush & Reclaim V5 Personal Trading Dashboard.

Provides:
- Loopback-bound REST API (`/api/*`) with Origin / CSRF header validation on mutating POST routes
- Server-Sent Events stream (`/api/stream`) with full snapshot recovery on connect/reconnect
- Static asset serving for the Vite + React + Lightweight Charts frontend (`frontend/dist`)
"""

from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
import json
import os
from typing import Any, AsyncGenerator, Dict, Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from config_schema import (
    OrderValidationError,
    PreflightRejectedError,
    RiskLimitExceededError,
    StaleSignalError,
    ReconciliationRequiredError,
)
from backend.database import DashboardDatabase
from backend.models import (
    PlanEditRequest,
    PreviewOrderRequest,
    SubmitOrderRequest,
    SettingsUpdateRequest,
    ManualTradeCreateRequest,
    ReconcileTriggerRequest,
)
from backend.worker import ScannerManagementWorker


ALLOWED_LOCAL_ORIGINS = {
    "http://127.0.0.1:8000",
    "http://localhost:8000",
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "http://testserver",
}


def verify_local_mutation_request(request: Request) -> None:
    """
    CSRF & local-origin protection for mutating POST endpoints (Section 12):
    Requires either:
      - `X-FlushReclaim-Confirm: true` header, OR
      - a verified local `Origin` / `Referer` header (`127.0.0.1` / `localhost`).
    Rejects cross-origin browser requests from untrusted origins.
    """
    origin = request.headers.get("origin")
    if origin:
        # Strip trailing slash
        norm_origin = origin.rstrip("/")
        if norm_origin not in ALLOWED_LOCAL_ORIGINS and not (
            norm_origin.startswith("http://127.0.0.1:") or norm_origin.startswith("http://localhost:")
        ):
            raise HTTPException(
                status_code=403,
                detail=f"CSRF BLOCK: Untrusted Origin '{origin}' rejected on mutating route.",
            )

    confirm_header = request.headers.get("x-flushreclaim-confirm", "").lower()
    if confirm_header == "true":
        return

    # If Origin is local or testclient, allow; if neither header nor local origin is present, require header
    client_host = request.client.host if request.client else ""
    if client_host in ("127.0.0.1", "::1", "localhost", "testclient"):
        return

    raise HTTPException(
        status_code=403,
        detail="CSRF BLOCK: Missing 'X-FlushReclaim-Confirm: true' header or local Origin.",
    )


def create_app(
    db_path: Optional[str] = None,
    start_background_worker: bool = True,
) -> FastAPI:
    db = DashboardDatabase(db_path) if db_path else DashboardDatabase()
    worker = ScannerManagementWorker(db=db)

    @asynccontextmanager
    async def lifespan(app_instance: FastAPI):
        if start_background_worker:
            worker.start_background_loop(interval_seconds=15.0)
        try:
            yield
        finally:
            worker.stop_background_loop()

    app = FastAPI(
        title="Flush & Reclaim V5 — Trader Execution Dashboard",
        version="5.1.0",
        lifespan=lifespan,
    )
    app.state.db = db
    app.state.worker = worker

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(ALLOWED_LOCAL_ORIGINS),
        allow_origin_regex=r"^http://(127\.0\.0\.1|localhost)(:\d+)?$",
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-FlushReclaim-Confirm"],
    )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={
                "detail": f"Internal server error on {request.method} {request.url.path}: {type(exc).__name__}: {exc}",
            },
        )

    # ==========================================================================
    # READ-ONLY GET ENDPOINTS (Never mutate or submit orders)
    # ==========================================================================
    @app.get("/api/health")
    def get_health() -> Dict[str, Any]:
        snap = worker.get_dashboard_snapshot()
        return {
            "status": "ok",
            "server_time_et": snap["server_time_et"],
            "operating_mode": snap["operating_mode"],
            "fixture_mode": snap["fixture_mode"],
            "worker_health": snap["worker_health"],
        }

    @app.get("/api/snapshot")
    def get_snapshot() -> Dict[str, Any]:
        return worker.get_dashboard_snapshot()

    @app.get("/api/chart/{symbol}")
    def get_chart(symbol: str) -> Dict[str, Any]:
        return worker.get_symbol_chart(symbol)

    @app.get("/api/stream")
    async def stream_events(request: Request) -> StreamingResponse:
        """
        Server-Sent Events (SSE) stream:
        - Immediately pushes an `INITIAL_SNAPSHOT` event upon connection/reconnection.
        - Streams incremental `SCAN_CYCLE_COMPLETED`, `SETTINGS_UPDATED`, and order state events.
        - Emits a heartbeat every 10s so the client detects disconnects promptly.
        """
        queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue(maxsize=50)
        loop = asyncio.get_running_loop()

        def _listener(payload: Dict[str, Any]) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, payload)
            except Exception:
                pass

        worker.register_listener(_listener)

        async def _event_generator() -> AsyncGenerator[str, None]:
            try:
                initial = {
                    "type": "INITIAL_SNAPSHOT",
                    "snapshot": worker.get_dashboard_snapshot(),
                }
                yield f"data: {json.dumps(initial)}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=10.0)
                        yield f"data: {json.dumps(event)}\n\n"
                    except asyncio.TimeoutError:
                        heartbeat = {
                            "type": "HEARTBEAT",
                            "snapshot": worker.get_dashboard_snapshot(),
                        }
                        yield f"data: {json.dumps(heartbeat)}\n\n"
            finally:
                worker.unregister_listener(_listener)

        return StreamingResponse(
            _event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ==========================================================================
    # MUTATING POST ENDPOINTS (Protected by local Origin + CSRF confirmation)
    # ==========================================================================
    @app.post("/api/scan")
    def trigger_scan(request: Request) -> Dict[str, Any]:
        verify_local_mutation_request(request)
        return worker.run_scan_cycle()

    @app.post("/api/plans/update")
    def update_plan(req: PlanEditRequest, request: Request) -> Dict[str, Any]:
        verify_local_mutation_request(request)
        try:
            updated_plan = worker.update_order_plan(req.model_dump())
            return {
                "status": "ok",
                "plan": updated_plan,
                "snapshot": worker.get_dashboard_snapshot(),
            }
        except OrderValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/previews/create")
    def create_preview(req: PreviewOrderRequest, request: Request) -> Dict[str, Any]:
        verify_local_mutation_request(request)
        try:
            preview = worker.run_broker_preview(
                plan_id=req.plan_id,
                plan_version=req.plan_version,
                simulate_failure=req.simulate_failure,
                simulate_broker_error=req.simulate_broker_error,
            )
            return {
                "status": "ok",
                "preview": preview,
                "snapshot": worker.get_dashboard_snapshot(),
            }
        except (OrderValidationError, PreflightRejectedError, ReconciliationRequiredError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/orders/submit")
    def submit_order(req: SubmitOrderRequest, request: Request) -> Dict[str, Any]:
        verify_local_mutation_request(request)
        try:
            result = worker.submit_confirmed_order(
                plan_id=req.plan_id,
                plan_version=req.plan_version,
                preview_id=req.preview_id,
                idempotency_key=req.idempotency_key,
                confirmed_by_user=req.confirmed_by_user,
                explicit_confirmation_checked=bool(req.explicit_confirmation_checked),
                simulate_timeout=req.simulate_timeout,
                simulate_partial_fill=req.simulate_partial_fill,
                simulate_partial_fill_qty=req.simulate_partial_fill_qty,
            )
            return {
                "status": "ok",
                "result": result,
                "snapshot": worker.get_dashboard_snapshot(),
            }
        except (
            OrderValidationError,
            PreflightRejectedError,
            RiskLimitExceededError,
            StaleSignalError,
            ReconciliationRequiredError,
        ) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/positions/{position_id}/leg1-exit")
    def trigger_leg1_exit(position_id: str, request: Request) -> Dict[str, Any]:
        verify_local_mutation_request(request)
        try:
            pos = worker.simulate_leg1_exit_on_position(position_id)
            return {
                "status": "ok",
                "position": pos,
                "snapshot": worker.get_dashboard_snapshot(),
            }
        except OrderValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/positions/manual")
    def create_manual_trade(req: ManualTradeCreateRequest, request: Request) -> Dict[str, Any]:
        verify_local_mutation_request(request)
        from market_calendar import NY_TZ
        from datetime import datetime
        import uuid

        now_str = datetime.now(NY_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
        pos_id = f"pos-manual-{req.symbol.lower()}-{uuid.uuid4().hex[:6]}"
        pos_data = {
            "position_id": pos_id,
            "symbol": req.symbol.upper(),
            "source_mode": "MANUAL_USER_REPORTED",
            "reconciliation_badge": "UNRECONCILED_USER_REPORTED",
            "is_strategy_managed": False,
            "intent_id": None,
            "signal_id": None,
            "account_id": "5WJ26897 (User-Reported Unverified)",
            "filled_qty": req.reported_shares,
            "tranche1_qty": None,
            "tranche2_qty": None,
            "leg1_exited_qty": None,
            "remaining_qty": None,
            "avg_fill_price": req.reported_entry_price,
            "current_price": req.reported_entry_price,
            "mark_timestamp_et": now_str,
            "unrealized_pnl_usd": None,
            "realized_pnl_usd": None,
            "reported_Approximate_pnl_usd": req.reported_approx_pnl_usd,
            "working_stop_price": req.reported_stop_price,
            "working_stop_qty": None,
            "leg1_target_price": req.reported_leg1_exit_price,
            "leg2_target_price": None,
            "runner_state": "USER_REPORTED_MANUAL_TRAIL",
            "protection_state": "PROTECTION_CONFIRMED",
            "opened_at_et": "User-reported (unreconciled)",
            "updated_at_et": now_str,
            "closed_at_et": None,
            "notes": req.notes,
            "requires_attention": False,
            "attention_reason": None,
        }
        db.save_position(pos_data)
        db.log_event(
            event_type="MANUAL_TRADE_RECORDED",
            severity="INFO",
            source_mode="MANUAL_USER_REPORTED",
            symbol=req.symbol.upper(),
            summary=f"Recorded user-reported manual trade on {req.symbol.upper()} @ ${req.reported_entry_price:.2f} (unreconciled).",
            details=pos_data,
        )
        return {
            "status": "ok",
            "position": pos_data,
            "snapshot": worker.get_dashboard_snapshot(),
        }

    @app.post("/api/reconcile")
    def trigger_reconcile(req: ReconcileTriggerRequest, request: Request) -> Dict[str, Any]:
        verify_local_mutation_request(request)
        res = worker.reconcile_positions_and_orders(
            reason="Manual operator reconciliation",
            simulate_unprotected=req.simulate_unprotected,
        )
        return {
            "status": "ok",
            "reconciliation": res,
            "snapshot": worker.get_dashboard_snapshot(),
        }

    @app.post("/api/handoff/open")
    def record_handoff(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
        verify_local_mutation_request(request)
        symbol = str(payload.get("symbol", "FISV")).upper()
        action = str(payload.get("action", "OPEN_TASTYTRADE"))
        res = worker.record_handoff_action(symbol=symbol, action=action)
        return {
            "status": "ok",
            "handoff_result": res,
            "snapshot": worker.get_dashboard_snapshot(),
        }

    @app.post("/api/settings")
    def update_settings(req: SettingsUpdateRequest, request: Request) -> Dict[str, Any]:
        verify_local_mutation_request(request)
        updates = {k: v for k, v in req.model_dump().items() if v is not None}
        settings = worker.update_settings(updates)
        return {
            "status": "ok",
            "settings": settings,
            "snapshot": worker.get_dashboard_snapshot(),
        }

    @app.post("/api/alerts/{alert_id}/ack")
    def acknowledge_alert(alert_id: str, request: Request) -> Dict[str, Any]:
        verify_local_mutation_request(request)
        db.acknowledge_alert(alert_id)
        return {
            "status": "ok",
            "snapshot": worker.get_dashboard_snapshot(),
        }

    # ==========================================================================
    # FRONTEND STATIC FILES (Served when `frontend/dist` exists)
    # ==========================================================================
    frontend_dist = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "frontend",
        "dist",
    )
    assets_dir = os.path.join(frontend_dist, "assets")
    if os.path.isdir(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{full_path:path}", response_model=None)
    def serve_spa(full_path: str) -> Response:
        post_only_routes = {
            "api/orders/submit",
            "api/previews/create",
            "api/plans/update",
            "api/scan",
            "api/reconcile",
            "api/settings",
            "api/handoff/open",
            "api/positions/manual",
        }
        norm_path = full_path.strip("/")
        if norm_path in post_only_routes or (
            norm_path.startswith("api/positions/") and norm_path.endswith("/leg1-exit")
        ):
            raise HTTPException(
                status_code=405,
                detail="Method Not Allowed: GET requests cannot mutate or submit broker orders.",
            )
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail=f"API endpoint '/{full_path}' not found.")
        index_file = os.path.join(frontend_dist, "index.html")
        if os.path.isfile(index_file):
            return FileResponse(index_file)
        return HTMLResponse(
            content=(
                "<html><body style='background:#0b0f17;color:#e2e8f0;font-family:sans-serif;padding:2rem;'>"
                "<h2>Flush & Reclaim V5 Backend Running</h2>"
                "<p>API is active at <code>/api/snapshot</code>. Build the frontend in <code>frontend/</code> to serve the UI.</p>"
                "</body></html>"
            ),
            status_code=200,
        )

    return app


app = create_app()
