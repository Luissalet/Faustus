"""Seven things to settle before writing code, and a schema that enforces them.

The idea comes from a spec-first methodology surveyed on 22-09 (see
``docs/radar/2026-09-22.md``): before generating code, declare the requirements,
the entities, the approach, the structure, the operations, the norms and the
safeguards. Good discipline -- and in the original, nothing but discipline: a
markdown template, a few commands that hand it to the model, and no mechanism
anywhere that checks a section was actually filled in. A model that skips
"safeguards" gets no complaint from anyone.

Here the seven dimensions are a JSON Schema instead of a template, so the
constrained decoding of OBJ-27 makes them structural: the model physically
cannot emit a token that leaves one out, because the grammar has no path to the
closing brace until every field is present. That is the whole difference
between a convention and a guarantee.

The filled canvas is stored as a concept in the project graph, with the files
from `structure` as its refs. That buys staleness for free: the moment the
design names a file that no longer exists, `stale_check` marks the concept
obsolete without anyone maintaining it -- an old design that has drifted from
the code says so by itself.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


class DesignCanvasError(Exception):
    """Raised when a canvas cannot be produced or does not hold up."""


# ---------------------------------------------------------------------------
# The seven dimensions
# ---------------------------------------------------------------------------
#
# Each one is (key, heading, what the model is asked for). The prompt is built
# from this table and so is the schema, so the two can never drift apart -- a
# dimension added here appears in both without touching anything else.

DIMENSIONS: Tuple[Tuple[str, str, str], ...] = (
    ("requirements", "Requirements",
     "What must be true once this is done. Observable statements, not tasks. "
     "Each one has to be checkable by someone who did not write the code."),
    ("entities", "Entities",
     "The named things this introduces or changes -- records, settings, "
     "events, states -- and the data each one carries."),
    ("approach", "Approach",
     "How it works, in a few sentences, and the alternative that was "
     "rejected with the reason. One of these must be a rejected option."),
    ("structure", "Structure",
     "The files and modules to create or change, by path. Existing paths "
     "must be real; new ones must sit where their neighbours already live."),
    ("operations", "Operations",
     "The functions, endpoints or commands this adds, each with what it "
     "takes, what it returns and what it does when it fails."),
    ("norms", "Norms",
     "The conventions of THIS repository this has to respect -- the way its "
     "neighbours are written, tested and wired in."),
    ("safeguards", "Safeguards",
     "What can break, and what stops it. Each entry pairs a failure with the "
     "thing that prevents or catches it; a risk with no guard is not a "
     "safeguard."),
)

#: `approach` is prose; the rest are lists, because a list of one vague
#: sentence is easy to spot and a paragraph of hedging is not.
_PROSE = {"approach"}

#: Below this, an entry is padding rather than an answer.
MIN_ENTRY_CHARS = 24
MIN_PROSE_CHARS = 120

#: How many real entries a dimension needs. One is the floor everywhere,
#: because a small change can honestly introduce a single entity or a single
#: operation -- the first run of this against the local model was rejected for
#: exactly that, on a change whose one entity was the right answer. Two stand
#: where one is the tell: a design with a single requirement is a task, and a
#: single safeguard means the second failure mode was never looked for.
_MIN_ENTRIES = {"requirements": 2, "safeguards": 2}
_DEFAULT_MIN_ENTRIES = 1

#: Answers that fill the shape while saying nothing. The grammar guarantees a
#: string is there; it cannot guarantee the string means anything, so this is
#: the second line of defence and it runs after every generation.
_EMPTY_PATTERNS = (
    r"^n/?a$", r"^none$", r"^tbd$", r"^todo$", r"^pending$", r"^unknown$",
    r"^not applicable$", r"^\W*$", r"^see above$", r"^as described$",
    r"^\.\.\.$", r"^same as", r"^to be (determined|defined|decided)",
)
_EMPTY_RE = re.compile("|".join(_EMPTY_PATTERNS), re.IGNORECASE)


def canvas_schema() -> Dict[str, Any]:
    """The JSON Schema that goes on the wire.

    Every dimension is required and `additionalProperties` is false, so a
    backend that decodes under this schema cannot return a partial canvas and
    cannot invent an eighth section to hide in.

    Shape only, deliberately. The first version of this carried `minItems` and
    `minLength` as well, which is the obvious thing to write and does not
    work: llama-server compiles the schema to a grammar, and the floors turn a
    simple state machine into one the decoder spent nearly two minutes in
    before returning an empty string with HTTP 200 -- a failure that looks
    like a healthy answer. A grammar is for the shape it can actually
    enforce; `validate()` owns the floors, where they can be checked properly
    and reported field by field. That division is also the honest one: no
    grammar can tell a filled field from a meant one.
    """
    properties: Dict[str, Any] = {}
    for key, _heading, _ask in DIMENSIONS:
        if key in _PROSE:
            properties[key] = {"type": "string"}
        else:
            properties[key] = {"type": "array", "items": {"type": "string"}}
    return {
        "type": "object",
        "properties": properties,
        "required": [key for key, _h, _a in DIMENSIONS],
        "additionalProperties": False,
    }


def build_prompt(goal: str, *, context: str = "") -> List[Dict[str, str]]:
    """Messages that ask for the canvas and nothing else."""
    goal = str(goal or "").strip()
    if not goal:
        raise DesignCanvasError("a design canvas needs a goal")
    asks = "\n".join(
        f"- {key}: {ask}" for key, _heading, ask in DIMENSIONS
    )
    system = (
        "You are settling the design of a change before any code is written. "
        "Answer with the seven fields below and nothing else.\n\n"
        f"{asks}\n\n"
        "Be specific to this codebase. A generic answer that would fit any "
        "project is a wrong answer. Write what you actually intend to do, not "
        "what sounds thorough: this is read before the work, and again after "
        "it, to check the work matches."
    )
    user = f"The change to design:\n\n{goal}"
    if context.strip():
        user += f"\n\nWhat is already known about this codebase:\n\n{context.strip()}"
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


def validate(canvas: Any) -> Dict[str, Any]:
    """Return the canvas, normalised, or raise with what is wrong.

    The schema is enforced by the decoder, but only for shape. This checks the
    part a grammar cannot: that the strings say something. It runs on every
    canvas, including one a caller built by hand or one that came back from a
    backend with no constrained decoding at all.
    """
    if not isinstance(canvas, dict):
        raise DesignCanvasError("a canvas is an object with the seven fields")

    out: Dict[str, Any] = {}
    problems: List[str] = []
    for key, heading, _ask in DIMENSIONS:
        value = canvas.get(key)
        if key in _PROSE:
            text = str(value or "").strip()
            if len(text) < MIN_PROSE_CHARS or _EMPTY_RE.match(text):
                problems.append(f"{heading} is empty or says nothing")
            out[key] = text
            continue
        if isinstance(value, str):
            # A model that answered one field as prose when the rest are
            # lists: take the lines rather than fail the whole canvas.
            value = [line.strip(" -*\t") for line in value.splitlines()]
        if not isinstance(value, (list, tuple)):
            problems.append(f"{heading} must be a list")
            out[key] = []
            continue
        entries = [str(item).strip() for item in value if str(item).strip()]
        entries = [e for e in entries
                   if len(e) >= MIN_ENTRY_CHARS and not _EMPTY_RE.match(e)]
        floor = _MIN_ENTRIES.get(key, _DEFAULT_MIN_ENTRIES)
        if len(entries) < floor:
            word = "two real entries" if floor > 1 else "one real entry"
            problems.append(f"{heading} needs at least {word}")
        out[key] = entries

    if problems:
        raise DesignCanvasError("; ".join(problems))
    return out


def referenced_paths(canvas: Dict[str, Any]) -> List[str]:
    """The file paths named in `structure`, for the concept's refs.

    Deliberately narrow: a file extension is required. The first live run
    produced "token/latency" out of a sentence about measuring tokens and
    latency, which would have become a permanently broken ref in the graph --
    a slash alone is not evidence of a path. A directory named in prose
    contributes nothing, which is the right trade: a missing ref costs a
    little recall, an invented one costs trust in every staleness report.
    """
    found: List[str] = []
    pattern = re.compile(r"[\w./\\-]*[\w-]+\.[A-Za-z][A-Za-z0-9]{0,4}\b")
    for entry in canvas.get("structure") or []:
        for match in pattern.findall(str(entry)):
            ref = match.strip(".,;:()[]`'\"")
            if ref and ref not in found:
                found.append(ref)
    return found


def render(canvas: Dict[str, Any]) -> str:
    """The canvas as markdown, for the concept's `details`."""
    parts: List[str] = []
    for key, heading, _ask in DIMENSIONS:
        value = canvas.get(key)
        parts.append(f"## {heading}\n")
        if key in _PROSE:
            parts.append(f"{value}\n")
        else:
            for entry in value or []:
                parts.append(f"- {entry}")
            parts.append("")
    return "\n".join(parts).strip() + "\n"


def summarise(canvas: Dict[str, Any], goal: str) -> str:
    """One line for the concept's `summary`: the goal plus its shape."""
    goal = " ".join(str(goal or "").split())
    if len(goal) > 160:
        goal = goal[:157].rstrip() + "…"
    counts = ", ".join(
        f"{len(canvas.get(key) or [])} {heading.lower()}"
        for key, heading, _ask in DIMENSIONS if key not in _PROSE
    )
    return f"{goal} — {counts}"


def parse_response(text: str) -> Dict[str, Any]:
    """Read a canvas out of a model answer.

    With constrained decoding the answer is already exactly the object. Without
    it -- a hosted endpoint, or the setting turned off -- it may be an object
    wrapped in prose, so the first balanced object is taken. Anything else
    raises rather than guessing, because a half-read canvas would be stored as
    if it were a design.
    """
    raw = str(text or "").strip()
    if not raw:
        raise DesignCanvasError("the model returned nothing")
    try:
        return validate(json.loads(raw))
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    while start != -1:
        depth, in_string, escaped = 0, False, False
        for index in range(start, len(raw)):
            char = raw[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return validate(json.loads(raw[start:index + 1]))
                    except json.JSONDecodeError:
                        break
        start = raw.find("{", start + 1)
    raise DesignCanvasError("no canvas object in the answer")
