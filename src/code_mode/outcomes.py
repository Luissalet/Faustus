"""Bounded invocation receipts; never retain tool arguments or output bodies."""
from src.contracts.tool import TOOL_RESULT_STATUSES
from src.tool_presentation import tool_result_fields

MAX_RECEIPTS = 32


class CallOutcomes:
    def __init__(self):
        self.counts = {status: 0 for status in TOOL_RESULT_STATUSES}
        self.calls = 0
        self.records = []

    def begin(self, call_id, tool):
        self.calls += 1
        self.counts["outcome_unknown"] += 1
        record = {"call_id": str(call_id)[:512], "tool": str(tool)[:256],
                  "status": "outcome_unknown"}
        if len(self.records) < MAX_RECEIPTS:
            self.records.append(record)
        return record

    def finish(self, record, result):
        if isinstance(result, dict) and "result_status" in result:
            result = dict(result)
            status = result["result_status"]
            result["status"] = status if isinstance(status, str) and status in TOOL_RESULT_STATUSES else "outcome_unknown"
        if isinstance(result, dict) and "status" in result and not isinstance(result["status"], str):
            result = {"status": "outcome_unknown"}
        fields = tool_result_fields(result)
        self.counts[record["status"]] -= 1
        record["status"] = fields["result_status"]
        self.counts[record["status"]] += 1
        if "uncertainty" in fields:
            record["uncertainty"] = fields["uncertainty"]

    def fields(self):
        result = {"tool_outcomes": {"calls": self.calls, "counts": dict(self.counts),
                  "records": [dict(row) for row in self.records],
                  "omitted_records": self.calls - len(self.records)}}
        if self.counts["outcome_unknown"] or self.counts["cancelled"]:
            result["status"] = "outcome_unknown"
        elif self.counts["partial"]:
            result["status"] = "partial"
        if "status" in result:
            result["uncertainty"] = {"reason": "One or more nested tool calls have an unconfirmed or partial result.",
                                     "reconcile_action": "Inspect those calls before retrying their effects."}
        return result
