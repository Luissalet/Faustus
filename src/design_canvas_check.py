# -*- coding: utf-8 -*-
"""Closing the loop on a design canvas (OBJ-47 / OBJ-30).

`design_canvas` writes the design before the code. That is half of the idea:
a design nobody goes back to is a document, not a check. This module is the
other half. When the work that followed the canvas finishes, the harness
compares what was done against what was designed and records, item by item,
whether it holds.

What is compared, and by whom:

* `structure` -- deterministic. Each file the design names is looked up in the
  files this work changed (and, failing that, on disk). No model involved.
* tests -- deterministic. The turn's own project-test result, nothing else.
* `requirements`, `operations`, `safeguards` -- one tool-less completion under
  a schema reads the diff and the test result and answers per item. A "met"
  without evidence in the answer is not accepted as "met".

Every item ends as `met`, `partial`, `not_met` or `unverified`. `unverified` is
a real answer and not a failure mode: when the verifier cannot run (no model,
timeout, malformed answer) the items say so instead of vanishing, because a
silent skip would look exactly like a pass.

Nothing here blocks a turn. The verdict is recorded where people look: the
turn's harness summary, a `harness_check` event, and the canvas itself (the
concept in the project graph gets a "Closure check" section). A canvas that
is not met says so loudly; it does not stop the answer from being delivered.

Persistence is one small JSON file per chat session holding the latest canvas
drafted there. The canvas tool arms it (`register`); the end of the turn
(`close_turn`) reads it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional

from src import design_canvas
from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

STORE_DIR = os.path.join(DATA_DIR, "design_canvas_checks")

VERDICTS = ("met", "partial", "not_met", "unverified")

#: Dimensions whose entries a model has to judge: they are statements about
#: behaviour, which no path lookup can settle.
JUDGED_KINDS = ("requirements", "operations", "safeguards")
MAX_JUDGED_PER_KIND = 8
MAX_DIFF_CHARS = 14_000
JUDGE_TIMEOUT_S = 150.0
#: A "met" needs more than a word behind it.
MIN_EVIDENCE_CHARS = 8

#: A `structure` entry that says the file goes away rather than appears. The
#: verb has to sit right before the path ("delete src/old.py") or right after
#: it ("src/old.py: remove"): "src/ui.py: add a delete button" is not one.
_REMOVE_VERBS = r"(?:remov\w*|delet\w*|drop\w*|retir\w*|deprecat\w*|elimin\w*|borr\w*|quit\w*)"
_REMOVAL_BEFORE = re.compile(r"^\W*" + _REMOVE_VERBS + r"\W+$", re.IGNORECASE)
_REMOVAL_AFTER = re.compile(r"^[\s:,;()\-–—]*(?:to be |will be |is |are )?" + _REMOVE_VERBS + r"\b", re.IGNORECASE)


def _is_removal(entry: str, path: str) -> bool:
    if path not in entry:
        return False
    head, _, tail = entry.partition(path)
    return bool(_REMOVAL_BEFORE.match(head) or _REMOVAL_AFTER.match(tail))


_SECTION_RE = re.compile(r"\n*## (?:Closure check|Comprobación de cierre)[^\n]*\n.*\Z", re.DOTALL)

_WORDS = {
    "en": {
        "met": "met", "partial": "partially met", "not_met": "not met", "unverified": "unverified",
        "title": "Design canvas", "tests": "Project tests", "closure": "Closure check",
        "changed": "changed in this work", "exists_untouched": "exists, but nothing in this work changed it",
        "missing": "does not exist and was not written", "no_workspace": "no workspace to look in",
        "removed": "the file is gone, as designed", "still_there": "the design removes it and it is still there",
        "tests_ok": "the project tests passed", "tests_failed": "the project tests failed",
        "tests_pre": "the only failures were already there before this work",
        "tests_none": "no project tests ran for this work",
        "tests_inconclusive": "the test run was inconclusive",
        "no_judge": "the verifier could not run", "no_answer": "the verifier did not answer this item",
        "no_evidence": "the verifier gave no evidence for this verdict",
        "no_changes": "no file was changed, so there is nothing to compare",
        "summary": "{met} met, {partial} partial, {not_met} not met, {unverified} unverified",
    },
    "es": {
        "met": "cumplido", "partial": "cumplido en parte", "not_met": "no cumplido", "unverified": "sin verificar",
        "title": "Canvas de diseño", "tests": "Pruebas del proyecto", "closure": "Comprobación de cierre",
        "changed": "modificado en este trabajo", "exists_untouched": "existe, pero este trabajo no lo tocó",
        "missing": "no existe y no se escribió", "no_workspace": "no hay carpeta de trabajo donde mirar",
        "removed": "el fichero ya no está, como diseñado", "still_there": "el diseño lo elimina y sigue ahí",
        "tests_ok": "las pruebas del proyecto pasan", "tests_failed": "las pruebas del proyecto fallan",
        "tests_pre": "los únicos fallos ya estaban antes de este trabajo",
        "tests_none": "no se ejecutaron pruebas del proyecto para este trabajo",
        "tests_inconclusive": "la ejecución de pruebas no fue concluyente",
        "no_judge": "el verificador no pudo ejecutarse", "no_answer": "el verificador no respondió a este punto",
        "no_evidence": "el verificador no dio evidencia de este veredicto",
        "no_changes": "no se cambió ningún fichero, así que no hay nada que comparar",
        "summary": "{met} cumplidos, {partial} en parte, {not_met} no cumplidos, {unverified} sin verificar",
    },
}


def _w(language: str, key: str) -> str:
    return _WORDS["es" if language == "es" else "en"][key]


def _setting(key: str, default: Any = None) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:  # noqa: BLE001
        return default


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm(path: str) -> str:
    return str(path or "").replace("\\", "/").strip().lstrip("./").lower()


# ---------------------------------------------------------------------------
# Persistence: the latest canvas drafted in a chat session
# ---------------------------------------------------------------------------

def _path(session_id: str) -> str:
    digest = hashlib.sha1(str(session_id).encode("utf-8")).hexdigest()[:20]
    return os.path.join(STORE_DIR, f"{digest}.json")


def load(session_id: str) -> Optional[Dict[str, Any]]:
    if not session_id:
        return None
    try:
        with open(_path(session_id), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("canvas"), dict) else None


def save(record: Dict[str, Any]) -> bool:
    sid = str(record.get("session_id") or "")
    if not sid:
        return False
    try:
        os.makedirs(STORE_DIR, exist_ok=True)
        path = _path(sid)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except OSError as exc:
        logger.debug("[design_canvas_check] could not persist: %s", exc)
        return False


def register(session_id: str, *, goal: str, canvas: Dict[str, Any],
             concept_id: Optional[str] = None, project_id: Optional[str] = None,
             workspace: Optional[str] = None, turn_id: Optional[str] = None) -> bool:
    """Arm the closure check with a freshly drafted canvas.

    A new canvas replaces the previous one of the session: the closure is
    about the task now in hand, and an older design is history.
    """
    if not session_id or not isinstance(canvas, dict):
        return False
    return save({
        "version": 1,
        "session_id": str(session_id),
        "goal": str(goal or "").strip()[:1000],
        "canvas": canvas,
        "concept_id": concept_id or None,
        "project_id": project_id or None,
        "workspace": workspace or None,
        "turn_id": str(turn_id or ""),
        "drafted_at": _now(),
        "touched": [],
        "status": "open",
        "checks": 0,
        "verdict": None,
    })


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------

def build_items(canvas: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The checkable entries of a canvas, each with a stable id."""
    items: List[Dict[str, Any]] = []
    for kind in JUDGED_KINDS:
        entries = [str(e) for e in (canvas.get(kind) or [])][:MAX_JUDGED_PER_KIND]
        for n, text in enumerate(entries, 1):
            items.append({"id": f"{kind}.{n}", "kind": kind, "text": text})
    entries = [str(e) for e in (canvas.get("structure") or [])]
    for n, path in enumerate(design_canvas.referenced_paths(canvas), 1):
        entry = next((e for e in entries if path in e), "")
        items.append({"id": f"structure.{n}", "kind": "structure", "text": path,
                      "removal": _is_removal(entry, path)})
    return items


def check_structure(items: List[Dict[str, Any]], touched: Iterable[str],
                    workspace: Optional[str], language: str = "en") -> None:
    """Fill in the verdict of every `structure` item, in place."""
    touched_n = [_norm(p) for p in touched if p]
    for item in items:
        if item["kind"] != "structure":
            continue
        want = _norm(item["text"])
        removal = bool(item.get("removal"))
        was_touched = any(t == want or t.endswith("/" + want) or want.endswith("/" + t) for t in touched_n)
        if not workspace:
            if was_touched and not removal:
                item["verdict"], item["evidence"] = "met", _w(language, "changed")
            else:
                item["verdict"], item["evidence"] = "unverified", _w(language, "no_workspace")
            continue
        exists = os.path.isfile(os.path.join(workspace, item["text"].replace("/", os.sep)))
        if removal:
            # The design says the file goes away: absence is the goal.
            if exists:
                item["verdict"], item["evidence"] = "not_met", _w(language, "still_there")
            else:
                item["verdict"], item["evidence"] = "met", _w(language, "removed")
        elif was_touched:
            item["verdict"], item["evidence"] = "met", _w(language, "changed")
        elif exists:
            item["verdict"], item["evidence"] = "partial", _w(language, "exists_untouched")
        else:
            item["verdict"], item["evidence"] = "not_met", _w(language, "missing")


def tests_item(tests: Optional[Dict[str, Any]], language: str = "en") -> Dict[str, Any]:
    """The turn's own project-test result as one item."""
    item = {"id": "tests.1", "kind": "tests", "text": _w(language, "tests")}
    if not isinstance(tests, dict) or not tests.get("ran"):
        item["verdict"], item["evidence"] = "unverified", _w(language, "tests_none")
        return item
    summary = str(tests.get("summary") or "").strip()
    suffix = f" ({summary})" if summary else ""
    if tests.get("inconclusive"):
        item["verdict"], item["evidence"] = "unverified", _w(language, "tests_inconclusive") + suffix
    elif tests.get("ok") is True:
        item["verdict"], item["evidence"] = "met", _w(language, "tests_ok") + suffix
    elif tests.get("pre_existing_only"):
        item["verdict"], item["evidence"] = "partial", _w(language, "tests_pre") + suffix
    else:
        item["verdict"], item["evidence"] = "not_met", _w(language, "tests_failed") + suffix
    return item


# ---------------------------------------------------------------------------
# The judge: one completion under a schema
# ---------------------------------------------------------------------------

def judge_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "verdict": {"type": "string", "enum": list(VERDICTS)},
                "evidence": {"type": "string"},
            },
            "required": ["id", "verdict", "evidence"],
            "additionalProperties": False,
        }}},
        "required": ["items"],
        "additionalProperties": False,
    }


def build_judge_messages(goal: str, items: List[Dict[str, Any]], *, user_text: str,
                         changed: List[str], tests: Optional[Dict[str, Any]],
                         diff: str) -> List[Dict[str, str]]:
    listing = "\n".join(f"- {i['id']} [{i['kind']}]: {i['text']}" for i in items)
    system = (
        "You compare finished work with the design that was written before it. "
        "For every numbered item answer a verdict and one short sentence of evidence.\n"
        "Verdicts: met = the diff or the test result shows it; partial = some of it is "
        "there; not_met = the work contradicts it or leaves it out; unverified = the "
        "material you were given cannot settle it.\n"
        "Rules: never answer met unless the evidence names a file, a change or a test "
        "you can see below. An item the diff does not mention is partial or unverified, "
        "not met. Answer with a JSON object {\"items\": [{\"id\", \"verdict\", \"evidence\"}]} "
        "covering every id, and nothing else."
    )
    tests_line = ""
    if isinstance(tests, dict) and tests.get("ran"):
        tests_line = (f"\n\nProject tests: ok={tests.get('ok')} "
                      f"{str(tests.get('summary') or '')[:300]}")
    user = (
        f"The design goal:\n{goal}\n\nThe user's request for this work:\n{(user_text or '')[:1200]}\n\n"
        f"Items to judge:\n{listing}\n\nFiles changed: {', '.join(changed[:40]) or '(none)'}"
        f"{tests_line}\n\nDiff of the work:\n{diff[:MAX_DIFF_CHARS] or '(no diff available)'}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _first_object(text: str) -> Optional[Dict[str, Any]]:
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except ValueError:
        pass
    start = raw.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for idx in range(start, len(raw)):
            ch = raw[idx]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(raw[start:idx + 1])
                        return data if isinstance(data, dict) else None
                    except ValueError:
                        break
        start = raw.find("{", start + 1)
    return None


def apply_judgement(items: List[Dict[str, Any]], raw: str, language: str = "en") -> None:
    """Merge the judge's answer into the items it covered, in place."""
    data = _first_object(raw) or {}
    answers = {}
    for entry in data.get("items") or []:
        if isinstance(entry, dict) and entry.get("id"):
            answers[str(entry["id"])] = entry
    for item in items:
        if item["kind"] not in JUDGED_KINDS:
            continue
        entry = answers.get(item["id"])
        if not entry:
            item["verdict"], item["evidence"] = "unverified", _w(language, "no_answer")
            continue
        verdict = str(entry.get("verdict") or "").strip().lower()
        evidence = " ".join(str(entry.get("evidence") or "").split())[:400]
        if verdict not in VERDICTS:
            item["verdict"], item["evidence"] = "unverified", _w(language, "no_answer")
        elif verdict == "met" and len(evidence) < MIN_EVIDENCE_CHARS:
            item["verdict"], item["evidence"] = "unverified", _w(language, "no_evidence")
        else:
            item["verdict"], item["evidence"] = verdict, evidence


def mark_unjudged(items: List[Dict[str, Any]], reason: str, language: str = "en") -> None:
    for item in items:
        if item["kind"] in JUDGED_KINDS and "verdict" not in item:
            item["verdict"] = "unverified"
            item["evidence"] = f"{_w(language, 'no_judge')}: {reason}"[:300]


async def _default_model_call(messages: List[Dict[str, str]], schema: Dict[str, Any], *,
                              endpoint_url: str, model: str, headers: Optional[Dict],
                              workload: str, timeout_s: float) -> str:
    from src.llm_core import llm_call_async
    raw = await asyncio.wait_for(
        llm_call_async(
            url=endpoint_url, model=model, messages=messages, headers=headers,
            temperature=0.1, max_tokens=1400, timeout=int(timeout_s), max_retries=1,
            workload=workload or "foreground", response_schema=schema,
        ),
        timeout=timeout_s + 30,
    )
    return raw[0] if isinstance(raw, tuple) else raw


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

def overall(items: List[Dict[str, Any]]) -> str:
    # Project tests are a side signal: a design with no test run behind it is
    # not "partial" for that reason alone. Every other unverified item is.
    counted = [i for i in items if not (i.get("kind") == "tests" and i.get("verdict") == "unverified")]
    scored = [i["verdict"] for i in counted if i.get("verdict") != "unverified"]
    if not scored:
        return "unverified"
    if all(v == "met" for v in scored):
        return "met" if len(scored) == len(counted) else "partial"
    if all(v == "not_met" for v in scored):
        return "not_met"
    return "partial"


def counts(items: List[Dict[str, Any]]) -> Dict[str, int]:
    out = {v: 0 for v in VERDICTS}
    for item in items:
        out[item.get("verdict") if item.get("verdict") in out else "unverified"] += 1
    return out


def verdict_line(verdict: Dict[str, Any], language: str = "en") -> str:
    c = verdict.get("counts") or {}
    return f"{_w(language, 'title')}: {_w(language, verdict.get('overall') or 'unverified')} — " + \
        _w(language, "summary").format(**{k: c.get(k, 0) for k in VERDICTS})


def render_markdown(verdict: Dict[str, Any], language: str = "en") -> str:
    lines = [f"## {_w(language, 'closure')} — {str(verdict.get('checked_at') or '')[:10]}", "",
             verdict_line(verdict, language), ""]
    for item in verdict.get("items") or []:
        lines.append(f"- **{_w(language, item.get('verdict') or 'unverified')}** "
                     f"({item.get('kind')}) {item.get('text')}")
        if item.get("evidence"):
            lines.append(f"  - {item['evidence']}")
    return "\n".join(lines).rstrip() + "\n"


def write_back(record: Dict[str, Any], language: str = "en") -> bool:
    """Append the verdict to the canvas concept so the canvas view shows it."""
    concept_id = record.get("concept_id")
    verdict = record.get("verdict")
    if not concept_id or not isinstance(verdict, dict):
        return False
    try:
        from src.project_concepts import _store
        store = _store(project_id=record.get("project_id"), workspace=record.get("workspace"))
        concept = store.get_concept(concept_id)
        if not concept:
            return False
        base = _SECTION_RE.sub("", str(concept.get("details") or "")).rstrip()
        store.upsert_concept(
            name=concept["name"], kind=concept["kind"], summary=concept.get("summary") or "",
            details=base + "\n\n" + render_markdown(verdict, language),
            refs=concept.get("refs") or [], parent_id=concept.get("parent_id"),
            concept_id=concept_id, embed=False,
        )
        return True
    except Exception as exc:  # noqa: BLE001 -- the verdict is already recorded elsewhere
        logger.debug("[design_canvas_check] concept write-back failed: %s", exc)
        return False


def compact(record: Optional[Dict[str, Any]], language: str = "en") -> Optional[Dict[str, Any]]:
    """What goes to the UI and into the turn summary."""
    if not record or not isinstance(record.get("verdict"), dict):
        return None
    v = record["verdict"]
    return {
        "overall": v.get("overall"),
        "counts": v.get("counts"),
        "title": _w(language, "title"),
        "label": verdict_line(v, language),
        "goal": record.get("goal"),
        "concept_id": record.get("concept_id"),
        "checked_at": v.get("checked_at"),
        "judge_error": v.get("judge_error"),
        "items": [
            {"id": i.get("id"), "kind": i.get("kind"), "text": i.get("text"),
             "verdict": i.get("verdict"), "verdict_label": _w(language, i.get("verdict") or "unverified"),
             "evidence": i.get("evidence")}
            for i in v.get("items") or []
        ],
    }


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------

async def check(record: Dict[str, Any], *, changed: List[str], tests: Optional[Dict[str, Any]],
                user_text: str = "", diff: str = "", language: str = "en",
                model_call: Optional[Callable[..., Awaitable[str]]] = None,
                workspace: Optional[str] = None) -> Dict[str, Any]:
    """Compare the work with the canvas of `record` and return the verdict.

    Never raises: a verifier that cannot run reports `unverified` items.
    """
    canvas = record.get("canvas") or {}
    items = build_items(canvas)
    touched = sorted(set(record.get("touched") or []) | set(changed))
    check_structure(items, touched, workspace or record.get("workspace"), language)
    judged = [i for i in items if i["kind"] in JUDGED_KINDS]
    judge_error = None
    if judged:
        if not changed:
            mark_unjudged(items, _w(language, "no_changes"), language)
        else:
            try:
                messages = build_judge_messages(
                    str(record.get("goal") or ""), judged, user_text=user_text,
                    changed=touched, tests=tests, diff=diff)
                raw = await model_call(messages, judge_schema()) if model_call else ""
                if not str(raw or "").strip():
                    raise RuntimeError("the verifier returned an empty answer")
                apply_judgement(items, raw, language)
            except Exception as exc:  # noqa: BLE001
                judge_error = f"{type(exc).__name__}: {exc}"[:240]
                logger.warning("[design_canvas_check] verifier failed: %s", judge_error)
                mark_unjudged(items, judge_error, language)
    items.append(tests_item(tests, language))
    for item in items:
        item.setdefault("verdict", "unverified")
        item.setdefault("evidence", "")
    return {
        "overall": overall(items), "counts": counts(items), "items": items,
        "checked_at": _now(), "judge_error": judge_error,
    }


async def close_turn(ledger: Any, *, session_id: str, turn_id: Optional[str] = None,
                     endpoint_url: Optional[str] = None, model: Optional[str] = None,
                     headers: Optional[Dict] = None, workload: str = "foreground",
                     model_call: Optional[Callable[..., Awaitable[str]]] = None,
                     diff_text: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The end-of-turn hook: look up the session's canvas and, when this turn
    changed files, check the work against it. Returns the compact verdict (for
    the `harness_check` event) or None when there is nothing to report. Never
    raises and never blocks the turn."""
    try:
        if not _setting("agent_design_canvas_closure", True):
            return None
        record = load(session_id)
        if not record:
            return None
        changed = [p for p in (ledger.mutated_paths() or []) if p]
        if not changed:
            return None   # designed, not built (yet): nothing to compare
        language = getattr(ledger, "language", "en") or "en"
        ledger.canvas_check_runs = int(getattr(ledger, "canvas_check_runs", 0)) + 1
        ledger.canvas_check_mutations_at_run = len(getattr(ledger, "mutations", []) or [])

        if model_call is None and endpoint_url and model:
            async def model_call(messages, schema, _u=endpoint_url, _m=model, _h=headers, _w_=workload):  # noqa: E306
                return await _default_model_call(messages, schema, endpoint_url=_u, model=_m,
                                                 headers=_h, workload=_w_, timeout_s=JUDGE_TIMEOUT_S)
        diff = diff_text
        if diff is None:
            try:
                from src import auto_review
                cp = getattr(ledger, "checkpoint", None)
                sha = cp.get("sha") if isinstance(cp, dict) else None
                diff = await asyncio.to_thread(
                    auto_review.turn_diff, ledger.workspace or record.get("workspace") or "", changed, sha,
                    MAX_DIFF_CHARS)
                diff = (diff or {}).get("diff") or ""
            except Exception:  # noqa: BLE001
                diff = ""
        verdict = await check(
            record, changed=changed, tests=getattr(ledger, "tests", None),
            user_text=getattr(ledger, "user_text", "") or "", diff=diff or "",
            language=language, model_call=model_call, workspace=ledger.workspace or None)
        record["touched"] = sorted(set(record.get("touched") or []) | set(changed))
        record["verdict"] = verdict
        record["status"] = "checked"
        record["checks"] = int(record.get("checks") or 0) + 1
        record["last_turn_id"] = str(turn_id or "")
        save(record)
        write_back(record, language)
        out = compact(record, language)
        ledger.canvas_check = out
        if verdict["overall"] in ("partial", "not_met"):
            note = f"canvas_{verdict['overall']}:{verdict['counts'].get('not_met', 0)}"
            if note not in ledger.notes:
                ledger.notes.append(note)
        return out
    except Exception as exc:  # noqa: BLE001 -- closing the loop must never break a turn
        logger.warning("[design_canvas_check] closure failed: %s", exc, exc_info=True)
        return None
