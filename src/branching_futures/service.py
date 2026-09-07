"""Create equivalent branches, record observed results and select safely."""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Mapping, Optional, Tuple

from src.branching_futures import contracts, persistence
from src.contracts.base import fingerprint, now_iso

_service: Optional["BranchingService"] = None


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class BranchingService:
    def __init__(self, store: Any = None) -> None:
        self._store = store

    def _db(self):
        return self._store or persistence.store()

    def _emit(self, owner: str, name: str, kind: str, ident: str, payload: Mapping[str, Any]) -> None:
        self._db().emit(owner=owner, name=name, entity_kind=kind, entity_id=ident, payload=payload)

    def create(self, *, owner: str, request: Any, project_id: str = "",
               session_id: str = "") -> Dict[str, Any]:
        data = contracts.future_request(request)
        data["project_id"] = project_id or data["project_id"]
        data["session_id"] = session_id or data["session_id"]
        future_id = _id("future")
        snapshot_id = _id("base")
        base_payload = {"id": snapshot_id, "future_run_id": future_id,
                        "project_execution_context": {"project_id": data["project_id"],
                                                      "session_id": data["session_id"]},
                        **data["base_snapshot"]}
        base_payload["fingerprint"] = fingerprint(
            (("kind", "branch_base_snapshot"), ("snapshot", base_payload))
        )
        self._db().put("base_snapshot", base_payload, owner=owner,
                       project_id=data["project_id"], session_id=data["session_id"], status="frozen")
        branches = []
        for strategy in data["strategies"]:
            branch_id = _id("branch")
            namespace_prefix = "simulation" if data["mode"] in ("plan_only", "simulate", "shadow") else "branch"
            branch = {"id": branch_id, "future_run_id": future_id,
                      "base_snapshot_id": snapshot_id, "strategy": strategy,
                      "status": "pending", "namespace": f"{namespace_prefix}:{future_id}/{branch_id}",
                      "effect_policy": {"real_external_effects": False,
                                        "workspace_isolated": data["mode"] != "plan_only"},
                      "result_id": "", "started_at": "", "completed_at": ""}
            branches.append(self._db().put("branch", branch, owner=owner,
                                           project_id=data["project_id"], session_id=data["session_id"],
                                           status="pending"))
        from src.branching_futures.activation import decide
        activation = decide({"requested": True})
        future = {"id": future_id, "title": data["title"], "intent": data["intent"],
                  "project_id": data["project_id"], "session_id": data["session_id"],
                  "mode": data["mode"], "status": "running", "base_snapshot_id": snapshot_id,
                  "base_fingerprint": base_payload["fingerprint"],
                  "criteria": data["criteria"], "budget": data["budget"],
                  "budget_usage": {"branches_started": 0, "total_cost": 0.0,
                                   "total_tokens": 0.0, "wall_seconds": 0.0},
                  "activation": activation,
                  "auto_select": data["auto_select"], "branch_ids": [row["id"] for row in branches],
                  "selection_id": "", "selected_branch_id": "", "commit": {}, "started_at": now_iso()}
        saved = self._db().put("future", future, owner=owner, project_id=data["project_id"],
                              session_id=data["session_id"], status="running")
        self._emit(owner, "branching_future_created", "future", future_id,
                   {"base_snapshot_id": snapshot_id, "base_fingerprint": base_payload["fingerprint"],
                    "branch_ids": future["branch_ids"], "mode": data["mode"]})
        return {**saved, "branches": branches, "base_snapshot": base_payload}

    def future(self, *, owner: str, future_id: str) -> Optional[Dict[str, Any]]:
        future = self._db().get("future", future_id, owner=owner)
        if future is None:
            return None
        future["branches"] = [row for ident in future.get("branch_ids", [])
                              if (row := self._db().get("branch", ident, owner=owner)) is not None]
        future["results"] = [row for branch in future["branches"]
                             if branch.get("result_id")
                             and (row := self._db().get("result", branch["result_id"], owner=owner)) is not None]
        if future.get("selection_id"):
            future["selection"] = self._db().get("selection", future["selection_id"], owner=owner)
        return future

    def futures(self, *, owner: str, project_id: str = "", status: str = "",
                limit: int = 100) -> List[Dict[str, Any]]:
        return self._db().list("future", owner=owner, project_id=project_id, status=status, limit=limit)

    def start_branch(self, *, owner: str, future_id: str, branch_id: str) -> Dict[str, Any]:
        future, branch = self._pair(owner, future_id, branch_id)
        if future.get("status") not in ("running", "evaluating") or branch.get("status") != "pending":
            raise contracts.BranchingError("branch.status", "branch cannot start", got=branch.get("status"))
        usage = future.get("budget_usage") or {}
        budget = future.get("budget") or {}
        if int(usage.get("branches_started") or 0) >= int(budget.get("max_branches") or len(future.get("branch_ids") or [])):
            raise contracts.BranchingError("future.budget", "branch budget is exhausted")
        for used_key, max_key in (("total_cost", "max_total_cost"),
                                  ("total_tokens", "max_total_tokens"),
                                  ("wall_seconds", "max_wall_seconds")):
            cap = float(budget.get(max_key) or 0)
            if cap > 0 and float(usage.get(used_key) or 0) >= cap:
                raise contracts.BranchingError("future.budget", f"{max_key} is exhausted")
        branch.update({"status": "running", "started_at": now_iso()})
        saved = self._db().put("branch", branch, owner=owner,
                              project_id=str(future.get("project_id") or ""),
                              session_id=str(future.get("session_id") or ""), status="running",
                              expected_revision=int(branch["revision"]))
        fresh_future = self._db().get("future", future_id, owner=owner)
        fresh_usage = dict(fresh_future.get("budget_usage") or {})
        fresh_usage["branches_started"] = int(fresh_usage.get("branches_started") or 0) + 1
        fresh_future["budget_usage"] = fresh_usage
        self._db().put("future", fresh_future, owner=owner,
                       project_id=str(fresh_future.get("project_id") or ""),
                       session_id=str(fresh_future.get("session_id") or ""),
                       status=str(fresh_future.get("status") or "running"),
                       expected_revision=int(fresh_future["revision"]))
        self._emit(owner, "branching_branch_started", "branch", branch_id,
                   {"future_run_id": future_id, "namespace": saved["namespace"]})
        return saved

    def submit_result(self, *, owner: str, future_id: str, branch_id: str,
                      result: Any) -> Dict[str, Any]:
        future, branch = self._pair(owner, future_id, branch_id)
        if branch.get("status") != "running":
            raise contracts.BranchingError(
                "branch.status", "start the branch before submitting a result", got=branch.get("status")
            )
        data = contracts.result_request(result)
        declared = {criterion["name"] for criterion in future.get("criteria", [])}
        unknown_scores = set(data.get("scores", {})) - declared
        unknown_gates = set(data.get("gate_results", {})) - declared
        if unknown_scores or unknown_gates:
            raise contracts.BranchingError(
                "result", f"contains undeclared criteria: {sorted(unknown_scores | unknown_gates)}")
        result_id = _id("branchresult")
        row = {**data, "id": result_id, "future_run_id": future_id,
               "branch_id": branch_id, "base_snapshot_id": future["base_snapshot_id"],
               "strategy_id": (branch.get("strategy") or {}).get("id", ""),
               "namespace": branch["namespace"], "isolated": True,
               "observed_at": now_iso()}
        saved = self._db().put("result", row, owner=owner,
                              project_id=str(future.get("project_id") or ""),
                              session_id=str(future.get("session_id") or ""), status=data["status"])
        branch.update({"status": data["status"], "result_id": result_id, "completed_at": now_iso()})
        self._db().put("branch", branch, owner=owner,
                       project_id=str(future.get("project_id") or ""),
                       session_id=str(future.get("session_id") or ""), status=data["status"],
                       expected_revision=int(branch["revision"]))
        self._refresh_future(owner=owner, future=future)
        self._emit(owner, "branching_branch_completed", "branch", branch_id,
                   {"future_run_id": future_id, "result_id": result_id, "status": data["status"],
                    "isolated": True})
        return saved

    def _pair(self, owner: str, future_id: str, branch_id: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        future = self._db().get("future", future_id, owner=owner)
        branch = self._db().get("branch", branch_id, owner=owner)
        if future is None or branch is None or branch.get("future_run_id") != future_id:
            raise contracts.BranchingError("branch_id", "not found")
        return future, branch

    def _refresh_future(self, *, owner: str, future: Dict[str, Any]) -> None:
        branches = [self._db().get("branch", ident, owner=owner) for ident in future.get("branch_ids", [])]
        results = [self._db().get("result", str(row.get("result_id") or ""), owner=owner)
                   for row in branches if row and row.get("result_id")]
        usage = dict(future.get("budget_usage") or {})
        usage["total_cost"] = sum(float((row.get("cost") or {}).get("cost") or 0)
                                  for row in results if row)
        usage["total_tokens"] = sum(float((row.get("cost") or {}).get("tokens") or 0)
                                    for row in results if row)
        usage["wall_seconds"] = sum(float((row.get("cost") or {}).get("wall_seconds") or 0)
                                    for row in results if row)
        future["budget_usage"] = usage
        terminals = {"completed", "failed", "pruned", "cancelled", "inconclusive"}
        if branches and all(row and row.get("status") in terminals for row in branches):
            future["status"] = "ready_for_selection" if any(row.get("status") == "completed" for row in branches if row) else "inconclusive"
        self._db().put("future", future, owner=owner,
                       project_id=str(future.get("project_id") or ""),
                       session_id=str(future.get("session_id") or ""), status=future["status"],
                       expected_revision=int(future["revision"]))

    @staticmethod
    def _score(result: Mapping[str, Any], criteria: List[Mapping[str, Any]]) -> float:
        total = 0.0
        for criterion in criteria:
            if criterion.get("kind") == "blocking":
                continue
            value = float((result.get("scores") or {})[criterion["name"]])
            direction = -1.0 if criterion["direction"] == "min" else 1.0
            total += direction * float(criterion["weight"]) * value
        return total

    def evaluate(self, *, owner: str, future_id: str) -> Dict[str, Any]:
        future = self.future(owner=owner, future_id=future_id)
        if future is None:
            raise contracts.BranchingError("future_id", "not found")
        unfinished = [
            row["id"] for row in future.get("branches", [])
            if row.get("status") in ("pending", "running")
        ]
        if unfinished:
            raise contracts.BranchingError(
                "future.branches", "all branches must be terminal before evaluation", got=unfinished
            )
        candidates = [row for row in future.get("results", []) if row.get("status") == "completed"]
        if not candidates:
            raise contracts.BranchingError("future.results", "no completed branch can be evaluated")
        blocking = [c for c in future["criteria"] if c.get("kind") == "blocking"]
        scored = [c for c in future["criteria"] if c.get("kind") != "blocking"]
        ranking = []
        eligible_candidates = []
        for row in candidates:
            gates = row.get("gate_results") or {}
            failed_gates = [c["name"] for c in blocking
                            if (gates.get(c["name"]) or {}).get("status") != "passed"]
            missing_scores = [c["name"] for c in scored if c["name"] not in (row.get("scores") or {})]
            eligible = not failed_gates and not missing_scores
            item = {"branch_id": row["branch_id"], "result_id": row["id"],
                    # Missing values remain null; zero would be an invented
                    # measurement and could silently change the winner.
                    "score": self._score(row, future["criteria"]) if eligible else None,
                    "eligible": eligible, "failed_gates": failed_gates,
                    "missing_scores": missing_scores,
                    "limitations": row.get("limitations", []),
                    "proof_count": len(row.get("proof_refs") or [])}
            ranking.append(item)
            if eligible:
                eligible_candidates.append(row)
        ranking.sort(key=lambda item: (item["eligible"], item["score"] if item["score"] is not None else float("-inf"),
                                       item["proof_count"], item["branch_id"]), reverse=True)
        # A non-dominated set over the declared criteria, useful even when the
        # weighted winner changes with one preference slider.
        pareto = []
        for candidate in eligible_candidates:
            dominated = False
            for rival in eligible_candidates:
                if rival is candidate:
                    continue
                no_worse, better = True, False
                for criterion in future["criteria"]:
                    if criterion.get("kind") == "blocking":
                        continue
                    a = float((candidate.get("scores") or {}).get(criterion["name"], 0.0))
                    b = float((rival.get("scores") or {}).get(criterion["name"], 0.0))
                    if criterion["direction"] == "min":
                        a, b = -a, -b
                    no_worse = no_worse and b >= a
                    better = better or b > a
                if no_worse and better:
                    dominated = True
                    break
            if not dominated:
                pareto.append(candidate["branch_id"])
        future_row = self._db().get("future", future_id, owner=owner)
        best_score = next((row["score"] for row in ranking if row["eligible"]), None)
        ties = [row["branch_id"] for row in ranking
                if row["eligible"] and row["score"] == best_score] if best_score is not None else []
        evaluation_status = "ready_for_selection" if best_score is not None else "inconclusive"
        future_row["status"] = evaluation_status
        future_row["evaluation"] = {"ranking": ranking, "pareto_branch_ids": pareto,
                                    "tied_winner_branch_ids": ties,
                                    "evaluated_at": now_iso()}
        self._db().put("future", future_row, owner=owner,
                       project_id=str(future_row.get("project_id") or ""),
                       session_id=str(future_row.get("session_id") or ""), status=evaluation_status,
                       expected_revision=int(future_row["revision"]))
        self._emit(owner, "branching_evaluation_completed", "future", future_id,
                   {"ranking": ranking, "pareto_branch_ids": pareto})
        return future_row["evaluation"]

    def select(self, *, owner: str, future_id: str, branch_id: str,
               rationale: str, authority: str = "human") -> Dict[str, Any]:
        future, branch = self._pair(owner, future_id, branch_id)
        if branch.get("status") != "completed" or not branch.get("result_id"):
            raise contracts.BranchingError("branch.status", "only a completed branch can be selected",
                                           got=branch.get("status"))
        if not str(rationale).strip():
            raise contracts.BranchingError("rationale", "is required")
        evaluation = future.get("evaluation") or {}
        eligible = {row.get("branch_id") for row in evaluation.get("ranking", []) if row.get("eligible")}
        if branch_id not in eligible:
            raise contracts.BranchingError("branch_id", "branch is not eligible in the current evaluation")
        selection = {"id": _id("selection"), "future_run_id": future_id,
                     "branch_id": branch_id, "result_id": branch["result_id"],
                     "rationale": str(rationale).strip()[:8000], "authority": authority,
                     "base_fingerprint": future["base_fingerprint"], "selected_at": now_iso(),
                     "status": "selected"}
        saved = self._db().put("selection", selection, owner=owner,
                              project_id=str(future.get("project_id") or ""), status="selected")
        future.update({"status": "selected", "selection_id": saved["id"],
                       "selected_branch_id": branch_id})
        self._db().put("future", future, owner=owner,
                       project_id=str(future.get("project_id") or ""),
                       session_id=str(future.get("session_id") or ""), status="selected",
                       expected_revision=int(future["revision"]))
        self._emit(owner, "branching_branch_selected", "selection", saved["id"],
                   {"future_run_id": future_id, "branch_id": branch_id,
                    "authority": authority, "rationale": selection["rationale"]})
        return saved

    def revalidate(self, *, owner: str, future_id: str,
                   real_state_fingerprint: str) -> Dict[str, Any]:
        future = self._db().get("future", future_id, owner=owner)
        if future is None:
            raise contracts.BranchingError("future_id", "not found")
        if future.get("status") != "selected":
            raise contracts.BranchingError("future.status", "select a branch before commit",
                                           got=future.get("status"))
        applicable = str(real_state_fingerprint) == str(future.get("base_fingerprint"))
        result = {"status": "applicable" if applicable else "requires_regeneration",
                  "expected_fingerprint": future.get("base_fingerprint"),
                  "observed_fingerprint": str(real_state_fingerprint),
                  "checked_at": now_iso()}
        future["revalidation"] = result
        self._db().put("future", future, owner=owner,
                       project_id=str(future.get("project_id") or ""),
                       session_id=str(future.get("session_id") or ""), status=str(future["status"]),
                       expected_revision=int(future["revision"]))
        self._emit(owner, "branching_revalidation_completed", "future", future_id, result)
        return result

    def commit(self, *, owner: str, future_id: str, real_state_fingerprint: str,
               proof_refs: List[str], approved: bool) -> Dict[str, Any]:
        # Fail closed until the common Changeset/workflow committer can apply,
        # observe and prove the real effect.  A database receipt is not a commit.
        self.revalidate(owner=owner, future_id=future_id,
                        real_state_fingerprint=real_state_fingerprint)
        raise contracts.BranchingError(
            "commit", "real commit is unavailable until a common execution adapter is configured; no state was changed")

    def fuse(self, *, owner: str, future_id: str, parent_branch_ids: List[str],
             strategy: Mapping[str, Any]) -> Dict[str, Any]:
        future = self._db().get("future", future_id, owner=owner)
        if future is None:
            raise contracts.BranchingError("future_id", "not found")
        if future.get("status") not in ("ready_for_selection", "selected"):
            raise contracts.BranchingError(
                "future.status", "evaluate the completed parent branches before fusion",
                got=future.get("status"),
            )
        parents = list(dict.fromkeys(str(x) for x in parent_branch_ids if str(x).strip()))
        if len(parents) < 2:
            raise contracts.BranchingError("parent_branch_ids", "at least two distinct parents are required")
        branch_rows = [row for row in (self._db().get("branch", ident, owner=owner)
                                       for ident in future.get("branch_ids", [])) if row]
        known = {row["id"] for row in branch_rows}
        if any(parent not in known for parent in parents):
            raise contracts.BranchingError("parent_branch_ids", "contains a branch outside this future")
        by_id = {row["id"]: row for row in branch_rows}
        if any(by_id[parent].get("status") != "completed" or not by_id[parent].get("result_id")
               for parent in parents):
            raise contracts.BranchingError(
                "parent_branch_ids", "fusion parents must have completed observed results"
            )
        if len(future.get("branch_ids", [])) >= int((future.get("budget") or {}).get("max_branches") or 0):
            raise contracts.BranchingError("future.budget.max_branches", "cannot create a fusion branch")
        normalized_strategy = contracts.strategy_request(
            strategy, path="fusion.strategy", default_id="fusion"
        )
        existing_strategy_ids = {
            str((row.get("strategy") or {}).get("id") or "") for row in branch_rows
        }
        if normalized_strategy["id"] in existing_strategy_ids:
            raise contracts.BranchingError("fusion.strategy.id", "must be unique within the future")
        branch_id = _id("branch")
        branch = {"id": branch_id, "future_run_id": future_id,
                  "base_snapshot_id": future["base_snapshot_id"], "strategy": normalized_strategy,
                  "status": "pending", "namespace": f"branch:{future_id}/{branch_id}",
                  "effect_policy": {"real_external_effects": False, "workspace_isolated": True},
                  "parent_branch_ids": parents, "result_id": "", "started_at": "", "completed_at": ""}
        saved = self._db().put("branch", branch, owner=owner,
                              project_id=str(future.get("project_id") or ""),
                              session_id=str(future.get("session_id") or ""), status="pending")
        future["branch_ids"] = list(future.get("branch_ids", [])) + [saved["id"]]
        future.update({"status": "running", "selection_id": "", "selected_branch_id": ""})
        future.pop("evaluation", None)
        self._db().put("future", future, owner=owner,
                       project_id=str(future.get("project_id") or ""),
                       session_id=str(future.get("session_id") or ""), status="running",
                       expected_revision=int(future["revision"]))
        self._emit(owner, "branching_fusion_created", "branch", branch_id,
                   {"future_run_id": future_id, "parent_branch_ids": parents})
        return saved

    def cancel(self, *, owner: str, future_id: str) -> Dict[str, Any]:
        future = self._db().get("future", future_id, owner=owner)
        if future is None:
            raise contracts.BranchingError("future_id", "not found")
        if future.get("status") in ("committed", "cancelled"):
            raise contracts.BranchingError("future.status", "cannot be cancelled", got=future.get("status"))
        future["status"] = "cancelled"
        saved = self._db().put("future", future, owner=owner,
                              project_id=str(future.get("project_id") or ""),
                              session_id=str(future.get("session_id") or ""), status="cancelled",
                              expected_revision=int(future["revision"]))
        self._emit(owner, "branching_future_cancelled", "future", future_id, {})
        for branch_id in future.get("branch_ids", []):
            branch = self._db().get("branch", branch_id, owner=owner)
            if branch and branch.get("status") in ("pending", "running"):
                branch["status"] = "cancelled"
                branch["completed_at"] = now_iso()
                self._db().put("branch", branch, owner=owner,
                               project_id=str(future.get("project_id") or ""),
                               session_id=str(future.get("session_id") or ""), status="cancelled",
                               expected_revision=int(branch["revision"]))
        return saved

    def events(self, *, owner: str, since: int = 0, limit: int = 200) -> List[Dict[str, Any]]:
        return self._db().events(owner=owner, since=since, limit=limit)


def service() -> BranchingService:
    global _service
    if _service is None:
        _service = BranchingService()
    return _service


def reset_service() -> None:
    global _service
    _service = None
