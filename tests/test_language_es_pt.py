"""Spanish was being read as Portuguese by a handful of shared words.

Found by using the app: "Prueba de envio numero dos" was answered with
"Recebido! Mensagem de teste nº 2 chegou sem problemas."

The cause was not the missing accents. `dos` was listed as a Portuguese
function word only -- there it is the contraction de+os -- while in Spanish it
is the number two, one of the most common words in the language. Since each
hit is weighted by how many languages share the word, a word listed under one
language settles it outright:

    "dos"                 -> pt 1.0
    "dame dos ejemplos"   -> pt 1.0

The same was true of `o` (Spanish "or"), `da` (from dar) and `segundo`. They
are now listed under both, so the weighting can do its job.

The other half of these tests matters as much: Portuguese must still be read
as Portuguese. The words were shared, not moved.
"""
import pytest

from src.research_citations import detect_language, language_signal


# ---------------------------------------------------------------------------
# The sentences that were wrong
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Prueba de envio numero dos",
    "dame dos ejemplos",
    "son dos ficheros",
    "necesito dos cosas",
    "hazlo de dos maneras",
    "el primero o el segundo",
    "da igual cual de los dos",
])
def test_spanish_is_read_as_spanish(text):
    assert detect_language(text) == "es"


# ---------------------------------------------------------------------------
# Portuguese still has to work
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Você pode explicar como funciona isto?",
    "Não entendi a resposta, pode repetir",
    "Quais são os melhores exemplos disto?",
    "Onde estão os ficheiros que foram alterados?",
])
def test_portuguese_is_still_read_as_portuguese(text):
    """The shared words were added to Spanish, not taken from Portuguese. A
    real Portuguese sentence carries plenty that Spanish does not."""
    assert detect_language(text) == "pt"


# ---------------------------------------------------------------------------
# The weighting
# ---------------------------------------------------------------------------

def test_a_shared_word_no_longer_settles_a_language_alone():
    """"dos" on its own must not be evidence of anything much."""
    code, signal = language_signal("dos")
    assert signal < 1.0


def test_a_sentence_with_real_signal_still_settles():
    code, signal = language_signal("necesito dos cosas")
    assert code == "es"
    assert signal >= 1.0


def test_english_is_untouched():
    assert detect_language("What are the two files that changed?") == "en"
