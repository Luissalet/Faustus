"""Evidence inspector routes (BENCH-03).

Faustus attaches an ``EvidenceRef`` (``src/contracts/tool.py``) to a read, a
web fetch, a citation or an artifact whenever the agent points at something
as proof of a claim — the exact file lines it read, the URL and hash of a
page it fetched, and so on (``src/context_ledger.py::evidence_for_read``,
``src/agent_tools/web_tools.py``). What was missing was a way for a person to
open one of those pointers back up: "is this still true, or did the file
move on since?"

This module is the one authority that answers that question. It never
re-derives a hash on its own (``src/context_ledger.py::verify_read_evidence``
already is that authority — reused, not duplicated) and it never quietly
swaps in today's content and calls it the original: the captured evidence is
what the agent actually saw, and only the caller — who already holds that
captured text from the turn that produced it — can compare the two. This
endpoint's job is strictly the other half: fetch what the source says *now*,
for the same locator, and say plainly whether that still matches the hash
the evidence was captured with.

Only ``source_type == "file"`` can be re-read today (the exact byte range
underneath ``EvidenceLocator``'s ``lines``/``whole`` kinds is well-defined
and lives on disk within a bound workspace). Every other source type
(``web``, ``artifact``, ``tool``, ``test``, ``receipt``, ``memory``,
``conversation``) has no single, safe, always-available "current" byte
source to re-fetch from here, so the resolver says so explicitly instead of
guessing — the same "never substitute the source silently" rule, applied to
the case where there is nothing to compare against at all.
"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.auth_helpers import get_current_user
from src.tool_security import owner_is_admin_or_single_user
from src.contracts.tool import EvidenceRef
from src.contracts.base import ContractError
from src.context_ledger import verify_read_evidence


class EvidenceResolveRequest(BaseModel):
    # The full EvidenceRef mapping (EvidenceRef.to_mapping()) the caller
    # already holds from wherever it saw this evidence attached (a tool
    # result, a citation, an artifact record).
    evidence: Dict[str, Any]
    # Needed only for source_type == "file": which bound workspace root to
    # confine the re-read to. Omitted for other source types.
    workspace: Optional[str] = None


def _confine_workspace_file(workspace: str, path: str) -> str:
    """Resolve ``path`` inside ``workspace``, refusing anything outside it.

    Mirrors ``routes/workspace_routes.py::_confine`` (same two underlying
    authorities: ``vet_workspace`` validates the root, then
    ``_resolve_tool_path_in_roots`` confines the path to it) rather than
    reimplementing path confinement a second time.
    """
    from src.tool_execution import vet_workspace, _resolve_tool_path_in_roots

    root = vet_workspace(workspace or "")
    if not root:
        raise HTTPException(status_code=400, detail="workspace is not a valid folder")
    try:
        return _resolve_tool_path_in_roots([root], path, root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _lines_window(text: str, value: str) -> str:
    """The exact 1-based inclusive line range a ``lines`` locator names.

    Matches the range convention ``context_ledger.evidence_for_read`` and
    ``src/read_plan.py`` already use — a single number is a one-line range.
    """
    raw = (value or "").strip()
    try:
        if "-" in raw:
            start_s, end_s = raw.split("-", 1)
            start, end = int(start_s), int(end_s)
        else:
            start = end = int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Malformed lines locator")
    if start < 1 or end < start:
        raise HTTPException(status_code=400, detail="Malformed lines locator")
    lines = text.splitlines()
    return "\n".join(lines[start - 1:end])


def setup_evidence_routes() -> APIRouter:
    router = APIRouter(tags=["evidence"])

    @router.post("/api/evidence/resolve")
    async def resolve_evidence(request: Request, body: EvidenceResolveRequest) -> Dict[str, Any]:
        owner = get_current_user(request)
        if not owner_is_admin_or_single_user(owner):
            raise HTTPException(status_code=403, detail="Evidence inspection is admin-only")

        try:
            evidence = EvidenceRef.from_mapping(body.evidence)
        except ContractError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid evidence reference: {exc}")

        result: Dict[str, Any] = {
            "evidence": evidence.to_mapping(),
            # Whether *this* call could reach a "now" version to compare
            # against at all (not whether it matched).
            "current_available": False,
            "current_content": None,
            # None = not checkable from here (source_type has no re-fetch
            # authority yet); True/False once a real comparison ran.
            "still_valid": None,
            "reason": None,
        }

        if evidence.source_type != "file":
            result["reason"] = (
                f"Re-checking {evidence.source_type!r} evidence live is not "
                "supported yet here; the captured content is the only record."
            )
            return result

        if not body.workspace:
            result["reason"] = "No workspace given; cannot re-read the source file."
            return result

        target = _confine_workspace_file(body.workspace, evidence.source_ref)
        try:
            with open(target, "r", encoding="utf-8", errors="replace") as handle:
                full_text = handle.read()
        except FileNotFoundError:
            result["still_valid"] = False
            result["reason"] = "The source file no longer exists."
            return result
        except IsADirectoryError:
            result["reason"] = "The source path is now a directory, not a file."
            return result
        except OSError as exc:
            result["reason"] = f"Could not read the source file: {exc}"
            return result

        if evidence.locator.kind == "lines":
            window = _lines_window(full_text, evidence.locator.value)
        elif evidence.locator.kind == "whole":
            window = full_text
        else:
            result["reason"] = (
                f"Locator kind {evidence.locator.kind!r} has no defined "
                "re-read for files yet."
            )
            return result

        result["current_available"] = True
        result["current_content"] = window
        if evidence.content_sha256:
            result["still_valid"] = verify_read_evidence(evidence, window)
            if result["still_valid"] is False:
                result["reason"] = (
                    "The file changed since this evidence was captured; "
                    "showing the current text separately, not as the original."
                )
        else:
            result["reason"] = "This evidence carries no content hash to verify against."
        return result

    return router
