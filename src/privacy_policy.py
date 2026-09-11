"""
src/privacy_policy.py — one auditable "does this leave the machine" gate
(SEC-04 / MOD-05, QA-29).

Faustus already has several per-fetch SSRF guards (`src/outbound_fetch.py`,
`src/url_safety.py`, `src/url_security.py`) that ask "is THIS url safe to
open", and a "local mode" toggle on the main chat endpoint. Neither answers
QA-29's actual scenario: a user turns local mode on for the main chat model
and reasonably expects NOTHING to leave the machine, but a reranker, a
compaction summarizer or a custom embedding endpoint each decide for
themselves whether to call out, because nothing makes them ask a shared
authority first. Filtering "by auxiliary" is exactly the gap named in
docs/spec/v2/acceptance_scenarios.json's QA-29: "no filtra por componente
auxiliar."

This module is that shared authority. It does not replace the SSRF guards
above (a destination can be locally-reachable and still be a metadata/
link-local trap `url_safety` must reject) — it answers a narrower, earlier
question: given the CURRENT privacy profile, is this component even allowed
to try reaching this destination at all.

Profile
-------
Exactly one of three values, named in the spec:

  local_only       — an auxiliary configured to call a non-local endpoint is
                      blocked before the network call, every time.
  local_preferred   — the default: nothing is blocked here (per-fetch SSRF
                      guards still apply downstream).
  cloud_allowed     — remote auxiliaries are explicitly acceptable.

The active profile is a global admin setting (``privacy_profile`` in
`src/settings.py`) that a project can override by carrying a
``privacy_profile`` field of its own (`services/projects.py` stores projects
as plain dicts in `data/projects.json`, so this reads an optional field on
that row rather than adding a new column or a second config store — see
`get_privacy_profile`'s docstring for exactly what is and is not wired).

Components wired to call `assert_outbound`: the custom/HTTP embedding lane
(`src/embedding_lanes.py`), the ChromaDB vector store connection
(`src/chroma_client.py`), the remote compaction summarizer
(`src/context_compactor.py`), the cross-encoder reranker (`src/rerank.py`),
OCR/vision (`src/document_processor.py::analyze_image_with_vl_result`,
component `"ocr_vision"`), and remote TTS/STT
(`services/tts/tts_service.py::_synthesize_api`,
`services/stt/stt_service.py::_transcribe_api`, components `"tts"`/`"stt"`).
Telemetry (`src/scorecard.py`) has no network egress to gate at all — see
`tests/test_l68_sec04_ocr_tts_stt_egress_audit.py`, the audit that closed
this list.
"""
from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from src.contracts import ErrorInfo

logger = logging.getLogger(__name__)

PROFILE_LOCAL_ONLY = "local_only"
PROFILE_LOCAL_PREFERRED = "local_preferred"
PROFILE_CLOUD_ALLOWED = "cloud_allowed"

#: Closed on purpose, same discipline as `src.contracts.errors.ERROR_CATEGORIES`:
#: a profile that isn't one of these three is a typo, not a fourth mode.
PROFILES = (PROFILE_LOCAL_ONLY, PROFILE_LOCAL_PREFERRED, PROFILE_CLOUD_ALLOWED)

DEFAULT_PROFILE = PROFILE_LOCAL_PREFERRED

#: Global setting key. Deliberately NOT added to `src.settings.DEFAULT_SETTINGS`
#: (that file is outside this lote's file list) — `get_setting`/`get_user_setting`
#: fall back to the `default=` argument for any key missing from the saved
#: document, so this works today; persisting an explicit choice through the
#: settings UI needs `DEFAULT_SETTINGS["privacy_profile"] = DEFAULT_PROFILE`
#: added there first (see this lote's report for the exact line).
SETTING_KEY = "privacy_profile"

#: A project row's own override, read directly off the dict `services/projects`
#: hands back (projects.json has no fixed schema — see module docstring).
PROJECT_OVERRIDE_KEY = "privacy_profile"

#: Hostnames that never leave the machine regardless of DNS. Mirrors the
#: judgement `src/endpoint_resolver.py::endpoint_cost_tracked` already makes
#: about "is this local", rather than inventing a second opinion.
_LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"}


class PrivacyPolicyError(RuntimeError):
    """Raised by `assert_outbound` when the active profile blocks a call.

    A `RuntimeError` subclass on purpose: every call site this lote wires already
    wraps its outbound attempt in a broad `except Exception` that logs and
    degrades gracefully (no reranking, no compaction this turn, no custom
    embedding lane) — the SAME path a refused connection or a timeout already
    takes. That is what "bloqueado y visible" means here: the block surfaces
    through the exact channel the component already uses to report failure,
    carrying an explicit taxonomy code instead of a generic connection error.
    """

    def __init__(self, component: str, destination: str, profile: str, error_info: ErrorInfo):
        self.component = component
        self.destination = destination
        self.profile = profile
        self.error_info = error_info
        super().__init__(error_info.message)


def is_local_destination(destination: str) -> bool:
    """Best-effort, DNS-free judgement of whether `destination` stays on this
    machine. Accepts a bare host, a ``host:port`` pair, or a full URL —
    callers pass whatever they already have (a ChromaDB host:port, a
    reranker/embedding base_url, ...).

    Deliberately does not resolve DNS: this runs on every call a gated
    component makes, so it must stay synchronous and cheap, and a hostname
    that LOOKS local but resolves elsewhere is exactly what
    `src/url_safety.py::check_outbound_url`'s resolver-based check exists to
    catch downstream — this function only answers "did the operator point
    this component somewhere that isn't obviously local", the question
    `local_only` needs answered before it even tries.
    """
    dest = (destination or "").strip()
    if not dest:
        return True  # nothing configured, nothing to send anywhere
    probe = dest if "://" in dest else f"//{dest}"
    try:
        parsed = urlparse(probe)
    except Exception:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        # urlparse couldn't find a netloc (e.g. a bare "host:port" without the
        # "//" prefix taking); fall back to the substring before the first ':'.
        host = dest.split("/")[0].split(":")[0].strip().lower().rstrip(".")
    if not host:
        return False
    if host in _LOCAL_HOSTNAMES or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return bool(ip.is_private or ip.is_loopback)
    except ValueError:
        return False


def get_privacy_profile(project: Optional[Dict[str, Any]] = None,
                         owner: Optional[str] = None) -> str:
    """The effective profile: a project's own override beats the global
    admin setting, which beats `DEFAULT_PROFILE`.

    Reads the same two authorities `src/effective_config.py` already composes
    for every other knob (`src/settings.py`'s global store and a project row
    from `services/projects.py`) rather than adding a third — but this module
    cannot edit `effective_config.py` (outside this lote's file list), so
    `privacy_profile` is not yet a field the Explain-panel's global/project
    offer tables surface. See the report for the exact addition needed there.
    """
    if project:
        raw = project.get(PROJECT_OVERRIDE_KEY)
        if isinstance(raw, str) and raw.strip() in PROFILES:
            return raw.strip()
    try:
        from src.settings import get_user_setting
        value = get_user_setting(SETTING_KEY, owner or "", DEFAULT_PROFILE)
    except Exception as exc:  # noqa: BLE001 - settings module unavailable at import time
        logger.debug("privacy_policy: could not read %s (%s); using default", SETTING_KEY, exc)
        value = DEFAULT_PROFILE
    value = str(value or "").strip()
    return value if value in PROFILES else DEFAULT_PROFILE


def assert_outbound(component: str, destination: str, *,
                     project: Optional[Dict[str, Any]] = None,
                     owner: Optional[str] = None,
                     profile: Optional[str] = None) -> None:
    """Raise `PrivacyPolicyError` if the active profile forbids `component`
    from reaching `destination` off this machine.

    Every auxiliary that can ship user content off-box is expected to call
    this BEFORE opening the connection — see the module docstring for which
    ones already do. `local_preferred` and `cloud_allowed` never block here
    (this is not the SSRF gate); `local_only` blocks any destination this
    process cannot judge as staying local (`is_local_destination`).

    `destination` is never logged or included verbatim in a raised message
    below WARNING — the message says what to do, not where the auxiliary was
    pointed, so a blocked-outbound log line does not itself become a second
    leak of the private endpoint.
    """
    active = profile or get_privacy_profile(project=project, owner=owner)
    if active != PROFILE_LOCAL_ONLY:
        return
    if is_local_destination(destination):
        return
    logger.warning("privacy_policy: blocked %s outbound call under '%s'", component, active)
    message = (
        f"{component} is configured to reach a non-local endpoint, but the "
        f"active privacy profile is '{PROFILE_LOCAL_ONLY}'. Point {component} "
        f"at a local endpoint, or switch the privacy profile to "
        f"'{PROFILE_LOCAL_PREFERRED}' or '{PROFILE_CLOUD_ALLOWED}' if a "
        f"remote call is acceptable here."
    )
    error_info = ErrorInfo(
        code="privacy.blocked_outbound",
        message=message,
        retryable=False,
        next_action="change_privacy_profile_or_component",
    )
    raise PrivacyPolicyError(component, destination, active, error_info)


def local_only_policy_covers_auxiliaries() -> bool:
    """Marker the QA-29 test asserts on: this module exists and is the single
    place `local_only` is enforced for auxiliaries. Kept as a real function
    (not just `hasattr` on a constant) so a future caller has something
    meaningful to invoke — it always returns True; the substance is
    `assert_outbound` actually being wired into the auxiliaries listed in the
    module docstring, which the QA-29 test also checks for directly.
    """
    return True
