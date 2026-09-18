"""ADP-02 — the provenance manifest is DATA, not prose: validate its shape
and cross-check it against the actual repository instead of trusting that
whoever edits it keeps it honest.

Guards two failure modes a plain markdown-only manifest cannot catch:
  * a malformed/incomplete entry (missing a required field, wrong type);
  * a `destination` that points at a file that does not (or no longer)
    exist -- the classic way a provenance record silently rots after a
    rename or a removal.
"""
from __future__ import annotations

import json
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROVENANCE_PATH = os.path.join(REPO_ROOT, "docs", "adaptations", "provenance.json")
NOTICES_PATH = os.path.join(REPO_ROOT, "THIRD_PARTY_NOTICES.md")
BASELINE_PATH = os.path.join(REPO_ROOT, "docs", "adaptations", "baseline.md")

# Every field an entry must carry, per the shape this lote's contract
# specifies. `blob_sha` is deliberately NOT required (marked optional there).
_REQUIRED_ENTRY_FIELDS = (
    "id",
    "source_repo",
    "commit",
    "path",
    "license",
    "notice_kept",
    "destination",
    "reason",
    "modifications",
)


def _load_provenance() -> dict:
    with open(PROVENANCE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def test_provenance_file_exists_and_is_valid_json():
    assert os.path.isfile(PROVENANCE_PATH), "docs/adaptations/provenance.json is missing"
    data = _load_provenance()
    assert isinstance(data, dict)


def test_provenance_schema_version_and_entries_shape():
    data = _load_provenance()
    assert data.get("schema_version") == 1
    entries = data.get("entries")
    assert isinstance(entries, list) and entries, "entries must be a non-empty list"

    seen_ids = set()
    for entry in entries:
        assert isinstance(entry, dict), f"entry is not an object: {entry!r}"
        missing = [field for field in _REQUIRED_ENTRY_FIELDS if field not in entry]
        assert not missing, f"entry {entry.get('id')!r} is missing fields: {missing}"

        # Every required field is a non-empty string, except notice_kept
        # (bool) -- a blank string here is exactly the kind of "technically
        # present, actually useless" entry this test exists to catch.
        for field in _REQUIRED_ENTRY_FIELDS:
            if field == "notice_kept":
                assert isinstance(entry["notice_kept"], bool), (
                    f"{entry.get('id')}.notice_kept must be a bool"
                )
                continue
            value = entry[field]
            assert isinstance(value, str) and value.strip(), (
                f"{entry.get('id')}.{field} must be a non-empty string, got {value!r}"
            )

        # Never a guessed commit: an unpinned/unknown source must say so
        # explicitly, matching this repo's "unknown, never fabricated"
        # discipline for anything it cannot actually verify.
        commit = entry["commit"]
        assert commit == "unknown" or len(commit) >= 7, (
            f"{entry.get('id')}.commit looks fabricated (not 'unknown', not a "
            f"real-looking hash): {commit!r}"
        )

        # blob_sha, when present, must be a string (it's the one optional field).
        if "blob_sha" in entry and entry["blob_sha"] is not None:
            assert isinstance(entry["blob_sha"], str)

        assert entry["id"] not in seen_ids, f"duplicate entry id: {entry['id']!r}"
        seen_ids.add(entry["id"])


def test_every_destination_exists_in_this_repo():
    """The whole point of a machine-checkable manifest: a `destination` that
    stops existing (renamed, deleted) must fail CI, not rot silently."""
    data = _load_provenance()
    missing = []
    for entry in data["entries"]:
        destination = entry["destination"]
        # A destination naming a directory (e.g. a future package-level
        # entry) is also acceptable -- isdir covers that.
        full = os.path.join(REPO_ROOT, destination)
        if not (os.path.isfile(full) or os.path.isdir(full)):
            missing.append((entry["id"], destination))
    assert not missing, f"destinations that do not exist in this repo: {missing}"


def test_no_license_is_silently_upgraded_from_copyleft():
    """ADP-02 limit: never replace an upstream GPL/AGPL notice with a
    permissive one. This only checks the manifest is internally honest
    about aigraphstudio being MIT, not GPL/AGPL -- a real license swap
    would have to be a deliberate, reviewed edit to this very assertion."""
    data = _load_provenance()
    for entry in data["entries"]:
        if "aigraphstudio" in entry["source_repo"]:
            assert entry["license"] == "MIT", (
                f"{entry['id']}: aigraphstudio is MIT upstream; got {entry['license']!r}"
            )


def test_third_party_notices_file_exists_and_lists_expected_sources():
    assert os.path.isfile(NOTICES_PATH), "THIRD_PARTY_NOTICES.md is missing"
    with open(NOTICES_PATH, "r", encoding="utf-8") as f:
        text = f.read()
    assert "Odysseus" in text
    assert "AGPL" in text
    assert "aigraphstudio" in text
    assert "MIT" in text
    # The "how to add an entry" section is a hard requirement of this lote.
    lowered = text.lower()
    assert "how to add an entry" in lowered


def test_baseline_document_exists_and_is_pinned_to_the_inventory_sha():
    assert os.path.isfile(BASELINE_PATH), "docs/adaptations/baseline.md is missing"
    with open(BASELINE_PATH, "r", encoding="utf-8") as f:
        text = f.read()
    assert "3b1c402" in text, "baseline.md must cite the ADP-01 inventory's HEAD sha"
    # It must actually be the table, not just a pointer back to the scratch
    # inventory -- spot-check a handful of ADP ids are present as rows.
    for adp_id in ("ADP-04", "ADP-15", "ADP-22", "ADP-28", "ADP-32"):
        assert adp_id in text, f"baseline.md table is missing {adp_id}"
