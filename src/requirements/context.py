"""requirements/context.py — hand the agent the requirement it needs, not the
whole spec (ADP-19).

``for_task`` is the only entry point. It answers three, and only three,
questions about a project's requirements, each one explicit rather than
silently folded into the others (ADP-19 acceptance criteria):

  * which requirements are actually relevant (a direct ``REQ-N`` id always
    wins over one merely linked to a file the caller mentions);
  * which REQUESTED id does not exist (``unknown``) -- never answered with a
    fabricated or nearest-match requirement;
  * which relevant requirement did NOT fit the character budget
    (``omitted``) -- never silently cut down to a shorter version of itself.

No LLM call, no tokenizer: `budget_chars` is a plain character budget the
caller already computed (this module does not reach into
``context_engine.budgets`` -- that machinery measures a whole turn's context
across many sections; this is one section's own accountant). A requirement is
included WHOLE or not at all -- ADP-19's own limit ("una restricción esencial
no se corta en silencio") is honoured by never truncating a title/text/
acceptance list to make something fit; the alternative is reporting it
`omitted`, in full view of the caller.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Sequence

from src.requirements import store as req_store

logger = logging.getLogger(__name__)

DEFAULT_BUDGET_CHARS = 4000


def _render(req: Dict[str, Any]) -> str:
    """The full text block for one requirement -- title, text, every
    acceptance criterion. Rendered whole because inclusion is whole-or-
    nothing (see module docstring)."""
    lines = [f"{req['key']} [{req['status']}] {req['title']}"]
    if req.get("text"):
        lines.append(req["text"])
    for item in req.get("acceptance") or []:
        lines.append(f"- {item}")
    return "\n".join(lines)


def _linked_keys_for_files(project_id: str, files: Sequence[str]) -> List[str]:
    """Requirement keys with an `implements`/`tests` link whose target
    starts with one of `files` -- "this file's requirement", not a text
    search. A target is `path[@symbol]` or `path::symbol`; matching is by
    path prefix only (a requirement about `src/auth.py` also surfaces for a
    caller citing `src/auth.py@login`)."""
    if not files:
        return []
    wanted = [str(f or "").strip() for f in files if str(f or "").strip()]
    if not wanted:
        return []
    out: List[str] = []
    for key in req_store.all_requirement_keys(project_id):
        for link in req_store.list_links(project_id, key):
            if link["kind"] not in ("implements", "tests"):
                continue
            target_path = link["target"].split("::")[0].split("@")[0]
            if any(target_path == w or target_path.startswith(w.rstrip("/") + "/") for w in wanted):
                if key not in out:
                    out.append(key)
                break
    return out


def for_task(
    project_id: str, *, files: Sequence[str] = (), keys: Sequence[str] = (),
    budget_chars: int = DEFAULT_BUDGET_CHARS,
) -> Dict[str, Any]:
    """Requirements relevant to one task, budgeted.

    ``keys`` are requirement ids the caller already knows it wants (a task
    description that names ``REQ-7`` directly, say) -- these are prioritised
    FIRST and any that do not exist land in ``unknown``, never silently
    dropped. ``files`` are paths the task touches; any requirement linked to
    one of them is added next, in the order ``all_requirement_keys`` returns
    (stable, not reordered by relevance scoring this module does not have
    the evidence to do honestly).
    """
    project_id = str(project_id or "").strip()
    budget_chars = max(0, int(budget_chars or 0))
    unknown: List[str] = []
    ordered_keys: List[str] = []

    for raw_key in keys or ():
        key = str(raw_key or "").strip().upper()
        if not key:
            continue
        if req_store.get(project_id, key) is None:
            if key not in unknown:
                unknown.append(key)
            continue
        if key not in ordered_keys:
            ordered_keys.append(key)

    for key in _linked_keys_for_files(project_id, files):
        if key not in ordered_keys:
            ordered_keys.append(key)

    included: List[Dict[str, Any]] = []
    omitted: List[Dict[str, Any]] = []
    used = 0
    for key in ordered_keys:
        req = req_store.get(project_id, key)
        if req is None:  # a link pointed at a since-deleted requirement
            continue
        block = _render(req)
        cost = len(block) + (2 if included else 0)  # a blank-line separator between blocks
        if used + cost > budget_chars:
            omitted.append({"key": key, "title": req["title"], "reason": "budget_exceeded", "chars": len(block)})
            continue
        included.append(req)
        used += cost

    return {
        "project_id": project_id,
        "requirements": included,
        "omitted": omitted,
        "unknown": unknown,
        "budget_chars": budget_chars,
        "used_chars": used,
    }
