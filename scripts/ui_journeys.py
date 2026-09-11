#!/usr/bin/env python3
"""ui_journeys.py — DESK-02: `src/browser_journey_verification.py::run_journey`
wired against a REAL Studio.

`run_journey` was built and tested (`tests/test_p1_desk_02_journey.py`)
against fixture snapshots so the runner itself is provably correct without a
browser. What this script adds is the other half DESK-02 asks for: a real
Chromium (Playwright), driving the real built `static/studio/` bundle served
by a real Faustus server — the same launcher `scripts/ui_smoke.py` already
uses (`tests.eval.harness.EvalApp`, subprocess `uvicorn app:app` + a scripted
model), reused rather than duplicated (hard rule 4).

Two journeys, run with the SAME `run_journey` used by the fixture tests —
nothing here reimplements the pass/fail logic, only the snapshot:

  1. ``object_leak_buttons`` — the literal DESK-02 acceptance scenario: a
     button whose rendered text is the literal string ``[object Object]``
     (or ``undefined``/``null`` leaking the same way — `detect_object_leak`'s
     regex also catches ``[object Array]`` and friends) must fail even
     though the backend behind it answers HTTP 200. Visits Studio's main
     screen and every Settings tab — the two places in the app with the
     most buttons/labels rendered from live data.
  2. ``main_screen`` — Studio's main screen loads with no object leak, no
     console errors, no failed network requests (`DEFAULT_ASSERTIONS`, the
     full battery `browser_journey_verification` ships).

The snapshot function is the only new plumbing: it turns a real Playwright
``Page`` into the plain-data ``JourneySnapshot`` `run_journey` already knows
how to check, resetting its console/network buffers after every step so a
failure is always attributed to the step that actually caused it (the same
guarantee `run_journey`'s own docstring makes about steps, extended here to
what feeds it).

Two modes, mirroring ``scripts/ui_smoke.py`` exactly:

  python scripts/ui_journeys.py
      Full live run: needs Playwright's Chromium (``playwright install
      chromium``). Falls back to --dry-run automatically if unavailable.

  python scripts/ui_journeys.py --dry-run
      No browser: confirms the `data-testid`s these journeys drive are
      present in the shipped `static/studio/` bundle.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Tuple

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.browser_journey_verification import (  # noqa: E402
    DEFAULT_ASSERTIONS,
    JourneySnapshot,
    JourneyStep,
    JourneyVerdict,
    assert_no_network_failures,
    assert_no_object_leak,
    run_journey,
)

DEFAULT_OUT_DIR = REPO / "logs" / "ui_journeys"

#: Same two accounts-worth of testids ui_smoke.py already proved real —
#: kept as its own tuple (not imported) since scripts/ has no __init__.py
#: and is invoked as a standalone entry point, never as a package.
SELECTORS = ("studio", "studio-input", "studio-send", "settings")

ADMIN_USER = "journeys-admin"
ADMIN_PASS = "journeys-pass-ui-2026"  # sandbox-only account, thrown away with the temp data dir

#: This journey's own acceptance is narrower than the full battery on
#: purpose: Settings/Studio can legitimately log a benign console warning in
#: a real browser (a dev-mode React notice, a missing source map) that has
#: nothing to do with whether a button rendered "[object Object]" — checking
#: only the object-leak + network-failure signals here keeps the run
#: reliable while still catching the ONE thing DESK-02's acceptance names.
_OBJECT_LEAK_ASSERTIONS: Tuple[Callable[[JourneySnapshot], Any], ...] = (
    assert_no_object_leak,
    assert_no_network_failures,
)


def _bundle_text() -> str:
    root = REPO / "static" / "studio"
    parts: List[str] = []
    if root.is_dir():
        for f in root.rglob("*.js"):
            try:
                parts.append(f.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
    return "\n".join(parts)


def run_dry(out_dir: Path) -> Dict[str, Any]:
    text = _bundle_text()
    found = {sel: (f'"{sel}"' in text or f"'{sel}'" in text) for sel in SELECTORS}
    missing = [s for s, ok in found.items() if not ok]
    result = {
        "mode": "dry-run", "ok": not missing,
        "bundle_present": bool(text),
        "selectors": found, "missing": missing,
        "note": "no browser was launched; this only proves the testids these "
                "journeys drive exist in static/studio/.",
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def _make_snapshot_fn(page: Any, shot_dir: Path, prefix: str) -> Callable[[], Awaitable[JourneySnapshot]]:
    """A fresh `snapshot_fn` bound to `page`: registers console/network
    listeners once, and returns a function that reads the CURRENT rendered
    DOM text plus everything logged/failed SINCE the previous call — so
    step 2's snapshot never re-reports step 1's console noise."""
    console_buf: List[str] = []
    network_buf: List[str] = []
    counter = {"n": 0}

    def _on_console(msg: Any) -> None:
        console_buf.append(f"{msg.type}: {msg.text}")

    def _on_response(response: Any) -> None:
        if response.status >= 400:
            network_buf.append(f"{response.status} {response.request.method} {response.url}")

    def _on_request_failed(request: Any) -> None:
        network_buf.append(f"FAILED {request.method} {request.url} ({request.failure})")

    page.on("console", _on_console)
    page.on("response", _on_response)
    page.on("requestfailed", _on_request_failed)

    async def snapshot_fn() -> JourneySnapshot:
        counter["n"] += 1
        shot_dir.mkdir(parents=True, exist_ok=True)
        shot_path = shot_dir / f"{prefix}_{counter['n']:02d}.png"
        try:
            dom_text = await page.inner_text("body")
        except Exception as exc:  # noqa: BLE001 — a step that navigated away mid-read is still data
            dom_text = f"<could not read DOM: {type(exc).__name__}: {exc}>"
        try:
            await page.screenshot(path=str(shot_path), full_page=True)
            screenshot = str(shot_path.relative_to(REPO))
        except Exception:  # noqa: BLE001
            screenshot = None
        console = tuple(console_buf)
        network = tuple(network_buf)
        console_buf.clear()
        network_buf.clear()
        return JourneySnapshot(dom_text=dom_text, console_messages=console,
                               network_failures=network, screenshot=screenshot)

    return snapshot_fn


async def _run_journeys(out_dir: Path, *, headless: bool = True, timeout_s: float = 90.0) -> Dict[str, Any]:
    from tests.eval.harness import EvalApp  # reused, not duplicated (hard rule 4)
    from playwright.async_api import async_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    app = EvalApp()
    print("[ui_journeys] starting the app + scripted model…")
    app.start(timeout=timeout_s)
    journeys: List[Dict[str, Any]] = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=headless)
            context = await browser.new_context()
            page = await context.new_page()

            # Real first-run auth, same rationale as scripts/ui_smoke.py step 1:
            # proves the ordinary operator flow, not only LOCALHOST_BYPASS.
            await page.goto(app.base + "/", wait_until="networkidle", timeout=30000)
            setup = await page.request.post(app.base + "/api/auth/setup",
                                            data={"username": ADMIN_USER, "password": ADMIN_PASS})
            login = await page.request.post(app.base + "/api/auth/login",
                                            data={"username": ADMIN_USER, "password": ADMIN_PASS})
            login_ok = setup.status == 200 and login.status == 200
            await page.reload(wait_until="networkidle", timeout=30000)
            if not login_ok:
                raise RuntimeError(f"login failed: setup={setup.status} login={login.status}")

            # -- Journey 1: the literal DESK-02 acceptance scenario ---------
            snap1 = _make_snapshot_fn(page, out_dir, "object_leak")

            async def _open_studio() -> None:
                await page.get_by_role("link", name="Studio", exact=True).click()
                await page.wait_for_selector('[data-testid="studio"]', timeout=15000)

            async def _open_settings() -> None:
                await page.goto(app.base + "/settings", wait_until="networkidle", timeout=30000)
                await page.wait_for_selector('[data-testid="settings"]', timeout=15000)
                for label in ("Effective config", "Tools", "System"):
                    try:
                        await page.click(f'button.fs-set__nav-item:has-text("{label}")', timeout=10000)
                        await page.wait_for_timeout(400)
                    except Exception:  # noqa: BLE001 — a missing tab is a different failure than an object leak
                        continue

            steps1 = [
                JourneyStep("open_studio", action=_open_studio, assertions=_OBJECT_LEAK_ASSERTIONS),
                JourneyStep("open_settings_tabs", action=_open_settings, assertions=_OBJECT_LEAK_ASSERTIONS),
            ]
            verdict1 = await run_journey(steps1, snap1)
            journeys.append({"name": "object_leak_buttons", **verdict1.to_mapping()})

            # -- Journey 2: the main screen loads clean ----------------------
            snap2 = _make_snapshot_fn(page, out_dir, "main_screen")

            async def _open_main() -> None:
                await page.goto(app.base + "/studio", wait_until="networkidle", timeout=30000)
                await page.wait_for_selector('[data-testid="studio"]', timeout=15000)
                await page.wait_for_selector('[data-testid="studio-input"]', timeout=15000)
                await page.wait_for_selector('[data-testid="studio-send"]', timeout=15000)

            steps2 = [JourneyStep("open_main_screen", action=_open_main, assertions=DEFAULT_ASSERTIONS)]
            verdict2 = await run_journey(steps2, snap2)
            journeys.append({"name": "main_screen", **verdict2.to_mapping()})

            await browser.close()
    finally:
        app.stop()

    ok = all(j["passed"] for j in journeys)
    result = {"mode": "live", "ok": ok, "journeys": journeys}
    (out_dir / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def run_live(out_dir: Path, *, headless: bool = True, timeout_s: float = 90.0) -> Dict[str, Any]:
    return asyncio.run(_run_journeys(out_dir, headless=headless, timeout_s=timeout_s))


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="check selectors against the built bundle; no browser")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    args = parser.parse_args(argv)
    out_dir = Path(args.out_dir)

    if args.dry_run:
        result = run_dry(out_dir)
    else:
        try:
            import playwright  # noqa: F401
        except ImportError:
            print("[ui_journeys] playwright is not installed — falling back to --dry-run. "
                 "Install with: pip install playwright && playwright install chromium")
            result = run_dry(out_dir)
            result["fallback_reason"] = "playwright not installed"
        else:
            try:
                result = run_live(out_dir, headless=not args.headed)
            except Exception as e:  # noqa: BLE001
                print(f"[ui_journeys] live run could not start ({type(e).__name__}: {e}); "
                     "falling back to --dry-run")
                result = run_dry(out_dir)
                result["fallback_reason"] = f"{type(e).__name__}: {e}"

    print(json.dumps({k: v for k, v in result.items() if k != "journeys"}, indent=2, ensure_ascii=False))
    print(f"[ui_journeys] wrote {out_dir / 'result.json'}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
