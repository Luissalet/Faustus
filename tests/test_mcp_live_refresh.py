"""A server restarted mid-turn with a new contract is announced live from the next step on."""
from src.agent_loop import _refresh_live_mcp_schemas
from src.step_snapshot import authorize_call, capture_step


def schema(name, props, description="Use it."):
    return {"type": "function", "function": {"name": name, "description": description,
                                             "parameters": {"type": "object", "properties": props}}}


OLD = schema("mcp__browser_session__browser_session", {"action": {"type": "string"}})
NEW = schema("mcp__browser_session__browser_session",
             {"action": {"type": "string"}, "view": {"type": "object", "properties": {"visible": {"type": "boolean"}}}},
             description="Use it, now with a visible view.")
OTHER = schema("mcp__notes__search", {"q": {"type": "string"}})


class Manager:
    def __init__(self, schemas, fail=False):
        self.schemas, self.fail, self.calls = schemas, fail, 0

    def get_all_openai_schemas(self, disabled_map=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("catalogue unavailable")
        return self.schemas


def test_a_changed_contract_is_swapped_for_the_live_one_and_the_call_is_then_allowed():
    offered = [OLD, OTHER]
    refreshed = _refresh_live_mcp_schemas(offered, Manager([NEW, OTHER]))
    assert refreshed[0] is NEW and refreshed[1] is OTHER
    live = {NEW["function"]["name"]: NEW["function"]}.get
    stale = capture_step([OLD], session_id="s", run_id="r", round_num=2, candidate_index=0)
    assert authorize_call(stale, NEW["function"]["name"], live_definition=live).status == "contract_changed"
    fresh = capture_step(refreshed, session_id="s", run_id="r", round_num=3, candidate_index=0)
    assert authorize_call(fresh, NEW["function"]["name"], live_definition=live).allowed


def test_unchanged_vanished_or_unreadable_keep_what_was_offered():
    offered = [OLD, OTHER]
    assert _refresh_live_mcp_schemas(offered, Manager([OLD, OTHER])) is offered
    assert _refresh_live_mcp_schemas(offered, Manager([OTHER])) is offered          # the call gate revokes it
    assert _refresh_live_mcp_schemas(offered, Manager([], fail=True)) is offered
    assert _refresh_live_mcp_schemas(offered, None) is offered
    retold = schema(OLD["function"]["name"], OLD["function"]["parameters"]["properties"], description="Reworded.")
    assert _refresh_live_mcp_schemas(offered, Manager([retold, OTHER])) is offered  # descriptions apart
