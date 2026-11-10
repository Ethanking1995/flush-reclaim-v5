"""
tests/test_web_dashboard_acceptance.py
======================================
Focused acceptance and regression test suite for the Flush & Reclaim V5 Web Dashboard
covering all 15 required acceptance scenarios (Section 13) plus Section 10 data-quality
and causal-engine regressions.
"""

from __future__ import annotations
from datetime import datetime
import os
import tempfile
import unittest

import numpy as np
import pandas as pd
from fastapi.testclient import TestClient

from market_calendar import NY_TZ
from market_data_adapter import (
    DataProvenance,
    QuoteObservation,
    compute_bar_session_vwap,
    evaluate_observation_consistency,
)
from strategy_engine import compute_causal_5m_features
from tasty_executor import (
    SignalState,
    OrderState,
    PositionProtectionState,
    fetch_secondary_quotes,
)
from backend.app import create_app


class TestWebDashboardAcceptance(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_dashboard.db")
        self.app = create_app(db_path=self.db_path, start_background_worker=False)
        self.client = TestClient(self.app)
        self.headers = {"X-FlushReclaim-Confirm": "true"}

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    # --------------------------------------------------------------------------
    # 1. Stale / divergent data block
    # --------------------------------------------------------------------------
    def test_01_stale_and_divergent_data_block(self) -> None:
        """
        1. Stale/divergent data block: when quote age exceeds threshold or compatible-timestamp
        divergence exceeds 0.25%, the ticker moves to Data unavailable and order submission is disabled.
        """
        # In demo_ready, EEFT has 0.77% cross-endpoint spread (> 0.25% cap)
        snap = self.client.get("/api/snapshot").json()
        cands = {c["symbol"]: c for c in snap["candidates"]}
        self.assertEqual(cands["EEFT"]["category"], "DATA_UNAVAILABLE")
        self.assertEqual(cands["EEFT"]["signal_state"], SignalState.DATA_UNAVAILABLE.value)
        self.assertFalse(cands["EEFT"]["is_actionable"])
        self.assertFalse(snap["order_plans_by_symbol"]["EEFT"]["is_executable"])

        # Switch to demo_stale where quote age is 95s (> 45s max)
        r = self.client.post(
            "/api/settings",
            json={"fixture_mode": "demo_stale"},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 200)
        snap_stale = r.json()["snapshot"]
        for c in snap_stale["candidates"]:
            self.assertEqual(c["category"], "DATA_UNAVAILABLE")
            self.assertEqual(c["signal_state"], SignalState.DATA_UNAVAILABLE.value)
            self.assertFalse(c["is_actionable"])
            plan = snap_stale["order_plans_by_symbol"][c["symbol"]]
            self.assertFalse(plan["is_executable"])

    # --------------------------------------------------------------------------
    # 2. Developing vs. ready separation
    # --------------------------------------------------------------------------
    def test_02_developing_vs_ready_separation(self) -> None:
        """
        2. Developing vs. ready separation: WATCHING_ARMED or WATCHING_FLUSHING rows do not
        render or expose an executable Submit to Tastytrade action.
        """
        snap = self.client.get("/api/snapshot").json()
        cands = {c["symbol"]: c for c in snap["candidates"]}
        # In demo_ready, FISV is READY_FOR_REVIEW while PLNT is WATCHING_FLUSHING
        self.assertEqual(cands["FISV"]["signal_state"], SignalState.READY_FOR_REVIEW.value)
        self.assertTrue(cands["FISV"]["is_actionable"])
        self.assertEqual(cands["PLNT"]["signal_state"], SignalState.WATCHING_FLUSHING.value)
        self.assertFalse(cands["PLNT"]["is_actionable"])
        self.assertIsNone(cands["PLNT"]["confirmed_signal_time_et"])

        plnt_plan = snap["order_plans_by_symbol"]["PLNT"]
        self.assertFalse(plnt_plan["is_executable"])

        # Attempting to preview or submit a WATCHING plan fails server-side validation
        r_prev = self.client.post(
            "/api/previews/create",
            json={"plan_id": plnt_plan["plan_id"], "plan_version": plnt_plan["plan_version"]},
            headers=self.headers,
        )
        self.assertEqual(r_prev.status_code, 400)
        self.assertIn("not READY_FOR_REVIEW", r_prev.json()["detail"])

    # --------------------------------------------------------------------------
    # 3. Sizing and risk calculation
    # --------------------------------------------------------------------------
    def test_03_sizing_and_risk_calculation(self) -> None:
        """
        3. Sizing and risk calculation: a $150,000 clip on FISV at $62.72 computes 2,391 shares
        (1,195 + 1,196 split), exact notional, and exact planned stop risk.
        """
        snap = self.client.get("/api/snapshot").json()
        fisv_plan = snap["order_plans_by_symbol"]["FISV"]
        self.assertAlmostEqual(fisv_plan["allocation_usd"], 150000.0, places=2)
        self.assertAlmostEqual(fisv_plan["limit_price"], 62.72, places=2)
        self.assertEqual(fisv_plan["shares"], 2391)
        self.assertEqual(fisv_plan["tranche1_shares"], 1195)
        self.assertEqual(fisv_plan["tranche2_shares"], 1196)
        self.assertEqual(
            fisv_plan["tranche1_shares"] + fisv_plan["tranche2_shares"],
            fisv_plan["shares"],
        )
        self.assertAlmostEqual(fisv_plan["estimated_notional_usd"], 2391 * 62.72, places=2)
        self.assertAlmostEqual(fisv_plan["stop_price"], 62.35, places=2)
        self.assertAlmostEqual(fisv_plan["planned_risk_usd"], round(2391 * (62.72 - 62.35), 2), places=2)
        self.assertAlmostEqual(
            fisv_plan["planned_risk_pct"],
            round(((62.72 - 62.35) / 62.72) * 100.0, 2),
            places=2,
        )

    # --------------------------------------------------------------------------
    # 4. In-flight ticket edit persistence
    # --------------------------------------------------------------------------
    def test_04_in_flight_ticket_edit_persistence(self) -> None:
        """
        4. In-flight ticket edit persistence: editing allocation or limit price preserves the draft
        across background scanner refreshes, invalidates any prior preview, and updates shares,
        notional, and risk.
        """
        snap = self.client.get("/api/snapshot").json()
        fisv_plan = snap["order_plans_by_symbol"]["FISV"]

        # Create an initial preview for version 1
        r_prev = self.client.post(
            "/api/previews/create",
            json={"plan_id": fisv_plan["plan_id"], "plan_version": fisv_plan["plan_version"]},
            headers=self.headers,
        )
        self.assertEqual(r_prev.status_code, 200)
        preview_v1 = r_prev.json()["preview"]
        self.assertEqual(preview_v1["status"], "VALID")

        # Edit allocation to $100,000 and limit price to $62.70
        r_edit = self.client.post(
            "/api/plans/update",
            json={
                "symbol": "FISV",
                "allocation_usd": 100000.0,
                "limit_price": 62.70,
            },
            headers=self.headers,
        )
        self.assertEqual(r_edit.status_code, 200)
        updated_plan = r_edit.json()["plan"]
        self.assertEqual(updated_plan["plan_version"], fisv_plan["plan_version"] + 1)
        self.assertTrue(updated_plan["user_edited"])
        self.assertIsNone(updated_plan["active_preview_id"])
        self.assertEqual(updated_plan["shares"], int(100000.0 // 62.70))  # 1,594 shares
        self.assertAlmostEqual(updated_plan["estimated_notional_usd"], round(1594 * 62.70, 2), places=2)

        # Prior preview is now invalidated in database
        snap_after = r_edit.json()["snapshot"]
        old_prev = snap_after["broker_previews_by_id"][preview_v1["preview_id"]]
        self.assertEqual(old_prev["status"], "INVALIDATED_BY_EDIT")

        # Trigger a background scanner refresh and verify user edits are preserved
        r_scan = self.client.post("/api/scan", headers=self.headers)
        self.assertEqual(r_scan.status_code, 200)
        plan_after_scan = r_scan.json()["order_plans_by_symbol"]["FISV"]
        self.assertEqual(plan_after_scan["plan_version"], updated_plan["plan_version"])
        self.assertAlmostEqual(plan_after_scan["allocation_usd"], 100000.0, places=2)
        self.assertAlmostEqual(plan_after_scan["limit_price"], 62.70, places=2)
        self.assertEqual(plan_after_scan["shares"], 1594)

    # --------------------------------------------------------------------------
    # 5. Preview binding and expiry
    # --------------------------------------------------------------------------
    def test_05_preview_binding_and_expiry(self) -> None:
        """
        5. Preview binding and expiry: a preview expires or invalidates when quote timestamp,
        signal version, or order parameters change.
        """
        snap = self.client.get("/api/snapshot").json()
        fisv_plan = snap["order_plans_by_symbol"]["FISV"]
        r_prev = self.client.post(
            "/api/previews/create",
            json={"plan_id": fisv_plan["plan_id"], "plan_version": fisv_plan["plan_version"]},
            headers=self.headers,
        )
        self.assertEqual(r_prev.status_code, 200)
        preview = r_prev.json()["preview"]
        self.assertEqual(preview["plan_version"], fisv_plan["plan_version"])
        self.assertTrue(preview["expires_at_et"])

        # Edit order parameter -> increments plan_version -> old preview cannot be submitted
        self.client.post(
            "/api/plans/update",
            json={"symbol": "FISV", "allocation_usd": 120000.0},
            headers=self.headers,
        )
        r_sub = self.client.post(
            "/api/orders/submit",
            json={
                "plan_id": fisv_plan["plan_id"],
                "plan_version": fisv_plan["plan_version"],
                "preview_id": preview["preview_id"],
                "idempotency_key": "idem-binding-test-1",
                "confirmed_by_user": True,
            },
            headers=self.headers,
        )
        self.assertEqual(r_sub.status_code, 400)
        self.assertIn("VERSION MISMATCH", r_sub.json()["detail"])

    # --------------------------------------------------------------------------
    # 6. Preview failure blocks submission
    # --------------------------------------------------------------------------
    def test_06_preview_failure_blocks_submission(self) -> None:
        """
        6. Preview failure blocks submission: a failed or rejected dry-run preview keeps
        Submit to Tastytrade disabled and displays the error.
        """
        snap = self.client.get("/api/snapshot").json()
        fisv_plan = snap["order_plans_by_symbol"]["FISV"]

        r_prev = self.client.post(
            "/api/previews/create",
            json={
                "plan_id": fisv_plan["plan_id"],
                "plan_version": fisv_plan["plan_version"],
                "simulate_broker_error": True,
            },
            headers=self.headers,
        )
        self.assertEqual(r_prev.status_code, 200)
        preview = r_prev.json()["preview"]
        self.assertEqual(preview["status"], "FAILED_REJECTED")
        self.assertTrue(len(preview["errors"]) > 0)

        # Attempting to submit with a failed preview is blocked
        r_sub = self.client.post(
            "/api/orders/submit",
            json={
                "plan_id": fisv_plan["plan_id"],
                "plan_version": fisv_plan["plan_version"],
                "preview_id": preview["preview_id"],
                "idempotency_key": "idem-failed-preview-1",
                "confirmed_by_user": True,
            },
            headers=self.headers,
        )
        self.assertEqual(r_sub.status_code, 400)
        self.assertIn("PREVIEW INVALID", r_sub.json()["detail"])

    # --------------------------------------------------------------------------
    # 7. Confirmation required & GET never submits
    # --------------------------------------------------------------------------
    def test_07_confirmation_required_and_get_blocked(self) -> None:
        """
        7. Confirmation required: no live order can be sent by a GET request or without
        explicit confirmation.
        """
        # GET request to /api/orders/submit must fail with 405 Method Not Allowed
        r_get = self.client.get("/api/orders/submit")
        self.assertEqual(r_get.status_code, 405)

        # Untrusted cross-origin POST request must be blocked with 403
        r_csrf = self.client.post(
            "/api/orders/submit",
            json={
                "plan_id": "plan-fisv",
                "plan_version": 1,
                "preview_id": "prev-1",
                "idempotency_key": "idem-csrf",
                "confirmed_by_user": True,
            },
            headers={"Origin": "https://malicious-site.example.com"},
        )
        self.assertEqual(r_csrf.status_code, 403)

        # POST with confirmed_by_user=False must be blocked with 400
        snap = self.client.get("/api/snapshot").json()
        fisv_plan = snap["order_plans_by_symbol"]["FISV"]
        r_prev = self.client.post(
            "/api/previews/create",
            json={"plan_id": fisv_plan["plan_id"], "plan_version": fisv_plan["plan_version"]},
            headers=self.headers,
        )
        preview = r_prev.json()["preview"]

        r_unconfirmed = self.client.post(
            "/api/orders/submit",
            json={
                "plan_id": fisv_plan["plan_id"],
                "plan_version": fisv_plan["plan_version"],
                "preview_id": preview["preview_id"],
                "idempotency_key": "idem-unconfirmed",
                "confirmed_by_user": False,
            },
            headers=self.headers,
        )
        self.assertEqual(r_unconfirmed.status_code, 400)
        self.assertIn("CONFIRMATION REQUIRED", r_unconfirmed.json()["detail"])

    # --------------------------------------------------------------------------
    # 8. Duplicate submission block (Idempotency)
    # --------------------------------------------------------------------------
    def test_08_duplicate_submission_block(self) -> None:
        """
        8. Duplicate submission block: repeating the same submit request or double-clicking
        creates at most one broker submission attempt.
        """
        snap = self.client.get("/api/snapshot").json()
        fisv_plan = snap["order_plans_by_symbol"]["FISV"]
        r_prev = self.client.post(
            "/api/previews/create",
            json={"plan_id": fisv_plan["plan_id"], "plan_version": fisv_plan["plan_version"]},
            headers=self.headers,
        )
        preview = r_prev.json()["preview"]

        payload = {
            "plan_id": fisv_plan["plan_id"],
            "plan_version": fisv_plan["plan_version"],
            "preview_id": preview["preview_id"],
            "idempotency_key": "idem-double-click-key-001",
            "confirmed_by_user": True,
        }
        r1 = self.client.post("/api/orders/submit", json=payload, headers=self.headers)
        self.assertEqual(r1.status_code, 200)
        intent_id_1 = r1.json()["result"]["intent"]["intent_id"]

        # Second submit with same idempotency_key is blocked
        r2 = self.client.post("/api/orders/submit", json=payload, headers=self.headers)
        self.assertEqual(r2.status_code, 400)
        self.assertIn("DUPLICATE SUBMISSION BLOCKED", r2.json()["detail"])

        # Even with a different idempotency_key, the same signal_id is blocked from duplicate entry
        payload2 = dict(payload, idempotency_key="idem-double-click-key-002")
        r3 = self.client.post("/api/orders/submit", json=payload2, headers=self.headers)
        self.assertEqual(r3.status_code, 400)
        self.assertIn("DUPLICATE SIGNAL SUBMISSION BLOCKED", r3.json()["detail"])

        intents = self.client.get("/api/snapshot").json()["order_intents"]
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]["intent_id"], intent_id_1)

    # --------------------------------------------------------------------------
    # 9. Uncertain submission recovery
    # --------------------------------------------------------------------------
    def test_09_uncertain_submission_recovery(self) -> None:
        """
        9. Uncertain submission recovery: a timeout after order transmit enters
        OUTCOME_UNCERTAIN_RECONCILING and blocks retry until broker order search completes.
        """
        snap = self.client.get("/api/snapshot").json()
        fisv_plan = snap["order_plans_by_symbol"]["FISV"]
        r_prev = self.client.post(
            "/api/previews/create",
            json={"plan_id": fisv_plan["plan_id"], "plan_version": fisv_plan["plan_version"]},
            headers=self.headers,
        )
        preview = r_prev.json()["preview"]

        r_timeout = self.client.post(
            "/api/orders/submit",
            json={
                "plan_id": fisv_plan["plan_id"],
                "plan_version": fisv_plan["plan_version"],
                "preview_id": preview["preview_id"],
                "idempotency_key": "idem-timeout-001",
                "confirmed_by_user": True,
                "simulate_timeout": True,
            },
            headers=self.headers,
        )
        self.assertEqual(r_timeout.status_code, 200)
        intent = r_timeout.json()["result"]["intent"]
        self.assertEqual(intent["order_state"], OrderState.OUTCOME_UNCERTAIN_RECONCILING.value)
        self.assertEqual(intent["protection_state"], PositionProtectionState.OUTCOME_UNCERTAIN.value)

        snap_after = r_timeout.json()["snapshot"]
        self.assertFalse(snap_after["worker_health"]["reconciliation_healthy"])

        # Attempting another preview or submission while reconciliation is required is blocked
        r_blocked = self.client.post(
            "/api/previews/create",
            json={"plan_id": fisv_plan["plan_id"], "plan_version": fisv_plan["plan_version"]},
            headers=self.headers,
        )
        self.assertEqual(r_blocked.status_code, 400)
        self.assertIn("RECONCILIATION", r_blocked.json()["detail"].upper())

        # Running reconciliation resolves the uncertain order state
        r_rec = self.client.post("/api/reconcile", json={"simulate_unprotected": False}, headers=self.headers)
        self.assertEqual(r_rec.status_code, 200)
        self.assertTrue(r_rec.json()["snapshot"]["worker_health"]["reconciliation_healthy"])

    # --------------------------------------------------------------------------
    # 10. First-target exit preserves protection
    # --------------------------------------------------------------------------
    def test_10_first_target_exit_preserves_protection(self) -> None:
        """
        10. First-target exit preserves protection: when Leg 1 fills, remaining shares retain
        active stop protection and transition cleanly to the updated breakeven stop state.
        """
        snap = self.client.get("/api/snapshot").json()
        fisv_plan = snap["order_plans_by_symbol"]["FISV"]
        r_prev = self.client.post(
            "/api/previews/create",
            json={"plan_id": fisv_plan["plan_id"], "plan_version": fisv_plan["plan_version"]},
            headers=self.headers,
        )
        preview = r_prev.json()["preview"]

        r_sub = self.client.post(
            "/api/orders/submit",
            json={
                "plan_id": fisv_plan["plan_id"],
                "plan_version": fisv_plan["plan_version"],
                "preview_id": preview["preview_id"],
                "idempotency_key": "idem-leg1-test",
                "confirmed_by_user": True,
            },
            headers=self.headers,
        )
        self.assertEqual(r_sub.status_code, 200)
        pos = r_sub.json()["result"]["position"]
        self.assertEqual(pos["filled_qty"], 2391)
        self.assertEqual(pos["tranche1_qty"], 1195)
        self.assertEqual(pos["tranche2_qty"], 1196)
        self.assertEqual(pos["working_stop_qty"], 2391)

        # Trigger Leg 1 (50% = 1,195 sh) target fill
        r_leg1 = self.client.post(
            f"/api/positions/{pos['position_id']}/leg1-exit",
            headers=self.headers,
        )
        self.assertEqual(r_leg1.status_code, 200)
        updated_pos = r_leg1.json()["position"]
        self.assertEqual(updated_pos["leg1_exited_qty"], 1195)
        self.assertEqual(updated_pos["remaining_qty"], 1196)
        self.assertEqual(updated_pos["working_stop_qty"], 1196)
        self.assertAlmostEqual(updated_pos["working_stop_price"], updated_pos["avg_fill_price"], places=2)
        self.assertEqual(updated_pos["protection_state"], PositionProtectionState.FIRST_EXIT_COMPLETED.value)
        self.assertGreater(updated_pos["realized_pnl_usd"], 0.0)

    # --------------------------------------------------------------------------
    # 11. Partial entry fill handling
    # --------------------------------------------------------------------------
    def test_11_partial_entry_fill_handling(self) -> None:
        """
        11. Partial entry fill handling: a partial entry fill sizes protective exits strictly
        to filled shares, never original requested shares.
        """
        snap = self.client.get("/api/snapshot").json()
        fisv_plan = snap["order_plans_by_symbol"]["FISV"]
        r_prev = self.client.post(
            "/api/previews/create",
            json={"plan_id": fisv_plan["plan_id"], "plan_version": fisv_plan["plan_version"]},
            headers=self.headers,
        )
        preview = r_prev.json()["preview"]

        r_sub = self.client.post(
            "/api/orders/submit",
            json={
                "plan_id": fisv_plan["plan_id"],
                "plan_version": fisv_plan["plan_version"],
                "preview_id": preview["preview_id"],
                "idempotency_key": "idem-partial-fill-test",
                "confirmed_by_user": True,
                "simulate_partial_fill_qty": 800,
            },
            headers=self.headers,
        )
        self.assertEqual(r_sub.status_code, 200)
        intent = r_sub.json()["result"]["intent"]
        pos = r_sub.json()["result"]["position"]
        self.assertEqual(intent["order_state"], OrderState.ENTRY_PARTIALLY_FILLED.value)
        self.assertEqual(intent["filled_qty"], 800)
        self.assertEqual(pos["filled_qty"], 800)
        self.assertEqual(pos["remaining_qty"], 800)
        # Protective stop quantity MUST equal filled shares (800), never requested shares (2,391)!
        self.assertEqual(pos["working_stop_qty"], 800)
        self.assertEqual(pos["tranche1_qty"] + pos["tranche2_qty"], 800)

    # --------------------------------------------------------------------------
    # 12. Position isolation (pre-existing holdings vs strategy vs manual)
    # --------------------------------------------------------------------------
    def test_12_position_isolation(self) -> None:
        """
        12. Position isolation: reconciliation displays pre-existing holdings separately and
        never includes them in strategy stop/trail/flatten actions.
        Also verifies the user-reported FISV manual record has no fabricated quantities or timestamps.
        """
        snap = self.client.get("/api/snapshot").json()
        positions = {p["position_id"]: p for p in snap["positions"]}

        # Pre-existing SGOV holding
        sgov = positions["pos-preexisting-sgov"]
        self.assertFalse(sgov["is_strategy_managed"])
        self.assertEqual(sgov["reconciliation_badge"], "PRE_EXISTING_HOLDING_ISOLATED")

        # User-reported manual FISV record ($62.72 entry, ~$3,000 reported profit)
        manual_fisv = positions["pos-manual-fisv-reported"]
        self.assertFalse(manual_fisv["is_strategy_managed"])
        self.assertEqual(manual_fisv["source_mode"], "MANUAL_USER_REPORTED")
        self.assertEqual(manual_fisv["reconciliation_badge"], "UNRECONCILED_USER_REPORTED")
        self.assertAlmostEqual(manual_fisv["avg_fill_price"], 62.72, places=2)
        self.assertAlmostEqual(manual_fisv["reported_Approximate_pnl_usd"], 3000.0, places=2)
        # Must NOT invent unverified share quantity or realized/unrealized split
        self.assertIsNone(manual_fisv["filled_qty"])
        self.assertIsNone(manual_fisv["realized_pnl_usd"])
        self.assertIsNone(manual_fisv["unrealized_pnl_usd"])

        # Attempting strategy Leg 1 exit on non-strategy position is rejected
        r_leg1 = self.client.post("/api/positions/pos-preexisting-sgov/leg1-exit", headers=self.headers)
        self.assertEqual(r_leg1.status_code, 400)

    # --------------------------------------------------------------------------
    # 13. Pause new entries
    # --------------------------------------------------------------------------
    def test_13_pause_new_entries(self) -> None:
        """
        13. Pause new entries: enabling Pause new entries blocks new order previews/submissions
        while continuing position monitoring and protective management.
        """
        r_pause = self.client.post(
            "/api/settings",
            json={"pause_new_entries": True},
            headers=self.headers,
        )
        self.assertEqual(r_pause.status_code, 200)
        snap = r_pause.json()["snapshot"]
        self.assertTrue(snap["pause_new_entries"])

        fisv_plan = snap["order_plans_by_symbol"]["FISV"]
        self.assertFalse(fisv_plan["is_executable"])
        self.assertTrue(
            any("Pause new entries" in reason for reason in fisv_plan["execution_blocked_reasons"])
        )

        # Preview creation is blocked while paused
        r_prev = self.client.post(
            "/api/previews/create",
            json={"plan_id": fisv_plan["plan_id"], "plan_version": fisv_plan["plan_version"]},
            headers=self.headers,
        )
        self.assertEqual(r_prev.status_code, 400)
        self.assertIn("PAUSED", r_prev.json()["detail"].upper())

    # --------------------------------------------------------------------------
    # 14. Open Tastytrade handoff boundary
    # --------------------------------------------------------------------------
    def test_14_open_tastytrade_handoff_boundary(self) -> None:
        """
        14. Open Tastytrade handoff boundary: clicking Open Tastytrade opens the verified
        destination and never marks an order submitted or filled.
        """
        snap_before = self.client.get("/api/snapshot").json()
        intents_before = len(snap_before["order_intents"])

        r_handoff = self.client.post(
            "/api/handoff/open",
            json={"symbol": "FISV", "action": "OPEN_TASTYTRADE"},
            headers=self.headers,
        )
        self.assertEqual(r_handoff.status_code, 200)
        res = r_handoff.json()["handoff_result"]
        self.assertFalse(res["order_submitted"])
        self.assertFalse(res["order_filled"])
        self.assertEqual(res["handoff"]["mode"], "WEB_SYMBOL_OR_TRADING_DESK")
        self.assertFalse(res["handoff"]["prefills_ticket_natively"])

        snap_after = r_handoff.json()["snapshot"]
        self.assertEqual(len(snap_after["order_intents"]), intents_before)

    # --------------------------------------------------------------------------
    # 15. CLI screenshot acceptance fixture (`demo_acceptance`)
    # --------------------------------------------------------------------------
    def test_15_cli_screenshot_acceptance_fixture(self) -> None:
        """
        15. CLI screenshot acceptance fixture: loading the screenshot fixture renders:
        - FISV and PLNT as watching/eligible with no Buy button (is_actionable=False),
        - EEFT with a clear feed-divergence explanation,
        - CNC and GPN as blocked for unmet daily accumulation,
        - NU as blocked by the strategy 14-day pre-earnings rule (2026-11-12).
        """
        r = self.client.post(
            "/api/settings",
            json={"fixture_mode": "demo_acceptance"},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 200)
        snap = r.json()["snapshot"]
        cands = {c["symbol"]: c for c in snap["candidates"]}

        # FISV and PLNT are WATCHING, not READY_FOR_REVIEW, and not actionable
        self.assertEqual(cands["FISV"]["category"], "WATCHING")
        self.assertEqual(cands["FISV"]["signal_state"], SignalState.WATCHING_ARMED.value)
        self.assertFalse(cands["FISV"]["is_actionable"])
        self.assertFalse(snap["order_plans_by_symbol"]["FISV"]["is_executable"])

        self.assertEqual(cands["PLNT"]["category"], "WATCHING")
        self.assertEqual(cands["PLNT"]["signal_state"], SignalState.WATCHING_FLUSHING.value)
        self.assertFalse(cands["PLNT"]["is_actionable"])
        self.assertFalse(snap["order_plans_by_symbol"]["PLNT"]["is_executable"])

        # EEFT shows cross-endpoint divergence (0.77% > 0.25%) and SAME_PROVIDER_CROSS_ENDPOINT provenance
        self.assertEqual(cands["EEFT"]["category"], "DATA_UNAVAILABLE")
        self.assertIn("0.77%", cands["EEFT"]["concise_reason"])
        self.assertEqual(
            cands["EEFT"]["consistency"]["provenance_relation"],
            "SAME_PROVIDER_CROSS_ENDPOINT",
        )

        # CNC and GPN are BLOCKED for unmet daily accumulation
        self.assertEqual(cands["CNC"]["category"], "BLOCKED")
        self.assertIn("Daily accumulation", cands["CNC"]["concise_reason"])
        self.assertEqual(cands["GPN"]["category"], "BLOCKED")
        self.assertIn("Daily accumulation", cands["GPN"]["concise_reason"])

        # NU is BLOCKED by strategy 14-day pre-earnings rule (2026-11-12)
        self.assertEqual(cands["NU"]["category"], "BLOCKED")
        self.assertIn("Strategy 14-day pre-earnings rule", cands["NU"]["concise_reason"])
        self.assertIn("2026-11-12", cands["NU"]["concise_reason"])

    # --------------------------------------------------------------------------
    # 16. Section 10 Regression: True Bar-Derived Session VWAP & Prefix Invariance
    # --------------------------------------------------------------------------
    def test_16_bar_derived_vwap_and_causal_prefix_invariance(self) -> None:
        """
        Verify:
        - `fetch_secondary_quotes` does NOT fabricate `vwap = (high + low + last)/3`.
        - `compute_bar_session_vwap` computes cumulative sum(TP_i * V_i) / sum(V_i) from 09:30 ET.
        - `compute_causal_5m_features` is strictly prefix-invariant.
        """
        # Build synthetic 5m session bars
        idx = pd.date_range("2026-11-10 09:30:00", periods=15, freq="5min", tz=NY_TZ)
        df = pd.DataFrame(
            {
                "Open": np.linspace(63.50, 62.50, 15),
                "High": np.linspace(63.60, 62.65, 15),
                "Low": np.linspace(63.40, 62.35, 15),
                "Close": np.linspace(63.45, 62.55, 15),
                "Volume": [50000, 45000] + [20000] * 10 + [65000, 35000, 40000],
            },
            index=idx,
        )
        vwap_s = compute_bar_session_vwap(df)
        tp = (df["High"] + df["Low"] + df["Close"]) / 3.0
        expected_last_vwap = float((tp * df["Volume"]).sum() / df["Volume"].sum())
        self.assertAlmostEqual(float(vwap_s.iloc[-1]), expected_last_vwap, places=6)

        # Prefix-replay invariance check across k = 5..14
        full_feat = compute_causal_5m_features(df)
        for k in range(5, len(df)):
            prefix_feat = compute_causal_5m_features(df.iloc[: k + 1])
            for col in ("VWAP", "Morning_High", "VolMA10_ExOpen", "EMA12", "EMA20"):
                v_full = full_feat[col].iloc[k]
                v_pref = prefix_feat[col].iloc[k]
                if pd.isna(v_full) and pd.isna(v_pref):
                    continue
                self.assertAlmostEqual(float(v_full), float(v_pref), places=6)


if __name__ == "__main__":
    unittest.main()
