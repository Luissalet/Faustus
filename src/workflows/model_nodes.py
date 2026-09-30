"""
workflows/model_nodes.py — the node types that use a model.

`agent`, `classify`, `extract` and `guard` are where a workflow stops being
plumbing and starts deciding things, so every rule in `handlers.py` applies here
harder, not softer:

**Nothing is capable by default.** Each handler is built from a seam — an agent
runner, a `ModelCalls` — and with nothing wired it refuses by name, like
`deliver` with no sender. A `classify` that "chose" a branch with no model
behind it would send every ticket down the first edge and record that it was
sure.

**A model's answer is never trusted as a shape.** Structured output is parsed,
checked against the node's schema, and — once — sent back to the model with the
exact errors (`repair`). After that it is a failed node, with the errors in the
reason, not a result that looks complete and is not.

**A decision carries its receipt.** `classify` records the options it chose
between, the choice, the confidence, whether the fallback fired and which model
answered. A routing decision nobody can audit is the one that gets blamed.

**Effects are claimed the way `deliver` claims them.** An `agent` turn may call
tools, so it runs behind the same idempotency key and `mark_effect` protocol a
`skill` does: the turn is marked pending before it starts and confirmed when it
returns; a worker that dies in between leaves `unknown_effect`, never a silent
second turn. `classify`, `extract` and `guard` only read, so a retry is merely
another model call.
"""
from __future__ import annotations

import json
import logging
import math
import re
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from src.contracts.workflow import WorkflowNode

from . import schema_check
from .handlers import _check_fenced
from .model_calls import ModelCalls, ModelUnavailable
from .templating import TemplateError, render

logger = logging.getLogger(__name__)

__all__ = ["agent_handler", "classify_handler", "extract_handler", "guard_handler",
           "model_node_handlers"]

_MAX_PROMPT_CHARS = 100_000


def _failed(reason: str, **extra: Any) -> Dict[str, Any]:
    return {"status": "failed", "reason": reason, **extra}


def _number(config: Mapping[str, Any], key: str, default: float, low: float, high: float) -> Tuple[float, str]:
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return default, f"config.{key} must be a number"
    if value < low or value > high:
        return default, f"config.{key} must be between {low:g} and {high:g}"
    return float(value), ""


def _whole_number(config: Mapping[str, Any], key: str, default: int, low: int, high: int) -> Tuple[int, str]:
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        return default, f"config.{key} must be a whole number"
    if value < low or value > high:
        return default, f"config.{key} must be between {low} and {high}"
    return value, ""


def _text_config(config: Mapping[str, Any], key: str, *, required: bool = True,
                 limit: int = _MAX_PROMPT_CHARS) -> Tuple[str, str]:
    value = config.get(key)
    if value is None or value == "":
        return "", (f"a model node needs `config.{key}`" if required else "")
    if not isinstance(value, str):
        return "", f"config.{key} must be text"
    if len(value) > limit:
        return "", f"config.{key} is longer than {limit} characters"
    return value, ""


def _mark(context: Mapping[str, Any], state: str) -> bool:
    marker = context.get("mark_effect")
    if not callable(marker):
        return True
    try:
        return bool(marker(state))
    except Exception:  # noqa: BLE001 - a bookkeeping hiccup is not proof nothing happened
        logger.exception("mark_effect(%s) raised", state)
        return False


# ── structured output: parse, check, repair once ─────────────────────────

_REPAIR_SYSTEM = (
    "You repair JSON. You are given a schema, a task and an answer that does not "
    "satisfy the schema, with the exact problems found. Reply with ONE corrected "
    "JSON value that satisfies the schema and nothing else: no prose, no code fence."
)


def structured(text: str, schema: Mapping[str, Any], *, models: Optional[ModelCalls],
               owner: str, purpose: str, task: str, timeout_s: float) -> Dict[str, Any]:
    """`{"ok": True, "data", "repaired"}` or `{"ok": False, "errors": [...]}`.

    One repair retry, never more: a model that cannot fix its own JSON when
    shown the errors is not going to on the third try, and every extra call is
    spent on the owner's GPU."""
    value, why = schema_check.extract_json(text)
    errors = [why] if why else schema_check.validate(value, schema)
    if not errors:
        return {"ok": True, "data": value, "repaired": False}
    first_errors = list(errors)
    if models is None:
        return {"ok": False, "errors": first_errors, "repair": "no model is wired to repair it"}
    prompt = ("Schema:\n" + json.dumps(schema, ensure_ascii=False, sort_keys=True) +
              "\n\nTask:\n" + task[:4000] +
              "\n\nAnswer to fix:\n" + str(text)[:8000] +
              "\n\nProblems:\n- " + "\n- ".join(first_errors[:12]))
    try:
        fixed = models.complete(
            [{"role": "system", "content": _REPAIR_SYSTEM}, {"role": "user", "content": prompt}],
            owner=owner, purpose=purpose, timeout_s=timeout_s, max_tokens=1500, temperature=0.0)
    except ModelUnavailable as exc:
        return {"ok": False, "errors": first_errors, "repair": f"the repair call failed: {exc}"}
    value, why = schema_check.extract_json(fixed)
    errors = [why] if why else schema_check.validate(value, schema)
    if errors:
        return {"ok": False, "errors": errors, "first_errors": first_errors, "repair": "tried once"}
    return {"ok": True, "data": value, "repaired": True, "first_errors": first_errors}


def _schema_config(config: Mapping[str, Any], key: str = "output_schema",
                   required: bool = False) -> Tuple[Optional[Mapping[str, Any]], str]:
    schema = config.get(key)
    if schema is None:
        return None, (f"a model node needs `config.{key}`" if required else "")
    problems = schema_check.schema_problems(schema)
    if problems:
        return None, f"config.{key} cannot be enforced: " + "; ".join(problems[:4])
    return schema, ""


# ── agent ────────────────────────────────────────────────────────────────

def agent_handler(run_turn: Optional[Callable[[Dict[str, Any]], Mapping[str, Any]]] = None, *,
                  models: Optional[ModelCalls] = None) -> Callable:
    """`agent`: one Faustus agent turn as a workflow step.

    Config: `prompt` (template, required), `system` (template), `agent` (profile
    slug), `tools` (allowlist; `[]` = think only, absent = the worker default),
    `output_schema` (JSON schema; checked after the turn, one repair), `timeout_s`
    (default 300), `max_rounds` (default 8)."""

    def handle(node: WorkflowNode, context: Mapping[str, Any]) -> Dict[str, Any]:
        config = node.config or {}
        prompt_template, problem = _text_config(config, "prompt")
        if problem:
            return _failed(problem)
        system_template, problem = _text_config(config, "system", required=False)
        if problem:
            return _failed(problem)
        timeout_s, problem = _number(config, "timeout_s", 300, 5, 3600)
        if problem:
            return _failed(problem)
        max_rounds, problem = _whole_number(config, "max_rounds", 8, 1, 50)
        if problem:
            return _failed(problem)
        tools = config.get("tools")
        if tools is not None and (not isinstance(tools, (list, tuple))
                                  or any(not isinstance(t, str) or not t.strip() for t in tools)):
            return _failed("config.tools must be a list of tool names")
        agent = config.get("agent")
        if agent is not None and (not isinstance(agent, str) or not agent.strip()):
            return _failed("config.agent must name an agent profile")
        schema, problem = _schema_config(config)
        if problem:
            return _failed(problem)
        if run_turn is None:
            return _failed(
                "no agent runner is wired to the 'agent' node type; nothing ran. Pass one to "
                "default_handlers(agent=...) — Faustus will not pretend an agent answered")
        try:
            prompt = render(prompt_template, context)
            system = render(system_template, context) if system_template else ""
        except TemplateError as exc:
            return _failed(str(exc))
        if not prompt.strip():
            return _failed("the prompt rendered to nothing")

        fenced = _check_fenced(context)
        if fenced is not None:
            return fenced

        owner = str(context.get("owner") or "")
        spec = {
            "prompt": prompt, "system": system, "agent": (agent or "").strip(),
            "tools": list(tools) if tools is not None else None,
            "max_rounds": max_rounds, "timeout_s": timeout_s,
            "owner": owner, "project_id": str(context.get("project_id") or ""),
            "run_id": str(context.get("run_id") or ""), "node_id": node.id,
            "idempotency_key": str(context.get("idempotency_key") or ""),
            "cancel_requested": context.get("cancel_requested"),
            "begin_effect": lambda: _mark(context, "pending"),
        }
        if isinstance(config.get("model"), str) and config["model"].strip():
            spec["model"] = config["model"].strip()
        try:
            turn = dict(run_turn(spec) or {})
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ == "AgentTurnUnavailable":
                # Raised before the turn began: nothing ran, nothing to doubt.
                return _failed(f"the agent turn could not start: {exc}")
            raise                                # the engine records the uncertainty

        text = str(turn.get("text") or "")
        stop = str(turn.get("stop_reason") or "")
        if stop in ("timeout", "cancelled") or turn.get("error") or int(turn.get("approvals_requested") or 0):
            # The turn began and did not end cleanly, so what its tools did is
            # not known: the effect stays `pending` and the engine withholds
            # any automatic retry.
            if int(turn.get("approvals_requested") or 0):
                reason = ("the agent asked for an approval a workflow cannot answer mid-turn; "
                          "remove that tool from `tools`, or do the step in a `skill`/`deliver` "
                          "node behind a `human_approval`")
            elif stop == "timeout":
                reason = f"the agent turn did not finish within {timeout_s:g}s"
            elif stop == "cancelled":
                reason = "the agent turn was stopped because the workflow lost its claim"
            else:
                reason = f"the agent turn failed: {turn.get('error')}"
            return _failed(reason, text=text[:2000], rounds=turn.get("rounds", 0),
                           tool_calls=turn.get("tool_calls", 0), stop_reason=stop or "error")
        _mark(context, "confirmed")
        if not text.strip():
            return _failed("the agent finished without an answer", rounds=turn.get("rounds", 0),
                           tool_calls=turn.get("tool_calls", 0))

        result: Dict[str, Any] = {
            "text": text, "rounds": int(turn.get("rounds") or 0),
            "tool_calls": int(turn.get("tool_calls") or 0),
            "model": str(turn.get("model") or ""), "profile": str(turn.get("profile") or ""),
            "session_id": str(turn.get("session_id") or ""),
            "tool_events": list(turn.get("tool_events") or [])[:40],
            "stop_reason": stop or "completed",
            "idempotency_key": str(context.get("idempotency_key") or ""),
        }
        if schema is not None:
            verdict = structured(text, schema, models=models, owner=owner,
                                 purpose=str(config.get("purpose") or "utility"),
                                 task=prompt, timeout_s=min(timeout_s, 120.0))
            if not verdict["ok"]:
                return _failed("the agent's answer does not satisfy `output_schema`: "
                               + "; ".join(verdict["errors"][:6])
                               + (f" ({verdict['repair']})" if verdict.get("repair") else ""),
                               **result)
            result["data"] = verdict["data"]
            result["repaired"] = bool(verdict["repaired"])
        return result

    return handle


# ── classify ─────────────────────────────────────────────────────────────

DEFAULT_THRESHOLD = 0.7
MAX_LABELS = 20
_LABEL_RE = re.compile(r"[^A-Za-z0-9]+")


def _labels(config: Mapping[str, Any]) -> Tuple[List[Tuple[str, str]], str]:
    raw = config.get("labels")
    if not isinstance(raw, (list, tuple)):
        return [], "a classify node needs `config.labels`, a list of at least two labels"
    out: List[Tuple[str, str]] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            out.append((item.strip(), ""))
        elif isinstance(item, Mapping) and isinstance(item.get("name"), str) and item["name"].strip():
            out.append((item["name"].strip(), str(item.get("description") or "").strip()))
        else:
            return [], "each label is a non-empty string or {name, description}"
    names = [n for n, _ in out]
    if len(names) < 2:
        return [], "a classify node needs at least two labels to choose between"
    if len(names) > MAX_LABELS:
        return [], f"a classify node takes at most {MAX_LABELS} labels"
    if len(set(names)) != len(names):
        return [], "labels must be unique"
    return out, ""


def _match_label(reply: str, names: Sequence[str]) -> Optional[str]:
    """The one label a free-text answer names, or None when it names none or
    several. Exact (case/punctuation-insensitive) first, then as a whole word —
    never a guess between two."""
    flat = _LABEL_RE.sub(" ", str(reply or "")).strip().lower()
    if not flat:
        return None
    for name in names:
        if flat == _LABEL_RE.sub(" ", name).strip().lower():
            return name
    hits = [n for n in names
            if re.search(rf"(?<![a-z0-9]){re.escape(_LABEL_RE.sub(' ', n).strip().lower())}(?![a-z0-9])", flat)]
    return hits[0] if len(hits) == 1 else None


def classify_handler(models: Optional[ModelCalls] = None) -> Callable:
    """`classify`: route to exactly one branch among declared labels.

    Config: `text` (template; what to classify), `labels` (2–20, strings or
    `{name, description}`), `question`, `instructions`, `threshold` (default
    0.7), `fallback` (a label), `on_uncertain` (`fallback` | `ask`; default
    `fallback` when a fallback is set, else `ask`), `purpose`, `timeout_s`.

    The choice is one token read as a probability (`typed_decision`), so it
    comes with a confidence. Below `threshold` — or when the server gave no
    usable answer — the node either takes `fallback`, or asks the model once
    in plain words; if that settles nothing and there is a `fallback`, it is
    used, otherwise the node fails rather than pick a branch at random. The
    outcome, with its receipt, is `{branch, label, receipt}`; downstream nodes
    opt into a label with `branch: {this_node: label}`."""

    def handle(node: WorkflowNode, context: Mapping[str, Any]) -> Dict[str, Any]:
        config = node.config or {}
        labels, problem = _labels(config)
        if problem:
            return _failed(problem)
        names = [n for n, _ in labels]
        text_template, problem = _text_config(config, "text")
        if problem:
            return _failed(problem)
        question, problem = _text_config(config, "question", required=False, limit=500)
        if problem:
            return _failed(problem)
        instructions, problem = _text_config(config, "instructions", required=False, limit=2000)
        if problem:
            return _failed(problem)
        threshold, problem = _number(config, "threshold", DEFAULT_THRESHOLD, 0.0, 1.0)
        if problem:
            return _failed(problem)
        timeout_s, problem = _number(config, "timeout_s", 30, 1, 600)
        if problem:
            return _failed(problem)
        fallback = config.get("fallback")
        if fallback is not None and fallback not in names:
            return _failed(f"config.fallback {fallback!r} is not one of the labels {names}")
        mode = config.get("on_uncertain") or ("fallback" if fallback else "ask")
        if mode not in ("fallback", "ask"):
            return _failed("config.on_uncertain must be 'fallback' or 'ask'")
        if mode == "fallback" and not fallback:
            return _failed("config.on_uncertain is 'fallback' but no `config.fallback` label is set")
        if models is None:
            return _failed(
                "no model is wired to the 'classify' node type; no branch was chosen. Pass "
                "`models=` to default_handlers — Faustus will not route on a guess")
        try:
            text = render(text_template, context)
        except TemplateError as exc:
            return _failed(str(exc))
        if not text.strip():
            return _failed("there is nothing to classify: the text rendered to nothing")

        purpose = str(config.get("purpose") or "utility")
        owner = str(context.get("owner") or "")
        from src.typed_decision import Field            # noqa: PLC0415 - heavy import, only here
        fld = Field(name="label", question=question or "Which category best describes the text?",
                    choices=names, descriptions=[d for _, d in labels] if any(d for _, d in labels) else None)
        decisions = models.decide(text, [fld], owner=owner or None, purpose=purpose,
                                  instructions=instructions, timeout_s=timeout_s,
                                  min_confidence=threshold, caller="workflow.classify") or {}
        decision = decisions.get("label")
        receipt: Dict[str, Any] = {
            "options": names, "threshold": threshold, "fallback": fallback,
            "on_uncertain": mode, "fallback_used": False, "asked": False,
            "choice": None, "confidence": None, "method": "", "distribution": {},
            "uncertain_reason": "", "model": "",
        }
        if decision is not None:
            receipt.update(confidence=getattr(decision, "confidence", None),
                           distribution=dict(getattr(decision, "distribution", None) or {}),
                           model=str(getattr(decision, "model", "") or ""),
                           method=str(getattr(decision, "method", "") or ""),
                           best=getattr(decision, "best", None))
        value = getattr(decision, "value", None) if decision is not None else None
        if value in names:
            receipt["choice"] = value
            return {"branch": value, "label": value, "receipt": receipt}

        receipt["uncertain_reason"] = (getattr(decision, "reason", "") if decision is not None
                                       else "") or "no_decision"
        if mode == "ask":
            receipt["asked"] = True
            listing = "\n".join(f"- {n}" + (f": {d}" if d else "") for n, d in labels)
            try:
                reply = models.complete(
                    [{"role": "system", "content": (
                        "You classify text. Reply with exactly one of the labels given, "
                        "spelled exactly as given, and nothing else.")},
                     {"role": "user", "content": (
                         (question or "Which label best describes the text?") +
                         "\n\nLabels:\n" + listing + "\n\nText:\n" + text[:6000])}],
                    owner=owner, purpose=purpose, timeout_s=timeout_s, max_tokens=24,
                    temperature=0.0)
            except ModelUnavailable as exc:
                receipt["ask_error"] = str(exc)
                reply = ""
            picked = _match_label(reply, names)
            receipt["ask_reply"] = str(reply or "")[:200]
            if picked:
                receipt.update(choice=picked, method="ask")
                return {"branch": picked, "label": picked, "receipt": receipt}
        if fallback:
            receipt.update(choice=fallback, method="fallback", fallback_used=True)
            return {"branch": fallback, "label": fallback, "receipt": receipt}
        return _failed(
            "the model was not sure enough to choose a label "
            f"({receipt['uncertain_reason']}) and there is no `fallback`; nothing was routed",
            receipt=receipt)

    return handle


# ── extract ──────────────────────────────────────────────────────────────

MAX_EXTRACT_CHARS = 20_000

_EXTRACT_SYSTEM = (
    "You extract structured parameters from a text. Reply with ONE JSON object that "
    "satisfies the given schema and nothing else: no prose, no code fence. Use only "
    "what the text says; never invent a value. When the text does not give a value "
    "and the schema allows it, leave the field out or use null.")


def extract_handler(models: Optional[ModelCalls] = None) -> Callable:
    """`extract`: pull the parameters a JSON schema describes out of a text.

    Config: `text` (template — a run input or an upstream output), `schema`
    (object schema), `instructions`, `purpose`, `timeout_s` (default 60).
    The model's answer is parsed and validated; one repair call with the exact
    errors, then the node fails. Output: `{data, repaired, input_truncated}`."""

    def handle(node: WorkflowNode, context: Mapping[str, Any]) -> Dict[str, Any]:
        config = node.config or {}
        text_template, problem = _text_config(config, "text")
        if problem:
            return _failed(problem)
        schema, problem = _schema_config(config, "schema", required=True)
        if problem:
            return _failed(problem)
        if schema.get("type") != "object":
            return _failed("config.schema must describe an object (`\"type\": \"object\"`): an "
                           "extract node returns named parameters")
        instructions, problem = _text_config(config, "instructions", required=False, limit=2000)
        if problem:
            return _failed(problem)
        timeout_s, problem = _number(config, "timeout_s", 60, 1, 600)
        if problem:
            return _failed(problem)
        if models is None:
            return _failed(
                "no model is wired to the 'extract' node type; nothing was extracted. Pass "
                "`models=` to default_handlers")
        try:
            text = render(text_template, context)
        except TemplateError as exc:
            return _failed(str(exc))
        if not text.strip():
            return _failed("there is nothing to extract from: the text rendered to nothing")
        truncated = len(text) > MAX_EXTRACT_CHARS
        body = text[:MAX_EXTRACT_CHARS]
        purpose = str(config.get("purpose") or "utility")
        owner = str(context.get("owner") or "")
        task = ("Schema:\n" + json.dumps(schema, ensure_ascii=False, sort_keys=True) +
                (("\n\nInstructions:\n" + instructions) if instructions else "") +
                "\n\nText:\n\"\"\"\n" + body + "\n\"\"\"")
        try:
            reply = models.complete(
                [{"role": "system", "content": _EXTRACT_SYSTEM}, {"role": "user", "content": task}],
                owner=owner, purpose=purpose, timeout_s=timeout_s, max_tokens=1500, temperature=0.0)
        except ModelUnavailable as exc:
            return _failed(f"the model could not be reached: {exc}")
        verdict = structured(reply, schema, models=models, owner=owner, purpose=purpose,
                             task=task, timeout_s=min(timeout_s, 120.0))
        if not verdict["ok"]:
            return _failed("the extracted parameters do not satisfy `schema`: "
                           + "; ".join(verdict["errors"][:6])
                           + (f" ({verdict['repair']})" if verdict.get("repair") else ""),
                           raw=str(reply)[:2000])
        return {"data": verdict["data"], "repaired": bool(verdict["repaired"]),
                "input_truncated": truncated}

    return handle


# ── guard ────────────────────────────────────────────────────────────────

MAX_GUARD_CHARS = 200_000
MAX_CHECKS = 12
PII_KINDS = ("EMAIL", "PHONE", "IBAN", "CARD", "ID", "IP")
DEFAULT_PII_KINDS = ("EMAIL", "PHONE", "IBAN")
_URL_RE = re.compile(r"\b(?:https?|ftp)://[^\s<>\"'`)\]}]+", re.IGNORECASE)
_CHECK_TYPES = ("secrets", "pii", "urls", "injection", "model")


def _host_matches(host: str, pattern: str) -> bool:
    """`example.com` matches itself and its subdomains; `*.example.com` only
    subdomains; a leading-dot or bare form is the same as the first."""
    host = host.lower().rstrip(".")
    pat = pattern.strip().lower().rstrip(".")
    if not pat:
        return False
    if pat.startswith("*."):
        return host.endswith(pat[1:]) and host != pat[2:]
    pat = pat.lstrip(".")
    return host == pat or host.endswith("." + pat)


def _host_list(value: Any, name: str) -> Tuple[List[str], str]:
    if value is None:
        return [], ""
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) and v.strip() for v in value):
        return [], f"{name} must be a list of host names"
    return [v.strip() for v in value], ""


def _guard_checks(config: Mapping[str, Any]) -> Tuple[List[Dict[str, Any]], str]:
    raw = config.get("checks")
    if not isinstance(raw, (list, tuple)) or not raw:
        return [], "a guard needs `config.checks`, a non-empty list (secrets, pii, urls, injection, model)"
    if len(raw) > MAX_CHECKS:
        return [], f"a guard runs at most {MAX_CHECKS} checks"
    checks: List[Dict[str, Any]] = []
    for index, item in enumerate(raw):
        spec = {"type": item} if isinstance(item, str) else item
        if not isinstance(spec, Mapping) or spec.get("type") not in _CHECK_TYPES:
            return [], f"config.checks[{index}] must be one of {list(_CHECK_TYPES)} (or an object with that `type`)"
        spec = dict(spec)
        kind = spec["type"]
        spec["id"] = str(spec.get("id") or f"{kind}" + (f"_{index}" if sum(1 for c in checks if c["type"] == kind) else ""))
        if any(c["id"] == spec["id"] for c in checks):
            spec["id"] = f"{spec['id']}_{index}"
        if kind == "pii":
            kinds = spec.get("kinds", list(DEFAULT_PII_KINDS))
            if (not isinstance(kinds, (list, tuple)) or not kinds
                    or any(str(k).upper() not in PII_KINDS for k in kinds)):
                return [], f"config.checks[{index}].kinds must be a non-empty subset of {list(PII_KINDS)}"
            spec["kinds"] = [str(k).upper() for k in kinds]
        elif kind == "urls":
            spec["allow"], problem = _host_list(spec.get("allow"), f"config.checks[{index}].allow")
            if problem:
                return [], problem
            spec["deny"], problem = _host_list(spec.get("deny"), f"config.checks[{index}].deny")
            if problem:
                return [], problem
            if not spec["allow"] and not spec["deny"]:
                return [], f"config.checks[{index}] (urls) needs an `allow` or a `deny` list"
        elif kind == "model":
            question = spec.get("question")
            if not isinstance(question, str) or not question.strip() or len(question) > 500:
                return [], f"config.checks[{index}].question must be a yes/no question (up to 500 characters)"
            threshold, problem = _number(spec, "threshold", DEFAULT_THRESHOLD, 0.0, 1.0)
            if problem:
                return [], problem.replace("config.", f"config.checks[{index}].")
            spec["threshold"] = threshold
        checks.append(spec)
    return checks, ""


def _check_secrets(text: str, spec: Mapping[str, Any]) -> Dict[str, Any]:
    from src.security_scan import find_secrets
    found = find_secrets(text)
    return {"status": "fail" if found else "pass",
            "evidence": [{"kind": f["kind"], "line": f["line"]} for f in found[:10]],
            "count": len(found)}


def _check_pii(text: str, spec: Mapping[str, Any]) -> Dict[str, Any]:
    from src.pii_redaction import redact_pii
    _, counts = redact_pii(text)
    hits = {k: v for k, v in counts.items() if k in spec["kinds"]}
    return {"status": "fail" if hits else "pass", "evidence": hits, "count": sum(hits.values())}


def _check_urls(text: str, spec: Mapping[str, Any]) -> Dict[str, Any]:
    from urllib.parse import urlsplit
    allow, deny = spec["allow"], spec["deny"]
    bad: List[Dict[str, str]] = []
    seen = 0
    for match in _URL_RE.finditer(text):
        seen += 1
        url = match.group(0).rstrip(".,;:!?")
        try:
            host = (urlsplit(url).hostname or "").lower()
        except ValueError:
            host = ""
        if not host:
            bad.append({"url": url[:120], "why": "no readable host"})
        elif any(_host_matches(host, d) for d in deny):
            bad.append({"url": url[:120], "why": "host is on the deny list"})
        elif allow and not any(_host_matches(host, a) for a in allow):
            bad.append({"url": url[:120], "why": "host is not on the allow list"})
    return {"status": "fail" if bad else "pass", "evidence": bad[:10], "count": len(bad), "urls_seen": seen}


def _check_injection(text: str, spec: Mapping[str, Any]) -> Dict[str, Any]:
    from src.security_scan import scan_text
    hits = [f for f in scan_text(text, kind="generic").findings if f.category == "prompt_injection"]
    return {"status": "fail" if hits else "pass",
            "evidence": [{"rule": f.rule_id, "line": f.line} for f in hits[:10]], "count": len(hits)}


_DETERMINISTIC = {"secrets": _check_secrets, "pii": _check_pii, "urls": _check_urls,
                  "injection": _check_injection}


def guard_handler(models: Optional[ModelCalls] = None) -> Callable:
    """`guard`: deterministic checks, plus optional yes/no model checks, then
    two branches, `pass` and `fail`.

    Config: `text` (template), `checks` (list; each a type name or an object):

    * `secrets` — credential-shaped strings (`security_scan.find_secrets`);
    * `pii` — `kinds` from EMAIL, PHONE, IBAN, CARD, ID, IP (default the first
      three), validated (IBAN mod-97, card Luhn) by `pii_redaction`;
    * `urls` — `allow` and/or `deny` host lists (`*.host` for subdomains only);
    * `injection` — instruction-hijack phrases (`security_scan`);
    * `model` — `question` whose answer "yes" means the text is NOT acceptable,
      and a `threshold` (default 0.7) the model's confidence must reach.

    `on_unknown` (`fail` default, or `pass`) decides a check the model could not
    settle: a guard is a gate, so it fails closed unless told otherwise.
    Output: `{branch, passed, checks: [{id, type, status: pass|fail|unknown,
    evidence...}], failed, unknown}`. Evidence never contains the matched
    secret or address itself, only what kind it was and where."""

    def handle(node: WorkflowNode, context: Mapping[str, Any]) -> Dict[str, Any]:
        config = node.config or {}
        text_template, problem = _text_config(config, "text")
        if problem:
            return _failed(problem)
        checks, problem = _guard_checks(config)
        if problem:
            return _failed(problem)
        on_unknown = config.get("on_unknown", "fail")
        if on_unknown not in ("fail", "pass"):
            return _failed("config.on_unknown must be 'fail' or 'pass'")
        timeout_s, problem = _number(config, "timeout_s", 30, 1, 600)
        if problem:
            return _failed(problem)
        model_checks = [c for c in checks if c["type"] == "model"]
        if model_checks and models is None:
            return _failed(
                "no model is wired to the 'guard' node type, and this guard has a model check; "
                "no verdict was given. Pass `models=` to default_handlers")
        try:
            text = render(text_template, context)
        except TemplateError as exc:
            return _failed(str(exc))
        truncated = len(text) > MAX_GUARD_CHARS
        if truncated:
            text = text[:MAX_GUARD_CHARS]

        results: Dict[str, Dict[str, Any]] = {}
        for spec in checks:
            if spec["type"] in _DETERMINISTIC:
                outcome = _DETERMINISTIC[spec["type"]](text, spec)
                results[spec["id"]] = {"id": spec["id"], "type": spec["type"], **outcome}

        if model_checks:
            from src.typed_decision import Field            # noqa: PLC0415
            fields = [Field(name=c["id"], question=c["question"].strip(), choices="bool")
                      for c in model_checks]
            owner = str(context.get("owner") or "")
            purpose = str(config.get("purpose") or "utility")
            try:
                decisions = models.decide(
                    text[:12000], fields, owner=owner or None, purpose=purpose,
                    instructions=str(config.get("instructions") or ""), timeout_s=timeout_s,
                    min_confidence=min(c["threshold"] for c in model_checks),
                    caller="workflow.guard") or {}
                error = ""
            except ModelUnavailable as exc:
                decisions, error = {}, str(exc)
            for spec in model_checks:
                decision = decisions.get(spec["id"])
                value = getattr(decision, "value", None) if decision is not None else None
                confidence = getattr(decision, "confidence", None) if decision is not None else None
                if value in ("yes", "no") and isinstance(confidence, (int, float)) \
                        and confidence >= spec["threshold"]:
                    status = "fail" if value == "yes" else "pass"
                else:
                    status = "unknown"
                results[spec["id"]] = {
                    "id": spec["id"], "type": "model", "status": status,
                    "evidence": {"answer": value, "best": getattr(decision, "best", None),
                                 "confidence": confidence, "threshold": spec["threshold"],
                                 "model": str(getattr(decision, "model", "") or ""),
                                 "reason": error or str(getattr(decision, "reason", "") or "")}}

        ordered = [results[c["id"]] for c in checks]
        failed = [r["id"] for r in ordered if r["status"] == "fail"]
        unknown = [r["id"] for r in ordered if r["status"] == "unknown"]
        passed = not failed and (not unknown or on_unknown == "pass")
        return {"branch": "pass" if passed else "fail", "passed": passed, "checks": ordered,
                "failed": failed, "unknown": unknown, "on_unknown": on_unknown,
                "input_truncated": truncated}

    return handle


def model_node_handlers(*, agent: Optional[Callable] = None,
                        models: Optional[ModelCalls] = None) -> Dict[str, Callable]:
    return {"agent": agent_handler(agent, models=models),
            "classify": classify_handler(models),
            "extract": extract_handler(models),
            "guard": guard_handler(models)}
