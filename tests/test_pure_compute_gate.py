"""Pure-computation python runs without the external-context approval card
(live, exam runs 13-16: a Caesar-shift snippet after reading the puzzle's
files stopped the turn on a card, and the resumed turn started over).
Anything that can act outside its process still asks."""
import json

import pytest

from src.pure_compute import is_pure_compute
from src.tool_capabilities import ToolRunSecurityContext


@pytest.fixture(autouse=True)
def ask_mode(monkeypatch):
    import src.tool_capabilities as caps
    monkeypatch.setattr(caps, "tool_approval_mode", lambda: "ask")


def _armed() -> ToolRunSecurityContext:
    ctx = ToolRunSecurityContext()
    ctx.external_untrusted_context_seen = True
    ctx.external_sources.append("read_file")
    return ctx


CAESAR = (
    "def shift(word, n):\n    out = []\n    for c in word:\n        if c.isalpha():\n"
    "            base = ord('A') if c.isupper() else ord('a')\n"
    "            out.append(chr((ord(c) - base + n) % 26 + base))\n        else:\n"
    "            out.append(c)\n    return ''.join(out)\n\n"
    "import math, itertools\nfrom collections import Counter\n"
    "print(shift('Hamlet', -4), math.radians(3839 * 185.2 / 1000), Counter('abca'))\n"
)


@pytest.mark.parametrize("code", [
    CAESAR,
    json.dumps({"code": CAESAR}),
    "km = 3839 * 0.1852\nprint(f'{km:.4f} km')",
    "import re\nprint(re.findall(r'\\\\d+', 'a1b22'))",
])
def test_pure_computation_is_recognised_and_runs_without_a_card(code):
    assert is_pure_compute(code)
    assert _armed().decision_for("python", code).allowed


@pytest.mark.parametrize("code", [
    "open('RESPUESTA.md', 'w').write('x')",
    "import os\nos.remove('a.txt')",
    "import subprocess\nsubprocess.run(['ls'])",
    "import urllib.request\nurllib.request.urlopen('http://x')",
    "from PIL import Image\nImage.open('a.jpg').save('b.png')",
    "__import__('os').system('dir')",
    "eval('1+1')",
    "().__class__.__bases__[0].__subclasses__()",
    "getattr(__builtins__, 'open')('x')",
    "from . import secrets",
    "import pathlib\npathlib.Path('x').write_text('y')",
    "this is not python (",
    "",
])
def test_anything_that_can_act_is_not_pure_and_still_asks(code):
    assert not is_pure_compute(code)
    assert not _armed().decision_for("python", code).allowed


def test_only_the_python_tool_gets_the_exemption():
    assert not _armed().decision_for("bash", "echo hi").allowed
