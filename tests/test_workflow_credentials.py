import json

import pytest
from cryptography.fernet import Fernet
from src import secret_storage
from src.workflows import credentials as store


@pytest.fixture
def credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "credentials.json")
    monkeypatch.setattr(secret_storage, "_fernet", Fernet(Fernet.generate_key()))
    return store


def test_storage_is_encrypted_and_metadata_never_exposes_values(credentials):
    row = credentials.put("alice", "provider", "synthetic-secret-value")
    assert "synthetic-secret-value" not in credentials.STORE_PATH.read_text()
    assert credentials.list_metadata("alice") == [row]
    binding = credentials.bind("alice", ["API_KEY"], {"API_KEY": "provider"})
    assert "synthetic-secret-value" not in str(binding)
    assert credentials.bind("alice", ["API_KEY"], {"API_KEY": "provider"}, reveal=True,
                            expected=binding) == {"API_KEY": "synthetic-secret-value"}


def test_same_name_is_owner_scoped(credentials):
    credentials.put("alice", "provider", "alice-secret")
    assert credentials.list_metadata("bob") == []
    with pytest.raises(ValueError, match="unavailable"):
        credentials.bind("bob", ["KEY"], {"KEY": "provider"})


def test_rotation_invalidates_an_approved_binding(credentials):
    row = credentials.put("alice", "provider", "old-secret")
    approved = credentials.bind("alice", ["KEY"], {"KEY": "provider"})
    credentials.put("alice", "provider", "new-secret", expected_revision=row["revision"])
    with pytest.raises(ValueError, match="changed"):
        credentials.bind("alice", ["KEY"], {"KEY": "provider"}, reveal=True, expected=approved)
    with pytest.raises(ValueError, match="changed"):
        credentials.remove("alice", "provider", expected_revision=row["revision"])


def test_revocation_and_unknown_slots_refuse(credentials):
    row = credentials.put("alice", "provider", "secret")
    assert credentials.remove("alice", "provider", expected_revision=row["revision"])
    with pytest.raises(ValueError):
        credentials.bind("alice", ["KEY"], {"KEY": "provider"})
    with pytest.raises(ValueError):
        credentials.bind("alice", [], {"KEY": "provider"})


def test_corrupt_store_is_not_overwritten(credentials):
    credentials.STORE_PATH.write_text("broken")
    with pytest.raises(ValueError, match="unreadable"):
        credentials.put("alice", "provider", "secret")
    assert credentials.STORE_PATH.read_text() == "broken"


def test_swapped_encrypted_values_cannot_cross_owners(credentials):
    credentials.put("alice", "provider", "alice-secret")
    credentials.put("bob", "provider", "bob-secret")
    approved = credentials.bind("alice", ["KEY"], {"KEY": "provider"})
    data = json.loads(credentials.STORE_PATH.read_text())
    alice, bob = data[store._key("alice", "provider")], data[store._key("bob", "provider")]
    alice["encrypted"] = bob["encrypted"]
    credentials.STORE_PATH.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="decrypted"):
        credentials.bind("alice", ["KEY"], {"KEY": "provider"}, reveal=True, expected=approved)
