"""B-023: staged compose attachments must have an owner and an expiry.

Staging used to hand the client `<uuid>_<original name>` and treat that string
as the whole capability: any authenticated user who learned one could attach
the file to their own message or delete it, and an abandoned draft stayed on
disk forever. These tests pin the replacement - opaque owner-bound ids, (id,
owner) lookups on every read/send/delete, a TTL sweep, and a lease that keeps
a scheduled message's attachment alive past that TTL.
"""
import base64
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import routes.email_helpers as email_helpers  # noqa: E402
import routes.email_routes as email_routes  # noqa: E402


def _route_endpoint(router, path: str, method: str):
    method = method.upper()
    for route in router.routes:
        if route.path == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


@pytest.fixture
def staging(tmp_path, monkeypatch):
    """Point the staging registry at a scratch attachments tree."""
    attachments = tmp_path / "mail-attachments"
    compose = attachments / "_compose"
    compose.mkdir(parents=True)
    monkeypatch.setattr(email_helpers, "COMPOSE_UPLOADS_DIR", compose)
    monkeypatch.setattr(
        email_helpers, "COMPOSE_STAGING_INDEX", attachments / "_compose_index.json"
    )
    monkeypatch.setattr(email_helpers, "_compose_last_sweep", 0.0)
    return SimpleNamespace(root=attachments, compose=compose)


def _index(staging) -> dict:
    path = staging.root / "_compose_index.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


# --- opaque, owner-bound ids -------------------------------------------------

def test_staging_id_is_opaque_and_scoped_to_its_owner(staging):
    row = email_helpers.register_compose_upload(
        "alice", "Quarterly Report.pdf", content=b"%PDF-1.4 alice"
    )

    assert re.fullmatch(r"[0-9a-f]{32}", row["id"]), row["id"]
    assert "Quarterly" not in row["id"] and "pdf" not in row["id"]
    # The bytes on disk are named by the id alone, under a directory that does
    # not spell out the account.
    stored = Path(row["path"])
    assert stored.name == row["id"]
    assert "alice" not in str(stored)
    assert stored.parent != staging.compose

    assert email_helpers.resolve_compose_upload(row["id"], "alice") is not None
    assert email_helpers.resolve_compose_upload(row["id"], "bob") is None
    # A garbage id must not be mistaken for a path fragment either.
    assert email_helpers.resolve_compose_upload("../../etc/passwd", "alice") is None


def test_leaked_staging_id_cannot_be_attached_from_another_account(staging):
    row = email_helpers.register_compose_upload(
        "alice", "salary.txt", content=b"alice private payroll"
    )

    thief = MIMEMultipart("mixed")
    email_helpers._attach_compose_uploads(thief, [row["id"]], owner="bob")
    assert thief.get_payload() == [], "another owner's staged file was attached"

    mine = MIMEMultipart("mixed")
    email_helpers._attach_compose_uploads(mine, [row["id"]], owner="alice")
    parts = mine.get_payload()
    assert len(parts) == 1
    assert parts[0].get_filename() == "salary.txt"
    assert base64.b64decode(parts[0].get_payload()) == b"alice private payroll"


def test_cleanup_ignores_another_owners_staged_file(staging):
    row = email_helpers.register_compose_upload("alice", "keep.txt", content=b"keep")

    email_helpers._cleanup_compose_uploads([row["id"]], owner="bob")
    assert Path(row["path"]).is_file()

    email_helpers._cleanup_compose_uploads([row["id"]], owner="alice")
    assert not Path(row["path"]).exists()
    assert row["id"] not in _index(staging)


# --- the delete endpoint -----------------------------------------------------

@pytest.mark.asyncio
async def test_delete_compose_upload_404s_for_another_owner(staging):
    row = email_helpers.register_compose_upload("alice", "notes.txt", content=b"mine")
    delete_upload = _route_endpoint(
        email_routes.setup_email_routes(), "/api/email/compose-upload/{token}", "DELETE"
    )

    with pytest.raises(HTTPException) as thief:
        await delete_upload(row["id"], owner="bob")
    assert thief.value.status_code == 404
    assert Path(row["path"]).is_file(), "a stranger's DELETE removed the bytes"

    # An id that never existed answers exactly the same way, so the endpoint
    # cannot be used to confirm that someone else's id is real.
    with pytest.raises(HTTPException) as unknown:
        await delete_upload("0" * 32, owner="bob")
    assert unknown.value.status_code == 404

    assert await delete_upload(row["id"], owner="alice") == {"success": True}
    assert not Path(row["path"]).exists()
    assert _index(staging) == {}


@pytest.mark.asyncio
async def test_compose_upload_token_carries_no_filename_and_binds_the_owner(staging):
    upload_endpoint = _route_endpoint(
        email_routes.setup_email_routes(), "/api/email/compose-upload", "POST"
    )

    class _Upload:
        filename = "Board Minutes 2026.docx"

        async def read(self, _size):
            return b"board minutes"

    result = await upload_endpoint(file=_Upload(), owner="alice")

    assert result["success"] is True
    assert re.fullmatch(r"[0-9a-f]{32}", result["token"])
    assert "Board" not in result["token"] and "docx" not in result["token"]
    assert result["filename"] == "Board Minutes 2026.docx"
    assert email_helpers.resolve_compose_upload(result["token"], "bob") is None
    assert email_helpers.resolve_compose_upload(result["token"], "alice") is not None


# --- expiry, lease, sweep ----------------------------------------------------

def test_abandoned_draft_expires_and_the_sweep_removes_its_bytes(staging):
    row = email_helpers.register_compose_upload(
        "alice", "draft.txt", content=b"abandoned", ttl_seconds=60
    )
    path = Path(row["path"])
    assert path.is_file()

    assert email_helpers.cleanup_expired_compose_uploads(now=time.time() + 120) == 1
    assert not path.exists()
    assert _index(staging) == {}
    assert email_helpers.resolve_compose_upload(row["id"], "alice") is None


def test_staging_sweeps_expired_siblings_opportunistically(staging, monkeypatch):
    """Nothing else in the request lifecycle revisits an abandoned draft, so
    the next staging call is the only reliable trigger."""
    stale = email_helpers.register_compose_upload(
        "alice", "stale.txt", content=b"stale", ttl_seconds=0.05
    )
    assert Path(stale["path"]).is_file()
    time.sleep(0.1)
    monkeypatch.setattr(email_helpers, "_compose_last_sweep", 0.0)

    email_helpers.register_compose_upload("bob", "fresh.txt", content=b"fresh")

    assert not Path(stale["path"]).exists()
    assert stale["id"] not in _index(staging)


def test_lease_only_applies_to_the_callers_own_items(staging):
    alice = email_helpers.register_compose_upload(
        "alice", "a.txt", content=b"a", ttl_seconds=60
    )
    bob = email_helpers.register_compose_upload(
        "bob", "b.txt", content=b"b", ttl_seconds=60
    )

    leased = email_helpers.lease_compose_uploads(
        [alice["id"], bob["id"]], "alice", time.time() + 3600
    )

    assert leased == [alice["id"]]
    assert email_helpers.cleanup_expired_compose_uploads(now=time.time() + 120) == 1
    assert Path(alice["path"]).is_file()
    assert not Path(bob["path"]).exists()


@pytest.mark.asyncio
async def test_scheduled_send_leases_the_attachment_past_the_draft_ttl(
    staging, tmp_path, monkeypatch
):
    db_path = tmp_path / "scheduled_emails.db"
    monkeypatch.setattr(email_helpers, "SCHEDULED_DB", db_path)
    monkeypatch.setattr(email_routes, "SCHEDULED_DB", db_path)
    email_helpers._init_scheduled_db()

    row = email_helpers.register_compose_upload(
        "alice", "invoice.pdf", content=b"%PDF invoice", ttl_seconds=1
    )
    schedule_email = _route_endpoint(
        email_routes.setup_email_routes(), "/api/email/schedule", "POST"
    )
    send_at = datetime.now(timezone.utc) + timedelta(days=30)

    scheduled = await schedule_email(
        {
            "to": "a@example.com",
            "body": "body",
            "send_at": send_at.isoformat(),
            "attachments": [row["id"]],
        },
        owner="alice",
    )
    assert scheduled["success"] is True

    # Well past the one-second draft TTL, but inside the lease.
    assert email_helpers.cleanup_expired_compose_uploads(now=time.time() + 86400) == 0
    assert Path(row["path"]).is_file()

    # The poller has no request identity: it resolves by id alone, on behalf of
    # a row it already selected by owner.
    delivered = MIMEMultipart("mixed")
    email_helpers._attach_compose_uploads(delivered, [row["id"]])
    assert len(delivered.get_payload()) == 1

    # ...and the lease is not a way to pin somebody else's attachment.
    assert email_helpers.resolve_compose_upload(row["id"], "bob") is None


# --- survives a restart ------------------------------------------------------

_CHILD_STAGE = r"""
import os, sys
sys.path.insert(0, sys.argv[1])
os.environ["ODYSSEUS_MAIL_ATTACHMENTS_DIR"] = sys.argv[2]
import routes.email_helpers as H
print(H.register_compose_upload("alice", "persisted.txt", content=b"survive")["id"])
"""


def test_staged_item_keeps_its_owner_across_a_restart(tmp_path, monkeypatch):
    attachments = tmp_path / "mail-attachments"
    attachments.mkdir()

    child = subprocess.run(
        [sys.executable, "-c", _CHILD_STAGE, str(PROJECT_ROOT), str(attachments)],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(PROJECT_ROOT),
    )
    assert child.returncode == 0, child.stderr
    item_id = child.stdout.strip().splitlines()[-1]

    monkeypatch.setattr(email_helpers, "COMPOSE_UPLOADS_DIR", attachments / "_compose")
    monkeypatch.setattr(
        email_helpers, "COMPOSE_STAGING_INDEX", attachments / "_compose_index.json"
    )

    assert email_helpers.resolve_compose_upload(item_id, "alice") is not None
    assert email_helpers.resolve_compose_upload(item_id, "bob") is None


# --- the legacy bridge, deliberately kept ------------------------------------

def test_pre_registry_flat_tokens_still_resolve(staging):
    """`routes/document/document_routes.py` still writes `<uuid>_<name>` files
    straight into the staging root and is outside this change, so the old
    ownerless shape has to keep working. Nothing here produces one any more -
    this pins the bridge so its removal is a deliberate act."""
    legacy = staging.compose / "deadbeef_signed.pdf"
    legacy.write_bytes(b"%PDF legacy")

    outer = MIMEMultipart("mixed")
    email_helpers._attach_compose_uploads(outer, [legacy.name], owner="alice")
    parts = outer.get_payload()
    assert len(parts) == 1
    assert parts[0].get_filename() == "signed.pdf"

    # New staging never creates one: every id is 32 hex with no name in it.
    row = email_helpers.register_compose_upload("alice", "x.txt", content=b"x")
    assert "_" not in row["id"]
