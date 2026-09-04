"""The workspace picker's security sentence (static/js/workspace_note.js).

The picker used to tell everyone, flatly, that shell commands "are not
sandboxed and can reach outside" the folder. Once `agent_sandbox_execution`
landed that became false — and a security note that is wrong in the *safe*
direction is still wrong: it is the line someone reads before deciding what to
let the agent run.

Three states, and the middle one is the one worth pinning. On, with the
backend missing, must NOT read as "unsandboxed": Faustus refuses the command
rather than running it on the host, and a user who reads "not available" would
otherwise assume the old behaviour is the fallback.

Driven through `node --input-type=module`, like the other JS helper tests.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_HELPER = _REPO / "static" / "js" / "workspace_note.js"
_HAS_NODE = shutil.which("node") is not None

pytestmark = pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")


def note(state, html=True):
    js = (
        f"import {{ shellNote }} from '{_HELPER.as_uri()}';"
        f"console.log(JSON.stringify(shellNote({json.dumps(state)},"
        f" {{ html: {json.dumps(html)} }})));"
    )
    proc = subprocess.run(["node", "--input-type=module"], input=js,
                          capture_output=True, text=True, encoding="utf-8",
                          cwd=str(_REPO), timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())


def test_with_the_sandbox_off_it_says_so_and_keeps_the_caveat():
    text = note({"enabled": False, "ready": False, "image": "python:3.12-slim"})
    assert "not sandboxed" in text
    assert "can reach outside it" in text
    assert "not a security boundary" in text


def test_an_unknown_state_takes_the_cautious_branch():
    """Null is "not fetched yet, or the request failed". While we do not know,
    we do not reassure."""
    assert note(None) == note({"enabled": False})
    assert "not sandboxed" in note(None)


def test_with_the_sandbox_on_it_names_the_image_and_the_network():
    text = note({"enabled": True, "ready": True, "image": "python:3.12-slim",
                 "network": False})
    assert "in a container" in text
    assert "python:3.12-slim" in text
    assert "only this folder" in text
    assert "no network" in text
    assert "not sandboxed" not in text

    open_net = note({"enabled": True, "ready": True, "image": "x:1", "network": True})
    assert "the network open" in open_net


def test_on_but_unavailable_says_refused_and_never_unsandboxed():
    """The state that would be easiest to word badly: the setting is on and
    Docker is down. The command is refused, not run on the host, and the note
    must not let anyone infer the opposite."""
    text = note({"enabled": True, "ready": False,
                 "detail": "backend_unavailable: the daemon did not answer"})
    assert "refused" in text
    assert "the daemon did not answer" in text
    assert "not sandboxed" not in text
    assert "can reach outside" not in text


def test_a_missing_reason_still_reads_as_a_sentence():
    text = note({"enabled": True, "ready": False})
    assert "reason unknown" in text
    assert "refused" in text


def test_the_plain_text_form_carries_no_markup():
    for state in (None, {"enabled": False}, {"enabled": True, "ready": True,
                                             "image": "i", "network": False},
                  {"enabled": True, "ready": False, "detail": "d"}):
        text = note(state, html=False)
        assert "<strong>" not in text and "</strong>" not in text
        assert "<" not in text
