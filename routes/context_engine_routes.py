"""The Context Engine over HTTP (plan §19).

`src/context_engine/` is the one place that decides what a model is told. Until
this file existed it decided it only for callers already inside the process:
there was no way to ask "what would the agent know about X?", no way to read
the ledger a compiled packet leaves behind, and no way to write a connectable
block or a blackboard finding from anywhere but Python. The failure the whole
subsystem exists to prevent is "nobody can tell what the model was told", and
an audit trail reachable only from inside the turn that wrote it answers that
question for nobody.

Three rules run through every handler here, each one a specific failure:

* **the owner is never read from the body.** `POST /compile` is an
  administrative door into the same compiler the turn path uses. A compiler
  that accepted `execution.owner` from JSON would be a privilege escalation
  wearing a diagnostic's clothes, so the session's owner is stamped over
  whatever arrives. Caller-supplied mandatory candidates are not accepted at
  all: they carry `trust_class` and `authority`, and minting trust from a
  request body is the same hole in a different field. The policy block *is*
  accepted, because every flag on it defaults to permissive and a caller can
  therefore only narrow what they are given.

* **a refusal is an answer, not a failure.** A block carrying a credential, a
  capsule whose revision moved, a finding with no evidence: each is a 200 with
  `{"ok": false, "error": {"path", "message"}}`, exactly as
  `routes/contracts_routes.py` does it. The 4xx codes stay reserved for a body
  that is not JSON — a caller that cannot tell "your input was refused" from
  "your request was malformed" retries the wrong one, and a conflict retried
  blindly is how the write that beat you gets overwritten.

* **no route returns a packet's text.** `GET /packets/{id}` is the ledger row:
  counts, ids and section totals. The manifest is served because §5.2's
  manifest is one row per item *without* the item — it says where a sentence
  came from and never repeats it.
"""

import logging
import re
from typing import Any, Dict, Mapping, Optional

from fastapi import APIRouter, HTTPException, Request

from core.middleware import require_admin, require_human
from src.auth_helpers import effective_user
from src.context_engine import (
    blocks,
    capsules,
    code_index,
    experiences,
    maintenance,
    manifest,
    multimodal_memory,
    shared_memory,
    store,
)
from src.context_engine.contracts import ContextReceipt, ContextRequest, ContractError

logger = logging.getLogger(__name__)

#: Every exception the subsystems raise to mean "your input was refused". The
#: narrower ones are subclasses and arrive through the same door: `BlockConflict`
#: and `SecretInBlock` are `BlockError`s, `CapsuleConflict` is a `CapsuleError`,
#: and `FindingRejected` is a `ContractError`. Listing the bases keeps the
#: handlers from growing a tuple each.
REFUSALS = (ContractError, blocks.BlockError, capsules.CapsuleError,
            experiences.ExperienceRejected)

#: The ceiling every `limit` is clamped to, as `approvals_routes.py` does it. A
#: listing endpoint with no ceiling is a way to ask one process to hold a
#: hundred thousand rows in memory on behalf of a typo.
MAX_LIMIT = 200

#: Route-level knobs that may travel in the same object as a `ContextRequest`.
#: `ContextRequest.parse` rejects unknown keys on purpose, so these are removed
#: before parsing rather than being silently tolerated by a looser contract.
_REQUEST_KNOBS = ("max_output_tokens", "context_length", "window_known",
                  "messages", "mandatory")


# ── shared helpers ─────────────────────────────────────────────────────────

def _compiler():
    """`src.context_engine.compiler`, imported late.

    Importing it reaches the adapter registry, and through it memory, RAG and
    the provenance graph. The package docstring is explicit that this must not
    be paid by `import src.context_engine`; paying it at app import instead
    would put the same second on the startup of a process that may never
    compile a packet."""
    from src.context_engine import compiler

    return compiler


def _cache():
    """`src.context_engine.cache`, imported late, for the same reason."""
    from src.context_engine import cache

    return cache


def _owner(request: Request) -> str:
    """Who the caller is, for scoping.

    `effective_user` rather than `get_current_user`: for a cookie session the
    two are identical, and for a bearer token it resolves to the human who
    minted it, so a paired client sees the same blocks and packets as that
    person's browser instead of a separate "api"-owned silo. An empty string
    means "do not filter", which is what the ledger's own `_scope_sql` already
    means by it — in auth-disabled single-user mode there is nobody to filter
    against and a scope clause would answer every diagnostic with zero rows."""
    return str(effective_user(request) or "").strip()


def _limit(value: Any, default: int = 50) -> int:
    return max(1, min(_whole(value, default) or default, MAX_LIMIT))


def _whole(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _seconds(value: Any, default: float) -> float:
    """A time budget, kept a float: truncating 2.5s to 2s is a small lie the
    caller did not ask for, and this number is the only thing standing between
    a maintenance pass and a turn waiting behind it."""
    try:
        wanted = float(value)
    except (TypeError, ValueError):
        return default
    return wanted if wanted > 0 else default


async def _json_body(request: Request) -> Dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Body must be JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    return payload


def _refused(exc: Exception) -> Dict[str, Any]:
    """A rejection as a 200 body, following `contracts_routes.py`.

    `path` is read from whichever name the raising module uses — the contracts
    say `path`, `experiences` says `field` — because a caller fixing one field
    should not have to know which subsystem refused it. A conflict also carries
    `revision`: the entire point of optimistic concurrency is that the loser
    can reload and re-apply, and a refusal that does not say what the revision
    is now makes them guess, which is how the winning write gets clobbered."""
    path = getattr(exc, "path", "") or getattr(exc, "field", "") or "<root>"
    message = getattr(exc, "message", "") or getattr(exc, "reason", "") or str(exc)
    error: Dict[str, Any] = {"path": str(path), "message": str(message),
                             "detail": str(exc)}
    out: Dict[str, Any] = {"ok": False, "error": error}
    revision = getattr(exc, "revision", None)
    if isinstance(revision, int) and not isinstance(revision, bool):
        error["revision"] = revision
        out["revision"] = revision
        expected = getattr(exc, "expected", None)
        if isinstance(expected, int) and not isinstance(expected, bool):
            error["expected_revision"] = expected
    return out


def _mine(row_owner: Any, owner: str) -> bool:
    """Is this row the caller's?

    An empty caller owner is single-user or auth-disabled mode and sees
    everything; an owner-less row is install-wide and is seen by everybody.
    Anything else has to match exactly — a lookup by id that skipped this
    check would serve another owner's packet to whoever guessed its id."""
    stored = str(row_owner or "").strip()
    return not owner or not stored or stored == owner


def _context_request(payload: Mapping[str, Any], owner: str) -> ContextRequest:
    """A `ContextRequest` out of the body, with the session's owner stamped on.

    The execution block is rebuilt rather than patched after parsing, so there
    is no ordering in which a body-supplied owner survives. This is §19's one
    hard rule and the only thing separating an administrative compile endpoint
    from a way to read somebody else's memory by asking politely."""
    raw = payload.get("request", payload)
    if not isinstance(raw, Mapping):
        raise ContractError("context_request", "expected an object", got=raw)
    data = {key: value for key, value in raw.items() if key not in _REQUEST_KNOBS}
    execution = dict(data.get("execution") or {})
    execution["owner"] = owner
    data["execution"] = execution
    return ContextRequest.parse(data)


def _remember_manifest(ctx_request: ContextRequest, packet: Any) -> None:
    """Keep a compiled packet's manifest beside its ledger row.

    §24 keeps the ledger to numbers and ids so it can be pruned by age without
    losing the audit trail, which means the per-item rows live nowhere durable.
    They belong in the working set rather than in SQLite: they are rebuildable
    by recompiling, they are almost always read seconds after they were
    written, and a per-item table in an audit store grows forever to answer a
    question that stops being asked. A miss is reported as a miss."""
    try:
        cache = _cache()
        cache.working_set().put(
            cache.scope_of(ctx_request),
            cache.PREFIX_PACKET + packet.packet_id,
            {"owner": packet.owner, "manifest": packet.manifest(),
             "summary": manifest.summarize(packet)},
            cost_bytes=4096)
    except Exception:  # noqa: BLE001 - the cache is never load-bearing
        logger.debug("context routes could not cache a manifest", exc_info=True)


def _recall_manifest(packet_id: str, owner: str) -> Optional[Dict[str, Any]]:
    """The cached manifest for a packet, verified against its ledger owner.

    The scope key carries the council and the branch and cannot be rebuilt from
    a packet id, so the scopes are walked — and every candidate is checked
    against the owner the ledger recorded, because a scan that trusted whatever
    it found would be the leak `cache.py` spends its whole design preventing."""
    try:
        cache = _cache()
        working = cache.working_set()
        key = cache.PREFIX_PACKET + str(packet_id)
        for scope in working.scopes():
            value = working.get(scope, key)
            if isinstance(value, dict) and str(value.get("owner") or "") == owner:
                return value
    except Exception:  # noqa: BLE001 - a cache miss is not a failure
        logger.debug("context routes could not recall a manifest", exc_info=True)
    return None


# ── the router ─────────────────────────────────────────────────────────────

def setup_context_engine_routes():
    router = APIRouter(prefix="/api/context", tags=["context"])

    # ── compiling ──────────────────────────────────────────────────────────

    @router.post("/compile")
    async def compile_context(request: Request):
        """Compile one packet and answer with its manifest, never its text.

        Administrative and diagnostic: this is how "what would the agent know
        about X?" gets asked without running a turn. The owner comes from the
        session and is written over whatever the body says; `mandatory` is
        dropped for the same reason, since a caller-supplied candidate carries
        its own `trust_class`."""
        require_admin(request)
        payload = await _json_body(request)
        try:
            ctx_request = _context_request(payload, _owner(request))
        except REFUSALS as exc:
            return _refused(exc)

        packet = await _compiler().compile_packet(
            ctx_request,
            max_output_tokens=_whole(payload.get("max_output_tokens")),
            context_length=_whole(payload.get("context_length")),
            window_known=bool(payload.get("window_known") or False),
        )
        _remember_manifest(ctx_request, packet)
        return {
            "ok": True,
            "packet_id": packet.packet_id,
            "request_id": packet.request_id,
            "owner": packet.owner,
            "degraded": packet.degraded,
            "summary": manifest.summarize(packet),
            "manifest": packet.manifest(),
            "omissions": [o.to_dict() for o in packet.omissions],
        }

    @router.post("/shadow")
    async def shadow_context(request: Request):
        """Compile a packet, deliver nothing, and say what the difference is.

        The instrument that measures the baseline before the hot path changes:
        no ledger row, no receipt, no prompt. `messages` is what the caller
        actually sent; both sides are counted with the same estimator, because
        a difference produced by two different rulers is not a difference."""
        require_admin(request)
        payload = await _json_body(request)
        try:
            ctx_request = _context_request(payload, _owner(request))
        except REFUSALS as exc:
            return _refused(exc)
        messages = payload.get("messages") or []
        if not isinstance(messages, list):
            raise HTTPException(status_code=400, detail="messages must be a list")

        report = await _compiler().compiler().shadow(
            ctx_request,
            messages=[m for m in messages if isinstance(m, Mapping)],
            max_output_tokens=_whole(payload.get("max_output_tokens")),
            context_length=_whole(payload.get("context_length")),
            window_known=bool(payload.get("window_known") or False),
        )
        # The packet exists only inside the report and carries every body it
        # compiled; the manifest says the same thing without the text.
        packet = report.pop("packet", None)
        if isinstance(packet, dict):
            report["manifest"] = packet.get("manifest") or []
            report["omissions"] = packet.get("omissions") or []
        # `shadow` swallows its own failures and reports them, because a
        # measurement may never break a turn. Reporting that as `ok: true`
        # would make a broken instrument look like a clean baseline.
        return {"ok": not report.get("error"), **report}


    # ── the ledger ─────────────────────────────────────────────────────────

    @router.get("/packets")
    def list_packets(request: Request, project_id: str = "", limit: int = 50):
        """The newest ledger rows for this owner. Numbers and ids only."""
        owner = _owner(request)
        rows = _compiler().recent_packets(owner=owner, project_id=project_id,
                                          limit=_limit(limit))
        return {"ok": True, "owner": owner, "packets": rows, "count": len(rows)}

    @router.get("/packets/{packet_id}")
    def get_packet(packet_id: str, request: Request):
        """One ledger row: how big, how degraded, what was left out — never
        what it said. The bodies were never stored, and the reason is the same
        reason the ledger can be pruned by age without losing the audit."""
        row = _compiler().get_packet_row(packet_id)
        if row is None or not _mine(row.get("owner"), _owner(request)):
            raise HTTPException(status_code=404, detail="No such packet")
        return {"ok": True, "packet": row}

    @router.get("/packets/{packet_id}/manifest")
    def get_packet_manifest(packet_id: str, request: Request):
        """One row per injected item, without the item.

        `retained: false` is an honest miss, not an empty packet: the manifest
        lives in the working set, so a restart, an eviction or another process
        loses it while the ledger row survives. Saying "recompile" beats
        answering a question about provenance with an empty list."""
        row = _compiler().get_packet_row(packet_id)
        if row is None or not _mine(row.get("owner"), _owner(request)):
            raise HTTPException(status_code=404, detail="No such packet")
        cached = _recall_manifest(packet_id, str(row.get("owner") or ""))
        if cached is not None:
            return {"ok": True, "packet_id": packet_id, "retained": True,
                    "manifest": cached.get("manifest") or [],
                    "summary": cached.get("summary") or {}}
        return {
            "ok": True, "packet_id": packet_id, "retained": False,
            "manifest": None,
            "section_tokens": row.get("section_tokens") or {},
            "omission_counts": row.get("omission_counts") or {},
            "note": ("the ledger keeps counts, not rows: the per-item manifest "
                     "lives in the working set and this packet's has been "
                     "evicted. Recompile the request to see it again."),
        }

    @router.get("/packets/{packet_id}/items/{item_id}/fragment")
    async def get_manifest_item_fragment(packet_id: str, item_id: str, request: Request):
        """CTX-03: the exact fragment ONE manifest row cites, reopened live —
        Context.tsx's ManifestPane "Ver fragmento" button.

        Looks `item_id` up in the same cached manifest `GET .../manifest`
        already serves (so the same `retained: false` honesty applies: an
        evicted packet has no rows left to look one up in), then resolves
        its `source_ref` through the SAME adapter registry `POST
        /sources/fetch` uses (`context_engine.candidates.fetch_ref`) —
        `file:` via the files adapter (itself backed by `src/read_plan.py`),
        `doc:...#chunk` via the documents adapter, `session:...#idx` via the
        sessions adapter, `mem:` via the memory adapter. No second, ad-hoc
        per-prefix resolver: one fragment-reopening path for the whole
        server, this route just starts it from an item_id instead of a raw
        ref the caller already has to have."""
        owner = _owner(request)
        row = _compiler().get_packet_row(packet_id)
        if row is None or not _mine(row.get("owner"), owner):
            raise HTTPException(status_code=404, detail="No such packet")
        cached = _recall_manifest(packet_id, str(row.get("owner") or ""))
        if cached is None:
            return {"ok": True, "packet_id": packet_id, "item_id": item_id,
                    "resolvable": False, "text": "",
                    "note": ("the ledger keeps counts, not rows: the per-item "
                             "manifest lives in the working set and this "
                             "packet's has been evicted.")}
        item = next(
            (it for it in (cached.get("manifest") or [])
             if str(it.get("context_item_id") or "") == item_id),
            None,
        )
        if item is None:
            raise HTTPException(status_code=404, detail="No such manifest item")
        source_ref = str(item.get("source_ref") or "").strip()
        if not source_ref:
            return {"ok": True, "packet_id": packet_id, "item_id": item_id,
                    "resolvable": False, "text": ""}
        from src.context_engine import candidates as _candidates
        ctx_request = _context_request({}, owner)
        retrieval = _candidates.RetrievalRequest(request=ctx_request,
                                                 explicit_refs=(source_ref,))
        candidate = await _candidates.fetch_ref(source_ref, retrieval)
        if candidate is None or (candidate.owner and not _mine(candidate.owner, owner)):
            return {"ok": True, "packet_id": packet_id, "item_id": item_id,
                    "resolvable": False, "text": ""}
        return {"ok": True, "packet_id": packet_id, "item_id": item_id,
                "resolvable": True, "text": candidate.body}

    @router.post("/receipts")
    async def post_receipt(request: Request):
        """What happened to a packet after it was delivered (§1.5).

        Observed use and declared use stay in the fields the contract puts them
        in: merging them would let a model award itself credit for a memory it
        never read, and that credit becomes training signal for the curator."""
        require_admin(request)
        payload = await _json_body(request)
        try:
            receipt = ContextReceipt.parse(payload.get("receipt", payload))
        except REFUSALS as exc:
            return _refused(exc)
        row = _compiler().get_packet_row(receipt.packet_id)
        if row is not None and not _mine(row.get("owner"), _owner(request)):
            raise HTTPException(status_code=404, detail="No such packet")
        _compiler().record_receipt(receipt)
        return {"ok": True, "receipt": receipt.to_dict()}


    # ── connectable blocks (§7) ────────────────────────────────────────────

    def _owned_block(block_id: str, owner: str) -> blocks.ContextBlock:
        block = blocks.get_block(block_id)
        if block is None or not _mine(block.owner, owner):
            raise HTTPException(status_code=404, detail="No such block")
        return block

    @router.get("/blocks")
    def list_blocks(request: Request, project_id: str = "", type: str = "",  # noqa: A002
                    scope: str = "", limit: int = 50):
        """This owner's blocks, highest priority first."""
        owner = _owner(request)
        rows = blocks.list_blocks(owner=owner, project_id=project_id, type=type,
                                  scope=scope, limit=_limit(limit))
        return {"ok": True, "owner": owner, "count": len(rows),
                "blocks": [b.to_dict() for b in rows]}

    @router.get("/blocks/audit")
    def audit_blocks(request: Request, project_id: str = ""):
        """Size, duplication and contradiction — the report that makes the
        always-loaded ration humane. Going over the cap is reported here, not
        refused at write time: refusing would mean losing the block somebody
        just wrote, and "why is my block not loading?" deserves a list."""
        owner = _owner(request)
        return {"ok": True, "audit": blocks.audit(owner=owner, project_id=project_id)}

    @router.post("/blocks")
    async def create_block(request: Request):
        """Write a block. Refuses one carrying a credential, by name.

        `owner` is the session's and is not read from the body — a block is
        pasted into prompts, so being able to write one into somebody else's
        scope is being able to write their system prompt."""
        require_admin(request)
        payload = await _json_body(request)
        fields = {name: payload[name] for name in blocks.MUTABLE_FIELDS
                  if name in payload}
        try:
            block = blocks.create_block(owner=_owner(request), **fields)
        except REFUSALS as exc:
            return _refused(exc)
        return {"ok": True, "block": block.to_dict()}

    @router.patch("/blocks/{block_id}")
    async def update_block(block_id: str, request: Request):
        """Apply updates and bump the revision.

        `expected_revision` is optimistic concurrency, not advice: two agents
        editing the same `working_state` block is the normal case, and
        last-write-wins loses the first one silently."""
        require_admin(request)
        payload = await _json_body(request)
        owner = _owner(request)
        _owned_block(block_id, owner)
        updates = payload.get("updates", {name: payload[name]
                                           for name in blocks.MUTABLE_FIELDS
                                           if name in payload})
        expected = payload.get("expected_revision", None)
        try:
            block = blocks.update_block(
                block_id, updates,
                expected_revision=None if expected is None else expected)
        except REFUSALS as exc:
            return _refused(exc)
        return {"ok": True, "block": block.to_dict()}

    @router.delete("/blocks/{block_id}")
    def delete_block(block_id: str, request: Request):
        """Remove a block and every attachment pointing at it."""
        require_admin(request)
        _owned_block(block_id, _owner(request))
        try:
            removed = blocks.delete_block(block_id)
        except REFUSALS as exc:
            return _refused(exc)
        return {"ok": True, "deleted": bool(removed)}

    @router.post("/blocks/{block_id}/attach")
    async def attach_block(block_id: str, request: Request):
        """Connect a block to a session and/or an agent.

        `expires_at` is what makes a temporary connection safe to make: "load
        the incident notes for the next hour" should stop being true in an hour
        without anybody remembering to detach it."""
        require_admin(request)
        payload = await _json_body(request)
        _owned_block(block_id, _owner(request))
        try:
            blocks.attach(block_id,
                          session_id=str(payload.get("session_id") or ""),
                          agent_id=str(payload.get("agent_id") or ""),
                          expires_at=str(payload.get("expires_at") or ""))
        except REFUSALS as exc:
            return _refused(exc)
        return {"ok": True, "attachments": blocks.attachments(block_id)}

    @router.post("/blocks/{block_id}/detach")
    async def detach_block(block_id: str, request: Request):
        """Disconnect. Detaching what was never attached is not an error."""
        require_admin(request)
        payload = await _json_body(request)
        _owned_block(block_id, _owner(request))
        try:
            blocks.detach(block_id,
                          session_id=str(payload.get("session_id") or ""),
                          agent_id=str(payload.get("agent_id") or ""))
        except REFUSALS as exc:
            return _refused(exc)
        return {"ok": True, "attachments": blocks.attachments(block_id)}

    @router.post("/blocks/import-project-memory")
    async def import_project_memory(request: Request):
        """Propose one block per `.odysseus/` note; import them only when a
        person says so.

        `dry_run` is the default and stays admin, because a proposal costs
        nothing. `dry_run: false` writes standing context that will be pasted
        into prompts from then on, so it is `require_human` — the model does
        not get to decide which of its own notes become rules."""
        require_admin(request)
        payload = await _json_body(request)
        dry_run = bool(payload.get("dry_run", True))
        if not dry_run:
            require_human(request)
        try:
            report = blocks.import_project_memory(
                payload.get("project") or {}, owner=_owner(request), dry_run=dry_run)
        except REFUSALS as exc:
            return _refused(exc)
        return {"ok": True, "report": report}


    # ── capsules (§8) ──────────────────────────────────────────────────────

    @router.get("/capsules/{scope_id}")
    def get_capsule(scope_id: str, request: Request, render: bool = False):
        """What one worker needs to pick this scope up cold."""
        capsule = capsules.load(scope_id, owner=_owner(request))
        if capsule is None:
            raise HTTPException(status_code=404, detail="No such capsule")
        out = {"ok": True, "capsule": capsule.to_dict()}
        if render:
            out["rendered"] = capsules.render(capsule)
        return out

    @router.get("/capsules/{scope_id}/validate")
    def validate_capsule(scope_id: str, request: Request, workspace: str = ""):
        """Check the capsule's references against the world, and delete nothing.

        A claim on a file that was renamed is the trace of work somebody did;
        dropping it silently turns a question a human can answer into a gap
        nobody knows exists. A reference this cannot resolve is `unchecked`,
        not missing — not owning a namespace is not knowing the thing is gone."""
        capsule = capsules.load(scope_id, owner=_owner(request))
        if capsule is None:
            raise HTTPException(status_code=404, detail="No such capsule")
        return {"ok": True, "validation": capsules.validate(capsule, workspace=workspace)}

    @router.get("/capsules/{scope_id}/log")
    def capsule_log(scope_id: str, request: Request, limit: int = 50):
        """The append-only trail, newest first.

        Scoped on the log's own `owner` column rather than on the capsule's,
        because `DELETE` removes the checkpoint and keeps the record of how it
        got that way — a trail that 404s once the capsule is gone is not a
        trail."""
        owner = _owner(request)
        rows = [row for row in capsules.log(scope_id, limit=_limit(limit))
                if _mine(row.get("owner"), owner)]
        return {"ok": True, "entries": rows, "count": len(rows)}

    @router.post("/capsules/{scope_id}/deltas")
    async def apply_capsule_deltas(scope_id: str, request: Request):
        """Apply a batch of typed deltas. One bad delta does not lose the batch.

        `ensure: true` creates the capsule first, which is what a worker
        checkpointing for the first time wants; without it a scope with no
        capsule is refused rather than invented, so a typo in `scope_id` is an
        error instead of a second empty capsule nobody reads."""
        require_admin(request)
        payload = await _json_body(request)
        owner = _owner(request)
        deltas = payload.get("deltas") or []
        if not isinstance(deltas, list):
            raise HTTPException(status_code=400, detail="deltas must be a list")
        expected = payload.get("expected_revision", None)
        try:
            if bool(payload.get("ensure") or False):
                capsules.ensure(scope_id, owner=owner,
                                project_id=str(payload.get("project_id") or ""),
                                objective=str(payload.get("objective") or ""))
            result = capsules.apply_deltas(
                scope_id, [d for d in deltas if isinstance(d, Mapping)],
                actor=str(payload.get("actor") or "") or (owner or "unknown"),
                expected_revision=None if expected is None else expected,
                owner=owner)
        except REFUSALS as exc:
            return _refused(exc)
        return {"ok": True, **result}

    @router.delete("/capsules/{scope_id}")
    def delete_capsule(scope_id: str, request: Request):
        """Drop the capsule. The log stays: append-only means append-only."""
        require_admin(request)
        try:
            removed = capsules.delete(scope_id, owner=_owner(request))
        except REFUSALS as exc:
            return _refused(exc)
        return {"ok": True, "deleted": bool(removed)}


    # ── experiences (§9) ───────────────────────────────────────────────────

    def _owned_experience(exp_id: str, owner: str) -> experiences.Experience:
        exp = experiences.get(exp_id)
        if exp is None or not _mine(exp.owner, owner):
            raise HTTPException(status_code=404, detail="No such experience")
        return exp

    @router.get("/experiences")
    def search_experiences(request: Request, query: str = "", project_id: str = "",
                           intent: str = "", technologies: str = "", k: int = 5):
        """A few contrasting experiences for a task: patterns and the failures.

        Deliberately does not fill `k` — five near-identical experiences are a
        budget spent on repetition — and never returns `unproved` rows, which
        are kept as history rather than recommended as approaches."""
        owner = _owner(request)
        techs = [t.strip() for t in str(technologies or "").split(",") if t.strip()]
        hits = experiences.search(query, owner=owner, project_id=project_id,
                                  intent=intent, technologies=techs,
                                  k=_limit(k, 5))
        return {"ok": True, "owner": owner, "count": len(hits), "experiences": hits}

    @router.get("/experiences/{exp_id}")
    def get_experience(exp_id: str, request: Request):
        exp = _owned_experience(exp_id, _owner(request))
        return {"ok": True, "experience": {**exp.to_dict(), "role": exp.role(),
                                           "stale": exp.stale(),
                                           "usefulness": exp.usefulness()}}

    @router.post("/experiences/{exp_id}/feedback")
    async def experience_feedback(exp_id: str, request: Request):
        """Record that an experience helped or hurt, with what says so.

        `ref` should point at something durable — a packet id, a changeset id,
        a proof identity — because the counter is only answerable if somebody
        can ask "helpful according to whom?"."""
        require_admin(request)
        payload = await _json_body(request)
        _owned_experience(exp_id, _owner(request))
        kind = str(payload.get("kind") or "")
        updated = experiences.feedback(exp_id, kind,
                                       ref=str(payload.get("ref") or ""),
                                       weight=payload.get("weight", 1.0))
        if updated is None:
            return {"ok": False, "error": {
                "path": "feedback.kind",
                "message": f"expected one of {list(experiences.FEEDBACK_KINDS)}",
                "detail": f"got {kind!r}"}}
        return {"ok": True, "experience": updated.to_dict()}

    @router.delete("/experiences/{exp_id}")
    def delete_experience(exp_id: str, request: Request):
        require_admin(request)
        _owned_experience(exp_id, _owner(request))
        return {"ok": True, "deleted": bool(experiences.delete(exp_id))}

    # ── the code index (§10) ───────────────────────────────────────────────
    #
    # These three are the only routes here with no owner filter, and that is
    # deliberate rather than forgotten: the index is keyed by workspace, not by
    # person. What it holds is the shape of files already on this machine's
    # disk, so scoping it by owner would invent an isolation the filesystem
    # underneath does not have — and would hide a colleague's checkout from the
    # only tool that can tell you where a symbol lives.

    @router.get("/code-index/status")
    def code_index_status(request: Request, workspace: str = "", project_id: str = ""):
        """How much of a workspace is indexed, and how old the answer is."""
        return {"ok": True, "status": code_index.status(workspace, project_id=project_id)}

    @router.post("/code-index/refresh")
    async def code_index_refresh(request: Request):
        """Bring the index up to date and say what that cost.

        Incremental unless `full`. `truncated: true` is the honest answer for a
        monorepo whose walk ran out of file budget, and much better than the
        twenty minutes the alternative takes."""
        require_admin(request)
        payload = await _json_body(request)
        paths = payload.get("paths", None)
        result = code_index.refresh(
            str(payload.get("workspace") or ""),
            project_id=str(payload.get("project_id") or ""),
            paths=[str(p) for p in paths] if isinstance(paths, list) else None,
            full=bool(payload.get("full") or False))
        return {"ok": True, "refresh": result}

    @router.get("/code-index/search")
    def code_index_search(request: Request, query: str = "", workspace: str = "",
                          project_id: str = "", k: int = 12, kinds: str = ""):
        """Symbols that match, most likely first (IDX-03): lexical (BM25) +
        symbol identity + a model-free embedding lane, fused and reranked —
        degrading cleanly to name/path/text scoring alone when nothing can
        be vectorised. Each hit carries its own `tier`/`lanes` so a caller
        can tell how far the ranking had to degrade without a second call."""
        wanted = [s.strip() for s in str(kinds or "").split(",") if s.strip()]
        hits = code_index.search(query, workspace=workspace, project_id=project_id,
                                 k=_limit(k, 12), kinds=wanted)
        return {"ok": True, "count": len(hits), "symbols": hits}


    # ── the shared blackboard (§11) ────────────────────────────────────────

    def _owned_finding(finding_id: str, owner: str) -> shared_memory.Finding:
        finding = shared_memory.get(finding_id)
        if finding is None or not _mine(finding.owner, owner):
            raise HTTPException(status_code=404, detail="No such finding")
        return finding

    @router.get("/findings")
    def search_findings(request: Request, scope: str = "", topic: str = "",
                        kind: str = "", tags: str = "", status: str = "open",
                        query: str = "", k: int = 20):
        """Findings still being served, newest first.

        `status=""` drops the status filter and still hides superseded and
        withdrawn rows: those are history, and history is what a lookup by id
        is for."""
        owner = _owner(request)
        wanted = [t.strip() for t in str(tags or "").split(",") if t.strip()]
        hits = shared_memory.search(scope=scope, owner=owner, topic=topic, kind=kind,
                                    tags=wanted, status=status, query=query,
                                    k=_limit(k))
        return {"ok": True, "owner": owner, "count": len(hits),
                "findings": [f.to_dict() for f in hits]}

    @router.post("/findings")
    async def post_finding(request: Request):
        """Put a finding on the board. A fact or a result with no evidence is
        refused, because an unbacked claim other workers will read as current
        is the thing a blackboard exists to keep out."""
        require_admin(request)
        payload = await _json_body(request)
        owner = _owner(request)
        fields = {name: payload[name] for name in
                  ("scope", "project_id", "topic", "kind", "claim",
                   "evidence_refs", "tags", "status", "expires_at")
                  if name in payload}
        fields["owner"] = owner
        fields["author"] = str(payload.get("author") or "") or (owner or "unknown")
        try:
            finding = shared_memory.post(**fields)
        except REFUSALS as exc:
            return _refused(exc)
        return {"ok": True, "finding": finding.to_dict()}

    @router.post("/findings/{finding_id}/supersede")
    async def supersede_finding(finding_id: str, request: Request):
        """Correct a finding by writing a new one, never by editing the old.

        Only its own author may do this. A peer who disagrees posts an
        `objection`, which reads as a disagreement rather than as the other
        worker having changed their mind."""
        require_admin(request)
        payload = await _json_body(request)
        owner = _owner(request)
        target = _owned_finding(finding_id, owner)
        refs = payload.get("evidence_refs") or []
        try:
            correction = shared_memory.supersede(
                finding_id,
                author=str(payload.get("author") or "") or target.author,
                claim=str(payload.get("claim") or ""),
                evidence_refs=[str(r) for r in refs] if isinstance(refs, list) else [],
                reason=str(payload.get("reason") or ""))
        except REFUSALS as exc:
            return _refused(exc)
        return {"ok": True, "finding": correction.to_dict()}

    @router.post("/findings/{finding_id}/withdraw")
    async def withdraw_finding(finding_id: str, request: Request):
        """Take back your own finding. The row stays and says it was taken
        back: everybody who already read it needs to see that, and by whom."""
        require_admin(request)
        payload = await _json_body(request)
        owner = _owner(request)
        target = _owned_finding(finding_id, owner)
        try:
            withdrawn = shared_memory.withdraw(
                finding_id,
                author=str(payload.get("author") or "") or target.author,
                reason=str(payload.get("reason") or ""))
        except REFUSALS as exc:
            return _refused(exc)
        return {"ok": True, "finding": withdrawn.to_dict()}

    # ── generation recipes (§12) ───────────────────────────────────────────

    @router.get("/recipes")
    def search_recipes(request: Request, query: str = "", project_id: str = "",
                       media_type: str = "", models: str = "", k: int = 5):
        """Recipes worth reusing, hard-filtered first and ranked second.

        `models` is what this machine can actually run: media type, status and
        model availability decide what is even a candidate, so a recipe for a
        checkpoint nobody has is not offered as a suggestion."""
        owner = _owner(request)
        available = [m.strip() for m in str(models or "").split(",") if m.strip()]
        hits = multimodal_memory.search(query, owner=owner, project_id=project_id,
                                        media_type=media_type,
                                        available_models=available, k=_limit(k, 5))
        return {"ok": True, "owner": owner, "count": len(hits), "recipes": [
            {"recipe": hit["recipe"].to_dict(), "score": hit["score"],
             "signals": hit["signals"], "compatible": hit["compatible"]}
            for hit in hits
        ]}

    @router.get("/recipes/{recipe_id}")
    def get_recipe(recipe_id: str, request: Request):
        recipe = multimodal_memory.get(recipe_id)
        if recipe is None or not _mine(recipe.owner, _owner(request)):
            raise HTTPException(status_code=404, detail="No such recipe")
        return {"ok": True, "recipe": recipe.to_dict(),
                "reproducibility": recipe.reproducibility(),
                "compact": recipe.compact()}


    # ── maintenance and diagnostics (§24) ──────────────────────────────────

    @router.post("/maintenance/run")
    async def run_maintenance(request: Request):
        """Run the background pass now.

        `require_human`, not `require_admin`: this prunes the ledger, drops
        index rows, expires findings and vacuums the store. Every one of those
        is irreversible, none of them is urgent, and the model reaching admin
        routes through loopback by design means the gate has to be the one the
        internal token does not open. The pass yields to a turn in flight on
        its own; a task that did not run says so and is still `ok`."""
        require_human(request)
        payload = await _json_body(request)
        names = payload.get("names") or ()
        results = maintenance.run(
            [str(n) for n in names] if isinstance(names, list) else (),
            owner=_owner(request),
            project_id=str(payload.get("project_id") or ""),
            workspace=str(payload.get("workspace") or ""),
            budget_s=_seconds(payload.get("budget_s"), 30.0))
        return {"ok": True, "results": [r.to_dict() for r in results],
                "last_run": maintenance.last_run(), "due": maintenance.due()}

    @router.get("/diagnostics")
    def diagnostics(request: Request, project_id: str = "", workspace: str = ""):
        """Is the compiler degrading, and what is it dropping?

        Deliberately a small number of blunt numbers: the question this has to
        answer at three in the morning is not answered faster by a report with
        forty fields. `sources.unavailable` is declared-minus-built, so an
        adapter whose import failed is named rather than silently missing —
        degradation is declared, never silent."""
        owner = _owner(request)
        out: Dict[str, Any] = {
            "ok": True,
            "compiler": _compiler().diagnostics(owner=owner, project_id=project_id),
            "store": {"bytes": store.store_bytes(), "tables": store.table_counts()},
            "maintenance": {"last_run": maintenance.last_run(), "due": maintenance.due()},
        }
        try:
            cache = _cache()
            stats = cache.working_set().stats()
            looks = stats.hits + stats.misses
            out["cache"] = {
                "hits": stats.hits, "misses": stats.misses,
                "evictions": stats.evictions, "entries": stats.entries,
                "bytes": stats.bytes,
                "hit_rate": round(stats.hits / looks, 4) if looks else 0.0,
            }
        except Exception:  # noqa: BLE001 - a diagnostic may never raise
            logger.debug("context diagnostics could not read the cache", exc_info=True)
            out["cache"] = {"error": "the working set could not be read"}
        try:
            from src.context_engine import candidates, planner

            candidates.default_sources()
            built = set(candidates.registered_sources())
            declared = set(planner.known_sources())
            out["sources"] = {"declared": sorted(declared), "built": sorted(built),
                              "unavailable": sorted(declared - built)}
        except Exception:  # noqa: BLE001 - see above
            logger.debug("context diagnostics could not survey the sources",
                         exc_info=True)
            out["sources"] = {"error": "the source registry could not be read"}
        if workspace:
            out["code_index"] = code_index.status(workspace, project_id=project_id)
        return out

    # ── CTX-06: health without another LLM ──────────────────────────────────

    @router.post("/health")
    async def context_health(request: Request):
        """The three deterministic signals over a message list a caller
        already has in hand (the transcript about to be — or just — sent):
        repetition, stale evidence, tool-output ratio, and one actionable
        flag list. See `src/context_health.py` for what is and is not
        measured this way."""
        require_admin(request)
        payload = await _json_body(request)
        messages = payload.get("messages") or []
        if not isinstance(messages, list):
            raise HTTPException(status_code=400, detail="messages must be a list")
        from src import context_health

        report = context_health.health(
            [m for m in messages if isinstance(m, Mapping)],
            payload.get("tool_schemas") or (),
            context_length=_whole(payload.get("context_length")),
            model=str(payload.get("model") or ""))
        return {"ok": True, **report}

    @router.post("/reconstruct-task")
    async def reconstruct_task(request: Request):
        """The "reconstruir tarea" button: goal / constraints / changes /
        next_action, read back from whatever survived compaction. Never
        regenerates anything — see `src/context_health.reconstruct_task`."""
        require_admin(request)
        payload = await _json_body(request)
        messages = payload.get("messages") or []
        if not isinstance(messages, list):
            raise HTTPException(status_code=400, detail="messages must be a list")
        from src import context_health

        task = context_health.reconstruct_task([m for m in messages if isinstance(m, Mapping)])
        return {"ok": True, "task": task}

    # ── PERF-03: cache control ──────────────────────────────────────────────

    @router.post("/cache/invalidate")
    async def invalidate_cache(request: Request):
        """"Liberar cache" / "recalcular": forget this owner's entries (or one
        `scope`/`prefix` of them). Never touches any other owner's scope —
        `WorkingSet.invalidate` already refuses to guess one from an empty
        argument (see `src/context_engine/cache.py`)."""
        require_admin(request)
        payload = await _json_body(request)
        owner = _owner(request)
        scope = str(payload.get("scope") or "") or f"{owner}|" + str(payload.get("project_id") or "")
        prefix = str(payload.get("prefix") or "")
        dropped = _cache().working_set().invalidate(scope, prefix=prefix)
        return {"ok": True, "scope": scope, "prefix": prefix, "dropped": dropped}

    # ── CTX-05: user-controlled retrieval scope ─────────────────────────────

    @router.get("/selection")
    def list_selection(request: Request, project_id: str = "", session_id: str = ""):
        """Every exclude/exclude_prefix/pin control in play for this owner at
        this project/session scope, broadest to narrowest — see
        `src/context_selection.list_controls`."""
        from src import context_selection as selection

        owner = _owner(request)
        return {"ok": True, "owner": owner,
                "controls": selection.list_controls(owner, project_id=project_id,
                                                     session_id=session_id)}

    @router.post("/selection")
    async def set_selection(request: Request):
        """"No usar esta fuente" / "usar este fragmento". Never deletes or
        even reads the named source — only whether retrieval offers it."""
        require_admin(request)
        payload = await _json_body(request)
        from src import context_selection as selection

        try:
            record = selection.set_control(
                _owner(request), str(payload.get("kind") or ""),
                str(payload.get("ref") or ""),
                project_id=str(payload.get("project_id") or ""),
                session_id=str(payload.get("session_id") or ""))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"ok": True, "control": record}

    @router.delete("/selection")
    async def unset_selection(request: Request):
        """Undo one control — "sin memoria automatica" stays a session-level
        `no_memory` flag on the chat request itself (unchanged, see
        `context_engine/wiring.py::build_request`); this only ever removes an
        exclude/pin row, never a memory or a file."""
        require_admin(request)
        payload = await _json_body(request)
        from src import context_selection as selection

        removed = selection.unset_control(
            _owner(request), str(payload.get("kind") or ""),
            str(payload.get("ref") or ""),
            project_id=str(payload.get("project_id") or ""),
            session_id=str(payload.get("session_id") or ""))
        return {"ok": True, "removed": removed}

    # ── CTX-03: reopening a citation and searching inside a big read ───────

    @router.post("/sources/fetch")
    async def fetch_source_fragment(request: Request):
        """The exact fragment a summary or manifest row cited, reopened live.

        A manifest row (`GET /packets/{id}/manifest`) or a compaction marker
        only ever carries a `source_ref` — the module docstring is explicit
        that no route here returns a packet's *compiled* text. This is not
        that: it reopens the one source the ref names — the file, memory row
        or symbol it points at — through whichever adapter owns that prefix
        (`context_engine.candidates.fetch_ref`), so the model can go back to
        what a summary was actually built from even after the active window
        moved on. A ref nothing can resolve (the file moved, the memory was
        deleted, no source claims the prefix) is a clean `retained: false`,
        not an error — provenance that cannot be reopened is a normal outcome
        the caller has to handle, not a bug."""
        require_admin(request)
        payload = await _json_body(request)
        source_ref = str(payload.get("source_ref") or "").strip()
        if not source_ref:
            raise HTTPException(status_code=400, detail="source_ref is required")
        owner = _owner(request)
        try:
            ctx_request = _context_request(payload, owner)
        except REFUSALS as exc:
            return _refused(exc)
        from src.context_engine import candidates as _candidates

        retrieval = _candidates.RetrievalRequest(request=ctx_request,
                                                 explicit_refs=(source_ref,))
        candidate = await _candidates.fetch_ref(source_ref, retrieval)
        if candidate is None:
            return {"ok": True, "source_ref": source_ref, "retained": False,
                    "candidate": None}
        if candidate.owner and not _mine(candidate.owner, owner):
            # The ref resolved, but to something scoped to another owner —
            # answered exactly like a missing packet: no distinction between
            # "gone" and "not yours" is given to the caller.
            return {"ok": True, "source_ref": source_ref, "retained": False,
                    "candidate": None}
        return {"ok": True, "source_ref": source_ref, "retained": True,
                "candidate": {
                    "source_type": candidate.source_type,
                    "source_ref": candidate.source_ref,
                    "title": candidate.title,
                    "body": candidate.body,
                    "source_revision": candidate.source_revision,
                    "observed_at": candidate.observed_at,
                    "degraded": candidate.degraded,
                }}

    @router.post("/read/search")
    async def search_read_output(request: Request):
        """"Buscar dentro del output" of a big read — CTX-03's other gap.

        `src/read_plan.py` answers an un-ranged read of an oversized file with
        a symbol index and the head, plus the literal call to read any other
        range. What it never offered is *finding* the range worth reading:
        a model that knows the name it wants (a string, an error message, a
        rare identifier) had no way to locate it except guessing offsets. This
        streams the file — never loading more than one pass of it into memory
        — and returns matches with line numbers and a few lines of context,
        budgeted like every other read-plan output so a large hit count does
        not blow the caller's window."""
        require_admin(request)
        payload = await _json_body(request)
        path = str(payload.get("path") or "").strip()
        query = str(payload.get("query") or "")
        if not path or not query:
            raise HTTPException(status_code=400, detail="path and query are required")
        from src import read_plan

        try:
            result = read_plan.search_in_file(
                path, query,
                window_tokens=_whole(payload.get("window_tokens")),
                regex=bool(payload.get("regex") or False),
                case_sensitive=bool(payload.get("case_sensitive") or False),
                context_lines=max(0, min(10, _whole(payload.get("context_lines"), 2))),
            )
        except OSError as exc:
            raise HTTPException(status_code=404, detail=f"Cannot read {path}: {exc}")
        except re.error as exc:
            raise HTTPException(status_code=400, detail=f"Bad regex: {exc}")
        return {"ok": True, **result}

    # ── CTX-02: pinned fragments and the compaction log ─────────────────────

    @router.get("/compaction/pins")
    def list_compaction_pins(request: Request, session_id: str = ""):
        """Every fragment this owner pinned against compaction for one
        session — fragments `compact_with_integrity` will fold into every
        other message but these, no matter which turn triggers it."""
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id is required")
        from src.context_engine import compaction_pins

        return {"ok": True, "session_id": session_id,
                "pins": compaction_pins.list_pins(_owner(request), session_id)}

    @router.post("/compaction/pins")
    async def pin_compaction_fragment(request: Request):
        """Pin one message so compaction never folds it away.

        `fingerprint` is `context_compactor._row_fingerprint(role, content)`
        of the message to protect — the same identity compaction already uses
        to map a compacted prompt row back to its transcript row, so pinning
        needs no new id threaded through the message shape. A caller that has
        the message itself may pass `role`/`content` instead and the
        fingerprint is computed here."""
        require_admin(request)
        payload = await _json_body(request)
        session_id = str(payload.get("session_id") or "").strip()
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id is required")
        fingerprint = str(payload.get("fingerprint") or "").strip()
        if not fingerprint and "content" in payload:
            from src.context_compactor import _row_fingerprint

            fingerprint = _row_fingerprint(payload.get("role") or "user",
                                           payload.get("content"))
        if not fingerprint:
            raise HTTPException(status_code=400,
                               detail="fingerprint (or role+content) is required")
        from src.context_engine import compaction_pins

        try:
            pin = compaction_pins.pin_fragment(_owner(request), session_id, fingerprint,
                                               excerpt=str(payload.get("excerpt") or ""))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"ok": True, "pin": pin}

    @router.delete("/compaction/pins/{fingerprint}")
    async def unpin_compaction_fragment(fingerprint: str, request: Request,
                                        session_id: str = ""):
        require_admin(request)
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id is required")
        from src.context_engine import compaction_pins

        removed = compaction_pins.unpin_fragment(_owner(request), session_id, fingerprint)
        return {"ok": True, "removed": removed}

    @router.get("/compaction/{session_id}")
    def get_last_compaction(session_id: str, request: Request):
        """What compaction did to this session, most recently — the marker
        text, how many messages it folded, how many a pin rescued, and the
        EvidenceRefs at the untouched originals. `None` means compaction has
        never run for this session (not an error)."""
        from src.context_engine import compaction_pins

        event = compaction_pins.last_event(_owner(request), session_id)
        return {"ok": True, "session_id": session_id, "event": event}

    return router
