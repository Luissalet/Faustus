import json
import sqlite3

import pytest

from src.durable_feature_store import DurableFeatureStore, RevisionConflict


def test_stale_revision_cannot_overwrite_a_newer_document(tmp_path):
    store = DurableFeatureStore(str(tmp_path / "features.db"))
    original = store.put("asset", {"id": "tool://render", "value": "old"}, owner="alice")
    current = store.put(
        "asset", {"id": "tool://render", "value": "new"}, owner="alice",
        expected_revision=original["revision"],
    )

    with pytest.raises(RevisionConflict):
        store.put(
            "asset", {"id": "tool://render", "value": "stale"}, owner="alice",
            expected_revision=original["revision"],
        )

    saved = store.get("asset", "tool://render", owner="alice")
    assert saved["value"] == "new"
    assert saved["revision"] == current["revision"]


def test_legacy_global_key_schema_migrates_to_owner_scoped_keys(tmp_path):
    path = tmp_path / "legacy.db"
    payload = {
        "id": "tool://render", "owner": "alice", "value": "alice",
        "project_id": "", "session_id": "", "status": "healthy",
    }
    with sqlite3.connect(path) as db:
        db.execute(
            """CREATE TABLE documents (
                kind TEXT NOT NULL, id TEXT NOT NULL, owner TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT '', session_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 1,
                payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY (kind, id)
            )"""
        )
        db.execute(
            "INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("asset", "tool://render", "alice", "", "", "healthy", 1,
             json.dumps(payload), "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )

    store = DurableFeatureStore(str(path))
    store.put("asset", {"id": "tool://render", "value": "bob"}, owner="bob")

    assert store.get("asset", "tool://render", owner="alice")["value"] == "alice"
    assert store.get("asset", "tool://render", owner="bob")["value"] == "bob"
