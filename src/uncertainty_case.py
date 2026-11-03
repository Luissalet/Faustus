"""uncertainty_case.py -- the session record behind `uncertainty_next_observation`.

A case keeps the hypotheses, the candidate observations and every result
observed so far, so successive calls update the same case instead of restating
it. The maths lives in `src/uncertainty_sensor.py` (pure); this module only
loads, merges and saves the record (one JSON file per case under
DATA_DIR/uncertainty_cases/<owner>/).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
from typing import Any, Dict, List

from src import uncertainty_sensor as sensor

_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_OWNER_RE = re.compile(r"[^A-Za-z0-9_.@-]")


def _base_dir(owner: str) -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "uncertainty_cases", _OWNER_RE.sub("_", owner or "_")[:80] or "_")


def _path(owner: str, case_id: str) -> str:
    if not _ID_RE.match(case_id or ""):
        raise ValueError("case_id must be 1-64 letters, digits, '_', '-' or '.'")
    return os.path.join(_base_dir(owner), case_id + ".json")


def load(owner: str, case_id: str) -> Dict[str, Any] | None:
    try:
        with open(_path(owner, case_id), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except FileNotFoundError:
        return None


def save(owner: str, case_id: str, record: Dict[str, Any]) -> None:
    path = _path(owner, case_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def list_cases(owner: str) -> List[Dict[str, Any]]:
    base = _base_dir(owner)
    rows = []
    if os.path.isdir(base):
        for name in sorted(os.listdir(base)):
            if name.endswith(".json"):
                rec = load(owner, name[:-5]) or {}
                rows.append({"case_id": name[:-5], "title": rec.get("title", ""),
                             "hypotheses": len(rec.get("hypotheses") or []),
                             "observations": len(rec.get("observed") or [])})
    return rows


def delete(owner: str, case_id: str) -> bool:
    try:
        os.unlink(_path(owner, case_id))
        return True
    except FileNotFoundError:
        return False


def _merge_observed(old: List[dict], new: Any) -> List[dict]:
    if new in (None, "", []):
        return list(old)
    if isinstance(new, dict):
        new = [{"candidate": k, "result": v} for k, v in new.items()] if "candidate" not in new else [new]
    return list(old) + [o for o in new if isinstance(o, dict)]


def advance(args: Dict[str, Any], owner: str = "") -> Dict[str, Any]:
    """Run the sensor. With a `case_id` the case is loaded, updated with what
    the call supplies (hypotheses/candidates replace, observations append) and
    saved; without one nothing is stored unless `save` is true."""
    action = str(args.get("action") or "next").strip().lower()
    case_id = str(args.get("case_id") or "").strip()
    if action == "list":
        return {"cases": list_cases(owner)}
    if action in ("get", "delete") and not case_id:
        raise ValueError(f"action={action} needs case_id")
    if action == "delete":
        return {"deleted": delete(owner, case_id), "case_id": case_id}
    if action not in ("next", "get", "update"):
        raise ValueError("action must be next, get, list or delete")

    record = load(owner, case_id) if case_id else None
    if case_id and record is None and action == "get":
        raise ValueError(f"no such case {case_id!r}")
    record = record or {"title": "", "hypotheses": None, "candidates": None, "observed": [], "settings": {}}
    created = not (case_id and load(owner, case_id))

    if args.get("hypotheses") is not None:
        record["hypotheses"] = args["hypotheses"]
    if args.get("candidates") is not None:
        record["candidates"] = args["candidates"]
    if args.get("title"):
        record["title"] = str(args["title"])[:200]
    if args.get("reset_observations"):
        record["observed"] = []
    obs_in = args.get("observed")
    if obs_in is None and args.get("observe") is not None:
        obs_in = args["observe"]
    record["observed"] = _merge_observed(record.get("observed") or [], obs_in)
    for key in ("noise", "stop_threshold", "min_gain_per_cost"):
        if args.get(key) is not None:
            record.setdefault("settings", {})[key] = args[key]
    if not record.get("hypotheses") or not record.get("candidates"):
        raise ValueError("hypotheses and candidates are required (the case has none stored yet)")

    result = sensor.next_observation(record["hypotheses"], record["candidates"], record["observed"],
                                     **(record.get("settings") or {}))
    persist = bool(case_id) or bool(args.get("save"))
    if persist:
        if not case_id:
            case_id = "UC-" + secrets.token_hex(4)
        save(owner, case_id, record)
        result["case_id"] = case_id
        result["case_created"] = created
    return result
