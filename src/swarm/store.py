"""src/swarm/store.py — a swarm run's checkpoint on disk.

Layout (``DATA_DIR/swarm/<run_id>/``):

  * ``manifest.json`` — the run's spec (instruction, mode, output fields,
    route, limits), owner, status, counts, artifacts, reduce result.
  * ``items.json``    — the items, in order, exactly as they were given.
  * ``results.jsonl`` — one line per FINISHED item (ok or failed after its
    retry), appended as it finishes. An item with no line is pending: that is
    the whole resume rule, so a run killed mid-way picks up exactly the items
    it had not finished and never repeats one it had.
  * ``exports/``      — the final table as Markdown, CSV and JSONL, plus the
    reduce output, written once the run ends (also copied to the artifact
    store).
"""
from __future__ import annotations

import json
import os
import re
import threading
from typing import Any, Dict, List, Optional

from core.atomic_io import atomic_write_json

_RUN_ID_RE = re.compile(r"^swarm-[0-9a-f]{6,32}$")
_APPEND_LOCK = threading.Lock()


class SwarmNotFoundError(Exception):
    """No such run for this owner (another owner's run is reported the same
    way: its existence is not this caller's business)."""


def root_dir() -> str:
    from src import constants
    return os.path.join(constants.DATA_DIR, "swarm")


def valid_run_id(run_id: str) -> bool:
    return bool(_RUN_ID_RE.match(str(run_id or "")))


def run_dir(run_id: str) -> str:
    if not valid_run_id(run_id):
        raise SwarmNotFoundError(f"no swarm run {run_id!r}")
    return os.path.join(root_dir(), run_id)


def exports_dir(run_id: str) -> str:
    return os.path.join(run_dir(run_id), "exports")


def _load_json(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def load_manifest(run_id: str) -> Optional[Dict[str, Any]]:
    if not valid_run_id(run_id):
        return None
    data = _load_json(os.path.join(run_dir(run_id), "manifest.json"))
    return data if isinstance(data, dict) else None


def save_manifest(manifest: Dict[str, Any]) -> None:
    atomic_write_json(os.path.join(run_dir(manifest["run_id"]), "manifest.json"), manifest)


def manifest_for(run_id: str, owner: str) -> Dict[str, Any]:
    manifest = load_manifest(run_id)
    if manifest is None or str(manifest.get("owner") or "") != str(owner or ""):
        raise SwarmNotFoundError(f"no swarm run {run_id!r}")
    return manifest


def save_items(run_id: str, items: List[Any]) -> None:
    atomic_write_json(os.path.join(run_dir(run_id), "items.json"), list(items))


def load_items(run_id: str) -> List[Any]:
    data = _load_json(os.path.join(run_dir(run_id), "items.json"))
    return list(data) if isinstance(data, list) else []


def append_result(run_id: str, row: Dict[str, Any]) -> None:
    line = json.dumps(row, ensure_ascii=False, default=str)
    path = os.path.join(run_dir(run_id), "results.jsonl")
    with _APPEND_LOCK:
        # A torn last line (the process died mid-write) must not swallow
        # this record: start on a fresh line when the file does not end in one.
        prefix = ""
        try:
            with open(path, "rb") as raw:
                raw.seek(0, os.SEEK_END)
                if raw.tell() > 0:
                    raw.seek(-1, os.SEEK_END)
                    prefix = "" if raw.read(1) == b"\n" else "\n"
        except OSError:
            prefix = ""
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(prefix + line + "\n")
            fh.flush()


def load_results(run_id: str) -> Dict[int, Dict[str, Any]]:
    """index -> the item's finished row. A torn last line (the process died
    mid-write) is skipped, so that item simply counts as pending again."""
    out: Dict[int, Dict[str, Any]] = {}
    path = os.path.join(run_dir(run_id), "results.jsonl")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(row, dict) and isinstance(row.get("index"), int):
                    out[row["index"]] = row
    except OSError:
        pass
    return out


def list_manifests(owner: str) -> List[Dict[str, Any]]:
    root = root_dir()
    try:
        names = os.listdir(root)
    except OSError:
        return []
    out = []
    for name in names:
        if not valid_run_id(name):
            continue
        manifest = load_manifest(name)
        if manifest and str(manifest.get("owner") or "") == str(owner or ""):
            out.append(manifest)
    out.sort(key=lambda m: float(m.get("created_at") or 0), reverse=True)
    return out
