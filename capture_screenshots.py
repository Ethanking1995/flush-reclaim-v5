"""
capture_screenshots.py
======================
Automated visual verification and screenshot generator for the 5 required UI states
specified in Section 13:
  1. `ready_state.png`        — 1366x900 desktop: FISV READY_FOR_REVIEW with prefilled Order Review Ticket
  2. `watching_state.png`     — 1366x900 desktop: CLI Screenshot Acceptance Fixture (FISV & PLNT watching, no Buy button)
  3. `blocked_state.png`      — 1366x900 desktop: NU Blocked by Strategy 14-day pre-earnings rule (2026-11-12)
  4. `open_position_state.png`— 1366x960 desktop: Strategy-managed FISV position open (Leg 1 exited, Tranche B stop @ BE) + SGOV isolated + Manual User-Reported record
  5. `mobile_390px_state.png` — 390x844 mobile viewport: Responsive layout with Order Review drawer
"""

from __future__ import annotations
import os
import shutil
import subprocess
import tempfile
import threading
import time
import httpx
import uvicorn

from backend.app import create_app


CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 8095
BASE_URL = f"http://127.0.0.1:{PORT}"
HEADERS = {"X-FlushReclaim-Confirm": "true"}


def take_chrome_screenshot(url: str, output_path: str, width: int, height: int) -> None:
    cmd = [
        CHROME_BIN,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        f"--window-size={width},{height}",
        "--virtual-time-budget=3500",
        f"--screenshot={output_path}",
        url,
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> None:
    root_dir = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.path.join(root_dir, "docs", "screenshots")
    os.makedirs(out_dir, exist_ok=True)
    artifact_dir = "/Users/ethanking/.gemini/antigravity/brain/6f325843-b44d-43e8-84c4-1b914ef66e1d"

    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "screenshots.db")
        app = create_app(db_path=db_path, start_background_worker=False)
        config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error")
        server = uvicorn.Server(config)
        t = threading.Thread(target=server.run, daemon=True)
        t.start()

        # Wait for server ready
        for _ in range(30):
            try:
                r = httpx.get(f"{BASE_URL}/api/health", timeout=1.0)
                if r.status_code == 200:
                    break
            except Exception:
                time.sleep(0.2)

        client = httpx.Client(base_url=BASE_URL, headers=HEADERS, timeout=10.0)

        # 1. READY STATE (`demo_ready`, FISV selected, bound preview created)
        client.post("/api/settings", json={"fixture_mode": "demo_ready", "operating_mode": "PAPER_TRADING"})
        snap = client.get("/api/snapshot").json()
        fisv_plan = snap["order_plans_by_symbol"]["FISV"]
        prev_res = client.post(
            "/api/previews/create",
            json={"plan_id": fisv_plan["plan_id"], "plan_version": fisv_plan["plan_version"]},
        ).json()
        p1 = os.path.join(out_dir, "01_ready_state.png")
        take_chrome_screenshot(f"{BASE_URL}/?symbol=FISV&nosse=1", p1, 1366, 920)
        shutil.copy2(p1, os.path.join(artifact_dir, "01_ready_state.png"))

        # 2. WATCHING STATE (`demo_acceptance` CLI Screenshot Acceptance Fixture)
        client.post("/api/settings", json={"fixture_mode": "demo_acceptance"})
        p2 = os.path.join(out_dir, "02_watching_acceptance_state.png")
        take_chrome_screenshot(f"{BASE_URL}/?symbol=FISV&nosse=1", p2, 1366, 920)
        shutil.copy2(p2, os.path.join(artifact_dir, "02_watching_acceptance_state.png"))

        # 3. BLOCKED STATE (`demo_acceptance`, NU selected showing Strategy 14-day pre-earnings rule)
        p3 = os.path.join(out_dir, "03_blocked_state.png")
        take_chrome_screenshot(f"{BASE_URL}/?symbol=NU&nosse=1", p3, 1366, 920)
        shutil.copy2(p3, os.path.join(artifact_dir, "03_blocked_state.png"))

        # 4. OPEN POSITION STATE (`demo_ready`, submit FISV order + simulate Leg 1 fill -> BE stop)
        client.post("/api/settings", json={"fixture_mode": "demo_ready", "operating_mode": "PAPER_TRADING"})
        snap = client.get("/api/snapshot").json()
        fisv_plan = snap["order_plans_by_symbol"]["FISV"]
        prev_res = client.post(
            "/api/previews/create",
            json={"plan_id": fisv_plan["plan_id"], "plan_version": fisv_plan["plan_version"]},
        ).json()
        sub_res = client.post(
            "/api/orders/submit",
            json={
                "plan_id": fisv_plan["plan_id"],
                "plan_version": fisv_plan["plan_version"],
                "preview_id": prev_res["preview"]["preview_id"],
                "idempotency_key": "screenshot-open-pos-001",
                "confirmed_by_user": True,
            },
        ).json()
        pos_id = sub_res["result"]["position"]["position_id"]
        client.post(f"/api/positions/{pos_id}/leg1-exit")
        p4 = os.path.join(out_dir, "04_open_position_state.png")
        take_chrome_screenshot(f"{BASE_URL}/?symbol=FISV&diag=1&nosse=1", p4, 1366, 1260)
        shutil.copy2(p4, os.path.join(artifact_dir, "04_open_position_state.png"))

        # 5. MOBILE 390px STATE (`390x844` with Order Review Ticket drawer open)
        p5 = os.path.join(out_dir, "05_mobile_390px_state.png")
        take_chrome_screenshot(f"{BASE_URL}/?symbol=FISV&drawer=open&nosse=1", p5, 390, 844)
        shutil.copy2(p5, os.path.join(artifact_dir, "05_mobile_390px_state.png"))

        server.should_exit = True
        t.join(timeout=3.0)
        print("Captured all 5 required screenshots successfully!")


if __name__ == "__main__":
    main()
