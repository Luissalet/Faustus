"""core/oidc.py — corporate OIDC login (A22): Authorization Code + PKCE.

This is a *login* mechanism (the app's own users authenticate against a
corporate identity provider and get the same session cookie a local
username/password login gives them, via `core.auth.AuthManager`). It has
nothing to do with `src/mcp_oauth.py`, which authorizes *outbound* requests
to remote MCP servers.

Pieces:
  - PKCE (RFC 7636, S256) + a one-shot, server-side `state`/`nonce` registry
    (`register_pending` / `pop_pending_state`) so a replayed callback (same
    `state`, whether or not with the same `code`) is refused: the second
    lookup finds nothing.
  - Discovery (`/.well-known/openid-configuration`) and JWKS, each cached
    with its own TTL (`fetch_discovery` / `fetch_jwks`), with an injectable
    `http_get` fetcher so tests can serve a fake IdP without going over the
    network.
  - `verify_id_token`: signature (RS256/ES256, matched by `kid` against the
    fetched JWKS), `iss`, `aud` (exact match — never "in a list"), `exp`/
    `nbf`/`iat` with a bounded clock-skew allowance, and `nonce` bound to the
    login attempt that started this flow.
  - `is_email_allowed` / `is_admin_identity`: the allow-list + admin mapping
    `routes/auth_routes.py` applies before ever touching `core.auth`.

Uses PyJWT (`requirements-optional.txt`) for the JWS envelope, imported
lazily so nothing here executes at import time if the corporate-login
feature is never touched, and `cryptography` (already a hard dependency) to
turn a JWK into the public key PyJWT verifies against.
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import logging
import re
import secrets
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

CLOCK_SKEW_SECONDS = 60

# ---------------------------------------------------------------------------
# One-shot state/nonce/PKCE registry
# ---------------------------------------------------------------------------

#: state -> {nonce, verifier, redirect_uri, created_at}. In-memory and
#: per-process, matching src/mcp_oauth.py's own pending-flow registry: a
#: login attempt lives for at most _PENDING_TTL_SECONDS and is consumed
#: exactly once, so it does not need to survive a restart.
_pending: Dict[str, Dict[str, Any]] = {}
_pending_lock = threading.Lock()
_PENDING_TTL_SECONDS = 600  # 10 minutes to complete the round trip

_discovery_cache: Dict[str, Tuple[dict, float]] = {}
_jwks_cache: Dict[str, Tuple[dict, float]] = {}
_cache_lock = threading.Lock()

#: Test seam: set to an `httpx.MockTransport` (or any `httpx` transport) to
#: route every discovery/JWKS/token-exchange request in this module through
#: a fake IdP with no real network access, without threading a `transport`
#: argument through `routes/auth_routes.py`'s callers. None in production —
#: `httpx.AsyncClient(transport=None)` uses its normal real transport.
DEFAULT_TRANSPORT = None


def _prune_pending_locked() -> None:
    now = time.time()
    for state in [s for s, v in _pending.items() if now - v["created_at"] > _PENDING_TTL_SECONDS]:
        _pending.pop(state, None)


def new_pkce_pair() -> Tuple[str, str]:
    """(code_verifier, code_challenge) per RFC 7636 S256."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def register_pending(*, redirect_uri: str) -> Dict[str, str]:
    """Start a login attempt: mint `state` + `nonce` + a PKCE pair, store the
    verifier/nonce server-side keyed by `state`. Returns what the
    authorization URL needs (`state`, `nonce`, `code_challenge`)."""
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier, challenge = new_pkce_pair()
    with _pending_lock:
        _prune_pending_locked()
        _pending[state] = {
            "nonce": nonce,
            "verifier": verifier,
            "redirect_uri": redirect_uri,
            "created_at": time.time(),
        }
    return {"state": state, "nonce": nonce, "code_challenge": challenge}


def pop_pending_state(state: Optional[str]) -> Optional[Dict[str, Any]]:
    """Consume a pending login attempt exactly once.

    A replayed callback — the same `state` used twice, regardless of whether
    the `code` is the same or different, and regardless of whether the first
    call ever reached a session — finds nothing on the second call. Callers
    must answer 400 when this returns None, before doing anything else.
    """
    if not state:
        return None
    with _pending_lock:
        _prune_pending_locked()
        return _pending.pop(state, None)


def pending_count() -> int:
    """Test hook: how many login attempts are currently outstanding."""
    with _pending_lock:
        return len(_pending)


def build_authorization_url(*, authorization_endpoint: str, client_id: str,
                             redirect_uri: str, scope: str, state: str,
                             nonce: str, code_challenge: str) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    sep = "&" if "?" in authorization_endpoint else "?"
    return f"{authorization_endpoint}{sep}{urlencode(params)}"


# ---------------------------------------------------------------------------
# Discovery + JWKS, each cached with its own TTL
# ---------------------------------------------------------------------------


async def fetch_discovery(issuer: str, *, ttl_seconds: int, transport=None) -> dict:
    """GET ``{issuer}/.well-known/openid-configuration``, cached ``ttl_seconds``.

    ``transport`` is an injectable ``httpx`` transport (an ``httpx.
    MockTransport`` serving a fake IdP in tests); production leaves it unset
    and gets a real network request.
    """
    now = time.time()
    with _cache_lock:
        cached = _discovery_cache.get(issuer)
        if cached and now - cached[1] < ttl_seconds:
            return cached[0]
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    doc = await _http_get_json(url, transport=transport if transport is not None else DEFAULT_TRANSPORT)
    with _cache_lock:
        _discovery_cache[issuer] = (doc, now)
    return doc


async def fetch_jwks(jwks_uri: str, *, ttl_seconds: int, transport=None) -> dict:
    """GET the JWKS document, cached ``ttl_seconds`` keyed by its own URI (a
    provider can rotate `jwks_uri` independently of the discovery TTL)."""
    now = time.time()
    with _cache_lock:
        cached = _jwks_cache.get(jwks_uri)
        if cached and now - cached[1] < ttl_seconds:
            return cached[0]
    doc = await _http_get_json(jwks_uri, transport=transport if transport is not None else DEFAULT_TRANSPORT)
    with _cache_lock:
        _jwks_cache[jwks_uri] = (doc, now)
    return doc


async def _http_get_json(url: str, *, transport=None) -> dict:
    import httpx
    async with httpx.AsyncClient(timeout=10.0, transport=transport) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


async def exchange_code_for_token(token_endpoint: str, data: dict, *, transport=None) -> Tuple[int, dict]:
    """POST the authorization-code grant. Returns (status_code, json_body) —
    the caller decides what each status means (this module only fetches and
    parses; `routes/auth_routes.py` turns a non-200 into 401). ``transport``
    is the same injectable ``httpx`` transport `fetch_discovery`/`fetch_jwks`
    take, so a test's fake IdP serves all three calls through one handler.
    """
    import httpx
    async with httpx.AsyncClient(timeout=10.0,
                                  transport=transport if transport is not None else DEFAULT_TRANSPORT) as client:
        resp = await client.post(token_endpoint, data=data)
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 — a non-JSON error page still needs a body
        body = {}
    return resp.status_code, body


def clear_caches() -> None:
    """Test hook: drop discovery/JWKS caches between scenarios."""
    with _cache_lock:
        _discovery_cache.clear()
        _jwks_cache.clear()


# ---------------------------------------------------------------------------
# id_token validation
# ---------------------------------------------------------------------------


class OidcError(Exception):
    """A validation failure that must become a specific HTTP status.

    Defaults to 401 (expired, not-yet-valid, wrong-signature, wrong-audience
    id_token — an *invalid identity*, per A22's trigger). Callers raise 400
    directly for a replayed callback and 403 directly for an identity that
    validated but isn't on the allow-list — those are not `OidcError`s.
    """

    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


def _b64url_decode(seg: str) -> bytes:
    pad = "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg + pad)


def _default_backend():
    from cryptography.hazmat.backends import default_backend
    return default_backend()


def _jwk_to_public_key(jwk: dict):
    """Build a `cryptography` public key from one JWKS entry. RSA and EC
    only — the two families RS256/ES256 (the algorithms this module accepts)
    verify against."""
    kty = jwk.get("kty")
    if kty == "RSA":
        from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
        n = int.from_bytes(_b64url_decode(jwk["n"]), "big")
        e = int.from_bytes(_b64url_decode(jwk["e"]), "big")
        return RSAPublicNumbers(e, n).public_key(_default_backend())
    if kty == "EC":
        from cryptography.hazmat.primitives.asymmetric.ec import (
            EllipticCurvePublicNumbers, SECP256R1, SECP384R1, SECP521R1,
        )
        curve_map = {"P-256": SECP256R1(), "P-384": SECP384R1(), "P-521": SECP521R1()}
        curve = curve_map.get(jwk.get("crv"))
        if curve is None:
            raise OidcError(f"Unsupported EC curve: {jwk.get('crv')}")
        x = int.from_bytes(_b64url_decode(jwk["x"]), "big")
        y = int.from_bytes(_b64url_decode(jwk["y"]), "big")
        return EllipticCurvePublicNumbers(x, y, curve).public_key(_default_backend())
    raise OidcError(f"Unsupported JWK key type: {kty}")


def verify_id_token(id_token: str, *, jwks: dict, issuer: str, audience: str,
                     nonce: str, leeway: int = CLOCK_SKEW_SECONDS) -> Dict[str, Any]:
    """Full id_token validation. Raises `OidcError` (status 401) on any
    failure; returns the verified claims dict on success.

    Checks, in order: JWS signature (RS256/ES256 only, key selected from
    `jwks` by the token's `kid`), `iss` exact match, `aud` exact match
    (PyJWT's `audience=` already refuses a token whose `aud` doesn't
    contain/equal this value), `exp`/`nbf`/`iat` with `leeway` seconds of
    clock skew, and finally `nonce` — compared with `secrets.compare_digest`
    against the nonce this login attempt minted, so a token replayed from a
    different (or no) login attempt is refused even if every other claim is
    perfectly valid.
    """
    try:
        import jwt as pyjwt  # lazy: requirements-optional.txt (PyJWT)
    except ImportError as e:
        raise OidcError("PyJWT is not installed", status=500) from e

    try:
        header = pyjwt.get_unverified_header(id_token)
    except Exception as e:  # noqa: BLE001
        raise OidcError(f"Malformed id_token: {e}") from e

    alg = header.get("alg")
    if alg not in ("RS256", "ES256"):
        raise OidcError(f"Unsupported id_token signing algorithm: {alg}")

    kid = header.get("kid")
    keys = jwks.get("keys") or []
    candidates = [k for k in keys if kid is None or k.get("kid") == kid] or keys
    if not candidates:
        raise OidcError("No JWKS key available to verify id_token")

    last_error: Optional[Exception] = None
    claims: Optional[Dict[str, Any]] = None
    for jwk in candidates:
        try:
            public_key = _jwk_to_public_key(jwk)
            claims = pyjwt.decode(
                id_token,
                key=public_key,
                algorithms=[alg],
                audience=audience,
                issuer=issuer,
                leeway=leeway,
                options={"require": ["exp", "iat", "iss", "aud"]},
            )
            break
        except OidcError:
            raise
        except Exception as e:  # noqa: BLE001 — try the next candidate key
            last_error = e
            claims = None

    if claims is None:
        raise OidcError(f"id_token validation failed: {last_error}")

    token_nonce = str(claims.get("nonce") or "")
    if not token_nonce or not secrets.compare_digest(token_nonce, str(nonce or "")):
        raise OidcError("id_token nonce does not match this login attempt")

    return claims


# ---------------------------------------------------------------------------
# Identity mapping (allow-list + admin mapping)
# ---------------------------------------------------------------------------


def is_email_allowed(email: str, *, allowed_emails: List[str], allowed_domains: List[str]) -> bool:
    """Fail closed: with both lists empty, nothing is allowed — an operator
    who turns OIDC on but never configures an allow-list must get everyone
    rejected, not everyone admitted. Domains accept one leading wildcard
    segment (``*.example.com``)."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return False
    domain = email.rsplit("@", 1)[1]
    for allowed in allowed_emails or ():
        if email == str(allowed).strip().lower():
            return True
    for pattern in allowed_domains or ():
        pattern = str(pattern).strip().lower().lstrip("@")
        if pattern and (domain == pattern or fnmatch.fnmatch(domain, pattern)):
            return True
    return False


def is_admin_identity(claims: Dict[str, Any], *, admin_emails: List[str], admin_group: str = "") -> bool:
    """True if the verified claims name an admin, by exact email match
    against `oidc_admin_emails` or membership of `oidc_admin_group` in the
    id_token's `groups` (or `roles`) claim. Re-evaluated on every login."""
    email = str(claims.get("email") or "").strip().lower()
    for allowed in admin_emails or ():
        if email == str(allowed).strip().lower():
            return True
    admin_group = (admin_group or "").strip()
    if admin_group:
        groups = claims.get("groups") or claims.get("roles") or []
        if isinstance(groups, str):
            groups = [groups]
        if admin_group in [str(g) for g in groups]:
            return True
    return False


_SAFE_USERNAME_RE = re.compile(r"[^a-z0-9_.-]+")


def username_from_email(email: str) -> str:
    """A filesystem/JSON-key-safe candidate local username derived from the
    email's local part. Never the identity key itself — see
    `core.auth.AuthManager.resolve_or_create_oidc_user`, which keys the
    identity by `sub` and only uses this for a human-readable account name."""
    local = (email or "").split("@", 1)[0].strip().lower()
    local = _SAFE_USERNAME_RE.sub("-", local).strip("-.")
    return local or "oidc-user"
