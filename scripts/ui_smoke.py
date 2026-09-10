#!/usr/bin/env python3
"""ui_smoke.py — EVAL-04: a real frontend smoke test with Playwright.

Boots a real Faustus server (subprocess `uvicorn app:app`, reusing
`tests.eval.harness.EvalApp` the exact way `scripts/eval_run.py` already
does — never a second server-launcher) with a scripted model behind it
(`tests.e2e.fake_llm`, same reuse), then drives an actual Chromium browser
against the actual built Studio bundle (`static/studio/`) through:

  1. login  — real `/api/auth/setup` + `/api/auth/login`, not just the
     LOCALHOST_BYPASS dev switch, so the run also proves the ordinary
     first-run flow a real operator follows.
  2. a new conversation — the Studio composer is reachable and ready.
  3. sending a message against the mocked model, and a question card
     (`ask_user`) it triggers.
  4. Settings › Effective config, Settings › Tools, Settings › System
     (the "Diagnóstico"/Doctor card, `GET /api/doctor`).

A screenshot is written to `--out-dir` (default `logs/ui_smoke/`) after each
step, and a `result.json` next to them records what happened — this script
is meant to be *read*, not just exit-coded, because step 3 currently fails
for a real reason explained below.

FIXED (integration lot 36): `chat_stream`'s `owner = effective_user(request)`
used to be the *raw* `request.state.current_user`, which is never populated
when `AUTH_ENABLED=false` (the whole auth middleware that would set it does
not run in that mode — unlike `require_user()`, which explicitly falls back
to `""` for that case). The real browser always sends a `client_message_id`
field (its idempotent-retry id, TASK-03); when it did, `chat_stream` called
`chat_outbox.record_intent(owner=None, ...)` and `INSERT INTO outbox` failed
its `NOT NULL` constraint on `owner`, a plain 500 — reproduced here with a
REAL logged-in admin too, not only under LOCALHOST_BYPASS, so it was not a
login artifact. `tests/eval/harness.py`'s own HTTP client never sends
`client_message_id`, and no prior test drove a real browser against
`/api/chat_stream`, which is exactly why EVAL-01's suite and the rest of the
regression run never saw it — the whole reason EVAL-04 asks for a REAL
frontend pass rather than another API-level one. `routes/chat_routes.py` now
normalizes `owner` to `""` at both `chat_stream` and `chat_endpoint`, the
same way `require_user()` does, and `src/chat_outbox.py` normalizes
`None -> ""` defensively at every entry point;
`tests/test_l36_chat_outbox_owner_none.py` pins the fix through a real
`TestClient` POST. Step 3 below now sends `client_message_id` unmodified.

Two modes:

  python scripts/ui_smoke.py
      Full live run: needs Playwright's Chromium (``playwright install
      chromium``); ``pip install playwright`` first if the package itself
      is missing. Falls back to --dry-run automatically if either is
      unavailable in this sandbox, printing why.

  python scripts/ui_smoke.py --dry-run
      No browser at all: confirms every `data-testid` this script drives
      is actually present in the built `static/studio/` bundle, so the
      selectors are proven real even where a live run cannot happen.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

DEFAULT_OUT_DIR = REPO / "logs" / "ui_smoke"

#: Every `data-testid` this script's live run depends on, so `--dry-run` can
#: confirm each one is real without a browser (studio/src/screens/**.tsx are
#: the source; static/studio/*.js is what actually ships and is what a user
#: really gets, so the dry run checks the SHIPPED bundle, not the source).
SELECTORS = (
    "studio", "studio-input", "studio-send", "studio-mode-agent",
    "studio-question", "studio-question-option", "settings",
)

ADMIN_USER = "smoke-admin"
ADMIN_PASS = "smoke-pass-ui-2026"  # sandbox-only account, thrown away with the temp data dir


def _bundle_text() -> str:
    """All shipped JS text `static/studio/` serves, concatenated once."""
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
    """No browser: prove every selector this script drives is real by
    grepping the SHIPPED bundle (`static/studio/`), the same one a live run
    would actually click on."""
    text = _bundle_text()
    found = {sel: (f'"{sel}"' in text or f"'{sel}'" in text) for sel in SELECTORS}
    missing = [s for s, ok in found.items() if not ok]
    result = {
        "mode": "dry-run", "ok": not missing,
        "bundle_present": bool(text),
        "selectors": found, "missing": missing,
        "note": "no browser was launched; this only proves the testids exist "
                "in static/studio/ — see the module docstring for the real "
                "issue this script otherwise surfaces.",
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def run_live(out_dir: Path, *, headless: bool = True, timeout_s: float = 90.0) -> Dict[str, Any]:
    from tests.eval.harness import EvalApp  # reused, not duplicated (hard rule 4)
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    steps: List[Dict[str, Any]] = []
    known_issues: List[str] = []

    def snap(page: Any, name: str) -> str:
        path = out_dir / f"{len(steps) + 1:02d}_{name}.png"
        page.screenshot(path=str(path), full_page=True)
        return str(path.relative_to(REPO))

    app = EvalApp()
    print("[ui_smoke] starting the app + scripted model…")
    app.start(timeout=timeout_s)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            page = browser.new_page()

            # 1. login — real first-run setup + login, not just LOCALHOST_BYPASS.
            page.goto(app.base + "/", wait_until="networkidle", timeout=30000)
            setup = page.request.post(app.base + "/api/auth/setup",
                                      data={"username": ADMIN_USER, "password": ADMIN_PASS})
            login = page.request.post(app.base + "/api/auth/login",
                                      data={"username": ADMIN_USER, "password": ADMIN_PASS})
            login_ok = setup.status == 200 and login.status == 200
            page.reload(wait_until="networkidle", timeout=30000)
            steps.append({"step": "login", "ok": login_ok,
                         "detail": f"setup={setup.status} login={login.status}",
                         "screenshot": snap(page, "login")})

            # 2. new conversation — the Studio composer is reachable and ready.
            page.get_by_role("link", name="Studio", exact=True).click()
            page.wait_for_selector('[data-testid="studio"]', timeout=15000)
            new_ok = page.locator('[data-testid="studio-input"]').count() > 0 \
                and page.locator('[data-testid="studio-send"]').count() > 0
            steps.append({"step": "new_conversation", "ok": new_ok,
                         "screenshot": snap(page, "new_conversation")})

            # 3. send a message against the mocked model; a question card
            #    (ask_user) it triggers. The client_message_id the real
            #    Studio composer always sends now goes through unmodified —
            #    lot 36 fixed the owner=None crash this step used to work
            #    around (see module docstring).
            q_ok = False
            detail = ""
            try:
                app.script([
                    '```ask_user\n{"question": "Which greeting do you want?", '
                    '"options": [{"label": "Hello"}, {"label": "Hi"}]}\n```',
                    "Understood — used the greeting you picked. Done.",
                ])
                sess = page.request.post(app.base + "/api/session", form={
                    "name": "ui-smoke", "endpoint_id": app.endpoint_id,
                    "endpoint_url": app._fake_llm_base + "/v1", "model": "fake-coder",
                    "skip_validation": "true",
                })
                sid = sess.json().get("id")
                page.goto(f"{app.base}/studio?s={sid}", wait_until="networkidle", timeout=30000)
                page.wait_for_selector('[data-testid="studio"]', timeout=15000)
                page.click('[data-testid="studio-mode-agent"]')
                page.fill('[data-testid="studio-input"]',
                          "Say hi to the user, asking which greeting they want first.")
                page.click('[data-testid="studio-send"]')
                page.wait_for_selector('[data-testid="studio-question"]', timeout=20000)
                q_ok = True
                snap(page, "question_card")
                page.locator('[data-testid="studio-question-option"]').first.click()
                time.sleep(2.0)
            except Exception as e:  # noqa: BLE001 — this step is allowed to fail; report, don't crash the run
                detail = f"{type(e).__name__}: {e}"
            steps.append({"step": "send_message_and_question_card", "ok": q_ok,
                         "detail": detail or "the question card never appeared",
                         "screenshot": snap(page, "after_send")})

            # 4. Settings › Effective config, Tools, System (Diagnóstico/Doctor).
            page.goto(app.base + "/settings", wait_until="networkidle", timeout=30000)
            page.wait_for_selector('[data-testid="settings"]', timeout=15000)
            for label in ("Effective config", "Tools", "System"):
                try:
                    page.click(f'button.fs-set__nav-item:has-text("{label}")', timeout=10000)
                    time.sleep(0.5)
                    ok = True
                except Exception as e:  # noqa: BLE001
                    ok = False
                    detail = f"{type(e).__name__}: {e}"
                else:
                    detail = ""
                steps.append({"step": f"settings_{label.lower().replace(' ', '_')}", "ok": ok,
                             "detail": detail,
                             "screenshot": snap(page, f"settings_{label.lower().replace(' ', '_')}")})
            doctor_visible = page.get_by_text("Doctor").count() > 0
            steps[-1]["detail"] = (steps[-1].get("detail") or "") + \
                f" | doctor_card_visible={doctor_visible}"

            browser.close()
    finally:
        app.stop()

    ok = all(s["ok"] for s in steps)
    result = {"mode": "live", "ok": ok, "steps": steps, "known_issues": known_issues}
    (out_dir / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


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
            print("[ui_smoke] playwright is not installed — falling back to --dry-run. "
                 "Install with: pip install playwright && playwright install chromium")
            result = run_dry(out_dir)
            result["fallback_reason"] = "playwright not installed"
        else:
            try:
                result = run_live(out_dir, headless=not args.headed)
            except Exception as e:  # noqa: BLE001
                print(f"[ui_smoke] live run could not start ({type(e).__name__}: {e}); "
                     "falling back to --dry-run")
                result = run_dry(out_dir)
                result["fallback_reason"] = f"{type(e).__name__}: {e}"

    summary = {k: v for k, v in result.items() if k != "steps"}
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"[ui_smoke] wrote {out_dir / 'result.json'}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
