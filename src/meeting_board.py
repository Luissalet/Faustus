"""Turn explicit action bullets in a saved meeting into sourced board tasks."""

from __future__ import annotations

import hashlib
import re
import threading
from pathlib import Path
from typing import Any

from src import meetings, project_board

_LOCK = threading.RLock()
_BULLET = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(?:\[[ xX]\]\s*)?(.+?)\s*$")
_TIME = re.compile(r"\[(\d{1,2}:\d{2}(?::\d{2})?)\]")
_OWNER = re.compile(r"\s*\((?:responsable|owner):\s*([^()]+)\)\s*$", re.IGNORECASE)
_EMPTY = {"none recorded.", "none recorded", "ninguna", "ninguno", "ninguna registrada", "ninguna registrada."}


def extract_actions(markdown: str, meeting_id: str) -> list[dict[str, Any]]:
    """Read only the generated Action items section; never promote open questions."""
    in_actions = False
    actions: list[dict[str, Any]] = []
    for line_no, line in enumerate((markdown or "").splitlines(), 1):
        if line.startswith("## "):
            in_actions = line[3:].strip().casefold() == "action items"
            continue
        if not in_actions:
            continue
        match = _BULLET.match(line)
        if not match:
            continue
        raw = match.group(1).strip()
        if not raw or raw.casefold() in _EMPTY or raw.endswith("?"):
            continue
        stamp = _TIME.search(raw)
        clean_title = _TIME.sub("", raw, count=1).strip() if stamp and stamp.start() == 0 else raw
        owner_match = _OWNER.search(clean_title)
        assignee = owner_match.group(1).strip()[:120] if owner_match else ""
        if owner_match:
            clean_title = clean_title[:owner_match.start()].strip()
        key = hashlib.sha256((meeting_id + "\n" + raw.casefold()).encode("utf-8")).hexdigest()[:16]
        actions.append({"title": clean_title[:300], "original": raw, "assignee": assignee,
                        "source_line": line_no, "source_time": stamp.group(1) if stamp else None,
                        "source_key": f"action:{key}"})
        if len(actions) >= 30:
            break
    return actions


def actions_to_board(meeting_id: str, project_id: str, *, owner: str, key: str,
                     dry_run: bool = True) -> dict[str, Any]:
    meeting = meetings.get_meeting(meeting_id, owner=owner)
    if meeting is None:
        raise ValueError("Meeting not found")
    if meeting.get("project_id") and meeting["project_id"] != project_id:
        raise ValueError("Meeting belongs to another project")
    if meeting.get("model_ok") is False:
        raise ValueError("This meeting has transcript-only notes; no action items were extracted")
    path = str(Path(meetings.MEETINGS_DIR) / (meeting_id + ".md"))
    title = str(meeting.get("title") or meeting_id)
    actions = extract_actions(meeting.get("markdown") or "", meeting_id)
    results: list[dict[str, Any]] = []
    with _LOCK:
        for action in actions:
            previous = project_board.find_issue_by_ref(project_id, "file", path, action["source_key"])
            marker = f"meeting-action:{meeting_id}:{action['source_key']}"
            if previous is None:
                matches, _ = project_board.list_issues(project_id, q=marker, limit=1)
                previous = matches[0] if matches else None
            if previous:
                results.append({**action, "status": "already_on_board", "issue_id": previous["id"]})
                continue
            if dry_run:
                results.append({**action, "status": "ready", "issue_id": None})
                continue
            body = (f"<!-- {marker} -->\nFrom meeting [{title}](/api/meetings/{meeting_id}), action item on line "
                    f"{action['source_line']}.\n\n{action['original']}")
            if action["source_time"]:
                body += f"\n\nTranscript time: {action['source_time']}"
            issue = project_board.create_issue(project_id, key, type="task", title=action["title"],
                                               body_md=body, assignee=action["assignee"],
                                               created_by=owner or "agent")
            project_board.add_ref(issue["id"], "file", path, label=action["source_key"])
            results.append({**action, "status": "created", "issue_id": issue["id"]})
    return {"meeting_id": meeting_id, "project_id": project_id, "dry_run": dry_run,
            "actions": results, "created": sum(a["status"] == "created" for a in results)}
