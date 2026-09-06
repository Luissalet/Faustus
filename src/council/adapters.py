"""
council/adapters.py — the bridges to the engines Faustus already runs.

The failure this file exists to prevent is the one the plan repeats more often
than any other (§25): a council that grows its own agent loop, its own GPU
scheduler, its own judge, its own lock registry and its own idea of what
`verified` means.  Every one of those already exists in this repository, and a
second copy of any of them is not an integration — it is a second opinion that
will drift, and the one that drifts is always the one nobody tested.

So nothing here executes anything.  Each class is a translation: council
vocabulary in, one call into an engine that already works, and the engine's own
result translated back into the shapes the ledger and `prove` already read.

Which door each adapter uses, and why that one:

* `StreamingChatInvoker` speaks one intervention through `llm_core.stream_llm`
  — the streaming chat primitive `agent_loop.stream_agent_loop` itself sits on
  (§6.2: "Streaming del agente existente; Consejo no crea un LLM loop
  alternativo").  It is deliberately NOT the agent loop: a council intervention
  has no tools (§4.1 grants no mutating tools in a talking room) and the work
  that does need them goes to `DispatchTaskExecutor` below.  Driving the whole
  agent loop to say one sentence would drag tool schemas, retrieval and the
  approval gate onto a path where none of them can fire, and would hand the
  room a second place where tools could be granted.  Of the two existing doors
  this is the finer one, and it is the one that returns the final text AND the
  usage; the tokens matter because `scheduler.spend()` prices the room with
  them.
* `DispatchTaskExecutor` calls `dispatch.start/get/compact/cancel`, and steers
  and stops through `agent_tools.subagent_tools` — the same functions the chat
  surface's own Steer and Stop use.  It reimplements no part of a worker (§4.4:
  "Reutilizar Subagents/Dispatch como motor ... no debe copiar su ejecución
  interna").
* `TournamentAdapter` calls `tournament.run` for the blind round and
  `tournament`'s own judge for the verdict, so the anonymisation, the per-model
  locks, the shared GPU semaphore, the convergence test and the deterministic
  tiebreak are the ones already in production (§4.2, §4.6, §17.2).
* `verify_task` composes a `ChangeSet` and hands it to `prove`.  It holds no
  verdict of its own.

Two rules every entry point here obeys:

1. **`verified` is never this file's word.**  It is `prove`'s `proved` and
   nothing else (§20: "ninguna tarea con escritura se presenta como verificada
   solo por afirmación del agente").  The claims that go into a proof come from
   the ledger and the changes from what Faustus observed on disk — never from
   what a model said it did (§20: "los archivos observados y reclamados llegan
   a `prove`").
2. **An absent engine degrades with a reason and never fakes a success.**
   Every result carries `ok`; a false `ok` carries `reason` (a stable token)
   and `detail` (a sentence).  A judge that could not be read comes back with
   no score at all (§3.4: "un juez fallido no produce una puntuación
   inventada").
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import time
from collections import deque
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "ENGINE_MODULES",
    "INVOCATION_KEYS",
    "TASK_RESULT_KEYS",
    "VERDICTS",
    "VERIFIED",
    "UNPROVED",
    "PROVED_VERDICT",
    "DEFAULT_TASK_TIMEOUT_S",
    "StreamingChatInvoker",
    "DispatchTaskExecutor",
    "TournamentAdapter",
    "verify_task",
    "default_invoker",
    "default_executor",
    "use_engines",
    "reset_engines",
]


# ── the engines, named once ────────────────────────────────────────────────
#
# Import paths as data, and every import lazy.  Two reasons, both of them
# lessons from the modules below this one: `src.council.contracts` and
# `src.council.persistence` deliberately import nothing heavy, and an adapter
# module that pulled `llm_core`, `dispatch` and `tournament` in at import time
# would put a model client on the path of anything that so much as reads a
# session.  The second reason is that a missing engine has to be a degraded
# result with a reason (rule 2), and an ImportError at module scope is a
# process that does not start.

ENGINE_MODULES: Dict[str, str] = {
    "llm": "src.llm_core",
    "models": "src.ai_interaction",
    "dispatch": "src.dispatch",
    "workers": "src.agent_tools.subagent_tools",
    "tournament": "src.tournament",
    "prove": "src.prove",
    "changesets": "src.changesets",
}

_ENGINES: Dict[str, Any] = {}


def use_engines(**engines: Any) -> None:
    """Point one or more adapters at a double.

    The same seam `context.use_compiler` and `persistence.use_path` provide,
    for the same reason: a test of a bridge must be able to assert that the
    bridge CALLED the engine, and no test in this suite may reach a model, a
    worker or a GPU.  A name that is not in `ENGINE_MODULES` is refused rather
    than stored, because a typo that silently registers nothing is a test that
    passes while exercising the real engine.
    """
    unknown = sorted(k for k in engines if k not in ENGINE_MODULES)
    if unknown:
        raise KeyError(f"unknown engine(s) {unknown}; this module bridges to "
                       f"{sorted(ENGINE_MODULES)}")
    _ENGINES.update({k: v for k, v in engines.items() if v is not None})
    for name, value in engines.items():
        if value is None:
            _ENGINES.pop(name, None)


def reset_engines() -> None:
    """Forget every override and go back to the real modules."""
    _ENGINES.clear()


def _engine(name: str) -> Any:
    """The module behind `name`, or `None` — never an exception.

    `None` is a first-class answer here: every caller of this function has a
    degraded result to return, and none of them may invent a success.
    """
    if name in _ENGINES:
        return _ENGINES[name]
    path = ENGINE_MODULES.get(name, "")
    if not path:
        return None
    try:
        return importlib.import_module(path)
    except Exception as exc:  # noqa: BLE001 - an absent engine is a reason, not a crash
        logger.warning("council adapters: %s is unavailable (%s)", path, exc)
        return None


# ── the shapes these adapters return ───────────────────────────────────────
#
# Written down rather than left to each method, because an orchestrator and a
# route read them and a key that exists on one path and not another is how a
# caller learns to write `result.get("content") or ""` everywhere.

#: What `StreamingChatInvoker.invoke` always returns.
INVOCATION_KEYS: Tuple[str, ...] = (
    "ok", "engine", "participant_id", "model", "endpoint", "content", "usage",
    "elapsed_ms", "degraded", "reason", "detail",
)

#: What `DispatchTaskExecutor.run` always returns.  `status` is Dispatch's own
#: word (`done`, `partial`, `error`, `cancelled`, ...) and is passed through
#: untranslated on purpose: §17.3 says to store the `run_id` and translate the
#: events "sin inventar estados nuevos".
TASK_RESULT_KEYS: Tuple[str, ...] = (
    "ok", "engine", "task_id", "run_id", "session_id", "status", "summary",
    "files_changed", "claimed_only", "changes", "verification", "proof",
    "stopped_by", "workers", "degraded", "reason", "detail",
)

#: The four words a council task's verification may end on.  Three of them are
#: `prove`'s own; only `verified` is renamed, because `proved` is a statement
#: about evidence and `verified` is the status the plan's §3.4 ladder and
#: `synthesis.STATUSES` use for the same fact.
VERDICTS: Tuple[str, ...] = ("verified", "partial", "unproved", "contradicted")

VERIFIED: str = "verified"
UNPROVED: str = "unproved"

#: The only verdict from `src/prove.py` that earns `verified`.  Same constant,
#: same reason, as `synthesis.PROVED_VERDICT`.
PROVED_VERDICT: str = "proved"

_VERDICT_FROM_PROOF: Dict[str, str] = {
    "proved": VERIFIED,
    "partial": "partial",
    "unproved": UNPROVED,
    "contradicted": "contradicted",
}

#: How long a delegated task may run before the executor stops waiting for it.
#: Only a default: a session's `budgets.max_wall_seconds` wins when it has one,
#: because that number is the one the user agreed to (§3.6).
DEFAULT_TASK_TIMEOUT_S: float = 1800.0

#: Dispatch statuses that mean the job is still alive.  Read from the engine
#: when it is there; this tuple is the fallback for a double that has none.
_LIVE_STATUSES: Tuple[str, ...] = ("queued", "running", "verifying", "cancelling")

_STREAM_POLL_S: float = 5.0


# ── small readers (total: an adapter must not die reading its own inputs) ──

def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _field(obj: Any, name: str, default: Any = "") -> Any:
    """One field of a contract object or of the mapping standing in for it."""
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        value = obj.get(name, default)
    else:
        value = getattr(obj, name, default)
    return default if value is None else value


def _str_list(value: Any, limit: int = 500) -> List[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple, set)):
        return []
    return [_text(v) for v in list(value)[:limit] if _text(v)]


def _degraded(base: Dict[str, Any], reason: str, detail: str) -> Dict[str, Any]:
    """Mark a result as the honest failure it is, and log the sentence once."""
    base.update({"ok": False, "degraded": True, "reason": reason, "detail": detail})
    logger.info("council adapters: %s (%s)", detail, reason)
    return base


# ── a context packet becomes chat messages ─────────────────────────────────
#
# `context.build_packet` returns a `ContextPacket`; a policy under test hands a
# mapping; a caller with one prompt hands a string.  All three arrive here and
# none of them may raise, because the alternative is a room that stops
# answering because a packet had a shape this file had not met.

def _packet_text(packet: Any) -> Tuple[str, str]:
    """The system text a packet carries, and a note when it carried none."""
    if packet is None:
        return "", "the packet was empty"
    if isinstance(packet, str):
        return packet.strip(), ""
    sections = getattr(packet, "sections", None)
    if sections:
        blocks: List[str] = []
        for section in sections:
            kind = _text(_field(section, "kind")) or "context"
            lines: List[str] = []
            for item in _field(section, "items", ()) or ():
                title = _text(_field(item, "title"))
                body = _text(_field(item, "body"))
                if not (title or body):
                    continue
                lines.append(f"{title}\n{body}".strip() if title else body)
            if lines:
                blocks.append(f"[{kind}]\n" + "\n\n".join(lines))
        if blocks:
            return "\n\n".join(blocks), ""
        return "", "the packet compiled to no items"
    if isinstance(packet, Mapping):
        for key in ("system", "context", "text", "content"):
            value = _text(packet.get(key))
            if value:
                return value, ""
    return "", f"a packet of type {type(packet).__name__} carried no readable text"


def _messages_from_packet(packet: Any, *, turn: Any) -> Tuple[List[Dict[str, str]], str]:
    """`(messages, note)` for one intervention.

    A packet is CONTEXT, not a conversation: it becomes the system message, and
    the question is the turn's own content.  A caller that already holds chat
    messages passes them as `packet["messages"]` and they are used verbatim —
    including their roles, because rewriting a role here is exactly the
    `[Claude]: ...` bug §3.2 exists to end.

    A packet with context and no question comes back empty rather than with an
    invented prompt: this module does not author instructions.
    """
    if isinstance(packet, Mapping):
        raw = packet.get("messages")
        if isinstance(raw, (list, tuple)) and raw:
            out = [{"role": _text(_field(m, "role")) or "user",
                    "content": str(_field(m, "content", "") or "")}
                   for m in raw if isinstance(m, (Mapping, dict))]
            if out:
                return out, ""
    system, note = _packet_text(packet)
    question = _text(_field(turn, "content"))
    messages: List[Dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    if question:
        messages.append({"role": "user", "content": question})
    if not question:
        return [], (note or "the packet carried context but the turn carried no question")
    return messages, note


def _sse_payloads(chunk: Any) -> List[Dict[str, Any]]:
    """The JSON objects inside one `stream_llm` chunk.

    The stream is SSE text (`data: {...}\\n\\n`, and `event: error` before the
    payload that explains it).  Parsing it here rather than asking `llm_core`
    for a richer return keeps this file a reader of the existing stream instead
    of a reason to change it.
    """
    out: List[Dict[str, Any]] = []
    for line in str(chunk or "").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        body = line[5:].strip()
        if not body or body == "[DONE]":
            continue
        try:
            parsed = json.loads(body)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


# ── one participant says one thing (§6.2) ──────────────────────────────────

class StreamingChatInvoker:
    """Implements `orchestrator.ModelInvoker` on the streaming chat path.

    One call, no loop, no tools.  `llm_core.stream_llm` is the primitive the
    whole application streams through — the agent loop included — so a council
    intervention costs the same code path as a chat message and inherits its
    provider handling, its local-model slot and its usage reporting.  What this
    class adds is exactly three things the stream does not do for itself:
    turning a `ContextPacket` into messages, holding a deadline, and answering
    with a result instead of raising.

    Why not `agent_loop.stream_agent_loop`: it is the door for work WITH tools,
    and a council intervention has none (§4.1).  Sending a speech turn through
    it would build tool schemas and an approval gate that cannot fire, and —
    the part that matters — would put a second, quieter place in the system
    where a participant could end up holding tools.  Tool work in a council is
    a task, and a task goes to `DispatchTaskExecutor`, which drives the agent
    loop through Dispatch exactly once.
    """

    def __init__(self, *, endpoint_resolver: Any = None, timeout_s: float = 180.0) -> None:
        #: `(model, *, owner) -> (url, model_id, headers)`, sync or async.
        #: Defaults to `ai_interaction._resolve_model`, the resolution normal
        #: chat and `tournament.default_llm_call` both use (§6.2).
        self._resolver = endpoint_resolver
        try:
            self._timeout_s = max(1.0, float(timeout_s))
        except (TypeError, ValueError):
            self._timeout_s = 180.0

    async def invoke(self, *, participant: Any, packet: Any, turn: Any,
                     timeout_s: Optional[float] = None) -> Dict[str, Any]:
        """One intervention.  Never raises; see `INVOCATION_KEYS`."""
        started = time.monotonic()
        result: Dict[str, Any] = {
            "ok": False, "engine": "stream_llm",
            "participant_id": _text(_field(participant, "id")),
            "model": _text(_field(participant, "model")),
            "endpoint": "", "content": "", "usage": {},
            "elapsed_ms": 0, "degraded": False, "reason": "", "detail": "",
        }
        messages, note = _messages_from_packet(packet, turn=turn)
        if not messages:
            return _degraded(result, "empty_packet",
                             note or "there was nothing to send to the model")
        if not result["model"]:
            return _degraded(result, "no_model",
                             f"participant {result['participant_id'] or '?'} has no model; "
                             "a seat with no model cannot speak")
        owner = _text(_field(packet, "owner")) or _text(_field(turn, "owner"))
        try:
            url, model_id, headers = await self._resolve(result["model"], owner)
        except Exception as exc:  # noqa: BLE001 - an unresolvable model is a reason
            return _degraded(result, "endpoint_unresolved",
                             f"could not resolve {result['model']!r}: "
                             f"{type(exc).__name__}: {exc}")
        result["endpoint"] = _text(url)
        engine = _engine("llm")
        stream_llm = getattr(engine, "stream_llm", None) if engine else None
        if not callable(stream_llm):
            return _degraded(result, "engine_unavailable",
                             "src.llm_core.stream_llm is not available in this build; "
                             "no council intervention can be spoken")
        budget = float(timeout_s or self._timeout_s)
        stream = stream_llm(url, model_id or result["model"], messages,
                            headers=headers or None, timeout=int(max(1.0, budget)),
                            session_id=_text(_field(participant, "private_session_id")) or None)
        try:
            content, usage, error = await asyncio.wait_for(_consume_stream(stream), budget)
        except asyncio.TimeoutError:
            result["elapsed_ms"] = _ms_since(started)
            return _degraded(result, "timeout",
                             f"{result['model']} did not finish within {budget:.0f}s")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a provider failure is a reason
            result["elapsed_ms"] = _ms_since(started)
            return _degraded(result, "stream_failed",
                             f"{result['model']} failed: {type(exc).__name__}: {exc}")
        result["elapsed_ms"] = _ms_since(started)
        result["usage"] = dict(usage or {})
        result["content"] = content.strip()
        if error:
            return _degraded(result, "model_error", error[:500])
        if not result["content"]:
            # An empty answer is not an abstention: an abstention is a message
            # a participant chooses to write (§4.1), and reading silence as one
            # would let a broken endpoint look like a considered decision.
            return _degraded(result, "empty_answer",
                             f"{result['model']} returned no text")
        result["ok"] = True
        if note:
            result["detail"] = note
        return result

    async def _resolve(self, model: str, owner: str) -> Tuple[str, str, Any]:
        """`(url, model_id, headers)` from the injected resolver or the app's."""
        resolver = self._resolver
        if resolver is None:
            engine = _engine("models")
            resolver = getattr(engine, "_resolve_model", None) if engine else None
            if not callable(resolver):
                raise RuntimeError("src.ai_interaction._resolve_model is unavailable")
            return await asyncio.to_thread(resolver, model, owner or None)
        answer = resolver(model, owner=owner)
        if asyncio.iscoroutine(answer) or isinstance(answer, asyncio.Future):
            answer = await answer
        url, model_id, headers = answer
        return _text(url), _text(model_id), headers


def _ms_since(started: float) -> int:
    return max(0, int(round((time.monotonic() - started) * 1000)))


async def _consume_stream(stream: Any) -> Tuple[str, Dict[str, Any], str]:
    """Drain one `stream_llm` generator into `(text, usage, error)`.

    Split out of `invoke` so the deadline can wrap a coroutine: `wait_for`
    cannot hold an `async for` directly, and a timeout that leaves the
    generator open leaks the provider connection.
    """
    parts: List[str] = []
    usage: Dict[str, Any] = {}
    error = ""
    try:
        async for chunk in stream:
            for payload in _sse_payloads(chunk):
                if "delta" in payload:
                    parts.append(str(payload.get("delta") or ""))
                elif payload.get("type") == "usage" and isinstance(payload.get("data"), dict):
                    usage = dict(payload["data"])
                elif payload.get("error"):
                    error = _text(payload.get("error")) or _text(payload.get("text"))
    finally:
        closer = getattr(stream, "aclose", None)
        if callable(closer):
            try:
                await closer()
            except Exception:  # noqa: BLE001 - closing a closed stream is not news
                logger.debug("council adapters: closing the chat stream failed",
                             exc_info=True)
    return "".join(parts), usage, error


# ── one task is done by the engine that already does tasks (§4.4, §17.3) ───

class DispatchTaskExecutor:
    """Implements `orchestrator.TaskExecutor` on top of `src/dispatch.py`.

    Everything a worker needs already exists there: the Workers chat, the
    checkpoint before and the observed diff after, `claimed_only`, the
    watchdog, the verification pass, the file locks and the four-value outcome.
    §4.4 is explicit that Council "no debe copiar su ejecución interna", so this
    class starts a job, waits for it, and translates `dispatch.compact(job)`
    into the evidence a council ledger stores.  There is no worker loop here,
    no retry policy and no second idea of what a changed file is.

    Two translations are worth naming:

    * `status` is Dispatch's own word, passed through untranslated (§17.3:
      "traducir eventos sin inventar estados nuevos").  Mapping `done` onto a
      council word here would be the moment a job that merely finished starts
      reading as a job that succeeded.
    * `files_changed` is what Faustus OBSERVED and `claimed_only` is what a
      worker said and Faustus could not see.  Dispatch already does that
      reconciliation; keeping both fields is what lets `verify_task` build a
      proof that can be wrong about something.

    Steering and stopping go to the same functions the chat surface uses —
    `subagent_tools.steer_worker` (which the agent loop drains through
    `pending_user_messages`) and `dispatch.cancel`.  A council Stop and a chat
    Stop are then the same event, which is the only way the watchdog, the
    ledger and the user can agree about what happened.
    """

    def __init__(self, *, owner: str = "", workspace: str = "") -> None:
        self._owner = _text(owner)
        self._workspace = _text(workspace)
        #: run_id -> task_id, so a progress card can name the task it belongs
        #: to.  Bookkeeping only: Dispatch remains the source of truth for the
        #: run itself, and this map is rebuilt from `task.run_id` after a
        #: restart rather than being persisted here (§15.1).
        self._runs: Dict[str, str] = {}

    # -- start and wait ----------------------------------------------------

    async def run(self, *, task: Any, participant: Any, session: Any) -> Dict[str, Any]:
        """Delegate one council task and come back with evidence."""
        task_id = _text(_field(task, "id"))
        result: Dict[str, Any] = {
            "ok": False, "engine": "dispatch", "task_id": task_id, "run_id": "",
            "session_id": "", "status": "", "summary": "",
            "files_changed": [], "claimed_only": [], "changes": {},
            "verification": {}, "proof": {}, "stopped_by": "", "workers": [],
            "degraded": False, "reason": "", "detail": "",
        }
        engine = _engine("dispatch")
        start = getattr(engine, "start", None) if engine else None
        if not callable(start):
            return _degraded(result, "engine_unavailable",
                             "src.dispatch is not available in this build; this council "
                             "cannot delegate work and will not pretend it did")
        instruction = _text(_field(task, "instruction")) or _text(_field(task, "title"))
        if not instruction:
            return _degraded(result, "empty_task",
                             f"task {task_id or '?'} carries no instruction")
        workspace = self._workspace or _text(_field(session, "workspace"))
        if not workspace:
            return _degraded(result, "no_workspace",
                             "a delegated task needs the absolute folder its workers are "
                             "confined to; this session declares none")
        body = _dispatch_body(task=task, participant=participant, session=session,
                              instruction=instruction, workspace=workspace)
        owner = self._owner or _text(_field(session, "owner"))
        key = f"council:{_text(_field(session, 'id'))}:{task_id}"
        try:
            job = await start(owner or None, body, idempotency_key=key)
        except Exception as exc:  # noqa: BLE001 - a refused job is a reason
            return _degraded(result, "rejected",
                             f"dispatch refused the task: {type(exc).__name__}: {exc}")
        result["run_id"] = _text(_field(job, "id"))
        result["session_id"] = _text(_field(job, "session_id"))
        if result["run_id"]:
            self._runs[result["run_id"]] = task_id
        timed_out = await self._await_job(engine, job, session)
        compacted = self._compact(engine, job)
        _absorb_compact(result, compacted)
        if timed_out:
            return _degraded(result, "timeout",
                             f"run {result['run_id'] or '?'} was still running when the "
                             "session's wall-clock budget ran out; it was left alone and "
                             "not cancelled, so nothing here repeats an effect")
        if not compacted:
            return _degraded(result, "unreadable_run",
                             f"dispatch could not summarise run {result['run_id'] or '?'}")
        result["ok"] = _text(result["status"]) == "done"
        if not result["ok"]:
            result["degraded"] = True
            result["reason"] = result["reason"] or "run_not_done"
            result["detail"] = result["detail"] or (
                f"the run ended {result['status'] or 'in an unknown state'}; "
                "whether anything was proved is `verify_task`'s answer, not this one")
        return result

    async def _await_job(self, engine: Any, job: Any, session: Any) -> bool:
        """Wait for the job.  `True` when the budget ran out first.

        A job that outlives its budget is NOT cancelled here.  §25 forbids
        repeating an effect whose outcome is uncertain, and killing a worker
        mid-write to meet a deadline is how that uncertainty is created; the
        run is left to Dispatch, the council records that it was still running,
        and the user gets the choice.
        """
        wait = getattr(engine, "wait", None)
        deadline = time.monotonic() + _task_timeout(session)
        while _text(_field(job, "status")) in _LIVE_STATUSES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            slice_s = min(_STREAM_POLL_S, remaining)
            try:
                if callable(wait):
                    await wait(job, slice_s)
                else:
                    await asyncio.sleep(min(0.05, slice_s))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a poll that fails is not the job
                logger.warning("council adapters: waiting on a dispatch job failed: %s", exc)
                return False
        return False

    @staticmethod
    def _compact(engine: Any, job: Any) -> Dict[str, Any]:
        compact = getattr(engine, "compact", None)
        if not callable(compact):
            return {}
        try:
            data = compact(job)
        except Exception:  # noqa: BLE001 - a read path
            logger.exception("council adapters: dispatch.compact failed")
            return {}
        return dict(data) if isinstance(data, Mapping) else {}

    # -- steer, stop, watch (§13, §17.3) -----------------------------------

    async def steer(self, run_id: str, message: str) -> bool:
        """Queue a mid-task instruction for every worker of this run.

        `subagent_tools.steer_worker` is the mechanism; the agent loop drains
        it through `pending_user_messages` before its next round.  Nothing is
        written into a transcript here and no second channel is invented: a
        steer the workers cannot receive answers `False` rather than being
        recorded as delivered.
        """
        text = " ".join(_text(message).split())
        if not text:
            return False
        workers = _engine("workers")
        steer_worker = getattr(workers, "steer_worker", None) if workers else None
        board = getattr(workers, "worker_board", None) if workers else None
        if not (callable(steer_worker) and callable(board)):
            logger.info("council adapters: no worker registry; %r cannot be steered", run_id)
            return False
        parent = self._parent_session(run_id)
        sent = False
        try:
            cards = dict(board() or {})
        except Exception:  # noqa: BLE001 - a read path
            logger.exception("council adapters: the worker board could not be read")
            return False
        for child_id, card in cards.items():
            if parent and _text(_field(card, "parent")) != parent:
                continue
            try:
                sent = bool(steer_worker(child_id, text, "user")) or sent
            except Exception:  # noqa: BLE001 - one deaf worker is not the others
                logger.exception("council adapters: steering %s failed", child_id)
        return sent

    async def stop(self, run_id: str) -> bool:
        """Cancel one delegated run through Dispatch's own cancellation.

        Not `stop_worker` per worker: cancelling the job is what also settles
        its record, releases its locks and stops the watchdog.  A council that
        cancelled the workers behind Dispatch's back would leave a job Dispatch
        still believes is running.
        """
        engine = _engine("dispatch")
        cancel = getattr(engine, "cancel", None) if engine else None
        job = self._job(run_id)
        if job is None or not callable(cancel):
            return False
        try:
            return bool(cancel(job))
        except Exception:  # noqa: BLE001 - a cancel that fails is a False
            logger.exception("council adapters: cancelling %s failed", run_id)
            return False

    def progress(self, run_id: str) -> Dict[str, Any]:
        """The live board for one run: Dispatch's `compact`, not a new tally."""
        out: Dict[str, Any] = {
            "ok": False, "engine": "dispatch", "run_id": _text(run_id),
            "task_id": self._runs.get(_text(run_id), ""), "status": "",
            "phase": "", "progress": {}, "workers": {},
            "degraded": False, "reason": "", "detail": "",
        }
        engine = _engine("dispatch")
        job = self._job(run_id)
        if job is None:
            return _degraded(out, "unknown_run",
                             f"dispatch does not know run {_text(run_id) or '?'}")
        data = self._compact(engine, job)
        out["status"] = _text(data.get("status")) or _text(_field(job, "status"))
        out["phase"] = _text(data.get("phase"))
        out["progress"] = dict(data.get("progress") or {})
        states = getattr(engine, "worker_states", None)
        if callable(states):
            try:
                out["workers"] = dict(states(job) or {})
            except Exception:  # noqa: BLE001 - a read path
                logger.exception("council adapters: worker_states failed")
        out["ok"] = True
        return out

    # -- small lookups -----------------------------------------------------

    def _job(self, run_id: str) -> Any:
        engine = _engine("dispatch")
        get = getattr(engine, "get", None) if engine else None
        if not callable(get):
            return None
        try:
            return get(_text(run_id))
        except Exception:  # noqa: BLE001 - a read path
            logger.exception("council adapters: dispatch.get(%r) failed", run_id)
            return None

    def _parent_session(self, run_id: str) -> str:
        """The Workers chat this run's workers hang off, or `""`.

        `""` means "steer every live worker", which is only correct when the
        run cannot be located at all; it is logged, because a council that
        steers somebody else's worker is a worse bug than one that steers none.
        """
        job = self._job(run_id)
        parent = _text(_field(job, "session_id")) if job is not None else ""
        if not parent:
            logger.warning("council adapters: run %r has no worker session; a steer "
                           "cannot be aimed at it", run_id)
        return parent


def _dispatch_body(*, task: Any, participant: Any, session: Any,
                   instruction: str, workspace: str) -> Dict[str, Any]:
    """The `dispatch.start` payload for one council task.

    `files` carries the resources the ledger already granted this participant,
    so the worker's own `FileLockRegistry` and the council's claims are about
    the same paths (§11.2: no second lock semantics).  No reviewer is asked
    for: §4.4 gives the review to an independent council participant, and a
    second automatic reviewer would review the work twice and reconcile
    neither.
    """
    name = (_text(_field(participant, "display_name"))
            or _text(_field(participant, "id")) or "council-worker")
    body: Dict[str, Any] = {
        "workspace": workspace,
        "parallel": False,
        "tasks": [{
            "name": name,
            "instruction": instruction,
            "files": _str_list(_field(task, "claimed_resources", ())),
        }],
    }
    model = _text(_field(participant, "model"))
    if model:
        body["model"] = model
        body["tasks"][0]["model"] = model
    agent = _text(_field(participant, "agent_slug"))
    if agent:
        body["agent"] = agent
    return body


def _task_timeout(session: Any) -> float:
    """The wall clock a delegated task may spend: the room's budget (§3.6)."""
    budgets = _field(session, "budgets", None)
    raw = _field(budgets, "max_wall_seconds", 0) if budgets is not None else 0
    try:
        seconds = float(raw or 0)
    except (TypeError, ValueError):
        seconds = 0.0
    return seconds if seconds > 0 else DEFAULT_TASK_TIMEOUT_S


def _absorb_compact(result: Dict[str, Any], compacted: Mapping[str, Any]) -> None:
    """Copy Dispatch's summary into the council's result shape, verbatim."""
    if not compacted:
        return
    inner = compacted.get("result")
    inner = dict(inner) if isinstance(inner, Mapping) else {}
    result["status"] = _text(compacted.get("status")) or result["status"]
    result["summary"] = _text(inner.get("summary"))
    result["files_changed"] = _str_list(inner.get("files_changed"))
    result["claimed_only"] = _str_list(inner.get("claimed_only"))
    result["changes"] = dict(inner.get("changes") or {})
    result["verification"] = dict(inner.get("verification") or {})
    result["proof"] = dict(inner.get("proof") or {})
    result["stopped_by"] = _text(inner.get("stopped_by"))
    workers = inner.get("workers")
    if isinstance(workers, (list, tuple)):
        result["workers"] = [dict(w) for w in workers if isinstance(w, Mapping)]


# ── the blind round and the judge belong to Tournament (§4.2, §4.6) ────────

class TournamentAdapter:
    """`consult` and `debate` run on `src/tournament.py`.

    The blind round is the one piece of a council that Faustus has had working
    the longest: round 0 shows no participant any peer's answer, the per-model
    lock serialises two seats naming the same model, the shared GPU semaphore
    keeps the machine honest, `assess_convergence` decides when another round
    would add nothing, and a judge that cannot be read falls back to a
    deterministic tiebreak instead of to an invented score.  Re-deriving any of
    that here would give the room a second, worse tournament (§17.2: "no
    degradar anonimización, fallback determinista, cancelación individual ni
    convergencia").

    Identities: round 0 hides peers from each other completely, and the mapping
    from an entry back to a participant is returned to the CALLER — §4.2 hides
    identities between models and never from the user or the audit.
    """

    def __init__(self, *, owner: str = "") -> None:
        self._owner = _text(owner)

    async def blind_round(self, *, question: str, participants: Sequence[Any],
                          timeout_s: float = 180.0) -> Dict[str, Any]:
        """Ask everyone the same question with nobody seeing anybody.

        `rounds=1` is round 0 and only round 0.  `tournament.run` still ranks
        its finalists afterwards, which costs one judge call this method did
        not ask for and gives the coordinator the contrast §4.2 wants; that
        judge is the engine's, so a judge that cannot be read falls back to the
        deterministic tiebreak and `ranking` says which happened.  A caller
        that wants no verdict at all reads `answers` and ignores `judge`.
        """
        seats = list(participants or ())
        result: Dict[str, Any] = {
            "ok": False, "engine": "tournament", "answers": [], "convergence": None,
            "ranking": "", "judge": None, "stopped_by": "", "cancelled": [],
            "errors": [], "degraded": False, "reason": "", "detail": "",
        }
        engine = _engine("tournament")
        run = getattr(engine, "run", None) if engine else None
        if not callable(run):
            return _degraded(result, "engine_unavailable",
                             "src.tournament is not available in this build; there is no "
                             "blind round and this council will not fake one")
        models = [_text(_field(seat, "model")) for seat in seats]
        if any(not m for m in models):
            return _degraded(result, "no_model",
                             "every seat in a blind round needs a model; at least one has none")
        if len(models) < 2:
            return _degraded(result, "too_few_participants",
                             "a blind round contrasts answers, so it needs at least two "
                             f"participants; {len(models)} were given")
        call = self._model_call(engine)
        if call is None:
            return _degraded(result, "engine_unavailable",
                             "tournament.default_llm_call is unavailable; nothing can talk "
                             "to the models")
        state: Dict[str, Any] = {}
        cancel = asyncio.Event()
        timed_out = False
        try:
            await asyncio.wait_for(
                run(_text(question), models, rounds=1, llm_call=call,
                    state=state, cancel_event=cancel),
                max(1.0, float(timeout_s or 180.0)))
        except asyncio.TimeoutError:
            cancel.set()
            timed_out = True
        except asyncio.CancelledError:
            cancel.set()
            raise
        except Exception as exc:  # noqa: BLE001 - a refused round is a reason
            return _degraded(result, "round_failed",
                             f"the blind round failed: {type(exc).__name__}: {exc}")
        result["answers"] = _answers_for(seats, state)
        result["convergence"] = state.get("convergence")
        result["ranking"] = _text(state.get("ranking"))
        result["judge"] = state.get("judge")
        result["stopped_by"] = _text(state.get("stopped_by"))
        result["cancelled"] = list(state.get("cancelled") or ())
        result["errors"] = list(state.get("errors") or ())
        if timed_out:
            # `state` is the very dict `run` fills in as it goes, so a timeout
            # still hands back whatever answers arrived — a partial round the
            # user can read beats an empty one they cannot.
            return _degraded(result, "timeout",
                             f"the blind round did not finish within {float(timeout_s):.0f}s; "
                             f"{len([a for a in result['answers'] if a['content']])} of "
                             f"{len(seats)} answers arrived")
        spoke = [a for a in result["answers"] if a["content"]]
        if not spoke:
            return _degraded(result, "no_answers",
                             "no participant answered the blind round")
        result["ok"] = True
        if bool(state.get("degraded")) or len(spoke) < len(seats):
            result["degraded"] = True
            result["reason"] = "partial_round"
            result["detail"] = (f"{len(spoke)} of {len(seats)} participants answered; "
                                "the rest failed or were cancelled")
        return result

    async def judge(self, *, question: str, answers: Sequence[Any], rubric: str = "",
                    model: str = "") -> Dict[str, Any]:
        """Score answers with Tournament's judge, or return no score at all.

        §3.4 and §20 are the whole of this method: "un juez fallido no produce
        una puntuación inventada".  A judge that is missing, that raises, or
        that answers with something the rubric's parser cannot read comes back
        `ok=False` with `scores=None` and an empty `verdict`.  There is no
        branch in here that fills those in from anywhere else.
        """
        result: Dict[str, Any] = {
            "ok": False, "engine": "tournament", "judge_model": _text(model),
            "verdict": "", "scores": None, "ranking": [], "labels": {},
            "attempts": 0, "error": "", "degraded": False, "reason": "", "detail": "",
        }
        engine = _engine("tournament")
        judge_fn = getattr(engine, "_judge", None) if engine else None
        label_for = getattr(engine, "label_for", None) if engine else None
        if not (callable(judge_fn) and callable(label_for)):
            return _degraded(result, "judge_unavailable",
                             "src.tournament's judge is not available in this build; the "
                             "result stands as a synthesis with no authoritative verdict")
        rows = _judgeable(answers)
        if len(rows) < 2:
            return _degraded(result, "too_few_answers",
                             f"a judge compares answers; {len(rows)} was given")
        call = self._model_call(engine)
        if call is None:
            return _degraded(result, "engine_unavailable",
                             "tournament.default_llm_call is unavailable; no judge can run")
        scrub = getattr(engine, "_scrub", None)
        names = [row["model"] for row in rows if row["model"]]
        solutions = []
        for index, row in enumerate(rows):
            text = row["content"]
            if callable(scrub):
                try:
                    text = scrub(text, names)
                except Exception:  # noqa: BLE001 - unscrubbed is a caveat, not a crash
                    logger.exception("council adapters: scrubbing an answer failed")
            solutions.append({"label": label_for(index), "entry": index, "text": text})
        result["labels"] = {s["label"]: rows[s["entry"]]["participant_id"] for s in solutions}
        judge_model = _text(model) or self._strongest(engine, names) or (names[0] if names else "")
        result["judge_model"] = judge_model
        if not judge_model:
            return _degraded(result, "no_judge_model",
                             "no model was given and none could be chosen to judge")
        pool = self._slots(engine)
        try:
            block = await judge_fn(_rubric_prompt(question, rubric), solutions,
                                   judge_model, call, pool, None, deque())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a broken judge scores nothing
            return _degraded(result, "judge_failed",
                             f"the judge raised {type(exc).__name__}: {exc}")
        block = dict(block or {})
        result["attempts"] = int(block.get("attempts") or 0)
        result["error"] = _text(block.get("error"))
        scores = block.get("scores")
        if not (block.get("ok") and isinstance(scores, Mapping) and scores):
            return _degraded(result, "judge_unreadable",
                             "the judge did not answer the rubric; the result keeps no score "
                             f"and no verdict ({result['error'] or 'no error reported'})")
        by_participant: Dict[str, Any] = {}
        for label, row in scores.items():
            participant = result["labels"].get(_text(label), "")
            if participant:
                by_participant[participant] = dict(row) if isinstance(row, Mapping) else row
        missing = [p for p in result["labels"].values() if p not in by_participant]
        result["scores"] = by_participant
        result["ranking"] = _ranked(by_participant)
        result["verdict"] = result["ranking"][0] if result["ranking"] else ""
        result["ok"] = True
        if missing:
            result["degraded"] = True
            result["reason"] = "partial_judgement"
            result["detail"] = (f"the judge scored {len(by_participant)} of "
                                f"{len(result['labels'])} answers; the rest carry no score")
        return result

    # -- the engine's own helpers, guarded ---------------------------------

    def _model_call(self, engine: Any) -> Any:
        factory = getattr(engine, "default_llm_call", None) if engine else None
        if not callable(factory):
            return None
        try:
            return factory(self._owner or None)
        except Exception:  # noqa: BLE001 - a caller with no model function degrades
            logger.exception("council adapters: tournament.default_llm_call failed")
            return None

    @staticmethod
    def _strongest(engine: Any, names: Sequence[str]) -> str:
        chooser = getattr(engine, "strongest", None) if engine else None
        if not callable(chooser):
            return ""
        try:
            return _text(chooser(list(names)))
        except Exception:  # noqa: BLE001 - a read path
            logger.exception("council adapters: tournament.strongest failed")
            return ""

    @staticmethod
    def _slots(engine: Any) -> Any:
        slots = getattr(engine, "gpu_slots", None) if engine else None
        if not callable(slots):
            return None
        try:
            return slots()
        except Exception:  # noqa: BLE001 - a council without the semaphore still runs
            logger.exception("council adapters: tournament.gpu_slots failed")
            return None


def _answers_for(seats: Sequence[Any], state: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Tournament's flat answer records, mapped back onto the seats.

    Every seat appears, answered or not: a participant that failed or was
    cancelled is a fact the coordinator needs, and leaving it out of the list
    would let silence read as agreement (§3.4).
    """
    latest: Dict[int, Dict[str, Any]] = {}
    for record in state.get("answers") or ():
        if not isinstance(record, Mapping):
            continue
        try:
            index = int(record.get("entry"))
        except (TypeError, ValueError):
            continue
        latest[index] = dict(record)
    out: List[Dict[str, Any]] = []
    for index, seat in enumerate(seats):
        record = latest.get(index, {})
        content = _text(record.get("text"))
        out.append({
            "participant_id": _text(_field(seat, "id")),
            "model": _text(record.get("model")) or _text(_field(seat, "model")),
            "content": content,
            "round": record.get("round", 0),
            "tokens": record.get("tokens"),
            "tokens_source": _text(record.get("tokens_source")),
            "elapsed_s": record.get("elapsed_s"),
            "outcome": "success" if content else "no_answer",
        })
    return out


def _judgeable(answers: Sequence[Any]) -> List[Dict[str, str]]:
    """The answers a judge can actually compare: the ones with text in them."""
    rows: List[Dict[str, str]] = []
    for index, answer in enumerate(answers or ()):
        if isinstance(answer, str):
            row = {"participant_id": f"answer_{index}", "model": "", "content": answer.strip()}
        else:
            row = {"participant_id": _text(_field(answer, "participant_id")) or f"answer_{index}",
                   "model": _text(_field(answer, "model")),
                   "content": _text(_field(answer, "content")) or _text(_field(answer, "text"))}
        if row["content"]:
            rows.append(row)
    return rows


def _rubric_prompt(question: str, rubric: str) -> str:
    """The question the judge scores against, with the task's rubric attached.

    §4.3: "El juez debe utilizar una rúbrica configurada para la tarea, no
    aplicar siempre los tres ejes genéricos de Tournament".  The axes stay
    Tournament's — they are what its parser reads back — and the rubric says
    what they mean for THIS task.
    """
    body = _text(question)
    extra = _text(rubric)
    if not extra:
        return body
    return f"{body}\n\nRubric for this decision (apply it to every axis):\n{extra}"


def _ranked(scores: Mapping[str, Any]) -> List[str]:
    """Participant ids best first.  An unscored answer sorts last, never out."""
    def key(item: Tuple[str, Any]) -> Tuple[int, float, str]:
        participant, row = item
        total = row.get("total") if isinstance(row, Mapping) else None
        try:
            value = float(total)
        except (TypeError, ValueError):
            return (1, 0.0, participant)
        return (0, -value, participant)

    return [participant for participant, _ in sorted(scores.items(), key=key)]


# ── the verdict is `prove`'s, and only `prove`'s (§20) ─────────────────────

def verify_task(*, task: Any, session: Any, changes: Optional[Mapping[str, Any]] = None,
                verification: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Reconcile what a task CLAIMED with what Faustus OBSERVED, via `prove`.

    The two inputs are deliberately not symmetric, and §20 is why:

    * `changes` is the observation — `dispatch.compact(job)["result"]["changes"]`,
      a checkpoint diff or an mtime snapshot.  It is what Faustus saw on disk.
    * the claims are the LEDGER's `claimed_resources` for this task: the
      resources the room granted this participant before it wrote anything.
      They are never read from a worker's report, because the entire point of
      the comparison is that the report might be wrong.

    `verified` appears here only when `prove` says `proved`.  A task with no
    ChangeSet behind it comes back `unproved` however confidently a worker
    announced it had finished — and `unproved` is not a failure, it is the
    honest answer that nothing here can show it.

    One gate is deliberately NOT here: an open blocking objection also stops a
    task being called verified (§12.1), and that question belongs to
    `ledger.can_verify(task_id)`, which owns the objections.  A caller closing
    a task asks both; this function answers only about evidence.

    The ChangeSet's `intent` is always `implement`, the strictest of them.  A
    council task carries no intent field, and guessing `explore` for one would
    excuse exactly the gap that matters — a change with no verification behind
    it.  A task that wrote nothing is `unproved` under either intent, so the
    conservative choice costs an honest task nothing.
    """
    task_id = _text(_field(task, "id"))
    result: Dict[str, Any] = {
        "ok": False, "engine": "prove", "task_id": task_id, "verdict": UNPROVED,
        "confidence": 0.0, "uncertainty": [], "observations": [],
        "changeset_id": "", "changeset": {}, "proof": {},
        "claims": [], "changes": dict(changes or {}), "verification": dict(verification or {}),
        "degraded": False, "reason": "", "detail": "",
    }
    result["claims"] = [{"path": path, "kind": "modified"}
                        for path in _str_list(_field(task, "claimed_resources", ()))]
    builder = _engine("changesets")
    build = getattr(builder, "build", None) if builder else None
    judge = getattr(builder, "judge", None) if builder else None
    if not (callable(build) and callable(judge)):
        return _degraded(result, "engine_unavailable",
                         "src.changesets is not available in this build; with no ChangeSet "
                         "there is no evidence, and the verdict stays `unproved`")
    try:
        changeset = build(
            intent="implement",
            workspace=_text(_field(session, "workspace")),
            checkpoint=_text(result["changes"].get("checkpoint")),
            changes=result["changes"],
            verification=result["verification"],
            claims=result["claims"],
            title=_text(_field(task, "title")),
            run_id=_text(_field(task, "run_id")),
            owner=_text(_field(session, "owner")),
            project_id=_text(_field(session, "project_id")),
        )
    except Exception as exc:  # noqa: BLE001 - a ChangeSet that cannot be built proves nothing
        return _degraded(result, "changeset_failed",
                         f"the ChangeSet could not be built ({type(exc).__name__}: {exc}); "
                         "with no evidence packet the verdict stays `unproved`")
    result["changeset_id"] = _text(_field(changeset, "id"))
    to_dict = getattr(changeset, "to_dict", None)
    if callable(to_dict):
        try:
            result["changeset"] = dict(to_dict())
        except Exception:  # noqa: BLE001 - the audit view is a courtesy
            logger.exception("council adapters: a ChangeSet could not be serialised")
    try:
        proof = judge(changeset)
    except Exception as exc:  # noqa: BLE001 - `prove` is total, a double may not be
        return _degraded(result, "proof_failed",
                         f"the proof could not be built ({type(exc).__name__}: {exc}); "
                         "the verdict stays `unproved`")
    proof = dict(proof or {})
    result["proof"] = proof
    raw = _text(proof.get("verdict")).lower()
    result["verdict"] = _VERDICT_FROM_PROOF.get(raw, UNPROVED)
    try:
        result["confidence"] = float(proof.get("confidence") or 0.0)
    except (TypeError, ValueError):
        result["confidence"] = 0.0
    result["uncertainty"] = list(proof.get("uncertainty") or ())
    result["observations"] = list(proof.get("observations") or ())
    if raw not in _VERDICT_FROM_PROOF:
        return _degraded(result, "unknown_verdict",
                         f"`prove` answered {raw!r}, which this build does not know; "
                         "the verdict stays `unproved`")
    result["ok"] = True
    return result


# ── what the service builds when nobody injected anything ─────────────────

def default_invoker(**kw: Any) -> StreamingChatInvoker:
    """The invoker a `CouncilService` uses unless it was given one."""
    return StreamingChatInvoker(**kw)


def default_executor(**kw: Any) -> DispatchTaskExecutor:
    """The executor a `CouncilService` uses unless it was given one."""
    return DispatchTaskExecutor(**kw)
