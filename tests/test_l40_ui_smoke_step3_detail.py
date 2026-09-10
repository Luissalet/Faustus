"""Lot 40 — `scripts/ui_smoke.py` step 3 used to report the failure string
"the question card never appeared" as `detail` even when the card DID
appear (`q_ok=True`): `detail or "the question card never appeared"`
falls back unconditionally whenever `detail` is still `""`, success
included, since only the `except` branch ever sets it to something else.
A green `result.json` carrying a failure sentence is exactly the kind of
report a person stops trusting (this script's own module docstring: "meant
to be read, not just exit-coded").

A real live run pins the fix better than a source grep does — this repo's
sandbox actually has Chromium available, so `python3 scripts/ui_smoke.py`
was run for real for this lot's report — but that takes ~15s and a browser
this suite cannot assume elsewhere; this fast, deterministic check is what
CI runs. See `docs/spec/v2/EVAL_ESTADO.md` / this lot's final report for the
live `logs/ui_smoke/result.json` this produced (`step 3: ok=true,
detail=""`).
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "ui_smoke.py"


def test_step_3_detail_is_never_the_failure_sentence_on_a_successful_run():
    source = SCRIPT.read_text(encoding="utf-8")
    assert '"detail": detail or "the question card never appeared"' not in source, (
        "the old unconditional fallback is back — it reports a failure "
        "sentence even when q_ok is True"
    )
    assert '"detail": detail or ("" if q_ok else "the question card never appeared")' in source
