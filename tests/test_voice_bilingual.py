"""Output voice must follow the current reply when consecutive turns switch language."""
import pytest
from services.voice_stream import speech_language, strip_wake_word


@pytest.mark.parametrize("text,fallback,expected", [
    ("Archivo guardado.", "en", "es"), ("File saved.", "es", "en"),
    ("El informe está listo.", "en", "es"), ("The file is ready.", "es", "en"),
    ("Operación completada.", "en", "es"), ("Operation completed.", "es", "en"),
    ("No lo borres.", "en", "es"), ("Do not delete it.", "es", "en"),
    ("Espera un momento.", "en", "es"), ("Wait a moment.", "es", "en"),
    ("La carpeta contiene dos documentos.", "en", "es"), ("The folder contains two documents.", "es", "en"),
    ("He añadido los cambios.", "en", "es"), ("I have added the changes.", "es", "en"),
    ("¿Quieres continuar?", "en", "es"), ("Would you like to continue?", "es", "en"),
    ("Hola.", "en", "es"), ("Hello.", "es", "en"),
    ("Gracias.", "en", "es"), ("Thanks.", "es", "en"),
    ("42", "es", "es"), ("42", "en", "en"),
    ("No.", "es", "es"), ("No.", "en", "en"),
    ("Faustus", "es", "es"), ("Faustus", "en", "en"),
    ("Hello. Gracias.", "es", "es"), ("Hello. Gracias.", "en", "en"),
])
def test_voice_language_follows_reply(text, fallback, expected):
    assert speech_language(text, fallback) == expected


@pytest.mark.parametrize("text", ["Fosters, abre el informe.", "Foustas: abre el informe.",
                                 "Hola Foustas, abre el informe."])
def test_asr_wake_variants_require_direct_vocative(text):
    matched, body = strip_wake_word(text)
    assert matched and "fosters" not in body.lower() and "foustas" not in body.lower()


@pytest.mark.parametrize("text", ["Foster sent the report.", "Foster ha enviado el informe.",
                                 "Fosters makes beer.", "I read Fosters, yesterday.",
                                 "Fosters abre el informe.", "Foster's, open the report."])
def test_other_names_and_mentions_do_not_wake(text):
    assert strip_wake_word(text) == (False, text)
