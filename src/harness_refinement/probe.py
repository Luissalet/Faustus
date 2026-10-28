"""A probed round: propose in two lines, repeat the task, compose, select blind, adopt.

This is the method of google-research/harness2 (Apache-2.0) applied to the four
axes Faustus can edit. After a task that shows a real signal (the person
corrected the assistant, repeated themselves, a tool failed and was repeated):

1. **Propose.** Two proposals on lines that cannot overlap: INV (how the agent
   works) and DOM (what a practitioner of the domain knows). The earlier
   lessons of the domain (``EXPERIENCE.md``) are in both prompts. An edit that
   fails the deterministic validation or the harness validator is dropped.
2. **Probe.** The original request is repeated read-only in the background
   against the global model: k times with the harness as it is, once per
   candidate with its edit.
3. **Compose.** An aggregator accepts an edit only with a citation of the
   moment in a trajectory that justifies it; a failure seen in one run is noise.
4. **Select blind.** The deliverables of the base repeats and of the accepted
   candidates are shuffled under neutral labels and a judge picks one.
5. **Adopt.** If a candidate wins, its proposal is applied (``auto_apply``) or
   left pending for a person (``auto_apply`` off); the others are stored as
   rejected with their evidence. The exact undo by id is the one that already
   exists. A lesson is appended to ``EXPERIENCE.md`` when the round showed a
   real contrast.

Admission is the existing one (:func:`runner.admission_blocker`): nothing here
loads a model on its own or competes with a person using the app, and a round
that cannot continue is dropped whole, never half applied.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from src.harness_refinement import compose, experience, probe_store, proposer, rollout, signals, store, targets, validator
from src.harness_refinement.store import HarnessError

logger = logging.getLogger(__name__)

SETTING_PROBE = "harness_refinement_probe"
SETTING_AUTO_APPLY = "harness_refinement_auto_apply"
SETTING_K = "harness_refinement_probe_k"
DEFAULT_K = 2
MAX_K = 3
LINES = ("inv", "dom")
CONTEXT_MESSAGES = 6

ProbeLlm = Callable[[str, str, str, Dict[str, Any]], Awaitable[Any]]
AdmissionFn = Callable[[Optional[str], Optional[str]], Optional[str]]


# -- settings -------------------------------------------------------------------------

def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:  # noqa: BLE001 - settings trouble means the default
        return default


def probe_enabled() -> bool:
    return bool(_setting(SETTING_PROBE, True))


def auto_apply_enabled() -> bool:
    return bool(_setting(SETTING_AUTO_APPLY, True))


def probe_k() -> int:
    try:
        return max(1, min(MAX_K, int(_setting(SETTING_K, DEFAULT_K))))
    except (TypeError, ValueError):
        return DEFAULT_K


# -- the global model -------------------------------------------------------------------

def resolve_route(owner: str = "") -> Tuple[Optional[str], Optional[str], Optional[Dict[str, str]]]:
    """The model every Faustus feature uses: the global one (the Sparks while they serve)."""
    try:
        from src.endpoint_resolver import resolve_endpoint
        return resolve_endpoint(proposer.PURPOSE, owner=owner or None)
    except Exception:  # noqa: BLE001
        logger.debug("[harness_refinement] route resolution failed", exc_info=True)
        return None, None, None


def _make_llm(headers: Optional[Dict[str, str]]) -> ProbeLlm:
    async def call(url: str, model: str, prompt: str, schema: Dict[str, Any]) -> Any:
        from src.llm_core import llm_call_async
        return await llm_call_async(
            url=url, model=model, messages=[{"role": "user", "content": prompt}], headers=headers,
            temperature=0.0, max_tokens=2500, timeout=180, max_retries=1, workload="background",
            response_schema=schema)
    return call


# -- the task ---------------------------------------------------------------------------

def task_from(traj: signals.Trajectory, assessment: signals.Assessment) -> Optional[Dict[str, Any]]:
    """What to repeat: the request that was answered badly, what the person said afterwards, and the
    real session as a run the aggregator can cite (``o``)."""
    if not assessment.evidence:
        return None
    ev = next((e for e in assessment.evidence if e["kind"] == "correction"), None) \
        or next((e for e in assessment.evidence if e["kind"] == "repeat"), None) or assessment.evidence[0]
    turn = int(ev["turn"])
    msgs = traj.messages
    from src.skills_runtime.sleep_optimize import classify_reaction
    followup = ""
    end = turn
    if msgs[turn].role == "user":
        followup = msgs[turn].content.strip()
        end = turn - 1
    j = next((i for i in range(min(end, len(msgs) - 1), -1, -1)
              if msgs[i].role == "user" and classify_reaction(msgs[i].content.strip()) != "negative"), None)
    if j is None or not msgs[j].content.strip():
        return None
    steps: List[Dict[str, Any]] = []
    final_text = ""
    for msg in msgs[j + 1:end + 1]:
        if msg.role == "assistant":
            if msg.content.strip():
                steps.append({"n": len(steps) + 1, "kind": "assistant", "text": msg.content.strip()[:600]})
                final_text = msg.content.strip()
            for event in msg.tool_events:
                steps.append({"n": len(steps) + 1, "kind": "tool", "tool": str(event.get("tool") or ""),
                              "args": str(event.get("command") or "")[:160], "ok": not signals.is_failure(event),
                              "output": str(event.get("output") or "")[:240]})
    if followup:
        steps.append({"n": len(steps) + 1, "kind": "user", "text": followup[:400]})
    context = [{"role": m.role, "content": m.content.strip()} for m in msgs[max(0, j - CONTEXT_MESSAGES):j]
               if m.role in ("user", "assistant") and m.content.strip()]
    return {
        "text": msgs[j].content.strip()[:3000], "followup": followup[:800], "kind": ev["kind"], "context": context,
        "original": {"id": "o", "arm": "original", "title": "the real session (what the person actually got)",
                     "steps": steps, "text": final_text[:rollout.TEXT_CHARS], "delivered": bool(final_text)},
    }


# -- helpers --------------------------------------------------------------------------------

def workspace_of(project_id: str, owner: str) -> str:
    """The project's folder when it exists on disk, else '' (a repeat without one simply cannot read files)."""
    if not project_id:
        return ""
    try:
        folder = str((targets._project_store().get(project_id, owner or None) or {}).get("workspace") or "")
    except Exception:  # noqa: BLE001 - no project store, no workspace
        return ""
    return folder if folder and os.path.isdir(folder) else ""


async def _gate(admission: Optional[AdmissionFn], url: str, model: str, *, user_initiated: bool, sleep: Callable,
                budget: Dict[str, float], wait_s: float, poll_s: float) -> Optional[str]:
    """None when the next model call may start; otherwise the reason it cannot, after waiting."""
    if admission is None or user_initiated:
        return None
    blocker = admission(url, model)
    while blocker and budget["waited"] < wait_s:
        await sleep(poll_s)
        budget["waited"] += poll_s
        blocker = admission(url, model)
    return blocker


class _Dropped(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _brief_evidence(evidence: Sequence[Mapping[str, Any]]) -> str:
    return "; ".join(f"{e['run']} step {e['step']}: {e.get('quote', '')}" for e in evidence[:3])


# -- the round ----------------------------------------------------------------------------------

async def probe_for_session(session_id: str, *, owner: str = "", trigger: Optional[str] = None,
                            user_initiated: bool = False, force: bool = False, probe_id: Optional[str] = None,
                            llm: Optional[ProbeLlm] = None, loop: Optional[rollout.LoopFn] = None,
                            admission: Optional[AdmissionFn] = None, sleep: Callable = asyncio.sleep,
                            wait_s: float = 600.0, poll_s: float = 5.0,
                            route: Optional[Tuple[Optional[str], Optional[str], Optional[Dict[str, str]]]] = None,
                            k: Optional[int] = None, auto_apply: Optional[bool] = None,
                            max_rounds: int = rollout.DEFAULT_MAX_ROUNDS,
                            timeout_s: float = rollout.DEFAULT_TIMEOUT_S) -> Dict[str, Any]:
    """Run one probed round for a session. ``status`` is ``adopted`` (a candidate won and its proposal is
    ``applied`` or ``pending``), ``kept_base`` (nothing earned a place; the rejected candidates are on
    record), ``none`` (the model saw nothing to change), ``skipped`` or ``error``. Never raises for a
    model or loop failure."""
    if session_id in proposer._INFLIGHT:
        return {"status": "skipped", "reason": "already_running"}
    proposer._INFLIGHT.add(session_id)
    try:
        return await _run(session_id, owner, trigger, user_initiated, force, probe_id, llm, loop, admission, sleep,
                          wait_s, poll_s, route, k, auto_apply, max_rounds, timeout_s)
    except _Dropped as dropped:
        if probe_id:
            probe_store.update(probe_id, status="aborted", phase="aborted", reason=dropped.reason)
        return {"status": "skipped", "reason": dropped.reason, "blocked": True, "probe_id": probe_id}
    finally:
        proposer._INFLIGHT.discard(session_id)
        if probe_id:
            probe_store._ACTIVE.discard(probe_id)


async def _run(session_id, owner, trigger, user_initiated, force, probe_id, llm, loop, admission, sleep,
               wait_s, poll_s, route, k, auto_apply, max_rounds, timeout_s) -> Dict[str, Any]:
    traj = signals.load_trajectory(session_id, owner or None)
    if traj is None:
        return {"status": "error", "reason": "session_not_found"}
    owner = owner or traj.owner or ""
    pending = store.pending_for_session(session_id)
    if pending:
        return {"status": "skipped", "reason": "already_pending", "proposal": pending}
    assessment = signals.assess(traj)
    if not assessment.worth and not force:
        if user_initiated:
            store.log_event("skipped", trigger=trigger or "manual", result=assessment.reason, session_id=session_id,
                            owner=owner, detail={"score": assessment.score})
        return {"status": "skipped", "reason": assessment.reason, "assessment": assessment.to_dict()}
    trig = trigger or (assessment.trigger if assessment.worth else "manual")
    if force and not assessment.evidence:
        last_user = next((i for i in range(len(traj.messages) - 1, -1, -1) if traj.messages[i].role == "user"), None)
        if last_user is not None:
            assessment.evidence.append(signals._evidence(traj, last_user, "manual", traj.messages[last_user].content))
    task = task_from(traj, assessment)
    if task is None:
        store.log_event("none", trigger=trig, result="no_task", session_id=session_id, owner=owner)
        return {"status": "none", "reason": "no_task"}

    url, model, headers = route if route is not None else resolve_route(owner)
    if llm is None:
        if not url or not model:
            store.log_event("model_error", trigger=trig, result="no_endpoint", session_id=session_id, owner=owner)
            return {"status": "error", "reason": "no_endpoint"}
        llm = _make_llm(headers)
    url, model = url or "fake://", model or "fake-model"

    k = max(1, min(MAX_K, int(k if k is not None else probe_k())))
    auto = auto_apply_enabled() if auto_apply is None else bool(auto_apply)
    domain = experience.domain_for(traj.project_id, traj.project_name, owner)
    workspace = workspace_of(traj.project_id, owner)
    budget = {"waited": 0.0}

    async def gate() -> None:
        blocker = await _gate(admission, url, model, user_initiated=user_initiated, sleep=sleep, budget=budget,
                              wait_s=wait_s, poll_s=poll_s)
        if blocker:
            raise _Dropped(blocker)

    await gate()                                   # the first model call: refuse before any record exists
    record = probe_store.create(session_id, owner, trig, probe_id=probe_id, domain=domain.key, k=k)
    pid = record["id"]
    probe_store.update(pid, task={k_: v for k_, v in task.items() if k_ not in ("original", "context")},
                       phase="proposing")
    try:
        return await _round(pid, traj, assessment, task, domain, owner, trig, url, model, headers, llm, loop, gate,
                            k, auto, max_rounds, timeout_s, workspace)
    except _Dropped as dropped:
        store.log_event("skipped", trigger=trig, result=dropped.reason, session_id=session_id, owner=owner,
                        detail={"probe_id": pid, "waited_s": budget["waited"]})
        probe_store.update(pid, status="aborted", phase="aborted", reason=dropped.reason)
        return {"status": "skipped", "reason": dropped.reason, "blocked": True, "probe_id": pid}
    except asyncio.CancelledError:
        probe_store.update(pid, status="aborted", phase="aborted", reason="cancelled")
        raise
    except Exception as exc:  # noqa: BLE001 - a probe that breaks leaves a record, not a half-applied edit
        logger.warning("[harness_refinement] probe %s failed: %s", pid, exc, exc_info=True)
        store.log_event("probe_error", trigger=trig, result=type(exc).__name__, session_id=session_id, owner=owner,
                        detail={"probe_id": pid, "message": str(exc)[:300]})
        probe_store.update(pid, status="error", phase="error", error=f"{type(exc).__name__}: {exc}"[:300])
        return {"status": "error", "reason": "probe_failed", "message": str(exc)[:300], "probe_id": pid}


async def _round(pid, traj, assessment, task, domain, owner, trig, url, model, headers, llm, loop, gate, k, auto,
                 max_rounds, timeout_s, workspace="") -> Dict[str, Any]:
    session_id = traj.session_id
    probe_text = " ".join([m.content for m in traj.messages if m.role == "user"][-10:]
                          + [e["quote"] for e in assessment.evidence])[-4000:]
    cands = proposer.candidates_for(traj, owner, probe_text)
    components = rollout.components_from(cands)
    lessons = experience.for_prompt(domain)

    # 1. propose, two lines that cannot touch the same target ----------------------------------
    edits: Dict[str, Dict[str, Any]] = {}
    rows: List[Dict[str, Any]] = []
    taken: set = set()
    for index, line in enumerate(LINES, start=1):
        cid = f"c{index}"
        row: Dict[str, Any] = {"id": cid, "line": line, "valid": False, "result": None}
        rows.append(row)
        offered = [c for c in cands if c.target not in taken or c.new_target]
        if not offered:
            row.update(result="none", reason="no target left for this line")
            continue
        await gate()
        prompt = proposer.build_prompt(traj, assessment, offered, lens=line, lessons=lessons)
        try:
            raw = await llm(url, model, prompt, proposer.RESPONSE_SCHEMA)
        except Exception as exc:  # noqa: BLE001
            row.update(result="error", reason=f"model_error: {type(exc).__name__}")
            continue
        data = proposer._parse(raw)
        if data is None:
            row.update(result="error", reason="unparseable")
            continue
        if not data.get("propose"):
            row.update(result="none", reason=str(data.get("reason") or "")[:300])
            continue
        try:
            edit = proposer.validate_proposed(data, offered, assessment, owner)
        except HarnessError as exc:
            row.update(result="invalid", reason=exc.error_class, message=exc.message[:300])
            store.log_event("invalid", trigger=trig, result=exc.error_class, session_id=session_id, owner=owner,
                            detail={"line": line, "target": str(data.get("target"))})
            continue
        edits[cid] = edit
        if not edit["new_target"]:
            taken.add(edit["target"])
        row.update(valid=True, axis=edit["axis"], target=edit["target"], op=edit["op"], rationale=edit["reason"][:600],
                   diff=store.make_diff(edit["before"], edit["after"], edit["target"]))
        probe_store.update(pid, candidates=rows)
    probe_store.update(pid, candidates=rows)

    if not edits:
        return _finish_without_candidates(pid, traj, trig, owner, rows)

    # 2. probe: k repeats of the base, one per candidate ------------------------------------------
    probe_store.update(pid, phase="probing")
    runs: Dict[str, Dict[str, Any]] = {}
    base_ids: List[str] = []
    for i in range(1, k + 1):
        await gate()
        msgs = rollout.build_messages(task["text"], components, task["context"])
        run = await rollout.run_rollout(f"b{i}", "base", msgs, url=url, model=model, headers=headers, owner=owner,
                                        max_rounds=max_rounds, timeout_s=timeout_s, loop=loop, workspace=workspace)
        run["title"] = f"repeat {i} of {k} under the unmodified configuration"
        runs[run["id"]] = run
        base_ids.append(run["id"])
        probe_store.update(pid, rollouts=list(runs.values()))
    if not any(runs[b].get("delivered") for b in base_ids) and all(runs[b].get("error") for b in base_ids):
        probe_store.update(pid, status="aborted", phase="aborted", reason="model_unavailable",
                           error=runs[base_ids[0]].get("error", "")[:300])
        store.log_event("probe_error", trigger=trig, result="model_unavailable", session_id=session_id, owner=owner,
                        detail={"probe_id": pid})
        return {"status": "error", "reason": "model_unavailable", "probe_id": pid}
    cand_runs: Dict[str, Dict[str, Any]] = {}
    for cid, edit in edits.items():
        await gate()
        msgs = rollout.build_messages(task["text"], rollout.with_edit(components, edit), task["context"])
        run = await rollout.run_rollout(cid, edit["axis"], msgs, url=url, model=model, headers=headers, owner=owner,
                                        candidate=cid, max_rounds=max_rounds, timeout_s=timeout_s, loop=loop,
                                        workspace=workspace)
        run["title"] = f"repeat with the {next(r['line'] for r in rows if r['id'] == cid).upper()} edit to {edit['target']}"
        runs[cid] = run
        cand_runs[cid] = run
        probe_store.update(pid, rollouts=list(runs.values()))

    # 3. compose --------------------------------------------------------------------------------------
    probe_store.update(pid, phase="composing")
    original = task["original"]
    all_runs = {"o": original, **runs}
    observed_ids = (["o"] if original["steps"] else []) + base_ids
    observed = [original] if original["steps"] else []
    observed += [runs[b] for b in base_ids]
    cand_rows = [r for r in rows if r["id"] in edits]
    await gate()
    prompt = compose.build_compose_prompt(task, observed, cand_rows, cand_runs, k, lessons)
    parsed = None
    try:
        parsed = proposer._parse(await llm(url, model, prompt, compose.COMPOSE_SCHEMA))
    except Exception as exc:  # noqa: BLE001
        logger.debug("compose call failed: %s", exc)
    verdicts = compose.enforce(parsed, observed_ids, all_runs, cand_runs, list(edits))
    probe_store.update(pid, compose={"notes": str((parsed or {}).get("notes") or "")[:600],
                                     "keep_base": bool((parsed or {}).get("keep_base")), "verdicts": verdicts})

    # 4. select blind -----------------------------------------------------------------------------------
    accepted = [v["candidate"] for v in verdicts if v["accepted"]]
    selection: Dict[str, Any] = {"pool": [], "winner": "base", "why": "no candidate was accepted"}
    winner_id: Optional[str] = None
    if accepted:
        probe_store.update(pid, phase="selecting")
        seed = random.SystemRandom().randrange(1, 2 ** 31)
        pool = compose.shuffle_pool(base_ids + accepted, seed)
        await gate()
        chosen = None
        why = ""
        try:
            parsed_choice = proposer._parse(await llm(url, model, compose.build_select_prompt(task, pool, runs),
                                                      compose.SELECT_SCHEMA))
            chosen = compose.parse_choice(parsed_choice, pool)
            why = str((parsed_choice or {}).get("why") or "")[:600]
        except Exception as exc:  # noqa: BLE001
            logger.debug("selection call failed: %s", exc)
        winner_id = chosen if chosen in accepted else None
        selection = {"seed": seed, "pool": [{"label": lab, "run": rid} for lab, rid in pool],
                     "chosen_run": chosen, "winner": winner_id or "base",
                     "why": why or ("the answer was not one of the labels" if chosen is None else "")}
    probe_store.update(pid, selection=selection)

    # 5. the harness validator, once more right before adopting ------------------------------------------
    adoption_note = ""
    if winner_id:
        edit = edits[winner_id]
        problems = validator.check_edit(edit["axis"], edit["target"], edit["op"], edit["before"], edit["after"], owner)
        if problems:
            adoption_note = "back to base: " + "; ".join(problems)
            winner_id = None
            selection["winner"] = "base"
            selection["why"] = (selection.get("why") or "") + " | " + adoption_note
            probe_store.update(pid, selection=selection)

    # 6. the lesson (decided now, written once the edits are on record) ---------------------------------------
    for r in rows:
        if r["id"] in edits:
            v = next((x for x in verdicts if x["candidate"] == r["id"]), {})
            r.update(accepted=bool(v.get("accepted")), rejected_because=v.get("rejected_because"),
                     won=(r["id"] == winner_id), delivered=bool(runs[r["id"]].get("delivered")))
    lesson_info = await _lesson(pid, task, rows, edits, base_ids, runs, verdicts, winner_id, selection, url, model,
                                llm, gate)

    # 7. record every candidate; apply or leave pending the winner ------------------------------------------------
    outcome = _record_edits(pid, traj, trig, owner, model, rows, edits, verdicts, winner_id, auto, lesson_info)
    entry_info = _write_lesson(domain, pid, lesson_info, edits, rows)
    store.mark_processed(session_id, traj.last_ts)
    probe_store.update(pid, candidates=rows, outcome=outcome, experience=entry_info, status="done", phase="done",
                       reason=adoption_note)
    store.log_event("probed", trigger=trig, result=outcome["winner"], session_id=session_id, owner=owner,
                    detail={"probe_id": pid, "mode": outcome["mode"], "k": k})
    status = "adopted" if outcome["winner"] != "base" else "kept_base"
    result = {"status": status, "reason": "ok", "probe_id": pid, "outcome": outcome}
    if outcome.get("proposal_id"):
        result["proposal"] = store.get(outcome["proposal_id"])
    return result


def _finish_without_candidates(pid, traj, trig, owner, rows) -> Dict[str, Any]:
    reasons = "; ".join(f"{r['line']}: {r.get('reason') or r.get('result')}" for r in rows)
    store.log_event("none", trigger=trig, result="no_valid_candidate", session_id=traj.session_id, owner=owner,
                    detail={"probe_id": pid, "lines": reasons[:400]})
    store.mark_processed(traj.session_id, traj.last_ts)
    probe_store.update(pid, status="done", phase="done", reason=reasons[:400],
                       outcome={"winner": "base", "mode": "none", "applied": False, "proposal_id": None},
                       experience={"status": "no_entry", "reason": "no candidate"})
    return {"status": "none", "reason": "no_valid_candidate", "probe_id": pid, "detail": reasons[:400]}


async def _lesson(pid, task, rows, edits, base_ids, runs, verdicts, winner_id, selection, url, model, llm, gate):
    cand_runs = [runs[c] for c in edits]
    if not compose.has_contrast([runs[b] for b in base_ids], cand_runs):
        return {"status": "no_entry", "reason": "no real contrast between the repeats"}
    outcome = "a candidate won" if winner_id else "the unmodified configuration was kept"
    prompt = compose.build_experience_prompt(task, [r for r in rows if r["id"] in edits], outcome,
                                             selection.get("why") or "")
    try:
        await gate()
        parsed = proposer._parse(await llm(url, model, prompt, compose.EXPERIENCE_SCHEMA))
    except _Dropped:
        raise
    except Exception as exc:  # noqa: BLE001
        return {"status": "no_entry", "reason": f"model_error: {type(exc).__name__}"}
    if not isinstance(parsed, dict) or parsed.get("no_entry"):
        return {"status": "no_entry", "reason": "NO_ENTRY", "verdicts": compose.read_verdicts(parsed, rows)}
    by_cand = compose.read_verdicts(parsed, [r for r in rows if r["id"] in edits])
    # a lesson written after a win is about the winner: only the winner's verdict can stand for it
    order = [winner_id] if winner_id in by_cand else list(by_cand)
    checked = experience.check_lesson(parsed, [by_cand[c] for c in order])
    if "entry" not in checked:
        return {"status": "no_entry", "reason": checked["skipped"], "verdicts": by_cand}
    about = next((c for c in order if by_cand[c] == checked["entry"]["verdict"]), "")
    return {"status": "lesson", "entry": checked["entry"], "verdicts": by_cand, "about": about}


def _record_edits(pid, traj, trig, owner, model, rows, edits, verdicts, winner_id, auto, lesson_info) -> Dict[str, Any]:
    """Store every candidate as a proposal: the winner applied (or pending), the rest rejected with evidence."""
    session_id = traj.session_id
    outcome: Dict[str, Any] = {"winner": winner_id or "base", "mode": "none", "applied": False, "proposal_id": None}
    by_cand = lesson_info.get("verdicts") or {}
    for row in rows:
        cid = row["id"]
        edit = edits.get(cid)
        if edit is None:
            continue
        verdict = next((v for v in verdicts if v["candidate"] == cid), {})
        won = cid == winner_id
        evidence = verdict.get("evidence") or []
        if won:
            why = "won the blind selection against the unmodified configuration; " + (_brief_evidence(evidence) or "")
        elif verdict.get("accepted"):
            why = "accepted by the aggregator but the blind selection chose another answer"
        else:
            why = {"keep_base": "the aggregator kept the unmodified configuration",
                   "not_accepted": "the aggregator did not accept it: " + str(verdict.get("reason") or ""),
                   "failure_not_repeated": "the failure it cites is not repeated across the observed runs (noise)",
                   "no_valid_citation": "it cited no moment of a trajectory that exists",
                   "candidate_run_delivered_nothing": "its repeat delivered nothing, so the edit is unsafe",
                   "no_decision": "the aggregator gave no decision",
                   "compose_unparseable": "the aggregator's answer could not be read",
                   }.get(verdict.get("rejected_because") or "", "it did not earn a place")
        extra = {"probe_id": pid, "probe_line": row["line"], "probe_candidate": cid,
                 "probe_result": "adopted" if won else "discarded", "probe_reason": why[:600],
                 "probe_evidence": evidence, "probe_verdict": by_cand.get(cid, "")}
        record = proposer.store_edit(edit, session_id=session_id, owner=owner, trigger=trig, model=model,
                                     extra=extra, delegate=won)
        row["proposal_id"] = record["id"]
        row["result"] = "adopted" if won else "discarded"
        row["reason"] = why[:600]
        if not won:
            store.reject(record["id"], by="harness-probe", reason=why[:500])
            continue
        outcome["proposal_id"] = record["id"]
        if auto:
            try:
                applied = store.approve(record["id"], by="harness-probe")
                outcome.update(mode="auto", applied=True)
                outcome["applied_at"] = applied.get("applied_at")
            except HarnessError as exc:
                outcome.update(mode="pending", applied=False, note=f"could not apply: {exc.error_class}")
                row["result"] = "pending"
        else:
            outcome.update(mode="pending", applied=False)
            row["result"] = "pending"
    return outcome


def _write_lesson(domain, pid, lesson_info, edits, rows) -> Dict[str, Any]:
    if lesson_info.get("status") != "lesson":
        return {"status": "no_entry", "reason": lesson_info.get("reason", "")}
    label = ""
    cid = lesson_info.get("about") or ""
    if cid in edits:
        label = f"{edits[cid]['axis']}:{edits[cid]['target']}"
    try:
        written = experience.append(domain, lesson_info["entry"], label=label)
    except Exception as exc:  # noqa: BLE001 - a lesson that cannot be written must not undo the round
        logger.debug("experience write failed", exc_info=True)
        return {"status": "error", "reason": f"{type(exc).__name__}: {exc}"[:200]}
    if not written.get("written"):
        return {"status": "skipped", "reason": written.get("reason", "")}
    return {"status": "written", "path": written["path"], "entry": lesson_info["entry"]}
