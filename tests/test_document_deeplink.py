"""Regression guards for in-chat document deep-links (#document-<id>).

The frontend module is browser-coupled (window/fetch/document) so there's
no JS unit harness for it — these pin the source-level invariants that the
404-silent-failure fix depends on. See issue #560.
"""

from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]


def test_chat_document_links_use_the_document_id():
    """The list/open tool must anchor to the real document id, not a slug —
    a slug 404s against the UUID-keyed /api/document/<id> route."""
    src = (_REPO / "src" / "agent_tools" /"document_tools.py").read_text(encoding="utf-8")
    assert "(#document-{d.id})" in src
    assert "(#document-{doc.id})" in src


