"""QA-10 · Reinicio en research (docs/spec/v2/acceptance_scenarios.json).

Estimulo: matar backend tras guardar tres rondas y una seccion.
Resultado exigido (literal): "Marca interrupted; Reanudar reutiliza
evidencia confirmada; Reintentar empieza otra ejecucion explicita."

Requisitos: TASK-02, RES-05.

Estado: verde (Lote 64). `ResearchHandler.resume_interrupted` (nuevo,
src/research_handler.py) lee el checkpoint que `_write_checkpoint` ya dejaba
en el marker JSON de una investigacion interrumpida — rondas confirmadas,
fuentes, consultas ya hechas, partes de informe ya escritas — y arranca una
sesion NUEVA con `max_rounds` reducido por las rondas ya pagadas, sin repetir
ninguna consulta/lectura ya confirmada. La ruta HTTP `POST
/api/research/{id}/resume` (introducida en una ola anterior) ahora es una
cascara fina sobre este metodo, en vez de contener ella misma la logica de
reanudacion — que es exactamente lo que faltaba para que este test dejara de
ser xfail: la aserción original ("existe un metodo resume/reanudar en la
clase") es ahora literalmente cierta, y el resto de este fichero prueba que
el metodo realmente reutiliza lo confirmado en vez de repetirlo.
"""
import json

import pytest

from src import research_handler as rh_module
from src.research_handler import ResearchHandler

pytestmark = pytest.mark.qa_state("green")


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "deep_research"
    d.mkdir()
    monkeypatch.setattr(rh_module, "RESEARCH_DATA_DIR", d)
    return d


def _handler():
    h = ResearchHandler.__new__(ResearchHandler)
    h._active_tasks = {}
    return h


def _checkpoint(**over):
    cp = {
        "rounds_done": 3,
        "findings": [
            {"url": "https://a.example/1", "title": "A1", "summary": "s1", "evidence": "e1"},
            {"url": "https://a.example/2", "title": "A2", "summary": "s2", "evidence": "e2"},
        ],
        "queries_done": ["query one", "query two", "query three"],
        "report_parts": ["## Section 1\nAlready written."],
        "evolving_report": "## Section 1\nAlready written.",
        "sources": {},
        "updated_at": 1000.0,
    }
    cp.update(over)
    return cp


def _interrupted_marker(path, session_id, **over):
    data = {
        "marker": True, "query": "the whiplash rehab question",
        "status": "interrupted", "error": ResearchHandler.INTERRUPTED_MESSAGE,
        "model": "m", "category": None, "started_at": 500.0, "owner": "luis",
        "max_rounds": 20, "checkpoint": _checkpoint(),
    }
    data.update(over)
    (path / f"{session_id}.json").write_text(json.dumps(data), encoding="utf-8")
    return data


# ── the literal assertion this test used to xfail on ───────────────────────

def test_a_resume_method_reuses_confirmed_rounds_and_sections():
    assert hasattr(ResearchHandler, "resume_interrupted")


# ── it survives an actual restart, not just the method's existence ─────────

def test_kill_and_restart_after_three_rounds_then_resume_without_repeating(data_dir):
    h = _handler()
    h._write_marker("rp-old", {"query": "the whiplash rehab question", "status": "running",
                               "progress": {"model": "m"}, "error": "", "started_at": 500.0,
                               "category": None, "owner": "luis", "max_rounds": 20})
    h._write_checkpoint("rp-old", _checkpoint())

    # "Matar backend" — a fresh process re-reads disk state on construction.
    import asyncio

    async def _never(self, *a, **k):
        await asyncio.sleep(3600)
    import unittest.mock as mock
    with mock.patch.object(ResearchHandler, "_initialize_legacy_engine", lambda self: None):
        fresh = ResearchHandler()

    before = json.loads((data_dir / "rp-old.json").read_text(encoding="utf-8"))
    assert before["status"] == "interrupted"
    assert before["checkpoint"]["rounds_done"] == 3

    captured = {}

    def fake_start_research(self, **kwargs):
        captured.update(kwargs)
        return {"session_id": kwargs["session_id"], "status": "running"}
    with mock.patch.object(ResearchHandler, "start_research", fake_start_research):
        result = fresh.resume_interrupted(
            "rp-old", "luis",
            resolve_endpoint=lambda model: ("http://127.0.0.1:11434/v1", "m", None))

    # A NEW session, never a second writer for the interrupted one.
    assert result["session_id"] != "rp-old"
    assert result["resumed_from"] == "rp-old"
    assert result["resumed_kept"]["rounds_done"] == 3

    # No repeated inference: max_rounds is cut down by the rounds already
    # confirmed, and every already-asked query is excluded from the next run.
    assert captured["max_rounds"] == 20 - 3
    assert captured["prior_queries"] == {"query one", "query two", "query three"}
    assert len(captured["prior_findings"]) == 2
    assert captured["prior_urls"] == {"https://a.example/1", "https://a.example/2"}
    assert captured["prior_report_parts"] == ["## Section 1\nAlready written."]
    assert captured["prior_report"] == "## Section 1\nAlready written."
    assert captured["resumed_from"] == "rp-old"

    # The interrupted card's checkpoint has been consumed — resuming is not
    # offered a second time for the same marker.
    assert fresh.list_interrupted("luis") == []


def test_resume_is_distinct_from_retry(data_dir):
    """"Reanudar" (resume_interrupted, seeded from the checkpoint) and
    "Reintentar" (a plain start_research call with empty history) are two
    different code paths — resuming one must never look like the other."""
    h = _handler()
    _interrupted_marker(data_dir, "rp-old")

    captured = {}

    def fake_start_research(self, **kwargs):
        captured.update(kwargs)
        return {"session_id": kwargs["session_id"], "status": "running"}
    import unittest.mock as mock
    with mock.patch.object(ResearchHandler, "start_research", fake_start_research):
        h.resume_interrupted("rp-old", "luis", resolve_endpoint=lambda model: ("http://x", "m", None))
    resume_kwargs = dict(captured)

    captured.clear()
    with mock.patch.object(ResearchHandler, "start_research", fake_start_research):
        h.start_research(session_id="rp-fresh-retry", query="the whiplash rehab question",
                         llm_endpoint="http://x", llm_model="m", owner="luis")
    retry_kwargs = dict(captured)

    assert resume_kwargs.get("prior_findings")
    assert not retry_kwargs.get("prior_findings")
    assert resume_kwargs.get("resumed_from") == "rp-old"
    assert not retry_kwargs.get("resumed_from")


def test_resume_without_a_checkpoint_refuses_explicitly(data_dir):
    h = _handler()
    (data_dir / "rp-empty.json").write_text(
        json.dumps({"marker": True, "query": "q", "status": "interrupted",
                    "owner": "luis"}), encoding="utf-8")

    with pytest.raises(ResearchHandler.NoCheckpointError):
        h.resume_interrupted("rp-empty", "luis", resolve_endpoint=lambda model: ("http://x", "m", None))


def test_resume_never_leaks_another_owners_session(data_dir):
    h = _handler()
    _interrupted_marker(data_dir, "rp-theirs", owner="ana")

    with pytest.raises(FileNotFoundError):
        h.resume_interrupted("rp-theirs", "luis", resolve_endpoint=lambda model: ("http://x", "m", None))


def test_resume_of_a_nonexistent_session_raises_not_found(data_dir):
    h = _handler()
    with pytest.raises(FileNotFoundError):
        h.resume_interrupted("rp-does-not-exist", "luis", resolve_endpoint=lambda model: ("http://x", "m", None))
