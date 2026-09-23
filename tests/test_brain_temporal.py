"""Tests for src/brain/temporal.py — table-driven, deterministic, ES+EN."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.brain.temporal import parse_temporal

NOW = datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


def _parse(text: str, **kw):
    return parse_temporal(text, now=NOW, **kw)


# ---------------------------------------------------------------------------
# Negatives — the "never guess" contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "",
    "   ",
    "Ada works at Cordera Labs",
    "Ada trabaja en Cordera Labs",
    "Marzo said hello to everyone in the office",
    "Marzo es una persona muy simpatica",
])
def test_no_marker_means_nothing(text):
    result = _parse(text)
    assert result == {"valid_from": None, "valid_until": None, "state": None, "markers": []}


def test_bare_year_without_context_is_not_a_date():
    result = _parse("The invoice number is 2024 for this client")
    assert result["valid_from"] is None
    assert result["valid_until"] is None


def test_bare_month_alone_is_not_a_date():
    result = _parse("Marzo is Ada's cat")
    assert result["valid_from"] is None
    assert result["state"] is None


# ---------------------------------------------------------------------------
# since / desde
# ---------------------------------------------------------------------------


def test_desde_month_year_es():
    result = _parse("Ada trabaja en Cordera Labs desde marzo de 2025")
    assert result["valid_from"] == "2025-03-01T00:00:00Z"
    assert result["valid_until"] is None
    assert result["markers"]


def test_since_month_year_en():
    result = _parse("Ada has worked at Cordera Labs since March 2025")
    assert result["valid_from"] == "2025-03-01T00:00:00Z"


def test_a_partir_de_bare_month_uses_now_year():
    result = _parse("Bruno vive en Madrid a partir de marzo")
    assert result["valid_from"] == "2025-03-01T00:00:00Z"


def test_from_bare_year():
    result = _parse("Ada worked there from 2020")
    assert result["valid_from"] == "2020-01-01T00:00:00Z"


def test_desde_iso_date():
    result = _parse("valido desde 2025-03-10")
    assert result["valid_from"] == "2025-03-10T00:00:00Z"


def test_desde_slash_month_year():
    result = _parse("Ada trabaja alli desde 03/2025")
    assert result["valid_from"] == "2025-03-01T00:00:00Z"


# ---------------------------------------------------------------------------
# until / hasta
# ---------------------------------------------------------------------------


def test_hasta_year_es():
    result = _parse("Ada trabajo en Cordera Labs hasta 2024")
    assert result["valid_until"] == "2024-12-31T23:59:59Z"


def test_until_month_year_en():
    result = _parse("Ada worked at Cordera Labs until June 2024")
    assert result["valid_until"] == "2024-06-30T23:59:59Z"


def test_hasta_bare_month_uses_now_year():
    result = _parse("Bruno vivio en Madrid hasta junio")
    assert result["valid_until"] == "2025-06-30T23:59:59Z"


def test_till_full_date():
    result = _parse("valid till 2024-12-31")
    assert result["valid_until"] == "2024-12-31T23:59:59Z"


# ---------------------------------------------------------------------------
# between / entre
# ---------------------------------------------------------------------------


def test_entre_years_es():
    result = _parse("Ada trabajo en Cordera Labs entre 2020 y 2023")
    assert result["valid_from"] == "2020-01-01T00:00:00Z"
    assert result["valid_until"] == "2023-12-31T23:59:59Z"


def test_between_month_years_en():
    result = _parse("Ada worked there between March 2020 and June 2021")
    assert result["valid_from"] == "2020-03-01T00:00:00Z"
    assert result["valid_until"] == "2021-06-30T23:59:59Z"


# ---------------------------------------------------------------------------
# standalone dated mentions (no directional marker)
# ---------------------------------------------------------------------------


def test_standalone_month_year_es_no_marker():
    result = _parse("En marzo de 2025 Ada empezo en Cordera Labs")
    assert result["valid_from"] == "2025-03-01T00:00:00Z"


def test_standalone_month_year_bare_es():
    result = _parse("marzo 2025 fue un buen mes")
    assert result["valid_from"] == "2025-03-01T00:00:00Z"


def test_standalone_en_year_with_context():
    result = _parse("Ada joined the team in 2024")
    assert result["valid_from"] == "2024-01-01T00:00:00Z"


def test_standalone_iso_date():
    result = _parse("El evento fue el 2025-03-10 en la oficina")
    assert result["valid_from"] == "2025-03-10T00:00:00Z"


# ---------------------------------------------------------------------------
# relative expressions
# ---------------------------------------------------------------------------


def test_el_ano_pasado():
    result = _parse("Ada empezo el ano pasado")
    assert result["valid_from"] == "2024-01-01T00:00:00Z"


def test_last_year_since():
    result = _parse("Ada has worked there since last year")
    assert result["valid_from"] == "2024-01-01T00:00:00Z"


def test_este_mes():
    result = _parse("Ada empezo este mes")
    assert result["valid_from"] == "2025-06-01T00:00:00Z"


def test_this_month_since():
    result = _parse("Ada started this month")
    # "this month" with no directional marker -> standalone, start of period.
    assert result["valid_from"] == "2025-06-01T00:00:00Z"


def test_el_mes_pasado_hasta():
    result = _parse("Bruno trabajo alli hasta el mes pasado")
    assert result["valid_until"] == "2025-05-31T23:59:59Z"


# ---------------------------------------------------------------------------
# state words — no invented date
# ---------------------------------------------------------------------------


def test_ya_no_sets_past_state_no_date():
    result = _parse("Ada ya no trabaja en Cordera Labs")
    assert result["state"] == "past"
    assert result["valid_from"] is None
    assert result["valid_until"] is None


def test_no_longer():
    result = _parse("Ada no longer works at Cordera Labs")
    assert result["state"] == "past"


def test_used_to():
    result = _parse("Ada used to work at Cordera Labs")
    assert result["state"] == "past"


def test_antes_and_solia():
    assert _parse("Antes trabajaba en Cordera Labs")["state"] == "past"
    assert _parse("Ada solia trabajar en Cordera Labs")["state"] == "past"


def test_ahora_current():
    result = _parse("Ada ahora trabaja en Bluehaven")
    assert result["state"] == "current"


def test_currently_and_actualmente():
    assert _parse("Ada currently works at Bluehaven")["state"] == "current"
    assert _parse("Ada actualmente trabaja en Bluehaven")["state"] == "current"


def test_future_state():
    assert _parse("Ada trabajara en Bluehaven proximamente")["state"] == "future"
    assert _parse("Ada will move to Bluehaven soon")["state"] == "future"


def test_past_state_combined_with_explicit_date():
    result = _parse("Ada ya no trabaja en Cordera Labs desde marzo de 2025")
    assert result["state"] == "past"
    assert result["valid_from"] == "2025-03-01T00:00:00Z"


# ---------------------------------------------------------------------------
# case / accent insensitivity
# ---------------------------------------------------------------------------


def test_accent_insensitive_matching():
    result = _parse("Ada trabaja en Cordera Labs DESDE MARZO DE 2025")
    assert result["valid_from"] == "2025-03-01T00:00:00Z"


def test_marker_text_is_sliced_from_original_case():
    result = _parse("Ada trabaja en Cordera Labs Desde Marzo de 2025")
    assert any("Desde Marzo" in m for m in result["markers"])
