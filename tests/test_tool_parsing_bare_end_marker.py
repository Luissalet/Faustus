import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import src.agent_tools  # noqa: F401  (break agent_tools<->tool_parsing import cycle)
from src.tool_parsing import strip_tool_blocks

_REPO = Path(__file__).resolve().parent.parent
KEPT = [
    ("loop do\n    puts \"yo\"\nend\n", "\nend"),          # the reported Ruby case
    ("if x then\nend", "\nend"),
    ("function f()\nend\n", "\nend"),
    ("a end b", "a end b"),
    ("append end", "append end"),
    ("END", "END"),
    ("\nEnd\n", "End"),
    ("x assistant y", "x assistant y"),          # mid-sentence must survive (#5971)
]

# Real markers — at least one pipe, plus the role word — with the exact output
# they must still produce. Asserted as equality rather than "marker not in out"
# so narrowing the pattern can't pass by deleting more than it should.
STRIPPED = [
    ("a |end| b", "a  b"),
    ("a /|end| b", "a  b"),
    ("a |end b", "a  b"),
    ("a end| b", "a  b"),
    ("Before\nassistant\nAfter", "Before \nAfter"),   # bare-marker on its own line still stripped
    ("Before\n  assistant\t \nAfter", "Before \nAfter"),     # whitespace-padded marker still stripped
    ("Before\n\tassistan  \nAfter", "Before \nAfter"),       # truncated marker variant still stripped
]


@pytest.mark.parametrize("text,kept", KEPT)
def test_bare_end_survives_stripping(text, kept):
    assert kept in strip_tool_blocks(text)


@pytest.mark.parametrize("text,expected", STRIPPED)
def test_piped_end_markers_are_still_stripped(text, expected):
    assert strip_tool_blocks(text) == expected


def test_bare_end_inside_a_fenced_block_survives():
    """The scrub runs over the whole message, fenced regions included."""
    out = strip_tool_blocks("Here:\n```ruby\nloop do\n  puts 1\nend\n```\nDone.")
    assert "\nend\n" in out
