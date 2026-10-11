"""The shape of an observation and of a turn summary, and the parser that
refuses anything else.

The small model is asked for ONE JSON object. What comes back is untrusted
text: it may be wrapped in a code fence, preceded by reasoning, truncated, or
not JSON at all. :func:`parse_observation_reply` returns an :class:`Observation`
(valid), a :class:`Skip` (the model judged the call noise) or ``None`` (garbage:
the call is dropped, never stored half-way). Same for
:func:`parse_summary_reply`.

Nothing here touches the database, the network or the settings.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

TYPES = ("bugfix", "feature", "refactor", "change", "discovery", "decision", "other")
CONCEPTS = ("how-it-works", "why-it-exists", "gotcha", "pattern", "trade-off", "problem-solution")

MAX_TITLE = 140
MAX_SUBTITLE = 220
MAX_FACTS = 12
MAX_FACT = 300
MAX_NARRATIVE = 700
MAX_FILES = 20
MAX_PATH = 400
MAX_REASON = 120

SUMMARY_FIELDS = ("request", "investigated", "learned", "completed", "next_steps")
MAX_SUMMARY_FIELD = 600


@dataclass
class Observation:
    type: str
    title: str
    subtitle: str = ""
    facts: List[str] = field(default_factory=list)
    narrative: str = ""
    concepts: List[str] = field(default_factory=list)
    files_read: List[str] = field(default_factory=list)
    files_modified: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type, "title": self.title, "subtitle": self.subtitle,
            "facts": list(self.facts), "narrative": self.narrative,
            "concepts": list(self.concepts), "files_read": list(self.files_read),
            "files_modified": list(self.files_modified),
        }


@dataclass
class Skip:
    reason: str = "noise"


@dataclass
class TurnSummary:
    request: str = ""
    investigated: str = ""
    learned: str = ""
    completed: str = ""
    next_steps: str = ""

    def as_dict(self) -> Dict[str, str]:
        return {name: getattr(self, name) for name in SUMMARY_FIELDS}


_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def extract_json_object(raw: Any) -> Optional[Dict[str, Any]]:
    """The first JSON object found in `raw` (str), or None. Tolerates a code
    fence, a leading reasoning block and chatter around the object."""
    if isinstance(raw, tuple):
        raw = raw[0] if raw else ""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = _THINK_RE.sub("", raw).strip()
    candidates = [text]
    fenced = _FENCE_RE.search(text)
    if fenced:
        candidates.insert(0, fenced.group(1).strip())
    for candidate in candidates:
        obj = _first_object(candidate)
        if obj is not None:
            return obj
    return None


def _first_object(text: str) -> Optional[Dict[str, Any]]:
    decoder = json.JSONDecoder()
    index = text.find("{")
    attempts = 0
    while index != -1 and attempts < 8:
        try:
            value, _end = decoder.raw_decode(text, index)
        except ValueError:
            index = text.find("{", index + 1)
            attempts += 1
            continue
        if isinstance(value, dict):
            return value
        index = text.find("{", index + 1)
        attempts += 1
    return None


def _clip(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _string_list(value: Any, *, item_limit: int, max_items: int) -> List[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out: List[str] = []
    seen = set()
    for item in value:
        text = _clip(item, item_limit)
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= max_items:
            break
    return out


def _path_list(value: Any) -> List[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out: List[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        path = item.strip().replace("\\", "/")
        if not path or len(path) > MAX_PATH or "\n" in path or path in out:
            continue
        out.append(path)
        if len(out) >= MAX_FILES:
            break
    return out


def parse_observation_reply(raw: Any) -> Union[Observation, Skip, None]:
    """Validate the model's reply. Strict about the type (an unknown type is a
    reply that did not follow the schema) and about having something to say
    (a title and at least one fact or a narrative); lenient about size (long
    fields are clipped) and about concepts (unknown ones are dropped)."""
    data = extract_json_object(raw)
    if data is None:
        return None
    if data.get("skip") is True or str(data.get("skip", "")).lower() == "true":
        return Skip(_clip(data.get("reason"), MAX_REASON) or "noise")
    kind = str(data.get("type") or "").strip().lower()
    if kind not in TYPES:
        return None
    title = _clip(data.get("title"), MAX_TITLE)
    if not title:
        return None
    facts = _string_list(data.get("facts"), item_limit=MAX_FACT, max_items=MAX_FACTS)
    narrative = _clip(data.get("narrative"), MAX_NARRATIVE)
    if not facts and not narrative:
        return None
    concepts = [c for c in _string_list(data.get("concepts"), item_limit=40, max_items=6)
                if c.lower() in CONCEPTS]
    return Observation(
        type=kind, title=title, subtitle=_clip(data.get("subtitle"), MAX_SUBTITLE),
        facts=facts, narrative=narrative, concepts=[c.lower() for c in concepts],
        files_read=_path_list(data.get("files_read")),
        files_modified=_path_list(data.get("files_modified")),
    )


def parse_summary_reply(raw: Any) -> Union[TurnSummary, Skip, None]:
    """Validate a turn summary: at least `request` and one of the other fields."""
    data = extract_json_object(raw)
    if data is None:
        return None
    if data.get("skip") is True or str(data.get("skip", "")).lower() == "true":
        return Skip(_clip(data.get("reason"), MAX_REASON) or "empty")
    fields = {name: _clip(data.get(name), MAX_SUMMARY_FIELD) for name in SUMMARY_FIELDS}
    if not fields["request"]:
        return None
    if not any(fields[name] for name in SUMMARY_FIELDS if name != "request"):
        return None
    return TurnSummary(**fields)
