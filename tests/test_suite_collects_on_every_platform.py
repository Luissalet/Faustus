"""One test module can take the whole suite down, and this pins the way.

`tests/test_history_import.py` carried `import resource` — a leftover, never
used, invisible on Linux. On Windows that module does not exist, so importing
it raises at COLLECTION time, and pytest does not just skip the file: it stops
the run. `!!! Interrupted: 1 error during collection !!!` after 27 seconds,
zero tests executed, on a machine where the other ~10.000 were fine.

Two things make that worth a test of its own rather than a fix:

* The blast radius is the opposite of the mistake's size. An unused import is
  the smallest possible defect and it cost the entire suite, so "we will
  notice" is not true — what you notice is that nothing ran.
* It is silent on the machine where the code is written. Anyone developing on
  Linux can add one of these and every check they run stays green.

The rule pinned here is therefore not "resource is banned" but the shape:
**a test module may not import a POSIX-only module at import time.** Inside a
function, or behind a try/except ImportError, or under an `if os.name` — all
fine, because none of those run during collection. Only the unconditional
top-level form is refused, and the failure message says which file and which
module, because the traceback pytest prints for a collection error names the
importer, not the fix.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


# Stdlib modules that simply do not exist on Windows. Not a style list: every
# one of these is an ImportError, not a warning, and therefore a stopped run.
POSIX_ONLY = {
    "resource", "fcntl", "pwd", "grp", "termios", "tty", "pty",
    "posix", "syslog", "crypt", "spwd", "nis", "ossaudiodev",
}

TESTS_DIR = Path(__file__).resolve().parent


def _top_level_imports(tree: ast.Module):
    """Only the imports that run on import. Anything nested — in a function, a
    try, an if — is the author saying "this may not be here", which is exactly
    the thing being asked for."""
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0], node.lineno
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.module.split(".")[0], node.lineno


def test_no_test_module_imports_a_posix_only_module_at_import_time():
    offenders = []
    for path in sorted(TESTS_DIR.rglob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as e:  # a broken test file is a different failure
            pytest.fail(f"{path.name} does not parse: {e}")
        for name, line in _top_level_imports(tree):
            if name in POSIX_ONLY:
                offenders.append(f"{path.name}:{line} imports {name!r}")

    assert not offenders, (
        "these run at collection time and do not exist on Windows, so they do "
        "not fail their own file — they stop the whole suite before any test "
        "runs:\n  " + "\n  ".join(offenders)
        + "\nMove the import inside the function that needs it, or guard it "
          "with try/except ImportError and skip."
    )


def test_the_check_would_have_caught_the_real_one(tmp_path):
    """A guard nobody has seen fail is a guard nobody can trust. This is the
    exact line that took the suite down on 04-09-2026."""
    sample = tmp_path / "test_sample.py"
    sample.write_text("import json\nimport resource\n", encoding="utf-8")
    tree = ast.parse(sample.read_text(encoding="utf-8"))

    found = [name for name, _ in _top_level_imports(tree) if name in POSIX_ONLY]
    assert found == ["resource"]


def test_a_guarded_import_is_allowed(tmp_path):
    """The point is collection safety, not banning the module. Both of these
    shapes are fine and must stay fine, or the rule turns into a nuisance that
    someone eventually deletes."""
    sample = tmp_path / "test_sample.py"
    sample.write_text(
        "try:\n"
        "    import resource\n"
        "except ImportError:\n"
        "    resource = None\n"
        "\n"
        "def test_limits():\n"
        "    import fcntl\n"
        "    assert fcntl\n",
        encoding="utf-8")
    tree = ast.parse(sample.read_text(encoding="utf-8"))

    assert [name for name, _ in _top_level_imports(tree) if name in POSIX_ONLY] == []
