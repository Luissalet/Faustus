"""B-018: "newest version" was "last alphabetically".

`load()` sorted `w.version` as text, so `1.9.0` came out above `1.10.0` and
Faustus ran the older template while a newer approved one sat next to it.
Ordering now goes through the project's one semver contract.
"""

import json
from pathlib import Path

import pytest

from src.contracts.base import is_semver, semver_key
from src.media_workflows import WORKFLOWS_DIR, catalogue, load

TEMPLATE = Path(WORKFLOWS_DIR) / "image.quick-draft.v1.json"


@pytest.fixture
def library(tmp_path):
    """A disposable template directory; `add(version)` puts one in it."""
    base = json.loads(TEMPLATE.read_text(encoding="utf-8"))

    def add(version, workflow_id="test.workflow", title=None):
        payload = dict(base)
        payload["id"] = workflow_id
        payload["version"] = version
        payload["title"] = title or f"{workflow_id} {version}"
        name = f"{workflow_id}.{version}.json".replace("+", "_plus_")
        (tmp_path / name).write_text(json.dumps(payload), encoding="utf-8")
        return payload

    return tmp_path, add


# ── the ordering itself ────────────────────────────────────────────────────

def test_ten_is_newer_than_nine():
    """The bug, in one line."""
    assert semver_key("1.10.0") > semver_key("1.9.0")
    assert "1.10.0" < "1.9.0"  # ...which is exactly what text ordering said


def test_semver_precedence_follows_the_spec():
    ordered = [
        "1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta",
        "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0",
        "1.9.0", "1.10.0", "2.0.0",
    ]
    assert sorted(ordered, key=semver_key) == ordered


def test_build_metadata_carries_no_precedence():
    assert semver_key("1.0.0+alpha") == semver_key("1.0.0+omega") == semver_key("1.0.0")


def test_semver_key_refuses_what_is_not_a_version():
    for bad in ("1.0", "v1.0.0", "", None, "1.0.0.0", "latest", "01.0.0"):
        assert not is_semver(bad)
        with pytest.raises(ValueError):
            semver_key(bad)


# ── what load() picks ──────────────────────────────────────────────────────

def test_load_picks_1_10_over_1_9(library):
    folder, add = library
    add("1.9.0")
    add("1.10.0")
    chosen = load("test.workflow", directory=str(folder))
    assert chosen is not None and chosen.version == "1.10.0"


def test_load_prefers_the_release_over_its_prereleases(library):
    folder, add = library
    add("2.0.0-rc.1")
    add("2.0.0-rc.2")
    add("2.0.0")
    assert load("test.workflow", directory=str(folder)).version == "2.0.0"


def test_a_prerelease_wins_only_when_it_is_all_there_is(library):
    folder, add = library
    add("2.0.0-rc.1")
    add("2.0.0-rc.2")
    add("1.9.0")
    assert load("test.workflow", directory=str(folder)).version == "2.0.0-rc.2"


def test_an_exact_request_gets_that_version_and_no_other(library):
    folder, add = library
    add("1.9.0")
    add("1.10.0")
    assert load("test.workflow", "1.9.0", directory=str(folder)).version == "1.9.0"
    assert load("test.workflow", "3.0.0", directory=str(folder)) is None


def test_a_build_metadata_tie_is_broken_the_same_way_every_time(library):
    folder, add = library
    add("1.0.0+alpha")
    add("1.0.0+omega")
    picks = {load("test.workflow", directory=str(folder)).version for _ in range(5)}
    assert len(picks) == 1, "the tie-break must be deterministic"
    # Documented policy: the version string decides, since precedence does not.
    assert picks.pop() == "1.0.0+omega"


def test_other_ids_are_not_considered(library):
    folder, add = library
    add("9.9.9", workflow_id="other.workflow")
    add("1.0.0")
    assert load("test.workflow", directory=str(folder)).version == "1.0.0"


# ── rejected at registration, not at run time ──────────────────────────────

def test_a_bad_version_is_refused_when_the_template_is_read(library):
    folder, add = library
    add("1.0.0")
    add("banana")
    report = catalogue(directory=str(folder))
    assert [w.version for w in report["workflows"]] == ["1.0.0"]
    assert len(report["broken"]) == 1
    assert report["broken"][0]["field"] == "workflow.version"
    assert "semantic version" in report["broken"][0]["reason"]
    # ...and the good one is still loadable: one bad file is not an outage.
    assert load("test.workflow", directory=str(folder)).version == "1.0.0"


def test_the_shipped_templates_all_declare_a_real_version():
    for workflow in catalogue()["workflows"]:
        assert is_semver(workflow.version), f"{workflow.id} has {workflow.version!r}"
