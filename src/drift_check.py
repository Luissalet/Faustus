"""src/drift_check.py — the agent harness's hook into
`src.code_graph.drift`: one architecture-drift check per turn, in a bound
workspace, entirely off the turn's critical path.

`DriftCheckState` is instantiated once per turn next to `TurnLedger`
(`src/agent_loop.py`). Two calls, both meant to run through
`asyncio.to_thread` with a timeout by the caller (this class does not do its
own asyncio — it is plain, synchronous, blocking code, exactly like
`src.code_graph.drift` itself):

* `snapshot_before_first_edit()` — called once the turn's first successful
  mutation is recorded. Records a baseline UNLESS the setting
  `code_graph_drift_check` is off, the workspace has fewer than
  `code_graph_drift_min_files` files, or anything about taking the snapshot
  fails — any of those permanently disables the rest of the turn's check
  (`self.enabled = False`), never retried.
* `run_after_turn()` — called once, right before the turn's harness summary
  is built. Compares the current graph against the baseline this turn took
  and returns a short note to append to `TurnLedger.notes`/the summary's
  metadata, or `None` when there is nothing worth mentioning (below
  `code_graph_drift_note_threshold`) or the check never got a baseline in
  the first place.

Every failure mode here is "quietly do nothing" — this must never turn into
a blocked or failed turn, and the caller is expected to wrap both calls in
`asyncio.wait_for(..., timeout=...)` for the same reason (a slow Louvain
pass on a huge repo is a reason to skip the note, not a reason to make the
user wait for it).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: Below this many indexed files, drift is not worth checking -- everything
#: "moves" when there are only a handful of files.
DEFAULT_MIN_FILES = 8
#: Wall-clock ceiling handed to `code_graph.drift` itself (its own budget,
#: separate from whatever `asyncio.wait_for` timeout the caller applies).
DEFAULT_TIME_BUDGET_S = 8.0
#: A drift score at or above this attaches a note to the turn summary.
DEFAULT_NOTE_THRESHOLD = 25


def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:  # noqa: BLE001
        return default


class DriftCheckState:
    """One instance per turn. `workspace` may be empty (no bound workspace):
    every method is then a permanent no-op, same as the setting being off."""

    def __init__(self, workspace: Optional[str], project_id: str = ""):
        self.workspace = str(workspace or "")
        self.project_id = str(project_id or "")
        self.enabled = bool(self.workspace) and bool(_setting("code_graph_drift_check", True))
        self.snapshot_taken = False
        self.baseline_id: Optional[str] = None
        self.result: Optional[Dict[str, Any]] = None
        self.note: Optional[str] = None

    def _too_small(self) -> bool:
        try:
            from src.context_engine import code_index
            status = code_index.status(self.workspace, project_id=self.project_id)
            min_files = int(_setting("code_graph_drift_min_files", DEFAULT_MIN_FILES) or DEFAULT_MIN_FILES)
            return int(status.get("files") or 0) < max(1, min_files)
        except Exception as exc:  # noqa: BLE001 - unknown size is treated as "skip"
            logger.debug("drift_check: size check failed: %s", exc)
            return True

    def snapshot_before_first_edit(self) -> None:
        """Best-effort baseline for this turn. Idempotent — a second call
        (the loop should not make one, but nothing relies on that) is a
        no-op. Never raises."""
        if not self.enabled or self.snapshot_taken:
            return
        self.snapshot_taken = True  # never retried this turn, success or failure
        try:
            if self._too_small():
                logger.debug("drift_check: workspace too small, skipping for this turn")
                self.enabled = False
                return
            # `src.code_graph`'s package `__init__` re-exports the `snapshot`/
            # `drift` FUNCTIONS (not the `src.code_graph.drift` submodule) —
            # calling them off the package, as every other caller of this
            # package does, sidesteps the submodule-vs-function name clash
            # entirely (see tests/test_code_graph_communities.py's own note
            # on this for why `import src.code_graph.drift as x` is a trap).
            import src.code_graph as code_graph
            out = code_graph.snapshot(self.workspace, project_id=self.project_id, label="turn-start")
            if out.get("exit_code") == 0 and out.get("baseline_id"):
                self.baseline_id = str(out["baseline_id"])
            else:
                logger.debug("drift_check: snapshot failed: %s", out.get("error"))
                self.enabled = False
        except Exception as exc:  # noqa: BLE001
            logger.debug("drift_check: snapshot raised: %s", exc)
            self.enabled = False

    def run_after_turn(self) -> Optional[str]:
        """Best-effort drift comparison against this turn's own baseline.
        Returns a short note for the turn summary, or None. Never raises."""
        if not self.enabled or not self.baseline_id:
            return None
        try:
            import src.code_graph as code_graph
            budget = float(_setting("code_graph_drift_time_budget_s", DEFAULT_TIME_BUDGET_S)
                          or DEFAULT_TIME_BUDGET_S)
            out = code_graph.drift(self.workspace, project_id=self.project_id,
                                   baseline_id=self.baseline_id, time_budget_s=budget)
            self.result = out
            if out.get("exit_code") != 0:
                logger.debug("drift_check: comparison failed: %s", out.get("error"))
                return None
            threshold = int(_setting("code_graph_drift_note_threshold", DEFAULT_NOTE_THRESHOLD)
                           or DEFAULT_NOTE_THRESHOLD)
            score = int(out.get("score") or 0)
            if score < threshold:
                return None
            top = out.get("top_findings") or []
            headline = "; ".join(
                str(f.get("explanation")) for f in top[:2] if isinstance(f, dict) and f.get("explanation")
            )
            note = f"architecture drift: score {score}/100 vs baseline {self.baseline_id}"
            if headline:
                note += f" — {headline}"
            self.note = note
            return note
        except Exception as exc:  # noqa: BLE001
            logger.debug("drift_check: comparison raised: %s", exc)
            return None


__all__ = ["DriftCheckState", "DEFAULT_MIN_FILES", "DEFAULT_TIME_BUDGET_S", "DEFAULT_NOTE_THRESHOLD"]
