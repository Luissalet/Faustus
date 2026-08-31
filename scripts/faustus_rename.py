#!/usr/bin/env python3
"""Re-apply the Faustus brand after merging upstream Odysseus.

The fork keeps every internal identifier (ODYSSEUS_* environment variables,
localStorage keys, DOM ids / CSS classes, odysseus:* events, X-Odysseus-*
headers, module and folder names) so upstream merges stay cheap; only the
user-visible name changes. After `git merge upstream/main`, run:

    python scripts/faustus_rename.py            # rewrite tracked files
    python scripts/faustus_rename.py --check    # exit 1 if anything is left

Rules (same as the original rename, 31-08-2026):
  * the standalone token `Odysseus` (word boundaries; not `X-Odysseus-…`,
    not `fooOdysseus`, not `_Odysseus`) → `Faustus`;
  * lowercase `odysseus` and uppercase `ODYSSEUS` are identifiers: untouched;
  * upstream-only material is skipped: website/, specs/, .github/, licenses/,
    CONTRIBUTING.md, ROADMAP.md, ACKNOWLEDGMENTS.md; README.md keeps the
    upstream body (the fork notice at its top is maintained by hand);
    FAUSTUS.md is the fork's own record.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Dict, List, Tuple

TOKEN_RE = re.compile(r"(?<![\w\-])Odysseus(?![\w])")
SKIP_DIRS = ("website/", "specs/", ".github/", "licenses/")
SKIP_FILES = {
    "README.md", "FAUSTUS.md", "ACKNOWLEDGMENTS.md", "CONTRIBUTING.md", "ROADMAP.md",
    # Tests that check upstream material (issue templates, the README wordmark).
    "tests/test_issue_description_check.py", "tests/test_readme_ascii_fenced.py",
    # This very test suite / script mention both names on purpose.
    "tests/test_faustus_brand.py", "scripts/faustus_rename.py",
}


def tracked_files(root: str) -> List[str]:
    out = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True, encoding="utf-8",
                         errors="replace", check=True).stdout
    return [f for f in out.split("\n") if f]


def candidates(root: str) -> List[str]:
    files = []
    for f in tracked_files(root):
        if f.startswith(SKIP_DIRS) or f in SKIP_FILES:
            continue
        files.append(f)
    return files


def scan(root: str) -> Dict[str, int]:
    """{path: occurrences} of the visible brand still spelled Odysseus."""
    found: Dict[str, int] = {}
    for f in candidates(root):
        p = os.path.join(root, f)
        try:
            with open(p, "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        if b"Odysseus" not in raw:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        n = len(TOKEN_RE.findall(text))
        if n:
            found[f] = n
    return found


def rename(root: str) -> Tuple[int, int]:
    """Rewrite in place. Returns (files changed, replacements)."""
    files = 0
    total = 0
    for f, n in scan(root).items():
        p = os.path.join(root, f)
        with open(p, "rb") as fh:
            text = fh.read().decode("utf-8")
        new, k = TOKEN_RE.subn("Faustus", text)
        if k:
            with open(p, "wb") as fh:
                fh.write(new.encode("utf-8"))
            files += 1
            total += k
    return files, total


def main(argv: List[str]) -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if "--check" in argv:
        left = scan(root)
        if left:
            for f, n in sorted(left.items()):
                print(f"{n:4d}  {f}")
            print(f"{sum(left.values())} visible 'Odysseus' left in {len(left)} file(s) — run scripts/faustus_rename.py")
            return 1
        print("brand ok: no visible 'Odysseus' left")
        return 0
    files, total = rename(root)
    print(f"renamed {total} occurrence(s) in {files} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
