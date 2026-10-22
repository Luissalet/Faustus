"""Tracking a pasted plan must not replace its human-authored contract."""
import pytest

from src import plan_tracker as pt


CONTRACT = (
    "  Ejecuta este plan completo en la carpeta activa:\n\n"
    "## API pública estable\n"
    + "Conserva claves binarias, Unicode y operaciones atómicas.\n" * 110
    + "\n## Persistencia y aceptación\n"
    "Encoder y decoder admiten el mismo dominio; acepta65MiB o rechaza antes del ACK.\n"
    "## CLI estable\n"
    "Usa --key-b64 y --value-b64 con base64 estricto, no --hex.\n"
    "stdout contiene un objeto JSON ASCII exacto.\n"
    "## Entrega y comprobación\n"
    "Prueba recuperación, compactación y CLI. Última condición: 日本語, 🧪.  \n\n"
)


@pytest.fixture(autouse=True)
def tracker_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(pt, "PLAN_TRACKER_DIR", str(tmp_path / "trackers"))


def tracked(text, scope="own-plan-contract"):
    found = pt.find_plan_in_text(text)
    assert found is not None
    return pt.upsert_from_attachment(scope, *found)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_pasted_contract_is_literal_even_after_task_limit(newline):
    text = CONTRACT.replace("\n", newline)
    tracker = tracked(text)
    assert len(tracker["tasks"]) == 4
    assert len(text) > 6000
    assert pt.replace_pasted(text, tracker, max_task_chars=1) == text


def test_pasted_contract_and_following_attachment_remain_unchanged():
    text = CONTRACT + "\n=== File: notes.txt ===\n  Attachment body\r\n終わり\n"
    assert pt.replace_pasted(text, tracked(CONTRACT)) == text


def test_advancing_tracker_does_not_remove_other_contract_sections():
    scope = "own-plan-advance"
    tracker = tracked(CONTRACT, scope)
    tracker = pt.mark(scope, tracker["hash"], "t01", "done", "API operations confirmed by own test output")
    assert pt.current_task(tracker)["id"] == "t02"
    assert pt.replace_pasted(CONTRACT, tracker) == CONTRACT
    assert pt.progress(tracker)["done"] == 1


@pytest.mark.parametrize("language", ["es", "en"])
def test_brief_describes_tracker_without_denying_full_human_request(language):
    brief = pt.brief(tracked(CONTRACT), language=language)
    assert all(name in brief for name in ("plan_status", "plan_task", "plan_done"))
    assert "full plan is NOT" not in brief
    assert "plan completo NO" not in brief
    assert "must not be requested" not in brief
    assert "no debe volver a pedirse" not in brief


def test_real_loop_provider_receives_complete_pasted_contract_and_tracker_brief(tmp_path, monkeypatch):
    import httpx
    import requests
    from src import model_context, run_model_pin
    from tests.test_agent_harness_functional import _patch_common, _scripted_stream, _run

    attempted_http = []
    def no_http(*args, **kwargs):
        attempted_http.append(True)
        raise AssertionError("CPU provider fixture must not perform HTTP")
    monkeypatch.setattr(httpx.AsyncClient, "send", no_http)
    monkeypatch.setattr(httpx.Client, "send", no_http)
    monkeypatch.setattr(requests.Session, "request", no_http)
    monkeypatch.setattr(model_context, "_query_context_length", lambda *args, **kwargs: (131072, True))
    # A CPU peer has no engine whose keep_alive needs restoring on completion.
    monkeypatch.setattr(run_model_pin, "restore_keep_alive", lambda *args, **kwargs: None)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _patch_common(monkeypatch, settings={
        "agent_project_tests": False, "agent_ui_smoke": False,
        "agent_plan_autocontinue": False,
    })
    calls = _scripted_stream(monkeypatch, [
        ('```plan_status\n{}\n```', "tool_calls"),
        ("El estado del plan está consultado; las tareas siguen pendientes.", "stop"),
    ])
    events = _run(str(workspace), user=CONTRACT, harness_options={"project_id": "own-contract"})
    assert any(e.get("type") == "plan_tracker" and e.get("total") == 4 for e in events)
    for messages in calls["messages"]:
        humans = [m for m in messages if m.get("role") == "user" and not m.get("_harness_note")]
        assert any(m.get("content") == CONTRACT for m in humans)
    # The brief travels as a user-role harness note (some model families ignore
    # mid-prompt system messages) flagged so context trimming keeps it.
    assert any(m.get("role") == "user" and m.get("_harness_note") and (m.get("metadata") or {}).get("plan_note")
               and "plan_status" in m.get("content", "") for m in calls["messages"][0])
    assert attempted_http == []
