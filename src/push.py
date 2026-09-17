"""src/push.py — Web Push on top of the notification bus (`src/notifications.py`).

Sends real browser/OS push notifications (the kind that show up even when
Studio's tab isn't open, once the PWA's service worker registered a
`PushSubscription`) for the events `src/notifications.py` already emits:
`turn_finished`, `turn_error`, `approval_pending`, `approval_resolved`,
`task_finished`, `reminder`.

Deliberately built without `pywebpush`/`http-ece` — their wheels do not
build in this environment. Everything here is RFC 8291 (message encryption)
and RFC 8292 (VAPID) implemented directly on top of `cryptography`, which is
already a hard requirement (`requirements.txt`).

## RFC 8291 — message encryption ("aes128gcm")

Given the subscription's `p256dh` (the browser's P-256 public key) and
`auth` (a 16-byte secret), and a fresh ephemeral P-256 keypair + random
16-byte salt generated per message:

1. `ecdh_secret = ECDH(ephemeral_private, subscriber_public)`.
2. `prk_key = HMAC-SHA256(key=auth_secret, msg=ecdh_secret)` — the "auth"
   extraction step ("WebPush: info" key-combining HKDF from the RFC).
3. `key_info = b"WebPush: info\\x00" + ua_public_raw + ephemeral_public_raw`
   (both are the 65-byte uncompressed points), `ikm = HKDF-Expand(prk_key,
   key_info, 32)`.
4. `prk = HMAC-SHA256(key=salt, msg=ikm)` (the aes128gcm extraction step).
5. `cek = HKDF-Expand(prk, b"Content-Encoding: aes128gcm\\x00", 16)`.
6. `nonce = HKDF-Expand(prk, b"Content-Encoding: nonce\\x00", 12)`.
7. Plaintext is padded: the record body is `plaintext + 0x02 + zero padding`
   up to the record size (one record per push payload here — payloads are
   small enough to never need more than 4096 bytes, and multi-record
   aes128gcm is not implemented).
8. Ciphertext = `AES-128-GCM(cek, nonce, aad=b"", record)`.
9. Wire body = `salt(16) + rs(4, big-endian record size) + idlen(1) +
   ephemeral_public_raw(65) + ciphertext`. `Content-Encoding: aes128gcm`,
   no `Encryption`/`Crypto-Key` headers needed (those were the older
   `aesgcm` scheme).

## RFC 8292 — VAPID

`Authorization: vapid t=<JWT>, k=<base64url VAPID public key>`. The JWT is
a plain compact ES256 JWT — header `{"typ":"JWT","alg":"ES256"}`, claims
`{"aud": <push endpoint's origin>, "exp": <now + <=24h>, "sub":
"mailto:..."}`, signed with the server's VAPID private key. `cryptography`
produces a DER ECDSA signature; JWS wants the raw `r||s` (64 bytes for
P-256), so the DER signature is decoded and re-packed.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional

from cryptography.hazmat.primitives import hashes, hmac, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.backends import default_backend

from core.atomic_io import atomic_write_json
from core.file_lock import FileLock
from core.platform_compat import restrict_dir_to_owner
from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

PUSH_DIR = os.path.join(DATA_DIR, "push")
VAPID_FILE = os.path.join(PUSH_DIR, "vapid.json")
SUBSCRIPTIONS_FILE = os.path.join(PUSH_DIR, "subscriptions.json")

#: After this many consecutive send failures a subscription is dropped —
#: it is almost certainly dead (uninstalled PWA, revoked permission, a push
#: service that has given up on it) and endless retries just burn requests.
MAX_FAILURES = 10

#: 4096-byte records, the size RFC 8291 examples use and well above anything
#: a single notification payload here will ever need (title/body/url, a few
#: hundred bytes of JSON) — this module only ever emits one record.
RECORD_SIZE = 4096

_DEFAULT_CONTACT = "mailto:faustus@localhost"


# --------------------------------------------------------------------------- #
# VAPID keys
# --------------------------------------------------------------------------- #

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    s = s.strip()
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _ensure_push_dir() -> None:
    os.makedirs(PUSH_DIR, exist_ok=True)
    try:
        restrict_dir_to_owner(PUSH_DIR)
    except Exception:
        logger.debug("push: could not restrict %s", PUSH_DIR, exc_info=True)


def _generate_vapid_keys() -> Dict[str, str]:
    private_key = ec.generate_private_key(ec.SECP256R1(), default_backend())
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return {
        "private_key_pem": private_pem,
        "public_key": _b64url(public_raw),
        "created_at": time.time(),
    }


_vapid_cache: Optional[Dict[str, str]] = None


def _load_or_create_vapid() -> Dict[str, str]:
    """VAPID keys are generated once and persisted (0600) — a new keypair on
    every restart would invalidate every subscription a browser already
    holds (the browser pins `applicationServerKey` at subscribe time)."""
    global _vapid_cache
    if _vapid_cache is not None:
        return _vapid_cache
    _ensure_push_dir()
    with FileLock(VAPID_FILE + ".lock"):
        if os.path.exists(VAPID_FILE):
            try:
                with open(VAPID_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and data.get("private_key_pem") and data.get("public_key"):
                    _vapid_cache = data
                    return data
            except Exception:
                logger.warning("push: vapid.json unreadable, regenerating", exc_info=True)
        data = _generate_vapid_keys()
        atomic_write_json(VAPID_FILE, data, private=True)
        _vapid_cache = data
        return data


def _vapid_private_key() -> ec.EllipticCurvePrivateKey:
    data = _load_or_create_vapid()
    return serialization.load_pem_private_key(
        data["private_key_pem"].encode("ascii"), password=None, backend=default_backend()
    )


def vapid_public_key() -> str:
    """The VAPID public key as base64url of the uncompressed P-256 point —
    exactly what the browser's `PushManager.subscribe({applicationServerKey})`
    expects."""
    return _load_or_create_vapid()["public_key"]


def _vapid_contact() -> str:
    try:
        from src.settings import get_setting
        contact = (get_setting("push_contact", "") or "").strip()
    except Exception:
        contact = ""
    if not contact:
        return _DEFAULT_CONTACT
    return contact if contact.startswith("mailto:") else f"mailto:{contact}"


def _jwt_es256(claims: Dict[str, Any], private_key: ec.EllipticCurvePrivateKey) -> str:
    header = {"typ": "JWT", "alg": "ES256"}
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        + "."
        + _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    )
    der_sig = private_key.sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der_sig)
    # JWS ES256 wants the raw, fixed-width r||s concatenation (32 bytes each
    # for P-256), not the DER SEQUENCE `sign()` returns.
    raw_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return signing_input + "." + _b64url(raw_sig)


def _endpoint_origin(endpoint: str) -> str:
    from urllib.parse import urlsplit
    parts = urlsplit(endpoint)
    return f"{parts.scheme}://{parts.netloc}"


def vapid_headers(endpoint: str, *, ttl_seconds: int = 12 * 3600) -> Dict[str, str]:
    """`Authorization`/`Content-Encoding`-adjacent VAPID header for a push
    to `endpoint`. `ttl_seconds` is the JWT lifetime (<=24h per RFC 8292),
    unrelated to the `TTL` push header `send()` sets separately."""
    ttl_seconds = max(1, min(int(ttl_seconds), 24 * 3600))
    now = int(time.time())
    claims = {
        "aud": _endpoint_origin(endpoint),
        "exp": now + ttl_seconds,
        "sub": _vapid_contact(),
    }
    token = _jwt_es256(claims, _vapid_private_key())
    key = vapid_public_key()
    return {"Authorization": f"vapid t={token}, k={key}"}


# --------------------------------------------------------------------------- #
# RFC 8291 message encryption
# --------------------------------------------------------------------------- #

def _hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    h = hmac.HMAC(salt, hashes.SHA256(), backend=default_backend())
    h.update(ikm)
    return h.finalize()


def _hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    # Single-block expand (T(1) = HMAC(PRK, info || 0x01)) — every key this
    # module derives is <=32 bytes, always within one HMAC-SHA256 block.
    h = hmac.HMAC(prk, hashes.SHA256(), backend=default_backend())
    h.update(info + b"\x01")
    return h.finalize()[:length]


def encrypt_payload(
    plaintext: bytes,
    *,
    p256dh: str,
    auth: str,
    salt: Optional[bytes] = None,
    ephemeral_private_key: Optional[ec.EllipticCurvePrivateKey] = None,
    record_size: int = RECORD_SIZE,
) -> bytes:
    """RFC 8291 `aes128gcm` encryption. Returns the full wire body (salt +
    record size + key id length + key id + ciphertext) ready to POST as-is.

    `salt`/`ephemeral_private_key` are only ever passed explicitly by the
    RFC 8291 Appendix A test vector in the tests — real sends always
    generate fresh random values (the default when omitted).
    """
    ua_public_raw = _b64url_decode(p256dh)
    auth_secret = _b64url_decode(auth)
    if len(ua_public_raw) != 65:
        raise ValueError("p256dh must be an uncompressed P-256 point (65 bytes)")
    if len(auth_secret) != 16:
        raise ValueError("auth secret must be 16 bytes")

    if salt is None:
        salt = os.urandom(16)
    if ephemeral_private_key is None:
        ephemeral_private_key = ec.generate_private_key(ec.SECP256R1(), default_backend())

    ephemeral_public_raw = ephemeral_private_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    ua_public_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public_raw)
    ecdh_secret = ephemeral_private_key.exchange(ec.ECDH(), ua_public_key)

    # "auth" extraction (RFC 8291 §3.1): combine the shared secret with the
    # subscription's auth secret before deriving the actual encryption PRK.
    prk_key = _hkdf_extract(auth_secret, ecdh_secret)
    key_info = b"WebPush: info\x00" + ua_public_raw + ephemeral_public_raw
    ikm = _hkdf_expand(prk_key, key_info, 32)

    prk = _hkdf_extract(salt, ikm)
    cek = _hkdf_expand(prk, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf_expand(prk, b"Content-Encoding: nonce\x00", 12)

    # Single-record aes128gcm: plaintext + delimiter (0x02: last/only
    # record). `record_size` (the `rs` field in the header) only declares
    # the *maximum* size any record in the stream may reach — RFC 8291's
    # own Appendix A worked example encrypts a 41-byte message under
    # `rs=4096` and produces a 58-byte record (41 + 1-byte delimiter +
    # 16-byte GCM tag), not one padded out to 4096. Extra zero padding
    # between the delimiter and the record's natural end is legal (RFC
    # 8188 §2) but optional, and *not* added here to match that vector
    # byte-for-byte; nothing about push payload confidentiality depends on
    # fixed-size records the way it might for, say, traffic analysis on a
    # long-lived stream.
    max_plain = record_size - 16 - 1
    if len(plaintext) > max_plain:
        raise ValueError(f"payload too large for a single aes128gcm record ({len(plaintext)} > {max_plain})")
    padded = plaintext + b"\x02"

    ciphertext = AESGCM(cek).encrypt(nonce, padded, b"")

    header = (
        salt
        + record_size.to_bytes(4, "big")
        + len(ephemeral_public_raw).to_bytes(1, "big")
        + ephemeral_public_raw
    )
    return header + ciphertext


# --------------------------------------------------------------------------- #
# Subscription storage
# --------------------------------------------------------------------------- #

_store_cache: Optional[List[Dict[str, Any]]] = None


def _load_subscriptions() -> List[Dict[str, Any]]:
    global _store_cache
    if _store_cache is not None:
        return _store_cache
    _ensure_push_dir()
    try:
        with open(SUBSCRIPTIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        rows = data.get("subscriptions") if isinstance(data, dict) else None
        _store_cache = rows if isinstance(rows, list) else []
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        _store_cache = []
    return _store_cache


def _save_subscriptions(rows: List[Dict[str, Any]]) -> None:
    global _store_cache
    _ensure_push_dir()
    atomic_write_json(SUBSCRIPTIONS_FILE, {"subscriptions": rows}, private=True)
    _store_cache = rows


def _reload_subscriptions_from_disk() -> List[Dict[str, Any]]:
    global _store_cache
    _store_cache = None
    return _load_subscriptions()


def subscribe(owner: str, subscription: Dict[str, Any], device_name: str = "") -> Dict[str, Any]:
    """Register (or update) a browser's `PushSubscription`. Deduped by
    `endpoint` — resubscribing (a refreshed key, a re-registered SW) updates
    the existing row in place rather than accumulating dead duplicates."""
    endpoint = (subscription or {}).get("endpoint")
    keys = (subscription or {}).get("keys") or {}
    if not endpoint or not isinstance(endpoint, str):
        raise ValueError("subscription.endpoint is required")
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")
    if not p256dh or not auth:
        raise ValueError("subscription.keys.p256dh and .auth are required")

    with FileLock(SUBSCRIPTIONS_FILE + ".lock"):
        rows = _reload_subscriptions_from_disk()
        now = time.time()
        for row in rows:
            if row.get("owner") == owner and row.get("endpoint") == endpoint:
                row["keys"] = {"p256dh": p256dh, "auth": auth}
                row["device_name"] = device_name or row.get("device_name") or ""
                row["failures"] = 0
                _save_subscriptions(rows)
                return dict(row)
        row = {
            "id": uuid.uuid4().hex,
            "owner": owner,
            "endpoint": endpoint,
            "keys": {"p256dh": p256dh, "auth": auth},
            "device_name": device_name or "",
            "created_at": now,
            "last_ok": None,
            "failures": 0,
        }
        rows.append(row)
        _save_subscriptions(rows)
        return dict(row)


def unsubscribe(owner: str, endpoint_or_id: str) -> bool:
    """Remove a subscription by `endpoint` or by its `id`. Returns True if
    something was actually removed."""
    if not endpoint_or_id:
        return False
    with FileLock(SUBSCRIPTIONS_FILE + ".lock"):
        rows = _reload_subscriptions_from_disk()
        kept = [
            r for r in rows
            if not (r.get("owner") == owner and (r.get("endpoint") == endpoint_or_id or r.get("id") == endpoint_or_id))
        ]
        if len(kept) == len(rows):
            return False
        _save_subscriptions(kept)
        return True


def list_subscriptions(owner: str) -> List[Dict[str, Any]]:
    rows = _reload_subscriptions_from_disk()
    return [dict(r) for r in rows if r.get("owner") == owner]


def _drop_subscription(owner: str, endpoint: str) -> None:
    with FileLock(SUBSCRIPTIONS_FILE + ".lock"):
        rows = _reload_subscriptions_from_disk()
        kept = [r for r in rows if not (r.get("owner") == owner and r.get("endpoint") == endpoint)]
        if len(kept) != len(rows):
            _save_subscriptions(kept)


def _mark_result(owner: str, endpoint: str, *, ok: bool) -> None:
    with FileLock(SUBSCRIPTIONS_FILE + ".lock"):
        rows = _reload_subscriptions_from_disk()
        changed = False
        kept: List[Dict[str, Any]] = []
        for row in rows:
            if row.get("owner") == owner and row.get("endpoint") == endpoint:
                if ok:
                    row["last_ok"] = time.time()
                    row["failures"] = 0
                else:
                    row["failures"] = int(row.get("failures") or 0) + 1
                    if row["failures"] >= MAX_FAILURES:
                        changed = True
                        continue  # drop it
                changed = True
            kept.append(row)
        if changed:
            _save_subscriptions(kept)


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #

def send(subscription: Dict[str, Any], payload: Dict[str, Any], *, ttl: int = 3600,
         urgency: str = "high") -> Dict[str, Any]:
    """POST an encrypted push message to one subscription's push service.

    Returns `{"ok": bool, "status": <http status or "error">}`. On 404/410
    (the push service has permanently given up on this endpoint) or after
    `MAX_FAILURES` consecutive soft failures (429/5xx/network error), the
    subscription is dropped from the store — this is the *caller's*
    responsibility to invoke (via `_drop_subscription`/`_mark_result`), done
    here so a bare `send()` call against an arbitrary subscription dict
    (e.g. in tests) doesn't require a stored row to exist.
    """
    import httpx

    endpoint = subscription.get("endpoint")
    keys = subscription.get("keys") or {}
    owner = subscription.get("owner")
    body_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    try:
        wire = encrypt_payload(body_bytes, p256dh=keys.get("p256dh", ""), auth=keys.get("auth", ""))
    except Exception:
        logger.warning("push: encryption failed for endpoint=%s", endpoint, exc_info=True)
        return {"ok": False, "status": "encrypt_error"}

    headers = {
        "Content-Type": "application/octet-stream",
        "Content-Encoding": "aes128gcm",
        "TTL": str(max(0, int(ttl))),
        "Urgency": urgency if urgency in ("very-low", "low", "normal", "high") else "normal",
    }
    headers.update(vapid_headers(endpoint))

    try:
        # `trust_env=False` would strip the outbound HTTP(S)_PROXY this
        # environment routes internet-bound traffic through — a push
        # service (FCM/Mozilla autopush/...) is on the public internet, not
        # something to bypass the proxy for. Default env handling is kept.
        resp = httpx.post(endpoint, content=wire, headers=headers, timeout=10.0)
        status = resp.status_code
    except Exception:
        logger.warning("push: send failed for endpoint=%s", endpoint, exc_info=True)
        if owner:
            _mark_result(owner, endpoint, ok=False)
        return {"ok": False, "status": "error"}

    if status in (404, 410):
        if owner:
            _drop_subscription(owner, endpoint)
        return {"ok": False, "status": status}
    if status >= 400:
        if owner:
            _mark_result(owner, endpoint, ok=False)
        return {"ok": False, "status": status}

    if owner:
        _mark_result(owner, endpoint, ok=True)
    return {"ok": True, "status": status}


def broadcast(owner: str, payload: Dict[str, Any], *, ttl: int = 3600, urgency: str = "high") -> List[Dict[str, Any]]:
    """`send()` to every subscription `owner` has. Returns one result dict
    per subscription (with `id`/`device_name` mixed in so a caller can show
    per-device status)."""
    results = []
    for row in list_subscriptions(owner):
        result = send(row, payload, ttl=ttl, urgency=urgency)
        results.append({"id": row.get("id"), "device_name": row.get("device_name"), **result})
    return results


# --------------------------------------------------------------------------- #
# Bus subscriber — maps notification-bus events to push payloads
# --------------------------------------------------------------------------- #

_URL_BY_KIND = {
    "turn_finished": lambda e: f"/studio?s={e.get('session_id')}" if e.get("session_id") else "/studio",
    "turn_error": lambda e: f"/studio?s={e.get('session_id')}" if e.get("session_id") else "/studio",
    "approval_pending": lambda e: f"/studio?s={e.get('session_id')}" if e.get("session_id") else "/studio",
    "approval_resolved": lambda e: f"/studio?s={e.get('session_id')}" if e.get("session_id") else "/studio",
    "task_finished": lambda e: "/",
    "reminder": lambda e: "/notes",
}

_URGENT_KINDS = {"approval_pending"}


def event_to_push_payload(event: Dict[str, Any]) -> Dict[str, Any]:
    """Map one `notifications.py` event dict to the `{title, body, url,
    kind, id}` payload the service worker's `push` handler receives (see
    `docs/api/mobile.md` / the PWA service worker for the consumer side)."""
    kind = event.get("kind", "")
    url_fn = _URL_BY_KIND.get(kind, lambda e: "/")
    return {
        "title": event.get("title") or "Faustus",
        "body": event.get("body") or "",
        "url": url_fn(event),
        "kind": kind,
        "id": event.get("id"),
    }


_bus_task = None
_bus_started = False


def _push_sink(event: Dict[str, Any]) -> None:
    """Registered with `notifications.register_sink` — called synchronously,
    fire-and-forget, every time `notifications.emit()` records an event.
    Schedules the actual (network-bound) send onto the running event loop
    instead of blocking the emitter."""
    try:
        from src.settings import get_setting
        if not get_setting("push_enabled", True):
            return
    except Exception:
        pass

    owner = (event.get("owner") or "").strip()
    if not owner:
        return  # nothing to target — no ownerless push fan-out
    if not list_subscriptions(owner):
        return

    payload = event_to_push_payload(event)
    urgency = "high" if event.get("kind") in _URGENT_KINDS else "normal"

    async def _deliver():
        import asyncio
        await asyncio.to_thread(broadcast, owner, payload, urgency=urgency)

    try:
        import asyncio
        loop = asyncio.get_running_loop()
        loop.create_task(_deliver())
    except RuntimeError:
        # No running loop (e.g. a sync test emitting directly) — send
        # inline rather than silently dropping the notification.
        try:
            broadcast(owner, payload, urgency=urgency)
        except Exception:
            logger.debug("push: inline sink delivery failed", exc_info=True)


def start() -> None:
    """Idempotent: registers this module's bus sink with
    `notifications.register_sink` once. Safe to call from multiple request
    handlers/imports — subsequent calls are no-ops."""
    global _bus_started
    if _bus_started:
        return
    from src import notifications as notifications_bus
    notifications_bus.register_sink(_push_sink)
    _bus_started = True


def stop() -> None:
    """Best-effort unregister, mainly for tests that want a clean bus
    between cases."""
    global _bus_started
    if not _bus_started:
        return
    from src import notifications as notifications_bus
    try:
        notifications_bus.unregister_sink(_push_sink)
    except Exception:
        logger.debug("push: unregister_sink failed", exc_info=True)
    _bus_started = False
