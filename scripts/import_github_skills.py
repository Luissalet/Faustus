"""Import a set of skills from a public GitHub repository at a PINNED revision.

Usage (from the Faustus checkout, with the app's data directory selected the
usual way through ODYSSEUS_DATA_DIR):

    python scripts/import_github_skills.py --repo owner/name --sha <40-hex> \
        --path skills/unity/unity-physics --path skills/unity/unity-animation \
        --category gamedev --tag unity --license Apache-2.0

Every skill goes through the same fetcher and installer the Skills panel's
"import from URL" uses (`services.memory.skill_importer.fetch_skill_bundle`
and `SkillsManager.import_bundle_from_files`), so the SSRF guard and the size
limits apply. On top of that this script:

* refuses anything that is not a full 40-character commit SHA (a moving
  branch name is never accepted),
* lays each bundle down relative to the skill folder, so `references/` and
  `scripts/` sit beside `SKILL.md`,
* copies the upstream LICENSE and NOTICE next to each skill as
  `UPSTREAM-LICENSE.txt` / `UPSTREAM-NOTICE.txt`, and names the licence in the
  skill's imported-from note,
* adds the tags given with `--tag` (for example the game engine), which is
  what the per-turn skill selector matches against,
* skips a skill that was already imported from the same URL, so running it
  twice is safe.

`--dry-run` fetches and reports without writing anything.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _tree_url(repo: str, sha: str, path: str) -> str:
    return f"https://github.com/{repo}/tree/{sha}/{path.strip('/')}"


def _blob_text(repo: str, sha: str, name: str) -> str:
    # Same SSRF-guarded transport the importer uses; raw file at the pinned SHA.
    from services.memory.skill_importer import ResolvedSource, _fetch_text, _raw_url
    owner, _, name_repo = repo.partition("/")
    return _fetch_text(_raw_url(ResolvedSource(owner=owner, repo=name_repo, ref=sha, path=name), name))


def _relative_to_skill(files: dict, path: str) -> dict:
    prefix = path.strip("/") + "/"
    out = {}
    for rel, content in files.items():
        rel = rel.replace("\\", "/")
        out[rel[len(prefix):] if rel.startswith(prefix) else rel] = content
    return out


def import_skills(repo: str, sha: str, paths: list, *, category: str, tags: list,
                  license_name: str = "", dry_run: bool = False, owner=None,
                  publish: bool = False) -> list:
    if not SHA_RE.match(sha or ""):
        raise SystemExit("--sha must be a full 40-character commit SHA, not a branch or tag")
    from src.constants import DATA_DIR
    from services.memory.skill_importer import fetch_skill_bundle
    from services.memory.skills import SkillsManager

    manager = SkillsManager(DATA_DIR)
    already = {}
    for s in manager.load_all():
        for line in str(s.get("body_extra") or "").splitlines():
            if line.startswith("Imported from "):
                already[line[len("Imported from "):].split(" ", 1)[0]] = s["name"]

    licence_files = {}
    for upstream, local in (("LICENSE", "UPSTREAM-LICENSE.txt"), ("NOTICE", "UPSTREAM-NOTICE.txt")):
        try:
            licence_files[local] = _blob_text(repo, sha, upstream)
        except Exception as exc:  # NOTICE is optional upstream
            print(f"note: no {upstream} fetched ({exc})")

    results = []
    for path in paths:
        url = _tree_url(repo, sha, path)
        if url in already:
            results.append({"path": path, "status": "skipped", "skill": already[url]})
            continue
        files, _src = fetch_skill_bundle(url)
        files = _relative_to_skill(files, path)
        if "SKILL.md" not in files:
            results.append({"path": path, "status": "error", "error": "no SKILL.md"})
            continue
        files.update(licence_files)
        if dry_run:
            results.append({"path": path, "status": "ok-dry-run", "files": sorted(files)})
            continue
        note = url + (f" (pinned {sha[:12]}, licence {license_name})" if license_name
                      else f" (pinned {sha[:12]})")
        entry = manager.import_bundle_from_files(
            files, owner=owner, source_url=note, category=category, tags=tags,
            status="published" if publish else None)
        results.append({"path": path, "status": "imported", "skill": entry["name"],
                        "files": len(files)})
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", required=True, help="owner/name on GitHub")
    ap.add_argument("--sha", required=True, help="full 40-character commit SHA")
    ap.add_argument("--path", action="append", required=True, dest="paths",
                    help="skill folder inside the repo (repeatable)")
    ap.add_argument("--category", default="imported")
    ap.add_argument("--tag", action="append", default=[], dest="tags")
    ap.add_argument("--license", default="", dest="license_name")
    ap.add_argument("--owner", default=None)
    ap.add_argument("--publish", action="store_true",
                    help="mark the skills published (default: draft, which the selector "
                         "only injects above the confidence bar)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    results = import_skills(args.repo, args.sha, args.paths, category=args.category,
                            tags=args.tags, license_name=args.license_name,
                            dry_run=args.dry_run, owner=args.owner, publish=args.publish)
    for r in results:
        print(r)
    return 1 if any(r["status"] == "error" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
