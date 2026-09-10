"""PERF-02 · A backend that does not block by accident.

`app.py` runs one event loop; a synchronous, slow call made directly inside an
`async def` (not through `asyncio.to_thread`/`run_in_executor`) stalls every
other request on that loop for as long as it takes — the classic way a
FastAPI backend "hangs" without ever raising anything.

This is a static AST scan over `routes/` and `src/`, not a runtime probe: it
walks every `async def` (top-level and nested) and flags a direct call to a
known-blocking primitive — `time.sleep`, `requests.get/post/...`,
`subprocess.run/call/check_output/check_call`, `sqlite3.connect`, or the
builtin `open` — made in that function's own body. A call made inside a
*nested* plain `def` (the "outsource this bit to a thread" helper this repo
uses everywhere, e.g. `vram_admission._get`/`_evict`) is not flagged, because
what matters is only whether the offload actually happens — and a call
wrapped directly in `asyncio.to_thread(...)`/`loop.run_in_executor(...)` is
not flagged either, for the same reason.

The scan is repo-wide (see MAPA_REUTILIZACION.md HW/PERF rows: ~21 files
already use `to_thread` correctly), but this lot only owns a handful of
files — Lote 27 (HW-01/02, PERF-02/04) — so the enforced assertion is scoped
to those; every other finding is reported in ``test_static_scan_lists_every_
blocking_call_repo_wide`` and in the lot's final report (path + line), for
whoever owns that file to fix.
"""
from __future__ import annotations

import ast
import os
from typing import List, Optional, Tuple

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCAN_ROOTS = ("src", "routes")

# Attribute calls (`module.func(...)`) known to block the thread they run on.
BLOCKING_ATTR_CALLS = {
    ("time", "sleep"),
    ("requests", "get"), ("requests", "post"), ("requests", "put"),
    ("requests", "delete"), ("requests", "patch"), ("requests", "head"),
    ("requests", "request"),
    ("subprocess", "run"), ("subprocess", "call"),
    ("subprocess", "check_output"), ("subprocess", "check_call"),
    ("sqlite3", "connect"),
}
# Bare-name calls (builtins) known to block.
BLOCKING_NAME_CALLS = {"open"}
TO_THREAD_NAMES = {"to_thread", "run_in_executor"}

# This lot's own files (Lote 27 — HW-01/02, PERF-02/04): the ones we can
# actually fix. Everything else in the repo-wide scan is somebody else's file.
OWNED_FILES = (
    "src/vram_admission.py",
    "src/vram_fit.py",
    "src/bg_monitor.py",
    "src/model_load_options.py",
    "routes/hwfit_routes.py",
    "routes/local_models_routes.py",
    "services/hwfit/fit.py",
    "services/hwfit/hardware.py",
    "services/hwfit/hf_discovery.py",
    "services/hwfit/image_models.py",
    "services/hwfit/models.py",
    "services/hwfit/profiles.py",
)


class _BlockingCallFinder(ast.NodeVisitor):
    """Flags a blocking-primitive call sitting directly in an async def's own
    body — never inside a nested sync def (offloaded, by construction, once
    something actually calls it via to_thread), and never inside the argument
    list of a to_thread/run_in_executor call itself."""

    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.findings: List[Tuple[str, int, str]] = []
        self._async_stack: List[bool] = []
        self._thread_depth = 0

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._async_stack.append(False)
        self.generic_visit(node)
        self._async_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._async_stack.append(True)
        self.generic_visit(node)
        self._async_stack.pop()

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        is_offload = self._is_to_thread_call(node)
        if is_offload:
            self._thread_depth += 1
        if self._async_stack and self._async_stack[-1] and self._thread_depth == 0:
            name = self._blocking_name(node.func)
            if name:
                self.findings.append((self.filename, node.lineno, name))
        self.generic_visit(node)
        if is_offload:
            self._thread_depth -= 1

    @staticmethod
    def _is_to_thread_call(node: ast.Call) -> bool:
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr in TO_THREAD_NAMES:
            return True
        if isinstance(f, ast.Name) and f.id in TO_THREAD_NAMES:
            return True
        return False

    @staticmethod
    def _blocking_name(func: ast.expr) -> Optional[str]:
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            pair = (func.value.id, func.attr)
            if pair in BLOCKING_ATTR_CALLS:
                return f"{pair[0]}.{pair[1]}"
        if isinstance(func, ast.Name) and func.id in BLOCKING_NAME_CALLS:
            return func.id
        return None


def _iter_py_files(*roots: str):
    for root in roots:
        base = os.path.join(REPO_ROOT, root)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for fn in filenames:
                if fn.endswith(".py"):
                    path = os.path.join(dirpath, fn)
                    yield os.path.relpath(path, REPO_ROOT)


def scan_files(paths) -> List[Tuple[str, int, str]]:
    """Findings across `paths` (repo-relative). A file that fails to parse is
    skipped rather than raising — this is a lint pass, not a compile check."""
    findings: List[Tuple[str, int, str]] = []
    for rel in paths:
        abspath = os.path.join(REPO_ROOT, rel)
        if not os.path.isfile(abspath):
            continue
        try:
            with open(abspath, "r", encoding="utf-8-sig") as f:
                src = f.read()
            tree = ast.parse(src, filename=rel)
        except SyntaxError:
            continue
        finder = _BlockingCallFinder(rel)
        finder.visit(tree)
        findings.extend(finder.findings)
    return findings


def scan_repo() -> List[Tuple[str, int, str]]:
    return scan_files(_iter_py_files(*SCAN_ROOTS))


def test_no_blocking_calls_in_async_defs_of_owned_modules():
    """Lote 27's own files stay free of un-offloaded blocking calls in async
    code. Reverting the vram_admission.py reservation changes and instead
    pasting a bare `time.sleep(1)` inside `admit()` (an `async def`) makes
    this fail with exactly one finding at that line — confirmed by hand
    while preparing this lot, see the report."""
    findings = scan_files(OWNED_FILES)
    assert findings == [], (
        "blocking call(s) found directly inside async def in an owned module: "
        + ", ".join(f"{path}:{line} ({name})" for path, line, name in findings)
    )


def test_static_scan_lists_every_blocking_call_repo_wide():
    """Informational for the rest of the repo (not ours to fix in this lot):
    every direct blocking call inside an async def, anywhere under src/ or
    routes/. Fails only if the *set of files* involved shrinks to zero while
    still reporting hits inside OWNED_FILES (which the assertion above already
    guards precisely) — otherwise this always passes; its purpose is the
    printed listing for whoever owns each file."""
    findings = scan_repo()
    owned_hits = [f for f in findings if f[0] in OWNED_FILES]
    assert owned_hits == [], f"unexpected blocking calls in owned files: {owned_hits}"
    if findings:
        report = "\n".join(f"  {path}:{line}  {name}(...)" for path, line, name in findings)
        print(f"\nPERF-02 static scan — blocking calls in async def outside this lot's files "
              f"({len(findings)} found, not ours to fix here):\n{report}")


if __name__ == "__main__":
    import sys
    hits = scan_repo()
    for h in hits:
        print(h)
    sys.exit(0)
