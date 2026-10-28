"""Engine-aware skill selection and the pinned multi-skill GitHub import.

* `src/skills_runtime/engine_context.py` detects a Unity / Godot / Unreal
  project in the workspace from directory and file names only.
* The hybrid selector prefers skills tagged for that engine and ranks the
  other engines' skills down; engine-neutral skills and workspaces without an
  engine are untouched.
* `SkillsManager.import_bundle_from_files` merges tags and can publish, and
  the importer sends a GitHub token to the API host only.
* `scripts/import_github_skills.py` refuses anything but a full commit SHA.
"""
from __future__ import annotations

import importlib.util
import ipaddress
from pathlib import Path

import pytest

from src.skills_runtime import engine_context as ec
from src.skills_runtime import selector

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    ec.reset_cache()
    selector.reset_selector_state()
    monkeypatch.setattr(selector, "_get_embedder", lambda: None)
    monkeypatch.setattr(selector, "_get_mode", lambda: "hybrid")
    yield
    ec.reset_cache()


def _unity(root: Path) -> Path:
    (root / "Assets").mkdir(parents=True)
    (root / "ProjectSettings").mkdir()
    return root


def _godot(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "project.godot").write_text("config_version=5\n", encoding="utf-8")
    return root


# ── detection ───────────────────────────────────────────────────────────────

def test_detects_each_engine_at_the_workspace_root(tmp_path):
    assert ec.detect_engines(str(_unity(tmp_path / "u"))) == ["unity"]
    assert ec.detect_engines(str(_godot(tmp_path / "g"))) == ["godot"]
    unreal = tmp_path / "r"
    unreal.mkdir()
    (unreal / "Game.uproject").write_text("{}", encoding="utf-8")
    assert ec.detect_engines(str(unreal)) == ["unreal"]


def test_detects_a_nested_project_and_several_engines(tmp_path):
    _unity(tmp_path / "games" / "a" / "arrows")
    _godot(tmp_path / "games" / "b")
    assert sorted(ec.detect_engines(str(tmp_path))) == ["godot", "unity"]


def test_no_engine_for_a_plain_or_missing_workspace(tmp_path):
    (tmp_path / "src").mkdir()
    assert ec.detect_engines(str(tmp_path)) == []
    assert ec.detect_engines(str(tmp_path / "missing")) == []
    assert ec.detect_engines(None) == []
    assert ec.detect_engines("") == []


def test_does_not_walk_into_engine_cache_folders(tmp_path):
    _godot(tmp_path / ".godot" / "inner")
    _unity(tmp_path / "Library" / "inner")
    assert ec.detect_engines(str(tmp_path)) == []


def test_detection_is_cached_briefly(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    assert ec.detect_engines(str(proj)) == []
    _godot(proj)  # the folder becomes a project; the cached answer still holds
    assert ec.detect_engines(str(proj)) == []
    assert ec.detect_engines(str(proj), use_cache=False) == ["godot"]


def test_engine_factor_rules():
    unity = {"tags": ["Unity"]}
    godot = {"tags": ["godot", "physics"]}
    neutral = {"tags": ["physics"]}
    assert ec.engine_factor(unity, ["unity"]) == ec.MATCH_BOOST
    assert ec.engine_factor(godot, ["unity"]) == ec.MISMATCH_FACTOR
    assert ec.engine_factor(neutral, ["unity"]) == 1.0
    assert ec.engine_factor(unity, []) == 1.0  # no engine detected: no effect
    assert ec.engine_factor(unity, ["unity", "godot"]) == ec.MATCH_BOOST
    assert ec.engine_factor(godot, ["unity", "godot"]) == ec.MATCH_BOOST


# ── selector ────────────────────────────────────────────────────────────────

class _SM:
    def _filter_candidates(self, skills, min_confidence):
        return list(skills)

    def _load_usage(self):
        return {}

    def _usage_entry(self, usage, name, owner):
        return {}


def _skill(name, tags, text):
    return {"id": name, "name": name, "description": text, "when_to_use": "",
            "tags": tags, "status": "published", "confidence": 0.9, "extra": {}}


PHYSICS = "Make the character body collide and move with physics and collision layers"
LIB = [
    _skill("unity-physics", ["unity"], PHYSICS),
    _skill("godot-physics", ["godot"], PHYSICS),
    _skill("camera-systems", [], "Camera follow and collision for a game camera"),
]
QUERY = "make the character collide with physics layers"


def _names(engines):
    out = selector.select(_SM(), None, QUERY, skills=LIB, threshold=0.0, max_items=5,
                          engines=engines)
    return [s["name"] for s in out]


def test_unity_workspace_prefers_the_unity_skill():
    assert _names(["unity"])[0] == "unity-physics"
    assert _names(["unity"]).index("godot-physics") > _names(["unity"]).index("unity-physics")


def test_godot_workspace_prefers_the_godot_skill():
    assert _names(["godot"])[0] == "godot-physics"


def test_selector_reports_the_engine_factor_it_applied():
    out = selector.select(_SM(), None, QUERY, skills=LIB, threshold=0.0, max_items=5,
                          engines=["unity"])
    by_name = {s["name"]: s["_selector"] for s in out}
    assert by_name["unity-physics"]["engine"] == ec.MATCH_BOOST
    assert by_name["godot-physics"]["engine"] == ec.MISMATCH_FACTOR
    assert "engine" not in by_name["camera-systems"]


def test_without_an_engine_scores_are_unchanged():
    plain = selector.select(_SM(), None, QUERY, skills=LIB, threshold=0.0, max_items=5)
    empty = selector.select(_SM(), None, QUERY, skills=LIB, threshold=0.0, max_items=5,
                            engines=[])
    assert [(s["name"], s["_selector"]["score"]) for s in plain] == \
           [(s["name"], s["_selector"]["score"]) for s in empty]
    assert all("engine" not in s["_selector"] for s in plain)


def test_a_mismatched_engine_skill_falls_below_the_threshold():
    with_unity = selector.select(_SM(), None, QUERY, skills=LIB, threshold=0.2,
                                 max_items=5, engines=["unity"])
    assert "godot-physics" not in [s["name"] for s in with_unity]


def test_agent_loop_passes_the_workspace_engines_to_the_selector():
    text = (ROOT / "src" / "agent_loop.py").read_text(encoding="utf-8")
    assert "engine_context import detect_engines" in text
    assert "engines=_skill_engines" in text


# ── importer pieces ─────────────────────────────────────────────────────────

def test_import_bundle_merges_tags_and_publishes(tmp_path):
    from services.memory.skills import SkillsManager
    sm = SkillsManager(data_dir=str(tmp_path))
    files = {
        "SKILL.md": "---\nname: demo-engine\ndescription: Demo skill for an engine.\n---\n\n"
                    "## When to use\n\n- Use when testing.\n",
        "references/extra.md": "more\n",
        "UPSTREAM-LICENSE.txt": "licence text\n",
    }
    entry = sm.import_bundle_from_files(files, source_url="https://example/x (pinned abc)",
                                        category="gamedev", tags=["Unity"], status="published")
    assert entry["status"] == "published"
    assert "unity" in entry["tags"]
    assert entry["category"] == "gamedev"
    skill_dir = Path(entry["path"]).parent
    assert (skill_dir / "references" / "extra.md").is_file()
    assert (skill_dir / "UPSTREAM-LICENSE.txt").is_file()
    assert "Imported from https://example/x (pinned abc)" in Path(entry["path"]).read_text(encoding="utf-8")


def test_import_bundle_default_status_is_unchanged(tmp_path):
    from services.memory.skills import SkillsManager
    sm = SkillsManager(data_dir=str(tmp_path))
    entry = sm.import_bundle_from_files(
        {"SKILL.md": "---\nname: plain-one\ndescription: Plain.\n---\n\nbody\n"})
    assert entry["status"] != "published"


class _Resp:
    status_code = 200
    url = "https://api.github.com/repos/o/r/contents/x"

    def json(self):
        return []


def _capture_headers(monkeypatch):
    from services.memory import skill_importer
    seen = {}
    monkeypatch.setattr(skill_importer, "_get_checked",
                        lambda url, headers=None, timeout=30.0: seen.update(headers=headers) or _Resp())
    monkeypatch.setattr(skill_importer, "_resolve_and_check_url",
                        lambda url: [ipaddress.ip_address("93.184.216.34")])
    return skill_importer, seen


def test_github_token_is_sent_to_the_api_host_only_when_set(monkeypatch):
    mod, seen = _capture_headers(monkeypatch)
    src = mod.ResolvedSource(owner="o", repo="r", ref="a" * 40, path="x")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    mod._list_github_dir(src, "x", {})
    assert "Authorization" not in seen["headers"]
    monkeypatch.setenv("GH_TOKEN", "tok123")
    mod._list_github_dir(src, "x", {})
    assert seen["headers"]["Authorization"] == "Bearer tok123"


def _load_script():
    spec = importlib.util.spec_from_file_location("import_github_skills",
                                                  ROOT / "scripts" / "import_github_skills.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_import_script_requires_a_full_commit_sha():
    mod = _load_script()
    for bad in ("main", "v1.0", "19d93f9", ""):
        with pytest.raises(SystemExit):
            mod.import_skills("o/r", bad, ["skills/a"], category="x", tags=[])
    assert mod._tree_url("o/r", "a" * 40, "/skills/a/") == f"https://github.com/o/r/tree/{'a' * 40}/skills/a"
    assert mod._relative_to_skill({"skills/a/SKILL.md": "x", "skills/a/references/r.md": "y"},
                                  "skills/a") == {"SKILL.md": "x", "references/r.md": "y"}
