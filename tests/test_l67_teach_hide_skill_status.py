"""Lote 67 — ola A wiring: `TeachService._hide_installed_skill` now maps the
procedure FSM's target status onto `services.memory.skill_format.STATUSES`
vocabulary (deprecated / obsolete) instead of collapsing every hide into
`"draft"` — a status meaning "not reviewed yet", backwards for a skill a
person just deprecated, quarantined or revoked.

Before this lote: `grep -n '"status": "draft"' src/teach_mode/service.py`
matched unconditionally inside `_hide_installed_skill`, for all three
transitions (deprecate/revoke/quarantine) alike.
"""
from __future__ import annotations

import pytest

from src.teach_mode.service import TeachService


def test_status_map_deprecate_and_quarantine_reach_deprecated():
    assert TeachService._SKILL_STATUS_FOR_TARGET["deprecated"] == "deprecated"
    assert TeachService._SKILL_STATUS_FOR_TARGET["quarantined"] == "deprecated"


def test_status_map_revoke_reaches_obsolete():
    assert TeachService._SKILL_STATUS_FOR_TARGET["revoked"] == "obsolete"


def test_status_map_values_are_all_real_skill_statuses():
    from services.memory.skill_format import STATUSES
    for value in TeachService._SKILL_STATUS_FOR_TARGET.values():
        assert value in STATUSES


@pytest.mark.parametrize("target,expected_status", [
    ("deprecated", "deprecated"),
    ("quarantined", "deprecated"),
    ("revoked", "obsolete"),
])
def test_hide_installed_skill_writes_the_mapped_status(monkeypatch, target, expected_status):
    calls = []

    class _FakeSkillsManager:
        def __init__(self, data_dir):
            pass

        def update_skill(self, name, patch, *, owner=""):
            calls.append({"name": name, "patch": dict(patch), "owner": owner})
            return {"ok": True}

    monkeypatch.setattr("services.memory.skills.SkillsManager", _FakeSkillsManager)

    TeachService._hide_installed_skill(
        "alice",
        {"installed_skill_ref": "skill://my-taught-skill"},
        procedure_target=target,
    )

    assert len(calls) == 1
    assert calls[0]["name"] == "my-taught-skill"
    assert calls[0]["patch"] == {"status": expected_status}
    assert calls[0]["owner"] == "alice"


def test_hide_installed_skill_defaults_to_deprecated_for_an_unmapped_target(monkeypatch):
    """A defensive fallback, never `"draft"`: an unrecognised target must not
    silently reopen a hidden skill as though it were freshly authored."""
    calls = []

    class _FakeSkillsManager:
        def __init__(self, data_dir):
            pass

        def update_skill(self, name, patch, *, owner=""):
            calls.append(dict(patch))
            return {"ok": True}

    monkeypatch.setattr("services.memory.skills.SkillsManager", _FakeSkillsManager)
    TeachService._hide_installed_skill(
        "alice", {"installed_skill_ref": "skill://x"}, procedure_target="",
    )
    assert calls == [{"status": "deprecated"}]


def test_hide_installed_skill_no_op_without_an_installed_ref(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "services.memory.skills.SkillsManager",
        lambda data_dir: pytest.fail("must not touch the skills manager"),
    )
    TeachService._hide_installed_skill("alice", {}, procedure_target="revoked")
