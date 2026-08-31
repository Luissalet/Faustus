"""The fork's visible name is Faustus; internal identifiers stay Odysseus so
upstream merges remain cheap. scripts/faustus_rename.py re-applies the brand
after a merge and must be idempotent on the current tree."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "faustus_rename.py"


def test_visible_brand_is_faustus():
    index = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert "<title>Faustus Chat</title>" in index
    assert 'placeholder="Message Faustus..."' in index
    login = (ROOT / "static" / "login.html").read_text(encoding="utf-8")
    assert "<title>Faustus — Login</title>" in login
    manifest = json.loads((ROOT / "static" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "Faustus" and manifest["short_name"] == "Faustus"
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert readme.startswith("# Faustus") and "FAUSTUS.md" in readme
    assert (ROOT / "FAUSTUS.md").read_text(encoding="utf-8").startswith("# Faustus")


def test_internal_identifiers_are_untouched():
    mw = (ROOT / "core" / "middleware.py").read_text(encoding="utf-8")
    assert 'INTERNAL_TOOL_HEADER = "X-Odysseus-Internal-Token"' in mw
    storage = (ROOT / "static" / "js" / "storage.js").read_text(encoding="utf-8")
    assert "'odysseus-workspace'" in storage
    consts = (ROOT / "src" / "constants.py").read_text(encoding="utf-8")
    assert "ODYSSEUS_DATA_DIR" in consts


@pytest.mark.skipif(not (ROOT / ".git").exists() and not (ROOT / ".git").is_file(), reason="needs the git checkout")
def test_rename_script_is_idempotent_and_check_passes():
    sys.path.insert(0, str(ROOT / "scripts"))
    import faustus_rename as fr
    left = fr.scan(str(ROOT))
    assert left == {}, left
    proc = subprocess.run([sys.executable, str(SCRIPT), "--check"], cwd=str(ROOT), capture_output=True,
                          text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "brand ok" in proc.stdout


def test_rename_rules_on_a_scratch_tree(tmp_path):
    sys.path.insert(0, str(ROOT / "scripts"))
    import faustus_rename as fr
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "a.py").write_text('NAME = "Odysseus"\nH = "X-Odysseus-Run-Id"\nk = "odysseus-workspace"\nfn = startOdysseusApp\nu = "Ubuntu_Odysseus"\n', encoding="utf-8")
    (tmp_path / "website").mkdir()
    (tmp_path / "website" / "index.md").write_text("Odysseus landing\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("Odysseus upstream readme\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    files, total = fr.rename(str(tmp_path))
    assert (files, total) == (1, 1)
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == 'NAME = "Faustus"\nH = "X-Odysseus-Run-Id"\nk = "odysseus-workspace"\nfn = startOdysseusApp\nu = "Ubuntu_Odysseus"\n'
    assert (tmp_path / "website" / "index.md").read_text(encoding="utf-8") == "Odysseus landing\n"
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "Odysseus upstream readme\n"
    assert fr.scan(str(tmp_path)) == {}
