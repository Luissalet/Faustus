"""A default that changed is not frozen by the whole-document save, and a
saved old default reads as the new one."""
import json

from src import settings as st


def _use(tmp_path, monkeypatch, doc):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setattr(st, "SETTINGS_FILE", str(path))
    st._invalidate_caches()
    return path


def test_saved_old_default_reads_as_the_new_one(tmp_path, monkeypatch):
    _use(tmp_path, monkeypatch, {"agent_sticky_toolset_max": 28})
    assert st.get_setting("agent_sticky_toolset_max") == 48


def test_an_explicit_other_value_is_kept(tmp_path, monkeypatch):
    _use(tmp_path, monkeypatch, {"agent_sticky_toolset_max": 36})
    assert st.get_setting("agent_sticky_toolset_max") == 36


def test_whole_document_save_does_not_write_untouched_defaults(tmp_path, monkeypatch):
    path = _use(tmp_path, monkeypatch, {"theme": "x"})
    doc = st.load_settings()
    doc["theme"] = "y"
    st.save_settings(doc)
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["theme"] == "y"
    assert "agent_sticky_toolset_max" not in written
    doc2 = st.load_settings()
    doc2["agent_keep_images"] = 3
    st.save_settings(doc2)
    assert json.loads(path.read_text(encoding="utf-8"))["agent_keep_images"] == 3
