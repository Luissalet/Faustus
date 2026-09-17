"""src/push.py and routes/push_routes.py — Web Push (lot P-A).

Covers, in order:
- RFC 8291 message encryption against the RFC's own Appendix A worked
  example (byte-for-byte, salt and ephemeral key injected).
- RFC 8292 VAPID header shape (JWT decode, `aud`/`exp`/`sub`, ES256
  signature verifies against the published public key).
- Subscription persistence: subscribe/dedupe-by-endpoint/unsubscribe/list.
- `send()` against a mocked httpx: 404/410 drops the subscription,
  201 records success.
- The bus -> push mapping (`event_to_push_payload`) for every event kind
  `src/notifications.py` emits.
- The routes: auth (bearer vs cookie, same shape as
  `routes/mobile_routes.py`), vapid-key/subscribe/unsubscribe/subscriptions/
  test.
"""
from __future__ import annotations

import base64
import json
import time

import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)

from src import notifications as N
from src import push


def _b64u(s: str) -> bytes:
    s = s.strip()
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _b64u_enc(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture()
def isolated_push(tmp_path, monkeypatch):
    """Fresh on-disk push store + a fresh VAPID keypair per test, and a
    clean notification bus (this module registers a sink on it)."""
    push_dir = tmp_path / "push"
    monkeypatch.setattr(push, "PUSH_DIR", str(push_dir))
    monkeypatch.setattr(push, "VAPID_FILE", str(push_dir / "vapid.json"))
    monkeypatch.setattr(push, "SUBSCRIPTIONS_FILE", str(push_dir / "subscriptions.json"))
    push._vapid_cache = None
    push._store_cache = None
    push._bus_started = False

    monkeypatch.setattr(N, "NOTIFICATIONS_FILE", str(tmp_path / "notifications.jsonl"))
    N._ring.clear()
    N._subscribers.clear()
    N._sinks.clear()
    N._next_id = 1
    yield
    push._vapid_cache = None
    push._store_cache = None
    push._bus_started = False
    N._ring.clear()
    N._subscribers.clear()
    N._sinks.clear()
    N._next_id = 1


# --------------------------------------------------------------------------- #
# RFC 8291 — the Appendix A worked example, verbatim (whitespace in the RFC
# text removed; each value cross-checked to decode to the byte length the
# RFC states before being used here).
# --------------------------------------------------------------------------- #

RFC8291_PLAINTEXT_B64 = "V2hlbiBJIGdyb3cgdXAsIEkgd2FudCB0byBiZSBhIHdhdGVybWVsb24"
RFC8291_AS_PUBLIC = (
    "BP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8"
)
RFC8291_AS_PRIVATE = "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"
RFC8291_UA_PUBLIC = (
    "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
)
RFC8291_UA_PRIVATE = "q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94"
RFC8291_SALT = "DGv6ra1nlYgDCS1FRnbzlw"
RFC8291_AUTH_SECRET = "BTBZMqHH6r4Tts7J_aSIgg"
RFC8291_HEADER = (
    "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml"
    "mlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8"
)
RFC8291_CIPHERTEXT = (
    "8pfeW0KbunFT06SuDKoJH9Ql87S1QUrdirN6GcG7sFz1y1sqLgVi1VhjVkHsUoEsbI_0LpXMuGvnzQ"
)


def test_rfc8291_appendix_a_vector_matches_byte_for_byte():
    plaintext = _b64u(RFC8291_PLAINTEXT_B64)
    assert plaintext == b"When I grow up, I want to be a watermelon"

    as_private_int = int.from_bytes(_b64u(RFC8291_AS_PRIVATE), "big")
    ephemeral_private_key = ec.derive_private_key(as_private_int, ec.SECP256R1(), default_backend())

    salt = _b64u(RFC8291_SALT)
    out = push.encrypt_payload(
        plaintext,
        p256dh=RFC8291_UA_PUBLIC,
        auth=RFC8291_AUTH_SECRET,
        salt=salt,
        ephemeral_private_key=ephemeral_private_key,
    )

    expected = _b64u(RFC8291_HEADER) + _b64u(RFC8291_CIPHERTEXT)
    assert out == expected
    # And the header alone matches the RFC's stated 86 octets, containing
    # the salt, a 4-byte big-endian record size of 4096, a 1-byte key id
    # length, and the 65-byte uncompressed application server public key.
    assert len(_b64u(RFC8291_HEADER)) == 86
    assert out[:16] == salt
    assert int.from_bytes(out[16:20], "big") == push.RECORD_SIZE
    assert out[20] == 65
    assert out[21:86] == _b64u(RFC8291_AS_PUBLIC)


def test_encrypt_payload_defaults_produce_decryptable_random_output(isolated_push):
    """Without an injected salt/key (the real send() path), output still
    round-trips: same structural shape, different random bytes each call."""
    receiver_key = ec.generate_private_key(ec.SECP256R1(), default_backend())
    receiver_public_raw = receiver_key.public_key().public_bytes(
        __import__("cryptography.hazmat.primitives.serialization", fromlist=["Encoding"]).Encoding.X962,
        __import__("cryptography.hazmat.primitives.serialization", fromlist=["PublicFormat"]).PublicFormat.UncompressedPoint,
    )
    auth = base64.urlsafe_b64encode(b"0" * 16).rstrip(b"=").decode()
    out1 = push.encrypt_payload(b"hi", p256dh=_b64u_enc(receiver_public_raw), auth=auth)
    out2 = push.encrypt_payload(b"hi", p256dh=_b64u_enc(receiver_public_raw), auth=auth)
    assert out1 != out2  # fresh salt/ephemeral key each call
    # header (16 salt + 4 rs + 1 keyid-len + 65 keyid) + (2-byte plaintext +
    # 1-byte delimiter + 16-byte GCM tag).
    assert len(out1) == 86 + (2 + 1 + 16) == len(out2)


# --------------------------------------------------------------------------- #
# RFC 8292 — VAPID
# --------------------------------------------------------------------------- #

def test_vapid_public_key_is_a_valid_uncompressed_p256_point(isolated_push):
    key = push.vapid_public_key()
    raw = _b64u(key)
    assert len(raw) == 65
    assert raw[0] == 0x04
    # Round-trips through the curve without raising.
    ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
    # Stable across calls (persisted, not regenerated per call).
    assert push.vapid_public_key() == key


def test_vapid_headers_shape_aud_exp_sub_and_signature(isolated_push):
    headers = push.vapid_headers("https://push.example.com/abc123", ttl_seconds=3600)
    auth = headers["Authorization"]
    assert auth.startswith("vapid t=")
    assert ", k=" in auth
    token_part = auth.split("t=", 1)[1].split(", k=")[0]
    key_part = auth.split(", k=")[1]
    assert key_part == push.vapid_public_key()

    header_b64, claims_b64, sig_b64 = token_part.split(".")
    header = json.loads(_b64u(header_b64))
    claims = json.loads(_b64u(claims_b64))
    assert header == {"typ": "JWT", "alg": "ES256"}
    assert claims["aud"] == "https://push.example.com"
    assert claims["sub"] == "mailto:faustus@localhost"
    now = int(time.time())
    assert now < claims["exp"] <= now + 3600 + 5

    # Verify the ES256 signature against the published public key.
    raw_sig = _b64u(sig_b64)
    assert len(raw_sig) == 64
    r = int.from_bytes(raw_sig[:32], "big")
    s = int.from_bytes(raw_sig[32:], "big")
    der_sig = encode_dss_signature(r, s)
    pub_raw = _b64u(push.vapid_public_key())
    pub_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), pub_raw)
    pub_key.verify(der_sig, f"{header_b64}.{claims_b64}".encode("ascii"), ec.ECDSA(hashes.SHA256()))


def test_vapid_exp_is_capped_at_24_hours(isolated_push):
    headers = push.vapid_headers("https://push.example.com/x", ttl_seconds=999999)
    token_part = headers["Authorization"].split("t=", 1)[1].split(", k=")[0]
    _, claims_b64, _ = token_part.split(".")
    claims = json.loads(_b64u(claims_b64))
    now = int(time.time())
    assert claims["exp"] <= now + 24 * 3600 + 5


def test_vapid_contact_setting_is_used(isolated_push, monkeypatch):
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: (
        "luis@example.com" if key == "push_contact" else default
    ))
    headers = push.vapid_headers("https://push.example.com/x")
    token_part = headers["Authorization"].split("t=", 1)[1].split(", k=")[0]
    _, claims_b64, _ = token_part.split(".")
    claims = json.loads(_b64u(claims_b64))
    assert claims["sub"] == "mailto:luis@example.com"


# --------------------------------------------------------------------------- #
# Subscriptions: subscribe / dedupe / unsubscribe / list, persisted
# --------------------------------------------------------------------------- #

def _valid_p256dh() -> str:
    """A real base64url-encoded uncompressed P-256 point — a placeholder
    string of the right length is not a valid EC point and `encrypt_payload`
    rightly rejects it, so tests that actually call `send()` need a real
    one."""
    from cryptography.hazmat.primitives import serialization as _ser
    key = ec.generate_private_key(ec.SECP256R1(), default_backend())
    raw = key.public_key().public_bytes(_ser.Encoding.X962, _ser.PublicFormat.UncompressedPoint)
    return _b64u_enc(raw)


def _valid_auth() -> str:
    return _b64u_enc(b"0123456789abcdef")


def _sub(endpoint="https://push.example.com/ep1", p256dh=None, auth=None):
    return {
        "endpoint": endpoint,
        "keys": {"p256dh": p256dh or _valid_p256dh(), "auth": auth or _valid_auth()},
    }


def test_subscribe_persists_and_round_trips(isolated_push):
    row = push.subscribe("luis", _sub(), device_name="Pixel 8")
    assert row["owner"] == "luis"
    assert row["device_name"] == "Pixel 8"
    assert row["failures"] == 0
    assert row["id"]

    push._store_cache = None  # force a re-read from disk
    rows = push.list_subscriptions("luis")
    assert len(rows) == 1
    assert rows[0]["endpoint"] == "https://push.example.com/ep1"


def test_subscribe_same_endpoint_dedupes_not_duplicates(isolated_push):
    push.subscribe("luis", _sub(), device_name="Phone A")
    push.subscribe("luis", _sub(p256dh="q" * 87), device_name="Phone A renamed")
    rows = push.list_subscriptions("luis")
    assert len(rows) == 1
    assert rows[0]["keys"]["p256dh"] == "q" * 87
    assert rows[0]["device_name"] == "Phone A renamed"


def test_subscribe_different_owners_do_not_collide(isolated_push):
    push.subscribe("luis", _sub())
    push.subscribe("other", _sub())
    assert len(push.list_subscriptions("luis")) == 1
    assert len(push.list_subscriptions("other")) == 1


def test_subscribe_rejects_missing_keys(isolated_push):
    with pytest.raises(ValueError):
        push.subscribe("luis", {"endpoint": "https://push.example.com/x", "keys": {}})


def test_unsubscribe_by_endpoint_removes_it(isolated_push):
    push.subscribe("luis", _sub())
    assert push.unsubscribe("luis", "https://push.example.com/ep1") is True
    assert push.list_subscriptions("luis") == []
    assert push.unsubscribe("luis", "https://push.example.com/ep1") is False


def test_unsubscribe_by_id_removes_it(isolated_push):
    row = push.subscribe("luis", _sub())
    assert push.unsubscribe("luis", row["id"]) is True
    assert push.list_subscriptions("luis") == []


def test_unsubscribe_does_not_touch_another_owners_subscription(isolated_push):
    push.subscribe("luis", _sub())
    assert push.unsubscribe("other", "https://push.example.com/ep1") is False
    assert len(push.list_subscriptions("luis")) == 1


# --------------------------------------------------------------------------- #
# send() against a mocked httpx
# --------------------------------------------------------------------------- #

class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


def test_send_success_records_last_ok(isolated_push, monkeypatch):
    row = push.subscribe("luis", _sub())

    def fake_post(url, content=None, headers=None, timeout=None):
        assert url == row["endpoint"]
        assert headers["Content-Encoding"] == "aes128gcm"
        assert headers["Authorization"].startswith("vapid t=")
        return _FakeResponse(201)

    monkeypatch.setattr("httpx.post", fake_post)
    result = push.send(row, {"title": "hi", "body": "there"})
    assert result == {"ok": True, "status": 201}

    push._store_cache = None
    stored = push.list_subscriptions("luis")[0]
    assert stored["last_ok"] is not None
    assert stored["failures"] == 0


def test_send_404_drops_the_subscription(isolated_push, monkeypatch):
    row = push.subscribe("luis", _sub())
    monkeypatch.setattr("httpx.post", lambda *a, **kw: _FakeResponse(404))
    result = push.send(row, {"title": "hi"})
    assert result == {"ok": False, "status": 404}
    assert push.list_subscriptions("luis") == []


def test_send_410_drops_the_subscription(isolated_push, monkeypatch):
    row = push.subscribe("luis", _sub())
    monkeypatch.setattr("httpx.post", lambda *a, **kw: _FakeResponse(410))
    push.send(row, {"title": "hi"})
    assert push.list_subscriptions("luis") == []


def test_send_429_counts_failure_but_keeps_subscription(isolated_push, monkeypatch):
    row = push.subscribe("luis", _sub())
    monkeypatch.setattr("httpx.post", lambda *a, **kw: _FakeResponse(429))
    result = push.send(row, {"title": "hi"})
    assert result == {"ok": False, "status": 429}
    push._store_cache = None
    stored = push.list_subscriptions("luis")[0]
    assert stored["failures"] == 1


def test_send_drops_after_max_failures(isolated_push, monkeypatch):
    row = push.subscribe("luis", _sub())
    monkeypatch.setattr("httpx.post", lambda *a, **kw: _FakeResponse(500))
    for _ in range(push.MAX_FAILURES):
        push.send(row, {"title": "hi"})
    assert push.list_subscriptions("luis") == []


def test_broadcast_sends_to_every_subscription(isolated_push, monkeypatch):
    push.subscribe("luis", _sub(endpoint="https://push.example.com/a"))
    push.subscribe("luis", _sub(endpoint="https://push.example.com/b"))
    calls = []

    def fake_post(url, content=None, headers=None, timeout=None):
        calls.append(url)
        return _FakeResponse(201)

    monkeypatch.setattr("httpx.post", fake_post)
    results = push.broadcast("luis", {"title": "hi"})
    assert len(results) == 2
    assert all(r["ok"] for r in results)
    assert set(calls) == {"https://push.example.com/a", "https://push.example.com/b"}


# --------------------------------------------------------------------------- #
# Bus -> push payload mapping
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("kind,session_id,expected_url", [
    ("turn_finished", "s1", "/studio?s=s1"),
    ("turn_finished", None, "/studio"),
    ("turn_error", "s2", "/studio?s=s2"),
    ("approval_pending", "s3", "/studio?s=s3"),
    ("approval_pending", None, "/studio"),
    ("approval_resolved", "s4", "/studio?s=s4"),
    ("task_finished", None, "/"),
    ("reminder", None, "/notes"),
])
def test_event_to_push_payload_url_mapping(kind, session_id, expected_url):
    event = {
        "id": 1, "kind": kind, "owner": "luis", "title": "T", "body": "B",
        "session_id": session_id, "approval_id": None, "ts": time.time(),
    }
    payload = push.event_to_push_payload(event)
    assert payload == {"title": "T", "body": "B", "url": expected_url, "kind": kind, "id": 1}


def test_bus_sink_broadcasts_to_subscribed_owner(isolated_push, monkeypatch):
    push.subscribe("luis", _sub())
    push.start()

    sent = []
    monkeypatch.setattr(push, "broadcast", lambda owner, payload, urgency="normal": sent.append((owner, payload, urgency)))

    N.emit("approval_pending", owner="luis", title="publish", body="do it", session_id="s9")
    assert len(sent) == 1
    owner, payload, urgency = sent[0]
    assert owner == "luis"
    assert payload["url"] == "/studio?s=s9"
    assert urgency == "high"


def test_bus_sink_ignores_events_for_owners_with_no_subscription(isolated_push, monkeypatch):
    push.start()
    sent = []
    monkeypatch.setattr(push, "broadcast", lambda *a, **kw: sent.append(a))
    N.emit("turn_finished", owner="nobody-subscribed", title="t", body="b")
    assert sent == []


def test_bus_sink_respects_push_enabled_false(isolated_push, monkeypatch):
    push.subscribe("luis", _sub())
    push.start()
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: (
        False if key == "push_enabled" else default
    ))
    sent = []
    monkeypatch.setattr(push, "broadcast", lambda *a, **kw: sent.append(a))
    N.emit("reminder", owner="luis", title="t", body="b")
    assert sent == []


def test_start_is_idempotent(isolated_push):
    push.start()
    push.start()
    assert N._sinks.count(push._push_sink) == 1
    push.stop()
    assert push._push_sink not in N._sinks


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@pytest.fixture()
def route_client(isolated_push, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core import middleware
    from routes.push_routes import setup_push_routes

    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)

    app = FastAPI()

    @app.middleware("http")
    async def _stamp(request, call_next):
        bearer_owner = request.headers.get("X-Test-Bearer-Owner")
        if bearer_owner:
            request.state.api_token = True
            request.state.api_token_owner = bearer_owner
        else:
            request.state.api_token = False
            request.state.current_user = request.headers.get("X-Test-User", "luis")
        return await call_next(request)

    app.include_router(setup_push_routes())
    return TestClient(app)


def test_vapid_key_route_bearer(route_client):
    resp = route_client.get("/api/push/vapid-key", headers={"X-Test-Bearer-Owner": "luis"})
    assert resp.status_code == 200
    assert resp.json()["key"] == push.vapid_public_key()


def test_vapid_key_route_cookie(route_client):
    resp = route_client.get("/api/push/vapid-key", headers={"X-Test-User": "luis"})
    assert resp.status_code == 200


def test_subscribe_unsubscribe_subscriptions_route_roundtrip(route_client):
    headers = {"X-Test-Bearer-Owner": "luis"}
    body = {
        "subscription": {
            "endpoint": "https://push.example.com/route1",
            "keys": {"p256dh": "p" * 87, "auth": "a" * 22},
        },
        "device_name": "Test Phone",
    }
    resp = route_client.post("/api/push/subscribe", json=body, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    resp = route_client.get("/api/push/subscriptions", headers=headers)
    assert resp.status_code == 200
    subs = resp.json()["subscriptions"]
    assert len(subs) == 1
    assert subs[0]["device_name"] == "Test Phone"

    resp = route_client.post(
        "/api/push/unsubscribe", json={"endpoint": "https://push.example.com/route1"}, headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["removed"] is True

    resp = route_client.get("/api/push/subscriptions", headers=headers)
    assert resp.json()["subscriptions"] == []


def test_subscribe_route_another_owner_cannot_see_it(route_client):
    body = {
        "subscription": {
            "endpoint": "https://push.example.com/route2",
            "keys": {"p256dh": "p" * 87, "auth": "a" * 22},
        },
        "device_name": "",
    }
    route_client.post("/api/push/subscribe", json=body, headers={"X-Test-Bearer-Owner": "luis"})
    resp = route_client.get("/api/push/subscriptions", headers={"X-Test-Bearer-Owner": "other"})
    assert resp.json()["subscriptions"] == []


def test_test_route_sends_to_callers_subscriptions(route_client, monkeypatch):
    headers = {"X-Test-Bearer-Owner": "luis"}
    body = {
        "subscription": {
            "endpoint": "https://push.example.com/route3",
            "keys": {"p256dh": _valid_p256dh(), "auth": _valid_auth()},
        },
        "device_name": "",
    }
    route_client.post("/api/push/subscribe", json=body, headers=headers)

    monkeypatch.setattr(
        "httpx.post", lambda url, content=None, headers=None, timeout=None: _FakeResponse(201)
    )
    resp = route_client.post("/api/push/test", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert len(data["results"]) == 1
    assert data["results"][0]["ok"] is True
