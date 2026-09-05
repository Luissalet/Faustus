"""Every image the README embeds has to be *in the repository*, not merely on
the machine that wrote the README.

This exists because it failed exactly once, and in the most embarrassing way
available: the interface screenshots were generated, checked (`the file is
there`), referenced from the README, committed and pushed — and every one of
them 404'd on GitHub. `.gitignore` blanket-ignores `*.png` as uploaded/generated
media, with a list of per-folder exceptions, and `assets/screens/` was not on
it. `git add` said nothing, `git status` said nothing, the files existed, and
the check that was run asked the filesystem instead of git.

So the rule is: presence on disk proves nothing. Ask the index.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import pytest

REPO = Path(__file__).resolve().parents[1]

# `<img src="...">` in the HTML blocks, and `![alt](...)` in Markdown.
SRC = re.compile(r'<img\s[^>]*\bsrc="([^"]+)"', re.I)
MD_IMG = re.compile(r'!\[[^\]]*\]\(\s*([^)\s]+)')

DOCS = ("README.md", "FAUSTUS.md", "ROADMAP.md")


def _tracked() -> set[str] | None:
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=REPO,
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return {line.strip() for line in out.stdout.splitlines() if line.strip()}


def _local_refs(text: str) -> list[str]:
    refs = [*SRC.findall(text), *MD_IMG.findall(text)]
    out = []
    for ref in refs:
        # Absolute URLs and anchors are somebody else's problem.
        if urlsplit(ref).scheme or ref.startswith(("#", "//", "/")):
            continue
        out.append(ref.split("#", 1)[0].split("?", 1)[0])
    return out


@pytest.mark.parametrize("doc", DOCS)
def test_embedded_images_are_tracked_by_git(doc: str) -> None:
    path = REPO / doc
    if not path.exists():
        pytest.skip(f"{doc} not in this checkout")
    tracked = _tracked()
    if tracked is None:
        pytest.skip("not a git checkout")

    missing = []
    for ref in _local_refs(path.read_text(encoding="utf-8")):
        target = (path.parent / ref).resolve()
        try:
            rel = target.relative_to(REPO).as_posix()
        except ValueError:
            missing.append(f"{ref} (escapes the repository)")
            continue
        if rel not in tracked:
            on_disk = "; the file IS on disk, so check .gitignore" if target.exists() else ""
            missing.append(f"{ref}{on_disk}")

    assert not missing, (
        f"{doc} embeds images git does not track — they will 404 for everyone "
        f"but this machine: {missing}"
    )


def test_no_screenshot_is_committed_without_a_reader() -> None:
    """The mirror of the rule above: a screenshot nobody links to is dead weight
    in a repository people clone. `assets/screens/` holds one shot per screen —
    it is cheap to regenerate and expensive to carry, so only the ones a
    document actually embeds belong in git.
    """
    tracked = _tracked()
    if tracked is None:
        pytest.skip("not a git checkout")

    shots = {p for p in tracked if p.startswith("assets/screens/")}
    if not shots:
        pytest.skip("no screenshots committed")

    referenced: set[str] = set()
    for doc in DOCS:
        path = REPO / doc
        if not path.exists():
            continue
        for ref in _local_refs(path.read_text(encoding="utf-8")):
            target = (path.parent / ref).resolve()
            try:
                referenced.add(target.relative_to(REPO).as_posix())
            except ValueError:
                pass

    orphans = sorted(shots - referenced)
    assert not orphans, (
        "screenshots committed but linked from nothing — regenerate them when "
        f"they are needed instead of carrying them: {orphans}"
    )
