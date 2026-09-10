"""QA-42 · Render ya aceptado (docs/spec/v2/acceptance_scenarios.json).

Estimulo: perder conexion despues de submit multimedia.
Resultado exigido (literal): "Consultar job existente y recoger resultado,
no duplicar generacion."

Requisitos: MEDIA-04.

Estado: verde. `src/media_runs.py::start/poll/recent` (MEDIA-04, existente,
ver tests/test_media_runs.py y tests/test_media_runs_outbox.py) escriben la
fila ANTES de hablar con el motor y guardan su `engine_job_id`; un cliente
que perdio la conexion tras el submit encuentra el run existente via
`recent(owner=...)` y recoge el resultado con `poll()`, sin volver a
llamar a `start()`. Este test corre contra el motor falso real
(`tests.test_media_runs.world`/FakeComfy) y comprueba que, sea cual sea el
numero de reconexiones que consultan el job, el motor solo vio UN submit.
"""
import pytest

from src import media_runs
from tests.test_media_runs import ASK, world  # noqa: F401 - fixture reused whole

pytestmark = pytest.mark.qa_state("green")


def test_reconnecting_finds_the_existing_job_instead_of_resubmitting(world):
    out = media_runs.start("image.product", ASK, owner="luis")
    assert out["ok"] is True
    run_id = out["run_id"]
    assert len(world.submitted) == 1  # exactly one generation was ever queued

    # "Connection lost after submit": the client forgot run_id and must find
    # the job again through what it does know - the owner.
    found = [r for r in media_runs.recent(owner="luis") if r["id"] == run_id]
    assert found, "the client could not find its own already-accepted job"

    world.start_running(out["engine_job_id"])
    world.finish(out["engine_job_id"])

    # Polling the recovered run collects the result of the SAME job.
    result = media_runs.poll(run_id)
    assert result["run_id"] == run_id

    # Reconnecting/polling again and again never queues a second render.
    media_runs.poll(run_id)
    media_runs.poll(run_id)
    assert len(world.submitted) == 1
