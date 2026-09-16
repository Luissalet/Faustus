"""tool_result_offload.py — an oversized tool result is stored whole, first.

A32/A12 in `docs/spec/paridad/`: two parallel tools that each come back with
a huge payload must not blow up the prompt, and after the process restarts
the owner must still be able to read every byte back with integrity metadata
that checks out. The trick that makes both true at once is ordering: the
full result is written to the durable, content-addressed artifact store
(`src/artifact_store.py` / `src/artifact_identity.py`) BEFORE the in-prompt
copy is ever shrunk, never after. A crash between "shrink the prompt" and
"persist the bytes" is how a harness loses data nobody can get back; this
module cannot have that crash because the persist happens first and the
prompt is only ever touched with the artifact id already in hand.

Four decisions worth stating:

* **Whole-result JSON, not just one field.** `format_tool_result()`
  (`src/tool_execution.py`) picks a single field to show depending on the
  result's shape (`stdout`, `output`, `content`, ...). This module does not
  guess which field the caller cares about — it snapshots every string field
  of the raw result dict as one JSON document, so nothing the model would
  otherwise have seen is silently dropped from the artifact even if it never
  makes the preview.
* **Deterministic occurrence id.** The id is `uuid5` of
  `(owner, session_id, run_id, call_id, tool, sha256)`, the same recipe
  `artifact_store.collect()` uses for run outputs. Calling this function
  twice for the same call with the same bytes reuses the same occurrence
  (`artifact_identity.ensure_occurrence()` is itself idempotent on a matching
  id), so a retried or duplicated offload never creates a second artifact.
* **A result that is already a stand-in is left alone.** The marker key
  `_tool_result_offload` on the dict this function returns is checked on
  entry — offloading an offload's own summary would either throw away the
  artifact reference or wrap it in another layer of truncation for no
  reason. This is what "idempotent" means for the function itself, as
  opposed to the id-level idempotency above.
* **No owner, no promise.** When `owner` is empty (an unauthenticated
  request, or a caller that could not resolve one -- see
  `src.owner_identity.effective_storage_owner`), nothing is written to the
  store and the note attached to the truncated result says exactly that
  instead of pointing at an artifact_id that cannot be opened later. A
  summary that promises retrieval it cannot deliver is worse than a summary
  that admits the tail is gone.

`read_artifact_range()` is the matching read side: a range/substring read
over an offloaded result's full text, gated by the same ownership check
`routes/artifact_routes.py` uses (`artifact_identity.for_owner`), so a wrong
owner or a guessed id gets the same refusal an HTTP 404 would — no metadata
leak through a tool call either. It is not registered as an agent tool by
this module; see `T5_wiring.md` for the exact registration this lot could
not make (tool registration is outside `src/tool_result_offload.py`'s
ownership for this lot).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import uuid
from typing import Any, Dict, Optional

from src.contracts.base import now_iso
from src.settings import get_setting

logger = logging.getLogger(__name__)

#: Marker key on an already-offloaded result. Also the JSON key name is
#: chosen to be extremely unlikely to collide with a real tool's own field.
OFFLOAD_MARKER = "_tool_result_offload"

#: How much of an oversized string field survives in the in-prompt preview.
_PREVIEW_HEAD = 1500
_PREVIEW_TAIL = 500


def offload_threshold_chars() -> int:
    """`agent_tool_result_offload_chars` (default 20000), read fresh on every
    call — the switch is read before anything is written to disk, never
    cached across a setting change."""
    try:
        value = int(get_setting("agent_tool_result_offload_chars", 20000) or 20000)
    except Exception:  # noqa: BLE001
        value = 20000
    return max(1000, value)


def _string_chars(result: Dict[str, Any]) -> int:
    return sum(len(v) for v in result.values() if isinstance(v, str))


def _truncate_field(value: str) -> str:
    if len(value) <= _PREVIEW_HEAD + _PREVIEW_TAIL:
        return value
    omitted = len(value) - _PREVIEW_HEAD - _PREVIEW_TAIL
    return (value[:_PREVIEW_HEAD]
            + f"\n... [{omitted} chars omitted; open the artifact for the full text] ...\n"
            + value[-_PREVIEW_TAIL:])


def _persist_full_result(full_text: str, digest: str, *, owner: str, session_id: str,
                         run_id: str, call_id: str, tool: str) -> str:
    """Write `full_text` to the artifact store under a deterministic
    occurrence id and return that id. Raises on failure — the caller decides
    what an unwritable store means for the summary it hands the model."""
    from src import artifact_store, artifact_identity as identity
    from src.contracts.blob import ArtifactOccurrence

    store = artifact_store.ARTIFACT_STORE_DIR
    os.makedirs(store, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".offload-", suffix=".json", dir=store)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(full_text)
        sha, size, stored_name, _created = artifact_store.publish_copy(
            tmp_path, store, "tool_result.json")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    assert sha == digest, "publish_copy must hash the exact bytes it wrote"

    identity.ensure_blob(sha256=sha, byte_size=size, filename=stored_name,
                         media_type="application/json")

    id_seed = json.dumps([owner, session_id, run_id, call_id, tool, sha], sort_keys=True)
    occurrence_id = "occ_" + uuid.uuid5(uuid.NAMESPACE_URL, id_seed).hex

    note = f"tool_result_offload tool={tool or 'unknown'}"
    if call_id:
        note += f" call_id={call_id}"

    occurrence = ArtifactOccurrence.parse({
        "id": occurrence_id, "kind": "json", "blob_sha256": sha,
        "label": f"tool result: {tool or 'unknown'}", "owner": owner,
        "project_id": "", "run_id": run_id or "", "session_id": session_id or "",
        "skill_id": "", "skill_version": "", "created_at": now_iso(), "partial": False,
        "provenance": {"backend": "tool_result_offload", "note": note},
        "retention": {"policy": "session", "reason": "tool result offload"},
    })
    identity.ensure_occurrence(occurrence)
    return occurrence_id


def offload_if_oversized(result: Dict[str, Any], *, owner: str, session_id: str = "",
                         run_id: str = "", call_id: str = "", tool: str = "",
                         threshold_chars: Optional[int] = None) -> Dict[str, Any]:
    """Return `result` unchanged when it is small or already offloaded;
    otherwise persist it whole and return a bounded stand-in with an
    `artifact_id` the model can open for the parts that were cut.

    Idempotent: calling this again on the dict it just returned (or on any
    dict already carrying `OFFLOAD_MARKER`) is a no-op — see the module
    docstring for why, and for the separate id-level idempotency that also
    holds across process restarts for the SAME (owner, session, run, call,
    tool, bytes) tuple.
    """
    if not isinstance(result, dict) or result.get(OFFLOAD_MARKER):
        return result

    threshold = offload_threshold_chars() if threshold_chars is None else threshold_chars
    if _string_chars(result) <= threshold:
        return result

    full_text = json.dumps(result, ensure_ascii=False, indent=2, default=str, sort_keys=True)
    digest = hashlib.sha256(full_text.encode("utf-8")).hexdigest()

    artifact_id: Optional[str] = None
    storage_error: Optional[str] = None
    if owner:
        try:
            artifact_id = _persist_full_result(
                full_text, digest, owner=owner, session_id=session_id,
                run_id=run_id, call_id=call_id, tool=tool)
        except Exception as exc:  # noqa: BLE001 - an offload failure must never break a turn
            storage_error = str(exc)
            logger.exception("tool_result_offload: could not persist result for tool=%s", tool)
    else:
        storage_error = "no authenticated storage owner; nothing was written"

    truncated: Dict[str, Any] = {
        key: (_truncate_field(value) if isinstance(value, str) else value)
        for key, value in result.items()
    }

    if artifact_id:
        note = (f"Full result ({len(full_text)} chars, sha256={digest}) was stored "
                f"as artifact {artifact_id} before this preview was built. Open it "
                f"with read_artifact(artifact_id=\"{artifact_id}\", start=..., "
                "end=...) or query=... to read past what is shown above.")
    else:
        note = (f"Result truncated ({len(full_text)} chars total) and NOT durably "
                f"stored ({storage_error}); the omitted text cannot be recovered.")

    truncated[OFFLOAD_MARKER] = True
    truncated["offload_note"] = note
    truncated["offload_original_chars"] = len(full_text)
    truncated["offload_sha256"] = digest
    if artifact_id:
        truncated["artifact_id"] = artifact_id
    return truncated


class ArtifactAccessDenied(LookupError):
    """A read_artifact_range() call whose id/owner do not check out. Carries
    no distinguishing detail between "no such id" and "not yours" — see the
    module docstring and A13."""


def read_artifact_range(artifact_id: str, *, owner: str, start: int = 0,
                        end: Optional[int] = None,
                        query: Optional[str] = None,
                        context_chars: int = 400) -> Dict[str, Any]:
    """Read back an offloaded result's full text, scoped to `owner` exactly
    the way `routes/artifact_routes.py` scopes a download.

    `start`/`end` slice the text (0-based, `end` exclusive, clamped to the
    text's own bounds). `query`, when given instead, returns the first
    case-insensitive match plus `context_chars` on each side, or an empty
    `matches` list when nothing is found — never an exception for "not
    found within the text" (as opposed to "not found/not yours", which is
    `ArtifactAccessDenied`).
    """
    from src import artifact_identity as identity

    try:
        path = identity.path_for(artifact_id, owner=owner)
    except (identity.ArtifactNotFound, identity.NotTheOwner) as exc:
        # Same exception type's message differs by reason upstream, but
        # every caller of THIS function (a tool handler) collapses both to
        # one generic refusal, exactly like the HTTP routes' 404 — see A13.
        raise ArtifactAccessDenied(f"no artifact {artifact_id!r} available to this owner") from exc

    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()

    if query:
        idx = text.lower().find(query.lower())
        if idx < 0:
            return {"artifact_id": artifact_id, "query": query, "matches": [],
                    "total_chars": len(text)}
        lo = max(0, idx - context_chars)
        hi = min(len(text), idx + len(query) + context_chars)
        return {"artifact_id": artifact_id, "query": query,
                "matches": [{"offset": idx, "text": text[lo:hi]}],
                "total_chars": len(text)}

    lo = max(0, min(int(start or 0), len(text)))
    hi = len(text) if end is None else max(lo, min(int(end), len(text)))
    return {"artifact_id": artifact_id, "start": lo, "end": hi,
            "text": text[lo:hi], "total_chars": len(text)}
