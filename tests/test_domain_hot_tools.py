"""A request in a detected domain gets that domain's everyday tools as full
schemas on the first round (src/agent_loop.py `_DOMAIN_HOT_TOOLS`).

Seen live: «Mira mi calendario de esta semana y dime qué día tengo más
libre» matched the calendar domain, retrieval returned unrelated MCP tools,
`manage_calendar` went to the catalog, and the turn spent a round on
`lookup_tools` and a full prompt reprocess before it could read the calendar."""
import asyncio
import json

import src.agent_loop as al
import src.tool_index as ti


def _names(tools):
    return {t.get("function", {}).get("name") or t.get("name") for t in (tools or [])}


def test_domain_hot_tools_belong_to_their_domain():
    for domain, names in al._DOMAIN_HOT_TOOLS.items():
        assert names <= al._DOMAIN_TOOL_MAP[domain], domain


def test_a_calendar_question_gets_the_calendar_tools_on_round_one(monkeypatch, caplog):
    caplog.set_level("INFO", logger="src.agent_loop")
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)

    class FakeIndex:
        def get_tools_for_query(self, query, k=8, **kwargs):
            return {"web_fetch", "ask_user"}

        def index_mcp_tools(self, *a, **k):
            return None

    monkeypatch.setattr(ti, "get_tool_index", lambda: FakeIndex())
    monkeypatch.setattr(ti, "tool_rerank_options", lambda owner: {})
    sent = []

    async def _fake_stream(_candidates, messages, **kwargs):
        sent.append(kwargs.get("tools"))
        yield "data: " + json.dumps({"delta": "ok"}) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    async def _run():
        return [c async for c in al.stream_agent_loop(
            "https://api.openai.com/v1", "gpt-test",
            [{"role": "user", "content": "Mira mi calendario de esta semana y dime qué día tengo más libre."}],
            max_rounds=1, relevant_tools=None)]

    asyncio.run(_run())
    # Which of the three have a schema depends on what this test process has
    # configured; none of them may wait in the catalog.
    assert sent and "manage_notes" in _names(sent[0])
    debug = [r.getMessage() for r in caplog.records if "[agent-debug] round=1" in r.getMessage()]
    assert debug and "deferred=[]" in debug[0]
