"""PENDIENTES 23-09 noche — a research streak that never asks itself if it
has enough.

Live: without its own limit, a local model chained 32 `bash`+`curl` calls
reading a foreign repo's code. `agent_web_streak_nudge` (default 8, 0 = off)
covers it: N consecutive rounds that only read remote content (web_search/
web_fetch, or a bash/powershell call that is just curl/wget/
Invoke-WebRequest/git clone), with no file written and no plan/todo update,
get one gentle nudge to summarise, decide if it has enough, and either
answer or name the missing fact.
"""

import asyncio
import json

import src.agent_loop as al
from src.context_tool_gate import ContextAwareSecurityContext
from src.tool_capabilities import ToolGateDecision
from src.research_streak import (
    is_remote_read_only_round,
    looks_like_remote_read_shell_command,
    streak_nudge_message,
)


# ---------------------------------------------------------------------------
# Unit: the classification heuristics
# ---------------------------------------------------------------------------

def test_plain_curl_is_a_remote_read():
    assert looks_like_remote_read_shell_command(
        "curl -s https://raw.githubusercontent.com/foo/bar/main/x.py"
    )


def test_curl_piped_into_a_local_viewer_is_still_a_remote_read():
    assert looks_like_remote_read_shell_command("curl -s https://x/y.json | jq .")
    assert looks_like_remote_read_shell_command("curl -s https://x/y | head -50")


def test_wget_and_invoke_webrequest_and_git_clone_are_remote_reads():
    assert looks_like_remote_read_shell_command("wget -qO- https://x/y")
    assert looks_like_remote_read_shell_command("Invoke-WebRequest -Uri https://x/y")
    assert looks_like_remote_read_shell_command("iwr https://x/y")
    assert looks_like_remote_read_shell_command("git clone https://github.com/foo/bar")


def test_saving_the_response_to_disk_is_not_a_bare_read():
    assert not looks_like_remote_read_shell_command("curl -o out.html https://x/y")
    assert not looks_like_remote_read_shell_command("curl https://x/y -O")
    assert not looks_like_remote_read_shell_command("curl https://x/y > out.html")


def test_unrelated_or_mixed_commands_are_not_remote_reads():
    assert not looks_like_remote_read_shell_command("pytest -q")
    assert not looks_like_remote_read_shell_command("cat server.py")
    assert not looks_like_remote_read_shell_command("")
    assert not looks_like_remote_read_shell_command(
        "curl -s https://x/y && edit_this_file_somehow"
    )


def test_is_remote_read_only_round_requires_every_call_to_qualify():
    assert is_remote_read_only_round([
        ("web_search", "faustus opencode"),
        ("bash", "curl -s https://x/y"),
    ])
    assert not is_remote_read_only_round([
        ("bash", "curl -s https://x/y"),
        ("edit_file", "path=server.py"),
    ])
    assert not is_remote_read_only_round([])


def test_streak_nudge_message_names_the_streak_length():
    msg = streak_nudge_message(8)
    assert msg["role"] == "user"
    assert msg["_harness_note"] is True
    assert "8 rounds" in msg["content"]


# ---------------------------------------------------------------------------
# Integration: a fake stream through the real loop body
# ---------------------------------------------------------------------------

def _collect(gen):
    async def _run():
        return [c async for c in gen]
    return asyncio.run(_run())


def _events(chunks):
    out = []
    for c in chunks:
        if c.startswith("data: ") and not c.startswith("data: [DONE]"):
            try:
                out.append(json.loads(c[6:]))
            except Exception:
                pass
    return out


def _settings(overrides):
    def _get(key, default=None):
        if key in overrides:
            return overrides[key]
        return default
    return _get


def _patch_common(monkeypatch, settings_overrides=None):
    monkeypatch.setattr(al, "get_setting", _settings(settings_overrides or {}), raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)
    # This suite is about the round-level streak accounting, not the
    # external-context security gate: a real `curl` result arms that gate
    # (fetched content is untrusted), which would otherwise stop the fake
    # turn on an unrelated approval card after round 1. Always-allow it here.
    monkeypatch.setattr(
        ContextAwareSecurityContext, "decision_for",
        lambda self, tool_name, content=None: ToolGateDecision(True),
        raising=False,
    )

    async def _fake_exec(block, *a, **k):
        # A DISTINCT result per call: an identical (tool, result) pair
        # repeating round after round trips the unrelated loop-breaker/cycle
        # detector (src/agent_loop.py's LoopPolicy), which is exactly what a
        # real `curl` reading a DIFFERENT URL every round would never do.
        return (block.tool_type, {"output": f"ok: {block.content}", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)


#: Distinct words, not digits: `_probe_skeleton` (agent_loop.py's OWN,
#: separate loop-breaker) collapses every digit to "0" before comparing
#: consecutive bash commands, so `file_0.py`/`file_1.py` would look like the
#: SAME "diagnostic probe" and trip that unrelated guard at round 5 — a real
#: `curl` reading a DIFFERENT path each round (as the live 32-call bug did)
#: never looks like that to begin with.
_FAKE_REPO_FILES = [
    "alpha", "beta", "gamma", "delta", "epsilon",
    "zeta", "eta", "theta", "iota", "kappa", "lambda", "mu",
]


def _bash_curl_stream(monkeypatch, n_curl_rounds, final_text, snapshots=None):
    """`n_curl_rounds` rounds of a bare `curl` bash call, then a text answer."""
    calls = {"n": 0}

    async def _fake_stream(_candidates, messages, **kwargs):
        if snapshots is not None:
            snapshots.append(list(messages))
        i = calls["n"]
        calls["n"] += 1
        if i < n_curl_rounds:
            name = _FAKE_REPO_FILES[i % len(_FAKE_REPO_FILES)]
            cmd = f"curl -s https://example.invalid/repo/{name}.py"
            yield "data: " + json.dumps({
                "type": "tool_calls",
                "calls": [{"name": "bash", "arguments": json.dumps({"command": cmd})}],
            }) + "\n\n"
            yield "data: " + json.dumps({"type": "finish", "finish_reason": "tool_calls"}) + "\n\n"
        else:
            yield "data: " + json.dumps({"delta": final_text}) + "\n\n"
            yield "data: " + json.dumps({"type": "finish", "finish_reason": "stop"}) + "\n\n"
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    return calls


def test_default_threshold_nudges_once_after_eight_read_only_rounds(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    calls = _bash_curl_stream(monkeypatch, 10, "Ya he leído lo suficiente del repositorio ajeno.")
    gen = al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": "lee el código de ese repositorio externo por la URL X"}],
        max_rounds=15,
        relevant_tools={"bash"},
        workspace=str(tmp_path),
    )
    events = _events(_collect(gen))
    streak_events = [e for e in events if e.get("type") == "harness_check" and e.get("status") == "web_read_streak"]
    # Exactly one nudge, fired on the 8th read-only round, not repeated every
    # round afterwards even though the pattern keeps going for 2 more rounds.
    assert len(streak_events) == 1, events
    assert streak_events[0]["streak"] == 8
    assert streak_events[0]["round"] == 8


def test_a_lower_setting_fires_sooner(tmp_path, monkeypatch):
    _patch_common(monkeypatch, {"agent_web_streak_nudge": 3})
    _bash_curl_stream(monkeypatch, 5, "Resumen final tras revisar el repositorio.")
    gen = al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": "lee el código de ese repositorio externo"}],
        max_rounds=10,
        relevant_tools={"bash"},
        workspace=str(tmp_path),
    )
    events = _events(_collect(gen))
    streak_events = [e for e in events if e.get("type") == "harness_check" and e.get("status") == "web_read_streak"]
    assert len(streak_events) == 1, events
    assert streak_events[0]["streak"] == 3


def test_setting_zero_disables_the_nudge_entirely(tmp_path, monkeypatch):
    _patch_common(monkeypatch, {"agent_web_streak_nudge": 0})
    _bash_curl_stream(monkeypatch, 12, "Fin de la investigación.")
    gen = al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": "lee el código de ese repositorio externo"}],
        max_rounds=15,
        relevant_tools={"bash"},
        workspace=str(tmp_path),
    )
    events = _events(_collect(gen))
    streak_events = [e for e in events if e.get("type") == "harness_check" and e.get("status") == "web_read_streak"]
    assert streak_events == []


def test_a_local_edit_resets_the_streak(tmp_path, monkeypatch):
    """5 curl rounds, one edit_file round, then 5 more curl rounds: the
    streak must restart after the edit, so 8 never accumulates and no nudge
    fires (threshold left at the default 8)."""
    _patch_common(monkeypatch)
    calls = {"n": 0}

    async def _fake_stream(_candidates, messages, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        if i == 5:
            yield "data: " + json.dumps({
                "type": "tool_calls",
                "calls": [{"name": "edit_file", "arguments": json.dumps(
                    {"path": "notes.md", "old_str": "a", "new_str": "b"})}],
            }) + "\n\n"
            yield "data: " + json.dumps({"type": "finish", "finish_reason": "tool_calls"}) + "\n\n"
        elif i < 11:
            cmd = f"curl -s https://example.invalid/repo/file_{i}.py"
            yield "data: " + json.dumps({
                "type": "tool_calls",
                "calls": [{"name": "bash", "arguments": json.dumps({"command": cmd})}],
            }) + "\n\n"
            yield "data: " + json.dumps({"type": "finish", "finish_reason": "tool_calls"}) + "\n\n"
        else:
            yield "data: " + json.dumps({"delta": "Termino aquí."}) + "\n\n"
            yield "data: " + json.dumps({"type": "finish", "finish_reason": "stop"}) + "\n\n"
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    gen = al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": "lee el código de ese repositorio externo y anota lo que encuentres"}],
        max_rounds=15,
        relevant_tools={"bash", "edit_file"},
        workspace=str(tmp_path),
    )
    events = _events(_collect(gen))
    streak_events = [e for e in events if e.get("type") == "harness_check" and e.get("status") == "web_read_streak"]
    assert streak_events == [], events


def test_the_nudge_note_carries_the_users_language(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    snapshots = []
    _bash_curl_stream(monkeypatch, 9, "Ya tengo suficiente información.", snapshots=snapshots)
    gen = al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": "lee el código de ese repositorio externo por la URL"}],
        max_rounds=15,
        relevant_tools={"bash"},
        workspace=str(tmp_path),
    )
    events = _events(_collect(gen))
    streak_events = [e for e in events if e.get("type") == "harness_check" and e.get("status") == "web_read_streak"]
    assert len(streak_events) == 1, events
    notes = [
        m.get("content") for snap in snapshots for m in snap
        if isinstance(m, dict) and m.get("_harness_note") and isinstance(m.get("content"), str)
        and "rounds in a row" in m.get("content")
    ]
    assert notes and any("español" in n for n in notes), notes


def test_vision_questions_count_as_evidence_reads():
    """Live: 36 rounds of inspect_image on one page and no answer."""
    assert is_remote_read_only_round([("inspect_image", '{"path": "p.jpg", "question": "what is circled?"}')])
    assert is_remote_read_only_round([("inspect_image", "{}"), ("web_search", '{"query": "sonnet 60"}')])
    assert not is_remote_read_only_round([("inspect_image", "{}"), ("write_file", '{"path": "a.md"}')])


def test_reading_a_picture_is_an_evidence_read_but_a_source_file_is_not():
    assert is_remote_read_only_round([("read_file", '{"path": ".tmp_crops/op1.png"}'),
                                      ("inspect_image", '{"path": "op1.png"}')])
    assert not is_remote_read_only_round([("read_file", '{"path": "src/app.py"}')])


def test_a_python_crop_for_the_vision_model_is_part_of_the_look():
    """Live, exam run 13: PIL crops between vision questions broke the streak."""
    from src.research_streak import looks_like_image_prep_code
    crop = ('from PIL import Image\nim = Image.open("vistas/6a_pagina_1.jpg")\nw, h = im.size\n'
            'crop = im.crop((int(w*0.45), int(h*0.5), w, int(h*0.95)))\n'
            'crop = crop.resize((crop.width*2, crop.height*2), Image.LANCZOS)\n'
            'crop.save("citas_6a.png")\nprint(crop.size)\n')
    assert looks_like_image_prep_code(crop)
    import json as _json
    assert looks_like_image_prep_code(_json.dumps({"code": crop}))
    assert is_remote_read_only_round([("python", crop), ("inspect_image", '{"path": "citas_6a.png"}')])
    # a variable holding the target path still counts
    assert looks_like_image_prep_code('from PIL import Image\nout = "c1.png"\nImage.open("a.jpg").crop((0,0,9,9)).save(out)\n')


def test_python_that_does_more_than_crop_breaks_the_streak():
    from src.research_streak import looks_like_image_prep_code
    calc = "lat = 18.33\nkm = 3839 * 0.1852\nprint(lat, km)\n"
    writes = ('from PIL import Image\nImage.open("a.jpg").save("b.png")\n'
              'open("RESPUESTA.md", "w").write("answer")\n')
    no_save = 'from PIL import Image\nprint(Image.open("a.jpg").size)\n'
    net = 'import requests\nfrom PIL import Image\nImage.open("a.jpg").save("b.png")\nrequests.get("http://x")\n'
    for code in (calc, writes, no_save, net):
        assert not looks_like_image_prep_code(code), code
    assert not is_remote_read_only_round([("python", calc)])
