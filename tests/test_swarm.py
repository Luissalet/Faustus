"""Swarm map (src/swarm/): concurrency sized to the backend, llama.cpp slot
detection, retry and failure isolation, resume from the checkpoint, reduce,
exports/artifacts, the tools and the owner-scoped REST routes."""
from __future__ import annotations

import asyncio
import csv
import io
import json
import time

import pytest

from src.swarm import capacity, lane, render, runner, service, store

API_URL = "https://api.example.com/v1/chat/completions"
LLAMA_URL = "http://127.0.0.1:8080/v1/chat/completions"


@pytest.fixture
def env(tmp_path, monkeypatch):
    from src import constants
    import src.settings as settings_mod
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path / "data"))
    overrides = {"swarm_api_parallel": 3, "swarm_max_parallel": 16, "swarm_max_items": 200,
                 "swarm_ollama_parallel": 1}
    real_get = settings_mod.get_setting
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: overrides[key] if key in overrides else real_get(key, default))
    route = {"url": API_URL}
    monkeypatch.setattr(runner, "resolve_route",
                        lambda spec, owner: (route["url"], spec.get("model") or "m", None, "test"))
    from src import artifact_store
    persisted = []
    monkeypatch.setattr(artifact_store, "ARTIFACT_STORE_DIR", str(tmp_path / "store"))
    monkeypatch.setattr(artifact_store, "persist",
                        lambda arts, session_id="", call_id="": persisted.append((list(arts), session_id)))
    capacity._SLOT_CACHE.clear()
    service._TASKS.clear()
    return {"settings": overrides, "route": route, "persisted": persisted}


class FakeLLM:
    def __init__(self, delay=0.02, reply=None):
        self.delay = delay
        self.reply = reply or (lambda prompt, n: f"answer for {prompt.splitlines()[0]}")
        self.active = 0
        self.max_active = 0
        self.calls = []

    async def __call__(self, url, model, messages, *, headers, timeout, response_schema=None, max_tokens=0):
        prompt = messages[-1]["content"]
        self.calls.append(prompt)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delay)
            return self.reply(prompt, len(self.calls))
        finally:
            self.active -= 1


async def _run(owner, items, **kw):
    run_id = service.start(owner, "sess-1", kw.pop("instruction", "Describe {item}"), items,
                           run_in_background=False, **kw)
    manifest = await runner.run(run_id, owner)
    return run_id, manifest


# ── concurrency ───────────────────────────────────────────────────────────

async def test_concurrency_is_the_backend_parallelism(env, monkeypatch):
    # Long enough per item that the run's own bookkeeping (file writes, the
    # manifest) cannot decide the timing: 0.05 s per item failed on a loaded
    # Windows box with 0.48 s against a 0.45 s budget while running 3 at once,
    # and 0.2 s failed in the full parallel suite (1.38 s against 1.35 s).
    fake = FakeLLM(delay=0.5)
    monkeypatch.setattr(runner, "_llm_call", fake)
    t0 = time.monotonic()
    run_id, manifest = await _run("ana", [f"c{i}" for i in range(9)])
    elapsed = time.monotonic() - t0
    assert fake.max_active == 3            # swarm_api_parallel
    assert manifest["status"] == "done"
    assert manifest["counts"] == {"total": 9, "ok": 9, "failed": 0, "pending": 0}
    assert manifest["parallel"]["effective"] == 3
    assert elapsed < 0.5 * 9 * 0.75        # clearly less than one after another would take


async def test_concurrency_never_exceeds_the_global_cap_or_the_item_count(env, monkeypatch):
    env["settings"]["swarm_api_parallel"] = 50
    env["settings"]["swarm_max_parallel"] = 4
    fake = FakeLLM(delay=0.03)
    monkeypatch.setattr(runner, "_llm_call", fake)
    _, manifest = await _run("ana", [f"x{i}" for i in range(10)])
    assert fake.max_active == 4
    fake2 = FakeLLM(delay=0.03)
    monkeypatch.setattr(runner, "_llm_call", fake2)
    _, manifest2 = await _run("ana", ["a", "b"])
    assert manifest2["parallel"]["effective"] == 2


async def test_max_parallel_argument_lowers_but_never_raises(env, monkeypatch):
    fake = FakeLLM(delay=0.03)
    monkeypatch.setattr(runner, "_llm_call", fake)
    await _run("ana", [f"x{i}" for i in range(6)], max_parallel=2)
    assert fake.max_active == 2


# ── capacity detection ────────────────────────────────────────────────────

async def test_llamacpp_slots_are_read_from_slots_endpoint(env, monkeypatch):
    seen = []

    async def fake_get(url, timeout):
        seen.append(url)
        if url.endswith("/slots"):
            return [{"id": i, "n_ctx": 58880} for i in range(4)]
        raise RuntimeError("no props")

    monkeypatch.setattr(capacity, "_get_json", fake_get)
    info = await capacity.effective_parallel(LLAMA_URL)
    assert info["parallel"] == 4 and info["backend"] == "llamacpp" and info["local"] is True
    assert seen[0] == "http://127.0.0.1:8080/slots"
    # The slot count is cached, but a run start re-reads /slots: which slots
    # other conversations are busy with changes from one run to the next.
    n = len(seen)
    await capacity.llamacpp_slots(LLAMA_URL)
    assert len(seen) == n
    await capacity.effective_parallel(LLAMA_URL)
    assert len(seen) > n


async def test_llamacpp_props_total_slots_when_slots_endpoint_is_off(env, monkeypatch):
    async def fake_get(url, timeout):
        if url.endswith("/props"):
            return {"total_slots": 2, "default_generation_settings": {"n_ctx": 100000}}
        raise RuntimeError("501 --no-slots")

    monkeypatch.setattr(capacity, "_get_json", fake_get)
    info = await capacity.effective_parallel(LLAMA_URL, refresh=True)
    assert info["parallel"] == 2


async def test_llamacpp_slots_are_capped_and_unknown_local_is_one(env, monkeypatch):
    env["settings"]["swarm_max_parallel"] = 3

    async def many(url, timeout):
        if url.endswith("/slots"):
            return [{"id": i} for i in range(8)]
        raise RuntimeError

    monkeypatch.setattr(capacity, "_get_json", many)
    assert (await capacity.effective_parallel(LLAMA_URL, refresh=True))["parallel"] == 3

    async def nothing(url, timeout):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(capacity, "_get_json", nothing)
    info = await capacity.effective_parallel("http://127.0.0.1:9999/v1", refresh=True)
    assert info == {"parallel": 1, "source": "local_default", "backend": "local", "local": True}


async def test_ollama_uses_the_setting_and_remote_api_its_own(env, monkeypatch):
    async def boom(url, timeout):  # Ollama is never probed for slots
        raise AssertionError("probed")

    monkeypatch.setattr(capacity, "_get_json", boom)
    env["settings"]["swarm_ollama_parallel"] = 4
    info = await capacity.effective_parallel("http://127.0.0.1:11434/v1/chat/completions")
    assert info["parallel"] == 4 and info["backend"] == "ollama"
    info = await capacity.effective_parallel(API_URL)
    assert info["parallel"] == 3 and info["backend"] == "api" and info["local"] is False


async def test_a_local_run_goes_through_the_lane_with_the_detected_slots(env, monkeypatch):
    env["route"]["url"] = LLAMA_URL

    async def slots(url, timeout):
        if url.endswith("/slots"):
            return [{"id": i} for i in range(4)]
        raise RuntimeError

    monkeypatch.setattr(capacity, "_get_json", slots)
    lanes = []

    async def fake(url, model, messages, *, headers, timeout, response_schema=None, max_tokens=0):
        lanes.append(lane.lane_for(url))
        await asyncio.sleep(0.02)
        return "ok"

    monkeypatch.setattr(runner, "_llm_call", fake)
    _, manifest = await _run("ana", [str(i) for i in range(8)])
    assert manifest["parallel"]["effective"] == 4
    assert all(ln is not None and ln.parallel == 4 for ln in lanes)
    assert lane.lane_for(LLAMA_URL) is None  # nothing leaks out of the items


# ── the lane inside llm_core's local gate ─────────────────────────────────

async def test_lane_calls_overlap_while_ordinary_local_calls_serialise(monkeypatch):
    from src import llm_core
    monkeypatch.setattr("src.model_context.is_local_endpoint", lambda url: True)
    state = {"active": 0, "max": 0}

    async def call():
        async with llm_core._local_model_slot(LLAMA_URL, "m", "background"):
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
            await asyncio.sleep(0.03)
            state["active"] -= 1

    async def in_lane():
        with lane.enter(LLAMA_URL, "swarm-abc123", 3):
            await call()

    await asyncio.gather(*(in_lane() for _ in range(3)))
    assert state["max"] == 3
    state["max"] = 0
    monkeypatch.setattr("src.interactive_gate.has_foreground_activity", lambda: False)
    await asyncio.gather(*(call() for _ in range(3)))
    assert state["max"] == 1


async def test_lane_call_waits_while_a_foreground_call_wants_the_model(monkeypatch):
    from src import llm_core
    monkeypatch.setattr("src.model_context.is_local_endpoint", lambda url: True)
    monkeypatch.setattr(llm_core, "_LOCAL_MODEL_WAITING_FOREGROUND", 1)
    entered = asyncio.Event()

    async def in_lane():
        with lane.enter(LLAMA_URL, "swarm-abc123", 2):
            async with llm_core._local_model_slot(LLAMA_URL, "m", "background"):
                entered.set()

    task = asyncio.ensure_future(in_lane())
    await asyncio.sleep(0.4)
    assert not entered.is_set()
    monkeypatch.setattr(llm_core, "_LOCAL_MODEL_WAITING_FOREGROUND", 0)
    await asyncio.wait_for(task, 2)
    assert entered.is_set()


def test_lane_matches_host_and_port_only():
    with lane.enter("http://localhost:8080/v1/chat/completions", "swarm-abc123", 2):
        assert lane.lane_for("http://127.0.0.1:8080/slots") is not None
        assert lane.lane_for("http://127.0.0.1:8081/v1") is None
    assert lane.lane_for("http://127.0.0.1:8080/v1") is None


# ── retry and failure isolation ───────────────────────────────────────────

async def test_one_retry_then_recorded_failure_never_aborts_the_run(env, monkeypatch):
    attempts = {}

    async def flaky(url, model, messages, *, headers, timeout, response_schema=None, max_tokens=0):
        item = messages[-1]["content"].split()[-1]
        attempts[item] = attempts.get(item, 0) + 1
        if item == "b" and attempts[item] == 1:
            raise RuntimeError("503 busy")
        if item == "c":
            raise RuntimeError("always broken")
        return f"ok {item}"

    monkeypatch.setattr(runner, "_llm_call", flaky)
    run_id, manifest = await _run("ana", ["a", "b", "c", "d"], instruction="Handle {item}")
    rows = store.load_results(run_id)
    assert manifest["status"] == "partial"
    assert manifest["counts"] == {"total": 4, "ok": 3, "failed": 1, "pending": 0}
    assert rows[1]["status"] == "ok" and rows[1]["attempts"] == 2
    assert rows[2]["status"] == "failed" and rows[2]["attempts"] == 2 and "always broken" in rows[2]["error"]
    assert attempts["c"] == 2 and attempts["a"] == 1


async def test_per_item_timeout_is_a_failure_of_that_item_only(env, monkeypatch):
    async def slow(url, model, messages, *, headers, timeout, response_schema=None, max_tokens=0):
        if "slow" in messages[-1]["content"]:
            await asyncio.sleep(5)
        return "fine"

    monkeypatch.setattr(runner, "_llm_call", slow)
    run_id = service.start("ana", "s", "Do {item}", ["fast", "slow"], per_item_timeout=1,
                           run_in_background=False)
    assert store.load_manifest(run_id)["per_item_timeout"] == 5.0   # floor
    manifest = store.load_manifest(run_id)
    manifest["per_item_timeout"] = 0.1
    store.save_manifest(manifest)
    await runner.run(run_id, "ana")
    rows = store.load_results(run_id)
    assert rows[0]["status"] == "ok"
    assert rows[1]["status"] == "failed" and "timed out" in rows[1]["error"]


async def test_all_failed_is_failed_and_empty_reply_counts_as_failure(env, monkeypatch):
    async def empty(*a, **k):
        return "   "

    monkeypatch.setattr(runner, "_llm_call", empty)
    _, manifest = await _run("ana", ["a"])
    assert manifest["status"] == "failed"


# ── structured rows ───────────────────────────────────────────────────────

async def test_output_fields_become_columns_and_bad_json_is_retried(env, monkeypatch):
    seen_schema = []
    calls = {"n": 0}

    async def fake(url, model, messages, *, headers, timeout, response_schema=None, max_tokens=0):
        seen_schema.append(response_schema)
        calls["n"] += 1
        name = json.loads(messages[-1]["content"].split("Company: ")[1].split("\n")[0])["name"]
        if name == "Beta" and calls["n"] <= 2:
            return "Sorry, I think it is software."
        return '```json\n{"sector": "tech", "size": "' + name + '-size"}\n```'

    monkeypatch.setattr(runner, "_llm_call", fake)
    run_id, manifest = await _run(
        "ana", [{"name": "Acme"}, {"name": "Beta"}],
        instruction="Company: {item}\nWhat is {item.name}?", output_fields=["sector", "size"])
    assert seen_schema[0]["required"] == ["sector", "size"]
    rows = store.load_results(run_id)
    assert rows[0]["fields"] == {"sector": "tech", "size": "Acme-size"}
    assert rows[1]["status"] == "ok" and rows[1]["attempts"] == 2
    page = service.results(run_id, "ana")
    assert page["columns"] == ["index", "item", "status", "sector", "size", "error", "attempts"]
    assert page["rows"][1]["size"] == "Beta-size"


def test_render_prompt_placeholders_and_limits():
    assert render.render_prompt("Look up {item.name} ({item})", {"name": "Acme"}) == \
        'Look up Acme ({"name": "Acme"})'
    assert render.render_prompt("Summarise this", "doc.txt").endswith("Item:\ndoc.txt")
    assert "{item.missing}" in render.render_prompt("{item.missing}", {"a": 1})
    with pytest.raises(render.SwarmSpecError):
        render.normalize_items(["x" * (render.MAX_ITEM_CHARS + 1)], 10)
    with pytest.raises(render.SwarmSpecError):
        render.normalize_items(["a", "b", "c"], 2)
    assert render.normalize_items("a\n\n b \n", 5) == ["a", "b"]
    with pytest.raises(render.SwarmSpecError):
        render.normalize_fields(["ok", "status"])
    assert render.parse_fields('prose {"a": 1} more {"a": 2, "b": 3}', [{"name": "a"}, {"name": "b"}]) == \
        {"a": 2, "b": 3}


async def test_start_refuses_more_items_than_the_setting(env):
    env["settings"]["swarm_max_items"] = 3
    with pytest.raises(render.SwarmSpecError):
        service.start("ana", "", "x {item}", ["1", "2", "3", "4"], run_in_background=False)
    with pytest.raises(render.SwarmSpecError):
        service.start("ana", "", "x {item}", ["1", "2", "3"], max_items=2, run_in_background=False)


# ── cancel and resume ─────────────────────────────────────────────────────

async def test_cancel_keeps_finished_items_and_resume_does_only_the_rest(env, monkeypatch):
    release = asyncio.Event()
    calls = []

    async def gated(url, model, messages, *, headers, timeout, response_schema=None, max_tokens=0):
        item = messages[-1]["content"].split()[-1]
        calls.append(item)
        if int(item) >= 2:
            await release.wait()
        return f"done {item}"

    monkeypatch.setattr(runner, "_llm_call", gated)
    run_id = service.start("ana", "s", "Do {item}", [str(i) for i in range(6)])
    for _ in range(100):
        if store.load_manifest(run_id)["counts"]["ok"] >= 2:
            break
        await asyncio.sleep(0.02)
    out = service.cancel(run_id, "ana")
    assert out["cancelled"] is True
    await asyncio.wait_for(service._TASKS[run_id], 2)
    summary = service.status(run_id, "ana")
    assert summary["status"] == "cancelled"
    assert summary["counts"]["ok"] == 2 and summary["counts"]["pending"] == 4
    assert summary["resumable"] is True

    done_before = set(calls)
    calls.clear()
    release.set()
    service.resume(run_id, "ana")
    await asyncio.wait_for(service._TASKS[run_id], 2)
    summary = service.status(run_id, "ana")
    assert summary["status"] == "done" and summary["counts"]["ok"] == 6
    assert set(calls) == {"2", "3", "4", "5"} and not ({"0", "1"} & set(calls))
    assert {"0", "1"} <= done_before


async def test_resume_after_a_crash_reads_the_checkpoint_only(env, monkeypatch):
    fake = FakeLLM(delay=0)
    monkeypatch.setattr(runner, "_llm_call", fake)
    run_id = service.start("ana", "s", "Do {item}", ["a", "b", "c"], run_in_background=False)
    # a previous process finished item 0 and died mid-write on item 1
    store.append_result(run_id, {"index": 0, "status": "ok", "output": "prev", "attempts": 1})
    with open(store.run_dir(run_id) + "/results.jsonl", "a", encoding="utf-8") as fh:
        fh.write('{"index": 1, "status": "o')
    manifest = store.load_manifest(run_id)
    manifest["status"] = "running"
    store.save_manifest(manifest)
    assert service.status(run_id, "ana")["status"] == "interrupted"
    manifest = await runner.run(run_id, "ana")
    assert manifest["status"] == "done"
    assert len(fake.calls) == 2
    assert store.load_results(run_id)[0]["output"] == "prev"


# ── reduce ────────────────────────────────────────────────────────────────

async def test_reduce_runs_once_over_the_collected_results(env, monkeypatch):
    prompts = []

    async def fake(url, model, messages, *, headers, timeout, response_schema=None, max_tokens=0):
        prompts.append(messages[-1]["content"])
        if messages[-1]["content"].startswith("Rank"):
            return "Top: b"
        return "score " + messages[-1]["content"].split()[-1]

    monkeypatch.setattr(runner, "_llm_call", fake)
    run_id, manifest = await _run("ana", ["a", "b"], instruction="Score {item}", reduce="Rank them")
    reduce_prompts = [p for p in prompts if p.startswith("Rank")]
    assert len(reduce_prompts) == 1
    assert "[1] a => score a" in reduce_prompts[0] and "[2] b => score b" in reduce_prompts[0]
    assert manifest["reduce_result"] == {"status": "ok", "output": "Top: b", "chunks": 1}
    assert f"{run_id}-reduce.md" in manifest["files"]


async def test_reduce_is_chunked_when_the_results_are_large(env, monkeypatch):
    monkeypatch.setattr(runner, "REDUCE_CHUNK_CHARS", 60)
    prompts = []

    async def fake(url, model, messages, *, headers, timeout, response_schema=None, max_tokens=0):
        text = messages[-1]["content"]
        prompts.append(text)
        if text.startswith("Sum"):
            return "partial" if "part " in text and "Combine" not in text else "final"
        return "x" * 30

    monkeypatch.setattr(runner, "_llm_call", fake)
    _, manifest = await _run("ana", [str(i) for i in range(4)], reduce="Sum up")
    rr = manifest["reduce_result"]
    assert rr["status"] == "ok" and rr["output"] == "final" and rr["chunks"] >= 2
    assert sum(1 for p in prompts if p.startswith("Sum")) == rr["chunks"] + 1


async def test_reduce_skipped_when_every_item_failed(env, monkeypatch):
    async def broken(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(runner, "_llm_call", broken)
    _, manifest = await _run("ana", ["a"], reduce="Rank")
    assert "reduce_result" not in manifest


# ── exports and artifacts ─────────────────────────────────────────────────

async def test_exports_are_written_and_recorded_as_artifacts(env, monkeypatch):
    async def fake(url, model, messages, *, headers, timeout, response_schema=None, max_tokens=0):
        return '{"verdict": "yes | really"}'

    monkeypatch.setattr(runner, "_llm_call", fake)
    run_id = service.start("ana", "sess-9", "Judge {item}", ["a", "b"], output_fields=["verdict"],
                           run_in_background=False)
    manifest = await runner.run(run_id, "ana")
    names = set(manifest["files"])
    assert names == {f"{run_id}.md", f"{run_id}.csv", f"{run_id}.jsonl"}
    md = open(service.export_path(run_id, "ana", f"{run_id}.md"), encoding="utf-8").read()
    assert "| index | item | status | verdict | error | attempts |" in md and "yes \\| really" in md
    rows = list(csv.DictReader(io.StringIO(open(service.export_path(run_id, "ana", f"{run_id}.csv"),
                                                encoding="utf-8").read())))
    assert [r["verdict"] for r in rows] == ["yes | really", "yes | really"]
    jl = [json.loads(line) for line in open(service.export_path(run_id, "ana", f"{run_id}.jsonl"),
                                            encoding="utf-8")]
    assert jl[0]["index"] == 1 and jl[0]["item"] == "a"
    persisted = env["persisted"]
    assert len(manifest["artifacts"]) == 3 and persisted and persisted[0][1] == "sess-9"
    assert {a["name"] for a in manifest["artifacts"]} == names


# ── agent mode ────────────────────────────────────────────────────────────

async def test_agent_mode_runs_one_worker_per_item(env, monkeypatch):
    seen = []

    async def fake_agent(manifest, index, prompt, *, url, model, headers, owner):
        seen.append((index, prompt, manifest["agent"]["max_rounds"]))
        return {"text": f"worker {index}", "tokens": 10, "tool_calls": 1, "rounds": 2}

    monkeypatch.setattr(runner, "_agent_call", fake_agent)
    run_id, manifest = await _run("ana", ["u1", "u2"], mode="agent", tools=["web_fetch"], max_rounds=40,
                                  workspace="/tmp/ws")
    assert manifest["status"] == "done"
    assert sorted(i for i, _, _ in seen) == [0, 1]
    assert all(r == runner.MAX_AGENT_ROUNDS for _, _, r in seen)
    perms = manifest["agent"]["permissions"]
    assert perms["allowed_tools"] == ["web_fetch"]
    assert {"swarm_map", "delegate_agents"} <= set(perms["denied_tools"])
    assert store.load_results(run_id)[0]["tokens"] == 10


async def test_agent_mode_is_refused_for_a_worker_at_the_depth_ceiling(env):
    from src.subagent_permissions import ChildPermissions
    worker = ChildPermissions(depth=1, may_delegate=False)
    with pytest.raises(render.SwarmSpecError):
        service.start("ana", "", "x {item}", ["a"], mode="agent", ctx={"permissions": worker},
                      run_in_background=False)


async def test_agent_mode_keeps_a_parent_deny(env):
    from src.subagent_permissions import ChildPermissions
    parent = ChildPermissions(depth=0, may_delegate=True, denied_tools=frozenset({"bash"}))
    run_id = service.start("ana", "", "x {item}", ["a"], mode="agent", ctx={"permissions": parent},
                           run_in_background=False)
    perms = store.load_manifest(run_id)["agent"]["permissions"]
    assert "bash" in perms["denied_tools"]
    rebuilt = runner._perms_from_dict(perms)
    assert rebuilt.tool_denied("bash") and rebuilt.tool_denied("swarm_map")


# ── tools ─────────────────────────────────────────────────────────────────

async def test_swarm_map_tool_wait_returns_the_table_inline(env, monkeypatch):
    from src.agent_tools.swarm_tools import SwarmMapTool, SwarmResultsTool, SwarmStatusTool
    monkeypatch.setattr(runner, "_llm_call", FakeLLM(delay=0.01))
    progress = []

    async def cb(evt):
        progress.append(evt)

    out = await SwarmMapTool().execute(json.dumps({
        "instruction": "Describe {item}", "items": ["a", "b", "c"], "wait": True, "wait_timeout": 5,
    }), {"owner": "ana", "session_id": "s1", "progress_cb": cb})
    assert isinstance(out, dict) and out["exit_code"] == 0 and out["finished"] is True
    assert "| index | item | status | output |" in out["output"]
    assert len(out["rows"]) == 3
    assert progress and all(p["event"] == "swarm_progress" and "swarm" in p for p in progress)
    status = await SwarmStatusTool().execute(json.dumps({"run_id": out["run_id"]}), {"owner": "ana"})
    assert status["status"] == "done"
    page = await SwarmResultsTool().execute(json.dumps({"run_id": out["run_id"], "offset": 2, "limit": 1}),
                                            {"owner": "ana"})
    assert [r["index"] for r in page["rows"]] == [3] and page["next_offset"] is None


async def test_swarm_tools_are_owner_scoped_and_validate(env, monkeypatch):
    from src.agent_tools.swarm_tools import (SwarmCancelTool, SwarmMapTool, SwarmResultsTool,
                                             SwarmStatusTool)
    monkeypatch.setattr(runner, "_llm_call", FakeLLM(delay=0.01))
    out = await SwarmMapTool().execute(json.dumps({"instruction": "x {item}", "items": ["a"]}),
                                       {"owner": "ana"})
    run_id = out["run_id"]
    await asyncio.wait_for(service._TASKS[run_id], 2)
    for tool in (SwarmStatusTool(), SwarmResultsTool(), SwarmCancelTool()):
        res = await tool.execute(json.dumps({"run_id": run_id}), {"owner": "bob"})
        assert res["exit_code"] == 1 and res["error_class"] == "swarm.not_found"
    bad = await SwarmMapTool().execute(json.dumps({"items": ["a"]}), {"owner": "ana"})
    assert bad["exit_code"] == 1 and bad["error_class"] == "swarm.invalid_request"
    bad = await SwarmMapTool().execute(json.dumps({"instruction": "x", "items": []}), {"owner": "ana"})
    assert bad["exit_code"] == 1
    listing = await SwarmStatusTool().execute("{}", {"owner": "bob"})
    assert listing["runs"] == []


def test_registry_and_capability_classes():
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
    from src.tool_capabilities import ToolEffect, capabilities_for_action, capabilities_for_tool
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    names = {"swarm_map", "swarm_status", "swarm_results", "swarm_cancel"}
    assert names <= set(TOOL_HANDLERS) and names <= TOOL_TAGS
    assert names <= {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
    llm = capabilities_for_action("swarm_map", json.dumps({"instruction": "x", "items": ["a"]}))
    assert llm.effects == frozenset({ToolEffect.NETWORK_EGRESS})
    agent = capabilities_for_action("swarm_map", {"mode": "agent", "instruction": "x", "items": ["a"]})
    assert ToolEffect.EXECUTE_CODE in agent.effects
    resumed = capabilities_for_action("swarm_map", {"resume_run_id": "swarm-abc123"})
    assert ToolEffect.EXECUTE_CODE in resumed.effects
    assert capabilities_for_action("swarm_map", "not json").effects == capabilities_for_tool("swarm_map").effects
    assert capabilities_for_tool("swarm_status").effects == frozenset({ToolEffect.READ_PRIVATE})


def test_worker_recursion_tools_are_never_given_to_a_swarm_worker():
    assert {"swarm_map", "delegate_agents", "fanout_run"} <= runner.RECURSION_TOOLS


# ── REST ──────────────────────────────────────────────────────────────────

@pytest.fixture
def client(env, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routes.swarm_routes as swarm_routes
    monkeypatch.setattr(swarm_routes, "effective_user", lambda request: request.headers.get("X-Test-User", ""))
    app = FastAPI()
    app.include_router(swarm_routes.setup_swarm_routes())
    # Override the very function the routes depend on. Importing it from
    # src.auth_helpers here could hand back another object when a test
    # elsewhere swapped that module in sys.modules; the override then missed
    # and every call answered 401 (seen in a full Windows run).
    app.dependency_overrides[swarm_routes.require_user] = lambda: ""
    return TestClient(app)


async def test_rest_routes_are_owner_scoped(client, monkeypatch):
    monkeypatch.setattr(runner, "_llm_call", FakeLLM(delay=0))
    run_a, _ = await _run("ana", ["a", "b"])
    run_b, _ = await _run("bob", ["c"])
    ana = {"X-Test-User": "ana"}
    bob = {"X-Test-User": "bob"}
    listed = client.get("/api/swarm", headers=ana).json()["runs"]
    assert [r["run_id"] for r in listed] == [run_a]
    assert client.get(f"/api/swarm/{run_a}", headers=ana).json()["counts"]["ok"] == 2
    assert client.get(f"/api/swarm/{run_a}", headers=bob).status_code == 404
    assert client.get(f"/api/swarm/{run_a}/results", headers=bob).status_code == 404
    assert client.post(f"/api/swarm/{run_a}/cancel", headers=bob).status_code == 404
    assert client.post(f"/api/swarm/{run_a}/resume", headers=bob).status_code == 404
    assert client.get(f"/api/swarm/{run_a}/files/{run_a}.csv", headers=bob).status_code == 404
    res = client.get(f"/api/swarm/{run_a}/files/{run_a}.csv", headers=ana)
    assert res.status_code == 200 and res.text.startswith("index,item,status,output")
    assert client.get(f"/api/swarm/{run_a}/files/..%2Fmanifest.json", headers=ana).status_code == 404
    assert client.get("/api/swarm/not-a-run", headers=ana).status_code == 404
    page = client.get(f"/api/swarm/{run_a}/results?limit=1", headers=ana).json()
    assert len(page["rows"]) == 1 and page["next_offset"] == 1
    assert client.post(f"/api/swarm/{run_a}/cancel", headers=ana).json()["cancelled"] is False
    assert [r["run_id"] for r in client.get("/api/swarm", headers=bob).json()["runs"]] == [run_b]


async def test_agent_call_reuses_the_delegate_worker_with_the_swarm_limits(env, monkeypatch):
    import src.agent_tools.subagent_tools as sat
    seen = {}

    async def fake_worker(run, **kw):
        seen.update(kw)
        seen["run"] = run
        run.text = "final answer" if "ok" in run.instruction else ""
        if "boom" in run.instruction:
            run.error = "model request failed"

    monkeypatch.setattr(sat, "_run_subagent", fake_worker)
    run_id, manifest = await _run("ana", ["ok-1", "boom", "silent"], mode="agent", workspace="/tmp/ws",
                                  instruction="Handle {item}")
    rows = store.load_results(run_id)
    assert rows[0]["status"] == "ok" and rows[0]["output"] == "final answer"
    assert rows[1]["status"] == "failed" and "model request failed" in rows[1]["error"]
    assert rows[2]["status"] == "failed" and "no answer" in rows[2]["error"]
    run = seen["run"]
    assert run.system_prompt == runner.AGENT_PREAMBLE
    assert runner.RECURSION_TOOLS <= run.lane_disabled_tools
    assert run.permissions is not None and run.permissions.tool_denied("delegate_agents")
    assert seen["max_rounds"] == runner.DEFAULT_AGENT_ROUNDS and seen["workspace"] == "/tmp/ws"
    assert seen["save_transcript"] is False and seen["parent_session_id"] == "sess-1"


async def test_llamacpp_parallel_leaves_the_busy_slots_to_others(monkeypatch):
    from src.swarm import capacity as cap
    cap._SLOT_CACHE.clear()
    cap._BUSY_SEEN.clear()

    async def fake_get(url, timeout):
        if url.endswith("/slots"):
            return [{"id": i, "is_processing": i < 2} for i in range(4)]
        return {"total_slots": 4}

    monkeypatch.setattr(cap, "_get_json", fake_get)
    monkeypatch.setattr(cap, "_is_local", lambda url: True)
    monkeypatch.setattr(cap, "_looks_ollama", lambda url: False)
    info = await cap.effective_parallel("http://127.0.0.1:8081/v1/chat/completions")
    assert info["slots"] == 4 and info["busy"] == 2 and info["parallel"] == 2


def test_background_tools_travel_with_their_followers():
    from src import agent_loop
    from src.agent_tools import TOOL_HANDLERS
    fam = next(f for f in agent_loop._BACKGROUND_TOOL_FAMILIES if "swarm_map" in f)
    assert {"swarm_status", "swarm_results", "swarm_cancel"} <= fam
    for family in agent_loop._BACKGROUND_TOOL_FAMILIES:
        assert family <= set(TOOL_HANDLERS), family - set(TOOL_HANDLERS)


async def test_a_timed_out_wait_names_the_run_and_forbids_a_second_start(env, monkeypatch):
    monkeypatch.setattr(runner, "_llm_call", FakeLLM(delay=0.4))
    from src.agent_tools.swarm_tools import SwarmMapTool
    out = await SwarmMapTool().execute(json.dumps({"instruction": "x {item}", "items": ["a", "b", "c"],
                                                   "wait": True, "wait_timeout": 1, "max_parallel": 1}),
                                       {"owner": "ana", "session_id": "s1"})
    assert out["finished"] is False
    assert f"run_id={out['run_id']}" in out["output"] and "Do NOT call swarm_map again" in out["output"]
