"""Which language the agent answers in — said out loud, once per turn.

Nothing in the agent prompt ever said it. The tool descriptions, the rules
and the date block are English, the user writes in whatever he likes, and a
local model settles its output language on whichever cue in the context
happens to be loudest. That is how an English request in a brand-new session
came back as "Voy a inspeccionar el código": rule 2 of the coding harness
carried ``"voy a modificar X"`` as an example of what NOT to say, and the
model took the Spanish and dropped the "not".

So the turn names the language instead of hoping. One line, written in the
language it asks for — a model about to answer in the wrong language reads an
English sentence about English no better than a Spanish one — placed after
the date block, immediately before the user's message, where it is the last
thing read.

Saying nothing is a real answer here. "Hazlo", a pasted path, a stack trace
carry no function words at all, and pinning a language off nothing would be
worse than letting the model follow the conversation: the scan walks back to
the most recent user turn that actually settled a language, and when none
did, no message is built.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple
import re

from src.research_citations import language_signal

# One very common word ("de", "the") is shared by four of the six languages
# and scores 0.25, so a single hit at that weight is noise. This asks for the
# equivalent of four of them: every real sentence clears it, a bare "Hazlo"
# never does.
MIN_SIGNAL = 1.0

# Each directive is written in the language it names.  This is a runtime
# requirement, not merely descriptive context: weaker wording was easy for
# small/local models to lose among English tool descriptions and retrieved
# sources.
_FRAME = "[Runtime requirement — reply language]\n"

_DIRECTIVE: Dict[str, str] = {
    "en": "The user is writing in English. You must write your whole reply to the user in English — "
          "narration, summaries and questions alike. Do not switch language because tools, sources "
          "or system text use another one. Code, file paths, identifiers and tool arguments keep "
          "their own form.",
    "es": "El usuario escribe en español. Debes escribir toda tu respuesta al usuario en español: "
          "narración, resúmenes y preguntas. No cambies de idioma porque las herramientas, las "
          "fuentes o el texto del sistema usen otro. El código, las rutas, los identificadores y "
          "los argumentos de las herramientas se quedan como están.",
    "fr": "L'utilisateur écrit en français. Rédige toute ta réponse à l'utilisateur en français : "
          "narration, résumés et questions. Le code, les chemins, les identifiants et les "
          "arguments d'outils restent tels quels.",
    "de": "Der Nutzer schreibt auf Deutsch. Schreibe deine gesamte Antwort an den Nutzer auf "
          "Deutsch: Erläuterungen, Zusammenfassungen und Rückfragen. Code, Pfade, Bezeichner und "
          "Tool-Argumente bleiben unverändert.",
    "pt": "O utilizador escreve em português. Escreve toda a tua resposta ao utilizador em "
          "português: narração, resumos e perguntas. O código, os caminhos, os identificadores e "
          "os argumentos das ferramentas ficam como estão.",
    "it": "L'utente scrive in italiano. Scrivi tutta la tua risposta all'utente in italiano: "
          "narrazione, riepiloghi e domande. Il codice, i percorsi, gli identificatori e gli "
          "argomenti degli strumenti restano come sono.",
}


def visible_text(content: Any) -> str:
    """The words of a message, whatever shape it arrived in.

    Vision turns carry a list of parts; only the text ones say anything about
    the language the user is writing in.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "\n".join(parts)
    return ""


def language_of(text: Any) -> Optional[str]:
    """The language one message settles, or ``None`` when it settles none."""
    text = visible_text(text) if not isinstance(text, str) else text
    # A direct language request wins over the language used to ask for it.
    # Anchor to the user's opening instruction, not quotations or code later.
    explicit = re.match(r"\s*(?:(?:please|por favor)[, ]+)?(?:answer|reply|respond|write|responde|contesta|escribe)"
                        r"(?:\s+(?:only|entirely|solo|únicamente))?\s+(?:in|en)\s+"
                        r"(english|inglés|ingles|spanish|español|espanol|french|francés|german|alemán|portuguese|portugués|italian|italiano)\b",
                        text, re.IGNORECASE)
    if explicit:
        return {"english": "en", "inglés": "en", "ingles": "en", "spanish": "es", "español": "es", "espanol": "es",
                "french": "fr", "francés": "fr", "german": "de", "alemán": "de", "portuguese": "pt", "portugués": "pt",
                "italian": "it", "italiano": "it"}[explicit.group(1).lower()]
    code, signal = language_signal(text)
    return code if signal >= MIN_SIGNAL else None


def conversation_language(messages: Optional[Iterable[Dict[str, Any]]]) -> Optional[str]:
    """The language of the most recent user turn that settled one.

    Context blocks this module's own caller injects are skipped: they are
    English by construction and would otherwise pin English over whatever the
    user actually wrote.
    """
    for message in reversed(list(messages or [])):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        if message.get("_agent_injected"):
            continue
        if isinstance(message.get("metadata"), dict) and message["metadata"].get("trusted") is False:
            continue
        code = language_of(message.get("content"))
        if code:
            return code
    return None


def refresh_continuation(messages: List[Dict[str, Any]], hint: Optional[Dict[str, str]]) -> None:
    """One current reminder after tool results, never a growing prompt tail."""
    messages[:] = [m for m in messages if m.get("_agent_injected") != "reply_language_continuity"]
    if hint:
        messages.append({**hint, "_agent_injected": "reply_language_continuity"})


def context_message(
    messages: Optional[Iterable[Dict[str, Any]]],
) -> Optional[Dict[str, str]]:
    """The user-role note naming the reply language, or ``None`` to stay quiet.

    A ``user`` role rather than ``system`` for the same reason the date block
    is one: local backends key their KV-cache prefix off the system message
    byte-for-byte, and this line changes whenever the user switches language.
    """
    code = conversation_language(messages)
    if not code:
        return None
    return {
        "role": "user",
        "content": _FRAME + _DIRECTIVE[code],
        # Every language scan ignores injected context.  Keeping the marker on
        # the object itself makes the helper safe outside agent_loop too.
        "_agent_injected": "context",
    }


# ---------------------------------------------------------------------------
# LANG-02 — editing conserves language and accents
# ---------------------------------------------------------------------------
#
# ``docs/spec/v2/backlog.json`` (LANG-02): a search normalized for matching
# (accent/case-insensitive, so a user who types "movil" still finds "móvil")
# must not corrupt the ORIGINAL text it edits or shift where a caller thinks
# a match sits. `services.search.lang_normalize` is the actual normalization
# authority (rule 4 -- one, not a second copy here); this is the seam an
# editing caller in THIS lote's scope uses it through, returning offsets
# into the untouched original text so an edit lands exactly where the real
# (accented) text is, and the document itself is never rewritten by the
# lookup.


def locate_for_edit(document_text: str, needle: str) -> Optional[Tuple[int, int]]:
    """Where `needle` is in `document_text` for an edit, tolerant of
    diacritic/case differences between the two -- ``None`` when there is no
    match at all.

    The returned ``(start, end)`` are offsets into `document_text` EXACTLY
    as given (its own accents, its own case, untouched): normalizing for the
    comparison never normalizes the text an edit will actually touch, and
    never shifts those offsets (LANG-02's own acceptance criterion).
    """
    from services.search.lang_normalize import locate_normalized

    return locate_normalized(document_text, needle)
