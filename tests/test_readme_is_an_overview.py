"""The README says what the project is and what it does. It is not a log.

Delivery notes, dated verification reports, task numbers and anything about
another repository belong in git history and in the coordination board, not
here. The README is edited in place when a capability changes; nothing is
appended to it per task. FAUSTUS.md holds the detail.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
READMES = ("README.md", "README.es.md")
MAX_BYTES = 30_000

DATE = re.compile(
    r"\b\d{1,2}[-/]\d{1,2}[-/]20\d{2}\b|\b20\d{2}-\d{2}-\d{2}\b"
    r"|\b\d{1,2} (?:de )?(?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December|enero|febrero|marzo|abril|mayo|junio|"
    r"julio|agosto|septiembre|octubre|noviembre|diciembre)(?: de)? 20\d{2}\b",
    re.IGNORECASE,
)
LOG_WORDS = re.compile(
    r"\b(?:delivery|entrega|task|tarea|release|round|ronda)\s+#?\d{2,}\b"
    r"|\bSHA\b|\b[0-9a-f]{40,}\b",
    re.IGNORECASE,
)


@pytest.mark.parametrize("name", READMES)
def test_readme_stays_an_overview(name):
    path = ROOT / name
    raw = path.read_bytes()
    assert len(raw) <= MAX_BYTES, (
        f"{name} is {len(raw)} bytes (limit {MAX_BYTES}). Edit the section the "
        "change belongs to instead of appending; detail goes in FAUSTUS.md."
    )
    text = raw.decode("utf-8")
    lines = text.splitlines()
    assert "</p>" in lines[2], f"{name}: nothing goes inside the opening block"
    for number, line in enumerate(lines, 1):
        assert not DATE.search(line), f"{name}:{number} carries a date: {line[:80]!r}"
        assert not LOG_WORDS.search(line), (
            f"{name}:{number} reads like a delivery note: {line[:80]!r}"
        )
