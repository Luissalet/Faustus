"""@file mentions: ranking, parsing, resolution and the injected context block.

The feature exists to stop a small local model from editing a file the user did
not name, so the tests care most about the paths where guessing would be wrong:
an ambiguous basename, a mention that does not exist, and a path token that
must not be mistaken for prose.
"""
import os

import pytest

from src import file_mentions as fm


@pytest.fixture()
def ws(tmp_path):
    for rel in [
        "src/agent_loop.py",
        "src/repo_map.py",
        "src/util/__init__.py",
        "routes/workspace_routes.py",
        "static/js/chat.js",
        "static/js/chatRenderer.js",
        "tests/test_agent_loop.py",
        "node_modules/left-pad/index.js",
        "pkg/__init__.py",
        "README.md",
    ]:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# {rel}\n", encoding="utf-8")
    return str(tmp_path)


# ── ranking ───────────────────────────────────────────────────────────────

def test_exact_basename_outranks_a_longer_prefix_match(ws):
    rows = fm.search(ws, "chat.js", limit=5)
    assert rows[0]["rel"] == "static/js/chat.js"


def test_fuzzy_subsequence_finds_the_file_from_initials(ws):
    rows = fm.search(ws, "wsrt", limit=5)
    assert rows[0]["rel"] == "routes/workspace_routes.py"


def test_vendored_directories_are_not_offered_at_all(ws):
    # The picker reads the shared workspace index, which already skips
    # node_modules/dist/venv — so "@index.js" cannot hand the model a
    # dependency's file by accident.
    rels = [r["rel"] for r in fm.search(ws, "index.js", limit=5)]
    assert "node_modules/left-pad/index.js" not in rels


def test_a_vendor_folder_the_index_keeps_still_ranks_last(ws):
    import pathlib
    for rel in ("vendor/legacy/util.py", "src/util.py"):
        p = pathlib.Path(ws, rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n", encoding="utf-8")
    fm._index.cache_clear() if hasattr(fm._index, "cache_clear") else None
    from src import agent_harness
    agent_harness._index_cache.clear()
    rels = [r["rel"] for r in fm.search(ws, "util.py", limit=5)]
    assert rels[0] == "src/util.py"
    assert rels.index("vendor/legacy/util.py") > 0


def test_empty_query_offers_source_files_not_noise(ws):
    rels = [r["rel"] for r in fm.search(ws, "", limit=4)]
    assert any(r.startswith("src/") or r.startswith("routes/") for r in rels)
    assert "node_modules/left-pad/index.js" not in rels


def test_search_without_a_workspace_is_empty():
    assert fm.search("", "chat") == []


# ── parsing ───────────────────────────────────────────────────────────────

def test_extract_reads_plain_and_quoted_mentions_and_skips_emails():
    got = fm.extract('fix @src/a.py and @"my dir/b.js", mail me at me@host.com.')
    assert got == ["src/a.py", "my dir/b.js"]


def test_extract_drops_trailing_sentence_punctuation():
    assert fm.extract("look at @src/a.py.") == ["src/a.py"]


def test_extract_is_deduplicated_and_ordered():
    assert fm.extract("@b.py then @a.py then @b.py") == ["b.py", "a.py"]


# ── resolution ────────────────────────────────────────────────────────────

def test_relative_path_and_unique_basename_both_resolve(ws):
    res = fm.resolve(ws, "compare @src/repo_map.py with @chatRenderer.js")
    assert res["resolved"] == ["src/repo_map.py", "static/js/chatRenderer.js"]
    assert res["missing"] == [] and res["ambiguous"] == []


def test_an_ambiguous_basename_is_reported_not_guessed(ws):
    res = fm.resolve(ws, "open @__init__.py")
    assert res["ambiguous"] == ["__init__.py"]
    assert res["resolved"] == []


def test_a_mention_that_does_not_exist_is_reported_missing(ws):
    res = fm.resolve(ws, "fix @static/js/cards.js please")
    assert res["missing"] == ["static/js/cards.js"]


def test_backslashes_and_dot_slash_normalise(ws):
    res = fm.resolve(ws, r"see @src\repo_map.py and @./README.md")
    assert res["resolved"] == ["src/repo_map.py", "README.md"]


def test_resolution_is_case_insensitive(ws):
    assert fm.resolve(ws, "@SRC/Repo_Map.py")["resolved"] == ["src/repo_map.py"]


# ── the injected block ────────────────────────────────────────────────────

def test_context_inlines_small_files_and_names_the_path(ws):
    res = fm.resolve(ws, "@src/repo_map.py")
    text = fm.context_text(ws, res, inline_chars=4000)
    assert "src/repo_map.py" in text
    assert "# src/repo_map.py" in text            # the file body rode along


def test_context_never_truncates_a_file_into_a_useless_head(ws):
    big = os.path.join(ws, "src", "big.py")
    with open(big, "w", encoding="utf-8") as fh:
        fh.write("x = 1\n" * 4000)
    res = {"resolved": ["src/big.py"], "missing": [], "ambiguous": []}
    text = fm.context_text(ws, res, inline_chars=200)
    assert "too large to inline" in text
    assert "```" not in text


def test_context_tells_the_model_to_say_a_missing_file_is_missing(ws):
    text = fm.context_text(ws, {"resolved": [], "missing": ["cards.js"], "ambiguous": []})
    assert "cards.js" in text
    assert "NOT present" in text


def test_context_is_empty_when_nothing_was_mentioned(ws):
    assert fm.context_text(ws, {"resolved": [], "missing": [], "ambiguous": []}) == ""


def test_turn_context_returns_nothing_without_a_workspace():
    text, res = fm.turn_context("", "@src/a.py")
    assert text == "" and res["resolved"] == []


def test_strip_markers_leaves_a_readable_sentence():
    assert fm.strip_markers("fix @src/a.py now") == "fix src/a.py now"


# ── dotfiles and secrets ──────────────────────────────────────────────────

def test_a_dotfile_resolves_instead_of_losing_its_dot(tmp_path):
    """lstrip("./") strips a character set, so ".env" became "env" and was
    reported as a file that does not exist."""
    (tmp_path / ".env").write_text("KEY=1\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    from src import agent_harness
    agent_harness._index_cache.clear()
    res = fm.resolve(str(tmp_path), "mira @.env y @.gitignore y @./app.py")
    assert res["resolved"] == [".env", ".gitignore", "app.py"]
    assert res["missing"] == []


def test_secret_looking_files_are_named_but_never_inlined(tmp_path):
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-verysecret\n", encoding="utf-8")
    (tmp_path / "certs").mkdir()
    (tmp_path / "certs" / "server.pem").write_text("-----BEGIN PRIVATE KEY-----\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    from src import agent_harness
    agent_harness._index_cache.clear()
    res = fm.resolve(str(tmp_path), "@.env @certs/server.pem @app.py")
    text = fm.context_text(str(tmp_path), res, inline_chars=8000)
    # The paths are there — the model still knows which files are meant.
    assert ".env" in text and "certs/server.pem" in text
    # Their contents are not.
    assert "sk-verysecret" not in text
    assert "BEGIN PRIVATE KEY" not in text
    assert "read_file it if you need them" in text
    # An ordinary file in the same turn still rides along.
    assert "x = 1" in text


def test_the_secret_pattern_covers_the_usual_suspects():
    for rel in (".env", ".env.local", "app/.env", ".netrc", ".npmrc",
                "certs/server.pem", "keys/id_rsa", "a/b/private.key",
                "secrets.yaml", "config/secrets.json", "store.p12", "creds/credentials"):
        assert fm._SECRET_NAME_RE.search(rel), rel
    for rel in ("src/environment.py", "docs/keyboard.md", "pemberton.txt",
                "src/keys.py", "credentials_helper.go"):
        assert not fm._SECRET_NAME_RE.search(rel), rel
