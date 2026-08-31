"""The "@path" chips in a sent message.

They must agree with src/file_mentions.py about what a mention is: a chip that
appears where the server sees no mention (or the reverse) makes the picker look
unreliable exactly where it is trying to build trust.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from src import file_mentions as fm

_REPO = Path(__file__).resolve().parents[1]
_MODULE = (_REPO / "static" / "js" / "mentionChips.js").as_uri()

CASES = [
    'arregla @src/a.py y @"my dir/b.js", gracias.',
    "escribe a me@host.com y nada mas",
    "mira @src/a.py.",
    "sin menciones aqui",
    "@a.py @a.py otra vez",
    "correo user@example.org y @routes/x.py",
]


def _node(script):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    res = subprocess.run(["node", "--input-type=module"], input=script, capture_output=True,
                         text=True, encoding="utf-8", cwd=_REPO, timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


def test_the_chip_regex_finds_exactly_what_the_server_extracts():
    out = _node(f"""
      import {{ splitMentions }} from {json.dumps(_MODULE)};
      const cases = {json.dumps(CASES)};
      console.log(JSON.stringify(cases.map(c =>
        splitMentions(c).filter(p => p.mention).map(p => p.mention))));
    """)
    for case, js_paths in zip(CASES, out):
        # extract() de-duplicates; the chips do not (each occurrence is clickable).
        assert list(dict.fromkeys(js_paths)) == fm.extract(case), case


def test_plain_text_is_preserved_around_the_chips():
    out = _node(f"""
      import {{ splitMentions }} from {json.dumps(_MODULE)};
      const rebuilt = (s) => splitMentions(s).map(p => p.text != null ? p.text : p.token).join('');
      console.log(JSON.stringify({json.dumps(CASES)}.map(rebuilt)));
    """)
    assert out == CASES          # decoration must never alter the message


def test_trailing_punctuation_is_not_part_of_the_path():
    out = _node(f"""
      import {{ splitMentions }} from {json.dumps(_MODULE)};
      console.log(JSON.stringify(splitMentions('mira @src/a.py.')));
    """)
    assert out[1] == {"mention": "src/a.py", "token": "@src/a.py"}
    assert out[2] == {"text": "."}


def test_chips_never_decorate_code_or_links():
    src = (_REPO / "static" / "js" / "mentionChips.js").read_text(encoding="utf-8", errors="replace")
    for sel in ("code", "pre", "a"):
        assert re.search(rf"\b{sel}\b", src.split("SKIP_ANCESTORS =")[1].split("\n")[0])
    # Only the user's own messages carry chips — an answer quoting "@x.py" is
    # the model's text, not a path the user pointed at.
    assert ".msg-user .body" in src


def test_the_composer_wires_the_chips():
    src = (_REPO / "static" / "js" / "chat.js").read_text(encoding="utf-8", errors="replace")
    assert "./mentionChips.js" in src and "initMentionChips()" in src
    css = (_REPO / "static" / "style.css").read_text(encoding="utf-8", errors="replace")
    assert ".mention-chip" in css
