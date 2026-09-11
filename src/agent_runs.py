"""Detached agent-run manager.

Keeps an agent/chat stream running server-side after the SSE client disconnects
(tab close, navigate away, refresh). The streaming generator is drained by a
background asyncio task into a per-session replay buffer; SSE clients SUBSCRIBE
to that buffer (replay everything so far, then live). Closing the SSE only drops
the subscriber — the drain task keeps going.

The wrapped generator already persists the assistant message to the session on
completion, so reopening the session shows the finished result even if nobody
was connected when it finished. Reconnecting mid-run replays the buffer + streams
live (pick up where it is).

Durability
----------
* In memory while the server process runs (tab close / navigation / refresh).
* On disk as a replay log (DATA_DIR/runs/<session>.jsonl) so a run that the
  process took down with it (restart, crash) is not lost: at the next startup
  `recover_interrupted_runs()` turns every log that never reached a terminal
  status into a saved, clearly-marked partial assistant message ("interrupted
  by a restart") and flags the chat in the sidebar. The generation itself
  cannot be resumed — the model state is gone — but nothing the run produced
  disappears, and the user can press Continue.

Queue
-----
Runs may carry a *lane* ("local" for a local GPU endpoint). A lane admits
`limit` concurrent runs (setting `agent_queue_local_concurrency`, default 1 —
one GPU, one generation); the rest wait FIFO with a live `queue_status`
event (position) so several requests can be fired off and the GPU works
through them one by one, each chat notifying when it is done. Stop works on a
queued run too (it leaves the queue).
"""
import asyncio
import json
import logging
import os
import re
import threading
import time
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional

from src import api_version

logger = logging.getLogger(__name__)


def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


# ---------------------------------------------------------------------------
# OBS-02: the eight phases a turn can honestly be in, in the order they occur.
# ---------------------------------------------------------------------------
#
# Every module that has ever reported a run's phase invented its own words
# for it (this module: starting/queued/waiting_model/tool/thinking/writing/
# research/awaiting_user/finishing; src/vram_admission.py's admission gate:
# vram_blocked/unloading_model/warning; src/research_handler.py's model
# readiness: loading_model/probing). None of those get deleted or renamed —
# a client reading `phase` today keeps reading exactly the same values
# (COMUN rule 3) — but nothing outside this module could ask "is this run
# queued, loading, generating, or waiting on a human" without knowing every
# subsystem's private vocabulary. `phase_canonical` is that one answer,
# `phase_raw` is the ad hoc value it was computed from (nothing lost), and
# `CANONICAL_PHASES` is the closed set `phase_canonical` is ever one of.
CANONICAL_PHASES = (
    "queued", "admission", "loading_model", "prefill",
    "generating", "tool", "verifying", "waiting_human",
)

#: ad hoc phase string (this module's own, or one an upstream module's
#: progress payload carries) -> canonical bucket. Unmapped values leave the
#: previous canonical bucket in place (see `_canonical_phase`) rather than
#: guessing -- a phase word nothing has classified yet is not evidence the
#: run moved to a different stage of the turn.
_PHASE_CANON: Dict[str, str] = {
    # This module's own vocabulary (_set_phase / _observe_activity).
    "starting": "admission",
    "queued": "queued",
    "waiting_model": "prefill",
    "thinking": "generating",
    "writing": "generating",
    "tool": "tool",
    "research": "tool",
    "awaiting_user": "waiting_human",
    "finishing": "generating",
    # src/vram_admission.py's admission-gate phases (`vram_admission` SSE
    # events, routes/chat_routes.py::_vram_admission_events).
    "vram_blocked": "admission",
    "unloading_model": "loading_model",
    # src/research_handler.py's model-readiness phases (`research_progress`
    # SSE events, when the payload carries its own `phase`).
    "loading_model": "loading_model",
    "probing": "loading_model",
    # This module's own word for genuine post-tool verification work (see
    # `_HARNESS_VERIFICATION_STATUSES` / `_observe_activity`'s `harness_check`
    # branch below) — self-mapped, same pattern as "tool": "tool" above.
    "verifying": "verifying",
}

#: `harness_check` (src/agent_loop.py's reliability harness) statuses that
#: are actual verification work — static analysis, a test run, an
#: independent reviewer model, or a claims check — as opposed to a status
#: that is really "make the model continue" (checkpoint/auto_continue/
#: think_cutoff/rejected/empty_round/unknown_tool/required_action/
#: target_substituted). Only these move the phase; everything else leaves
#: it exactly where it was — a harness nudge is not evidence the turn moved
#: to a new stage, and treating it as one would be exactly the kind of
#: invented signal OBS-02 exists to stop.
_HARNESS_VERIFICATION_STATUSES = frozenset({
    "static_analysis", "syntax_error", "tests_running", "tests_failed",
    "review_running", "review_issues", "verified", "unverified",
})


def _canonical_phase(raw_phase: str, previous: str = "admission") -> str:
    """The canonical bucket for an ad hoc phase string, or `previous`
    (sticky) when nothing has classified this word yet. Never raises, never
    invents a bucket a mapping entry did not vouch for."""
    return _PHASE_CANON.get(str(raw_phase or ""), previous)


def _outcome_of(status: str) -> Optional[str]:
    """The four-value outcome (src/tool_outcome.py) of a run status: a run the
    user stopped is `cancelled`, not an error. None while the setting is off
    (so nothing new is written and nothing counts differently) and None while
    the run is still going — an unfinished run has no outcome yet."""
    if str(status or "").strip().lower() in ("", "running", "queued", "pending"):
        return None
    try:
        from src import tool_outcome
        if not tool_outcome.enabled():
            return None
        return tool_outcome.classify_status(status).value
    except Exception:  # noqa: BLE001 - bookkeeping, never load-bearing
        return None


class _Run:
    __slots__ = ("buffer", "subscribers", "status", "task", "evict_task", "run_id", "last_key",
                 "lane", "queued_position", "log", "started_at", "label",
                 "phase", "phase_since", "last_event_at", "round", "tool", "detail",
                 "model", "endpoint_url",
                 # OBS-02: canonical phase vocabulary, additive to `phase`
                 # (see the CANONICAL_PHASES block above) -- phase_raw is
                 # ALWAYS the exact ad hoc word a module used, never lost.
                 "phase_raw", "phase_canonical",
                 # OBS-02: a completion percentage, but ONLY when a real
                 # total is known (todowrite's done/total count today --
                 # see `_observe_activity`'s "progress_update" branch). None
                 # means exactly that: no measurable total, so nothing is
                 # shown rather than a number invented from elapsed time.
                 "percent",
                 # UX-04: pause/steer/queue for the MAIN session turn (the
                 # subagent equivalents are src/agent_tools/subagent_tools.py's
                 # own _WORKER_RUNS — deliberately separate stores, same shape).
                 # `pause_requested` is consumed once by stream_agent_loop's
                 # `pending_pause` at the next safe point. `steer_queue` is
                 # drained the same way via `pending_user_messages` and applied
                 # to the CURRENT turn. `send_after_queue` is NOT applied to the
                 # live turn; it is delivered as a new chat turn once this one
                 # ends (drained by the route layer via take_send_after).
                 "pause_requested", "steer_queue", "send_after_queue")

    @property
    def outcome(self) -> Optional[str]:
        return _outcome_of(self.status)

    def __init__(self, lane: Optional[str] = None, label: str = "") -> None:
        self.buffer: list = []          # ordered SSE event strings (replay log)
        self.subscribers: set = set()   # one asyncio.Queue per connected client
        self.status: str = "running"    # running | done | error | stopped
        self.task: Optional[asyncio.Task] = None
        self.evict_task: Optional[asyncio.Task] = None
        # Stable across every subscription/replay of this exact detached run.
        # The browser uses it to make local cost accounting replay-idempotent.
        self.run_id: str = uuid.uuid4().hex
        self.last_key: Optional[str] = None   # compaction key of buffer[-1] (see _compact_key)
        self.lane: Optional[str] = lane
        self.queued_position: int = 0         # >0 while waiting for the lane
        self.log: Optional["_RunLog"] = None
        self.started_at: float = time.time()
        self.label: str = label
        # A compact, owner-filtered account of what the detached task is doing.
        # The replay buffer has the evidence, but making every sidebar parse a
        # potentially huge token stream just to distinguish prefill from a
        # running tool would be wasteful and brittle.
        self.phase: str = "starting"
        self.phase_since: float = self.started_at
        self.last_event_at: float = self.started_at
        self.round: int = 1
        self.tool: Optional[str] = None
        self.detail: str = ""
        # Which model on which endpoint, so the heartbeat can ask Ollama what
        # the model is doing while the turn waits for its first token.
        self.model: str = ""
        self.endpoint_url: str = ""
        # OBS-02
        self.phase_raw: str = self.phase
        self.phase_canonical: str = _canonical_phase(self.phase)
        self.percent: Optional[float] = None
        # UX-04
        self.pause_requested: bool = False
        self.steer_queue: list = []
        self.send_after_queue: list = []


_RUNS: Dict[str, _Run] = {}

# The last /api/ps answer per Ollama root, so ten idle heartbeats a minute do
# not become ten HTTP calls; the state changes on the scale of seconds.
_MODEL_STATE_CACHE: Dict[str, tuple] = {}
_MODEL_STATE_TTL_S = 4.0


async def model_state(run: "_Run") -> Optional[Dict[str, Any]]:
    """What the run's model is doing right now, from Ollama's /api/ps.

    "Waiting for the model" with the model loaded read as a hang (Luis,
    09-09-2026: 35 GB in VRAM and PCIe spill at 01:28). The three states
    that look identical from the browser are different problems: not
    resident yet (loading, 13-34 s cold), resident and fully on the GPU
    (reading a long context — prefill), and resident but spilling to RAM
    (everything is slow and will stay slow). None for remote endpoints,
    an unknown model, or an Ollama that does not answer.
    """
    if not run.model or not run.endpoint_url:
        return None
    try:
        from src.vram_admission import ollama_root
        root = ollama_root(run.endpoint_url)
    except Exception:
        return None
    if not root:
        return None
    now = time.time()
    cached = _MODEL_STATE_CACHE.get(root)
    if cached and now - cached[0] < _MODEL_STATE_TTL_S:
        models = cached[1]
    else:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=1.5) as client:
                r = await client.get(f"{root}/api/ps")
                r.raise_for_status()
                models = list((r.json() or {}).get("models") or [])
        except Exception:
            return None
        _MODEL_STATE_CACHE[root] = (now, models)
    want = str(run.model).strip().lower()
    for m in models:
        names = {str(m.get("name") or "").lower(), str(m.get("model") or "").lower()}
        if want in names or (":" not in want and f"{want}:latest" in names):
            size = int(m.get("size") or 0)
            vram = int(m.get("size_vram") or 0)
            return {
                "resident": True,
                "size_bytes": size,
                "vram_bytes": vram,
                "spill": bool(size and vram < size * 0.98),
                "context": int(m.get("context_length") or 0),
            }
    return {"resident": False}

# How long a FINISHED run (and its full replay buffer) is retained after the
# last subscriber disconnects, so a reconnect within the window can still
# replay the result. After this, the run is evicted to bound memory — without
# it, every session that ever streamed kept its entire event log forever.
_EVICT_GRACE_S = 180


_PROGRESS_PREFIX = 'data: {"type": "tool_progress"'


def _compact_key(ev: str) -> Optional[str]:
    """Replay-log compaction key for a live-progress event, else None.

    A long bash/python command emits a `tool_progress` (elapsed + stdout tail)
    every 2 s; only the LATEST one matters for a client that reconnects, and a
    1-hour command would otherwise leave ~1800 near-identical events in the
    buffer. Consecutive progress events of the same tool call collapse into
    one slot.

    Sub-agent board events are state changes and are never merged — except
    the two periodic ones: a worker's own bash tail (`tool`/`progress`, keyed
    by worker id + tool) and the watchdog `tick` (keyed by worker id).
    """
    if not ev.startswith(_PROGRESS_PREFIX):
        return None
    try:
        d = json.loads(ev[6:])
    except Exception:
        return None
    sa = d.get("subagent")
    if isinstance(sa, dict):
        if sa.get("event") == "tick":
            return f"subagent|{sa.get('id')}|tick"
        if sa.get("event") == "tool" and sa.get("phase") == "progress":
            return f"subagent|{sa.get('id')}|progress|{sa.get('tool')}"
        return None
    if "subagent" in d:
        return None
    return f"{d.get('tool')}|{d.get('round')}|{d.get('approved')}"


# ---------------------------------------------------------------------------
# On-disk replay log
# ---------------------------------------------------------------------------

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def _runs_dir() -> str:
    try:
        from src.constants import DATA_DIR
    except Exception:  # pragma: no cover
        DATA_DIR = os.path.join(os.getcwd(), "data")
    return os.path.join(DATA_DIR, "runs")


def _log_path(session_id: str) -> str:
    return os.path.join(_runs_dir(), _SAFE_NAME_RE.sub("_", str(session_id))[:120] + ".jsonl")


def persistence_enabled() -> bool:
    return bool(_setting("agent_runs_persist", True))


class _RunLog:
    """Append-only JSONL mirror of a run's replay buffer. Deltas are flushed in
    small batches; every other event (tool cards, harness, status) is flushed
    immediately so a crash loses at most a few tokens of prose."""

    def __init__(self, session_id: str, run: _Run):
        self.path = _log_path(session_id)
        self.session_id = session_id
        self._f = None
        self._pending = 0
        self._orphaned = False
        self._lock = threading.Lock()
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self._f = open(self.path, "w", encoding="utf-8")
            self._write({"status": "running", "run_id": run.run_id, "ts": time.time(),
                         "session_id": session_id, "lane": run.lane, "label": run.label}, flush=True)
        except OSError as e:
            logger.debug("[agent-run] log unavailable for %s: %s", session_id, e)
            self._f = None

    def orphan(self) -> None:
        """Detach this log from its file: the run it belongs to was replaced and
        a NEW _RunLog now owns `self.path` (same session → same file name, and
        it opened the file with "w"). Anything this one wrote afterwards — its
        remaining events, and above all its `finish()` status line — landed
        INSIDE the new run's log, which then read back as terminal (so
        `recover_interrupted_runs` skipped the live run) or as a mix of both
        runs' text. Every later write is a no-op and the descriptor is closed.
        """
        with self._lock:
            self._orphaned = True
            try:
                if self._f is not None:
                    self._f.close()
            except OSError:
                pass
            self._f = None

    def _write(self, obj: dict, flush: bool) -> None:
        if self._f is None or self._orphaned:
            return
        with self._lock:
            # Re-check inside the lock: orphan() may have closed the file
            # between the fast path above and here.
            if self._f is None or self._orphaned:
                return
            try:
                self._f.write(json.dumps(obj, ensure_ascii=False) + "\n")
                self._pending += 1
                if flush or self._pending >= 25:
                    self._f.flush()
                    self._pending = 0
            except (OSError, ValueError):
                self._f = None

    def event(self, seq: int, ev: str, replaced: bool) -> None:
        is_delta = ev.startswith('data: {"delta"')
        self._write({"seq": seq, "ev": ev, "r": replaced} if replaced else {"seq": seq, "ev": ev}, flush=not is_delta)

    def finish(self, status: str) -> None:
        line = {"status": status, "ts": time.time()}
        outcome = _outcome_of(status)
        if outcome:
            line["outcome"] = outcome
        self._write(line, flush=True)
        with self._lock:
            try:
                if self._f is not None:
                    self._f.close()
            except OSError:
                pass
            self._f = None


def _augment_sse_fields(ev: str, fields: Dict[str, Any]) -> str:
    """Add `fields` into a `data: {...}` SSE frame's JSON object payload,
    appended AFTER any existing keys, so byte-for-byte prefix checks
    elsewhere (`_compact_key`'s `_PROGRESS_PREFIX`) keep matching as long as
    `type` was already the payload's first key -- which every event this
    module or agent_loop.py emits already puts first.

    Never overrides a key the payload already has: this is strictly
    additive, so a value agent_loop.py deliberately set (or an old client's
    own `sequence`-shaped field, however unlikely) is never clobbered.

    Any frame this cannot safely parse as an object payload -- the `[DONE]`
    sentinel, a non-JSON body, a JSON array/scalar -- passes through
    unchanged rather than guessing at a shape it is not.
    """
    if not fields:
        return ev
    event_line = ""
    rest = ev
    if ev.startswith("event:"):
        nl = ev.find("\n")
        if nl == -1:
            return ev
        event_line, rest = ev[: nl + 1], ev[nl + 1 :]
    if not rest.startswith("data: "):
        return ev
    body = rest[len("data: ") :]
    if body.endswith("\n\n"):
        body = body[:-2]
    else:
        body = body.rstrip("\n")
    if body.strip() == "[DONE]":
        return ev
    try:
        payload = json.loads(body)
    except ValueError:
        return ev
    if not isinstance(payload, dict):
        return ev
    changed = False
    for k, v in fields.items():
        if k not in payload:
            payload[k] = v
            changed = True
    if not changed:
        return ev
    return event_line + "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _observability_fields(run: _Run, *, sequence: int) -> Dict[str, Any]:
    """OBS-01/QA-09 fields every SSE event of this run's stream carries,
    additively: `trace_id` (one per turn -- the run's own opaque identity,
    already the value handed to the client as X-Odysseus-Run-Id, so nothing
    new to correlate), `step_id` (one per round, from the round this
    module's own `_observe_activity` already tracks), `sequence`/`stream_id`
    (QA-09 replay: a stream IS one detached run, so its run_id doubles as
    its stream_id -- reusing the identity rather than minting a second one),
    and `schema_version` (ARCH-01's negotiated wire version)."""
    return {
        "trace_id": run.run_id,
        "step_id": f"{run.run_id}:{run.round}",
        "sequence": sequence,
        "stream_id": run.run_id,
        "schema_version": api_version.API_VERSION,
    }


def _publish(run: _Run, ev: str) -> None:
    """Append one SSE event (or replace the previous progress tick of the same
    tool call) and fan it out to every live subscriber."""
    _observe_activity(run, ev)
    key = _compact_key(ev)
    replaced = key is not None and run.last_key == key and bool(run.buffer)
    # 1-based, growing per stream, and stable across a compacted replace: a
    # progress tick that overwrites the previous one keeps that tick's own
    # sequence number, matching what a client de-duping by (stream_id,
    # sequence) already expects -- the newer content for the same slot.
    seq = len(run.buffer) if not replaced else len(run.buffer) - 1
    ev = _augment_sse_fields(ev, _observability_fields(run, sequence=seq + 1))
    if replaced:
        run.buffer[-1] = ev
    else:
        run.buffer.append(ev)
    run.last_key = key
    if run.log is not None:
        run.log.event(seq, ev, replaced)
    for q in list(run.subscribers):
        try:
            q.put_nowait((seq, ev, replaced))
        except Exception:
            pass


def _brief(value: Any, limit: int = 160) -> str:
    """One safe line for an activity card, never a full command or output."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    try:
        from core.log_safety import redact_secrets
        text = redact_secrets(text)
    except Exception:
        pass
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _set_phase(run: _Run, phase: str, *, tool: Optional[str] = None, detail: Any = "") -> None:
    now = time.time()
    if phase != run.phase or tool != run.tool:
        run.phase_since = now
    run.phase = phase
    run.last_event_at = now
    run.tool = tool
    run.detail = _brief(detail)
    # OBS-02: this module's own vocabulary always maps to a canonical bucket
    # (every value `_set_phase` is ever called with has an entry in
    # _PHASE_CANON above), computed here so every call site gets it for free.
    run.phase_raw = phase
    run.phase_canonical = _canonical_phase(phase, previous=run.phase_canonical)


def _observe_activity(run: _Run, ev: str) -> None:
    """Fold one replay event into the small live activity snapshot.

    This never affects delivery.  Unknown/malformed events merely count as a
    sign of life, so observability cannot break a model run.
    """
    run.last_event_at = time.time()
    if ev.startswith("data: [DONE]"):
        _set_phase(run, "finishing")
        return
    if not ev.startswith("data: "):
        return
    try:
        payload = json.loads(ev[6:])
    except (TypeError, ValueError):
        return
    if not isinstance(payload, dict):
        return
    if "delta" in payload and not payload.get("type"):
        _set_phase(run, "thinking" if payload.get("thinking") else "writing")
        return
    event_type = str(payload.get("type") or "")
    if event_type == "queue_status":
        if payload.get("queued"):
            _set_phase(run, "queued", detail=f"position {payload.get('position') or '?'}")
        else:
            _set_phase(run, "waiting_model")
    elif event_type == "tool_start":
        try:
            run.round = max(run.round, int(payload.get("round") or run.round))
        except (TypeError, ValueError):
            pass
        _set_phase(run, "tool", tool=_brief(payload.get("tool"), 64) or "tool",
                   detail=payload.get("command") or payload.get("full_command"))
    elif event_type == "tool_progress":
        # Worker-board progress is still useful as a sign of life, but its
        # nested payload can be large and may contain task instructions.
        detail = payload.get("message") or payload.get("event") or payload.get("tail")
        _set_phase(run, "tool", tool=_brief(payload.get("tool"), 64) or run.tool or "tool",
                   detail=detail)
    elif event_type == "tool_output":
        _set_phase(run, "waiting_model")
    elif event_type == "agent_step":
        try:
            run.round = max(run.round, int(payload.get("round") or run.round))
        except (TypeError, ValueError):
            pass
        _set_phase(run, "waiting_model")
    elif event_type == "research_progress":
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        detail = data.get("message") or data.get("phase") or "research"
        _set_phase(run, "research", detail=detail)
        # OBS-02: src/research_handler.py's own model-readiness phase
        # (loading_model/probing) is more specific than the "research"
        # bucket _set_phase just computed from THIS module's vocabulary --
        # refine the canonical bucket with it when present, without touching
        # `phase`/`phase_raw` (those stay this module's own value; a wire
        # consumer reading them today sees nothing new).
        _raw = data.get("phase")
        if isinstance(_raw, str) and _raw in _PHASE_CANON:
            run.phase_canonical = _canonical_phase(_raw, previous=run.phase_canonical)
    elif event_type == "ask_user":
        _set_phase(run, "awaiting_user")
    elif event_type in {"model_info", "fallback"}:
        _set_phase(run, "waiting_model")
    elif event_type == "vram_admission":
        # OBS-02: src/vram_admission.py's admission-gate phase
        # (vram_blocked/unloading_model/warning/...), routed through
        # routes/chat_routes.py::_vram_admission_events. Refines ONLY the
        # canonical bucket -- `phase`/`phase_raw` stay whatever this
        # module's own state machine says, so nothing already reading them
        # sees a value it has never seen before.
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        _raw = data.get("phase")
        if isinstance(_raw, str) and _raw in _PHASE_CANON:
            run.phase_canonical = _canonical_phase(_raw, previous=run.phase_canonical)
    elif event_type == "progress_update":
        # OBS-02: "a % only when there is a measurable total" -- the ONE
        # total agent_runs can see honestly today is the plan's own
        # done/total count from `todowrite` (src/agent_loop.py emits this
        # event with the ledger's annotated todos). No progress_update ever
        # observed -> `run.percent` stays None -> activity_snapshot omits
        # the key entirely, never a fabricated number.
        todos = payload.get("todos")
        if isinstance(todos, list) and todos:
            total = len(todos)
            done = sum(1 for t in todos if isinstance(t, dict) and t.get("status") == "done")
            run.percent = round(100.0 * done / total, 1)
    elif event_type == "harness_check":
        # OBS-02: the eighth canonical phase. `_HARNESS_VERIFICATION_STATUSES`
        # is the closed set of statuses that are real verification; anything
        # else (checkpoint, auto_continue, ...) is a harness nudge, not a
        # new stage, so the phase is left untouched for those.
        status = str(payload.get("status") or "")
        if status in _HARNESS_VERIFICATION_STATUSES:
            _set_phase(run, "verifying", detail=status)


def activity_snapshot(session_id: str) -> Optional[Dict[str, Any]]:
    """Current phase of one detached run; None for absent/terminal runs."""
    run = _RUNS.get(session_id)
    if run is None or run.status != "running":
        return None
    now = time.time()
    snapshot = {
        "run_id": run.run_id,
        # OBS-01: the run IS the stream/trace for one turn -- see _publish's
        # per-event trace_id, which reuses this same identity.
        "trace_id": run.run_id,
        "status": run.status,
        "phase": run.phase,
        "phase_since": run.phase_since,
        "last_event_at": run.last_event_at,
        "server_alive_at": now,
        "started_at": run.started_at,
        "elapsed_s": max(0, round(now - run.started_at, 1)),
        "round": run.round,
        "tool": run.tool,
        "detail": run.detail,
        "queued_position": run.queued_position,
        "label": run.label,
        "subscribers": len(run.subscribers),
        # OBS-02: additive. phase_raw duplicates `phase` under the name the
        # spec asks for (nothing about `phase` itself changes -- see the
        # CANONICAL_PHASES block); phase_canonical is the new 8-value
        # vocabulary; percent is present ONLY when a measurable total was
        # observed (never a fabricated number -- see "progress_update" in
        # _observe_activity).
        "phase_raw": run.phase_raw,
        "phase_canonical": run.phase_canonical,
    }
    if run.percent is not None:
        snapshot["percent"] = run.percent
    return snapshot


async def _heartbeat_snapshot(session_id: str, run: _Run) -> Dict[str, Any]:
    """The activity snapshot plus, while the turn waits for its first token,
    what Ollama says the model is doing (see model_state)."""
    snapshot = activity_snapshot(session_id) or {}
    if snapshot.get("phase") in ("waiting_model", "starting"):
        try:
            state = await model_state(run)
        except Exception:  # noqa: BLE001 - observability never breaks the stream
            state = None
        if state:
            snapshot["model_state"] = state
    return snapshot


def activity_details() -> Dict[str, Dict[str, Any]]:
    """Current detached-run details keyed by session id."""
    out: Dict[str, Dict[str, Any]] = {}
    for session_id in list(_RUNS):
        snapshot = activity_snapshot(session_id)
        if snapshot is not None:
            out[session_id] = snapshot
    return out


def _wake_run_subscribers(run: _Run) -> None:
    """Close subscribers even when the drain task never reached its body."""
    for q in list(run.subscribers):
        try:
            q.put_nowait((None, None, False))
        except Exception:
            pass


def _schedule_evict(session_id: str, expected_run: Optional[_Run] = None) -> None:
    """(Re)arm a grace-period eviction for a terminal run with no subscribers.
    Identity-checked so a run that gets replaced/reused is never evicted by a
    stale timer."""
    run = _RUNS.get(session_id)
    if run is None:
        return
    if expected_run is not None and run is not expected_run:
        return
    if run.evict_task and not run.evict_task.done():
        run.evict_task.cancel()

    async def _evict(run_ref: _Run) -> None:
        try:
            await asyncio.sleep(_EVICT_GRACE_S)
        except asyncio.CancelledError:
            return
        cur = _RUNS.get(session_id)
        if cur is run_ref and cur.status != "running" and not cur.subscribers:
            _RUNS.pop(session_id, None)

    run.evict_task = asyncio.create_task(_evict(run))


def is_active(session_id: str) -> bool:
    r = _RUNS.get(session_id)
    return bool(r and r.status == "running")


# Sessions that are busy WITHOUT a detached run of their own — e.g. the worker
# chats of `delegate_agents`, which are driven by the parent's tool call. They
# get the same blinking dot in the sidebar while their run is in flight.
_EXTERNAL_BUSY: set = set()


def mark_busy(session_id: Optional[str]) -> None:
    if session_id:
        _EXTERNAL_BUSY.add(session_id)


def clear_busy(session_id: Optional[str]) -> None:
    if session_id:
        _EXTERNAL_BUSY.discard(session_id)


def active_session_ids() -> List[str]:
    """Sessions with a run still going (sidebar activity dots): detached runs
    plus externally-marked busy sessions (sub-agent workers)."""
    ids = [sid for sid, r in list(_RUNS.items()) if r.status == "running"]
    for sid in list(_EXTERNAL_BUSY):
        if sid not in ids:
            ids.append(sid)
    return ids


def queued_positions() -> Dict[str, int]:
    """session → 1-based queue position, for runs still waiting for their lane."""
    return {sid: r.queued_position for sid, r in list(_RUNS.items()) if r.status == "running" and r.queued_position > 0}


def get_status(session_id: str) -> Optional[str]:
    r = _RUNS.get(session_id)
    return r.status if r else None


def get_outcome(session_id: str) -> Optional[str]:
    """The run's four-value outcome (`success` / `expected_error` /
    `cancelled` / `panic`), or None when there is no run (or the setting is
    off). A run the user stopped is `cancelled`, never a failure."""
    r = _RUNS.get(session_id)
    return r.outcome if r else None


def get_run_id(session_id: str) -> Optional[str]:
    """Return the opaque identity of the current detached run, if present."""
    r = _RUNS.get(session_id)
    return r.run_id if r else None


def get_active_run(session_id: str) -> Optional[_Run]:
    """Return the exact active run currently registered for a session."""
    r = _RUNS.get(session_id)
    return r if r and r.status == "running" else None


# ---------------------------------------------------------------------------
# Lanes (the task queue)
# ---------------------------------------------------------------------------

class _Lane:
    def __init__(self, name: str):
        self.name = name
        self.active: int = 0
        self.waiting: List[_Run] = []
        self.cond: Optional[asyncio.Condition] = None

    def _condition(self) -> asyncio.Condition:
        if self.cond is None:
            self.cond = asyncio.Condition()
        return self.cond

    @property
    def limit(self) -> int:
        key = "agent_queue_local_concurrency" if self.name == "local" else f"agent_queue_{self.name}_concurrency"
        try:
            v = int(_setting(key, 1 if self.name == "local" else 0) or 0)
        except (TypeError, ValueError):
            v = 1 if self.name == "local" else 0
        return v  # 0 = unlimited

    def positions(self) -> Dict[str, int]:
        return {r.run_id: i + 1 for i, r in enumerate(self.waiting)}

    async def acquire(self, run: _Run) -> None:
        limit = self.limit
        if limit <= 0:
            return
        cond = self._condition()
        self.waiting.append(run)
        try:
            async with cond:
                while True:
                    if self.active < limit and self.waiting and self.waiting[0] is run:
                        self.waiting.pop(0)
                        self.active += 1
                        run.queued_position = 0
                        _publish(run, "data: " + json.dumps({"type": "queue_status", "queued": False, "position": 0, "lane": self.name}) + "\n\n")
                        self._broadcast_positions()
                        return
                    pos = self.waiting.index(run) + 1 if run in self.waiting else 0
                    if pos != run.queued_position:
                        run.queued_position = pos
                        ahead = [r.label for r in self.waiting[: pos - 1]] if pos > 1 else []
                        _publish(run, "data: " + json.dumps({
                            "type": "queue_status", "queued": True, "position": pos, "lane": self.name,
                            "active": self.active, "ahead": ahead[:5],
                        }) + "\n\n")
                    await cond.wait()
        except BaseException:
            if run in self.waiting:
                self.waiting.remove(run)
            run.queued_position = 0
            async with cond:
                cond.notify_all()
            raise

    def _broadcast_positions(self) -> None:
        for i, r in enumerate(self.waiting):
            pos = i + 1
            if r.queued_position != pos:
                r.queued_position = pos
                _publish(r, "data: " + json.dumps({"type": "queue_status", "queued": True, "position": pos,
                                                   "lane": self.name, "active": self.active}) + "\n\n")

    async def prioritize(self, run_id: str) -> bool:
        """ACT-05: move a still-waiting run to the front of this lane's FIFO
        queue. Reuses `waiting` — the same list `acquire()` already treats as
        the queue's one authority — instead of a parallel priority store
        (COMUN rule 4). A run no longer in `waiting` (already admitted, or
        finished/stopped) has nothing left to reorder, so this returns False
        rather than inventing an effect for it."""
        cond = self._condition()
        async with cond:
            for i, r in enumerate(self.waiting):
                if r.run_id == run_id:
                    if i > 0:
                        self.waiting.pop(i)
                        self.waiting.insert(0, r)
                        self._broadcast_positions()
                        cond.notify_all()
                    return True
            return False

    async def release(self, run: _Run) -> None:
        if self.limit <= 0 and self.active == 0:
            return
        cond = self._condition()
        async with cond:
            self.active = max(0, self.active - 1)
            cond.notify_all()


_LANES: Dict[str, _Lane] = {}


def _lane(name: Optional[str]) -> Optional[_Lane]:
    if not name:
        return None
    lane = _LANES.get(name)
    if lane is None:
        lane = _Lane(name)
        _LANES[name] = lane
    return lane


def queue_snapshot() -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for name, lane in _LANES.items():
        out[name] = {"active": lane.active, "limit": lane.limit,
                     "waiting": [{"run_id": r.run_id, "label": r.label, "position": i + 1} for i, r in enumerate(lane.waiting)]}
    return out


async def prioritize_run(run_id: str) -> bool:
    """ACT-05: bump a queued run to the front of its lane. Looked up by
    `run_id` (opaque per-turn identity), not session id, because a session
    id is reused across turns and a stale one could silently prioritize the
    wrong run. Returns False for a run that is not currently waiting (running
    already, or unknown) — see `_Lane.prioritize`."""
    for run in list(_RUNS.values()):
        if run.run_id == run_id and run.status == "running" and run.lane:
            lane = _lane(run.lane)
            if lane is not None:
                return await lane.prioritize(run_id)
    return False


async def _drain(session_id: str, run: _Run, agen: AsyncGenerator[str, None],
                 prev_task: Optional[asyncio.Task] = None) -> None:
    """Pull every event from the wrapped generator into the run buffer, fanning
    each out to live subscribers. Runs to completion regardless of subscribers."""
    subscribers_woken = False
    lane = _lane(run.lane)
    acquired = False

    def _wake_subscribers() -> None:
        nonlocal subscribers_woken
        if subscribers_woken:
            return
        subscribers_woken = True
        _wake_run_subscribers(run)

    # If this run replaced an in-flight one (rapid double-send), wait for that
    # one to fully finish first. Its CancelledError handler calls aclose(), which
    # persists its partial response — letting it complete before we start writing
    # keeps the two runs' session saves sequential instead of interleaved.
    try:
        if prev_task is not None and not prev_task.done():
            await asyncio.wait({prev_task})
        if lane is not None:
            await lane.acquire(run)
            acquired = lane.limit > 0
        _ended_paused = False
        async for ev in agen:
            _publish(run, ev)
            # UX-04: stream_agent_loop's `pending_pause` break emits exactly
            # this event right before ending the generator normally -- same
            # shape as every other typed SSE event this loop already checks
            # by substring (see _observe_activity below), so a plain
            # generator "done" is told apart from "paused at a safe point,
            # resumable" without agent_runs reaching into the loop's
            # internals.
            if '"type": "paused"' in ev:
                _ended_paused = True
        if run.status == "running":
            run.status = "waiting_user" if _ended_paused else "done"
    except asyncio.CancelledError:
        run.status = "stopped"
        # Let the wrapped generator's own CancelledError handler run (it saves
        # the partial response to the session).
        try:
            await agen.aclose()
        except Exception:
            pass
        # A rapid third replacement can cancel this task while it is still
        # waiting for its predecessor. Close this run's subscribers promptly,
        # but keep the task alive until the predecessor finishes so the next
        # run still observes the transitive session-save ordering barrier.
        _wake_subscribers()
        if prev_task is not None and not prev_task.done():
            try:
                await asyncio.shield(prev_task)
            except (asyncio.CancelledError, Exception):
                pass
    except Exception as e:
        logger.error("[agent-run] %s failed: %s", session_id, e, exc_info=True)
        run.status = "error"
        _publish(
            run,
            "event: error\n"
            f"data: {json.dumps({'error': 'Agent run failed before completion.', 'status': 500})}\n\n",
        )
        _publish(run, "data: [DONE]\n\n")
    finally:
        if lane is not None and acquired:
            try:
                await lane.release(run)
            except Exception:
                pass
        if run.log is not None:
            try:
                run.log.finish(run.status if run.status != "running" else "done")
            except Exception:
                pass
        # Wake every subscriber with the end sentinel so their SSE closes.
        _wake_subscribers()
        # Run is terminal — arm the grace timer so it (and its buffer) is
        # eventually freed even if nobody ever reconnects. subscribe() cancels
        # this on connect and re-arms on disconnect.
        _schedule_evict(session_id, run)


def start(session_id: str, agen: AsyncGenerator[str, None], lane: Optional[str] = None, label: str = "",
          model: str = "", endpoint_url: str = "") -> _Run:
    """Start a detached run draining `agen` for a session. If a run is already in
    flight for this session (e.g. a rapid double-send), it's cancelled first.

    `lane` puts the run in a FIFO queue shared by every run of that lane
    (see _Lane); None runs immediately. `label` is what other queued chats see
    as "ahead of you"."""
    prev = _RUNS.get(session_id)
    prev_task: Optional[asyncio.Task] = None
    if prev:
        if prev.task and not prev.task.done():
            # A task cancelled before its first instruction never enters
            # _drain(), so its except/finally blocks cannot update status or
            # wake a response already bound to this exact run. Terminalize it
            # synchronously before cancelling; _drain's cleanup is idempotent
            # when the task had already started.
            if prev.status == "running":
                prev.status = "stopped"
                _wake_run_subscribers(prev)
            prev.task.cancel()
            prev_task = prev.task   # new run awaits this before it starts writing
        if prev.evict_task and not prev.evict_task.done():
            prev.evict_task.cancel()
        # The replay log is named after the SESSION, so the _RunLog built below
        # truncates the very file `prev` still has open. Retire the old one
        # first: from here on its writes (including the finish() its cancelled
        # _drain is about to emit) must not reach the new run's log.
        if prev.log is not None:
            try:
                prev.log.orphan()
            except Exception as e:      # pragma: no cover - best effort
                logger.debug("[agent-run] could not orphan the previous log: %s", e)
    run = _Run(lane=lane, label=label)
    run.model = str(model or "")
    run.endpoint_url = str(endpoint_url or "")
    _RUNS[session_id] = run
    if persistence_enabled():
        try:
            run.log = _RunLog(session_id, run)
        except Exception as e:
            logger.debug("[agent-run] log init failed: %s", e)
            run.log = None
    run.task = asyncio.create_task(_drain(session_id, run, agen, prev_task))
    return run


async def subscribe(
    session_id: str,
    expected_run: Optional[_Run] = None,
    from_sequence: Optional[int] = None,
) -> AsyncGenerator[str, None]:
    """Replay the run's buffer from the start, then stream live until it ends.
    Safe to call repeatedly (reconnect) and from multiple clients at once.

    ``expected_run`` binds a lazy StreamingResponse body to the same run whose
    identity was put in its response headers. Without that binding, a rapid
    replacement between response construction and body iteration could replay
    the replacement run under the prior run's identity.

    ``from_sequence`` (QA-09): resume from a cursor instead of replaying the
    whole buffer — the caller already has every event up to and including
    that sequence number (1-based; the `sequence` field `_publish` now stamps
    on every event). Every buffer slot's position already equals its own
    sequence minus one — compaction replaces a slot in place, it never
    shifts one — so the cursor maps straight onto a buffer index with no
    separate lookup. A cursor beyond what the buffer holds is CLAMPED rather
    than rejected: replay starts as far back as the buffer still goes (the
    "snapshot+cursor" case the lot description names, for a buffer that has
    moved on) and then continues live, which is a superset of what was asked
    for rather than a gap.
    """
    run = expected_run or _RUNS.get(session_id)
    if run is None:
        return
    q: asyncio.Queue = asyncio.Queue()
    run.subscribers.add(q)            # register BEFORE replaying so nothing is missed
    # A live subscriber is connected — don't let a pending grace timer evict
    # the run out from under it mid-replay.
    if run.evict_task and not run.evict_task.done():
        run.evict_task.cancel()
    try:
        next_seq = 0 if from_sequence is None else max(0, min(int(from_sequence), len(run.buffer)))
        while next_seq < len(run.buffer):
            yield run.buffer[next_seq]
            next_seq += 1
        if run.status != "running":
            return
        heartbeat_idx = 0
        while True:
            try:
                seq, ev, replaced = await asyncio.wait_for(q.get(), timeout=10.0)
            except asyncio.TimeoutError:
                # Keep slow local models/proxies alive while they prefill before
                # the first token. This is a visible sign of life as well as an
                # HTTP keepalive: the UI can now distinguish a 90-second model
                # prefill from a dead connection, and can retain the last known
                # phase while the user moves between conversations.
                if run.status == "running":
                    heartbeat_idx += 1
                    heartbeat = {
                        "type": "run_activity",
                        "data": await _heartbeat_snapshot(session_id, run),
                        "heartbeat": heartbeat_idx,
                        # OBS-01/ARCH-01: same additive fields _publish stamps
                        # on every buffered event; a heartbeat is synthesized
                        # here rather than drained from the buffer, so it
                        # needs its own copy rather than passing through
                        # _augment_sse_fields.
                        "trace_id": run.run_id,
                        "schema_version": api_version.API_VERSION,
                    }
                    yield "data: " + json.dumps(heartbeat, ensure_ascii=False) + "\n\n"
                    continue
                seq, ev, replaced = (None, None, False)
            if seq is None:            # end sentinel
                while next_seq < len(run.buffer):   # flush any tail the sentinel raced
                    yield run.buffer[next_seq]
                    next_seq += 1
                break
            # Skip events already replayed from the buffer — except a compacted
            # progress tick, which reuses the slot of the tick it replaced and
            # must still reach a live client.
            if seq >= next_seq or replaced:
                yield ev
                next_seq = seq + 1
    finally:
        run.subscribers.discard(q)
        # Last subscriber gone on a finished run — (re)arm eviction so the
        # buffer doesn't linger indefinitely.
        if not run.subscribers and run.status != "running":
            _schedule_evict(session_id, run)


def stop(session_id: str, expected_run_id: Optional[str] = None) -> bool:
    """Cancel the matching in-flight run (which saves its partial output).

    A stale browser may issue Stop after another tab has replaced the session's
    run. Once the caller knows its opaque run identity, fail closed rather than
    cancelling that newer run.
    """
    run = _RUNS.get(session_id)
    if not expected_run_id or run is None or run.run_id != expected_run_id:
        return False
    if run and run.task and not run.task.done():
        run.task.cancel()
        return True
    return False


# ---------------------------------------------------------------------------
# UX-04 -- pause, steer and "send after" for the MAIN session turn.
#
# Fail-closed the same way `stop()` does: `expected_run_id`, when given, must
# match the CURRENT run or nothing happens -- a stale tab must not pause/steer
# a run that already replaced its own. Unlike `stop()`, a bare call with no id
# is accepted here (the pause/steer/queue buttons only ever show while a
# specific run is on screen, so the caller has an id; callers that don't care
# which run -- tests, an internal trigger -- can omit it deliberately).
# ---------------------------------------------------------------------------

def request_pause(session_id: str, expected_run_id: Optional[str] = None) -> bool:
    """Ask a running turn to stop at its next safe point (see
    ``stream_agent_loop``'s ``pending_pause``) instead of finishing every
    round. Returns False when there is nothing running to pause."""
    run = _RUNS.get(session_id)
    if run is None or run.status != "running":
        return False
    if expected_run_id and run.run_id != expected_run_id:
        return False
    run.pause_requested = True
    return True


def take_pause_request(session_id: str) -> bool:
    """Consume (and clear) a pending pause. Called once per round by the
    loop's `pending_pause` callable; clearing it here means a turn resumed
    after a pause does not immediately repause itself."""
    run = _RUNS.get(session_id)
    if run is None or not run.pause_requested:
        return False
    run.pause_requested = False
    return True


def queue_steer(session_id: str, text: str, source: str = "user",
                 expected_run_id: Optional[str] = None) -> bool:
    """Queue an instruction that is injected as a user message at the turn's
    next safe point (mirrors `subagent_tools.steer_worker`, for the main
    session run instead of a delegated worker)."""
    text = " ".join(str(text or "").split()).strip()
    if not text:
        return False
    run = _RUNS.get(session_id)
    if run is None or run.status != "running":
        return False
    if expected_run_id and run.run_id != expected_run_id:
        return False
    run.steer_queue.append({
        "text": text[:4000],
        "source": "supervisor" if source == "supervisor" else "user",
    })
    return True


def take_steers(session_id: str) -> List[Dict[str, str]]:
    """Drain the steer queue -- what `pending_user_messages` hands the loop
    at its next safe point. Mirrors `subagent_tools.pending_steers`."""
    run = _RUNS.get(session_id)
    if run is None:
        return []
    out, run.steer_queue = list(run.steer_queue), []
    return out


def queue_send_after(session_id: str, text: str, source: str = "user",
                      expected_run_id: Optional[str] = None) -> bool:
    """Queue a message for delivery as a NEW turn once this one ends ("send
    after" / "Enviar despues") -- unlike `queue_steer`, this is never injected
    into the live turn, so it cannot alter work already in flight."""
    text = " ".join(str(text or "").split()).strip()
    if not text:
        return False
    run = _RUNS.get(session_id)
    if run is None or run.status != "running":
        return False
    if expected_run_id and run.run_id != expected_run_id:
        return False
    run.send_after_queue.append({
        "text": text[:4000],
        "source": "supervisor" if source == "supervisor" else "user",
    })
    return True


def take_send_after(session_id: str) -> List[Dict[str, str]]:
    """Drain the "send after" queue. Called by the route layer once a run has
    ended, to actually deliver the queued message as the next chat turn."""
    run = _RUNS.get(session_id)
    if run is None:
        return []
    out, run.send_after_queue = list(run.send_after_queue), []
    return out


def pending_send_after_count(session_id: str) -> int:
    run = _RUNS.get(session_id)
    return len(run.send_after_queue) if run is not None else 0


def tools_ran(session_id: str, run: Optional[_Run] = None) -> List[str]:
    """TASK-04: the tool names a run's buffer shows a `tool_start` for so
    far -- `what_ran_before` on a cancellation response. Reads the replay
    buffer already kept for reconnects; adds nothing new to persist."""
    r = run or _RUNS.get(session_id)
    if r is None:
        return []
    names: List[str] = []
    for ev in r.buffer:
        idx = ev.find('"type": "tool_start"')
        if idx == -1:
            continue
        try:
            payload = json.loads(ev.split("data: ", 1)[1])
        except Exception:
            continue
        tool = payload.get("tool")
        if tool:
            names.append(str(tool))
    return names


def _cancel_anywhere(task: "asyncio.Task") -> bool:
    """Cancel `task` from whatever thread we are on.

    FastAPI runs `def` routes in a threadpool, and asyncio tasks are not
    thread-safe: a bare `task.cancel()` from there can be lost. Hop through the
    task's own loop when we are not already on it.
    """
    try:
        loop = task.get_loop()
    except Exception:                                     # pragma: no cover
        loop = None
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if loop is not None and running is not loop:
        try:
            loop.call_soon_threadsafe(task.cancel)
            return True
        except RuntimeError:                              # loop already closed
            return False
    task.cancel()
    return True


def stop_for_session(session_id: str, reason: str = "session_deleted") -> bool:
    """Stop whatever run `session_id` currently has, without knowing its run_id.

    `stop()` stays fail-closed on purpose: a stale browser tab must never cancel
    the run that replaced its own. A server-side caller that is DESTROYING the
    session has no such ambiguity — there is no newer run left to protect — so
    it gets this explicit entry point instead of a relaxed `stop()`.

    Deleting a chat used to leave its run executing tools and writing files: it
    kept its queue-lane slot (with `agent_queue_local_concurrency=1` that blocks
    every other chat) and was unreachable from the UI, because /api/chat/activity
    and /api/chat/stop 404 once the session is gone.

    Returns True when there was something to stop. Safe to call off the event
    loop.
    """
    was_busy = session_id in _EXTERNAL_BUSY      # a sub-agent worker chat
    run = _RUNS.pop(session_id, None)
    clear_busy(session_id)
    _INTERRUPTED.pop(session_id, None)
    # A delegate_agents worker chat has no detached run of its own: its work
    # is a task inside the parent's tool call. Clearing the busy flag alone
    # left that task running (writing files) and unstoppable — the worker
    # endpoints 404 once the session is gone.
    worker_stopped = False
    try:
        from src.agent_tools.subagent_tools import stop_worker
        worker_stopped = stop_worker(session_id)
    except Exception as exc:                          # pragma: no cover - best effort
        logger.debug("[agent-run] could not stop worker %s: %s", session_id, exc)
    if worker_stopped:
        logger.info("[agent-run] sub-agent worker of session %s stopped (%s)", session_id, reason)
    if run is None:
        return was_busy or worker_stopped
    was_running = run.status == "running"
    if was_running:
        run.status = "stopped"
    # Close the replay log with a terminal status: the session is gone, so a
    # restart must not "recover" it into a chat that no longer exists.
    if run.log is not None:
        try:
            run.log.finish("stopped")
        except Exception:                                 # pragma: no cover
            pass
    if run.task is not None and not run.task.done():
        _cancel_anywhere(run.task)
    if run.evict_task is not None and not run.evict_task.done():
        _cancel_anywhere(run.evict_task)
    logger.info("[agent-run] run of session %s stopped (%s)", session_id, reason)
    return True


# ---------------------------------------------------------------------------
# Recovery after a restart
# ---------------------------------------------------------------------------

_INTERRUPTED: Dict[str, Dict[str, Any]] = {}
INTERRUPTED_NOTE = "[Interrupted: Faustus was restarted while this task was running. What it had produced is kept above; send \"continue\" to pick it up.]"


def _read_log(path: str) -> Dict[str, Any]:
    """Parse a run log: {"status", "run_id", "events": [ev...], "ts", "label"}."""
    events: Dict[int, str] = {}
    status = None
    meta: Dict[str, Any] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if "status" in obj:
                    status = obj["status"]
                    if obj["status"] == "running":
                        meta = obj
                    continue
                seq = obj.get("seq")
                if isinstance(seq, int) and isinstance(obj.get("ev"), str):
                    events[seq] = obj["ev"]
    except OSError:
        return {"status": "unreadable", "events": []}
    ordered = [events[k] for k in sorted(events)]
    return {"status": status, "events": ordered, **{k: meta.get(k) for k in ("run_id", "ts", "lane", "label", "session_id")}}


def _partial_from_events(events: List[str]) -> Dict[str, Any]:
    text_parts: List[str] = []
    tool_events: List[Dict[str, Any]] = []
    metrics = None
    saved = False
    for ev in events:
        if not ev.startswith("data: ") or ev.startswith("data: [DONE]"):
            continue
        try:
            d = json.loads(ev[6:])
        except ValueError:
            continue
        if "delta" in d and not d.get("type"):
            if not d.get("thinking"):
                text_parts.append(str(d["delta"]))
        elif d.get("type") == "tool_output":
            # CALL-05: the call transported cleanly (its tool_output event
            # made it into the run's own replay log) but that says nothing
            # about whether the ACTION succeeded — a non-zero exit_code or a
            # `blocked` refusal is exactly the "a successful transport can
            # still carry a functional error" case the typed contract exists
            # for (src/tool_result.py, lote 62). Reading `exit_code` here ad
            # hoc (as this line did before) and calling it "done" the moment
            # the event merely existed is the bug CALL-05 names: a step's
            # recovered/traced status now comes from the SAME classifier
            # `execute_tool_block` (src/tool_execution.py) already runs on
            # every tool result, not a second, independent reading of the
            # same two fields.
            from src.tool_result import normalize_tool_result
            _typed = normalize_tool_result(d)
            tool_events.append({"tool": d.get("tool"), "command": str(d.get("command") or "")[:400],
                                "output": str(d.get("output") or "")[:1500], "exit_code": d.get("exit_code"),
                                "status": _typed.status})
        elif d.get("type") == "metrics":
            metrics = d.get("data")
        elif d.get("type") == "message_saved":
            saved = True
    return {"text": "".join(text_parts), "tool_events": tool_events[:60], "metrics": metrics, "saved": saved}


# ---------------------------------------------------------------------------
# OBS-01: reconstruct everything one tool call produced, from the one id all
# three stores already carry.
# ---------------------------------------------------------------------------
#
# `_observability_fields` above already stamps every SSE event of a run with
# `trace_id`/`step_id`; `src/agent_loop.py` already puts the call's own
# `call_id` (the provider's native id, or the `call_{round}_{index}`
# fallback — see tests/test_obs_call_id.py) on its tool_start/tool_progress/
# tool_output events; `src/artifact_store.py::persist` and
# `src/command_guard.py::append_receipt` now take the SAME `call_id` and
# fold it into an artifact's manifest row and a command_guard receipt
# respectively (both additive, optional — neither call site is in this
# lot's file list). What was still missing is the one function that walks
# from a `call_id` back out to all three: today that meant grepping the
# event stream, the artifact manifest table, and the receipts log by hand,
# in three different formats, in three different files. `trace_for_call`
# is that function; wiring it behind an HTTP route (if the caller wants one)
# is integration's job, not this module's — see the lot report.

#: Bound on how many persisted run logs a session-less lookup scans, so one
#: call to `trace_for_call` can never become unbounded disk work on an
#: instance with years of `data/runs/*.jsonl` history. Ordered by mtime
#: (newest first) before the cap is applied, so a recent call_id is found
#: long before an ancient one would be missed.
_TRACE_SCAN_MAX_FILES = 200


def _trace_log_paths(session_id: Optional[str]) -> List[str]:
    if session_id:
        path = _log_path(session_id)
        return [path] if os.path.isfile(path) else []
    d = _runs_dir()
    try:
        names = [n for n in os.listdir(d) if n.endswith(".jsonl")]
    except OSError:
        return []

    def _mtime(name: str) -> float:
        try:
            return os.path.getmtime(os.path.join(d, name))
        except OSError:
            return 0.0

    names.sort(key=_mtime, reverse=True)
    return [os.path.join(d, n) for n in names[:_TRACE_SCAN_MAX_FILES]]


def _receipt_for_call_id(call_id: str) -> Optional[Dict[str, Any]]:
    """The command_guard decision receipt for this call, or None — never a
    fabricated "allowed"/"blocked" guess when the log does not have it.
    Mirrors `command_guard.tail_receipts()`'s own read-only file scan (only
    the live log, not a rotated `.1`; a call_id old enough to have rotated
    out is a real "not found", not a bug in this function)."""
    try:
        from src import command_guard
        match: Optional[Dict[str, Any]] = None
        with open(command_guard._log_path(), "r", encoding="utf-8") as fh:
            for line in fh:
                if call_id not in line:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(record, dict) and str(record.get("call_id") or "") == call_id:
                    match = record  # keep scanning; the last (most recent) wins
        return match
    except OSError:
        return None
    except Exception:  # noqa: BLE001 - a trace lookup must never raise
        logger.debug("[trace_for_call] receipt lookup failed for %s", call_id, exc_info=True)
        return None


def _artifacts_for_call_id(call_id: str) -> List[Dict[str, Any]]:
    """Every artifact manifest row this call produced (ART-01's
    `artifact_manifests` table, `src/artifact_identity.py`), enriched with
    the occurrence's own label/kind/owner when that occurrence can still be
    resolved. An empty list is a real, expected answer — most calls never
    produce an artifact at all — not a failure."""
    try:
        from sqlalchemy import text as sql_text
        from core.database import engine
        with engine.connect() as conn:
            rows = conn.execute(sql_text(
                "SELECT id, occurrence_id, version, state, format, byte_size, "
                "sha256, generator, created_at_iso FROM artifact_manifests "
                "WHERE call_id=:cid ORDER BY created_at_iso"
            ), {"cid": call_id}).mappings().all()
    except Exception:
        # Covers both "the table does not exist yet" (no artifact has ever
        # been manifested on this instance) and a transient DB error -- OBS-01
        # must never break a trace lookup for a call that simply made none.
        return []
    out: List[Dict[str, Any]] = []
    for row in rows:
        entry: Dict[str, Any] = {
            "occurrence_id": row["occurrence_id"], "manifest_id": row["id"],
            "version": row["version"], "state": row["state"],
            "format": row["format"] or "", "byte_size": row["byte_size"],
            "sha256": row["sha256"] or "", "generator": row["generator"] or "",
            "created_at": row["created_at_iso"],
        }
        try:
            from src import artifact_identity
            occ = artifact_identity.occurrence(row["occurrence_id"])
            if occ is not None:
                entry["label"] = occ.label
                entry["kind"] = occ.kind
                entry["owner"] = occ.owner
        except Exception:  # noqa: BLE001 - the manifest row alone is still useful
            logger.debug("[trace_for_call] occurrence lookup failed for %s",
                        row["occurrence_id"], exc_info=True)
        out.append(entry)
    return out


def trace_for_call(call_id: str, *, session_id: Optional[str] = None) -> Dict[str, Any]:
    """OBS-01: reconstruct everything ONE tool call produced, keyed by the
    `call_id` its event, its artifact (if any) and its command_guard receipt
    (if any) all already carry.

    `session_id` narrows the event search to one session's persisted run log
    (`data/runs/<session_id>.jsonl`, plus that session's live in-memory
    buffer if a run is still going); omitted, every currently-live run and
    every persisted run log under `_runs_dir()` is scanned, newest first,
    bounded by `_TRACE_SCAN_MAX_FILES`. A `call_id` this function cannot
    find anywhere in that window is reported as not found — never guessed
    at from a partial match, never a fabricated result.

    Returns a plain dict, never raises:
    - ``call_id``: the id looked up.
    - ``events``: the raw event payloads (dicts, already carrying
      ``trace_id``/``step_id`` per `_observability_fields`) that named this
      call_id, in the order they were produced — typically
      ``tool_start`` -> zero or more ``tool_progress`` -> ``tool_output``.
    - ``artifacts``: see `_artifacts_for_call_id`.
    - ``receipt``: see `_receipt_for_call_id`, or ``None``.
    - ``found``: True iff at least ONE of the three turned up something —
      so a caller can tell "nothing ever happened under this id" apart from
      "the event is here but the artifact/receipt legitimately isn't".
    """
    call_id = str(call_id or "").strip()
    if not call_id:
        return {"call_id": "", "events": [], "artifacts": [], "receipt": None, "found": False}

    events: List[Dict[str, Any]] = []
    seen_raw: set = set()

    def _collect(raw_events) -> None:
        for ev in raw_events or []:
            if not isinstance(ev, str) or not ev.startswith("data: ") or call_id not in ev:
                continue
            if ev in seen_raw:
                continue
            try:
                payload = json.loads(ev[6:])
            except (ValueError, TypeError):
                continue
            if not isinstance(payload, dict) or str(payload.get("call_id") or "") != call_id:
                continue
            seen_raw.add(ev)
            events.append(payload)

    # Live, still-buffered runs first -- the freshest state for a call that
    # just happened and has not been persisted (or persistence is off).
    if session_id:
        _run = _RUNS.get(session_id)
        if _run is not None:
            _collect(_run.buffer)
    else:
        for _run in list(_RUNS.values()):
            _collect(_run.buffer)

    # Persisted logs -- what survives after a run ends or the process
    # restarts; also covers a session whose run already finished.
    for path in _trace_log_paths(session_id):
        _collect(_read_log(path).get("events"))

    receipt = _receipt_for_call_id(call_id)
    artifacts = _artifacts_for_call_id(call_id)

    return {
        "call_id": call_id,
        "events": events,
        "artifacts": artifacts,
        "receipt": receipt,
        "found": bool(events or artifacts or receipt),
    }


def recover_interrupted_runs(session_manager=None) -> List[Dict[str, Any]]:
    """Scan DATA_DIR/runs for logs left in 'running' state by a previous
    process. For each: save what the run had produced as a partial assistant
    message (unless the run had already saved one), mark the log
    'interrupted', and remember the session for the sidebar/toast. Also prunes
    finished logs older than `agent_runs_keep_hours` (default 48)."""
    d = _runs_dir()
    if not os.path.isdir(d):
        return []
    try:
        keep_h = float(_setting("agent_runs_keep_hours", 48) or 48)
    except (TypeError, ValueError):
        keep_h = 48.0
    now = time.time()
    recovered: List[Dict[str, Any]] = []
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return []
    for name in names:
        if not name.endswith(".jsonl"):
            continue
        path = os.path.join(d, name)
        info = _read_log(path)
        status = info.get("status")
        if status in ("done", "stopped", "error", "interrupted", "unreadable",
                      "waiting_user", None) and status != "running":
            try:
                if now - os.path.getmtime(path) > keep_h * 3600:
                    os.remove(path)
            except OSError:
                pass
            continue
        sid = str(info.get("session_id") or name[:-6])
        partial = _partial_from_events(info.get("events") or [])
        entry = {"session_id": sid, "run_id": info.get("run_id"), "ts": info.get("ts"),
                 "label": info.get("label") or "", "chars": len(partial["text"]),
                 "tool_calls": len(partial["tool_events"]), "saved_message": False}
        # A restart is not the model failing: the run was cut short, like a
        # Stop — `cancelled`, never an error (src/tool_outcome.py).
        interrupted_outcome = _outcome_of("interrupted")
        if interrupted_outcome:
            entry["outcome"] = interrupted_outcome
        if session_manager is not None and not partial["saved"]:
            try:
                sess = session_manager.get_session(sid)
            except Exception:
                sess = None
            if sess is not None:
                try:
                    from core.models import ChatMessage
                    body = partial["text"].strip()
                    content = (body + "\n\n" if body else "") + INTERRUPTED_NOTE
                    meta: Dict[str, Any] = {"stopped": True, "interrupted": True, "run_id": info.get("run_id")}
                    if partial["tool_events"]:
                        meta["tool_events"] = partial["tool_events"]
                    if isinstance(partial.get("metrics"), dict):
                        meta.update({k: v for k, v in partial["metrics"].items() if k in ("model", "harness")})
                    sess.add_message(ChatMessage("assistant", content, metadata=meta))
                    session_manager.save_sessions()
                    entry["saved_message"] = True
                except Exception as e:
                    logger.warning("[agent-run] could not save interrupted run for %s: %s", sid, e)
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"status": "interrupted", "ts": now}) + "\n")
        except OSError:
            pass
        _INTERRUPTED[sid] = entry
        recovered.append(entry)
    if recovered:
        logger.warning("[agent-run] %d run(s) were interrupted by the previous restart: %s",
                       len(recovered), ", ".join(r["session_id"] for r in recovered))
    return recovered


def interrupted_runs() -> List[Dict[str, Any]]:
    return list(_INTERRUPTED.values())


def acknowledge_interrupted(session_id: Optional[str] = None) -> int:
    if session_id is None:
        n = len(_INTERRUPTED)
        _INTERRUPTED.clear()
        return n
    return 1 if _INTERRUPTED.pop(session_id, None) is not None else 0
