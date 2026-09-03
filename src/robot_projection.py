"""Robot-mode projections — the LEAN, scalar-only view of each payload.

Robot mode (``src/robot_envelope.py``) exists so a COORDINATING MODEL can read
this machine cheaply. Wrapping the browser's payload in an envelope and
re-encoding it as TOON did not do that: measured against a running instance,
``?format=toon`` came back *bigger* than the plain JSON body on three of four
endpoints (memory items 1.15x, objectives 1.24x, usage 1.23x, guard log 0.93x).

The reason is in ``src/toon.py``: TOON only pays where an array of objects is
TABULAR — two or more rows sharing one key set whose values are all scalars.
Then the keys are named once in a header instead of once per row. Every real
payload here breaks that rule with per-row containers: a memory item carries
``evidence`` / ``helpful`` / ``harmful`` event arrays, an objective carries a
``deps`` list (and its score lives in a separate per-id object), a GPU carries
its own ``models`` list, a receipt carries an optional ``note`` key that half
the rows do not have. Nothing tabularises, and TOON's two-space indent per
level costs more than JSON's braces.

So the fix is not in the encoder. Robot mode means a compact MACHINE view of
the endpoint — not the whole UI payload re-encoded. This module is that view:
one small pure function per payload kind, each turning the full answer into
flat rows a coordinator can act on.

The rules every projection follows:

* **Rows are uniform and all-scalar.** Every row of an array is built from the
  same fixed column tuple, in the same order, with a scalar in every cell —
  that is exactly TOON's tabular condition, so the table always fires.
* **A list inside a row becomes one cell.** ``deps`` becomes ``blocked_by``,
  a comma-joined string; a worker's file list becomes a count next to the
  job-level union of the paths.
* **Text cells are one line.** Whitespace is squashed, so a row is a line.
* **Identifiers are shortened, not dropped**: ``id8`` for a memory item, the
  first 8 characters of a receipt hash — enough to name the thing in a
  follow-up call, an eighth of the characters.
* **What the coordinator already knows is dropped**: the task instructions it
  sent, the enum tables the UI paints dropdowns from, the per-record chain
  hashes only the server verifies, ``status: "success"`` (the envelope's
  ``ok`` says that).
* **Order is fixed**, so the same payload always projects to the same bytes.

Nothing here may raise: every public function is wrapped so a payload it did
not expect comes back UNCHANGED rather than turning a working read into a 500.
Projections are lossy on purpose — the plain (no query parameter) response is
untouched and still carries everything.

Pure stdlib.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence

__all__ = [
    "memory_items", "objectives", "guard_log", "system_usage",
    "dispatch_status", "dispatch_events",
]

_TEXT_CELL = 400          # a squashed text cell; the long ones are already bounded
_NOTE_CELL = 200


# ── cell helpers ────────────────────────────────────────────────────────────

def _never_raises(fn: Callable[[Any], Any]) -> Callable[[Any], Any]:
    """A projection that meets a payload it did not expect answers with that
    payload untouched. Robot mode may lose the compaction, never the read."""
    def guarded(payload: Any) -> Any:
        if not isinstance(payload, dict):
            return payload
        try:
            return fn(payload)
        except Exception:  # noqa: BLE001 - a view may never break a response
            return payload
    guarded.__name__ = fn.__name__
    guarded.__doc__ = fn.__doc__
    guarded.__wrapped__ = fn
    return guarded


def _seq(value: Any) -> List[Any]:
    """The list under a key, whatever the payload put there."""
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any, limit: int = _TEXT_CELL) -> str:
    """One line, bounded — a table row is a line, so a cell cannot hold one."""
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        raw = value
    else:
        try:                 # an object whose __str__ (or __eq__) explodes is
            raw = str(value)  # an empty cell, never an exception
        except Exception:  # noqa: BLE001
            return ""
    out = " ".join(raw.split())
    return out if len(out) <= limit else out[: limit - 1].rstrip() + "…"


def _num(value: Any, default: Optional[float] = None) -> Any:
    """The number as it stands (a bool is not one), else `default`."""
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, (int, float)):
        return value
    try:
        text = str(value).strip()
    except Exception:  # noqa: BLE001
        return default
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return default


def _int(value: Any, default: int = 0) -> int:
    got = _num(value, None)
    if got is None:
        return default
    try:
        return int(got)
    except (TypeError, ValueError, OverflowError):
        return default


def _flag(value: Any) -> Optional[bool]:
    return bool(value) if isinstance(value, bool) else None


def _join(values: Any, limit: int = _NOTE_CELL) -> str:
    """A list cell as one string — the whole point of the projection: a row
    holding a list is not tabular, a row holding ``a,b,c`` is."""
    return _text(",".join(_text(v, 80) for v in _seq(values) if _text(v, 80)), limit)


def _pairs(value: Any, limit: int = _NOTE_CELL) -> str:
    """A small object cell as ``k=v;k=v`` (an audit record's `fields`)."""
    items = _dict(value)
    return _text(";".join(f"{_text(k, 40)}={_text(v, 80)}" for k, v in items.items()), limit)


def _scalars(value: Any, drop: Sequence[str] = ()) -> Dict[str, Any]:
    """The scalar keys of an object, in order — the containers are what made
    the payload un-tabular, and every one of them is somewhere else already."""
    out: Dict[str, Any] = {}
    for key, item in _dict(value).items():
        if key in drop or not isinstance(key, str):
            continue
        if item is None or isinstance(item, (str, int, float, bool)):
            out[key] = _text(item) if isinstance(item, str) else item
    return out


def _carry(out: Dict[str, Any], key: str, value: Any) -> None:
    """Keep an optional scalar exactly as it stands (a string gets squashed);
    skip it when the payload did not have one, so the shape stays predictable."""
    if value is None:
        return
    if isinstance(value, str):
        out[key] = _text(value, _NOTE_CELL)
    elif isinstance(value, (int, float, bool)):
        out[key] = value


def _strings(value: Any, limit: int = 60) -> List[str]:
    """A list of scalars stays a list — TOON writes it as ``- `` items and
    JSON as an array; neither repeats a key, so neither is the problem."""
    return [_text(v, 200) for v in _seq(value)[:limit]]


# ── /api/memory-engine/items ────────────────────────────────────────────────

_ITEM_COLUMNS = ("id8", "level", "status", "maturity", "trust_class",
                 "effective_score", "harmful_ratio", "helpful_count",
                 "harmful_count", "updated_at", "text")


def _memory_row(item: Any) -> Dict[str, Any]:
    row = _dict(item)
    ident = _text(row.get("id8")) or _text(row.get("id"))[:8]
    return {
        "id8": ident,
        "level": _text(row.get("level"), 40),
        "status": _text(row.get("status"), 40),
        "maturity": _text(row.get("maturity"), 40),
        "trust_class": _text(row.get("trust_class"), 40),
        "effective_score": _num(row.get("effective_score")),
        "harmful_ratio": _num(row.get("harmful_ratio")),
        "helpful_count": _int(row.get("helpful_count"), len(_seq(row.get("helpful")))),
        "harmful_count": _int(row.get("harmful_count"), len(_seq(row.get("harmful")))),
        "updated_at": _text(row.get("updated_at"), 40),
        "text": _text(row.get("text"), 2000),
    }


@_never_raises
def memory_items(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The learned rules as rows.

    Dropped: the ``evidence`` / ``helpful`` / ``harmful`` event arrays (their
    counts and the derived ``harmful_ratio`` are the decision), the 32-char
    ids (``id8`` resolves in every write endpoint), ``created_at`` and
    ``last_accessed``, and the ``levels`` / ``trust_classes`` enum tables the
    Brain page paints its dropdowns from.
    """
    out: Dict[str, Any] = {"items": [_memory_row(i) for i in _seq(payload.get("items"))]}
    stats = _scalars(payload.get("stats"))
    if stats:
        out["stats"] = stats
    return out


# ── /api/projects/{id}/objectives ───────────────────────────────────────────

_OBJECTIVE_COLUMNS = ("id", "status", "priority", "title", "owner",
                      "updated_at", "score", "hint", "blocked_by")
_LOG_COLUMNS = ("ts", "kind", "actor", "op", "id", "note")


def _objective_row(obj: Any, scores: Dict[str, Any]) -> Dict[str, Any]:
    row = _dict(obj)
    oid = _text(row.get("id"), 40)
    score = _dict(scores.get(oid))
    return {
        "id": oid,
        "status": _text(row.get("status"), 40),
        "priority": _num(row.get("priority")),
        "title": _text(row.get("title"), 200),
        "owner": _text(row.get("owner"), 60),
        "updated_at": _text(row.get("updated_at"), 40),
        "score": _num(score.get("score")),
        "hint": _text(score.get("hint"), 120),
        "blocked_by": _join(row.get("deps")),
    }


def _log_row(record: Any) -> Dict[str, Any]:
    rec = _dict(record)
    note = (_text(rec.get("rationale"), _NOTE_CELL) or _text(rec.get("note"), _NOTE_CELL)
            or _pairs(rec.get("fields")))
    return {
        "ts": _text(rec.get("ts"), 40),
        "kind": _text(rec.get("kind"), 40),
        "actor": _text(rec.get("actor") or rec.get("source"), 60),
        "op": _text(rec.get("op"), 20),
        "id": _text(rec.get("id"), 40),
        "note": note,
    }


@_never_raises
def objectives(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The objectives dashboard as rows.

    Each objective's structural score is folded INTO its row, so the per-id
    ``scores`` object — the single biggest reason this payload never
    tabularised — is gone, along with the five score components behind it.
    ``deps`` becomes ``blocked_by``, one comma-joined cell; the edge list is
    already tabular and is kept as its own array; the audit tail keeps one
    row per record with its ``fields`` object rendered as ``k=v;k=v``.
    """
    scores = _dict(payload.get("scores"))
    out: Dict[str, Any] = {
        "objectives": [_objective_row(o, scores) for o in _seq(payload.get("objectives"))],
        "edges": [{"from": _text(_dict(e).get("from"), 40), "to": _text(_dict(e).get("to"), 40)}
                  for e in _seq(payload.get("edges"))],
    }
    log = [_log_row(r) for r in _seq(payload.get("log"))]
    if log:
        out["log"] = log
    return out


# ── /api/command-guard/log ──────────────────────────────────────────────────

_RECEIPT_COLUMNS = ("ts", "tool", "tier", "rule", "action", "command_head",
                    "note", "hash8")


def _receipt_row(record: Any) -> Dict[str, Any]:
    rec = _dict(record)
    note = _text(rec.get("note"), _NOTE_CELL)
    corrupt = _text(rec.get("corrupt_line"), _NOTE_CELL)
    if corrupt and not note:
        note = "corrupt line: " + corrupt
    return {
        "ts": _text(rec.get("ts"), 40),
        "tool": _text(rec.get("tool"), 40),
        "tier": _text(rec.get("tier"), 20),
        "rule": _text(rec.get("rule") or rec.get("rule_id"), 60),
        "action": _text(rec.get("action") or rec.get("decision"), 20),
        "command_head": _text(rec.get("command_head") or rec.get("command"), 300),
        "note": note,
        "hash8": _text(rec.get("hash"), 64)[:8],
    }


@_never_raises
def guard_log(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The decision receipts as rows.

    Dropped: ``command_sha256``, ``prev_hash`` and the full ``hash`` — 192
    characters of chain plumbing per receipt that only the server's own
    ``verify_chain`` walks, whose verdict is right there in ``chain``. Eight
    characters of the hash stay so a coordinator can name one receipt. The
    optional ``note`` / ``rotated_from`` keys, which alone stopped these rows
    from sharing a key set, become one always-present ``note`` column.
    """
    return {
        "receipts": [_receipt_row(r) for r in _seq(payload.get("receipts"))],
        "chain": _scalars(payload.get("chain")),
    }


# ── /api/system/usage ───────────────────────────────────────────────────────

_GPU_COLUMNS = ("index", "name", "util", "temp", "power", "power_limit",
                "mem_used", "mem_free", "mem_total")
_MODEL_COLUMNS = ("name", "size", "size_vram", "gpu_pct", "cpu_pct", "placement",
                  "gpus", "context_length", "parameter_size", "quantization",
                  "expires_at")
_ORPHAN_COLUMNS = ("pid", "name", "gpus", "bytes", "started")


def _gpu_row(card: Any) -> Dict[str, Any]:
    row = _dict(card)
    return {
        "index": _num(row.get("index")),
        "name": _text(row.get("name"), 80),
        "util": _num(row.get("util")),
        "temp": _num(row.get("temp")),
        "power": _num(row.get("power")),
        "power_limit": _num(row.get("power_limit")),
        "mem_used": _num(row.get("mem_used")),
        "mem_free": _num(row.get("mem_free")),
        "mem_total": _num(row.get("mem_total")),
    }


def _model_row(model: Any) -> Dict[str, Any]:
    row = _dict(model)
    return {
        "name": _text(row.get("name"), 120),
        "size": _num(row.get("size")),
        "size_vram": _num(row.get("size_vram")),
        "gpu_pct": _num(row.get("gpu_pct")),
        "cpu_pct": _num(row.get("cpu_pct")),
        "placement": _text(row.get("placement"), 20),
        "gpus": _join(row.get("gpus"), 40),
        "context_length": _num(row.get("context_length")),
        "parameter_size": _text(row.get("parameter_size"), 20),
        "quantization": _text(row.get("quantization"), 20),
        "expires_at": _text(row.get("expires_at"), 40),
    }


def _orphan_row(runner: Any) -> Dict[str, Any]:
    row = _dict(runner)
    return {
        "pid": _num(row.get("pid")),
        "name": _text(row.get("name"), 120),
        "gpus": _join(row.get("gpus"), 40),
        "bytes": _num(row.get("bytes")),
        "started": _text(row.get("started"), 40),
    }


@_never_raises
def system_usage(payload: Dict[str, Any]) -> Dict[str, Any]:
    """What this machine has room for, as three tables and some folded keys.

    One row per card, per loaded model and per orphaned runner; the pool sums
    the fit arithmetic works against stay as scalar keys. Dropped: each card's
    ``uuid`` / ``bus_id`` / ``runner_pids`` and its repeat of the model list
    (the models table names its cards in ``gpus``), each model's ``per_gpu``
    breakdown and ``family``, the orphans' ``blob`` digest, the Ollama base
    URL the caller dialled, and ``sysmem_fallback.steps`` — remediation prose
    for a human at a driver control panel. ``gpu_mem.spilling`` stays: it is
    the one gauge that reads green while the box runs 20x slow.
    """
    ollama = _dict(payload.get("ollama"))
    models = _seq(ollama.get("models")) or _seq(payload.get("models"))
    gpu_mem = _dict(payload.get("gpu_mem"))
    fallback = _dict(payload.get("sysmem_fallback"))
    out: Dict[str, Any] = {
        "ts": _num(payload.get("ts")),
        "gpus": [_gpu_row(g) for g in (_seq(payload.get("gpu")) or _seq(payload.get("gpus")))],
        "models": [_model_row(m) for m in models],
        "orphans": [_orphan_row(o) for o in _seq(payload.get("orphans"))],
    }
    for key, section in (("pool", _scalars(payload.get("gpu_pool"), drop=("names",))),
                         ("cpu", _scalars(payload.get("cpu"))),
                         ("ram", _scalars(payload.get("ram")))):
        if section:                      # `pool: {}` on a box with no card is noise
            out[key] = section
    out["ollama"] = {"reachable": bool(ollama.get("reachable")), "loaded": len(models)}
    spill = _scalars(gpu_mem.get("ollama"))
    if spill or gpu_mem:
        spill["supported"] = _flag(gpu_mem.get("supported"))
        out["gpu_mem"] = spill
    exposed = _flag(fallback.get("exposed"))
    if exposed is not None:
        out["sysmem_fallback_exposed"] = exposed
    errors = _strings(payload.get("errors"), 20)
    if errors:
        out["errors"] = errors
    return out


# ── /api/dispatch/{id} and /{id}/events ─────────────────────────────────────

_WORKER_COLUMNS = ("name", "role", "status", "outcome", "rounds", "tool_calls",
                   "failed_calls", "input_tokens", "output_tokens", "duration_s",
                   "files", "checks_failed", "error", "summary")
_PROGRESS_COLUMNS = ("name", "last_event", "status", "round", "elapsed_s",
                     "idle_s", "last_tool", "stalled", "stall_reason")
_EVENT_COLUMNS = ("ts", "name", "event", "status", "round", "tool", "elapsed_s",
                  "message")


def _worker_row(worker: Any) -> Dict[str, Any]:
    row = _dict(worker)
    checks = _dict(row.get("static_checks"))
    return {
        "name": _text(row.get("name"), 60),
        "role": _text(row.get("role"), 40),
        "status": _text(row.get("status"), 40),
        "outcome": _text(row.get("outcome"), 40),
        "rounds": _int(row.get("rounds")),
        "tool_calls": _int(row.get("tool_calls")),
        "failed_calls": _int(row.get("failed_calls")),
        "input_tokens": _int(row.get("input_tokens")),
        "output_tokens": _int(row.get("output_tokens")),
        "duration_s": _num(row.get("duration_s")),
        "files": len(_seq(row.get("files_changed"))),
        "checks_failed": len(_seq(checks.get("failed"))),
        "error": _text(row.get("error"), 300),
        "summary": _text(row.get("summary"), 1200),
    }


def _progress_row(name: Any, tick: Any) -> Dict[str, Any]:
    row = _dict(tick)
    return {
        "name": _text(name, 60),
        "last_event": _text(row.get("last_event"), 40),
        "status": _text(row.get("status"), 40),
        "round": _num(row.get("round")),
        "elapsed_s": _num(row.get("elapsed_s")),
        "idle_s": _num(row.get("idle_s")),
        "last_tool": _text(row.get("last_tool") or row.get("tool"), 60),
        "stalled": bool(row.get("stalled")),
        "stall_reason": _text(row.get("stall_reason"), 120),
    }


def _event_row(event: Any) -> Dict[str, Any]:
    row = _dict(event)
    return {
        "ts": _num(row.get("ts")),
        "name": _text(row.get("name") or row.get("id"), 60),
        "event": _text(row.get("event"), 40),
        "status": _text(row.get("status"), 40),
        "round": _num(row.get("round")),
        "tool": _text(row.get("tool") or row.get("last_tool"), 60),
        "elapsed_s": _num(row.get("elapsed_s")),
        "message": _text(row.get("message"), 300),
    }


def _verification(value: Any) -> Dict[str, Any]:
    """The verdict as scalars plus the failure lines — never ``output_tail``,
    1500 raw characters whose signal is already in ``summary``/``failures``."""
    verdict = _scalars(value, drop=("output_tail",))
    failures = _strings(_dict(value).get("failures"), 10)
    if failures:
        verdict["failures"] = failures
    for key in ("new_failures", "pre_existing"):
        rows = _strings(_dict(value).get(key), 10)
        if rows:
            verdict[key] = rows
    return verdict


@_never_raises
def dispatch_status(payload: Dict[str, Any]) -> Dict[str, Any]:
    """A job as one table of workers plus the verdict scalars.

    ``result`` is unwrapped to the top level (a level of nesting is two spaces
    on every line under it), the per-worker file list becomes a count beside
    the job-level union of the observed paths, and the running job's
    ``progress`` map — one object per worker name, which is not an array at
    all — becomes a table keyed by ``name``. Dropped: the ``tasks`` the
    coordinator itself sent, the knobs it set (``parallel``, ``max_rounds``,
    ``timeout_s``, ``verify_scope``, ``fix_rounds``), the chat plumbing
    (``owner``, ``session_id``, ``chat_url``, ``created``/``started``/
    ``finished`` — ``duration_s`` is the number), and the ``changes`` split by
    kind, whose paths ``files_changed`` already lists.
    """
    result = _dict(payload.get("result"))
    changes = _dict(result.get("changes"))
    out: Dict[str, Any] = {
        "id": _text(payload.get("id"), 60),
        "status": _text(payload.get("status"), 40),
        "title": _text(payload.get("title"), 200),
        "verdict": _text(payload.get("verdict"), 400),
        "error": _text(payload.get("error"), 400),
        "workspace": _text(payload.get("workspace"), 300),
        "model": _text(payload.get("model"), 80),
        "duration_s": _num(payload.get("duration_s")),
        "workers": [_worker_row(w) for w in _seq(result.get("workers"))],
        "totals": _scalars(result.get("totals")),
        "files_changed": _strings(result.get("files_changed"), 180),
    }
    for key in ("claimed_only", "lock_conflicts"):
        rows = _strings(result.get(key), 20)
        if rows:
            out[key] = rows
    if changes:
        out["changes"] = _scalars(changes, drop=("added", "modified", "deleted", "git"))
    if result.get("verification") is not None:
        out["verification"] = _verification(result.get("verification"))
    if result.get("convergence") is not None:
        out["convergence"] = _scalars(result.get("convergence"), drop=("components",))
    for key in ("stopped_by", "exit_code", "dropped_tasks"):
        _carry(out, key, result.get(key))
    progress = _dict(payload.get("progress"))
    if progress:
        out["progress"] = [_progress_row(name, tick) for name, tick in progress.items()]
    for key in ("phase", "wait_again", "ceiling_s"):
        _carry(out, key, payload.get(key))
    return out


@_never_raises
def dispatch_events(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The board's events as rows: the union of the keys the job and the
    harness emit, so every event is the same eight columns and the array is
    one header instead of a key set per line."""
    return {
        "id": _text(payload.get("id"), 60),
        "status": _text(payload.get("status"), 40),
        "events": [_event_row(e) for e in _seq(payload.get("events"))],
    }
