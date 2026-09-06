"""
context_engine_server.py

MCP server exposing the Context Engine: compile a packet, explain why a source
was or was not in it, and read and write the stores the compiler draws from
(blocks, capsules, experiences, the code index, the shared blackboard).

The failure this answers is a specific one. When an agent gives a wrong answer
the first question is always "what did it actually know?", and until this
server existed the only way to find out was to add a print to `compiler.py` and
run the turn again. `context_compile` answers it in one call, from the outside,
without delivering anything to a model — and it answers with the manifest, not
the text, because a tool that dumps a whole packet into a transcript spends the
budget it was called to measure.

Two constraints shape everything here:

* **stdout is the JSON-RPC stream.** One stray print from the app code this
  server imports corrupts it and kills the session, so `src/stdio_guard.py` is
  raised before anything else is imported.
* **the engine is scoped by owner.** `ODYSSEUS_MCP_CONTEXT_OWNER` says whose
  context this server may touch. Without it the reads degrade to install-wide,
  which is what a single-user install already means, but every write is refused
  with a message that names the variable: a block written into the wrong
  owner's scope is a sentence pasted into somebody else's prompts, and there is
  no way to tell afterwards that it was not theirs.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# stdout belongs to the JSON-RPC stream: one print() from the app code this
# server imports would corrupt it and kill the session. The guard
# (src/stdio_guard.py) sends stdout writes to stderr while the session runs.
try:
    from src.stdio_guard import guard as stdout_guard
except Exception:  # pragma: no cover - the server must start regardless
    from contextlib import nullcontext as stdout_guard

server = Server("context")

# Late-initialized engine modules (set during the first tool call).
_engine: dict = {}
_initialized = False

_OWNER_ENV_KEYS = ("ODYSSEUS_MCP_CONTEXT_OWNER", "ODYSSEUS_CONTEXT_OWNER")
_OWNER_SCOPE_ERROR = (
    "Error: the Context Engine MCP server has no owner configured, so it cannot "
    "tell whose context this is. Set ODYSSEUS_MCP_CONTEXT_OWNER for this server. "
    "Reads would be ambiguous and a write would put standing context into "
    "somebody else's prompts."
)


def _configured_owner() -> str:
    for key in _OWNER_ENV_KEYS:
        owner = os.environ.get(key, "").strip()
        if owner:
            return owner
    return ""


def _text_result(text: str) -> list[TextContent]:
    return [TextContent(type="text", text=text)]


def _json_result(payload) -> list[TextContent]:
    """Always JSON, always indented, never a whole packet.

    The caller of these tools is a model reading a transcript: two spaces of
    indentation cost a few tokens and save it from having to parse a wall."""
    return _text_result(json.dumps(payload, indent=2, ensure_ascii=False,
                                   default=str))


def _ensure_init():
    """Import the engine on first use.

    `src.context_engine.compiler` reaches the adapter registry, and through it
    memory, RAG and the provenance graph. Importing that at module scope would
    put a second of import time on a server that may only ever be asked to list
    its tools."""
    global _initialized
    if _initialized:
        return
    _initialized = True

    from src.context_engine import (
        blocks,
        capsules,
        code_index,
        compiler,
        experiences,
        manifest,
        shared_memory,
        store,
    )
    from src.context_engine.contracts import ContextRequest, ContractError

    _engine.update(
        blocks=blocks, capsules=capsules, code_index=code_index,
        compiler=compiler, experiences=experiences, manifest=manifest,
        shared_memory=shared_memory, store=store,
        ContextRequest=ContextRequest, ContractError=ContractError,
    )


def _request(owner: str, arguments: dict):
    """The `ContextRequest` these tools compile from.

    Built through `ContextRequest.parse` rather than by calling the dataclass,
    so a bad `phase` is refused by the contract that owns the vocabulary
    instead of reaching the planner as a value it has no rule for.

    Owner comes from the server's environment and never from the arguments:
    the whole isolation story is that an actor can ask for information and
    cannot ask to be someone else."""
    return _engine["ContextRequest"].parse({
        "actor": {"agent_id": "mcp", "role": "assistant",
                  "model": str(arguments.get("model") or "")},
        "execution": {"owner": owner,
                      "project_id": str(arguments.get("project_id") or ""),
                      "workspace": str(arguments.get("workspace") or "")},
        "task": {"intent": str(arguments.get("intent") or ""),
                 "phase": str(arguments.get("phase") or "act"),
                 "query": str(arguments.get("query") or "")},
        "consumer": "agent",
    })


# ── the tool surface ───────────────────────────────────────────────────────
#
# Every description says WHEN to reach for the tool, not just what it does. A
# model choosing between eight tools reads the first sentence of each; "manages
# blocks" tells it nothing about whether this is the call it wants.

_SCOPE_PROPS = {
    "project_id": {"type": "string",
                   "description": "Project to scope to. Empty means every project."},
    "workspace": {"type": "string",
                  "description": "Absolute path of the repository, when the "
                                 "question involves code or files."},
}


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="context_compile",
            description=(
                "Compile the context packet an agent would be given for a task "
                "and return its MANIFEST — one row per included item, with the "
                "token cost of each section and the reasons for every omission "
                "— without any of the text. Use it to answer 'what would the "
                "agent actually know about X?', to check before delegating "
                "whether the worker will be told the thing it needs, and to "
                "find out why a section is eating the budget. It delivers "
                "nothing to a model and writes only a ledger row."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "The task or question the packet is for."},
                    "intent": {"type": "string",
                               "description": "Optional intent hint (code_edit, "
                                              "debug, research, chat...). Empty "
                                              "lets the planner classify it."},
                    "phase": {"type": "string",
                              "enum": ["plan", "act", "verify"],
                              "description": "Planning gets instructions, "
                                             "verification gets evidence."},
                    "model": {"type": "string",
                              "description": "Model the packet is budgeted for. "
                                             "Changes the window, not the ranking."},
                    **_SCOPE_PROPS,
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="context_explain",
            description=(
                "Explain what happened to ONE source in a freshly compiled "
                "packet: included (in which section, transformed how), omitted "
                "(with the reason), both, or unknown. Use it when a file, "
                "memory or document that should obviously have been used was "
                "not — 'unknown' means no retrieval lane ever produced it, "
                "which is the true answer to most 'why did it not read that?' "
                "questions and points at the index rather than the ranking."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "The task the packet is compiled for."},
                    "source_ref": {"type": "string",
                                   "description": "The reference to explain, e.g. "
                                                  "'file:src/app.py', 'mem:123', "
                                                  "'block:abc'."},
                    "intent": {"type": "string"},
                    **_SCOPE_PROPS,
                },
                "required": ["query", "source_ref"],
            },
        ),
        Tool(
            name="context_blocks",
            description=(
                "List, create or update connectable blocks: the standing "
                "context (project rules, known failures, decision log, working "
                "state) that is injected without being retrieved. Use 'list' "
                "to see what is already standing before adding a rule that "
                "duplicates one, and 'create' when a durable instruction comes "
                "out of a conversation. Content that looks like a credential is "
                "refused by name. Nothing here is ever marked always-loaded "
                "automatically — a person promotes a block deliberately."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "enum": ["list", "create", "update"],
                               "description": "The operation to perform."},
                    "block_id": {"type": "string",
                                 "description": "Required for 'update'."},
                    "type": {"type": "string",
                             "description": "Block type for create/list filter: "
                                            "project_rules, known_failures, "
                                            "decision_log, working_state, "
                                            "tool_policy, style_profile..."},
                    "title": {"type": "string"},
                    "content": {"type": "string",
                                "description": "The text of the block. Never a secret."},
                    "priority": {"type": "integer", "minimum": 0, "maximum": 100},
                    "expected_revision": {
                        "type": "integer",
                        "description": "Optimistic concurrency for 'update': the "
                                       "revision you read. A mismatch is refused "
                                       "with the current revision so you can "
                                       "re-read and re-apply.",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    **_SCOPE_PROPS,
                },
                "required": ["action"],
            },
        ),
        Tool(
            name="context_capsule",
            description=(
                "Read a scope's continuity capsule, or apply typed deltas to "
                "it. The capsule is what a worker needs to pick a task up cold: "
                "objective, definition of done, what is completed, next "
                "actions, decisions, open questions, claims and evidence. Use "
                "'get' when resuming a session or run, and 'apply' to record a "
                "decision or a completed step as the work happens rather than "
                "at the end. One bad delta does not lose the batch."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["get", "apply"]},
                    "scope_id": {"type": "string",
                                 "description": "Session, run, council or branch id."},
                    "deltas": {
                        "type": "array",
                        "description": "Typed deltas for 'apply', e.g. "
                                       "{'op': 'add_decision', 'value': '...'}.",
                        "items": {"type": "object"},
                    },
                    "expected_revision": {"type": "integer"},
                    "ensure": {"type": "boolean",
                               "description": "Create the capsule if this scope has "
                                              "none. Off by default so a typo in "
                                              "scope_id is an error, not a second "
                                              "empty capsule."},
                    "objective": {"type": "string",
                                  "description": "Used only when 'ensure' creates it."},
                    **_SCOPE_PROPS,
                },
                "required": ["action", "scope_id"],
            },
        ),
        Tool(
            name="context_experiences",
            description=(
                "Search verified experiences for a task: how a similar problem "
                "was approached, what the evidence said, and the ANTI-PATTERNS "
                "— approaches that were contradicted or that failed. Use it "
                "before starting work that smells familiar, and read the "
                "anti-patterns first: 'we tried that and it did not work' is "
                "the expensive thing to rediscover. Unproved experiences are "
                "never returned, and the answer is capped at a handful on "
                "purpose — five near-identical experiences are repetition."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "The problem, in the words you would "
                                             "use to describe it."},
                    "intent": {"type": "string",
                               "description": "Narrow to one intent, e.g. 'debug'."},
                    "technologies": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Stack in play, to weight the match.",
                    },
                    "k": {"type": "integer", "minimum": 1, "maximum": 20},
                    **_SCOPE_PROPS,
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="context_code_index",
            description=(
                "Find where a symbol is defined, or refresh the index of a "
                "workspace. Lexical and exact: this is the lane that keeps "
                "working when the embedding store is down, and an identifier is "
                "the query people actually type. Use 'search' before grepping a "
                "repository you do not know, and 'refresh' after a batch of "
                "edits — a stale index is what makes the compiler recommend a "
                "function that no longer exists."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "enum": ["search", "refresh", "status"]},
                    "query": {"type": "string",
                              "description": "Symbol name or phrase, for 'search'."},
                    "kinds": {"type": "array", "items": {"type": "string"},
                              "description": "Restrict to symbol kinds, e.g. "
                                             "['function', 'class']."},
                    "paths": {"type": "array", "items": {"type": "string"},
                              "description": "For 'refresh': only these files. "
                                             "Omit to walk the workspace."},
                    "full": {"type": "boolean",
                             "description": "For 'refresh': re-read every file "
                                            "instead of only the changed ones."},
                    "k": {"type": "integer", "minimum": 1, "maximum": 50},
                    **_SCOPE_PROPS,
                },
                "required": ["action"],
            },
        ),
        Tool(
            name="context_findings",
            description=(
                "Post a finding to the shared blackboard, or search what other "
                "workers have already put there. Use 'search' at the START of a "
                "delegated task — the peer who ran an hour ago may already have "
                "the answer — and 'post' when you establish something others "
                "will need: a fact, a risk, a result, an objection. A fact or a "
                "result with no evidence reference is refused, because an "
                "unbacked claim read as current is what a blackboard exists to "
                "keep out. Findings are corrected by superseding, never edited."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["search", "post"]},
                    "scope": {"type": "string",
                              "description": "The execution the board belongs to "
                                             "(run, council or session id)."},
                    "topic": {"type": "string",
                              "description": "What the finding is about; the "
                                             "handle other workers search on."},
                    "kind": {"type": "string",
                             "enum": ["fact", "result", "risk", "question",
                                      "objection", "correction"],
                             "description": "'fact' and 'result' require evidence."},
                    "claim": {"type": "string",
                              "description": "The finding itself, for 'post'."},
                    "evidence_refs": {"type": "array", "items": {"type": "string"},
                                      "description": "What backs the claim: file "
                                                     "refs, packet ids, proof ids."},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "query": {"type": "string", "description": "Free text, for 'search'."},
                    "status": {"type": "string",
                               "description": "Search filter; defaults to 'open'."},
                    "k": {"type": "integer", "minimum": 1, "maximum": 200},
                    **_SCOPE_PROPS,
                },
                "required": ["action"],
            },
        ),
        Tool(
            name="context_diagnostics",
            description=(
                "The state of the context engine: how big the store is, how "
                "often packets come back degraded and what they are dropping, "
                "the working set's hit rate, which retrieval sources failed to "
                "build, and how stale a workspace's code index is. Use it when "
                "answers have started to feel thin, when a turn is slower than "
                "it should be, or before blaming the model for not knowing "
                "something — a degraded packet says so, and this is where it "
                "says it."
            ),
            inputSchema={
                "type": "object",
                "properties": {**_SCOPE_PROPS},
            },
        ),
    ]


# ── the handlers ───────────────────────────────────────────────────────────

#: Tools whose actions write. Without a configured owner these are refused
#: rather than defaulted: a block or a finding written into the wrong scope is
#: standing context in somebody else's prompts, and there is no way to tell
#: afterwards that it was not theirs. Reads degrade to install-wide instead,
#: which is what a single-user install already means.
_WRITE_ACTIONS = {
    "context_blocks": ("create", "update"),
    "context_capsule": ("apply",),
    "context_code_index": ("refresh",),
    "context_findings": ("post",),
}


def _refuse_write(tool: str, action: str) -> list[TextContent]:
    return _text_result(
        f"{_OWNER_SCOPE_ERROR}\n(refused: {tool} action {action!r})")


def _refused(exc: Exception) -> list[TextContent]:
    """A rejection, named. `path` is whichever field the raising module uses,
    because a caller fixing one field should not have to know which subsystem
    said no. A revision conflict carries the revision that is actually stored,
    so the loser can re-read and re-apply instead of overwriting."""
    payload = {
        "ok": False,
        "error": {
            "path": str(getattr(exc, "path", "")
                        or getattr(exc, "field", "") or "<root>"),
            "message": str(getattr(exc, "message", "")
                           or getattr(exc, "reason", "") or exc),
        },
    }
    revision = getattr(exc, "revision", None)
    if isinstance(revision, int) and not isinstance(revision, bool):
        payload["error"]["revision"] = revision
    return _json_result(payload)


def _limit(value, default: int, ceiling: int) -> int:
    """A count, clamped. The floor is 1 because asking for zero results is
    asking for nothing, which is never what the caller meant."""
    try:
        wanted = int(value)
    except (TypeError, ValueError):
        wanted = default
    return max(1, min(wanted, ceiling))


def _priority(value, default: int = 50) -> int:
    """A block priority. Zero is a legal priority — "load this last if there
    is room" — so this floors at 0 and `_limit` is the wrong tool for it."""
    try:
        wanted = int(value)
    except (TypeError, ValueError):
        wanted = default
    return max(0, min(wanted, 100))


def _strings(value) -> list:
    return [str(v) for v in value] if isinstance(value, list) else []


async def _tool_compile(owner: str, args: dict) -> list[TextContent]:
    packet = await _engine["compiler"].compile_packet(_request(owner, args))
    summary = _engine["manifest"].summarize(packet)
    return _json_result({
        "ok": True,
        "packet_id": packet.packet_id,
        "degraded": packet.degraded,
        "warnings": list(packet.warnings),
        "tokens": summary["tokens"],
        "input_budget": summary["input_budget"],
        "sections": summary["sections"],
        "by_source_type": summary["by_source_type"],
        "manifest": packet.manifest(),
        "omissions": [o.to_dict() for o in packet.omissions],
        "note": "manifest rows carry no text: this is where each sentence came "
                "from, not what it said",
    })


async def _tool_explain(owner: str, args: dict) -> list[TextContent]:
    packet = await _engine["compiler"].compile_packet(_request(owner, args))
    verdict = _engine["manifest"].explain(packet, str(args.get("source_ref") or ""))
    return _json_result({"ok": True, **verdict})


def _tool_blocks(owner: str, args: dict) -> list[TextContent]:
    blocks = _engine["blocks"]
    action = str(args.get("action") or "")
    if action == "list":
        rows = blocks.list_blocks(owner=owner,
                                  project_id=str(args.get("project_id") or ""),
                                  type=str(args.get("type") or ""),
                                  limit=_limit(args.get("limit"), 50, 200))
        return _json_result({"ok": True, "count": len(rows), "blocks": [
            {"id": b.id, "type": b.type, "scope": b.scope, "title": b.title,
             "priority": b.priority, "always_loaded": b.always_loaded,
             "revision": b.revision, "chars": len(b.content),
             "truncated": b.truncated()}
            for b in rows
        ]})
    if action == "create":
        block = blocks.create_block(
            owner=owner, project_id=str(args.get("project_id") or ""),
            type=str(args.get("type") or "project_rules"),
            title=str(args.get("title") or ""),
            content=str(args.get("content") or ""),
            priority=_priority(args.get("priority")))
        return _json_result({"ok": True, "block": block.to_dict()})
    if action == "update":
        updates = {name: args[name] for name in ("type", "title", "content", "priority")
                   if name in args}
        expected = args.get("expected_revision", None)
        block = blocks.update_block(
            str(args.get("block_id") or ""), updates,
            expected_revision=None if expected is None else int(expected))
        return _json_result({"ok": True, "block": block.to_dict()})
    return _text_result(f"Unknown action for context_blocks: {action!r}")


def _tool_capsule(owner: str, args: dict) -> list[TextContent]:
    capsules = _engine["capsules"]
    scope_id = str(args.get("scope_id") or "")
    action = str(args.get("action") or "")
    if action == "get":
        capsule = capsules.load(scope_id, owner=owner)
        if capsule is None:
            return _json_result({"ok": True, "capsule": None,
                                 "note": f"no capsule for scope {scope_id!r}"})
        return _json_result({"ok": True, "capsule": capsule.to_dict(),
                             "rendered": capsules.render(capsule)})
    if action == "apply":
        deltas = args.get("deltas")
        if not isinstance(deltas, list):
            return _text_result("context_capsule 'apply' needs a list of deltas")
        if args.get("ensure"):
            capsules.ensure(scope_id, owner=owner,
                            project_id=str(args.get("project_id") or ""),
                            objective=str(args.get("objective") or ""))
        expected = args.get("expected_revision", None)
        result = capsules.apply_deltas(
            scope_id, [d for d in deltas if isinstance(d, dict)],
            actor=owner or "mcp",
            expected_revision=None if expected is None else int(expected),
            owner=owner)
        return _json_result({"ok": True, **result})
    return _text_result(f"Unknown action for context_capsule: {action!r}")


def _tool_experiences(owner: str, args: dict) -> list[TextContent]:
    hits = _engine["experiences"].search(
        str(args.get("query") or ""), owner=owner,
        project_id=str(args.get("project_id") or ""),
        intent=str(args.get("intent") or ""),
        technologies=_strings(args.get("technologies")),
        k=_limit(args.get("k"), 5, 20))
    return _json_result({
        "ok": True, "count": len(hits), "experiences": hits,
        "note": "read the anti_pattern rows first: they are what not to repeat",
    })


def _tool_code_index(owner: str, args: dict) -> list[TextContent]:
    code_index = _engine["code_index"]
    workspace = str(args.get("workspace") or "")
    project_id = str(args.get("project_id") or "")
    action = str(args.get("action") or "")
    if action == "search":
        hits = code_index.search(str(args.get("query") or ""), workspace=workspace,
                                 project_id=project_id,
                                 k=_limit(args.get("k"), 12, 50),
                                 kinds=_strings(args.get("kinds")))
        return _json_result({"ok": True, "count": len(hits), "symbols": hits})
    if action == "status":
        return _json_result({"ok": True,
                             "status": code_index.status(workspace,
                                                         project_id=project_id)})
    if action == "refresh":
        paths = args.get("paths")
        return _json_result({"ok": True, "refresh": code_index.refresh(
            workspace, project_id=project_id,
            paths=_strings(paths) if isinstance(paths, list) else None,
            full=bool(args.get("full") or False))})
    return _text_result(f"Unknown action for context_code_index: {action!r}")


def _tool_findings(owner: str, args: dict) -> list[TextContent]:
    shared_memory = _engine["shared_memory"]
    action = str(args.get("action") or "")
    if action == "search":
        hits = shared_memory.search(
            scope=str(args.get("scope") or ""), owner=owner,
            topic=str(args.get("topic") or ""), kind=str(args.get("kind") or ""),
            tags=_strings(args.get("tags")),
            status=str(args.get("status", "open") or ""),
            query=str(args.get("query") or ""), k=_limit(args.get("k"), 20, 200))
        return _json_result({"ok": True, "count": len(hits),
                             "findings": [f.to_dict() for f in hits]})
    if action == "post":
        finding = shared_memory.post(
            scope=str(args.get("scope") or ""), owner=owner,
            project_id=str(args.get("project_id") or ""),
            author=str(args.get("author") or "") or owner,
            topic=str(args.get("topic") or ""),
            kind=str(args.get("kind") or "fact"),
            claim=str(args.get("claim") or ""),
            evidence_refs=_strings(args.get("evidence_refs")),
            tags=_strings(args.get("tags")))
        return _json_result({"ok": True, "finding": finding.to_dict()})
    return _text_result(f"Unknown action for context_findings: {action!r}")


def _tool_diagnostics(owner: str, args: dict) -> list[TextContent]:
    compiler = _engine["compiler"]
    store = _engine["store"]
    project_id = str(args.get("project_id") or "")
    out = {
        "ok": True,
        "compiler": compiler.diagnostics(owner=owner, project_id=project_id),
        "store": {"bytes": store.store_bytes(), "tables": store.table_counts()},
    }
    try:
        from src.context_engine import cache

        stats = cache.working_set().stats()
        looks = stats.hits + stats.misses
        out["cache"] = {"hits": stats.hits, "misses": stats.misses,
                        "entries": stats.entries, "bytes": stats.bytes,
                        "hit_rate": round(stats.hits / looks, 4) if looks else 0.0}
    except Exception:  # noqa: BLE001 - a diagnostic may never raise
        out["cache"] = {"error": "the working set could not be read"}
    try:
        from src.context_engine import candidates, planner

        candidates.default_sources()
        built = set(candidates.registered_sources())
        declared = set(planner.known_sources())
        out["sources"] = {"built": sorted(built),
                          "unavailable": sorted(declared - built)}
    except Exception:  # noqa: BLE001 - see above
        out["sources"] = {"error": "the source registry could not be read"}
    workspace = str(args.get("workspace") or "")
    if workspace:
        out["code_index"] = _engine["code_index"].status(workspace,
                                                         project_id=project_id)
    return _json_result(out)


_HANDLERS = {
    "context_blocks": _tool_blocks,
    "context_capsule": _tool_capsule,
    "context_code_index": _tool_code_index,
    "context_diagnostics": _tool_diagnostics,
    "context_experiences": _tool_experiences,
    "context_findings": _tool_findings,
}
_ASYNC_HANDLERS = {
    "context_compile": _tool_compile,
    "context_explain": _tool_explain,
}


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch, and never raise.

    An exception out of here is a dead session, and a dead MCP session takes
    every other tool on this server with it. A refused write and a broken store
    both come back as text the caller can read and act on."""
    if name not in _HANDLERS and name not in _ASYNC_HANDLERS:
        return _text_result(f"Unknown tool: {name}")
    args = arguments if isinstance(arguments, dict) else {}

    owner = _configured_owner()
    action = str(args.get("action") or "")
    if not owner and action in _WRITE_ACTIONS.get(name, ()):
        return _refuse_write(name, action)

    try:
        _ensure_init()
    except Exception as exc:  # noqa: BLE001 - an unimportable engine is a message
        return _text_result(f"Error: the context engine could not be loaded: {exc}")

    try:
        if name in _ASYNC_HANDLERS:
            return await _ASYNC_HANDLERS[name](owner, args)
        return _HANDLERS[name](owner, args)
    except Exception as exc:  # noqa: BLE001 - see the docstring
        engine = _engine
        refusals = tuple(t for t in (
            engine.get("ContractError"),
            getattr(engine.get("blocks"), "BlockError", None),
            getattr(engine.get("capsules"), "CapsuleError", None),
            getattr(engine.get("experiences"), "ExperienceRejected", None),
        ) if isinstance(t, type))
        if refusals and isinstance(exc, refusals):
            return _refused(exc)
        return _text_result(f"Error in {name}: {type(exc).__name__}: {exc}")


async def run():
    # The guard goes up INSIDE stdio_server(): that context manager wraps the
    # real sys.stdout.buffer when it is entered, so the protocol keeps the
    # handle and everything else is diverted to stderr.
    async with stdio_server() as (read_stream, write_stream):
        with stdout_guard():
            await server.run(read_stream, write_stream,
                             server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
