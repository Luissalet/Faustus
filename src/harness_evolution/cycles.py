"""Measured configuration cycles on the versioned harness, never source-code edits.

Uses existing refinement signals, daily checks, global model resolution and
CandidatePatch/CAS promotion. Trial configurations never become active.
"""
from __future__ import annotations

import asyncio
import copy
import json
import math
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import runtime
from .models import HarnessRevision, new_id
from .service import HarnessEvolutionService
from .store import HarnessEvolutionStore, StaleParent
from src.contracts.base import now_iso


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=120)
    group: str = Field(min_length=1, max_length=100)
    prompt: str = Field(min_length=1, max_length=12000)
    messages: list[dict[str, str]] = Field(default_factory=list, max_length=40)
    reference: str = Field(default="", max_length=50000)
    mode: Literal["completion", "agent"] = "completion"
    checks: dict = Field(default_factory=dict)
    source: str = "manual"
    galton_checker: dict | None = None


class CycleSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="Ciclo de mejora", min_length=1, max_length=160)
    project_id: str = Field(default="", max_length=64)
    target_group: str = Field(min_length=1, max_length=100)
    cases: list[Case] = Field(min_length=2, max_length=100)
    regression_threshold: float = Field(default=.01, ge=0, le=1, allow_inf_nan=False)
    candidates: int = Field(default=2, ge=1, le=8)
    repeats: int = Field(default=1, ge=1, le=5)
    endpoint_url: str = ""
    model: str = ""
    base_system: str = Field(default="Responde con precisión a la petición.", max_length=8000)
    max_tokens: int = Field(default=2048, ge=64, le=16384)
    galton_url: str = ""
    galton_run_ids: list[str] = Field(default_factory=list, max_length=20)
    session_ids: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def groups_and_checks(self):
        if len({c.id for c in self.cases}) != len(self.cases):
            raise ValueError("case ids must be unique")
        groups = {c.group for c in self.cases}
        if self.target_group not in groups or len(groups) < 2:
            raise ValueError("provide target cases and at least one other group")
        for case in self.cases:
            if not case.checks and not case.galton_checker:
                raise ValueError(f"case {case.id} needs explicit checks")
            if case.galton_checker and not self.galton_url:
                raise ValueError("Galton checker needs the enabled evaluation API URL")
        if bool(self.endpoint_url) != bool(self.model):
            raise ValueError("endpoint_url and model must be provided together")
        return self


async def galton_call(url, name, arguments):
    """Read an explicitly enabled native evaluation module; never enable/start it."""
    import httpx
    import os
    token = os.environ.get("FAUSTUS_EVALUATION_TOKEN", "")
    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.post(url.rstrip("/") + "/api/agent/call",
            headers={"Authorization": f"Bearer {token}"} if token else {},
            json={"name": name, "arguments": arguments})
        response.raise_for_status()
        data = response.json()
        if data.get("error") or data.get("ok") is False:
            raise ValueError(f"Galton: {data.get('error', data.get('message', 'error'))}")
        return data.get("result", data)


async def galton_cases(url, suite_ids):
    cases = []
    for ident in suite_ids:
        listing = await galton_call(url, "suite_get", {"suite": ident, "include_cases": True, "limit": 100})
        if listing.get("total_cases", 0) > len(listing.get("cases", [])):
            raise ValueError("La suite tiene más casos de los que admite un ciclo; selecciona una batería menor.")
        for item in listing.get("cases", []):
            full = (await galton_call(url, "case_get", {"case": item["id"]}))["case"]
            prompt = full["prompt"]
            if full.get("tools") or full.get("images") or (isinstance(prompt, dict) and prompt.get("generate")):
                raise ValueError("Este importador admite casos textuales; los casos con herramientas, imágenes o generadores necesitan su batería específica.")
            messages = list(prompt.get("messages") or []) if isinstance(prompt, dict) else []
            question = (prompt.get("text") or next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")) if isinstance(prompt, dict) else prompt
            if isinstance(prompt, dict) and prompt.get("system"):
                messages.insert(0, {"role": "system", "content": prompt["system"]})
            if not any(m.get("role") == "user" for m in messages):
                messages.append({"role": "user", "content": question})
            cases.append(Case(id=full["id"], group=ident, prompt=question, messages=messages,
                              reference=full.get("reference", ""), source=f"galton:{ident}", galton_checker=full["checker"]))
    return [c.model_dump() for c in cases]


def _valid_score(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def gate(before, after, target_group, regression_threshold=.01):
    """Exact paired coverage, objective improvement, and a bound in EACH other group.

    Scores are [0,1]; .01 means one percentage point. Missing/errored judge
    results are insufficient evidence, never a zero that a candidate can beat.
    """
    result = {"eligible": False, "reason": "insufficient_data", "groups": {},
              "threshold": regression_threshold, "target_group": target_group}
    if not _valid_score(regression_threshold) or not before or not after:
        return result
    a = {r.get("id"): r for r in before}
    b = {r.get("id"): r for r in after}
    if len(a) != len(before) or len(b) != len(after) or set(a) != set(b):
        return result
    for ident in a:
        x, y = a[ident], b[ident]
        if not ident or not x.get("group") or x.get("group") != y.get("group"):
            return result
        if any(r.get("error") or not _valid_score(r.get("score")) for r in (x, y)):
            return result
    groups = {r["group"] for r in before}
    if target_group not in groups or len(groups) < 2:
        return result
    for group in sorted(groups):
        ids = [i for i in a if a[i]["group"] == group]
        old = sum(a[i]["score"] for i in ids) / len(ids)
        new = sum(b[i]["score"] for i in ids) / len(ids)
        result["groups"][group] = {"before": old, "after": new, "delta": new - old, "cases": len(ids)}
    if result["groups"][target_group]["delta"] <= 1e-12:
        result["reason"] = "target_not_improved"
    elif any(v["delta"] < -regression_threshold - 1e-12 for g, v in result["groups"].items() if g != target_group):
        result["reason"] = "regression"
    else:
        result.update(eligible=True, reason="improved")
    return result


class CycleStore:
    def __init__(self, store=None):
        self.harness = store or HarnessEvolutionStore()
        with self.harness._conn() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS improvement_cycles (id TEXT PRIMARY KEY, owner TEXT NOT NULL, payload TEXT NOT NULL)")

    def save(self, record):
        with self.harness._conn() as conn:
            conn.execute("INSERT INTO improvement_cycles VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                         (record["id"], record["owner"], json.dumps(record, ensure_ascii=False)))

    def get(self, ident, owner):
        with self.harness._conn() as conn:
            row = conn.execute("SELECT payload FROM improvement_cycles WHERE id=? AND owner=?", (ident, owner)).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, owner):
        with self.harness._conn() as conn:
            rows = conn.execute("SELECT payload FROM improvement_cycles WHERE owner=? ORDER BY rowid DESC LIMIT 100", (owner,)).fetchall()
        return [json.loads(row[0]) for row in rows]


def daily_cases():
    path = Path(__file__).resolve().parents[2] / "scripts" / "daily_eval_tasks.json"
    tasks = json.loads(path.read_text(encoding="utf-8"))
    return [Case(id=t["id"], group=t["id"].split("-")[0], mode="agent", source="daily_battery",
                 prompt="\n".join(t["messages"]), checks={k: v for k, v in t.items() if k not in ("id", "messages")})
            for t in tasks if not t.get("writes") and t.get("messages")]


def collect_signals(owner, session_ids=()):
    from src.harness_refinement import signals
    grouped = {}
    if not session_ids:
        from core.database import Session as ChatSession, SessionLocal
        db = SessionLocal()
        try:
            session_ids = [r.id for r in db.query(ChatSession).filter(ChatSession.owner == owner)
                           .order_by(ChatSession.updated_at.desc()).limit(20).all()]
        finally:
            db.close()
    for ident in session_ids:
        traj = signals.load_trajectory(ident, owner, since_ts=0)
        if not traj: continue
        assessment = signals.assess(traj)
        for evidence in assessment.evidence:
            kind = evidence.get("kind", "unknown")
            grouped.setdefault(kind, []).append({"source": "session", "id": ident, **evidence})
        from src import llm_trace
        for trace in llm_trace._iter_records(ident):
            if trace.get("error"):
                grouped.setdefault("model_error", []).append({"source": "llm_trace", "session_id": ident,
                    "seq": trace.get("seq"), "error": str(trace["error"])[:500]})
    # Existing evaluations keep their own stores; retain pointers and failures.
    from src.constants import DATA_DIR
    locations = [Path(DATA_DIR) / "daily_eval", Path(DATA_DIR) / "logs" / "daily_eval",
                 Path(__file__).resolve().parents[2] / "logs" / "daily_eval"]
    for folder in locations:
        for path in sorted(folder.glob("*.json"), reverse=True)[:10]:
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
                for row in report.get("tasks", report.get("results", [])):
                    failed = [c for c in row.get("checks", []) if c.get("ok") is False]
                    if failed:
                        grouped.setdefault("daily_evaluation", []).append({"source": path.name, "id": row.get("id"), "checks": failed})
            except (OSError, ValueError, AttributeError):
                continue
    return [{"type": key, "count": len(value), "evidence": value} for key, value in grouped.items()]


async def _model(url, model, messages, **kwargs):
    from src.llm_core import llm_call_async
    return await llm_call_async(url, model, messages, timeout=180, max_retries=0, workload="background", **kwargs)


def _route(spec, owner):
    if spec.endpoint_url:
        from src.endpoint_resolver import normalize_base, build_chat_url
        return build_chat_url(normalize_base(spec.endpoint_url)), spec.model, None
    from src.harness_refinement.probe import resolve_route
    url, model, headers = resolve_route(owner)
    if not url or not model: raise ValueError("No hay un modelo global configurado.")
    return url, model, headers


async def _evaluate_case(case, edits, spec, route, owner, model_call):
    url, model, headers = route
    started = time.monotonic()
    row = {"id": case.id, "group": case.group, "source": case.source, "score": None, "error": ""}
    try:
        if case.mode == "agent":
            from src.harness_refinement import rollout
            from src.agent_loop import stream_agent_loop
            async def loop(*args, **kwargs):
                kwargs["harness_options"] = {**kwargs.get("harness_options", {}), "runtime_config_trial": edits,
                                             "project_id": spec.project_id}
                async for event in stream_agent_loop(*args, **kwargs): yield event
            result = await rollout.run_rollout(case.id, "trial", case.messages or [{"role": "user", "content": case.prompt}],
                                              url=url, model=model, headers=headers, owner=owner, loop=loop)
            if result.get("error"): raise ValueError(result["error"])
            check_result = {"answer": result["text"], "tools": [s["tool"] for s in result["steps"] if s.get("ok")],
                            "seconds": result["seconds"], "cards": 0}
        else:
            if any(e["kind"] in ("tool_description", "tool_selection", "context") for e in edits):
                raise ValueError("Tool/context edits require agent-mode cases")
            _, temperature, tokens = runtime.options(edits, {}, 0, spec.max_tokens)
            messages = runtime.messages(edits, [{"role": "system", "content": spec.base_system}] +
                                        (case.messages or [{"role": "user", "content": case.prompt}]), owner)
            answer = await model_call(url, model, messages, temperature=temperature, max_tokens=tokens, headers=headers)
            check_result = {"answer": answer, "tools": [], "cards": 0, "seconds": time.monotonic() - started}
        from scripts.daily_eval import check
        checks = check(case.checks, check_result)
        row.update(answer=check_result["answer"], checks=checks, score=float(all(c["ok"] for c in checks)))
        if case.galton_checker:
            data = await galton_call(spec.galton_url, "answer_check", {"answer": check_result["answer"],
                                       "prompt": case.prompt, "reference": case.reference, "checker": case.galton_checker})
            row["judge"] = data
            if data.get("unavailable") or data.get("available") is False or not _valid_score(data.get("score")):
                raise ValueError("Galton no ha devuelto una puntuación disponible.")
            row["score"] = min(row["score"], data["score"])
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        row.update(error=f"{type(exc).__name__}: {exc}", score=None)
    row["seconds"] = time.monotonic() - started
    return row


async def evaluate_cases(spec, edits, route, owner, model_call):
    rows = []
    for repeat in range(spec.repeats):
        for case in spec.cases:
            row = await _evaluate_case(case, edits, spec, route, owner, model_call)
            row["case_id"] = row["id"]
            row["id"] += f":{repeat + 1}"
            rows.append(row)
    return rows


async def propose(spec, before, signals, route, model_call):
    url, model, headers = route
    schema = {"type": "object", "properties": {"candidates": {"type": "array", "items": {
        "type": "object", "properties": {"edit": runtime.ConfigEdit.model_json_schema(), "reason": {"type": "string"}},
        "required": ["edit", "reason"], "additionalProperties": False}}}, "required": ["candidates"]}
    prompt = (f"Propose {spec.candidates} distinct SINGLE small typed configuration edits. Target group: {spec.target_group}. "
              "Each must improve failures without harming other groups. No source code changes. "
              "Allowed system_section keys: response, reasoning, workflow, verification. "
              "Other kinds: model_parameter (temperature/max_tokens), tool_description (tool name), tool_selection (boolean), "
              "skill (skill name, boolean), context (no_memory/no_skills boolean, input_token_budget integer). "
              "Choose different hypotheses; do not bundle edits. Return JSON candidates with edit and reason.\n" +
              json.dumps({"base_system": spec.base_system, "results": before, "signals": signals,
                          "cases": [c.model_dump() for c in spec.cases]}, ensure_ascii=False))
    raw = await model_call(url, model, [{"role": "user", "content": prompt}], headers=headers,
                           temperature=.3, max_tokens=8192, response_schema=schema)
    from src.harness_refinement.proposer import _parse
    # Use the established tolerant JSON reader when the provider adds fences.
    parsed = _parse(raw)
    if (not isinstance(parsed, dict) or "candidates" not in parsed) and isinstance(raw, str):
        import re
        plain = re.sub(r"^```[a-zA-Z0-9_-]*\s*|\s*```$", "", raw.strip())
        try:
            array = json.loads(plain)
            if isinstance(array, list): parsed = {"candidates": array}
        except ValueError:
            pass
    if not isinstance(parsed, dict) or not isinstance(parsed.get("candidates"), list):
        raise ValueError(f"El modelo no ha devuelto candidatos tipados: {str(raw)[:500]!r}")
    return parsed["candidates"][:spec.candidates]


def create(spec: CycleSpec, owner, *, store=None):
    if not owner: raise ValueError("El ciclo necesita un propietario.")
    db = CycleStore(store)
    record = {"id": new_id("cycle"), "owner": owner, "name": spec.name, "spec": spec.model_dump(),
              "created_at": now_iso(), "status": "queued", "phase": "queued", "candidates": []}
    db.save(record)
    return record


async def run(ident, owner, *, store=None, model_call=None, proposer_fn=None):
    db = CycleStore(store)
    record = db.get(ident, owner)
    if not record: raise ValueError("No existe el ciclo.")
    # A durable claim makes double clicks and a scheduler overlap harmless.
    with db.harness._conn(immediate=True) as conn:
        row = conn.execute("SELECT payload FROM improvement_cycles WHERE id=? AND owner=?", (ident, owner)).fetchone()
        latest = json.loads(row[0])
        if latest["status"] != "queued": return latest
        latest.update(status="running", phase="baseline")
        conn.execute("UPDATE improvement_cycles SET payload=? WHERE id=?", (json.dumps(latest), ident))
        record = latest
    call = model_call or _model
    svc = HarnessEvolutionService(db.harness)
    spec = CycleSpec.model_validate(record["spec"])
    try:
        route = _route(spec, owner)
        record["model"] = {"url": route[0], "model": route[1]}
        record["signals"] = collect_signals(owner, spec.session_ids)
        for run_id in spec.galton_run_ids:
            if not spec.galton_url: raise ValueError("Falta la URL de evaluación de Galton.")
            failures = await galton_call(spec.galton_url, "run_results", {"run": run_id, "only_failed": True, "include_output": True})
            record["signals"].append({"type": "galton_evaluation", "run_id": run_id, "evidence": failures})
        base_revision = svc.active_revision()
        edits = runtime.snapshot(owner, spec.project_id, store=db.harness)
        before = await evaluate_cases(spec, edits, route, owner, call)
        record["baseline"] = before
        if any(r.get("error") for r in before):
            raise ValueError("La batería base no se ha podido completar; no se aplican ediciones.")
        record["phase"] = "proposing"
        db.save(record)
        candidates = await (proposer_fn or propose)(spec, before, record["signals"], route, call)
        for proposal in candidates:
            entry = {"proposal": proposal, "status": "evaluating"}
            record["candidates"].append(entry)
            try:
                edit = runtime.ConfigEdit.model_validate(proposal["edit"])
                changes = {"type": "runtime_config", "edit": edit.model_dump(), "owner": owner, "project_id": spec.project_id}
                patch = svc.propose_candidate(changes=changes, parent_revision=base_revision.revision_id,
                    scope=f"cycle:{ident}", source_trace_ids=spec.session_ids)
                entry.update(patch_id=patch.patch_id, high_risk=edit.high_risk)
                if patch.status == "rejected": raise ValueError(patch.trace)
                record["phase"] = "evaluating"
                db.save(record)
                after = await evaluate_cases(spec, runtime.with_edit(edits, edit.model_dump()), route, owner, call)
                comparison = gate(before, after, spec.target_group, spec.regression_threshold)
                patch.evaluation = {"before": before, "after": after, "target_group": spec.target_group,
                    "regression_threshold": spec.regression_threshold, "comparison": comparison,
                    "base_revision": base_revision.revision_id, "reason": proposal.get("reason", "")}
                patch.status = "evaluated" if comparison["eligible"] else "evaluated_failed"
                svc.store.update_patch(patch)
                entry.update(comparison=comparison, status="awaiting_person" if comparison["eligible"] and edit.high_risk else
                             "accepted" if comparison["eligible"] else "rejected")
                if comparison["eligible"] and not edit.high_risk:
                    svc.promote_candidate(patch.patch_id, actor="improvement-cycle", canary_runs=0)
                    # Next candidate is evaluated against the configuration now in use.
                    base_revision = svc.active_revision()
                    edits = runtime.snapshot(owner, spec.project_id, store=db.harness)
                    before = after
            except StaleParent:
                entry.update(status="stale", error="La configuración cambió durante la evaluación. Hay que lanzar otro ciclo.")
            except Exception as exc:
                entry.update(status="error", error=str(exc))
            db.save(record)
        record.update(status="done", phase="done", completed_at=now_iso())
    except asyncio.CancelledError:
        record.update(status="interrupted", phase="interrupted")
        raise
    except Exception as exc:
        record.update(status="error", phase="error", error=f"{type(exc).__name__}: {exc}")
    finally:
        db.save(record)
    return record


def candidate(ident, owner, *, store=None):
    svc = HarnessEvolutionService(store)
    patch = svc.get_patch(ident)
    if not patch or patch.change_type != "runtime_config" or patch.changes.get("owner") != owner:
        raise ValueError("No existe el candidato.")
    return svc, patch


def accept(ident, owner, *, store=None):
    svc, patch = candidate(ident, owner, store=store)
    # Existing promote_cas refuses stale evidence. No automatic rebase of a verdict.
    return svc.promote_candidate(ident, actor=owner, canary_runs=0, human_approved=True).to_dict()


async def scheduled_entry(owner: str, command: str = "", **kwargs):
    """The existing scheduler owns timing, leases, recurrence and cancellation."""
    spec = CycleSpec.model_validate_json(command)
    record = create(spec, owner)
    result = await run(record["id"], owner)
    return json.dumps({"cycle_id": result["id"], "status": result["status"]}), result["status"] == "done"


def revert(ident, owner, *, store=None):
    svc, patch = candidate(ident, owner, store=store)
    if patch.status != "promoted": raise ValueError("El candidato no está aplicado.")
    current = svc.active_revision()
    refs = copy.deepcopy(current.refs)
    key = runtime.slot(patch.changes)
    entry = refs.get("runtime_config", {}).get(key)
    if not entry or entry["patch_id"] != ident:
        raise ValueError("Otra edición ocupa este ajuste; revierte primero esa edición.")
    parent = svc.store.get_revision(patch.parent_revision)
    previous = parent.refs.get("runtime_config", {}).get(key)
    if previous is None: refs["runtime_config"].pop(key)
    else: refs["runtime_config"][key] = previous
    revision = HarnessRevision(new_id("rev"), current.revision_id, now_iso(), refs, "active", current.version + 1)
    patch.status, patch.trace = "reverted", f"Reverted individually by {owner} at {now_iso()}"
    committed = svc.store.promote_cas(parent_revision_id=current.revision_id, new_revision=revision, patch_state=patch)
    return committed.to_dict()
