"""Audit finding 5: browser intent detection was English-only.

`_explicit_browser_intent` (per-message) and `_RECENT_BROWSER_CONTEXT_RE`
(recent-history follow-ups) must recognise the Spanish phrasings the audit
used, with the same word-boundary discipline as the English rules — and must
NOT start classifying coding requests as browsing.
"""

import pytest

import routes.chat_routes as cr


SPANISH_BROWSER = [
    "abre el navegador y entra en la web de correos",
    "navega a https://example.com y dime qué ves",
    "haz una captura de pantalla de la página",
    "rellena el formulario de contacto con mis datos",
    "pincha en el botón de enviar",
    "haz clic en iniciar sesión",
    "abre la web del ayuntamiento",
    "abre la página de inicio de sesión",
    "hazme un pantallazo",
    "pulsa el botón azul",
    "cambia a la otra pestaña",
    "desplázate hasta el final de la página",
    "inicia sesión en mi cuenta",
]

NOT_BROWSER = [
    # coding requests that mention navigation/forms only figuratively
    "navegar por el código para encontrar la función",
    "quiero navegar por la estructura del repositorio",
    "captura la excepción y registra el error",
    "crea un formulario en React con validación",
    "escribe una función que rellene un array con ceros",
    "the formulary of the drug",
    "refactor apply_tax(total, rate) and add a unit test",
    "resume mi día",
]


@pytest.mark.parametrize("message", SPANISH_BROWSER)
def test_explicit_browser_intent_recognises_spanish(message):
    assert cr._explicit_browser_intent_for_message(message) is True


@pytest.mark.parametrize("message", SPANISH_BROWSER)
def test_recent_context_regex_recognises_spanish(message):
    assert cr._RECENT_BROWSER_CONTEXT_RE.search(message) is not None


@pytest.mark.parametrize("message", NOT_BROWSER)
def test_spanish_rules_do_not_fire_on_coding_requests(message):
    assert cr._explicit_browser_intent_for_message(message) is False
    assert cr._RECENT_BROWSER_CONTEXT_RE.search(message) is None


@pytest.mark.parametrize(
    "message",
    [
        "open the site and fill out the contact form",
        "click the submit button",
        "use the browser to check the page",
    ],
)
def test_english_rules_still_match(message):
    assert cr._explicit_browser_intent_for_message(message) is True
    assert cr._RECENT_BROWSER_CONTEXT_RE.search(message) is not None


def test_word_boundaries_are_kept():
    # "navega" must not match inside "navegar"/"navegación"; "clic" not inside "click-through" is fine
    assert cr._explicit_browser_intent_for_message("la navegación del menú está rota") is False
    assert cr._explicit_browser_intent_for_message("pestañear") is False
    assert cr._explicit_browser_intent_for_message("pulsar") is False


def test_contextual_followup_after_spanish_browser_turn():
    class _Msg:
        def __init__(self, content):
            self.content = content

    class _Sess:
        history = [_Msg("rellena el formulario de contacto"), _Msg("Necesito aprobación para enviar.")]

    assert cr._is_contextual_browser_followup("ok", _Sess()) is True
    assert cr._is_contextual_browser_followup("proceed", _Sess()) is True
