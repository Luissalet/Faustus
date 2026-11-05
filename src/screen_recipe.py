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

import difflib
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

SCHEMA = "talos.procedure_draft/1"
MASK = "[masked]"


class ScreenRecipeError(Exception):
    def __init__(self, error_class: str, detail: str, data: Optional[Dict[str, Any]] = None):
        super().__init__(detail)
        self.error_class, self.detail, self.data = error_class, detail, dict(data or {})


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


def propose(owner: str, draft: Mapping[str, Any], mapping: Any, *, title: str = "",
            observations: Optional[Sequence[Mapping[str, Any]]] = None) -> Dict[str, Any]:
    """A learned recipe from the mapped steps of a screen draft (see the module docstring). ``observations`` are other
    drafts of the same procedure: values that change between them become parameters, values that stay the same stay
    literals, and steps seen in only some of them are listed as branches."""
    from src import recipe_learning as rl
    from src import recipes as recipes_store
    draft = check_draft(draft)
    cmp = compare_drafts([draft, *[check_draft(o) for o in observations or []]]) if observations else None
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
    constants: set = set()
    if cmp:  # several observations: only what changed between them is a parameter
        constants = {str(v) for i in cmp["inputs"] if i["status"] == "constant" for v in i["values"][:1]}
        constants |= {str(w["values"][0]) for w in cmp["windows"] if w["status"] == "constant" and w["values"]}
        # by input name, not by value: a varying input whose first value equals another input's constant stays a parameter
        constant_names = {i["name"] for i in cmp["inputs"] if i["status"] == "constant"}
        hints = {k: v for k, v in hints.items() if k not in constant_names}
    # the values a person wrote in the mapping (paths, ids, names) are case values: listing them in the request lets the
    # learner turn them into parameters, as it does with the literals of a chat request
    # (free text with spaces is left alone: only paths, quoted when they have spaces, and single words or numbers)
    written = []
    for n in order:
        for v in mapped[n]["args"].values():
            if isinstance(v, bool) or not isinstance(v, (str, int, float)):
                continue
            s = str(v)
            if s in constants:
                continue
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
    limits += [f"Not observed: {u.get('message')}" for u in draft.get("unknowns") or [] if isinstance(u, Mapping) and u.get("message")
               and not (cmp and u.get("code") == "single_run")]
    if draft.get("masked_values"):
        limits.append(f"{draft['masked_values']} secret-looking value(s) were masked and are not part of the recipe.")
    if cmp:
        limits += cmp["limits"]
        varying = {i["name"]: str(i["values"][0]) for i in cmp["inputs"] if i["status"] == "varies" and i["values"]}
        for i in cmp["inputs"]:   # steps are matched to parameters by value: the same text in two roles cannot be told apart
            clash = [n for n, v in varying.items() if i["status"] == "constant" and i["values"] and str(i["values"][0]) == v]
            if clash:
                limits.append(f"The constant input {i['name']} (step {i['step']}) has the same value as the changing input "
                              f"{clash[0]} in the observed run, so every step that uses that value was turned into the parameter "
                              f"{clash[0]}: check the steps that should keep it fixed.")
    recipe.limits = list(recipe.limits) + limits
    recipe.source = {**(recipe.source or {}), "kind": "screen", "draft_schema": SCHEMA, "shape": draft.get("shape"),
                     "window": draft.get("window"), "mapped_steps": order, "manual_steps": [m["n"] for m in manual]}
    if draft.get("observed_with"):
        recipe.source["observed_with"] = draft["observed_with"]
    if cmp:
        recipe.source["observations"] = {"count": cmp["observations"], "shapes": cmp["shapes"], "same_shape": cmp["same_shape"],
                                         "varying": [i["name"] for i in cmp["inputs"] if i["status"] == "varies"]
                                         + [w["name"] for w in cmp["windows"] if w["status"] == "varies"],
                                         "constant": [i["name"] for i in cmp["inputs"] if i["status"] == "constant"]}
    recipes_store.save_recipe(owner, recipe)
    return {"recipe": recipe.to_dict(), "mapped_steps": order, "manual_steps": manual, "unknowns": draft.get("unknowns") or [],
            "comparison": cmp,
            "next": "Try it on a second, different case (recipe_learn action=try with its params); promotion needs that try."}


# ---------------------------------------------------------------------------
# several observations of the same procedure
# ---------------------------------------------------------------------------

def _sig(step: Mapping[str, Any]) -> str:
    return f"{step.get('kind')}:{str(step.get('app') or '').strip().lower()}"


def _input_value(draft: Mapping[str, Any], name: str) -> Any:
    for i in draft.get("inputs") or []:
        if isinstance(i, Mapping) and i.get("name") == name:
            return MASK if i.get("masked") or i.get("example") == MASK else i.get("example")
    return None


def compare_drafts(drafts: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Align the steps of several drafts of one procedure (by kind and app, in order) against the first one.

    For every step of the first draft: in how many observations it was seen and the window it used in each; for every
    input of the first draft: the value it had in each observation (``varies`` when they differ, ``constant`` when they
    are equal, ``masked`` when a secret hid it). Steps of the other observations with no counterpart are branches. Nothing
    is inferred beyond that: two observations that never vary say nothing about the values they did not show."""
    drafts = [check_draft(d) for d in drafts]
    if len(drafts) < 2:
        raise ScreenRecipeError("screen.bad_observations", "comparing needs at least two drafts of the procedure")
    ref = [s for s in drafts[0]["steps"] if isinstance(s, Mapping)]
    ref_sigs = [_sig(s) for s in ref]
    aligned: List[Dict[int, Mapping[str, Any]]] = []      # per other draft: ref index -> its step
    branches: List[Dict[str, Any]] = []
    for k, other in enumerate(drafts[1:], start=2):
        steps = [s for s in other["steps"] if isinstance(s, Mapping)]
        sm = difflib.SequenceMatcher(None, ref_sigs, [_sig(s) for s in steps], autojunk=False)
        to_b: Dict[int, int] = {}
        for block in sm.get_matching_blocks():
            for j in range(block.size):
                to_b[block.a + j] = block.b + j
        aligned.append({a: steps[b] for a, b in to_b.items()})
        used = set(to_b.values())
        for j, s in enumerate(steps):
            if j in used:
                continue
            after = max((a for a, b in to_b.items() if b < j), default=-1)
            branches.append({"observation": k, "kind": s.get("kind"), "app": s.get("app"), "title": s.get("title"),
                             "after_step": ref[after].get("n") if after >= 0 else None})
    n_obs = len(drafts)
    out_steps, windows, inputs = [], [], []
    for idx, s in enumerate(ref):
        seen = 1 + sum(1 for pairs in aligned if idx in pairs)
        wins = [s.get("window")] + [pairs[idx].get("window") for pairs in aligned if idx in pairs]
        out_steps.append({"n": s.get("n"), "title": s.get("title"), "kind": s.get("kind"), "app": s.get("app"), "seen_in": seen})
        if s.get("window") and seen > 1:
            status = "constant" if len({str(w) for w in wins}) == 1 else "varies"
            windows.append({"name": f"window_step_{s.get('n')}", "step": s.get("n"), "values": wins, "status": status})
        for pos, name in enumerate(s.get("inputs") or []):
            values = [_input_value(drafts[0], name)]
            for k, pairs in enumerate(aligned, start=1):
                if idx in pairs:
                    other_names = pairs[idx].get("inputs") or []
                    values.append(_input_value(drafts[k], other_names[pos]) if pos < len(other_names) else None)
            known = [v for v in values if v not in (None, MASK)]
            status = "masked" if MASK in values else "varies" if len({str(v) for v in known}) > 1 else "constant" if len(known) > 1 else "single"
            inputs.append({"name": name, "step": s.get("n"), "values": values, "status": status})
    shapes = [d.get("shape") for d in drafts]
    limits = [f"Learned from {n_obs} observations of the same procedure: only the values that changed between them are "
              "parameters; values that stayed the same are kept as they were seen."]
    limits += [f"Step {s['n']} was seen in {s['seen_in']} of {n_obs} observations (optional, or a branch): {s['title']}"
               for s in out_steps if s["seen_in"] < n_obs]
    limits += [f"Observation {b['observation']} has a step the first one does not ({b['title']}, after step {b['after_step']}): "
               "a branch the recipe does not take." for b in branches]
    limits += [f"Input {i['name']} (step {i['step']}) was masked in at least one observation, so whether it changes is unknown."
               for i in inputs if i["status"] == "masked"]
    return {"observations": n_obs, "shapes": shapes, "same_shape": len(set(shapes)) == 1 and not branches,
            "steps": out_steps, "windows": windows, "inputs": inputs, "branches": branches,
            "parameters": [i["name"] for i in inputs if i["status"] == "varies"] + [w["name"] for w in windows if w["status"] == "varies"],
            "constants": [i["name"] for i in inputs if i["status"] == "constant"] + [w["name"] for w in windows if w["status"] == "constant"],
            "limits": limits}


# ---------------------------------------------------------------------------
# suggested step -> tool mapping (a proposal a person confirms)
# ---------------------------------------------------------------------------

_FILE_RE = re.compile(r"([^\\/:*?\"<>|\s][^\\/:*?\"<>|]*?\.(xlsx|xlsm|xls|ods|csv|tsv|txt|md|json|log|xml|html?|docx|doc|odt|rtf))\b", re.I)
_APP_PREFIX_RE = re.compile(r"^(?:microsoft\s+)?(?:excel|word|libreoffice\s+(?:calc|writer)|wps\s+\w+|notepad\+*|bloc de notas|"
                            r"visual studio code|sublime text)\s+[-–—]\s+", re.I)
_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
_CELL_RE = re.compile(r"^[A-Za-z]{1,3}[1-9][0-9]{0,6}$")
_APPS = {
    "sheet": ("excel", "libreoffice calc", "localc", "google sheets", "numbers", "hoja de c"),
    "doc": ("word", "writer", "google docs", "pages"),
    "text": ("notepad", "bloc de notas", "textedit", "gedit", "visual studio code", "vs code", "sublime", "editor de texto", "kate"),
    "browser": ("chrome", "edge", "firefox", "safari", "opera", "brave", "navegador", "browser"),
}
_EXT_FAMILY = {".xlsx": "sheet", ".xlsm": "sheet", ".xls": "sheet", ".ods": "sheet", ".csv": "text", ".tsv": "text",
               ".txt": "text", ".md": "text", ".json": "text", ".log": "text", ".xml": "text", ".html": "text", ".htm": "text",
               ".docx": "doc", ".doc": "doc", ".odt": "doc", ".rtf": "doc"}


def file_in(window: Any) -> str:
    """The file a window title names ('Facturas.xlsx - Excel' -> 'Facturas.xlsx'), or ''."""
    m = _FILE_RE.search(str(window or ""))
    if not m:
        return ""
    name = m.group(1).strip().lstrip("●•○◦·*+ ")           # an editor's unsaved-changes marker is not part of the name
    for _ in range(3):                                         # 'Microsoft Excel - Facturas.xlsx': the app name comes first
        shorter = _APP_PREFIX_RE.sub("", name, count=1)
        if shorter == name:
            break
        name = shorter
    return name.strip()


def app_family(app: Any, window: Any) -> str:
    """sheet, doc, text, browser or other, from the app name first and the window's file second."""
    a = str(app or "").lower()
    for fam, names in _APPS.items():
        if any(n in a for n in names):
            return fam
    f = file_in(window)
    if f:
        return _EXT_FAMILY.get(os.path.splitext(f)[1].lower(), "other")
    return "browser" if _URL_RE.search(str(window or "")) else "other"


def _tools_available() -> Optional[set]:
    try:
        from src.agent_tools import TOOL_HANDLERS
        return set(TOOL_HANDLERS)
    except Exception:  # noqa: BLE001 - without the registry every suggestion stands
        return None


def _step_value(draft: Mapping[str, Any], step: Mapping[str, Any]) -> Dict[str, Any]:
    """The value a writing step put on screen: its first input, else the value it transferred; masked values stay masked."""
    names = step.get("inputs") or []
    for name in names:
        for i in draft.get("inputs") or []:
            if isinstance(i, Mapping) and i.get("name") == name:
                masked = bool(i.get("masked")) or i.get("example") == MASK
                return {"name": name, "value": None if masked else i.get("example"), "masked": masked,
                        "secret": i.get("kind") == "secret", "label": i.get("label")}
    for o in draft.get("outputs") or []:
        if isinstance(o, Mapping) and o.get("step") == step.get("n") and o.get("kind") == "transferred_value":
            masked = bool(o.get("masked")) or o.get("value") == MASK
            return {"name": None, "value": None if masked else o.get("value"), "masked": masked, "secret": False, "label": None}
    return {"name": None, "value": None, "masked": False, "secret": False, "label": None}


def _suggest_step(draft: Mapping[str, Any], step: Mapping[str, Any], read: set, locale: str,
                  wrote: Optional[set] = None) -> Dict[str, Any]:
    wrote = set() if wrote is None else wrote
    n, kind = step.get("n"), str(step.get("kind") or "")
    fam = app_family(step.get("app"), step.get("window"))
    f = file_in(step.get("window")) or file_in(step.get("path"))
    ext = os.path.splitext(f)[1].lower()
    app = step.get("app") or "?"

    def out(action: str, why: str, tool: Optional[str] = None, args: Optional[Dict[str, Any]] = None,
            confidence: str = "low") -> Dict[str, Any]:
        return {"step": n, "action": action, "tool": tool, "args": args or {}, "confidence": confidence, "why": why,
                "app_family": fam, "file": f or None}

    if kind in ("app_switch", "observe", "copy"):
        if fam == "sheet" and f and f not in read:
            read.add(f)
            if ext in (".csv", ".tsv"):
                return out("map", f"{f} was open in {app}: read it.", "read_file", {"path": f}, "medium")
            return out("map", f"{f} was open in {app}: read the workbook.", "manage_spreadsheet",
                       {"action": "read_workbook", "path": f}, "medium")
        if fam == "text" and f and kind == "observe" and f not in read:
            read.add(f)
            return out("map", f"{f} was looked at in {app}: read it.", "read_file", {"path": f}, "medium")
        if fam == "browser" and kind in ("app_switch", "observe"):
            url = _URL_RE.search(str(step.get("window") or ""))
            if url:
                return out("map", "A page was open in the browser: extract it.", "browser_extract", {"url": url.group(0)}, "low")
            return out("skip", "The page address is not visible in the recording; open it by hand or give its URL.")
        if kind == "copy":
            return out("skip", "The copied value is carried by the step that pastes it.")
        if kind == "observe" and fam == "other":
            return out("skip", "Only a picture of the screen: look at its screenshot to decide which tool, if any, repeats this step.")
        return out("skip", "Switching window is navigation: the step that reads or writes does the work.")
    if kind in ("copy_paste", "paste", "type_text", "fill_form"):
        val = _step_value(draft, step)
        if val["secret"]:
            return out("manual", "A secret was typed here; a recipe never keeps it, so this step stays manual.")
        content = val["value"] if val["value"] not in (None, "") else f"<{val['name'] or 'value'}>"
        masked_note = " The value was masked in the recording: write the value of this case." if val["masked"] or val["value"] in (None, "") else ""
        conf = "low" if masked_note else "medium"
        if fam == "text" and f:
            if ("file", f.lower()) in wrote:   # write_file replaces the whole file: a second write would erase the first
                return out("manual", f"{f} is already written by an earlier step and write_file replaces the whole file; put "
                                     "all the typed text in one write (edit that step's content) instead of a second write.")
            wrote.add(("file", f.lower()))
            return out("map", f"Text was written into {f} in {app}: write the file." + masked_note, "write_file",
                       {"path": f, "content": str(content)}, conf)
        if fam == "sheet" and f and ext not in (".csv", ".tsv"):
            label = str(val["label"] or "")
            cell = label if _CELL_RE.match(label) else "A1"
            sheet = "Hoja1" if locale == "es" else "Sheet1"
            if ("cell", f.lower(), cell.upper()) in wrote:
                return out("manual", f"{f} already gets a value in {cell.upper()} from an earlier step and the recording does "
                                     "not show a different cell for this one; give its sheet and cell to map it.")
            wrote.add(("cell", f.lower(), cell.upper()))
            return out("map", f"A value was typed into {f} in {app}: write it into the workbook. The recording does not show the "
                              f"sheet or the cell; confirm sheet_name and start_cell." + masked_note, "manage_spreadsheet",
                       {"action": "write_range", "path": f, "sheet_name": sheet, "start_cell": cell.upper(), "values": [[content]]}, "low")
        if fam == "doc":
            return out("manual", "Text was typed into a word-processor document; no Faustus tool edits a .docx in place, so this "
                                 "step stays manual (or write a text or Markdown file instead).")
        if fam == "browser":
            return out("manual", "Typing into a web page stays manual: a recipe does not fill in or send forms on websites.")
        return out("manual", f"Writing in {app} has no matching tool.")
    if kind == "file":
        return out("skip", "Saving is done by the tool of the step that writes the file.")
    if kind in ("click", "submit"):
        return out("manual", "A click or a submit seen on screen has no tool equivalent here; it stays manual.")
    return out("manual", f"A step of kind {kind or '?'} has no suggestion.")


def suggest_mapping(draft: Mapping[str, Any]) -> Dict[str, Any]:
    """Proposed tool for every step from the app and the kind of step seen (Excel -> manage_spreadsheet, Notepad and
    other editors -> read_file/write_file, the browser -> browser_extract; clicks, submits, secrets and word-processor
    typing stay manual). Each proposal says why and how sure it is; nothing is applied until a person confirms it."""
    draft = check_draft(draft)
    available = _tools_available()
    read: set = set()
    wrote: set = set()
    locale = str(draft.get("locale") or "es")
    rows = []
    for step in draft["steps"]:
        if not isinstance(step, Mapping):
            continue
        row = _suggest_step(draft, step, read, locale, wrote)
        if row["action"] == "map" and available is not None and row["tool"] not in available:
            row.update(action="manual", why=f"{row['tool']} is not available in this Faustus; {row['why']}", tool=None, args={})
        rows.append(row)
    mapping = [{"step": r["step"], "tool": r["tool"], "args": r["args"]} for r in rows if r["action"] == "map"]
    return {"suggestions": rows, "mapping": mapping,
            "counts": {a: sum(1 for r in rows if r["action"] == a) for a in ("map", "skip", "manual")},
            "note": "Proposals from the app and the kind of each step. Confirm or change them before making the recipe; "
                    "file names come from window titles and are relative to the workspace where the recipe runs."}


# ---------------------------------------------------------------------------
# Baird's Hoard as a source: pictures of another computer captured over HDMI
# ---------------------------------------------------------------------------

BAIRD_APP = "Other computer (HDMI)"


def _when(text: str, now: datetime) -> Optional[datetime]:
    s = str(text or "").strip()
    if not s:
        return None
    m = re.fullmatch(r"-(\d+(?:\.\d+)?)\s*([smhd])", s)
    if m:
        unit = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[m.group(2)]
        return now - timedelta(**{unit: float(m.group(1))})
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise ScreenRecipeError("screen.bad_range", f"{s!r} is not a time: use ISO (2026-11-04T10:00) or -30m, -2h, -1d") from None
    return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt


def baird_events(*, start: str = "", end: str = "", limit: int = 200, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Screenshots Baird's Hoard saved of the other computer in a time range, as events in the shape Talos segments
    (``type: screenshot``, the picture's name as reference). Baird only captures the picture of that computer: there are
    no window titles, keystrokes or clipboard, and nothing is ever done on it."""
    now = now or datetime.now()
    lo, hi = _when(start or "-1h", now), _when(end, now) or now
    res = _hub_call("baird", "media_list", {"kind": "all", "limit": max(1, min(int(limit or 200), 500))})
    if not res["ok"]:
        raise ScreenRecipeError("screen.baird_unavailable", f"Baird's Hoard did not answer: {res['error']}. Start it from the Hub "
                                                            "(it captures the other computer through the HDMI capture device).")
    items = res["result"].get("items") if isinstance(res["result"], Mapping) else None
    shots, clips = [], 0
    for it in items or []:
        try:
            when = datetime.fromisoformat(str(it.get("modified")))
        except (TypeError, ValueError):
            continue
        if not (lo <= when <= hi):
            continue
        if it.get("kind") == "image":
            shots.append({"ts": when.isoformat(timespec="seconds"), "type": "screenshot", "app": BAIRD_APP,
                          "title": "", "ref": str(it.get("name") or "")})
        elif it.get("kind") == "video":
            clips += 1
    shots.sort(key=lambda e: e["ts"])
    if not shots:
        raise ScreenRecipeError("screen.baird_empty", f"Baird's Hoard has no screenshots between {lo:%Y-%m-%d %H:%M} and "
                                                      f"{hi:%Y-%m-%d %H:%M}. Take screenshots of the other computer while the "
                                                      "procedure is done (Baird: Screenshot, or snapshot_save) and ask again.",
                                {"clips_in_range": clips})
    return {"events": shots, "clips_in_range": clips, "range": [lo.isoformat(timespec="seconds"), hi.isoformat(timespec="seconds")]}


def mark_baird(draft: Dict[str, Any], clips: int = 0) -> Dict[str, Any]:
    """Say in the draft that it came from HDMI pictures of another computer, and what that cannot show."""
    draft = dict(draft)
    draft["observed_with"] = "baird"
    unknowns = list(draft.get("unknowns") or [])
    unknowns.append({"code": "hdmi_pictures_only", "step": None,
                     "message": "Seen through Baird's HDMI capture of another computer: only pictures, no window titles, "
                                "keystrokes or clipboard; what each step did must be read from its screenshot."})
    if clips:
        unknowns.append({"code": "clips_not_segmented", "step": None,
                         "message": f"{clips} clip(s) recorded in the same range were not segmented into steps."})
    draft["unknowns"] = unknowns
    return draft


def fetch_baird_draft(*, start: str = "", end: str = "", title: str = "", locale: str = "es") -> Dict[str, Any]:
    got = baird_events(start=start, end=end)
    try:
        draft = fetch_draft(events=got["events"], title=title, locale=locale)
    except ScreenRecipeError as exc:
        raise ScreenRecipeError(exc.error_class, exc.detail + f" The {len(got['events'])} screenshot event(s) from Baird are in "
                                                              "`events`: paste them as a draft source once Talos can segment them.",
                                {"events": got["events"], "clips_in_range": got["clips_in_range"]}) from None
    return mark_baird(draft, got["clips_in_range"])
