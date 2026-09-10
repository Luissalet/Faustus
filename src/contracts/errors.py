"""
contracts/errors.py — one taxonomy for "why didn't it work" (OBS-03).

The masterplan's OBS-03 asks for a closed vocabulary of failure categories —
capability, schema, permission, resource, transport, timeout, cancelled,
conflict, verification, unknown — so a UI, a retry loop and an audit can
switch on the same ten names instead of each inventing its own reading of
whatever string an exception happened to carry. Before this module the repo
had two partial answers that did not talk to each other: `core/exceptions.py`
(four ad hoc classes) and `src/tool_outcome.py` (a four-valued outcome for a
tool call). Neither is replaced — `tool_outcome.Outcome` answers "did it
count as a failure" for a scorecard, a different and coarser question than
"what should the caller who just got this error do next", which is what
`ErrorInfo.next_action` is for.

`ErrorInfo` itself is deliberately permissive about `code`: it is the
`tool_result` §34.5 error object (`code`, `message`, `retryable`,
`next_action`), and a tool's own vocabulary — `BASE_REVISION_MISMATCH` in the
spec's own example — is exactly as valid a code as one of ours. What IS
closed is `from_exception()`: every exception this module knows how to
classify comes out dotted under one of the ten OBS-03 categories, so
Faustus's own failures land in one taxonomy instead of one string per call
site that raised them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, Type

from .base import ContractError, as_mapping, flag, reject_unknown, text

#: The ten buckets, closed on purpose. A failure that fits none of them is a
#: category this taxonomy has not met yet, not a reason to spell an eleventh
#: one differently at each call site.
ERROR_CATEGORIES = (
    "capability", "schema", "permission", "resource", "transport",
    "timeout", "cancelled", "conflict", "verification", "unknown",
)

#: Sub-codes already implied by the repo's own negative paths — `execution.py`'s
#: `REFUSED_REASONS`, the approval gate's own codes in `src/tool_security.py`,
#: the conflict this package's own `Run`/`ChangeSet` already name — grouped
#: under the category that answers them. Not exhaustive and not enforced: a
#: caller may mint a new subcode under a known category (`from_exception`
#: always does), this dict exists so two call sites reaching for "the sandbox
#: was not up" reuse `capability.backend_unavailable` instead of inventing a
#: second spelling of the same fact.
ERROR_SUBCODES: Dict[str, Tuple[str, ...]] = {
    "capability": ("backend_unavailable", "unsupported", "image_missing", "model_no_tools"),
    "schema": ("invalid_manifest", "unknown_field", "type_mismatch",
               "contract_violation", "invalid_upload"),
    "permission": ("policy", "spec_wider_than_permissions", "approval_required", "denied"),
    "resource": ("not_found", "quota_exceeded", "workspace_missing"),
    "transport": ("network_unreachable", "backend_unreachable",
                  "llm_service_error", "web_search_error"),
    "timeout": ("deadline_exceeded",),
    "cancelled": ("user_stopped", "session_deleted"),
    "conflict": ("base_revision_mismatch", "plan_changed", "concurrent_write"),
    "verification": ("inconclusive", "pre_existing_failures"),
    "unknown": ("panic", "internal_error", "unmapped_exception"),
}

#: `tool_result.error.code` (and `approval_request`-adjacent codes) are all
#: opaque identifiers in the spec's own JSON Schema: letters, digits, and
#: `_ . : -`. Reused here rather than `base.ident`, which is lowercase-only
#: and would reject the spec's own example (`BASE_REVISION_MISMATCH`).
_CODE_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")

#: What `from_exception()` fills in when the caller does not override it —
#: retryable and a next action are the two fields that make an error
#: actionable instead of just apologetic, and every category answers both.
_DEFAULTS: Dict[str, Tuple[bool, str]] = {
    "capability": (False, "choose_different_backend"),
    "schema": (False, "fix_payload_and_retry"),
    "permission": (False, "request_approval"),
    "resource": (False, "verify_resource_exists"),
    "transport": (True, "retry_with_backoff"),
    "timeout": (True, "retry_with_backoff"),
    "cancelled": (False, "none"),
    "conflict": (False, "read_current_and_reconcile"),
    "verification": (False, "rerun_verification"),
    "unknown": (False, "escalate_to_operator"),
}


@dataclass(frozen=True)
class ErrorInfo:
    """The `tool_result` error object: a machine code next to the human
    message, plus the two fields that say what to do about it.

    `code` is intentionally NOT restricted to `ERROR_CATEGORIES` by default —
    a tool's own error vocabulary is real data, not a violation — but
    `require_known_category=True` turns that restriction on for callers (like
    `from_exception`, indirectly) who want every code they mint to land in
    the closed OBS-03 taxonomy."""

    code: str
    message: str = ""
    retryable: bool = False
    next_action: str = ""

    _KEYS = ("code", "message", "retryable", "next_action")

    @classmethod
    def from_mapping(cls, raw, path: str = "error", *,
                      require_known_category: bool = False) -> "ErrorInfo":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        code = text(data, "code", path, max_len=128)
        if not _CODE_RE.fullmatch(code):
            raise ContractError(
                f"{path}.code", "must be letters, digits, and _ . : - only", got=code)
        if require_known_category and code.split(".", 1)[0] not in ERROR_CATEGORIES:
            raise ContractError(
                f"{path}.code", f"category must be one of {list(ERROR_CATEGORIES)}", got=code)
        if "message" not in data:
            raise ContractError(f"{path}.message",
                                "is required (use an empty string if there is none)")
        message = text(data, "message", path, required=True, allow_blank=True, max_len=4000)
        next_action = text(data, "next_action", path, max_len=512)
        if "retryable" not in data:
            raise ContractError(f"{path}.retryable", "is required")
        retryable = flag(data, "retryable", path, default=False)
        return cls(code=code, message=message, retryable=retryable, next_action=next_action)

    def to_mapping(self) -> Dict[str, object]:
        return {"code": self.code, "message": self.message,
                "retryable": self.retryable, "next_action": self.next_action}


#: Faustus exception class → (category, subcode). Populated once below by
#: `_install_default_mapping`; kept as a module-level dict rather than an
#: `if isinstance` chain so `from_exception` and any future registration stay
#: in one place.
_EXCEPTION_CATEGORY: Dict[Type[BaseException], Tuple[str, str]] = {}


def _register(exc_type: Type[BaseException], category: str, subcode: str) -> None:
    assert category in ERROR_CATEGORIES, category  # a typo here is a bug in this module, not data
    _EXCEPTION_CATEGORY[exc_type] = (category, subcode)


def _install_default_mapping() -> None:
    """Map the exceptions that already exist rather than adding new ones —
    `core/exceptions.py` stays the single definition (`src/exceptions.py` is
    already only a re-export shim of it), this just gives each class a second,
    closed name for what it means."""
    from core.exceptions import (
        InvalidFileUploadError, LLMServiceError, SessionNotFoundError, WebSearchError,
    )
    _register(ContractError, "schema", "contract_violation")
    _register(SessionNotFoundError, "resource", "not_found")
    _register(InvalidFileUploadError, "schema", "invalid_upload")
    _register(LLMServiceError, "transport", "llm_service_error")
    _register(WebSearchError, "transport", "web_search_error")


_install_default_mapping()


def from_exception(exc: BaseException, *, next_action: str = "",
                    retryable: Optional[bool] = None) -> ErrorInfo:
    """Turn one of the repo's own exceptions into the OBS-03 shape.

    An exception nobody registered is not swallowed into a generic "error" —
    it comes back as `unknown.unmapped_exception`, which is itself the
    correct signal that this map is missing an entry rather than a report
    that nothing went wrong."""
    for exc_type, (category, subcode) in _EXCEPTION_CATEGORY.items():
        if isinstance(exc, exc_type):
            default_retryable, default_action = _DEFAULTS[category]
            return ErrorInfo(
                code=f"{category}.{subcode}",
                message=str(exc),
                retryable=default_retryable if retryable is None else bool(retryable),
                next_action=next_action or default_action,
            )
    default_retryable, default_action = _DEFAULTS["unknown"]
    return ErrorInfo(
        code="unknown.unmapped_exception",
        message=str(exc),
        retryable=default_retryable if retryable is None else bool(retryable),
        next_action=next_action or default_action,
    )
