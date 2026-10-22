"""How a named public thing is doing is a question for the web.

Seen live (22-10): "¿Ha sido Minecraft Dungeons 2 un fracaso? Parece que se
habla de él aún menos de lo que se habló del 1" matched no freshness label,
the typed decision was not loaded, and the turn started without web tools.
"""
import pytest

from src.freshness import freshness_assessment, freshness_reasons, looks_time_sensitive


@pytest.mark.parametrize("text", [
    "¿Ha sido Minecraft Dungeons 2 un fracaso? Parece que se habla de él aún menos de lo que se habló del 1",
    "¿Qué tal le va a Hollow Knight Silksong en ventas?",
    "Has the Switch 2 been a success?",
    "how is Hades II doing on Steam reviews",
    "¿Cómo está funcionando la película \"Dune Messiah\" en taquilla?",
])
def test_reception_of_a_named_public_thing_is_time_sensitive(text):
    assert looks_time_sensitive(text)
    assert "public_reception" in freshness_reasons(text)
    a = freshness_assessment(text)
    assert a["confident"] and a["time_sensitive"]


@pytest.mark.parametrize("text", [
    "explícame el fracaso escolar",
    "¿por qué fracasa mi script de backup? Mira los logs",
    "escribe un poema sobre el éxito",
    "¿qué reseñas tengo pendientes en mi repo?",
    "¿Cuántas ventas hubo? Mira mis notas del mes",
])
def test_generic_or_own_things_stay_as_they_were(text):
    assert "public_reception" not in freshness_reasons(text)
