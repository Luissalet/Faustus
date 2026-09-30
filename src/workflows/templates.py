"""
workflows/templates.py — starting points a person fills in, not workflows that run themselves.

A template is a workflow definition with a few **parameters** (a folder, a
ceiling, a label) that cannot be left to run-time inputs, either because a node
reads them literally (`wait_for_event` takes its folder from `config`, and a
template language in a path would be an execution surface) or because they are
the kind of decision a person should make once, on purpose, when they set it
up. Filling one in returns an ordinary, validated `WorkflowDefinition`; saving,
publishing or running it is a separate, explicit step, exactly as for one
written by hand.

Parameters are substituted into the template as `@@name@@`. A string that is
*only* a placeholder takes the parameter's own type (a number stays a number);
a placeholder inside longer text is replaced as text. Every parameter is
checked before anything is built (type, bounds, and for a folder: absolute, no
`..`, and — for the folder pair a watch template reads and writes — not the
same folder, and the output not inside what is watched, because a workflow that
writes where it listens starts itself forever).

Nothing here reaches outside: listing, filling and validating touch no file and
call no model. The definitions use only node types and tools that exist:
`wait_for_event` with the `file_change` source, `agent` nodes with an explicit
tool list, `classify`, `guard`, `loop` and `artifact_store`.
"""
from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from src.contracts import ContractError, WorkflowDefinition

__all__ = ["Template", "TemplateError", "list_templates", "get_template", "instantiate"]


class TemplateError(ValueError):
    """The parameters cannot fill this template; `problems` lists every reason."""

    def __init__(self, message: str, problems: Optional[List[str]] = None):
        super().__init__(message)
        self.problems = list(problems or [])


@dataclass(frozen=True)
class Parameter:
    name: str
    label: str
    kind: str                                   # folder | text | integer | number
    help: str = ""
    required: bool = True
    default: Any = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    max_length: int = 500


@dataclass(frozen=True)
class Template:
    id: str
    title: str
    description: str
    category: str
    parameters: Tuple[Parameter, ...]
    body: Mapping[str, Any]
    #: Names of two folder parameters (watched, written) that must not overlap.
    folder_pair: Optional[Tuple[str, str]] = None
    #: Pairs of text parameters that must differ (two branches with one name
    #: are one branch).
    distinct: Tuple[Tuple[str, str], ...] = ()
    notes: Tuple[str, ...] = field(default_factory=tuple)

    def describe(self) -> Dict[str, Any]:
        return {"id": self.id, "title": self.title, "description": self.description, "category": self.category,
                "notes": list(self.notes),
                "parameters": [{"name": p.name, "label": p.label, "kind": p.kind, "help": p.help,
                                "required": p.required, "default": p.default, "minimum": p.minimum,
                                "maximum": p.maximum} for p in self.parameters],
                "nodes": [{"id": n["id"], "type": n["type"]} for n in self.body["nodes"]],
                "inputs": dict(self.body.get("inputs") or {})}


# ── the templates ─────────────────────────────────────────────────────────

_PDF_PROMPT = (
    "New or changed PDF files were detected in the watched folder:\n{{ results.watch.events | json }}\n\n"
    "Apply these operations to each of them with the `pdf_ops` tool: {{ inputs.operations }}\n\n"
    "Rules: never modify or delete an original; write every result into the folder @@output_dir@@ and "
    "nowhere else; give each output a name that keeps the original's name; if an operation cannot be "
    "done to a file, skip that file and say why. Answer with a JSON object "
    "{\"files\": [paths written], \"skipped\": [{\"file\": path, \"reason\": text}], \"summary\": text}.")

_PDF_SCHEMA = {
    "type": "object", "required": ["files", "summary"],
    "properties": {
        "files": {"type": "array", "items": {"type": "string"}},
        "skipped": {"type": "array", "items": {"type": "object", "required": ["file", "reason"],
                                               "properties": {"file": {"type": "string"},
                                                              "reason": {"type": "string"}}}},
        "summary": {"type": "string"}}}

PDF_FOLDER_BATCH = Template(
    id="pdf-folder-batch",
    title="Watched folder, PDF operations, output folder",
    description=("Waits for PDFs to appear in one folder, applies the operations you describe to them "
                 "(merge, split, rotate, compress, watermark, extract pages...) with the pdf_ops tool, "
                 "writes the results into another folder and stores a report. One run handles one settled "
                 "batch of files; start a run per batch (for example from a schedule)."),
    category="data",
    parameters=(
        Parameter("watch_dir", "Folder to watch", "folder", "Absolute path of the folder the PDFs arrive in."),
        Parameter("output_dir", "Output folder", "folder", "Absolute path results are written to. It must be a "
                  "different folder, and not inside the watched one."),
        Parameter("pattern", "File pattern", "text", "Which files count, as a file-name pattern.", False, "*.pdf", max_length=80),
        Parameter("settle_seconds", "Quiet time before processing (seconds)", "integer",
                  "How long nothing new may arrive before the batch is handed over; a file still being copied "
                  "is not picked up half written.", False, 10, 1, 3600),
        Parameter("timeout_minutes", "Give up after (minutes)", "integer",
                  "How long one run waits for files before it ends.", False, 60, 1, 10080),
    ),
    folder_pair=("watch_dir", "output_dir"),
    notes=("Files are found by comparing modification times every few seconds; what was already in the folder "
           "when the run started is not a new file.",
           "The operations are carried out by an agent turn restricted to the pdf_ops tool. If pdf_ops is set "
           "to ask for approval in your configuration, the turn stops and the run fails; allow it for this "
           "folder first.",
           "Publishing this as a tool is possible: it declares one input, `operations`."),
    body={
        "id": "pdf.folder-batch", "version": "1.0.0", "title": "PDF folder batch",
        "description": "Watch a folder for PDFs, apply PDF operations, write to an output folder.",
        "inputs": {"type": "object", "required": ["operations"],
                   "properties": {"operations": {"type": "string", "minLength": 3, "maxLength": 2000,
                                                 "description": "What to do to each PDF, in plain words, "
                                                                "e.g. 'compress, then add the watermark DRAFT'."}}},
        "nodes": [
            {"id": "start", "type": "manual", "title": "Start"},
            {"id": "watch", "type": "wait_for_event", "needs": ["start"], "title": "Wait for PDFs",
             "config": {"source": "file_change", "path": "@@watch_dir@@", "pattern": "@@pattern@@",
                        "settle_ms": "@@settle_ms@@", "timeout_ms": "@@timeout_ms@@", "poll_ms": 2000,
                        "max_events": 100}},
            {"id": "convert", "type": "agent", "needs": ["watch"], "title": "Apply the operations",
             "config": {"prompt": _PDF_PROMPT, "tools": ["pdf_ops"], "output_schema": _PDF_SCHEMA,
                        "max_rounds": 20, "timeout_s": 900}},
            {"id": "report", "type": "artifact_store", "needs": ["convert"], "title": "Store the report",
             "config": {"content_from": "results.convert.text", "filename": "pdf-batch-report.json"}},
        ]},
)

_TRIAGE_INPUTS = {"type": "object", "required": ["message"],
                  "properties": {"message": {"type": "string", "minLength": 1, "maxLength": 20000,
                                             "description": "The incoming message to route and answer."}}}
_REPLY_SCHEMA = {"type": "object", "required": ["reply"], "properties": {"reply": {"type": "string"}}}

TRIAGE_AND_REPLY = Template(
    id="triage-and-reply",
    title="Route a message, draft a reply, check it before it leaves",
    description=("Classifies an incoming message into the categories you name, drafts a reply with an agent "
                 "turn (no tools), runs deterministic checks (secrets, personal data, links) on the draft and "
                 "either stores it or holds it for a person."),
    category="model",
    parameters=(
        Parameter("category_a", "First category", "text", "", True, "billing", max_length=40),
        Parameter("category_b", "Second category", "text", "", True, "support", max_length=40),
        Parameter("threshold", "Confidence needed to route", "number",
                  "Below this the message goes to the fallback instead of a guessed category.", False, 0.7, 0, 1),
    ),
    distinct=(("category_a", "category_b"),),
    notes=("The classifier reads one token as a probability; a message it is unsure about takes the second "
           "category rather than a random one.",),
    body={
        "id": "triage.reply", "version": "1.0.0", "title": "Triage and reply",
        "description": "Route a message, draft a reply, guard it.", "inputs": _TRIAGE_INPUTS,
        "nodes": [
            {"id": "start", "type": "manual", "title": "Start"},
            {"id": "route", "type": "classify", "needs": ["start"], "title": "Route",
             "config": {"text": "{{ inputs.message }}", "labels": ["@@category_a@@", "@@category_b@@"],
                        "threshold": "@@threshold@@", "fallback": "@@category_b@@",
                        "question": "Which category does this message belong to?"}},
            {"id": "draft_a", "type": "agent", "needs": ["route"], "branch": {"route": "@@category_a@@"},
             "title": "Draft (first category)",
             "config": {"prompt": "Write a short, polite reply to this message, which is about @@category_a@@:\n"
                                  "{{ inputs.message }}\nAnswer as JSON {\"reply\": text}.",
                        "tools": [], "output_schema": _REPLY_SCHEMA}},
            {"id": "draft_b", "type": "agent", "needs": ["route"], "branch": {"route": "@@category_b@@"},
             "title": "Draft (second category)",
             "config": {"prompt": "Write a short, polite reply to this message, which is about @@category_b@@:\n"
                                  "{{ inputs.message }}\nAnswer as JSON {\"reply\": text}.",
                        "tools": [], "output_schema": _REPLY_SCHEMA}},
            {"id": "check_a", "type": "guard", "needs": ["draft_a"], "title": "Check the draft",
             "config": {"text": "{{ results.draft_a.data.reply }}", "checks": ["secrets", "pii", "injection"]}},
            {"id": "check_b", "type": "guard", "needs": ["draft_b"], "title": "Check the draft",
             "config": {"text": "{{ results.draft_b.data.reply }}", "checks": ["secrets", "pii", "injection"]}},
            {"id": "keep_a", "type": "artifact_store", "needs": ["check_a"], "branch": {"check_a": "pass"},
             "title": "Keep the reply", "config": {"content_from": "results.draft_a.data.reply", "filename": "reply.txt"}},
            {"id": "keep_b", "type": "artifact_store", "needs": ["check_b"], "branch": {"check_b": "pass"},
             "title": "Keep the reply", "config": {"content_from": "results.draft_b.data.reply", "filename": "reply.txt"}},
            {"id": "hold_a", "type": "human_approval", "needs": ["check_a"], "branch": {"check_a": "fail"},
             "title": "A person looks at it", "config": {"action": "review", "detail": "The draft failed a check."}},
            {"id": "hold_b", "type": "human_approval", "needs": ["check_b"], "branch": {"check_b": "fail"},
             "title": "A person looks at it", "config": {"action": "review", "detail": "The draft failed a check."}},
        ]},
)

REFINE_UNTIL_GOOD = Template(
    id="refine-until-good",
    title="Refine a draft until it passes a check, with a ceiling",
    description=("Drafts text with an agent turn, checks it with deterministic guards, and repeats — feeding "
                 "the previous attempt back in — until the check passes or the ceiling is reached."),
    category="model",
    parameters=(
        Parameter("max_iterations", "Most attempts", "integer", "The loop stops here whatever happens.", False, 4, 1, 20),
    ),
    notes=("If the ceiling is reached without a pass, the loop pauses for a person to extend it or stop the run.",),
    body={
        "id": "refine.loop", "version": "1.0.0", "title": "Refine until good",
        "description": "Draft, check, repeat within a ceiling.",
        "inputs": {"type": "object", "required": ["brief"],
                   "properties": {"brief": {"type": "string", "minLength": 1, "maxLength": 5000}}},
        "nodes": [
            {"id": "start", "type": "manual", "title": "Start"},
            {"id": "refine", "type": "loop", "needs": ["start"], "title": "Refine",
             "config": {"body": ["draft", "verify"], "budget": {"max_iterations": "@@max_iterations@@"},
                        "until": {"left": {"path": "loop.results.verify.passed"}, "op": "truthy"}}},
            {"id": "draft", "type": "agent", "needs": ["start"], "title": "Draft",
             "config": {"prompt": "Write the text described here:\n{{ inputs.brief }}\n\nPrevious attempt, if any:\n"
                                  "{{ loop.previous | json }}\nImprove on it. Reply with the text only.", "tools": []}},
            {"id": "verify", "type": "guard", "needs": ["draft"], "title": "Check",
             "config": {"text": "{{ results.draft.text }}", "checks": ["secrets", "pii"]}},
            {"id": "keep", "type": "artifact_store", "needs": ["refine"], "title": "Keep the result",
             "config": {"content_from": "results.refine.results.draft.text", "filename": "result.txt"}},
        ]},
)

TEMPLATES: Dict[str, Template] = {t.id: t for t in (PDF_FOLDER_BATCH, TRIAGE_AND_REPLY, REFINE_UNTIL_GOOD)}


def list_templates() -> List[Dict[str, Any]]:
    return [t.describe() for t in TEMPLATES.values()]


def get_template(template_id: str) -> Optional[Template]:
    return TEMPLATES.get(template_id)


# ── filling one in ────────────────────────────────────────────────────────

_PLACEHOLDER = re.compile(r"@@([a-z_]+)@@")
_CATEGORY_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _\-]{0,39}$")
_PATTERN_OK = re.compile(r"^[\w*?\[\]\-. ]{1,80}$")


def _check(param: Parameter, value: Any) -> Tuple[Any, Optional[str]]:
    if value is None or value == "":
        if param.required and param.default is None:
            return None, f"{param.name}: is required"
        value = param.default
    if param.kind in ("integer", "number"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None, f"{param.name}: must be a number"
        if param.kind == "integer" and (isinstance(value, float) and not value.is_integer()):
            return None, f"{param.name}: must be a whole number"
        if param.minimum is not None and value < param.minimum or param.maximum is not None and value > param.maximum:
            return None, f"{param.name}: must be between {param.minimum:g} and {param.maximum:g}"
        return (int(value) if param.kind == "integer" else float(value)), None
    if not isinstance(value, str) or not value.strip():
        return None, f"{param.name}: must be text"
    value = value.strip()
    if len(value) > param.max_length:
        return None, f"{param.name}: is longer than {param.max_length} characters"
    if "@@" in value:
        return None, f"{param.name}: may not contain '@@'"
    if param.kind == "folder":
        if not (os.path.isabs(value) or re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith("\\\\")):
            return None, f"{param.name}: must be an absolute folder path"
        if ".." in re.split(r"[\\/]+", value):
            return None, f"{param.name}: may not contain '..'"
    if param.name.startswith("category_") and not _CATEGORY_OK.match(value):
        return None, f"{param.name}: may only use letters, digits, spaces, `_` and `-`"
    if param.name == "pattern" and not _PATTERN_OK.match(value):
        return None, f"{param.name}: may only use letters, digits, `* ? [ ] - . _` and spaces"
    return value, None


def _norm(path: str) -> str:
    return re.sub(r"[\\/]+", "/", path).rstrip("/").lower()


def _fill(node: Any, values: Mapping[str, Any]) -> Any:
    if isinstance(node, str):
        whole = _PLACEHOLDER.fullmatch(node)
        if whole and whole.group(1) in values:
            return values[whole.group(1)]
        return _PLACEHOLDER.sub(lambda m: str(values.get(m.group(1), m.group(0))), node)
    if isinstance(node, list):
        return [_fill(x, values) for x in node]
    if isinstance(node, Mapping):
        return {k: _fill(v, values) for k, v in node.items()}
    return node


def instantiate(template_id: str, parameters: Optional[Mapping[str, Any]] = None) -> WorkflowDefinition:
    """The validated definition for `template_id` with `parameters` filled in.
    Raises :class:`TemplateError` naming every parameter problem at once."""
    template = TEMPLATES.get(template_id)
    if template is None:
        raise TemplateError(f"no template named {template_id!r}")
    given = dict(parameters or {})
    unknown = sorted(set(given) - {p.name for p in template.parameters})
    problems: List[str] = [f"{name}: is not a parameter of this template" for name in unknown]
    values: Dict[str, Any] = {}
    for param in template.parameters:
        value, problem = _check(param, given.get(param.name))
        if problem:
            problems.append(problem)
        else:
            values[param.name] = value
    if template.folder_pair and not problems:
        watched, written = (_norm(values[n]) for n in template.folder_pair)
        if watched == written:
            problems.append(f"{template.folder_pair[1]}: must not be the folder being watched")
        elif written.startswith(watched + "/") or watched.startswith(written + "/"):
            problems.append(f"{template.folder_pair[1]}: the watched and output folders must not contain each other, "
                            "or the workflow would keep finding its own output")
    for first, second in template.distinct:
        if first in values and second in values and str(values[first]).lower() == str(values[second]).lower():
            problems.append(f"{second}: must differ from {first}")
    if problems:
        raise TemplateError("the template was not filled in: " + " | ".join(problems[:6]), problems)
    if "settle_seconds" in values:
        values["settle_ms"] = values["settle_seconds"] * 1000
    if "timeout_minutes" in values:
        values["timeout_ms"] = values["timeout_minutes"] * 60_000
    body = _fill(copy.deepcopy(dict(template.body)), values)
    try:
        return WorkflowDefinition.parse(body)
    except ContractError as exc:                      # a template that no longer validates is a bug worth naming
        raise TemplateError(f"template {template_id!r} does not validate: {exc.path}: {exc.message}")
