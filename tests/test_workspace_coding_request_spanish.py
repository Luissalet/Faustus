"""Spanish imperatives with a pronoun tail, and plain files, enter code mode.

10-09-2026, live on 7001: "Cambia el contenido de saludo.txt para que
diga: hola de nuevo" with a workspace bound was classified low-signal
(".txt" was not a target), so the write-tool floor never shipped and the
turn depended on retrieval happening to bring `write_file`. And
"Impleméntame en este proyecto..." only entered code mode because
"guardar" appeared later: "impleméntame" (accent + enclitic) matched no
verb.
"""
import pytest

from src.agent_loop import _looks_like_workspace_coding_request as looks


@pytest.mark.parametrize("text", [
    "Cambia el contenido de saludo.txt para que diga: hola de nuevo",
    "Impleméntame en este proyecto un sistema para las preferencias de cada usuario",
    "Arréglalo en cart.py",
    "Añádele un botón a la interfaz",
    "Ponlo en el módulo de rutas",
    "Cámbialo en el fichero de configuración",
    "Bórrame la función duplicada",
    "Revisa los datos de ventas.csv",
])
def test_spanish_requests_enter_code_mode(text):
    assert looks(text)


@pytest.mark.parametrize("text", [
    "¿Qué tal estás hoy?",
    "Me gusta el café",
    "Explain what a closure is",
    "hazme un resumen de la película",
    "check example.com for me",
])
def test_ordinary_prose_stays_out(text):
    assert not looks(text)
