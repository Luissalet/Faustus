"""Single argument contract for the three PDF navigation tools.
Pure metadata/parser: imports no handlers, dispatcher or PDF backend.
"""
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ArgumentIssue:
    field: str
    code: str
    detail: str
    value: Any = None


class PdfArgumentsError(ValueError):
    pass


def tool_names() -> frozenset[str]:
    return frozenset(_DEFINITIONS)


def function_schemas() -> list[dict]:
    return [{"type": "function", "function": deepcopy(definition)}
            for definition in _DEFINITIONS.values()]


def argument_issues(name: str, args: Any) -> list[ArgumentIssue]:
    schema = _DEFINITIONS[name]["parameters"]
    if not isinstance(args, dict):
        return [ArgumentIssue("$", "wrong_type", "expected a JSON object")]
    errors = [ArgumentIssue(key, "missing_required", "required field is missing")
              for key in schema["required"] if key not in args]
    for key, value in args.items():
        prop = schema["properties"].get(key)
        if prop is None:
            errors.append(ArgumentIssue(key, "unknown_field", "field is not declared", value))
        elif prop["type"] == "integer":
            if not (type(value) is int or (type(value) is float and value.is_integer())):
                errors.append(ArgumentIssue(key, "wrong_type", "expected an integer (not bool or string)", value))
            elif value < prop["minimum"]:
                errors.append(ArgumentIssue(key, "range", f"must be at least {prop['minimum']}", value))
        elif not isinstance(value, str):
            errors.append(ArgumentIssue(key, "wrong_type", "expected a string", value))
        elif not value.strip():
            errors.append(ArgumentIssue(key, "range", "must contain non-whitespace text", value))
    return errors


def parse_content(name: str, content: Any) -> dict:
    """Native objects and JSON fences share validation; outline keeps bare paths."""
    args = content
    if isinstance(content, str):
        raw = content.strip()
        if name == "pdf_outline" and raw and not raw.startswith("{"):
            args = {"path": raw}
        else:
            try:
                args = json.loads(raw)
            except (ValueError, TypeError) as exc:
                raise PdfArgumentsError("expected a JSON object") from exc
    issues = argument_issues(name, args)
    if issues:
        raise PdfArgumentsError("; ".join(f"{issue.field}: {issue.detail}" for issue in issues))
    parsed = {key: value.strip() if isinstance(value, str) else value for key, value in args.items()}
    for key, prop in _DEFINITIONS[name]["parameters"]["properties"].items():
        if key not in parsed and "default" in prop:
            parsed[key] = prop["default"]
        elif key in parsed and prop["type"] == "integer":
            parsed[key] = int(parsed[key])
    return parsed

_DEFINITIONS = {'pdf_outline': {'name': 'pdf_outline',
                 'description': "Build (or reuse the cached) table-of-contents tree of a PDF -- from the PDF's "
                                'own outline/bookmarks when it has one, else conservative heading detection, else '
                                'fixed 10-page chunks -- and return it as a compact indented list ("id  title  '
                                '(pp. a-b)") plus the structured nodes. Workflow for a long PDF: call pdf_outline '
                                'to see the sections and their EXACT physical page ranges, pick a node id (or use '
                                'pdf_find_section when the outline is long), then call pdf_read_section with that '
                                'id. Page numbers always come from this tool, never invent one.',
                 'parameters': {'type': 'object',
                                'properties': {'path': {'type': 'string',
                                                        'description': 'Path to the PDF',
                                                        'minLength': 1,
                                                        'pattern': '\\S'},
                                               'max_depth': {'type': 'integer',
                                                             'description': 'Only return nodes up to this nesting '
                                                                            'depth (1 = top level only). Omit for '
                                                                            'the full tree',
                                                             'minimum': 1}},
                                'required': ['path'],
                                'additionalProperties': False}},
 'pdf_read_section': {'name': 'pdf_read_section',
                      'description': "Read the text of ONE node from a PDF's table-of-contents tree (see "
                                     "pdf_outline), by node id -- only that node's own physical page range, each "
                                     'page prefixed with a "[page N]" marker so the source page of every fact is '
                                     'traceable. Clipped to max_chars, with a note saying exactly which page it '
                                     'stopped at if so. node_id must be one pdf_outline (or pdf_find_section) '
                                     'just returned for this same PDF -- an unknown id is refused rather than '
                                     'guessed at, so page numbers can never be invented.',
                      'parameters': {'type': 'object',
                                     'properties': {'path': {'type': 'string',
                                                             'description': 'Path to the PDF -- same file '
                                                                            'pdf_outline was called on',
                                                             'minLength': 1,
                                                             'pattern': '\\S'},
                                                    'node_id': {'type': 'string',
                                                                'description': 'A node id from '
                                                                               'pdf_outline/pdf_find_section, '
                                                                               'e.g. "1.2"',
                                                                'minLength': 1,
                                                                'pattern': '\\S'},
                                                    'max_chars': {'type': 'integer',
                                                                  'description': 'Max characters of text to '
                                                                                 'return (default 20000)',
                                                                  'minimum': 1,
                                                                  'default': 20000}},
                                     'required': ['path', 'node_id'],
                                     'additionalProperties': False}},
 'pdf_find_section': {'name': 'pdf_find_section',
                      'description': "Search a PDF's table-of-contents tree (see pdf_outline) for node titles "
                                     'matching `query`, returning node ids + titles + page ranges, best match '
                                     'first -- use this to pick a node id when the outline is long or you only '
                                     'approximately know the section name, then pass the id to pdf_read_section. '
                                     'When the tree has no real structure (fixed page chunks), also searches each '
                                     "chunk's own page text.",
                      'parameters': {'type': 'object',
                                     'properties': {'path': {'type': 'string',
                                                             'description': 'Path to the PDF',
                                                             'minLength': 1,
                                                             'pattern': '\\S'},
                                                    'query': {'type': 'string',
                                                              'description': 'Section title or keyword to search '
                                                                             'for',
                                                              'minLength': 1,
                                                              'pattern': '\\S'},
                                                    'limit': {'type': 'integer',
                                                              'description': 'Max matches to return (default 8)',
                                                              'minimum': 1,
                                                              'default': 8}},
                                     'required': ['path', 'query'],
                                     'additionalProperties': False}}}
