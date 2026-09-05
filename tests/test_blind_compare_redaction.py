"""Regression tests for issue #1285 — blind Compare must not leak model
identities through helper-session names or GET /api/sessions.

Two guards are pinned here:

1. Backend: ``routes.session_routes._public_model`` blanks the ``model`` field
   of any ``[CMP] …`` helper session in the session list, so the sidebar /
   ``/api/sessions`` can't be used to map a neutral pane label ("Model A")
   back to its real model.
2. Naming: every ``[CMP]`` session name is built on the `blind` branch, so a
   blind session is named by its slot ("Model A") rather than by the real
   model.

The backend import mirrors tests/test_session_ghost_delete.py: stub the heavy
ORM modules so the real route module imports under conftest's MagicMock
sqlalchemy stub, then restore sys.modules so the stubs don't leak into sibling
test modules.
"""

import re
import sys
import importlib
from pathlib import Path
from unittest.mock import MagicMock

from tests.helpers.import_state import clear_module, preserve_import_state

_REPO = Path(__file__).resolve().parent.parent

# Stub only the ORM class modules and import the real core.session_manager so
# the cached routes.session_routes is identical regardless of collection order.
# preserve_import_state restores both sys.modules and parent-package attributes
# after the block, preventing stub leakage into siblings.
_TEMP_STUBS = ("core.database", "core.models")
with preserve_import_state(*_TEMP_STUBS, "core.session_manager", "routes.session_routes"):
    for _name in _TEMP_STUBS:
        sys.modules[_name] = MagicMock(name=_name)
    if isinstance(sys.modules.get("core.session_manager"), MagicMock):
        del sys.modules["core.session_manager"]
    clear_module("routes.session_routes")
    importlib.import_module("core.session_manager")
    import routes.session_routes as SR  # noqa: E402


# ── backend: GET /api/sessions model redaction ─────────────────────────────

def test_public_model_blanks_blind_compare_sessions():
    """A blind-compare helper session ("[CMP] Model A") must not expose its
    real model in the session list — that is the de-anonymization vector."""
    assert SR._public_model("[CMP] Model A", "gpt-4o") == ""
    assert SR._public_model("[CMP] Model B", "llama-3.1-70b") == ""


def test_public_model_blanks_any_cmp_prefixed_session():
    """Defense in depth: even a non-blind [CMP] session (named after the real
    model) gets its model field blanked. The name already carries whatever the
    user chose to reveal, and the session list never needs the raw model."""
    assert SR._public_model("[CMP] gpt-4o", "gpt-4o") == ""


def test_public_model_preserves_normal_sessions():
    """Ordinary chats are untouched — only the [CMP] prefix triggers redaction.
    The post-vote "Compare: a vs b" folder is a normal session, not a helper."""
    assert SR._public_model("My research chat", "gpt-4o") == "gpt-4o"
    assert SR._public_model("", "claude-sonnet") == "claude-sonnet"
    assert SR._public_model("Compare: gpt-4o vs llama", "gpt-4o") == "gpt-4o"


def test_compare_prefix_constant_matches_frontend():
    """The redaction prefix must match what the frontend prepends, or the
    guard silently stops matching new sessions."""
    assert SR.COMPARE_SESSION_PREFIX == "[CMP] "


# ── every [CMP] session name is blind-guarded ──────────────────────────────

def test_compare_session_names_are_blind_guarded():
    """A blind comparison must never be named after its real model.

    The name used to be built in the browser, in several places, and one of
    them forgot the blind branch — so the sidebar spelled out which model was
    which while the panels were still hiding it (#1285). It is built in one
    place now, on the server; this pins that the branch is still there and
    that no OTHER line in the compare routes builds one without it.
    """
    routes = _REPO / "routes" / "compare" / "compare_routes.py"
    # Only lines that actually BUILD a name: the prefix inside a string
    # literal. Prose that merely mentions [CMP] is not a name.
    builds = re.compile(r"""['"][^'"]*\[CMP\] """)
    offenders = [
        f"{lineno}: {line.strip()}"
        for lineno, line in enumerate(routes.read_text(encoding="utf-8").splitlines(), 1)
        if builds.search(line) and "blind" not in line
    ]
    assert not offenders, (
        "every line that builds a [CMP] session name must branch on `blind` "
        "(issue #1285):\n" + "\n".join(offenders)
    )
