"""tool_deliverables.py — files a tool made for the user become downloads.

A family app can end a tool result with ``"deliverables": [{"path": ..., "name": ...}]``:
files on this machine that the user asked for (Links Hoard's ``media_download`` lists the
video or audio it just saved). Faustus copies each one into the owner's artifact store and
appends a download link to the result the model reads, so the reply hands the file over
wherever the user is talking from: the desktop app (a save dialog, so it goes wherever they
choose), the browser, or the phone through the private network. The app keeps its own copy
and its own history; the copy here keeps the link working even if that one moves.

Owner-scoped like every artifact (``/api/artifacts/{id}/download`` checks the owner), so a
link is only good for the person whose turn produced it. Idempotent: the occurrence id is a
``uuid5`` of (owner, session, call, tool, sha256, name), so a replayed call reuses it, and a
result already carrying ``MARKER`` is returned as it is.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import uuid
from typing import Any, Dict, List, Optional

from src.contracts.base import now_iso
from src.settings import get_setting

logger = logging.getLogger(__name__)

MARKER = "_tool_deliverables"
MAX_FILES = 10
DOWNLOAD_URL = "/api/artifacts/{id}/download"


def max_bytes() -> int:
    """Largest file offered as a download (setting ``tool_deliverable_max_mb``, 2048 by default)."""
    try:
        mb = int(get_setting("tool_deliverable_max_mb", 2048) or 2048)
    except (TypeError, ValueError):
        mb = 2048
    return max(1, mb) * 1024 * 1024


def _payload(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    structured = result.get("structured_content")
    if isinstance(structured, dict):
        return structured
    text = result.get("stdout") if isinstance(result.get("stdout"), str) else result.get("output")
    if not isinstance(text, str) or '"deliverables"' not in text:
        return None
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _human(size: int) -> str:
    step = 1024.0
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < step or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= step
    return f"{size} B"


def link_label(name: str) -> str:
    """A file name as Markdown link text: square brackets (yt-dlp names carry ``[id]``) would end the label early
    and leave the link as plain text, so they become parentheses."""
    return " ".join(str(name).replace("[", "(").replace("]", ")").split()) or "file"


def publish(path: str, name: str, *, owner: str, session_id: str = "", call_id: str = "",
            tool: str = "") -> Dict[str, Any]:
    """Copy one file into the artifact store as this owner's occurrence; returns its download."""
    from src import artifact_identity as identity
    from src import artifact_store
    from src.contracts.blob import ArtifactOccurrence

    store = artifact_store.ARTIFACT_STORE_DIR
    sha, size, stored_name, _created = artifact_store.publish_copy(path, store, name, max_bytes=max_bytes())
    media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    identity.ensure_blob(sha256=sha, byte_size=size, filename=stored_name, media_type=media_type)
    seed = json.dumps([owner, session_id, call_id, tool, sha, name], sort_keys=True, ensure_ascii=False)
    occurrence_id = "occ_" + uuid.uuid5(uuid.NAMESPACE_URL, seed).hex
    note = f"tool_deliverable tool={tool or 'unknown'}"
    if call_id:
        note += f" call_id={call_id}"
    identity.ensure_occurrence(ArtifactOccurrence.parse({
        "id": occurrence_id, "kind": artifact_store.kind_of(name), "blob_sha256": sha,
        "label": name, "owner": owner, "project_id": "", "run_id": "",
        "session_id": session_id or "", "skill_id": "", "skill_version": "",
        "created_at": now_iso(), "partial": False,
        "provenance": {"backend": "tool_deliverable", "note": note},
        "retention": {"policy": "keep"},
    }))
    return {"id": occurrence_id, "name": name, "size": size, "media_type": media_type,
            "url": DOWNLOAD_URL.format(id=occurrence_id)}


def deliver(result: Any, *, owner: str, session_id: str = "", call_id: str = "",
            tool: str = "") -> Any:
    """Return ``result`` with its deliverables published and their links appended, or unchanged."""
    if not isinstance(result, dict) or result.get(MARKER) or result.get("exit_code"):
        return result
    payload = _payload(result)
    items = payload.get("deliverables") if payload else None
    if not isinstance(items, list) or not items:
        return result
    published: List[Dict[str, Any]] = []
    problems: List[str] = []
    limit = max_bytes()
    for item in items[:MAX_FILES]:
        path = item.get("path") if isinstance(item, dict) else item
        if not isinstance(path, str) or not path or not os.path.isabs(path):
            problems.append(f"{path!r}: not an absolute path")
            continue
        if not os.path.isfile(path):
            problems.append(f"{os.path.basename(path)}: the file is not there")
            continue
        name = os.path.basename(str((item.get("name") if isinstance(item, dict) else "") or path).replace("\\", "/")) or "file"
        size = os.path.getsize(path)
        if size > limit:
            problems.append(f"{name}: {_human(size)} is over the {_human(limit)} download limit")
            continue
        if not owner:
            problems.append(f"{name}: no signed-in owner to keep the download for")
            continue
        try:
            published.append(publish(path, name, owner=owner, session_id=session_id, call_id=call_id, tool=tool))
        except Exception as exc:  # noqa: BLE001 - a file that cannot be offered must not break the turn
            logger.warning("tool_deliverables: could not publish %s: %s", path, exc)
            problems.append(f"{name}: could not be copied ({exc})")
    if len(items) > MAX_FILES:
        problems.append(f"{len(items) - MAX_FILES} more files were not offered (at most {MAX_FILES} per call)")
    out = dict(result)
    out[MARKER] = True
    out["downloads"] = published
    lines = [""]
    if published:
        lines.append("Faustus made these files downloadable for the user (desktop app: a save dialog; "
                     "browser or phone: a normal download). Give them to the user as these Markdown links, unchanged:")
        lines += [f"- [{link_label(p['name'])}]({p['url']}) ({_human(p['size'])})" for p in published]
    if problems:
        lines.append("Not offered as downloads: " + "; ".join(problems))
    key = "stdout" if isinstance(out.get("stdout"), str) else "output"
    out[key] = (out.get(key) or "") + "\n".join(lines)
    return out
