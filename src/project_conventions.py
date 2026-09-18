"""project_conventions.py — the name of the per-project convention folder.

Faustus keeps a project's own conventions — skills, objectives,
INSTRUCTIONS.md, the story bible, the project identity marker, memory notes —
in one folder inside the project's workspace: ``.faustus/``.

A project created before this module existed may still have that folder
named ``.odysseus/``. Nothing migrates it automatically (a silent rename of a
folder full of a person's own notes is not a thing this codebase does), so
the rule everywhere is:

* **WRITE/CREATE always uses ``.faustus/``.** A new project, or a project
  that never had the folder, gets ``.faustus/``. Nothing new is ever written
  under the old name.
* **READ/DISCOVER looks in ``.faustus/`` first**, and falls back to reading
  an existing ``.odysseus/`` only when ``.faustus/`` is not there, so a
  project that predates the rename keeps working exactly as before.

Every module that names this folder should resolve it through
:func:`convention_dir` (or use :data:`CONVENTION_DIR_NAMES` for the places
that skip or ignore the folder rather than read it, such as snapshot skip
lists and trust checks — those must match both names).
"""

from __future__ import annotations

import os

#: The current name of the convention folder. All writes go here.
CONVENTION_DIRNAME = ".faustus"

#: The pre-rename name. Still read, never written to.
LEGACY_CONVENTION_DIRNAME = ".odysseus"

#: Both names, current first — for skip lists, ignore sets and trust checks
#: that must treat either folder the same way rather than resolve one path.
CONVENTION_DIR_NAMES = (CONVENTION_DIRNAME, LEGACY_CONVENTION_DIRNAME)


def convention_dir(root: str, *, create: bool = False) -> str:
    """Resolve the convention folder to use under ``root``.

    With ``create=False`` (the default, for reads and discovery): returns
    ``<root>/.faustus`` unless that folder does not exist and a pre-existing
    ``<root>/.odysseus`` does — in which case the legacy folder is returned,
    so an existing project's notes are never orphaned.

    With ``create=True`` (for a caller about to write or create something):
    always returns ``<root>/.faustus``, regardless of any legacy folder,
    since nothing new is ever written under the old name.

    Returns ``""`` when ``root`` is falsy, same as the callers that used to
    inline this check.
    """
    if not root:
        return ""
    faustus_dir = os.path.join(root, CONVENTION_DIRNAME)
    if create:
        return faustus_dir
    legacy_dir = os.path.join(root, LEGACY_CONVENTION_DIRNAME)
    if not os.path.isdir(faustus_dir) and os.path.isdir(legacy_dir):
        return legacy_dir
    return faustus_dir
