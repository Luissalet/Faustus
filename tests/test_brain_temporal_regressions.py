"""Regression tests for the temporal parser's false positives (review 1,
finding 8).

A wrong ``valid_until`` HIDES a memory from recall; a missed date costs
almost nothing. So the parser is held to a broad adversarial table of
ordinary technical sentences (quantities, versions, ports, sizes, prices,
times, ids, dates inside code or URLs, month names used as names) that must
produce NO window at all, next to the positives that must keep working —
including bare months rolled to the right year relative to ``now``.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.brain.temporal import parse_temporal  # noqa: E402

NOW = datetime(2026, 9, 23, 10, 0, 0, tzinfo=timezone.utc)
FEB = datetime(2026, 2, 10, 10, 0, 0, tzinfo=timezone.utc)
OCT = datetime(2026, 10, 15, 10, 0, 0, tzinfo=timezone.utc)


def _parse(text: str, now: datetime = NOW):
    return parse_temporal(text, now=now)


# ---------------------------------------------------------------------------
# Negatives — no window may come out of any of these
# ---------------------------------------------------------------------------

NO_WINDOW = [
    # quantities after hasta / until / from / desde
    "El plan gratuito permite hasta 1000 llamadas al dia",
    "El modelo acepta hasta 1024 tokens de salida",
    "Reintenta hasta 1500 veces con backoff",
    "Use batch sizes from 1024 to 4096",
    "Keep the context window until 2048 tokens",
    "El contexto admite hasta 2048",
    "La ventana de contexto llega a un maximo de hasta 2048",
    "Sube archivos de hasta 2000 MB",
    "Guarda hasta 1000 elementos en cache",
    "Procesa hasta 2000 al dia",
    "Limita el lote hasta 2000 o mas",
    "La API responde hasta 2000/min",
    "El limite es hasta 2000 por usuario",
    "Escala from 2000 to 4000 requests per second",
    "Escala de 2000 a 2048 tokens",
    "Acepta valores desde 1024 hasta 65535",
    "Timeout from 1000 ms",
    "Cuenta desde 1990 hasta 2100 pasos",
    "Allow retries until 2000 attempts",
    "The cache holds until 2000 entries",
    "Sube hasta 1950 metros de altura",
    "hasta 2048 px de ancho",
    "Entre 1024 y 2048 tokens",
    "Entre 2000 y 3000 usuarios",
    "Entre 2 y 4 GPUs",
    "from 2 to 4 GPUs",
    "Repite hasta 3 veces",
    "hasta 20245 filas",
    "hasta 2,048 tokens",
    "hasta 2.048 tokens",
    "hasta 2024x",
    "since 1024",
    # sizes / prices
    "Soporta resoluciones de hasta 1920x1080",
    "Descuentos de hasta 2000 euros",
    "Cuesta hasta 1999 €",
    "Los precios suben hasta 2000 dolares",
    # versions, ports, times, ids
    "Python 3.12 desde el lunes",
    "Instala Python 3.12 desde la web oficial",
    "Instalado en 2024.3",
    "El servidor escucha hasta el puerto 8080",
    "usa el puerto 8081",
    "Abierto hasta las 10",
    "La reunion dura hasta las 18:30",
    "El ticket #2024 sigue abierto desde ayer",
    "El color #2024ff viene desde el tema",
    "El contrato 03/2025-B sigue vigente",
    # dates inside code, URLs, file names, CLI flags, SQL
    "Ver https://example.com/archive/2024-03-10/post",
    "Consulta www.example.com/2023-01-05 para mas detalle",
    "El comando es `log --since 2024-01-01`",
    "Ejecuta report --until 2023-12-31 cada noche",
    "SELECT * FROM t WHERE day >= '2024-01-01'",
    "El archivo backup-2024-03-10.tar pesa mucho",
    "Lee /var/data/2024-03-10/dump.sql",
    "```\nsince 2019\n```",
    "Pon until=2024 en la config",
    # month names in other senses
    "Marzo vive en Madrid",
    "Trabajo con Ada desde Marzo",
    "Hablo con Abril hasta tarde",
    "The API may return 429 since May be busy",
    "Sin mayo, gracias",
    # both ends bare months: a past episode or a plan? Unknown -> nothing
    "entre marzo y junio",
    "from March to June",
    # inconsistent window
    "desde 2024 hasta 2020",
    # a future date with no "desde"/"since" is WHEN something happens, not
    # when the memory starts being true: as a start it would hide the memory
    "La demo es el 2026-10-01",
    "En 2027 Ada se muda a Lisboa",
    "Deadline: 2026-12-01",
]


@pytest.mark.parametrize("text", NO_WINDOW)
def test_no_window_for_non_dates(text):
    result = _parse(text)
    assert result["valid_from"] is None, result
    assert result["valid_until"] is None, result


NO_STATE = [
    "Ejecuta los tests antes de hacer push",
    "Hazlo cuanto antes",
    "Responde lo antes posible",
    "Revisa los logs antes que nada",
    "The regex is used to parse dates",
    "This flag was used to skip the cache",
    "It's used to cache results",
    "I am used to it",
]


@pytest.mark.parametrize("text", NO_STATE)
def test_no_past_state_for_non_state_uses(text):
    assert _parse(text)["state"] is None


def test_no_window_tables_are_broad():
    assert len(NO_WINDOW) >= 60
    assert len(NO_STATE) >= 8


# ---------------------------------------------------------------------------
# Positives — these must still work (now = 2026-09-23)
# ---------------------------------------------------------------------------

POSITIVES = [
    ("Ada trabaja en Cordera Labs desde marzo de 2025", "2025-03-01T00:00:00Z", None),
    ("Ada has worked at Bluehaven since 2019", "2019-01-01T00:00:00Z", None),
    ("desde 2019 trabaja en Bluehaven", "2019-01-01T00:00:00Z", None),
    ("Since 2019 Ada lives in Madrid", "2019-01-01T00:00:00Z", None),
    ("The license is valid until 2027", None, "2027-12-31T23:59:59Z"),
    ("Bruno trabajo en Bluehaven hasta 2020", None, "2020-12-31T23:59:59Z"),
    ("Bruno trabajo en Bluehaven hasta 2020.", None, "2020-12-31T23:59:59Z"),
    ("Ada lived there until 2024, then she moved", None, "2024-12-31T23:59:59Z"),
    ("Ada trabajo en Cordera Labs entre 2019 y 2021", "2019-01-01T00:00:00Z", "2021-12-31T23:59:59Z"),
    ("Ada worked there between 2019 and 2021", "2019-01-01T00:00:00Z", "2021-12-31T23:59:59Z"),
    ("Ada worked there from 2019 to 2021", "2019-01-01T00:00:00Z", "2021-12-31T23:59:59Z"),
    ("Ada trabajo alli de 2019 a 2021", "2019-01-01T00:00:00Z", "2021-12-31T23:59:59Z"),
    ("Ada trabajo alli desde 2019 hasta 2021", "2019-01-01T00:00:00Z", "2021-12-31T23:59:59Z"),
    ("Ada empezo el ano pasado", "2025-01-01T00:00:00Z", None),
    ("Ada empezó el año pasado", "2025-01-01T00:00:00Z", None),
    ("Ada has worked there since last month", "2026-08-01T00:00:00Z", None),
    ("Bruno trabajo alli hasta el mes pasado", None, "2026-08-31T23:59:59Z"),
    ("Ada has worked there since March 2024", "2024-03-01T00:00:00Z", None),
    ("En 2024 Ada se mudo a Madrid", "2024-01-01T00:00:00Z", None),
    ("Ada joined the team in 2024", "2024-01-01T00:00:00Z", None),
    ("valid till 2024-12-31", None, "2024-12-31T23:59:59Z"),
    ("Ada trabaja alli desde 03/2025", "2025-03-01T00:00:00Z", None),
    ("a partir de enero de 2027 Ada dirige el equipo", "2027-01-01T00:00:00Z", None),
    ("entre marzo y junio de 2025", "2025-03-01T00:00:00Z", "2025-06-30T23:59:59Z"),
    ("desde marzo de 2025 hasta junio", "2025-03-01T00:00:00Z", "2025-06-30T23:59:59Z"),
    ("desde marzo hasta junio de 2025", "2025-03-01T00:00:00Z", "2025-06-30T23:59:59Z"),
    ("Ada estuvo en Bluehaven de 2019 a 2021 y luego se fue", "2019-01-01T00:00:00Z",
     "2021-12-31T23:59:59Z"),
    ("desde el 1 de marzo de 2025", "2025-03-01T00:00:00Z", None),
    ("Ada nacio el 12/05/1990", "1990-05-12T00:00:00Z", None),
    ("El evento fue el 2025-03-10 en la oficina", "2025-03-10T00:00:00Z", None),
    ("hasta 2024 en Bluehaven", None, "2024-12-31T23:59:59Z"),
    # bare months rolled relative to now (September 2026)
    ("Bruno vive en Madrid hasta junio", None, "2027-06-30T23:59:59Z"),
    ("Ada works remotely until December", None, "2026-12-31T23:59:59Z"),
    ("Ada trabaja en remoto hasta septiembre", None, "2026-09-30T23:59:59Z"),
    ("HASTA JUNIO en remoto", None, "2027-06-30T23:59:59Z"),
    ("Uso el portatil nuevo desde mayo", "2026-05-01T00:00:00Z", None),
    ("Uso el portatil nuevo desde octubre", "2025-10-01T00:00:00Z", None),
    ("Ada lives in Madrid since May", "2026-05-01T00:00:00Z", None),
    ("Since May, Ada lives in Madrid", "2026-05-01T00:00:00Z", None),
]


@pytest.mark.parametrize("text,vf,vu", POSITIVES)
def test_positive_windows(text, vf, vu):
    result = _parse(text)
    assert (result["valid_from"], result["valid_until"]) == (vf, vu), result


def test_positive_table_is_broad():
    assert len(POSITIVES) >= 30


STATES = [
    ("Ada ya no trabaja en Cordera Labs", "past"),
    ("Ada no longer lives in Madrid", "past"),
    ("Ada used to live in Madrid", "past"),
    ("Antes vivia en Madrid", "past"),
    ("Ada solia vivir en Madrid", "past"),
    ("Ada ahora vive en Madrid", "current"),
    ("Ada currently lives in Madrid", "current"),
]


@pytest.mark.parametrize("text,state", STATES)
def test_state_words_still_work_and_invent_no_date(text, state):
    result = _parse(text)
    assert result["state"] == state
    assert result["valid_from"] is None and result["valid_until"] is None


# ---------------------------------------------------------------------------
# Bare months: the review's exact repro (r15)
# ---------------------------------------------------------------------------


def test_until_bare_month_said_in_october_is_next_year_not_already_expired():
    result = _parse("Ada trabaja en remoto hasta marzo", now=OCT)
    assert result["valid_until"] == "2027-03-31T23:59:59Z"


def test_since_bare_month_said_in_february_is_last_year_not_future():
    result = _parse("Uso el portatil nuevo desde mayo", now=FEB)
    assert result["valid_from"] == "2025-05-01T00:00:00Z"


def test_bare_month_windows_never_hide_a_memory_at_the_moment_it_is_written():
    for month in ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
                  "agosto", "septiembre", "octubre", "noviembre", "diciembre"):
        for now in (FEB, NOW, OCT):
            result = _parse(f"Ada trabaja en remoto hasta {month}", now=now)
            assert result["valid_until"] >= now.strftime("%Y-%m-%dT%H:%M:%SZ"), (month, now)
            result = _parse(f"Uso el portatil desde {month}", now=now)
            assert result["valid_from"] <= now.strftime("%Y-%m-%dT%H:%M:%SZ"), (month, now)


# ---------------------------------------------------------------------------
# End to end: ordinary technical memories stay valid now
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path, monkeypatch):
    from src import memory_engine as engine

    monkeypatch.setattr(engine, "DATA_DIR", str(tmp_path))
    engine.set_vector_store(None)
    engine.clear_injected()
    yield engine
    engine.reset_vector_store()
    engine.clear_injected()


@pytest.mark.parametrize("text,query", [
    ("El modelo acepta hasta 1024 tokens de salida", "tokens de salida"),
    ("El plan gratuito permite hasta 1000 llamadas al dia", "llamadas plan gratuito"),
    ("El servicio usa el puerto 8081", "puerto servicio"),
    ("Python 3.12 desde el lunes en el servidor", "Python servidor"),
    ("El contexto admite hasta 2048", "contexto admite"),
    ("Use batch sizes from 1024 to 4096", "batch sizes"),
])
def test_technical_memories_stay_valid_and_recallable(store, text, query):
    engine = store
    item = engine.add_item(text, owner="ada", trust_class="human_explicit", now=NOW)
    assert item["valid_until"] == ""
    later = NOW + timedelta(days=1)
    assert engine.is_valid_now(item, later)
    hits = engine.search(query, owner="ada", now=later, as_of=later, touch_hits=False)
    assert item["id"] in [h["id"] for h in hits]
