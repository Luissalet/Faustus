"""
workflows/handlers.py — what the node types actually do.

The engine knows about claiming, retrying and pausing; it deliberately knows
nothing about skills, approvals or email. This is where a node type becomes
work, and the rule that shapes every handler here is the same one from the
rest of the platform: **nothing is capable by default**.

So `deliver` has no sender until someone passes one in, and says so in the
refusal rather than pretending it sent something. `skill` has no runtime until
a runner is passed in. The handlers that need no capability at all — a trigger,
a condition, a wait — are complete, because they never reach outside.

The two-pass handlers (`wait`, `human_approval`) are the reason
`context["previous"]` exists: they pause, and the pass that comes back has to
recognise its own earlier work instead of starting again. That is also why
they read the approval id and the wake time from the previous result rather
than from a field they own — the engine already wrote it down.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Mapping, Optional

from src.contracts.base import now_iso
from src.contracts.workflow import WorkflowNode

logger = logging.getLogger(__name__)

#: Comparisons a `condition` node can make. Closed on purpose: a workflow file
#: is data, and an expression language in a data file is an execution surface.
OPERATORS = ("eq", "ne", "gt", "gte", "lt", "lte", "contains", "in",
             "exists", "truthy")


# ── reading values out of the run ─────────────────────────────────────────

_MISSING = object()


def resolve(path: str, context: Mapping[str, Any]) -> Any:
    """`results.gather.count` → the value, or `_MISSING`.

    Dotted paths only, no indexing and no calls. A workflow definition is
    something a user can paste in; the moment it can express a lookup with
    side effects, reading one is dangerous."""
    if not path:
        return _MISSING
    current: Any = context
    for part in path.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        else:
            return _MISSING
    return current


def _field(obj: Any, name: str) -> Any:
    """Read one field off a contract object or a plain dict.

    The approval store returns `Approval` instances; a test double, a JSON
    payload from a route, or a future store returns dicts. Reading both means
    the handler is not coupled to which one it got."""
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _describe(spec: Any) -> str:
    """How to name one side of a comparison in a message. `'inputs.score'`,
    not `{'path': 'inputs.score'}` — the reader is looking for the line in
    their own definition, not for Python's repr of it."""
    if isinstance(spec, Mapping) and "path" in spec:
        return repr(str(spec["path"]))
    return repr(spec)


def _side(spec: Any, context: Mapping[str, Any]) -> Any:
    """A side of a comparison: `{"path": "..."}` reads from the run, anything
    else is the literal itself. Explicit, because guessing whether a string is
    a path or a value is how `"status"` silently becomes a lookup."""
    if isinstance(spec, Mapping) and "path" in spec:
        return resolve(str(spec["path"]), context)
    return spec


def evaluate(when: Mapping[str, Any], context: Mapping[str, Any]) -> Dict[str, Any]:
    """One comparison, answered with what it saw.

    Returns `{"passed": bool, "detail": str}`. The detail matters more than it
    looks: a condition that quietly says False is the hardest kind of workflow
    bug, so the answer always carries the two values it compared."""
    op = str(when.get("op") or "truthy")
    if op not in OPERATORS:
        return {"passed": False, "error": f"unknown operator {op!r}",
                "detail": f"known operators: {', '.join(OPERATORS)}"}

    left = _side(when.get("left"), context)
    right = _side(when.get("right"), context)

    where = _describe(when.get("left"))
    if op == "exists":
        return {"passed": left is not _MISSING,
                "detail": f"{where} " +
                          ("is present" if left is not _MISSING else "is not set")}
    if left is _MISSING:
        return {"passed": False, "error": "missing_value",
                "detail": f"nothing at {where}"}
    if op == "truthy":
        return {"passed": bool(left), "detail": f"value is {left!r}"}
    if op in ("contains", "in"):
        haystack, needle = (left, right) if op == "contains" else (right, left)
        try:
            passed = needle in haystack
        except TypeError:
            return {"passed": False, "error": "not_a_container",
                    "detail": f"{haystack!r} cannot contain anything"}
        return {"passed": passed, "detail": f"{needle!r} in {haystack!r} is {passed}"}

    if op in ("eq", "ne"):
        passed = (left == right) if op == "eq" else (left != right)
        return {"passed": passed, "detail": f"{left!r} {op} {right!r}"}

    try:
        passed = {"gt": left > right, "gte": left >= right,
                  "lt": left < right, "lte": left <= right}[op]
    except TypeError:
        # Ordering a string against a number is a definition mistake, not a
        # False. Saying so is the difference between fixing it and staring at
        # a branch that never runs.
        return {"passed": False, "error": "not_comparable",
                "detail": f"cannot order {type(left).__name__} against "
                          f"{type(right).__name__} ({left!r} vs {right!r})"}
    return {"passed": passed, "detail": f"{left!r} {op} {right!r}"}


# ── the handlers ──────────────────────────────────────────────────────────

def trigger_handler(node: WorkflowNode, context: Mapping[str, Any]) -> Dict[str, Any]:
    """`manual`, `schedule`, `webhook`. A trigger node has already happened by
    the time the run exists; running it records what started this."""
    return {"trigger": node.type, "inputs": dict(context.get("inputs") or {}),
            "at": now_iso()}


def condition_handler(node: WorkflowNode, context: Mapping[str, Any]) -> Dict[str, Any]:
    """A branch, and a skip when it is not taken.

    `skipped` rather than `completed` is what stops the rest of the branch —
    see `_stopping` in the engine. A condition that completed with
    `passed: false` would let everything downstream run anyway, which is the
    opposite of a condition."""
    when = node.config.get("when")
    if not isinstance(when, Mapping):
        return {"status": "failed",
                "reason": "a condition node needs `config.when` with "
                          "`left`, `op` and (unless op is exists/truthy) `right`"}
    verdict = evaluate(when, context)
    if verdict.get("error"):
        return {"status": "failed",
                "reason": f"{verdict['error']}: {verdict['detail']}"}
    if verdict["passed"]:
        return {"passed": True, "detail": verdict["detail"]}
    return {"status": "skipped", "passed": False,
            "reason": f"condition not met: {verdict['detail']}"}


def wait_handler(node: WorkflowNode, context: Mapping[str, Any]) -> Dict[str, Any]:
    """Pause until a time, then carry on.

    Two passes. The first computes the wake time and pauses; the engine wakes
    the node once that time is past, and the second pass — which recognises
    itself by the wake time in `context["previous"]` — completes. Computing
    the deadline again on the second pass is how a `wait` becomes forever."""
    previous = context.get("previous") or {}
    already = str(previous.get("wake_at") or "")
    if already:
        if already <= now_iso():
            return {"waited_until": already}
        return {"status": "paused", "wake_at": already,
                "reason": f"waiting until {already}"}

    until = str(node.config.get("until") or "")
    seconds = node.config.get("seconds")
    if until:
        wake_at = until
    elif isinstance(seconds, (int, float)) and seconds >= 0:
        wake_at = (datetime.now(timezone.utc)
                   + timedelta(seconds=float(seconds))).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        return {"status": "failed",
                "reason": "a wait node needs `config.seconds` (a number) or "
                          "`config.until` (an ISO timestamp)"}
    if wake_at <= now_iso():
        return {"waited_until": wake_at, "detail": "the time had already passed"}
    return {"status": "paused", "wake_at": wake_at, "reason": f"waiting until {wake_at}"}


def approval_handler(store: Any = None, *, owner: str = "",
                     ttl_seconds: Optional[int] = None) -> Callable:
    """`human_approval`: open a card, pause, and read the answer on the way back.

    The card is opened once. The second pass finds its id in the previous
    result and asks the store what happened — it never opens a second card,
    because two cards for one gate is how a person approves something twice
    and a denial gets lost."""

    def handle(node: WorkflowNode, context: Mapping[str, Any]) -> Dict[str, Any]:
        approvals = store
        if approvals is None:
            from src import approval_store as approvals   # noqa: PLC0415

        previous = context.get("previous") or {}
        approval_id = str(previous.get("approval_id") or "")

        if approval_id:
            card = approvals.get(approval_id)
            if card is None:
                return {"status": "failed",
                        "reason": f"approval {approval_id} is gone; nothing to read"}
            status = _field(card, "status")
            if status == "granted":
                return {"approved": True, "approval_id": approval_id,
                        "decided_by": _field(card, "decided_by")}
            if status in ("denied", "revoked"):
                return {"status": "failed", "approval_id": approval_id,
                        "reason": "a person said no: "
                                  f"{_field(card, 'reason') or 'no reason given'}"}
            if status == "expired":
                return {"status": "failed", "approval_id": approval_id,
                        "reason": "the approval expired before anyone answered it"}
            if status == "consumed":
                # Someone already spent this yes elsewhere. Treating it as a
                # fresh grant would be exactly the double-use the store exists
                # to prevent.
                return {"status": "failed", "approval_id": approval_id,
                        "reason": "this approval was already used"}
            # Still pending: stay paused on the same card.
            return {"status": "paused", "approval_id": approval_id,
                    "reason": "waiting on a person"}

        plan = {
            "action": str(node.config.get("action") or "deliver"),
            "detail": str(node.config.get("detail")
                          or node.title
                          or f"workflow {context.get('workflow')} step {node.id}"),
        }
        for key in ("recipients", "cost_units", "skill_id", "skill_version",
                    "backend", "secret_names", "output_kinds", "permissions"):
            if key in node.config:
                plan[key] = node.config[key]

        extra: Dict[str, Any] = {}
        ttl = node.config.get("ttl_seconds", ttl_seconds)
        if ttl is not None:
            # Only when someone actually chose one: passing `None` through
            # would mean "never expires", which is not what "not configured"
            # says. Left out, the store's own default applies.
            extra["ttl_seconds"] = ttl

        # The run's owner is the fallback, and it matters: a card with no
        # owner is in nobody's pending list, so the gate would be waiting on a
        # person who is never shown the question.
        card_owner = str(node.config.get("owner")
                         or owner
                         or context.get("owner") or "")
        opened = approvals.request(plan, owner=card_owner,
                                   run_id=str(context.get("run_id") or ""), **extra)
        new_id = _field(opened, "id") or _field(opened, "approval_id")
        if not new_id:
            return {"status": "failed",
                    "reason": "the approval store returned no id to wait on"}
        return {"status": "paused", "approval_id": str(new_id),
                "reason": plan["detail"]}

    return handle


def deliver_handler(send: Optional[Callable] = None) -> Callable:
    """`deliver`: hand the message to whatever actually sends.

    With no sender this refuses. That is the point — a workflow that "sent"
    an email into a handler nobody wired up is worse than one that stopped,
    because the run says completed and nothing arrived.

    The sender is called with the node's config plus the run's idempotency key
    so it can refuse a repeat on its own terms; that key is the whole reason
    the rest of this phase exists."""

    def handle(node: WorkflowNode, context: Mapping[str, Any]) -> Dict[str, Any]:
        if send is None:
            return {"status": "failed", "reason": (
                "no sender is wired to the 'deliver' node type; nothing was sent. "
                "Pass one to default_handlers(deliver=...) — Faustus does not ship "
                "a mail client and will not pretend it did")}
        payload = dict(node.config)
        result = send(payload, dict(context)) or {}
        return {"delivered": True, **result}

    return handle


def skill_handler(run: Optional[Callable] = None, *,
                  media: Optional[Callable] = None) -> Callable:
    """`skill`: run a skill.

    A `config.skill` of `media:<template>` goes to the media engine, because
    that half is built and there is no reason to make a caller wire it up. Any
    other skill needs a runner passed in, and refuses by name until one is —
    same deny-by-default shape as `deliver`. A runner that raises is caught by
    the engine and becomes a node failure, not a dead run."""

    def handle(node: WorkflowNode, context: Mapping[str, Any]) -> Dict[str, Any]:
        skill_id = str(node.config.get("skill") or "")
        if not skill_id:
            return {"status": "failed",
                    "reason": "a skill node needs `config.skill` naming the skill to run"}
        if skill_id.startswith("media:"):
            return dict((media or media_skill_runner())(node, context) or {})
        if run is None:
            return {"status": "failed", "reason": (
                f"no runner is wired to the 'skill' node type, so {skill_id!r} did "
                "not run. Pass one to default_handlers(skill=...) — or name a media "
                "template as 'media:<id>', which is wired")}
        outcome = run(node, dict(context)) or {}
        if outcome.get("status") in ("failed", "refused"):
            return {"status": "failed",
                    "reason": str(outcome.get("reason") or "the skill run failed"),
                    "detail": outcome.get("detail", "")}
        return dict(outcome)

    return handle


def media_skill_runner(*, poll_seconds: int = 15) -> Callable:
    """A `skill` node that renders, for `config.skill` values like
    `media:image.product`.

    This is the seam between the media engine and the workflow engine, and it
    needed no new machinery in either. A render takes minutes, so the node
    starts it and **pauses with a wake time** — exactly what a `wait` does —
    and each wake asks the engine. The engine's own job id is on the media run
    row, so a Faustus that restarts mid-render comes back and carries on.

    It recognises its own earlier pass by the media run id in
    `context["previous"]`, which is the same trick the approval gate uses and
    for the same reason: starting a second render because nobody remembered
    the first is the exact failure this phase is about."""

    def run(node: WorkflowNode, context: Mapping[str, Any]) -> Dict[str, Any]:
        from src import media_runs                          # noqa: PLC0415

        previous = context.get("previous") or {}
        run_id = str(previous.get("media_run_id") or "")

        if not run_id:
            workflow_id = str(node.config.get("skill") or "")[len("media:"):]
            started = media_runs.start(
                workflow_id,
                node.config.get("inputs") or {},
                version=str(node.config.get("version") or ""),
                owner=str(context.get("owner") or ""),
                project_id=str(node.config.get("project_id") or ""),
                session_id=str(node.config.get("session_id") or ""))
            if not started.get("ok"):
                return {"status": "failed",
                        "reason": f"{started.get('reason')}: {started.get('detail', '')}",
                        "media_run_id": started.get("run_id", "")}
            run_id = started["run_id"]
            return {"status": "paused", "media_run_id": run_id,
                    "wake_at": _in_seconds(poll_seconds),
                    "reason": f"rendering {workflow_id} as {run_id}"}

        state = media_runs.poll(run_id)
        status = state.get("status")
        if status == "completed":
            return {"media_run_id": run_id,
                    "artifact_ids": [a["id"] for a in state.get("artifacts") or []],
                    "artifacts": state.get("artifacts") or [],
                    "values": state.get("values") or {}}
        if status in ("failed", "cancelled"):
            return {"status": "failed", "media_run_id": run_id,
                    "reason": state.get("reason") or f"the render {status}"}
        # Still going, or the engine could not be reached — either way this is
        # a wait, not a failure. `unknown` included: an engine that forgot the
        # job is a fact about the engine, and the node keeps asking rather
        # than deciding the render failed on its behalf.
        return {"status": "paused", "media_run_id": run_id,
                "wake_at": _in_seconds(poll_seconds),
                "reason": f"the render is {status}"}

    return run


def _in_seconds(seconds: int) -> str:
    return (datetime.now(timezone.utc)
            + timedelta(seconds=max(1, int(seconds)))).strftime("%Y-%m-%dT%H:%M:%SZ")


def artifact_handler(save: Optional[Callable] = None) -> Callable:
    """`artifact_store`: put something in the artifact store and name it.

    Deny-by-default like the others. A workflow that claims to have saved a
    report nobody can find is a lie the user only discovers later."""

    def handle(node: WorkflowNode, context: Mapping[str, Any]) -> Dict[str, Any]:
        if save is None:
            return {"status": "failed", "reason": (
                "no store is wired to the 'artifact_store' node type; nothing was "
                "saved. Pass one to default_handlers(artifact_store=...)")}
        return dict(save(node, dict(context)) or {"stored": True})

    return handle


def default_handlers(*, approvals: Any = None, owner: str = "",
                     deliver: Optional[Callable] = None,
                     skill: Optional[Callable] = None,
                     media: Optional[Callable] = None,
                     artifact_store: Optional[Callable] = None,
                     ttl_seconds: Optional[int] = None) -> Dict[str, Callable]:
    """Every node type the contract allows, wired or honestly refusing.

    A missing entry would make the engine fail the node with "no handler for
    node type", which reads like a bug in Faustus. A handler that refuses by
    name reads like what it is: a capability nobody connected yet."""
    return {
        "manual": trigger_handler,
        "schedule": trigger_handler,
        "webhook": trigger_handler,
        "condition": condition_handler,
        "wait": wait_handler,
        "human_approval": approval_handler(approvals, owner=owner,
                                           ttl_seconds=ttl_seconds),
        "deliver": deliver_handler(deliver),
        "skill": skill_handler(skill, media=media),
        "artifact_store": artifact_handler(artifact_store),
    }
