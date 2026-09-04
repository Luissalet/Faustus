"""The "where did this run" badge (static/js/tool_run_badge.js).

`sandbox_exec` had been putting `sandboxed`, `image`, `isolation` and
`duration_ms` in every result dict and nothing painted them, so the one place
a person looks after running a command could not say whether it had gone into
a container or straight onto their machine.

The rule worth pinning is when the badge says *nothing*. A missing `sandboxed`
key means either an event recorded before this existed or a tool that never
goes near the sandbox — neither of which is "it ran on the host". Labelling
those would be inventing a fact about someone's history.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_HELPER = _REPO / "static" / "js" / "tool_run_badge.js"
_HAS_NODE = shutil.which("node") is not None

pytestmark = pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")


def badge(ev, html=True):
    js = (
        f"import {{ runBadge }} from '{_HELPER.as_uri()}';"
        f"console.log(JSON.stringify(runBadge({json.dumps(ev)},"
        f" {{ html: {json.dumps(html)} }})));"
    )
    proc = subprocess.run(["node", "--input-type=module"], input=js,
                          capture_output=True, text=True, encoding="utf-8",
                          cwd=str(_REPO), timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())


def test_an_event_that_cannot_say_where_it_ran_says_nothing():
    assert badge({"tool": "bash", "output": "hi", "exit_code": 0}) == ""
    assert badge({"tool": "read_file"}) == ""
    assert badge({}) == "" and badge(None) == ""
    # Explicitly false without a refusal is still not a claim we can make.
    assert badge({"sandboxed": False}) == ""


def test_a_sandboxed_run_names_the_image_and_the_network():
    text = badge({"sandboxed": True, "isolation": "container",
                  "image": "faustus-sandbox:1", "network": False,
                  "duration_ms": 1234}, html=False)
    assert text == "container · faustus-sandbox:1 · no network · 1.2s"


def test_an_open_network_is_said_out_loud():
    text = badge({"sandboxed": True, "isolation": "container",
                  "image": "i:1", "network": True}, html=False)
    assert "network" in text and "no network" not in text


def test_a_refused_run_says_it_did_not_run():
    text = badge({"sandbox_refused": True, "sandboxed": False}, html=False)
    assert text == "refused · not run"
    assert "is-refused" in badge({"sandbox_refused": True})


def test_the_markup_escapes_what_came_from_the_result():
    html = badge({"sandboxed": True, "isolation": "container",
                  "image": '<img src=x onerror="alert(1)">', "network": False})
    assert "<img" not in html
    assert "&lt;img" in html
    assert html.startswith('<span class="agent-thread-where')


def test_a_missing_duration_is_left_out_rather_than_guessed():
    text = badge({"sandboxed": True, "isolation": "container", "image": "i:1",
                  "network": False}, html=False)
    assert text == "container · i:1 · no network"
    assert "NaN" not in text
