"""A formatted tool listing replaces the reply only when the model wrote none.

Seen live: a good answer about a free week was followed on screen by
"AI: No events between 2026-09-21 and 2026-09-27." and that raw line was
the only text saved for the turn."""
import src.agent_loop as al


def test_no_events_line_drops_the_ai_prefix():
    raw = "AI: No events between 2026-09-21 and 2026-09-27.\nmore"
    assert al._calendar_list_summary_from_tool_output(raw) == "No events between 2026-09-21 and 2026-09-27."
