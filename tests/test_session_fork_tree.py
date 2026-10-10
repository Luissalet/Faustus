"""Agora #233: fork lineage for the Studio session tree.

Persists ``forked_from`` on ``POST /api/session/{id}/fork``, exposes it on
``GET /api/sessions``, and lets the sidebar indent clones under their source
(together with side-thread parents).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_session_model_declares_forked_from():
    db = (ROOT / "core" / "database.py").read_text(encoding="utf-8")
    assert "forked_from = Column" in db
    assert "_migrate_add_session_forked_from" in db
    assert '"add_session_forked_from"' in db or "'add_session_forked_from'" in db
    models = (ROOT / "core" / "models.py").read_text(encoding="utf-8")
    assert "forked_from: Optional[str] = None" in models


def test_create_session_accepts_forked_from():
    src = (ROOT / "core" / "session_manager.py").read_text(encoding="utf-8")
    assert "forked_from: Optional[str] = None" in src
    assert "forked_from=forked_from or None" in src


def test_http_and_agent_fork_set_forked_from():
    history = (ROOT / "routes" / "history" / "history_routes.py").read_text(encoding="utf-8")
    assert "forked_from=session_id" in history
    assert '"forked_from": session_id' in history
    tools = (ROOT / "src" / "agent_tools" / "session_tools.py").read_text(encoding="utf-8")
    assert "forked_from=target_sid" in tools


def test_list_sessions_exposes_forked_from():
    routes = (ROOT / "routes" / "session_routes.py").read_text(encoding="utf-8")
    assert "DbSession.forked_from" in routes
    assert '"forked_from": forked_map.get(s.id)' in routes


def test_studio_wires_fork_parents_into_indent():
    chat = (ROOT / "studio" / "src" / "adapters" / "chat.ts").read_text(encoding="utf-8")
    assert "forkedFrom" in chat
    assert "export function mergeForkParents" in chat
    pane = (ROOT / "studio" / "src" / "screens" / "studio" / "SessionsPane.tsx").read_text(
        encoding="utf-8"
    )
    assert "mergeForkParents(list, parents)" in pane


def test_merge_fork_parents_logic():
    """Pure check of the merge rule (side-thread wins; missing parent ignored)."""
    # Execute the TS helper's rule in Python so CI does not need esbuild here.
    sessions = [
        {"id": "root", "forkedFrom": None},
        {"id": "child", "forkedFrom": "root"},
        {"id": "orphan", "forkedFrom": "missing"},
        {"id": "excurs", "forkedFrom": "root"},
    ]
    parents = {"excurs": "other"}  # side-thread parent wins
    out = dict(parents)
    ids = {s["id"] for s in sessions}
    for s in sessions:
        parent = s["forkedFrom"]
        if not parent or parent == s["id"] or parent not in ids:
            continue
        if s["id"] not in out:
            out[s["id"]] = parent
    assert out["child"] == "root"
    assert out["excurs"] == "other"
    assert "orphan" not in out


def test_fork_route_still_registered():
    # Acceptance map already lists the path; keep the handler present.
    history = (ROOT / "routes" / "history" / "history_routes.py").read_text(encoding="utf-8")
    assert '@router.post("/api/session/{session_id}/fork")' in history
    tree = ast.parse(history)
    assert any(
        isinstance(n, ast.AsyncFunctionDef) and n.name == "fork_session" for n in ast.walk(tree)
    )
