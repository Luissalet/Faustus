"""`@mentions` must not inline what a symlink points at outside the workspace.

The index comes from os.walk, which reports a symlink-to-a-file as an ordinary
file, and every filter in file_mentions matches on the mention's NAME. So a link
called `notas.md` pointing at ~/.aws/credentials passed every check and its whole
content was pasted into the turn's prompt — and on to the model endpoint, which
may be remote. The agent can create that link itself, and so can a cloned repo.
"""

import os

import pytest

from src import file_mentions


pytestmark = pytest.mark.skipif(
    not hasattr(os, "symlink"), reason="platform has no symlink support")


@pytest.fixture(autouse=True)
def _fresh_index():
    """The workspace index is cached per root; every test builds its own tree."""
    import src.agent_harness as ah
    ah._index_cache.clear()
    yield
    ah._index_cache.clear()


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    (root / "src").mkdir(parents=True)
    (root / "src" / "real.py").write_text("REAL_CONTENT = 1\n", encoding="utf-8")
    return root


def _link(src, dst):
    try:
        os.symlink(src, dst)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlinks here (Windows without privilege?)")


def _block(ws, text):
    res = file_mentions.resolve(str(ws), text)
    return file_mentions.context_text(str(ws), res), res


def test_a_link_escaping_the_workspace_is_named_but_never_inlined(ws, tmp_path):
    outside = tmp_path / "outside_secret.txt"
    outside.write_text("SUPER_SECRET_TOKEN=abc123\n", encoding="utf-8")
    _link(outside, ws / "notas.md")

    block, res = _block(ws, "mira @notas.md por favor")

    assert res["resolved"] == ["notas.md"], res
    # Named: the user did point at it, and the model must not go hunting.
    assert "notas.md" in block
    # But not a byte of what it resolves to.
    assert "SUPER_SECRET_TOKEN" not in block
    assert "abc123" not in block
    assert "resolves outside the workspace" in block


def test_a_link_to_etc_passwd_is_not_inlined(ws):
    if not os.path.isfile("/etc/passwd"):
        pytest.skip("no /etc/passwd on this platform")
    _link("/etc/passwd", ws / "users.md")

    block, res = _block(ws, "revisa @users.md")

    assert res["resolved"] == ["users.md"]
    assert "users.md" in block
    assert "root:" not in block and "/bin/" not in block
    assert "resolves outside the workspace" in block


def test_a_link_to_a_sensitive_file_inside_the_workspace_is_not_inlined(ws):
    """Containment is not enough on its own: the deny-list the file tools use
    (.ssh/, credentials, *.pem…) applies to the RESOLVED path, because the link
    picks its own innocuous name."""
    secrets = ws / ".ssh"
    secrets.mkdir()
    (secrets / "id_rsa").write_text("-----BEGIN PRIVATE KEY-----\nMIIE\n", encoding="utf-8")
    _link(secrets / "id_rsa", ws / "src" / "config.py")

    block, res = _block(ws, "@src/config.py")

    assert res["resolved"] == ["src/config.py"]
    assert "src/config.py" in block
    assert "BEGIN PRIVATE KEY" not in block


def test_a_legitimate_internal_link_still_works(ws):
    """The fix must not break the common case: a link that stays inside the
    workspace is an ordinary file and is inlined as before."""
    _link(ws / "src" / "real.py", ws / "alias.py")

    block, res = _block(ws, "abre @alias.py")

    assert res["resolved"] == ["alias.py"]
    assert "REAL_CONTENT = 1" in block, block


def test_an_ordinary_file_is_still_inlined(ws):
    block, res = _block(ws, "mira @src/real.py")
    assert res["resolved"] == ["src/real.py"]
    assert "REAL_CONTENT = 1" in block


def test_a_relative_link_that_climbs_out_is_caught(ws, tmp_path):
    """`../../secret` inside the workspace resolves outside it — the check has
    to be on the realpath, not on the link's own text."""
    outside = tmp_path / "climbed.txt"
    outside.write_text("CLIMBED_SECRET\n", encoding="utf-8")
    # ws/src/notes.md → ws/src/../../climbed.txt → tmp_path/climbed.txt
    _link(os.path.join("..", "..", "climbed.txt"), ws / "src" / "notes.md")

    block, res = _block(ws, "@src/notes.md")

    assert res["resolved"] == ["src/notes.md"]
    assert "CLIMBED_SECRET" not in block
    assert "resolves outside the workspace" in block


def test_an_escaping_link_does_not_leak_the_target_size(ws, tmp_path):
    """Even the byte count is a probe into a file the model may not read."""
    outside = tmp_path / "big_secret.txt"
    outside.write_text("x" * 4242, encoding="utf-8")
    _link(outside, ws / "size.md")

    block, _ = _block(ws, "@size.md")
    assert "4242" not in block


def test_a_broken_link_is_reported_not_inlined(ws):
    _link(ws / "does_not_exist.py", ws / "dangling.py")
    block, res = _block(ws, "@dangling.py")
    # Resolved (it is in the index) but nothing to inline, and no crash.
    assert "dangling.py" in block
    assert "```" not in block or "resolves outside" in block


def test_containment_helper_agrees_with_the_file_tools(ws, tmp_path):
    """_contained delegates to tool_execution._path_is_within_root so the two
    confinement answers cannot drift apart."""
    from src.tool_execution import _path_is_within_root

    inside = str(ws / "src" / "real.py")
    outside = str(tmp_path / "elsewhere.txt")
    assert file_mentions._contained(os.path.realpath(inside), str(ws)) is True
    assert file_mentions._contained(os.path.realpath(inside), str(ws)) == \
        _path_is_within_root(os.path.realpath(inside), str(ws))
    assert file_mentions._contained(os.path.realpath(outside), str(ws)) is False
