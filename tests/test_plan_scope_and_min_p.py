"""A chat without project or workspace keeps its plan to itself."""
from src import plan_tracker as pt


def test_chats_without_workspace_do_not_share_a_plan_scope():
    a = pt.scope_for(None, None, "chat-a")
    b = pt.scope_for(None, "", "chat-b")
    assert a != b and a == "session-chat-a"
    assert pt.scope_for("proj1", None, "chat-a") == "proj1"
    assert pt.scope_for(None, "D:\\Repo", "chat-a") == pt.scope_for(None, "d:/repo/", "chat-b")


def test_min_p_rejection_is_remembered_per_server():
    from src import llm_core
    llm_core._MIN_P_REJECTED_HOSTS.discard("10.9.9.9:8002")
    payload = {"min_p": 0.05}
    msg = "The min_p and logit_bias sampling parameters are not yet supported with speculative decoding."
    assert llm_core._neutralize_rejected_automatic_min_p(payload, "http://10.9.9.9:8002/v1/chat/completions", 400, msg, automatic=True)
    assert "10.9.9.9:8002" in llm_core._MIN_P_REJECTED_HOSTS