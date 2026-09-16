#!/usr/bin/env python3
"""lifecycle_check.py — automate what the install/upgrade/backup/restore/
uninstall lifecycle (docs/distribution/LIFECYCLE.md, A35) can be checked
without actually running an installer on a clean machine.

Two things this script does for real, against any ``--data-dir``:

    backup   zip a data directory (manifest of relative paths + sha256,
             so restore can verify byte-for-byte fidelity)
    restore  unzip a backup into a data directory, refusing by default to
             overwrite one that already has files in it (an accidental
             double-restore silently clobbering newer data is exactly the
             kind of "lifecycle" bug this exists to catch) unless
             ``--overwrite`` is passed

Plus one static check:

    check-uninstall-scripts   the documented uninstall scripts
             (scripts/uninstall.sh, scripts/uninstall.ps1) exist and their
             source never deletes data/ (or backups/, .env) without a
             --purge/-Purge guard around it

None of this replaces actually installing on a clean Windows/macOS/Docker
machine — see docs/distribution/LIFECYCLE.md for what stays a manual,
documented procedure and why.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_NAME = "_lifecycle_manifest.json"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def backup(data_dir: Path, out_zip: Path) -> Dict[str, Any]:
    """Zip every file under ``data_dir`` into ``out_zip``, with a manifest
    entry (relative path -> sha256, size) written inside the archive so
    ``restore`` (or a standalone check) can verify fidelity without needing
    the original directory any more."""
    if not data_dir.is_dir():
        raise FileNotFoundError(f"no such data dir: {data_dir}")
    out_zip.parent.mkdir(parents=True, exist_ok=True)

    files = sorted(p for p in data_dir.rglob("*") if p.is_file())
    manifest: Dict[str, Any] = {"files": {}}
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            rel = f.relative_to(data_dir).as_posix()
            manifest["files"][rel] = {"sha256": _sha256_file(f), "size": f.stat().st_size}
            zf.write(f, arcname=rel)
        zf.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2))
    return manifest


def restore(zip_path: Path, data_dir: Path, *, overwrite: bool = False) -> Dict[str, Any]:
    """Unzip ``zip_path`` into ``data_dir`` and verify every extracted
    file's sha256 against the archive's manifest. Refuses (raises) if
    ``data_dir`` already contains files and ``overwrite`` is not set."""
    if not zip_path.is_file():
        raise FileNotFoundError(f"no such backup archive: {zip_path}")
    data_dir.mkdir(parents=True, exist_ok=True)
    existing = [p for p in data_dir.rglob("*") if p.is_file()]
    if existing and not overwrite:
        raise RuntimeError(
            f"{data_dir} already has {len(existing)} file(s); pass overwrite=True "
            f"(--overwrite on the CLI) to restore into it anyway"
        )

    with zipfile.ZipFile(zip_path, "r") as zf:
        manifest = json.loads(zf.read(MANIFEST_NAME).decode("utf-8"))
        for rel in manifest["files"]:
            dest = (data_dir / rel).resolve()
            if not str(dest).startswith(str(data_dir.resolve())):
                raise ValueError(f"unsafe path in backup archive: {rel!r}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(rel) as src, dest.open("wb") as out:
                out.write(src.read())

    verified: Dict[str, bool] = {}
    for rel, meta in manifest["files"].items():
        dest = data_dir / rel
        verified[rel] = dest.is_file() and _sha256_file(dest) == meta["sha256"]
    manifest["verified"] = verified
    manifest["all_verified"] = all(verified.values())
    return manifest


_PURGE_GUARD_RE = re.compile(r"\bPURGE\b|\$Purge\b", re.IGNORECASE)


def check_uninstall_scripts(repo_root: Path = REPO_ROOT) -> Dict[str, Any]:
    """Static check: both documented uninstall scripts exist, and any line
    that deletes data/.env/backups is textually guarded by a purge flag
    check somewhere in the file (not a proof of correctness, but enough to
    catch the regression this case cares about: an uninstall that deletes
    user data unconditionally)."""
    results: Dict[str, Any] = {}
    candidates = {
        "scripts/uninstall.sh": (r"rm\s+-rf[^\n]*\b(data|\.env|backups)\b", "PURGE"),
        "scripts/uninstall.ps1": (r"Remove-Item[^\n]*\b(data|\.env|backups)\b", "Purge"),
    }
    for rel, (delete_pattern, guard_token) in candidates.items():
        path = repo_root / rel
        entry: Dict[str, Any] = {"exists": path.is_file()}
        if entry["exists"]:
            text = path.read_text(encoding="utf-8")
            has_guard = bool(re.search(rf"\b{guard_token}\b", text))
            delete_lines = [ln for ln in text.splitlines() if re.search(delete_pattern, ln, re.IGNORECASE)]
            entry["deletes_user_data"] = bool(delete_lines)
            entry["guarded_by_purge_flag"] = has_guard
            entry["safe"] = (not delete_lines) or has_guard
        else:
            entry["safe"] = False
        results[rel] = entry
    return results


def run(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    pb = sub.add_parser("backup")
    pb.add_argument("--data-dir", required=True)
    pb.add_argument("--out", required=True)

    pr = sub.add_parser("restore")
    pr.add_argument("--zip", required=True)
    pr.add_argument("--data-dir", required=True)
    pr.add_argument("--overwrite", action="store_true")

    sub.add_parser("check-uninstall-scripts")

    args = parser.parse_args(argv)

    if args.cmd == "backup":
        manifest = backup(Path(args.data_dir), Path(args.out))
        print(json.dumps({"files": len(manifest["files"])}, indent=2))
        return 0
    if args.cmd == "restore":
        manifest = restore(Path(args.zip), Path(args.data_dir), overwrite=args.overwrite)
        print(json.dumps({"all_verified": manifest["all_verified"]}, indent=2))
        return 0 if manifest["all_verified"] else 1
    if args.cmd == "check-uninstall-scripts":
        results = check_uninstall_scripts()
        print(json.dumps(results, indent=2))
        return 0 if all(r["safe"] for r in results.values()) else 1
    return 2


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
