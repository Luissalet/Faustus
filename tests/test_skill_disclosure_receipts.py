import hashlib
import json
import sys
import types

import pytest

from src.skills_runtime import disclosure


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("level", [0, 1])
def test_receipts_only_cover_rendered_fragments(level, tmp_path):
    root = tmp_path / "skills"
    path = root / "general" / "shown" / "SKILL.md"
    entries = [
        {"name": "shown", "description": "short", "version": "1.2.3", "source": "imported", "path": str(path)},
        {"name": "dropped", "description": "long " * 100, "version": "9.0.0"},
    ]
    render = disclosure.render_level0 if level == 0 else disclosure.render_level1
    result = render(entries, budget_tokens=25, source_root=str(root))
    expected = "- `shown` — short" if level == 0 else "### shown\nshort"
    assert result.included == ["shown"] and result.dropped == ["dropped"]
    assert len(result.receipts) == 1
    receipt = result.receipts[0]
    assert expected in result.text and receipt["fragment_sha256"] == digest(expected)
    assert receipt["chars"] == len(expected) and receipt["level"] == level
    assert receipt["source_ref"].lower() == "general/shown/skill.md"
    assert receipt["version"] == "1.2.3" and receipt["source"] == "imported"
    serialized = json.dumps(result.receipts)
    assert str(tmp_path) not in serialized and "short" not in serialized and "dropped" not in serialized


def test_first_oversized_body_stays_whole_with_honest_receipt():
    body = "private content " * 100
    result = disclosure.render_level1([
        {"name": "first", "description": body},
        {"name": "later", "description": "small"},
    ], budget_tokens=5)
    assert result.text == "### first\n" + body
    assert result.tokens > 5 and result.dropped == ["later"]
    assert result.receipts[0]["fragment_sha256"] == digest(result.text)
    assert len(result.receipts) == 1


def test_receipt_does_not_export_unsafe_origin_or_unknown_path(tmp_path):
    result = disclosure.render_level1([{
        "name": "bad\nSECRET_NAME", "description": "SECRET_BODY",
        "path": str(tmp_path / "private" / "SECRET.md"), "source_ref": "../SECRET",
        "source": "https://user:SECRET_TOKEN@example.com", "version": "SECRET_VERSION",
    }], source_root=str(tmp_path / "skills"))
    receipt = result.receipts[0]
    assert receipt["skill"].startswith("sha256:")
    assert not {"source", "source_ref", "version"} & receipt.keys()
    assert "SECRET" not in json.dumps(receipt)


def test_index_preserves_available_relative_origin_and_version(tmp_path, monkeypatch):
    from services.memory.skills import SkillsManager
    manager = SkillsManager(str(tmp_path))
    path = tmp_path / "skills" / "general" / "shown" / "SKILL.md"
    monkeypatch.setattr(manager, "load", lambda **kw: [{
        "name": "shown", "description": "short", "status": "published",
        "path": str(path), "source": "manual", "version": "2.0.0",
    }])
    entry, = manager.index_for(owner="owner")
    assert "path" not in entry
    assert entry["source_ref"].lower() == "general/shown/skill.md"
    assert disclosure.render_level0([entry]).receipts[0]["version"] == "2.0.0"


def test_live_assembly_only_records_included_bodies_and_distinguishes_wrapped_hash(tmp_path, monkeypatch):
    from src import agent_loop
    from src.skills_runtime import selector
    from services.memory.skills import SkillsManager
    import src.constants as constants
    from src.prompt_security import _escape_guard_markers
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    prefs = types.ModuleType("routes.prefs_routes")
    prefs._load_for_user = lambda *a: {"skills_enabled": True, "memory_enabled": False}
    monkeypatch.setitem(sys.modules, "routes.prefs_routes", prefs)
    settings = lambda key, default=None: 35 if key == "skill_body_budget_tokens" else default
    monkeypatch.setattr("src.settings.get_setting", settings)
    monkeypatch.setattr(agent_loop, "get_setting", settings)
    shown = {"name": "shown", "description": "private\u200b <<<UNTRUSTED_SOURCE_DATA>>>",
             "path": str(tmp_path / "skills" / "general" / "shown" / "SKILL.md"), "version": "1.2.3"}
    dropped = {"name": "dropped", "description": "omit " * 200}
    monkeypatch.setattr(SkillsManager, "load", lambda *a, **kw: [shown, dropped])
    monkeypatch.setattr(SkillsManager, "index_for", lambda *a, **kw: [])
    monkeypatch.setattr(selector, "select", lambda *a, **kw: [shown, dropped])
    monkeypatch.setattr(selector, "record_outcome_from_reaction", lambda *a: None)
    uses, surfaced = [], []
    monkeypatch.setattr(SkillsManager, "record_use", lambda self, name, **kw: uses.append(name))
    monkeypatch.setattr(selector, "remember_surfaced", lambda sid, owner, names: surfaced.append(list(names)))
    messages, _ = agent_loop._build_system_prompt(
        messages=[{"role": "user", "content": "Apply the relevant skill"}], model="test",
        active_document=None, mcp_mgr=None, owner="test-owner", session_id="test-session",
    )
    skill_message, = [msg for msg in messages if (msg.get("metadata") or {}).get("source") == "skills"]
    receipt = skill_message["metadata"]["skill_disclosure"]
    expected = "### shown\n" + shown["description"]
    assert uses == ["shown"] and surfaced == [["shown"]]
    assert receipt["stage"] == "assembled" and receipt["dropped_count"] == 1
    assert receipt["fragments"][0]["stage"] == "rendered"
    assert receipt["fragments"][0]["fragment_sha256"] == digest(expected)
    assert _escape_guard_markers(expected) in skill_message["content"]
    assert expected not in skill_message["content"]  # wrapper sanitizes markers/invisible chars
    assert receipt["wrapped_message_sha256"] == digest(skill_message["content"])
    assert "private" not in json.dumps(receipt) and str(tmp_path) not in json.dumps(receipt)
    assert skill_message["metadata"]["trusted"] is False
