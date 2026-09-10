"""Lote 49b (P1) - A11Y-02: the grouped stream announcement is actually wired
into the transcript, not just implemented as an unused pure function.

Static source check (same approach as tests/qa/test_qa_32_preview_malicioso.py):
reads the real deployed Transcript.tsx, so removing the wiring fails this.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TRANSCRIPT = (REPO_ROOT / "studio" / "src" / "screens" / "studio" / "Transcript.tsx").read_text(encoding="utf-8")


def test_grouped_announcement_is_imported_from_the_pure_module():
    assert "nextStreamAnnouncement" in TRANSCRIPT
    assert "from '../../adapters/streamAnnounce'" in TRANSCRIPT


def test_the_live_region_is_polite_and_only_active_while_streaming():
    assert 'aria-live="polite"' in TRANSCRIPT
    assert "useGroupedStreamAnnouncement" in TRANSCRIPT
    # It only renders while the turn is streaming — a settled turn has
    # nothing left to announce, so the region is gone rather than sitting
    # around empty (which would still cost nothing, but the intent is
    # explicit here).
    assert "turn.streaming && <span className=\"fs-sr-only\"" in TRANSCRIPT


def test_the_visible_bubble_is_never_itself_an_aria_live_region():
    """The requirement is explicit: grouped announcements, not the raw
    growing text repainted into a live region (which would re-read
    everything, not just what changed)."""
    assert 'aria-live="polite"><Rich' not in TRANSCRIPT
