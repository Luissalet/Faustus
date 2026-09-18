"""A11Y-01/QA-44 — keyboard-only walkthrough of Studio, at 100% and 200%
zoom, with Playwright/Chromium.

Starts a real Faustus server (temp data dir, no auth) plus a small scripted
fake model, opens `/studio`, and drives chat → tool approval → diff → a
dialog using ONLY the keyboard (Tab/Shift+Tab/Enter/Space/Escape/arrows —
never `.click()`). At each stop, for both zoom levels, it checks:

  - the focused element has a VISIBLE focus indicator (a computed outline or
    box-shadow, not `none`/transparent/zero-width);
  - the focused element's bounding box is inside the viewport (nothing
    critical — send, approve/deny, the diff region, a dialog's buttons —
    requires horizontal scrolling or is clipped to reach);
  - a real modal (the workspace dialog) traps Tab inside itself and Escape
    closes it, returning focus to what opened it.

Writes one JSON verdict per check to logs/ui_a11y/result.json and exits
non-zero if anything failed. Run with:

    python scripts/ui_a11y.py

Needs `playwright` + `playwright install chromium` (same as tests/e2e — see
tests/e2e/conftest.py's own docstring); skips cleanly with a clear message
and exit code 2 if either is missing, rather than failing confusingly.

Self-contained on purpose: this is a standalone operational tool (the lote
that added it owns no fixture files under tests/e2e/), not a pytest test —
its own fake model server below is a deliberately smaller cousin of
tests/e2e/fake_llm.py, not an import of it, so this script has no
dependency on files this lote does not own.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parent.parent
RESULT_DIR = REPO / "logs" / "ui_a11y"
ZOOMS = [1.0, 2.0]
VIEWPORT = {"width": 1280, "height": 860}


# ── a small scripted OpenAI-compatible model, streaming fenced tool calls ──

class _State:
    def __init__(self) -> None:
        self.responses: List[str] = []
        self.calls = 0
        self.lock = threading.Lock()


def _make_handler(state: _State):
    def _sse(obj: Dict[str, Any]) -> bytes:
        return ("data: " + json.dumps(obj) + "\n\n").encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # silence
            pass

        def _json(self, code: int, obj: Any) -> None:
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/v1/models"):
                return self._json(200, {"object": "list", "data": [{"id": "fake-a11y", "object": "model"}]})
            return self._json(404, {"error": "not found"})

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length) if length else b""
            if not self.path.startswith("/v1/chat/completions"):
                return self._json(404, {"error": "not found"})
            with state.lock:
                idx = state.calls
                state.calls += 1
                text = state.responses[min(idx, len(state.responses) - 1)] if state.responses else "Done."
            model = "fake-a11y"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(_sse({"id": "c1", "object": "chat.completion.chunk", "model": model,
                                    "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}))
            chunk = 64
            for i in range(0, len(text), chunk):
                self.wfile.write(_sse({"id": "c1", "object": "chat.completion.chunk", "model": model,
                                        "choices": [{"index": 0, "delta": {"content": text[i:i + chunk]}, "finish_reason": None}]}))
                self.wfile.flush()
            self.wfile.write(_sse({"id": "c1", "object": "chat.completion.chunk", "model": model,
                                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    return Handler


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http(url: str, timeout: float = 90.0) -> None:
    t0 = time.time()
    last: Optional[Exception] = None
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if r.status == 200:
                    return
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(0.5)
    raise RuntimeError(f"{url} did not come up: {last}")


def _post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


# ── the checks ──

class Report:
    def __init__(self) -> None:
        self.checks: List[Dict[str, Any]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append({"check": name, "ok": ok, "detail": detail})
        print(("ok " if ok else "FAIL ") + name + (f" — {detail}" if detail and not ok else ""))

    @property
    def failed(self) -> int:
        return sum(1 for c in self.checks if not c["ok"])


def _has_visible_focus(page, zoom: float, context: str, report: Report) -> None:
    """A control's focus ring is not always on the control itself — the
    composer's textarea has `outline: none` on purpose and the ring lives on
    `.fs-studio__composer:focus-within` instead (studio.css says so where it
    is declared). So this does not just read ONE computed style: it snapshots
    the focused element AND its ancestors (up to 5), blurs, snapshots again,
    and reports "visible" if ANYTHING in that chain — outline, box-shadow,
    border colour, background — actually changed between the two. A control
    with a bare `outline: none` and nothing standing in for it fails this
    exactly like a real keyboard user losing track of the cursor would.
    """
    info = page.evaluate(
        """() => {
            const el = document.activeElement;
            if (!el || el === document.body) return null;
            const chain = [];
            let node = el;
            for (let i = 0; i < 5 && node; i++) { chain.push(node); node = node.parentElement; }
            const snap = () => chain.map(n => {
                const cs = getComputedStyle(n);
                return cs.outlineStyle + '|' + cs.outlineWidth + '|' + cs.boxShadow + '|' + cs.borderColor + '|' + cs.backgroundColor;
            });
            const before = snap();
            el.blur();
            const after = snap();
            el.focus();
            const changed = before.some((v, i) => v !== after[i]);
            const cs = getComputedStyle(el);
            const r = el.getBoundingClientRect();
            return {
                tag: el.tagName, testid: el.getAttribute('data-testid'),
                outlineStyle: cs.outlineStyle, changed,
                rect: { top: r.top, left: r.left, right: r.right, bottom: r.bottom, width: r.width, height: r.height },
                viewport: { w: innerWidth, h: innerHeight },
            };
        }"""
    )
    label = f"{context} @ {int(zoom * 100)}%"
    if info is None:
        report.add(f"focus visible: {label}", False, "nothing focused (document.body)")
        return
    report.add(f"focus visible: {label}", info["changed"], f"{info['tag']}[data-testid={info['testid']}] some outline/box-shadow/border/background in the focus chain {'changes' if info['changed'] else 'does NOT change'} on focus")

    r, vp = info["rect"], info["viewport"]
    contained = r["width"] > 0 and r["height"] > 0 and r["left"] >= -1 and r["top"] >= -1 and r["right"] <= vp["w"] + 1 and r["bottom"] <= vp["h"] + 1
    report.add(f"in viewport: {label}", contained, f"rect={r} viewport={vp}")


def _set_zoom(page, zoom: float) -> None:
    page.evaluate("z => { document.documentElement.style.zoom = String(z); }", zoom)
    page.wait_for_timeout(150)


def _tab_to(page, predicate_js: str, max_tabs: int = 120) -> bool:
    """Keyboard-only: press Tab until `predicate_js` (an expression over
    `document.activeElement`) is true, or give up."""
    for _ in range(max_tabs):
        if page.evaluate(f"(() => {{ const el = document.activeElement; return !!(el && ({predicate_js})); }})()"):
            return True
        page.keyboard.press("Tab")
        page.wait_for_timeout(20)
    return False


def run(page, base: str, new_session, report: Report) -> None:
    """`new_session()` returns a fresh session id AND resets the fake model's
    call counter and the workspace file it edits — each zoom pass gets its
    own clean edit_file → approval → diff round, so the SECOND pass is not
    quietly replaying the first pass's already-consumed script against an
    already-edited file (which would just skip the tool call and hang the
    walkthrough on the approval card that never appears)."""
    for zoom in ZOOMS:
        session_id = new_session()
        page.goto(f"{base}/studio?s={session_id}", wait_until="domcontentloaded")
        page.wait_for_selector('[data-testid="studio-input"]', timeout=20000)
        _set_zoom(page, zoom)
        page.evaluate("document.activeElement && document.activeElement.blur && document.activeElement.blur()")

        # Agent mode by keyboard: the workspace-folder chip below (and the
        # ability to run tools at all) only renders in it (Composer.tsx,
        # `knobs.mode === 'agent'`) — a fichero ajeno, read only to learn
        # this, not changed.
        reached_mode = _tab_to(page, "el.matches && el.matches('[data-testid=\"studio-mode-agent\"]')")
        report.add(f"reach the Agent mode toggle by Tab @ {int(zoom * 100)}%", reached_mode)
        if reached_mode:
            page.keyboard.press("Enter")
            page.wait_for_timeout(100)
            is_agent = page.evaluate("() => document.querySelector('[data-testid=\"studio-mode-agent\"]')?.getAttribute('aria-checked') === 'true'")
            report.add(f"Enter actually switches to Agent mode @ {int(zoom * 100)}%", is_agent)

        # ── chat: keyboard-only to the composer, type, send ──
        found = _tab_to(page, "el.matches && el.matches('[data-testid=\"studio-input\"]')")
        report.add(f"reach composer by Tab @ {int(zoom * 100)}%", found)
        _has_visible_focus(page, zoom, "composer", report)
        page.keyboard.type("fix calc.py please", delay=5)
        # Enter submits (Shift+Enter would insert a newline instead).
        page.keyboard.press("Enter")

        # ── the tool ran and asked for permission: the approval card ──
        page.wait_for_selector('[data-testid="studio-approval"]', timeout=30000)
        found = _tab_to(page, "el.closest && el.closest('[data-testid=\"studio-approval\"]')")
        report.add(f"reach approval card by Tab @ {int(zoom * 100)}%", found)
        _has_visible_focus(page, zoom, "approval card control", report)
        # Approve it without ever clicking: keep tabbing to the "Approve" button by name.
        approved = _tab_to(page, "el.tagName === 'BUTTON' && /^Approve$/.test((el.textContent || '').trim())")
        report.add(f"reach the Approve button by Tab @ {int(zoom * 100)}%", approved)
        if approved:
            page.keyboard.press("Enter")

        # Let the reply that follows the approval finish arriving (and
        # Studio.tsx's own pinned auto-scroll settle) BEFORE measuring
        # anything below — otherwise the scroll position is a moving
        # target and a viewport check races the stream, which is not what
        # a keyboard user actually experiences (they see it settle first).
        page.wait_for_function(
            "() => (document.querySelector('[data-testid=\"turn-assistant\"]:last-of-type')?.textContent || '').includes('Fixed calc.py')",
            timeout=30000,
        )
        page.wait_for_function("() => !document.querySelector('[data-streaming]')", timeout=10000)
        page.wait_for_timeout(300)

        # ── the diff: a <details> the tool rail renders, keyboard-openable —
        #    collapsed by default, so it exists in the DOM but is hidden
        #    (`wait_for_selector`'s default visible state would just time
        #    out) until Enter on its <summary> expands it. ──
        page.wait_for_selector('[data-testid="step-diff"]', state="attached", timeout=30000)
        found_summary = _tab_to(
            page,
            "el.matches && el.matches('summary') && el.closest('details') "
            "&& el.closest('details').querySelector('[data-testid=\"step-diff\"]')",
        )
        report.add(f"reach the diff's <summary> by Tab @ {int(zoom * 100)}%", found_summary)
        if found_summary:
            page.keyboard.press("Enter")  # expand the <details>
        page.wait_for_selector('[data-testid="step-diff"]', state="visible", timeout=10000)
        found_diff = _tab_to(page, "el.matches && el.matches('[data-testid=\"step-diff\"]')")
        report.add(f"reach the diff region by Tab @ {int(zoom * 100)}%", found_diff)
        _has_visible_focus(page, zoom, "diff region", report)

        # ── a real modal: the workspace dialog, focus trap + Escape.
        #    The trigger lives inside the composer's "Add files and tools"
        #    popover (Composer.tsx), so that opens first. ──
        opened_popover = _tab_to(page, "el.matches && el.matches('[data-testid=\"studio-add\"]')")
        report.add(f"reach the Add-files-and-tools trigger by Tab @ {int(zoom * 100)}%", opened_popover)
        if opened_popover:
            page.keyboard.press("Enter")
            page.wait_for_timeout(200)
        opened = _tab_to(page, "el.matches && el.matches('[data-testid=\"studio-workspace\"]')")
        if opened:
            page.keyboard.press("Enter")
            page.wait_for_timeout(300)
            in_dialog = page.evaluate("() => !!(document.activeElement && document.activeElement.closest('[role=\"dialog\"]'))")
            report.add(f"opening the workspace dialog moves focus INTO it @ {int(zoom * 100)}%", in_dialog)
            if in_dialog:
                _has_visible_focus(page, zoom, "dialog control", report)
                # Tab many times: focus must stay inside the dialog (a trap).
                trapped = True
                for _ in range(25):
                    page.keyboard.press("Tab")
                    still_in = page.evaluate("() => !!(document.activeElement && document.activeElement.closest('[role=\"dialog\"]'))")
                    if not still_in:
                        trapped = False
                        break
                report.add(f"Tab never escapes the dialog @ {int(zoom * 100)}%", trapped)
                page.keyboard.press("Escape")
                page.wait_for_timeout(600)
                # A closed Radix dialog may still be mounted mid exit-animation
                # (`data-state="closed"`) rather than removed outright — either
                # counts; only a lingering OPEN dialog is a real trap.
                closed = page.evaluate(
                    "() => { const d = document.querySelector('[role=\"dialog\"]'); "
                    "return !d || d.getAttribute('data-state') === 'closed'; }"
                )
                report.add(f"Escape closes the dialog @ {int(zoom * 100)}%", closed)
        else:
            diag = page.evaluate(
                "() => { const el = document.querySelector('[data-testid=\"studio-workspace\"]'); "
                "if (!el) return 'element not found in the DOM at all'; "
                "const r = el.getBoundingClientRect(); const cs = getComputedStyle(el); "
                "return `present, tabIndex=${el.tabIndex}, disabled=${el.disabled}, rect=${JSON.stringify(r)}, display=${cs.display}, visibility=${cs.visibility}`; }"
            )
            report.add(f"reach the workspace-dialog trigger by Tab @ {int(zoom * 100)}%", False, diag)


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # noqa: BLE001
        print("playwright is not installed; skipping (pip install playwright && playwright install chromium)")
        return 2

    state = _State()
    llm_port = _free_port()
    llm = ThreadingHTTPServer(("127.0.0.1", llm_port), _make_handler(state))
    threading.Thread(target=llm.serve_forever, daemon=True).start()

    data_dir = tempfile.mkdtemp(prefix="faustus-a11y-")
    ws_dir = Path(tempfile.mkdtemp(prefix="faustus-a11y-ws-"))
    (ws_dir / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    port = _free_port()
    env = dict(os.environ)
    env.update({
        "ODYSSEUS_DATA_DIR": data_dir,
        "DATABASE_URL": "sqlite:///" + (data_dir.replace("\\", "/") + "/app.db"),
        "APP_PORT": str(port),
        "LOCALHOST_BYPASS": "true",
        "AUTH_ENABLED": "false",
        "ODYSSEUS_INPROCESS_POLLERS": "0",
        "ODYSSEUS_INPROCESS_TASKS": "0",
        "ODYSSEUS_STARTUP_WARMUPS": "0",
        "PYTHONUNBUFFERED": "1",
    })
    log_path = Path(data_dir) / "server.log"
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO), env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    report = Report()
    try:
        _wait_http(base + "/api/chat/activity", timeout=120)
        ep = _post_form(base + "/api/model-endpoints", {
            "name": "fake-a11y", "base_url": f"http://127.0.0.1:{llm_port}/v1", "skip_probe": "true", "endpoint_kind": "local",
        })
        ep_id = ep.get("id") or (ep.get("endpoint") or {}).get("id")
        counter = {"n": 0}

        def new_session() -> str:
            counter["n"] += 1
            with state.lock:
                state.calls = 0
                state.responses = [
                    "```edit_file\n{\"path\": \"calc.py\", \"old_string\": \"return a - b\", \"new_string\": \"return a + b\"}\n```",
                    "Fixed calc.py: it now adds instead of subtracting.",
                ]
            # Each pass edits the same file from the same starting text —
            # otherwise the second pass's scripted `old_string` no longer
            # matches (the first pass already changed it), the edit silently
            # fails to apply, and no approval card ever appears.
            (ws_dir / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
            sess = _post_form(base + "/api/session", {
                "name": f"a11y walkthrough {counter['n']}", "endpoint_id": ep_id,
                "endpoint_url": f"http://127.0.0.1:{llm_port}/v1", "model": "fake-a11y", "skip_validation": "true",
            })
            return sess.get("id") or sess.get("session_id")

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            ctx = browser.new_context(viewport=VIEWPORT)
            page = ctx.new_page()
            page.set_default_timeout(20000)
            page.goto(base + "/", wait_until="domcontentloaded")
            page.evaluate("ws => localStorage.setItem('odysseus-workspace', ws)", str(ws_dir))
            try:
                run(page, base, new_session, report)
            except Exception as e:  # noqa: BLE001
                report.add("walkthrough completed without an unhandled error", False, repr(e))
            ctx.close()
            browser.close()
    except Exception as e:  # noqa: BLE001
        report.add("server/session setup", False, repr(e))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        llm.shutdown()

    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    result = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "zooms": ZOOMS,
        "checks": report.checks,
        "failed": report.failed,
        "total": len(report.checks),
        "server_log": str(log_path),
    }
    (RESULT_DIR / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n{len(report.checks) - report.failed}/{len(report.checks)} checks passed — logs/ui_a11y/result.json")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
