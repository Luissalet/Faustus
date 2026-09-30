"""outcome_verification.py -- ONE step that says whether a piece of work is
verified, unverified or uncertain.

Faustus used to answer "did that work?" in five places with five vocabularies:
the harness ledger (`TurnLedger.check_completion`: did the words match the tool
log), `completion_gate` (tests and smoke), the change-set proof (`prove`:
proved/partial/unproved/contradicted), the worker result (`claimed_only`,
`unguarded`, exit code) and Code Mode's per-call receipts. Each was right about
its own evidence and none of them could see the others, so a turn could be
"verified" by one and quietly unsupported by another.

`verify_outcome(evidence)` is the single function they all feed. It takes the
evidence that already exists, recomputes nothing, and answers with exactly one of

    verified     something positive was observed (an exit code 0, a passing test
                 run, a change the checkpoint saw, a process that exited 0) and
                 nothing contradicts it or leaves its effects open;
    unverified   evidence says the work did NOT happen or does not match what
                 was claimed (a failing exit code, a failing test run, a claim
                 the checkpoint contradicts, a refusal), or there is no evidence
                 at all;
    uncertain    the work may have happened but Faustus cannot tell: an outcome
                 that never arrived (cancelled, timed out, `outcome_unknown`), a
                 process whose state is unknown, an agent that runs outside the
                 command guard, and -- always -- effects of Code Mode code that
                 ran straight on the host, where only the bridged tool calls are
                 observed.

Precedence is unverified > uncertain > verified: a known failure is never
softened by an unrelated doubt, and a doubt is never resolved by an unrelated
success. "Verified" is never the default -- it needs at least one positive
observation.

Everything is additive: callers keep every field they already write and read
`outcome` beside them. Never raises: malformed evidence is itself a doubt.

Evidence sections (all optional, all plain dicts so a caller can hand over what
it already holds):

    tool_results   [{tool, ok, exit_code, status|result_status, error,
                     approval_required, blocked, code_mode?, process?}]
    tests          project_tests.compact()
    ui_smoke       ui_smoke.compact()
    completion     TurnLedger.check_completion() result
    changeset      {verdict, unsupported_claims, uncertainty}  (changesets + prove)
    processes      [process handle views from src/process_manager.py]
    code_mode      [run_code results]
    workers        [dispatch.compact() / external_worker.run_task() results]
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional

VERIFIED = "verified"
UNVERIFIED = "unverified"
UNCERTAIN = "uncertain"
OUTCOMES = (VERIFIED, UNVERIFIED, UNCERTAIN)
SCHEMA_VERSION = 1

_MAX_REASONS = 24
_DETAIL = 240

#: Tool-result statuses (src/contracts/tool.py) that mean "we do not know".
_UNKNOWN_STATUSES = frozenset({"outcome_unknown", "cancelled", "partial"})
_SUCCESS_STATUSES = frozenset({"", "succeeded", "ok", "success", "done", "complete", "passed"})

#: Process states (src/process_manager.py).
_PROCESS_LIVE = frozenset({"starting", "running"})
_PROCESS_UNKNOWN = frozenset({"lost", "uncertain", "orphaned"})


def enabled() -> bool:
    """Setting ``agent_outcome_verification`` (default on): whether results
    carry the classification. The function itself always works."""
    try:
        from src.settings import get_setting
        return bool(get_setting("agent_outcome_verification", True))
    except Exception:  # noqa: BLE001 - never raise into a hot path
        return True


class _Tally:
    def __init__(self) -> None:
        self.unverified: List[Dict[str, str]] = []
        self.uncertain: List[Dict[str, str]] = []
        self.positive: List[Dict[str, str]] = []
        self.notes: List[Dict[str, str]] = []
        self.sections: List[str] = []

    @staticmethod
    def _row(kind: str, source: str, detail: str) -> Dict[str, str]:
        return {"kind": kind, "source": source, "detail": str(detail or "")[:_DETAIL]}

    def section(self, name: str) -> None:
        if name not in self.sections:
            self.sections.append(name)

    def bad(self, kind: str, source: str, detail: str = "") -> None:
        self.unverified.append(self._row(kind, source, detail))

    def doubt(self, kind: str, source: str, detail: str = "") -> None:
        self.uncertain.append(self._row(kind, source, detail))

    def good(self, kind: str, source: str, detail: str = "") -> None:
        self.positive.append(self._row(kind, source, detail))

    def note(self, kind: str, source: str, detail: str = "") -> None:
        self.notes.append(self._row(kind, source, detail))


def _mapping(value: Any) -> Optional[Mapping[str, Any]]:
    return value if isinstance(value, Mapping) else None


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _exit_code(result: Mapping[str, Any]) -> Optional[int]:
    code = result.get("exit_code")
    if isinstance(code, bool):
        return int(code)
    if isinstance(code, (int, float)):
        return int(code)
    if isinstance(code, str) and code.strip().lstrip("-").isdigit():
        return int(code.strip())
    return None


def _status_of(result: Mapping[str, Any]) -> str:
    status = result.get("result_status")
    if not isinstance(status, str) or not status:
        status = result.get("status")
    return status.strip().lower() if isinstance(status, str) else ""


# -- Code Mode: what its effects are made of ---------------------------------

def code_mode_direct_effects(result: Any) -> Dict[str, Any]:
    """What a Code Mode result lets Faustus observe about the code's OWN effects
    (everything that did not go through the tool bridge).

    Only bridged `tools.call` invocations are receipted. Whatever the guest did
    by itself -- write a file, run a subprocess, open a socket -- leaves no
    receipt, so:

      host runtime ................ always unobserved: the code had the Faustus
                                    user's authority, anywhere on the machine;
      confined, workspace read-write unobserved inside the workspace (a change
                                    diff can cover exactly that);
      confined, network granted ... unobserved (nothing sees the traffic);
      confined, read-only, no net . none possible: the container could not
                                    change anything outside the bridge.
    """
    res = _mapping(result)
    if res is None:
        return {"observed": False, "scope": "unknown", "reason": "no result to read the runtime from",
                "coverable_by_diff": False}
    guarantees = _mapping(res.get("runtime_guarantees")) or {}
    mode = str(guarantees.get("mode") or "")
    if mode == "not_executed" or res.get("refused"):
        return {"observed": True, "scope": "none", "reason": "the code did not run", "coverable_by_diff": False}
    if mode != "container":
        return {"observed": False, "scope": "host", "coverable_by_diff": False,
                "reason": ("the code ran directly on the host: only the bridged tool calls are receipted, so "
                           "files, processes and network effects of the code itself are not observed")}
    scope = str(guarantees.get("filesystem_scope") or "")
    networked = guarantees.get("network_isolated") is False
    if networked:
        return {"observed": False, "scope": "network", "coverable_by_diff": False,
                "reason": "the container had network access and nothing observes that traffic"}
    if scope == "workspace_read_write":
        return {"observed": False, "scope": "workspace_write", "coverable_by_diff": True,
                "reason": "the container could write the workspace directly, outside the tool bridge"}
    return {"observed": True, "scope": "none", "coverable_by_diff": False,
            "reason": "read-only workspace and no network: only bridged calls could have effects"}


# -- per-section readers ------------------------------------------------------

def _read_tool_result(t: _Tally, row: Mapping[str, Any], *, source: str, later_ok_tools: frozenset) -> None:
    tool = str(row.get("tool") or row.get("name") or "tool")
    tag = f"{source}:{tool}"
    if row.get("approval_required") or row.get("requires_approval") or row.get("needs_approval"):
        t.bad("waiting_approval", tag, "the call was held at an approval boundary and did not run")
        return
    if row.get("blocked") or row.get("refused"):
        t.bad("blocked", tag, str(row.get("error") or "blocked by policy")[:_DETAIL])
        return
    status = _status_of(row)
    if status in _UNKNOWN_STATUSES:
        t.doubt(f"result_{status}", tag, "the call's outcome is unconfirmed or incomplete")
        return
    code = _exit_code(row)
    ok = row.get("ok")
    failed = bool(row.get("error")) or (code is not None and code != 0) or ok is False \
        or (status not in _SUCCESS_STATUSES)
    if failed:
        if tool in later_ok_tools:
            t.note("failure_recovered", tag, "an earlier call failed and a later call of the same tool succeeded")
        elif row.get("kind") == "read":
            t.note("lookup_failed", tag, "a lookup came back empty or failed; that is not a failure of the work")
        else:
            t.bad("tool_failed", tag, str(row.get("error") or (f"exit code {code}" if code is not None else status))[:_DETAIL])
        return
    if code == 0 or ok is True or status == "succeeded":
        t.good("tool_succeeded", tag, "exit code 0" if code == 0 else "succeeded")
    # A result with no exit code, no ok flag and no status says nothing either way.


def _read_tool_results(t: _Tally, rows: Iterable[Any], *, source: str = "tool") -> None:
    rows = [r for r in rows if isinstance(r, Mapping)]
    if rows:
        t.section(source + "_results")
    # A failure is cancelled out only by a LATER success of the same tool.
    last_ok_index: Dict[str, int] = {}
    for i, row in enumerate(rows):
        tool = str(row.get("tool") or row.get("name") or "tool")
        failed = bool(row.get("error")) or (_exit_code(row) not in (None, 0)) or row.get("ok") is False \
            or _status_of(row) not in _SUCCESS_STATUSES
        if not failed and (_exit_code(row) == 0 or row.get("ok") is True or _status_of(row) == "succeeded"):
            last_ok_index[tool] = i
    for i, row in enumerate(rows):
        tool = str(row.get("tool") or row.get("name") or "tool")
        later = frozenset([tool]) if last_ok_index.get(tool, -1) > i else frozenset()
        _read_tool_result(t, row, source=source, later_ok_tools=later)
        cm = _mapping(row.get("code_mode"))
        if cm is not None:
            _read_code_mode(t, cm, source=f"{source}:{tool}", count_exit=False)
        proc = _mapping(row.get("process"))
        if proc is not None:
            _read_process(t, proc)


def _read_tests(t: _Tally, tests: Any, label: str = "tests") -> None:
    data = _mapping(tests)
    if not data or not data.get("ran"):
        return
    t.section(label)
    if data.get("inconclusive"):
        t.doubt("tests_inconclusive", label, str(data.get("summary") or "the test run could not decide")[:_DETAIL])
    elif data.get("ok") is False and data.get("pre_existing_only"):
        t.doubt("tests_preexisting_failures", label,
                "tests fail, but only with failures that predate this change")
    elif data.get("ok") is False:
        t.bad("tests_failed", label, str(data.get("summary") or "the project's tests failed")[:_DETAIL])
    elif data.get("ok") is True:
        t.good("tests_passed", label, str(data.get("summary") or data.get("command") or "tests passed")[:_DETAIL])


def _read_ui_smoke(t: _Tally, smoke: Any) -> None:
    data = _mapping(smoke)
    if not data or not data.get("ran"):
        return
    t.section("ui_smoke")
    if data.get("ok") is False:
        t.bad("ui_smoke_failed", "ui_smoke", str(data.get("summary") or "the UI smoke check failed")[:_DETAIL])
    elif data.get("ok") is True:
        t.good("ui_smoke_passed", "ui_smoke", "the UI smoke check passed")


#: check_completion reasons that are a claim the log does not support; every
#: other reason is a stall (asking permission, announcing instead of acting).
_CLAIM_REASONS = frozenset({"claims_without_mutation", "fabricated_paths", "claimed_paths_untouched",
                            "ui_unverified", "fabricated_citations", "unconsulted_sources",
                            "image_claim_without_tool", "plan_without_action"})


def _read_completion(t: _Tally, check: Any) -> None:
    data = _mapping(check)
    if data is None:
        return
    t.section("completion")
    reasons = [str(r) for r in (data.get("reasons") or [])]
    if not reasons and data.get("ok", True):
        t.note("completion_claims_supported", "completion", "no sentence of the answer outruns the tool log")
        return
    for reason in reasons:
        kind = "claim_unsupported" if reason in _CLAIM_REASONS else "stopped_short"
        t.bad(kind, "completion", reason)


def _read_changeset(t: _Tally, changeset: Any) -> None:
    data = _mapping(changeset)
    if not data:
        return
    t.section("changeset")
    verdict = str(data.get("verdict") or "")
    claims = data.get("unsupported_claims") or []
    if verdict == "contradicted":
        t.bad("changeset_contradicted", "changeset", "the checkpoint contradicts what the answer claims changed")
    elif verdict == "unproved" and claims:
        t.bad("changeset_unproved", "changeset", "the answer names changes the checkpoint did not see")
    elif verdict == "partial":
        t.doubt("changeset_partial", "changeset", "the change is only partly proved")
    elif verdict == "proved":
        t.good("changeset_proved", "changeset", "the checkpoint saw the claimed change")
    for doubt in (data.get("uncertainty") or [])[:4]:
        if isinstance(doubt, Mapping) and doubt.get("kind"):
            t.note("changeset_doubt:" + str(doubt.get("kind")), "changeset", str(doubt.get("detail") or ""))


def _read_process(t: _Tally, rec: Mapping[str, Any]) -> None:
    handle = str(rec.get("handle") or "process")
    tag = f"process:{handle}"
    state = str(rec.get("state") or "").lower()
    code = _exit_code(rec)
    t.section("processes")
    if state in _PROCESS_LIVE:
        t.note("process_running", tag, "still running: its final result does not exist yet")
    elif state in _PROCESS_UNKNOWN:
        t.doubt(f"process_{state}", tag, str(rec.get("termination_reason") or "the process state is unknown"))
    elif state == "exited":
        if code == 0:
            t.good("process_exited_ok", tag, "exit code 0")
        elif code is None:
            t.doubt("process_exit_unknown", tag, "it ended without a recorded exit code")
        else:
            t.bad("process_exited_nonzero", tag, f"exit code {code}")
    elif state == "failed_to_start":
        t.bad("process_failed_to_start", tag, str(rec.get("termination_reason") or "")[:_DETAIL])
    elif state == "timed_out":
        t.doubt("process_timed_out", tag, "it was stopped at its runtime limit before finishing")
    elif state == "stopped":
        t.note("process_stopped", tag, "stopped on request: that is not a result")
    else:
        t.doubt("process_state_unrecognised", tag, state or "no state recorded")


def _read_code_mode(t: _Tally, result: Mapping[str, Any], *, source: str = "code_mode",
                    count_exit: bool = True, covered_by_diff: bool = False) -> None:
    if count_exit:
        t.section("code_mode")
    if result.get("refused") or (_mapping(result.get("runtime_guarantees")) or {}).get("mode") == "not_executed":
        t.bad("code_mode_refused", source, str(result.get("error") or "Code Mode was refused and did not run")[:_DETAIL])
        return
    terminated = str(((_mapping(result.get("receipt")) or {}).get("terminated_by")) or "")
    if terminated in ("timeout", "cancelled", "output", "memory", "error", "max_calls"):
        t.doubt(f"code_mode_{terminated}", source,
                f"it was stopped ({terminated}); calls and effects up to that point are not all accounted for")
    outcomes = _mapping(result.get("tool_outcomes")) or {}
    counts = _mapping(outcomes.get("counts")) or {}
    if counts.get("outcome_unknown") or counts.get("cancelled") or counts.get("partial"):
        t.doubt("code_mode_nested_unconfirmed", source,
                "one or more nested tool calls have an unconfirmed or partial result")
    if counts.get("failed") or counts.get("error"):
        t.note("code_mode_nested_failures", source, "some nested calls failed")
    if count_exit:
        code = _exit_code(result)
        if result.get("error") or (code is not None and code != 0):
            t.bad("code_mode_failed", source, str(result.get("error") or f"exit code {code}")[:_DETAIL])
        elif code == 0:
            t.good("code_mode_exit_0", source, "exit code 0")
    effects = code_mode_direct_effects(result)
    if not effects["observed"]:
        if effects.get("coverable_by_diff") and covered_by_diff:
            t.note("code_mode_direct_effects_covered", source, "workspace writes are covered by the change diff")
        else:
            t.doubt("code_mode_direct_effects_unobserved", source, effects["reason"])


def _read_worker(t: _Tally, worker: Mapping[str, Any]) -> None:
    t.section("workers")
    name = str(worker.get("runner") or worker.get("title") or worker.get("id") or "worker")
    tag = f"worker:{name}"
    res = _mapping(worker.get("result")) or worker          # dispatch envelope or a bare result
    status = str(res.get("status") or worker.get("status") or "").lower()
    code = _exit_code(res)
    if worker.get("cancelled") or res.get("cancelled") or status in ("cancelled", "canceled", "stopped"):
        t.doubt("worker_cancelled", tag, "it was stopped part-way; work on disk may be incomplete")
    elif worker.get("timed_out") or res.get("timed_out") or status in ("timeout", "timed_out", "stalled"):
        t.doubt("worker_timed_out", tag, "it hit its time limit; work on disk may be incomplete")
    elif status in ("error", "failed", "crashed") or (code is not None and code != 0) or res.get("error"):
        t.bad("worker_failed", tag, str(res.get("error") or f"status {status or 'error'}")[:_DETAIL])
    elif status in ("done", "complete") or res.get("ok") is True or code == 0:
        t.good("worker_finished", tag, "it reported done")
    else:
        t.doubt("worker_state_unknown", tag, f"status {status or 'missing'}")
    claimed_only = [p for p in _as_list(res.get("claimed_only")) if p]
    if claimed_only:
        t.bad("worker_claims_not_on_disk", tag,
              "it says it changed files the checkpoint did not see: " + ", ".join(map(str, claimed_only[:5])))
    changes = _mapping(res.get("changes"))
    if changes and (changes.get("added") or changes.get("modified") or changes.get("deleted")):
        t.good("worker_changes_observed", tag, "the checkpoint saw changed files")
    verification = _mapping(res.get("verification"))
    if verification:
        _read_tests(t, verification, label=f"{tag}:verification")
    proof = _mapping(res.get("proof"))
    if proof:
        _read_changeset(t, {"verdict": proof.get("verdict"), "uncertainty": proof.get("uncertainty"),
                            "unsupported_claims": claimed_only})
    gate = _mapping(res.get("gate"))
    proof_unguarded = proof is not None and any(
        isinstance(u, Mapping) and u.get("kind") == "external_agent_unguarded"
        for u in (proof.get("uncertainty") or []))
    if res.get("unguarded") is True or proof_unguarded \
            or (res.get("unguarded") is None and worker.get("unguarded") is True):
        t.doubt("worker_unguarded", tag,
                "it ran its own tools outside Faustus's command guard: effects beyond the workspace diff are not observed")
    elif gate is not None and res.get("unguarded") is False:
        t.note("worker_gated", tag, "its tool calls passed through Faustus's command guard")


# -- the function -------------------------------------------------------------

def verify_outcome(evidence: Optional[Mapping[str, Any]] = None, **sections: Any) -> Dict[str, Any]:
    """Classify the work behind `evidence` as verified / unverified / uncertain.

    Returns ``{"outcome", "reasons", "positive", "notes", "sources", "summary",
    "schema_version"}``. ``reasons`` are the rows that decided a non-verified
    outcome (unverified rows first, then uncertain ones). Never raises."""
    try:
        return _verify(dict(evidence or {}, **sections))
    except Exception as exc:  # noqa: BLE001 - a report about the work never breaks it
        return _result(UNCERTAIN, [_Tally._row("verification_error", "verify_outcome",
                                               f"{type(exc).__name__}: {exc}")], [], [], [])


def _verify(ev: Dict[str, Any]) -> Dict[str, Any]:
    t = _Tally()
    _read_tool_results(t, _as_list(ev.get("tool_results")))
    _read_tests(t, ev.get("tests"))
    _read_ui_smoke(t, ev.get("ui_smoke"))
    _read_completion(t, ev.get("completion"))
    _read_changeset(t, ev.get("changeset"))
    for rec in _as_list(ev.get("processes")):
        if isinstance(rec, Mapping):
            _read_process(t, rec)
    covered = bool(ev.get("changeset") or ev.get("changes_observed"))
    for res in _as_list(ev.get("code_mode")):
        if isinstance(res, Mapping):
            _read_code_mode(t, res, covered_by_diff=covered and _diff_has_changes(ev))
    for worker in _as_list(ev.get("workers")):
        if isinstance(worker, Mapping):
            _read_worker(t, worker)

    if t.unverified:
        outcome = UNVERIFIED
    elif t.uncertain:
        outcome = UNCERTAIN
    elif t.positive:
        outcome = VERIFIED
    else:
        outcome = UNVERIFIED
        t.bad("no_evidence", "verify_outcome", "nothing was observed that confirms the work")
    return _result(outcome, t.unverified + t.uncertain, t.positive, t.notes, t.sections)


def _diff_has_changes(ev: Mapping[str, Any]) -> bool:
    cs = _mapping(ev.get("changeset"))
    return bool(cs and str(cs.get("verdict") or "") in ("proved", "partial")) or bool(ev.get("changes_observed"))


def _result(outcome: str, reasons: List[Dict[str, str]], positive: List[Dict[str, str]],
            notes: List[Dict[str, str]], sections: List[str]) -> Dict[str, Any]:
    top = reasons[0] if reasons else (positive[0] if positive else None)
    summary = outcome if top is None else f"{outcome}: {top['kind']} ({top['source']})"
    return {
        "outcome": outcome,
        "reasons": reasons[:_MAX_REASONS],
        "positive": positive[:_MAX_REASONS],
        "notes": notes[:_MAX_REASONS],
        "sources": list(dict.fromkeys(sections)),
        "summary": summary,
        "schema_version": SCHEMA_VERSION,
    }


# -- adapters: hand the evidence a caller already has ------------------------

def verify_code_mode(result: Any) -> Dict[str, Any]:
    """The classification of one Code Mode result (attached to it by the runner)."""
    return verify_outcome(code_mode=[result] if isinstance(result, Mapping) else [])


def verify_worker(result: Any) -> Dict[str, Any]:
    """The classification of one worker result (dispatch.compact / external_worker)."""
    return verify_outcome(workers=[result] if isinstance(result, Mapping) else [])


def verify_process(record: Any) -> Dict[str, Any]:
    return verify_outcome(processes=[record] if isinstance(record, Mapping) else [])


def verify_turn(summary: Mapping[str, Any], *, tool_results: Optional[List[Mapping[str, Any]]] = None,
                completion: Optional[Mapping[str, Any]] = None,
                changeset: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """The classification of a finished agent turn from `TurnLedger.summary()`
    plus what the ledger recorded call by call."""
    summary = summary if isinstance(summary, Mapping) else {}
    return verify_outcome(
        tool_results=tool_results or [],
        tests=summary.get("tests"),
        ui_smoke=summary.get("ui_smoke"),
        completion=completion,
        changeset=changeset if changeset is not None else summary.get("changeset"),
    )


__all__ = ["OUTCOMES", "UNCERTAIN", "UNVERIFIED", "VERIFIED", "code_mode_direct_effects", "enabled",
           "verify_code_mode", "verify_outcome", "verify_process", "verify_turn", "verify_worker"]
