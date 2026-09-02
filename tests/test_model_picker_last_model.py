"""A new chat starts on the model the user last picked by hand (per signed-in
user), not on the operator default. Seen live (ronda 6): the default was a
29 GB model that spills out of the 12 GB card while the user had just picked
the 6 GB one; every page reload put him back on the slow model."""
import json
import os
import re
import shutil
import subprocess

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_JS = os.path.join(_ROOT, "static", "js", "modelPicker.js")
_HAS_NODE = shutil.which("node") is not None


def _src():
    with open(_JS, encoding="utf-8") as f:
        return f.read()


def test_manual_pick_is_remembered_and_consulted_before_the_default():
    src = _src()
    assert "rememberLastModel(m);" in src
    # The last-pick lookup sits BEFORE the operator-default cache lookup.
    i_last = src.index("lastModelFor(_currentUserKey())")
    i_default = src.index("odysseus-default-chat-cache', JSON.stringify(dc)")
    i_default_read = src.index("localStorage.getItem('odysseus-default-chat-cache')")
    assert i_last < i_default_read
    # It is only applied when the model still exists on a connected endpoint.
    seg = src[i_last:i_last + 400]
    assert "_modelExists(last.modelId, last.url)" in seg
    assert "source: 'last'" in seg


@pytest.mark.skipif(not _HAS_NODE, reason="node is required")
def test_last_model_is_keyed_by_user():
    src = _src()
    # Extract only the pure helpers (no DOM needed beyond user-bar-name).
    start = src.index("const LAST_MODEL_KEY")
    end = src.index("export function lastModelFor")
    end = src.index("\n}\n", end) + 3
    helpers = src[start:end].replace("export function", "function")
    script = r"""
const store = {};
globalThis.localStorage = { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } };
let user = 'luis';
globalThis.document = { getElementById: () => ({ textContent: user }) };
""" + helpers + r"""
rememberLastModel({ mid: 'qwen3.5:9b', url: 'http://127.0.0.1:11434/v1', endpointId: 'ep1' });
const same = lastModelFor('luis');
const other = lastModelFor('alice');
user = 'User';   // the placeholder before /api/auth/status answers ⇒ no key
rememberLastModel({ mid: 'x', url: 'u' });
const anon = lastModelFor('');
console.log(JSON.stringify({ same, other, anon }));
"""
    proc = subprocess.run(["node", "--input-type=module"], input=script, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["same"]["modelId"] == "qwen3.5:9b" and out["same"]["endpointId"] == "ep1"
    assert out["other"] is None
    assert out["anon"]["modelId"] == "x"
