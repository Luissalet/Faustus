"""SEC-1 · B-009 and B-020: secrets must not reach logs or world-readable files.

Sentinel-based, as the audit's minimum test matrix demands: a value that could
only come from the credential itself is planted, and every place it could
surface — the log records, the formatted log line, the file on disk — is then
inspected. Asserting "the code looks careful" is not a test; asserting the
sentinel is absent is.
"""

import asyncio
import io
import json
import logging
import os
import subprocess
import sys

import pytest

import routes.mcp.mcp_routes as mcp_routes
from core.atomic_io import atomic_write_json, atomic_write_text
from core.database import McpServer
from core.log_safety import (
    SecretRedactingFormatter,
    harden_handler,
    install_secret_redaction,
    redact_secrets,
)
from core.platform_compat import IS_WINDOWS
from src.mcp_manager import McpManager

SENTINEL = "GOCSPX-sec1-sentinel-do-not-log"


# ── the redaction itself ──────────────────────────────────────────────────

@pytest.mark.parametrize("line", [
    'oauth_file=\'{"client_secret": "%s", "client_id": "ok"}\'' % SENTINEL,
    "client_secret=%s&client_id=ok" % SENTINEL,
    "Authorization: Bearer %s" % SENTINEL,
    "headers={'authorization': 'Bearer %s'}" % SENTINEL,
    "https://host/v1?api_key=%s&model=gpt" % SENTINEL,
    "recovery_code: %s" % SENTINEL,
    "password='%s'" % SENTINEL,
    'HF_TOKEN="%s"' % SENTINEL,
])
def test_redact_secrets_removes_the_value(line):
    out = redact_secrets(line)
    assert SENTINEL not in out
    assert "***" in out


def test_redact_secrets_keeps_ordinary_numbers():
    # A blunt rule on the word "token" would eat these and make logs useless.
    line = "token_count=812 prompt_tokens=44 completion_tokens=91"
    assert redact_secrets(line) == line


def test_redact_secrets_survives_none_and_empty():
    assert redact_secrets(None) == ""
    assert redact_secrets("") == ""


# ── the handler, which is what actually writes app.log ────────────────────

def _hardened_stream_logger(name):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    harden_handler(handler)
    logger = logging.getLogger(name)
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    return logger, stream


def test_a_hardened_handler_redacts_an_interpolated_secret():
    logger, stream = _hardened_stream_logger("sec1.test.msg")
    logger.info(f'oauth payload: {{"client_secret": "{SENTINEL}"}}')
    assert SENTINEL not in stream.getvalue()


def test_a_hardened_handler_redacts_a_traceback_message():
    """The filter cannot see `exc_text`; the formatter wrapper must."""
    logger, stream = _hardened_stream_logger("sec1.test.exc")
    try:
        raise RuntimeError(f"token exchange failed: client_secret={SENTINEL}")
    except RuntimeError:
        logger.exception("OAuth callback error")
    text = stream.getvalue()
    assert "Traceback" in text
    assert SENTINEL not in text


def test_harden_handler_is_idempotent():
    handler = logging.StreamHandler(io.StringIO())
    harden_handler(handler)
    harden_handler(handler)
    assert len(handler.filters) == 1
    assert isinstance(handler.formatter, SecretRedactingFormatter)
    assert not isinstance(handler.formatter.inner, SecretRedactingFormatter)


def test_install_secret_redaction_hardens_the_root_handlers():
    root = logging.getLogger()
    handler = logging.StreamHandler(io.StringIO())
    root.addHandler(handler)
    try:
        assert install_secret_redaction() >= 1
        assert any(f.__class__.__name__ == "SecretRedactingFilter" for f in handler.filters)
    finally:
        root.removeHandler(handler)


# ── private files on disk ─────────────────────────────────────────────────

def _acl_lines(path):
    out = subprocess.run(["icacls", str(path)], capture_output=True, text=True)
    return out.stdout


def assert_owner_only(path):
    """The file must not be readable by anyone but the account that wrote it."""
    if IS_WINDOWS:
        acl = _acl_lines(path)
        # `(I)` marks an inherited ACE: its presence means the file still
        # answers to whatever the parent directory grants (Administrators,
        # SYSTEM, another user of the same machine).
        assert "(I)" not in acl, acl
        assert os.environ.get("USERNAME", "?").lower() in acl.lower(), acl
    else:
        assert os.stat(path).st_mode & 0o077 == 0


def test_private_json_write_is_owner_only(tmp_path):
    path = tmp_path / "creds.json"
    atomic_write_json(str(path), {"client_secret": SENTINEL}, indent=2, private=True)
    assert json.loads(path.read_text(encoding="utf-8"))["client_secret"] == SENTINEL
    assert_owner_only(path)


def test_private_text_write_is_owner_only(tmp_path):
    path = tmp_path / "creds.txt"
    atomic_write_text(str(path), SENTINEL, private=True)
    assert_owner_only(path)


def test_private_write_leaves_no_temp_file_behind(tmp_path):
    path = tmp_path / "creds.json"
    atomic_write_json(str(path), {"a": 1}, private=True)
    assert [p.name for p in tmp_path.iterdir()] == ["creds.json"]


def test_private_write_replaces_a_previously_public_file(tmp_path):
    """The upgrade path: the file already exists with loose permissions."""
    path = tmp_path / "creds.json"
    path.write_text('{"old": true}', encoding="utf-8")
    atomic_write_json(str(path), {"client_secret": SENTINEL}, private=True)
    assert_owner_only(path)


# ── the route that leaked it (B-009) ──────────────────────────────────────

@pytest.fixture
def oauth_dir(tmp_path, monkeypatch):
    d = tmp_path / "data" / "mcp_oauth"
    monkeypatch.setattr(mcp_routes, "MCP_OAUTH_DIR", str(d))
    return d


@pytest.fixture
def mcp_db(tmp_path, monkeypatch):
    import sqlalchemy
    from sqlalchemy.orm import sessionmaker

    engine = sqlalchemy.create_engine(f"sqlite:///{tmp_path / 'mcp.db'}")
    McpServer.__table__.create(bind=engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(mcp_routes, "SessionLocal", Session)
    try:
        yield Session
    finally:
        engine.dispose()


@pytest.fixture
def add_server(monkeypatch, mcp_db, oauth_dir):
    """The POST /api/mcp/servers endpoint with connection stubbed out."""
    manager = McpManager()

    async def fake_connect(**kwargs):
        return True

    monkeypatch.setattr(manager, "connect_server", fake_connect)
    monkeypatch.setattr(mcp_routes, "require_admin", lambda request: None)
    router = mcp_routes.setup_mcp_routes(manager)
    # `router` is module-level, so every call to setup_mcp_routes appends
    # another copy of the routes (B-006, route factories accumulate global
    # state). The last registration is the one closed over *this* manager;
    # picking the first one silently runs against a previous test's manager,
    # whose stub monkeypatch has already been undone — and then the real
    # connect_server tries to spawn `npx` and the test hangs forever.
    endpoint = None
    for route in router.routes:
        if route.path.endswith("/servers") and "POST" in (getattr(route, "methods", ()) or ()):
            endpoint = route.endpoint
    if endpoint is None:
        raise AssertionError("POST /servers not found")
    return endpoint


def _register_gmail_server(add_server):
    payload = json.dumps({
        "dir": "gmail",
        "filename": "gcp-oauth.keys.json",
        "client_id": "1234.apps.googleusercontent.com",
        "client_secret": SENTINEL,
    })
    return asyncio.run(add_server(
        request=object(), name="Gmail", transport="stdio", command="npx",
        args="[]", env="{}", url=None, oauth_file=payload,
        oauth_config=None, inherit_env=None,
    ))


def test_add_server_never_logs_the_oauth_payload(add_server, caplog):
    with caplog.at_level(logging.DEBUG):
        body = _register_gmail_server(add_server)
    assert body["id"]
    # caplog sees the raw records, before any handler redaction — which is the
    # point: the route must not put the secret into a record at all. Relying on
    # the redaction filter here would make the test pass for the wrong reason.
    for record in caplog.records:
        assert SENTINEL not in record.getMessage()
        assert SENTINEL not in str(record.args or "")
    assert SENTINEL not in caplog.text


def test_add_server_writes_the_credentials_owner_only(add_server, oauth_dir):
    _register_gmail_server(add_server)
    creds = oauth_dir / "gmail" / "gcp-oauth.keys.json"
    assert creds.exists()
    assert json.loads(creds.read_text(encoding="utf-8"))["installed"]["client_secret"] == SENTINEL
    assert_owner_only(creds)


def test_add_server_keeps_the_secret_out_of_the_server_env(add_server, mcp_db):
    """The credentials go to the file; the env must not carry a copy."""
    body = _register_gmail_server(add_server)
    session = mcp_db()
    try:
        row = session.query(McpServer).filter(McpServer.id == body["id"]).first()
        assert SENTINEL not in (row.env or "")
    finally:
        session.close()
