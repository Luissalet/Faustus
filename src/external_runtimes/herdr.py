"""src/external_runtimes/herdr.py — CMP-06 (INFORME_COMPARATIVO_V2.md §3.5):
a READ-ONLY adapter to an externally configured Herdr runtime.

**Sin investigación externa, por instrucción del encargo.** Faustus never
fetched Herdr's real API while building this — every shape below
(`GET /version`, a session/presence listing) is inferred strictly from what
INFORME_COMPARATIVO_V2.md §3.5 describes. Treat the wire contract as
PENDING VALIDATION against a real Herdr instance
(`docs/api/external_runtimes.md` repeats this warning where an operator
will actually read it); `SUPPORTED_VERSIONS` is deliberately a short,
explicit allowlist rather than "accept anything that parses", so an
unrecognised `/version` response fails loudly (`UnsupportedVersionError`)
instead of this adapter silently guessing at a shape it has never seen.

**Read-only, permanently.** Nothing in this module sends a session, an
input, or a command TO Herdr — `list_presence`/`negotiate_version` only
ever GET. If a future ficha adds a verb that sends something, that verb
MUST apply the same `delivery` discipline
`src/desktop_semantics/contracts.py::ActionResult` already uses for the
desktop layer: a request that times out AFTER the socket write already
happened is `unknown`, never `not_delivered` — silently retrying a send
that may have already landed is exactly the bug the repo's `unknown`
convention exists to prevent. No such verb exists here today, on purpose;
`TransportError.delivery` below already distinguishes a confirmed
before-any-bytes-left failure (`not_delivered`) from a timeout
(`unknown`) so that future verb has the right base class ready.

Presence rows carry `certainty ∈ {structured, heuristic}`: `structured`
when Herdr gave a fields Faustus can date (a real last-seen timestamp),
`heuristic` otherwise (a status string this adapter is inferring recency
from, or none at all) — never presented as equally reliable.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

CERTAINTY_VALUES = ("structured", "heuristic")

# Deliberately short: the /version contract itself is unvalidated (see
# module docstring), so this allowlist grows only once a real Herdr
# response has actually been seen and checked in, never by guessing.
SUPPORTED_VERSIONS: tuple = ("1",)


class HerdrError(Exception):
    """Base for every Herdr adapter failure. `.code` matches the repo's
    `{"error": ..., "error_class": "external_runtimes.<reason>"}` shape."""

    code = "external_runtimes.error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class NotConfiguredError(HerdrError):
    """No `base_url` saved (`settings: external_runtimes.herdr`) — connect
    a Herdr runtime before asking it anything."""

    code = "external_runtimes.not_configured"


class UnsupportedVersionError(HerdrError):
    """Herdr answered `/version` with something outside `SUPPORTED_VERSIONS`
    (or a shape this adapter cannot even read a version out of) — never
    guessed at, always a typed refusal naming what came back."""

    code = "external_runtimes.unsupported_version"


class TransportError(HerdrError):
    """Network/HTTP failure talking to Herdr.

    `delivery` mirrors `src.desktop_semantics.contracts.ActionResult`'s
    three-state discipline for the (currently READ-only) calls this module
    makes, and is the base any future write-verb must reuse:
      'not_delivered'  the transport affirmatively knows nothing reached
                        Herdr (DNS failure, connection refused before any
                        bytes left this process).
      'unknown'        the request timed out — Herdr may or may not have
                        received/processed it; never treated as "did not
                        happen".
    """

    code = "external_runtimes.transport_error"

    def __init__(self, message: str, *, delivery: str = "unknown"):
        super().__init__(message)
        if delivery not in ("not_delivered", "unknown"):
            delivery = "unknown"
        self.delivery = delivery


@dataclass(frozen=True)
class HerdrConfig:
    base_url: str = ""
    token: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def to_dict(self, *, include_token: bool = False) -> Dict[str, Any]:
        out: Dict[str, Any] = {"base_url": self.base_url, "configured": self.configured}
        if include_token:
            out["token_set"] = bool(self.token)
        return out


@dataclass(frozen=True)
class Presence:
    """One Herdr session/presence row, normalised.

    `raw` keeps the untouched source row for callers/UI that want a field
    this adapter did not think to normalise — never a second source of
    truth, since every typed field here is DERIVED from `raw`, not stored
    independently of it.
    """

    session_id: str
    label: str
    state: str
    certainty: str
    signal_age_s: Optional[float]
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "label": self.label,
            "state": self.state,
            "certainty": self.certainty,
            "signal_age_s": self.signal_age_s,
        }


# (method, url, headers, timeout_s) -> {"status": int, "json": Any | None}.
# Injectable so tests never touch a real socket ("sin red en tests (fakes)").
Transport = Callable[[str, str, Dict[str, str], float], Dict[str, Any]]


def _default_transport(method: str, url: str, headers: Dict[str, str], timeout: float) -> Dict[str, Any]:
    import requests

    try:
        response = requests.request(method, url, headers=headers, timeout=timeout)
    except requests.exceptions.Timeout as exc:
        raise TransportError(f"Herdr request timed out after {timeout}s: {url}", delivery="unknown") from exc
    except requests.exceptions.RequestException as exc:
        raise TransportError(f"could not reach Herdr at {url}: {exc}", delivery="not_delivered") from exc
    try:
        body = response.json()
    except ValueError:
        body = None
    return {"status": response.status_code, "json": body}


def _presence_from_row(row: Dict[str, Any], now: float) -> Presence:
    session_id = str(row.get("id") or row.get("session_id") or "")
    label = str(row.get("label") or row.get("name") or session_id or "(unnamed)")
    state = str(row.get("state") or row.get("status") or "unknown")

    last_seen = row.get("last_seen_at")
    if last_seen is None:
        last_seen = row.get("updated_at")
    signal_age_s: Optional[float] = None
    certainty = "heuristic"
    if isinstance(last_seen, (int, float)):
        signal_age_s = max(0.0, now - float(last_seen))
        certainty = "structured"
    elif row.get("certainty") in CERTAINTY_VALUES:
        certainty = str(row["certainty"])

    return Presence(
        session_id=session_id, label=label, state=state,
        certainty=certainty, signal_age_s=signal_age_s, raw=dict(row),
    )


class HerdrClient:
    """Thin, read-only HTTP client to one configured Herdr runtime.

    `transport` is injectable precisely so tests exercise this class with
    zero network (a fake that returns canned `{"status", "json"}` dicts, or
    raises `TransportError` itself to simulate a timeout) — see
    `tests/test_cmp06_herdr_adapter.py`.
    """

    def __init__(self, config: HerdrConfig, *, transport: Optional[Transport] = None, timeout: float = 10.0):
        self.config = config
        self._transport = transport or _default_transport
        self.timeout = timeout

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.config.token:
            headers["Authorization"] = f"Bearer {self.config.token}"
        return headers

    def _require_configured(self) -> None:
        if not self.config.configured:
            raise NotConfiguredError(
                "external_runtimes.herdr.base_url is not set — connect a Herdr runtime first"
            )

    def negotiate_version(self) -> Dict[str, Any]:
        """`GET {base_url}/version`. INFORME §3.5 names this endpoint
        without pinning its exact response shape — this accepts either
        `{"version": "..."}` or a bare string/number body; anything else,
        or a version outside `SUPPORTED_VERSIONS`, is
        `UnsupportedVersionError` (never guessed at)."""
        self._require_configured()
        resp = self._transport("GET", self.config.base_url.rstrip("/") + "/version", self._headers(), self.timeout)
        if resp.get("status", 0) >= 400:
            raise HerdrError(f"Herdr /version returned HTTP {resp.get('status')}")
        body = resp.get("json")
        version: Optional[str] = None
        if isinstance(body, dict):
            raw_version = body.get("version")
            version = str(raw_version) if raw_version is not None else None
        elif isinstance(body, (str, int, float)):
            version = str(body)
        if version not in SUPPORTED_VERSIONS:
            raise UnsupportedVersionError(
                f"Herdr reports version {version!r}, which this adapter does not recognise "
                f"(supported: {', '.join(SUPPORTED_VERSIONS)}) — the /version contract itself is "
                "pending validation against a real Herdr instance (docs/api/external_runtimes.md)"
            )
        return {"version": version, "raw": body}

    def list_presence(self) -> List[Presence]:
        """`GET {base_url}/sessions` (the endpoint INFORME §3.5 describes
        for session/presence listing, without pinning its exact path —
        adjust once validated against real Herdr). Never mutates anything;
        never relays an input INTO Herdr."""
        self._require_configured()
        resp = self._transport("GET", self.config.base_url.rstrip("/") + "/sessions", self._headers(), self.timeout)
        if resp.get("status", 0) >= 400:
            raise HerdrError(f"Herdr /sessions returned HTTP {resp.get('status')}")
        body = resp.get("json")
        if isinstance(body, list):
            rows = body
        elif isinstance(body, dict):
            rows = body.get("sessions") or body.get("items") or []
        else:
            rows = []
        now = time.time()
        return [_presence_from_row(r, now) for r in rows if isinstance(r, dict)]


def load_config() -> HerdrConfig:
    """Read `external_runtimes.herdr {base_url, token}` from settings.

    Stored under the flat key `external_runtimes_herdr` (`src/settings.py`
    has no nested-path settings — see that module's `DEFAULT_SETTINGS`);
    the token is decrypted with `src.secret_storage` (the same Fernet store
    IMAP/SMTP passwords already use) — plaintext never sits in the settings
    file even though it is written there.
    """
    from src.settings import get_setting
    from src import secret_storage

    raw = get_setting("external_runtimes_herdr", {}) or {}
    if not isinstance(raw, dict):
        raw = {}
    token_stored = str(raw.get("token") or "")
    token = secret_storage.decrypt(token_stored) if token_stored else ""
    return HerdrConfig(base_url=str(raw.get("base_url") or "").strip(), token=token)


def save_config(*, base_url: str, token: Optional[str] = None) -> HerdrConfig:
    """Write `base_url` (and, when given, a new `token`) — `token=None`
    keeps whatever is already stored (so updating the base URL alone does
    not force re-entering the token); `token=""` clears it explicitly."""
    from src.settings import get_setting, update_settings
    from src import secret_storage

    existing = get_setting("external_runtimes_herdr", {}) or {}
    if not isinstance(existing, dict):
        existing = {}
    stored_token = existing.get("token", "")
    if token is not None:
        stored_token = secret_storage.encrypt(token) if token else ""
    update_settings({"external_runtimes_herdr": {"base_url": base_url.strip(), "token": stored_token}})
    return load_config()
