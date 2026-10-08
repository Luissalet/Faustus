# -*- coding: utf-8 -*-
"""Pins the agent-loop hook of OBJ-47."""
from pathlib import Path


def test_the_agent_loop_calls_the_closure_after_the_review():
    """The hook is a few lines in a very large file; pin that it is there and
    where: after the diff review, before the advisor's last word, and wired to
    the `harness_check` event the Studio card reads."""
    src = (Path(__file__).resolve().parent.parent / "src" / "agent_loop.py").read_text(encoding="utf-8")
    review = src.index("review_defects:")
    hook = src.index("design_canvas_check as _dcc")
    advisor = src.index("Advisor, third trigger")
    assert review < hook < advisor
    assert '"canvas_" + str(_cvc.get("overall")' in src
    # ...and it survives a reload: the verdict is part of what is persisted with the message.
    persisted = src[src.index('metrics["harness"] = {'):]
    persisted = persisted[:persisted.index(")\n        }")]
    assert '"canvas_check"' in persisted
