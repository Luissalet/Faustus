"""The composer's "#" shortcut: which lines become a remembered project rule.

Getting this predicate wrong is user-visible in the worst way — a Markdown
heading someone meant to send would silently be written into AGENTS.md instead.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_MODULE = (_REPO / "static" / "js" / "composerSigils.js").as_uri()


def _node(script):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    res = subprocess.run(["node", "--input-type=module"], input=script, capture_output=True,
                         text=True, encoding="utf-8", cwd=_REPO, timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


def test_hash_lines_are_memory_lines_and_headings_are_not():
    out = _node(f"""
      import {{ isMemoryLine }} from {json.dumps(_MODULE)};
      const cases = ['# use pnpm', '#use pnpm', '  # spaced', '## Heading',
                     '### Deep', '#', '#   ', 'plain text', 'line\\n# x', ''];
      console.log(JSON.stringify(cases.map(c => [c, isMemoryLine(c)])));
    """)
    got = dict((k, v) for k, v in out)
    assert got["# use pnpm"] and got["#use pnpm"] and got["  # spaced"]
    assert not got["## Heading"] and not got["### Deep"]
    assert not got["#"] and not got["#   "]
    assert not got["plain text"] and not got["line\n# x"] and not got[""]


def test_the_dispatcher_re_exports_the_same_predicate():
    src = (_REPO / "static" / "js" / "slashCommands.js").read_text(encoding="utf-8", errors="replace")
    assert "import { isMemoryLine } from './composerSigils.js';" in src
    assert "export { isMemoryLine };" in src
    # isCommand must claim "#" lines, or the dispatcher never sees them.
    assert "if (isMemoryLine(str) && _boundWorkspacePath()) return true;" in src


def test_the_composer_wires_the_at_mention_picker():
    src = (_REPO / "static" / "js" / "chat.js").read_text(encoding="utf-8", errors="replace")
    assert "./fileMentions.js" in src and "initFileMentions" in src


def test_the_versions_command_is_registered_with_a_restore_action():
    src = (_REPO / "static" / "js" / "slashCommands.js").read_text(encoding="utf-8", errors="replace")
    assert "  versions: {" in src and "handler: _cmdVersions," in src
    # The list rows and the delegated click handler have to agree on the
    # data attribute, or Restore silently does nothing.
    assert 'data-cv-restore="${payload}"' in src
    assert "closest('[data-cv-restore]')" in src
    assert "/versions/${encodeURIComponent(p.id)}/restore" in src
