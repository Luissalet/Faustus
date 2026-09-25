"""Reading back an artifact this run's own tool output was stored in.

Seen live on the 27B: a model evaluation was large enough to be offloaded,
the model opened the stored copy with `read_artifact` to find the AUC, and
the turn stopped at «Allow this task to continue?» — to read its own result.
"""
from src.prompt_security import untrusted_context_message
from src.tool_capabilities import ToolRunSecurityContext

OWN = "occ_" + "a" * 32
OTHER = "occ_" + "b" * 32


def _armed():
    ctx = ToolRunSecurityContext(user_request="dame su diagnóstico")
    ctx.observe_messages([untrusted_context_message("MCP tools", "- a tool a server described")])
    ctx.observe_tool_result("mcp__abcd__data_model", {"stdout": "{\"metrics\": {}}", "exit_code": 0})
    assert ctx.external_untrusted_context_seen
    return ctx


def test_an_artifact_this_run_produced_reads_without_a_card():
    ctx = _armed()
    ctx.note_run_artifacts({"stdout": f"[result stored as artifact {OWN}; open it with read_artifact]"})
    assert ctx.decision_for("read_artifact", {"artifact_id": OWN, "query": "auc"}).allowed is True
    assert ctx.decision_for("artifact_search", f'{{"artifact_id": "{OWN}"}}').allowed is True


def test_any_other_artifact_still_asks():
    ctx = _armed()
    ctx.note_run_artifacts({"stdout": f"stored as {OWN}"})
    assert ctx.decision_for("read_artifact", {"artifact_id": OTHER}).allowed is False
    assert ctx.decision_for("read_artifact", {"artifact_id": OWN, "also": OTHER}).allowed is False
    assert ctx.decision_for("read_artifact", {}).allowed is False
