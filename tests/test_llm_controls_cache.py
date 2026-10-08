import asyncio
from src import llm_core
from src.reasoning_levels import thinking_switch


def test_thinking_switch_keeps_effort_and_overrides_its_enable():
    assert thinking_switch({"think": True, "reasoning_effort": "high"}, False) == {
        "think": False, "reasoning_effort": "high"}


def test_generation_settings_partition_cached_answers():
    args = ("http://configured/v1", "glm-5.3-flash-nvfp4", [{"role": "user", "content": "same question"}], 0.6, 512)
    plain = llm_core._get_cache_key(*args)
    assert llm_core._get_cache_key(*args, gen_overrides={}) == plain
    keys = {llm_core._get_cache_key(*args, gen_overrides=overrides) for overrides in (
        {"think": False, "reasoning_effort": "high"},
        {"think": True, "reasoning_effort": "high"},
        {"think": True, "reasoning_effort": "low"},
        {"think": True, "reasoning_effort": "high", "reasoning_budget": 128},
    )}
    assert len(keys) == 4
    assert plain not in keys


def test_async_call_does_not_reuse_answer_after_switch_change(monkeypatch):
    import httpx
    sent = []
    cache = {}
    monkeypatch.setattr(llm_core, "_get_cached_response", cache.get)
    monkeypatch.setattr(llm_core, "_set_cached_response", lambda key, answer, **kw: cache.__setitem__(key, answer))
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda _: False)
    async def post(client, url, headers, **kwargs):
        payload = kwargs["json"]
        sent.append(payload)
        return httpx.Response(200, request=httpx.Request("POST", url),
                              json={"choices": [{"message": {"content": "answer " + str(len(sent))}}]})
    monkeypatch.setattr(llm_core, "httpx_post_kimi_aware_async", post)
    async def run():
        args = ("https://api.example/v1", "same-model", [{"role": "user", "content": "same prompt"}])
        first = await llm_core.llm_call_async(*args, gen_overrides={"think": False, "reasoning_effort": "high"})
        second = await llm_core.llm_call_async(*args, gen_overrides={"think": True, "reasoning_effort": "high"})
        assert first == "answer 1"
        assert second == "answer 2"
        assert len(sent) == 2
    asyncio.run(run())
