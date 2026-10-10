# -*- coding: utf-8 -*-
"""Auto reasoning mode with cross-turn signals (radar #458).

The previous turn can only RAISE this turn's mode: a correction after a
quick answer thinks, a correction after a full think goes deep, a failed
tool makes a short follow-up think, and nothing in the history ever lowers
a mode the message alone already earned.
"""

import pytest

from src.think_mode import decide, prior_signals, resolve_turn
from routes.chat_routes import _resolve_think_mode, _think_prior_signals

SETTINGS = {"think_mode_budget_think": 4096, "think_mode_budget_deep": 16384, "think_mode_budget_light": 1024}


def _hist(*pairs):
    return [{"role": r, "content": c} for r, c in pairs]


def test_prior_signals_reads_previous_user_turn_and_skips_current():
    msgs = _hist(("user", "hola"), ("assistant", "¡Hola!"), ("user", "no, eso está mal"))
    sig = prior_signals(msgs, "no, eso está mal")
    assert sig["has_prior"] is True
    assert sig["prev_mode"] == "fast"  # "hola" is small talk
    assert sig["tool_failures"] == 0 and sig["empty_answer"] is False


def test_prior_signals_counts_failed_tools_and_empty_answers():
    msgs = _hist(("user", "lista los ficheros de D:/x"), ("tool", "Error: no such file or directory"),
                 ("tool", '{"ok": false, "error": "denied"}'), ("assistant", ""), ("user", "¿y ahora?"))
    sig = prior_signals(msgs, "¿y ahora?")
    assert sig["tool_failures"] == 2
    assert sig["empty_answer"] is True


@pytest.mark.parametrize("bad", [None, "x", 3, [None, 1, "a"], [{"role": "user", "content": 5}]])
def test_prior_signals_never_raises(bad):
    out = prior_signals(bad, "")
    assert set(out) == {"prev_mode", "tool_failures", "empty_answer", "has_prior", "prev_mode_source", "deep_without_answer"}


def test_no_history_means_same_decision_as_before():
    for text in ("hola", "¿qué hora es?", "arregla el bug en app.py", "demuestra el teorema"):
        assert decide(text) == decide(text, prior={"has_prior": False})


@pytest.mark.parametrize("text", ["no, eso está mal", "te has equivocado", "that's wrong", "vuelve a intentarlo",
                                  "no es lo que te he pedido", "No: el total no cuadra"])
def test_correction_after_quick_answer_thinks(text):
    alone = decide(text)
    assert alone["mode"] == "fast" or alone.get("effort") == "low"
    d = decide(text, history_len=2, prior={"has_prior": True, "prev_mode": "light"})
    assert d["mode"] == "think" and d.get("effort") != "low"
    assert d["reasons"][0] == "correction"


def test_correction_after_full_think_goes_deep():
    d = decide("no, sigue mal", history_len=2, prior={"has_prior": True, "prev_mode": "think"})
    assert d["mode"] == "deep" and d["reasons"][0] == "correction_after_think"
    assert "light" not in d["reasons"]  # a deep pass is not a light one


def test_bare_no_is_a_decline_not_a_correction():
    d = decide("no", history_len=2, prior={"has_prior": True, "prev_mode": "think"})
    assert d["mode"] != "deep" and "correction" not in d["reasons"]


def test_failed_tool_makes_short_follow_up_think():
    d = decide("¿y ahora?", history_len=2, prior={"has_prior": True, "prev_mode": "think", "tool_failures": 1})
    assert d["mode"] == "think" and d.get("effort") != "low" and d["reasons"][0] == "prior_tool_failure"


def test_empty_previous_answer_gets_a_light_look():
    d = decide("gracias", history_len=2, prior={"has_prior": True, "prev_mode": "think", "empty_answer": True})
    assert d["mode"] == "think" and d.get("effort") == "low"


def test_history_never_lowers_a_mode():
    deep = decide("demuestra paso a paso que hay infinitos primos", prior={"has_prior": True, "prev_mode": "fast"})
    assert deep["mode"] == "deep"
    think = decide("arregla el bug en app.py", history_len=4, prior={"has_prior": True, "tool_failures": 3})
    assert think["mode"] == "think"


def test_asked_speed_still_wins_over_history():
    d = decide("rápido: ¿te has equivocado?", history_len=2, prior={"has_prior": True, "prev_mode": "think"})
    assert d["mode"] == "fast"


def test_resolve_turn_carries_prior_into_overrides():
    r = resolve_turn("auto", {}, "no, eso está mal", history_len=2, settings=SETTINGS,
                     prior={"has_prior": True, "prev_mode": "think"})
    assert r["overrides"]["think"] is True and r["overrides"]["reasoning_budget"] == 16384
    assert r["event"]["reasons"][0] == "correction_after_think"


def test_route_helper_wires_history(monkeypatch):
    from src import settings as settings_mod
    real = settings_mod.get_setting
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda k, d=None: {"think_mode_default": "auto", **SETTINGS}.get(k, real(k, d)))
    msgs = _hist(("user", "¿cuántos días faltan para el 2 de noviembre?"), ("assistant", "23 días."),
                 ("user", "no, eso está mal"))
    prior = _think_prior_signals(msgs, "no, eso está mal")
    assert prior["prev_mode"] == "think"
    ov, ev = _resolve_think_mode("auto", {}, "no, eso está mal", model="qwen3.8-27b-q8-llamacpp",
                                 endpoint_url="http://127.0.0.1:8081/v1", chat_mode="chat", workspace=None,
                                 history_len=2, prior=prior)
    assert ev["mode"] == "deep" and ov["think"] is True
    assert _think_prior_signals(object(), None)["has_prior"] is False


@pytest.mark.parametrize("result", [
    "File not found: notes/plan.md",
    "bash: rg: command not found",
    "  Error: no such file or directory",
    "File not found: C:\\Users\\qa\\native search with spaces\\plan.md",
    "'rg' is not recognized: command not found in C:\\Program Files\\tools",
])
def test_error_shaped_not_found_counts_as_a_failed_tool(result):
    msgs = _hist(("user", "abre el plan"), ("tool", result), ("assistant", "No pude."), ("user", "vale"))
    assert prior_signals(msgs, "vale")["tool_failures"] == 1


@pytest.mark.parametrize("result", [
    "Results for 'http status codes':\n1. 404 Not Found - the server cannot find the resource",
    "src/app.py:12: raise KeyError('user not found')\nsrc/db.py:40: # no such file is fine here",
    "C:\\Users\\qa\\native search with spaces\\a.py:12:    raise ValueError('user not found')",
    "D:/QA dir/b.py-12-    raise ValueError('user not found')",
    "notes/my plan.md:3: the file was not found last week",
])
def test_not_found_inside_content_is_not_a_failed_tool(result):
    msgs = _hist(("user", "busca"), ("tool", result), ("assistant", "Aqui esta."), ("user", "vale"))
    assert prior_signals(msgs, "vale")["tool_failures"] == 0
