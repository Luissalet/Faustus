"""support_bundle.py — OBS-04: reproduce a bug without shipping a person's data.

A support bundle answers "what would a developer need to reproduce this",
never "here is everything". Three pieces, each reused rather than
reimplemented, each already safe by construction before this module touches
it:

  * effective configuration — `src.effective_config.compile_effective`
    already masks secrets (`_mask_value`) at the source; this module never
    unmasks anything, it only serializes what that function already gives;
  * recent events — the on-disk replay log under DATA_DIR/runs (the same
    location `src.agent_runs`'s `_RunLog` writes) is read field-ALLOWLISTED:
    structural fields only (trace_id, phase, tool, status...), never the
    conversational text/delta fields those same lines also carry. An
    allowlist cannot miss a new personal field a future event type adds; a
    denylist could;
  * a log excerpt — the tail of THIS process's own configured log file,
    secret-pattern redacted. No attempt to discover or read any other file
    on disk.

Nothing here reads a chat message, an uploaded file, or a credential. The
"select and preview before sharing" step (OBS-04's frontend half) is a
Studio surface, not this module's job — this module's contract is that
whatever it hands back is safe to look at by default, so a preview step has
something honest to show rather than something to filter first.

CLI: `python -m src.support_bundle [--output-dir DIR]` — writes a bundle zip
and prints its path. Reproducible: same inputs (same run logs, same
effective-config inputs) produce the same content modulo the timestamp.
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import platform
import re
import sys
import time
import zipfile
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Structural fields a replay-log line may carry that are safe to keep
# verbatim in a support bundle. Everything else on the line (prose deltas,
# tool arguments/output, file contents) is dropped outright.
_SAFE_EVENT_FIELDS = (
    "event", "type", "phase", "tool", "tool_name", "status", "stop_reason",
    "trace_id", "step_id", "call_id", "sequence", "stream_id", "ts", "timestamp",
    "error", "http_status", "duration_ms",
)

_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{10,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{10,}"),
    re.compile(r"[A-Za-z0-9_\-]{15,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),  # jwt-shaped
]


def _redact(text: str) -> str:
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub("[redacted]", out)
    return out


def _data_dir() -> str:
    try:
        from src.constants import DATA_DIR
        return DATA_DIR
    except Exception:  # pragma: no cover
        return os.path.join(os.getcwd(), "data")


def version_info() -> Dict[str, Any]:
    """Everything a developer needs to know which build produced a bug,
    nothing that identifies who hit it."""
    info: Dict[str, Any] = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
    }
    try:
        from src import api_version
        info["api_version"] = api_version.API_VERSION
        info["min_client_version"] = api_version.MIN_CLIENT_VERSION
    except Exception as e:  # noqa: BLE001
        logger.debug("support_bundle: api_version unavailable: %s", e)
    return info


def effective_config_snapshot(*, owner: str = "", session_id: str = "", project_id: str = "") -> Dict[str, Any]:
    """Reused, not reimplemented — `compile_effective` already masks secrets
    before a value ever reaches this function."""
    try:
        from src import effective_config as ec
        cfg = ec.compile_effective(owner=owner, session_id=session_id or None, project_id=project_id or None)
        return cfg.to_dict()
    except Exception as e:  # noqa: BLE001 - a bundle with a note beats one that crashes
        logger.debug("support_bundle: effective_config unavailable: %s", e)
        return {"error": str(e)[:200]}


def recent_events(*, limit: int = 200, max_files: int = 5) -> List[Dict[str, Any]]:
    """The newest events across the newest run-replay logs, field-
    allowlisted to structural data only. Best effort: a missing or
    unreadable runs directory yields an empty list, never a raise."""
    runs_dir = os.path.join(_data_dir(), "runs")
    if not os.path.isdir(runs_dir):
        return []
    try:
        files = sorted(glob.glob(os.path.join(runs_dir, "*.jsonl")),
                       key=lambda p: os.path.getmtime(p), reverse=True)[:max_files]
    except OSError:
        return []
    events: List[Dict[str, Any]] = []
    for path in files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-limit:]
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except ValueError:
                continue
            if not isinstance(raw, dict):
                continue
            safe = {k: raw[k] for k in _SAFE_EVENT_FIELDS if k in raw}
            if safe:
                events.append(safe)
    events.sort(key=lambda e: e.get("ts") or e.get("sequence") or 0)
    return events[-limit:]


def sanitized_log_excerpt(*, max_lines: int = 500) -> str:
    """The tail of this process's OWN configured log file, secret-redacted.
    Empty when no log file is configured — never a search for one."""
    log_path = os.getenv("ODYSSEUS_LOG_FILE") or os.getenv("FAUSTUS_LOG_FILE") or ""
    if not log_path or not os.path.isfile(log_path):
        return ""
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-max_lines:]
    except OSError as e:  # noqa: BLE001
        return f"[log unreadable: {e}]"
    return _redact("".join(lines))


def build_bundle(
    *, output_dir: Optional[str] = None, owner: str = "", session_id: str = "",
    project_id: str = "", include_events: bool = True, include_log: bool = True,
    event_limit: int = 200,
) -> str:
    """Assemble a support bundle zip and return its path. Nothing here is
    personal content by default: `effective_config_snapshot` is masked at
    the source, `recent_events` is field-allowlisted, `sanitized_log_excerpt`
    is secret-redacted. Anything MORE (an actual conversation, a specific
    file) is a person choosing to attach it elsewhere — this function never
    reaches for it on its own, which is what `manifest.json`'s
    `personal_content_included: false` documents in the bundle itself."""
    output_dir = output_dir or os.path.join(_data_dir(), "support_bundles")
    os.makedirs(output_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    bundle_path = os.path.join(output_dir, f"support_bundle_{stamp}.zip")

    manifest = {
        "created_at": stamp,
        "version": version_info(),
        "personal_content_included": False,
        "contents": ["manifest.json", "version.json", "effective_config.json"]
                    + (["recent_events.json"] if include_events else [])
                    + (["log_excerpt.txt"] if include_log else []),
    }
    with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        zf.writestr("version.json", json.dumps(version_info(), ensure_ascii=False, indent=2))
        zf.writestr("effective_config.json", json.dumps(
            effective_config_snapshot(owner=owner, session_id=session_id, project_id=project_id),
            ensure_ascii=False, indent=2))
        if include_events:
            zf.writestr("recent_events.json", json.dumps(
                recent_events(limit=event_limit), ensure_ascii=False, indent=2))
        if include_log:
            zf.writestr("log_excerpt.txt", sanitized_log_excerpt())
    return bundle_path


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Build a Faustus support bundle (OBS-04).")
    p.add_argument("--output-dir", default=None)
    p.add_argument("--owner", default="")
    p.add_argument("--session-id", default="")
    p.add_argument("--project-id", default="")
    p.add_argument("--no-events", action="store_true")
    p.add_argument("--no-log", action="store_true")
    args = p.parse_args(argv)
    path = build_bundle(
        output_dir=args.output_dir, owner=args.owner, session_id=args.session_id,
        project_id=args.project_id, include_events=not args.no_events, include_log=not args.no_log,
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
