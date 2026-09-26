"""A managed engine that answers a trivial prompt with one repeated symbol is
restarted (src/engine_swap.py restart_if_garbled)."""
import asyncio

import src.engine_swap as es


def test_garbage_is_one_symbol_repeated():
    assert es._is_garbage("////////////////////")
    assert es._is_garbage("0000000000")
    assert not es._is_garbage("Hola, ¿qué tal?")
    # 26-09: a corrupted 27B repeating one Cyrillic syllable.
    assert es._is_garbage("жнымжнымжнымжнымжнымжнымжнымжным")
    assert not es._is_garbage("Hello! Hope you are having a lovely day.")
    assert not es._is_garbage("¡Hola! ¿Qué tal estás hoy?")
    assert not es._is_garbage("hahahahahahaha")  # letters: a model's choice, not a broken engine
    assert not es._is_garbage("")


def test_a_garbled_engine_is_restarted(monkeypatch):
    engine = {"id": "e1", "host": "127.0.0.1", "port": 8081}
    monkeypatch.setattr(es, "restartable_engine_for_url", lambda url: engine)
    answers = iter([False, True])

    async def sane(url, model, **kw):
        return next(answers)

    monkeypatch.setattr(es, "generates_sanely", sane)
    calls = []

    import src.engines as engines

    async def stop(eid):
        calls.append(("stop", eid))
        return {"stopped": True}

    async def start_wait(eid, timeout):
        calls.append(("start", eid))
        return {"action": "started"}

    monkeypatch.setattr(engines, "stop_engine", stop)
    monkeypatch.setattr(es, "_do_start_and_wait", start_wait)
    monkeypatch.setattr(es, "_RESTART_PAUSE_S", 0.0)
    assert asyncio.run(es.restart_if_garbled("http://127.0.0.1:8081/v1", "m")) is True
    assert calls == [("stop", "e1"), ("start", "e1")]


def test_a_sane_or_unknown_engine_is_left_alone(monkeypatch):
    monkeypatch.setattr(es, "restartable_engine_for_url", lambda url: {"id": "e1"})
    for verdict in (True, None):
        async def sane(url, model, _v=verdict, **kw):
            return _v
        monkeypatch.setattr(es, "generates_sanely", sane)
        assert asyncio.run(es.restart_if_garbled("http://127.0.0.1:8081/v1", "m")) is False
