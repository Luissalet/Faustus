# -*- coding: utf-8 -*-
"""Facts that were true for one minute and are filed forever.

Found by reading the memory screen after a session of ordinary use. Among
the stored memories were:

    "En la raiz del workspace hay 2 ficheros: `datos.json` y `NOTAS.md`."
    "Hay 2 ficheros en la raiz del workspace."

Both were pulled into the prompt three times each before anyone noticed.
Neither is a fact about the person: they are a snapshot of a directory that
stopped being true the moment a file was added, and now they contradict
reality on every future turn.

The extraction prompt already asks for "durable personal facts" and warns
against "what they asked about today". It still produced these, because a
prompt is a request and not a guard. This module is the guard: a narrow,
deterministic reject list applied to every candidate before it is stored,
so it holds whichever model does the extraction and however the prompt is
reworded later.

Narrow on purpose. It rejects only two shapes, and both need the sentence
to be about the workspace rather than about the person:

  * a count or a listing of files, folders or directories;
  * a statement explicitly scoped to "right now".

"User's pipeline converts PNG to STL", "User prefers millimetres", "User's
GitHub account is Luissalet" all pass untouched, which is the point: a
guard that eats good facts is worse than the problem it solves.
"""

from __future__ import annotations

import re
from typing import Optional

#: Files, folders, directories -- the nouns a snapshot is made of.
_FS_NOUN = (r"(?:fichero|ficheros|archivo|archivos|carpeta|carpetas|"
            r"directorio|directorios|file|files|folder|folders|"
            r"director(?:y|ies))")

#: "hay 2 ficheros", "there are 3 files", "contains 4 folders", "2 files".
_COUNT = re.compile(
    r"(?:\bhay\b|\bthere\s+(?:is|are)\b|\bcontains?\b|\btiene\b|\bhas\b|\b\d+\b)"
    r"[^.]{0,40}?\b" + _FS_NOUN + r"\b",
    re.IGNORECASE,
)

#: "en la raiz del workspace", "in the workspace root", "del directorio actual".
_WORKSPACE_SCOPE = re.compile(
    r"\b(?:ra[ií]z|root|workspace|directorio\s+actual|current\s+director(?:y|ies)|cwd)\b",
    re.IGNORECASE,
)

#: A listing: two or more file-ish names, backticked or not.
_FILENAMES = re.compile(r"[\w./\\-]*[\w-]+\.[A-Za-z][A-Za-z0-9]{0,4}\b")

#: Explicitly scoped to this moment.
_RIGHT_NOW = re.compile(
    r"\b(?:ahora\s+mismo|en\s+este\s+momento|actualmente|right\s+now|"
    r"at\s+the\s+moment|currently|as\s+of\s+(?:now|today)|hoy\s+en\s+d[ií]a)\b",
    re.IGNORECASE,
)


def volatile_reason(text: str) -> Optional[str]:
    """Why this candidate must not be stored, or None to store it.

    The string comes back so the caller can log WHICH rule fired; a guard
    nobody can debug gets deleted the first time it is wrongly blamed.
    """
    candidate = str(text or "").strip()
    if not candidate:
        return None

    mentions_fs = re.search(r"\b" + _FS_NOUN + r"\b", candidate, re.IGNORECASE)
    if mentions_fs:
        if _COUNT.search(candidate):
            return "a file or folder count, true only until the next write"
        if _WORKSPACE_SCOPE.search(candidate):
            return "a snapshot of the workspace, not a fact about the person"
    if len(_FILENAMES.findall(candidate)) >= 2 and _WORKSPACE_SCOPE.search(candidate):
        return "a directory listing, not a fact about the person"
    if _RIGHT_NOW.search(candidate):
        return "scoped to this moment, so it is stale by the next turn"
    return None


def is_volatile(text: str) -> bool:
    """`volatile_reason` as a predicate."""
    return volatile_reason(text) is not None
