"""screen_recipe.py -- from a procedure observed on screen to a learned recipe (Hangar E40, first cut).

Talos's Hoard drafts a procedure from what was seen on screen (``activity_procedure_draft``: windows, clipboard, screenshots
and files segmented into steps, typed or pasted values as named inputs with secrets masked, and an explicit list of what the
observation cannot say). It never acts on the observed computer, and neither does this module.

Here that draft becomes a learned recipe (``src/recipe_learning.py``): every step a person maps to a Faustus tool and its
arguments becomes a recipe step; the draft's inputs become parameter hints, so their example values turn into parameters;
the steps left unmapped and everything the observation could not see become the recipe's limits. The recipe then follows
the normal path -- try on a second case, execute in a workspace, promote -- so it is tested on a case other than the one
that was watched. The intent of the person is never inferred from the recording: the mapping is explicit.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)

SCHEMA = "talos.procedure_draft/1"
MASK = "[masked]"


class ScreenRecipeError(Exception):
    def __init__(self, error_class: str, detail: str):
        super().__init__(detail)
        self.error_class, self.detail = error_class, detail


def _hub_call(app: str, tool: str, arguments: Mapping[str, Any]) -> Dict[str, Any]:
    """Call a family app's tool through the local Hub; never raises."""
    try:
        from src import family_services
        from src.hoard_link import family
        family_services.configure()
        res = family.call(app, tool, dict(arguments), timeout=60.0)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(res, dict):
        return {"ok": False, "error": "unexpected answer from the hub"}
    if res.get("ok") is False or res.get("error"):
        return {"ok": False, "error": str(res.get("error") or f"HTTP {res.get('status')}")}
    return {"ok": True, "result": res.get("result", res)}


def fetch_draft(*, start: str = "", end: str = "", events: Optional[List[Mapping[str, Any]]] = None, title: str = "",
                locale: str = "es") -> Dict[str, Any]:
    """Ask Talos for a draft of a recorded range (``start``/``end``) or of an event log given here (``events``)."""
    args: Dict[str, Any] = {"locale": locale if locale in ("es", "en") else "es", "format": "json"}
    if title:
        args["title"] = title[:200]
    if events:
        args["events"] = list(events)[:5000]
    else:
        if start:
            args["start"] = start
        if end:
            args["end"] = end
    res = _hub_call("talos", "activity_procedure_draft", args)
    if not res["ok"]:
        hint = (" The Activity module of Talos's Hoard is off; it only drafts from what it recorded while it was on. "
                "Turn it on there if you want it to record, or paste a draft exported from Talos."
                if "module_disabled" in res["error"] else "")
        raise ScreenRecipeError("screen.talos_unavailable", f"Talos did not give a draft: {res['error']}.{hint}")
    draft = res["result"]
    return check_draft(draft)


def check_draft(draft: Any) -> Dict[str, Any]:
    if not isinstance(draft, Mapping) or draft.get("schema") != SCHEMA:
        raise ScreenRecipeError("screen.bad_draft", f"not a Talos procedure draft ({SCHEMA})")
    if not isinstance(draft.get("steps"), list):
        raise ScreenRecipeError("screen.bad_draft", "the draft has no steps list")
    return dict(draft)


def _mapping(raw: Any) -> Dict[int, Dict[str, Any]]:
    """``[{step, tool, args}]`` or ``{"<step>": {tool, args}}`` -> {step: {tool, args}}; steps without a tool are left out."""
    items = raw.items() if isinstance(raw, Mapping) else ((m.get("step") if isinstance(m, Mapping) else None, m) for m in (raw or []))
    out: Dict[int, Dict[str, Any]] = {}
    for key, m in items:
        if not isinstance(m, Mapping) or not str(m.get("tool") or "").strip():
            continue
        try:
            n = int(key)
        except (TypeError, ValueError):
            raise ScreenRecipeError("screen.bad_mapping", f"mapping key {key!r} is not a step number") from None
        args = m.get("args", m.get("arguments", {}))
        if not isinstance(args, Mapping):
            raise ScreenRecipeError("screen.bad_mapping", f"step {n}: args must be an object")
        out[n] = {"tool": str(m["tool"]).strip()[:200], "args": dict(args)}
    return out


def summary(draft: Mapping[str, Any]) -> Dict[str, Any]:
    """What the screen panel shows: the steps with their app, inputs and unknowns, ready to be mapped."""
    return {"title": draft.get("title"), "window": draft.get("window") or {}, "shape": draft.get("shape"),
            "steps": [{"n": s.get("n"), "title": s.get("title"), "kind": s.get("kind"), "app": s.get("app"),
                       "window": s.get("window"), "inputs": s.get("inputs") or [], "unknowns": s.get("unknowns") or []}
                      for s in draft.get("steps") or []],
            "inputs": [{"name": i.get("name"), "kind": i.get("kind"), "source": i.get("source"), "example": i.get("example"),
                        "masked": i.get("example") == MASK or bool(i.get("masked"))} for i in draft.get("inputs") or []],
            "outputs": draft.get("outputs") or [], "unknowns": draft.get("unknowns") or [], "masked_values": draft.get("masked_values", 0)}


def propose(owner: str, draft: Mapping[str, Any], mapping: Any, *, title: str = "") -> Dict[str, Any]:
    """A learned recipe from the mapped steps of a screen draft (see the module docstring)."""
    from src import recipe_learning as rl
    from src import recipes as recipes_store
    draft = check_draft(draft)
    mapped = _mapping(mapping)
    steps_by_n = {int(s.get("n")): s for s in draft["steps"] if isinstance(s, Mapping) and s.get("n") is not None}
    unknown_steps = sorted(set(mapped) - set(steps_by_n))
    if unknown_steps:
        raise ScreenRecipeError("screen.bad_mapping", f"the draft has no step {unknown_steps[0]}")
    if not mapped:
        raise ScreenRecipeError("screen.no_mapped_steps", "map at least one observed step to a Faustus tool and its arguments; "
                                                          "a recording shows actions, not which tool should repeat them")
    order = sorted(mapped)
    hints = {i["name"]: i["example"] for i in draft.get("inputs") or []
             if isinstance(i, Mapping) and i.get("name") and i.get("example") not in (None, "", MASK) and not i.get("masked")}
    # the values a person wrote in the mapping (paths, ids, names) are case values: listing them in the request lets the
    # learner turn them into parameters, as it does with the literals of a chat request
    # (free text with spaces is left alone: only paths, quoted when they have spaces, and single words or numbers)
    written = []
    for n in order:
        for v in mapped[n]["args"].values():
            if isinstance(v, bool) or not isinstance(v, (str, int, float)):
                continue
            s = str(v)
            if "\\" in s or "/" in s:
                written.append(f'"{s}"' if " " in s else s)
            elif " " not in s.strip():
                written.append(s)
    request = "\n".join([str(draft.get("title") or "")] + [f"{n}. {steps_by_n[n].get('title')}" for n in order]
                        + [f"{k}: {v}" for k, v in hints.items()] + written)
    recipe = rl.propose(owner, request=request, steps=[{"tool": mapped[n]["tool"], "args": mapped[n]["args"]} for n in order],
                        title=title or str(draft.get("title") or "Procedure seen on screen"), params_hint=hints)
    manual = [{"n": n, "title": s.get("title"), "app": s.get("app")} for n, s in sorted(steps_by_n.items()) if n not in mapped]
    limits = [f"Learned from a procedure seen on screen ({draft.get('window', {}).get('events', '?')} events, shape {draft.get('shape')}); "
              "the recording shows actions, not intent."]
    limits += [f"Step {m['n']} stays manual (not mapped to a tool): {m['title']}" for m in manual]
    limits += [f"Not observed: {u.get('message')}" for u in draft.get("unknowns") or [] if isinstance(u, Mapping) and u.get("message")]
    if draft.get("masked_values"):
        limits.append(f"{draft['masked_values']} secret-looking value(s) were masked and are not part of the recipe.")
    recipe.limits = list(recipe.limits) + limits
    recipe.source = {**(recipe.source or {}), "kind": "screen", "draft_schema": SCHEMA, "shape": draft.get("shape"),
                     "window": draft.get("window"), "mapped_steps": order, "manual_steps": [m["n"] for m in manual]}
    recipes_store.save_recipe(owner, recipe)
    return {"recipe": recipe.to_dict(), "mapped_steps": order, "manual_steps": manual, "unknowns": draft.get("unknowns") or [],
            "next": "Try it on a second, different case (recipe_learn action=try with its params); promotion needs that try."}
