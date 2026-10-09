"""Confirm native receipts only after their full content reaches a model request.

Execution, formatting, a UI event or an offload preview is insufficient. The
final transport payload is observed through the existing prompt audit sink;
confirmation waits for that candidate to answer successfully.
"""
from __future__ import annotations

from copy import deepcopy

from src.context_engine.prompt_audit import PromptAuditRecorder
from .tools import NAMES


def text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(block.get("text", "")) for block in content if isinstance(block, dict) and block.get("type") == "text")
    return ""


class CoordinationAudit(PromptAuditRecorder):
    def __init__(self, *, grant=None, **kwargs):
        super().__init__(**kwargs)
        self.grant = grant
        self.pending = {}
        self.wired = {}

    def stage(self, records):
        if self.grant is None:
            return
        for record in records:
            result = record.get("result") or {}
            output = result.get("output")
            call_id = record.get("call_id")
            if (record.get("tool_name") not in NAMES or result.get("exit_code") != 0
                    or not result.get("_coordination_delivery") or not call_id
                    or not isinstance(output, str) or not output or output not in record.get("text", "")):
                continue
            # Both output and rendered representation must be complete. Never
            # derive a receipt from model-authored text or a shortened preview.
            self.pending[call_id] = {"result": deepcopy(result), "output": output, "rendered": record["text"]}

    def observe_wire(self, round_num, payload):
        super().observe_wire(round_num, payload)
        candidate = self._by_round.get(int(round_num), {}).get("candidate", 0)
        self.wired[(int(round_num), candidate)] = set()
        messages = payload.get("messages") if isinstance(payload, dict) else None
        if not isinstance(messages, list):
            return
        delivered = set()
        for call_id, record in self.pending.items():
            for message in messages:
                body = text(message.get("content"))
                native = message.get("role") == "tool" and message.get("tool_call_id") == call_id
                if message.get("role") == "user" and isinstance(message.get("content"), list):
                    for block in message["content"]:
                        if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id") == call_id:
                            if record["output"] in text(block.get("content")):
                                delivered.add(call_id)
                fenced = message.get("role") == "user" and record["rendered"] in body and "UNTRUSTED" in body
                if (native or fenced) and record["output"] in body:
                    delivered.add(call_id)
                    break
        # Only the latest outgoing payload for a candidate can be confirmed.
        self.wired[(int(round_num), candidate)] = delivered

    def accepted(self, round_num, candidate=0):
        if self.grant is None:
            return
        for call_id in self.wired.get((int(round_num), candidate), ()):
            record = self.pending.get(call_id)
            if record is None:
                continue
            try:
                self.grant.delivered(record["result"])
            except Exception:
                # Revocation or failed persistence leaves the detail pending.
                continue
            self.pending.pop(call_id, None)
