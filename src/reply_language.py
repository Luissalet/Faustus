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

# Chat preprocessing appends readable uploads after the user's own words.
# Scoring those bodies for reply language is what flipped English Silhouettes
# turns ("Implement it" + Spanish plan zip/md) to an "answer in Spanish"
# directive. Same envelope the agent loop already strips for tool routing.
_ATTACHMENT_BODY_START_RE = re.compile(
    r"(?m)^(?:=== (?:File|ZIP archive): .+? ===|\[Attached non-text file\])\s*$"
)
# Image / media chrome is English boilerplate and can outweigh a short Spanish
# caption when left in the scored text.
_ATTACHMENT_CHROME_LINE_RE = re.compile(
    r"(?m)^\[(?:Image attached:.*?|\d+\s+inline media payload.*?omitted|Attachment:.*?)\]\s*$"
)

# Each directive is written in the language it names.  This is a runtime
# requirement, not merely descriptive context: weaker wording was easy for
# small/local models to lose among English tool descriptions and retrieved
# sources.
_FRAME = "[Runtime requirement — reply language]\n"

_DIRECTIVE: Dict[str, str] = {
    "en": "The user is writing in English. You must write your whole reply to the user in English — "
          "narration, summaries and questions alike. Attached files, plans, zip contents, tool output "
          "and earlier assistant turns may be in another language; that must not change YOUR reply "
          "language. Do not switch because tools, sources or system text use another one. Code, file "
          "paths, identifiers and tool arguments keep their own form.",
    "es": "El usuario escribe en español. Debes escribir toda tu respuesta al usuario en español: "
          "narración, resúmenes y preguntas. Los archivos adjuntos, planes, zips, salidas de "
          "herramientas y turnos anteriores del asistente pueden estar en otro idioma; eso no debe "
          "cambiar el idioma de TU respuesta. No cambies porque las herramientas, las fuentes o el "
          "texto del sistema usen otro. El código, las rutas, los identificadores y los argumentos "
          "de las herramientas se quedan como están.",
    "fr": "L'utilisateur écrit en français. Rédige toute ta réponse à l'utilisateur en français : "
          "narration, résumés et questions. Les pièces jointes, plans, zips, sorties d'outils et "
          "tours d'assistant précédents peuvent être dans une autre langue ; cela ne doit pas "
          "changer la langue de TA réponse. Le code, les chemins, les identifiants et les "
          "arguments d'outils restent tels quels.",
    "de": "Der Nutzer schreibt auf Deutsch. Schreibe deine gesamte Antwort an den Nutzer auf "
          "Deutsch: Erläuterungen, Zusammenfassungen und Rückfragen. Angehängte Dateien, Pläne, "
          "Zips, Tool-Ausgaben und frühere Assistenten-Antworten können in einer anderen Sprache "
          "sein; das ändert nicht die Sprache deiner Antwort. Code, Pfade, Bezeichner und "
          "Tool-Argumente bleiben unverändert.",
    "pt": "O utilizador escreve em português. Escreve toda a tua resposta ao utilizador em "
          "português: narração, resumos e perguntas. Anexos, planos, zips, saídas de ferramentas e "
          "turnos anteriores do assistente podem estar noutro idioma; isso não muda a língua da "
          "TUA resposta. O código, os caminhos, os identificadores e os argumentos das ferramentas "
          "ficam como estão.",
    "it": "L'utente scrive in italiano. Scrivi tutta la tua risposta all'utente in italiano: "
          "narrazione, riepiloghi e domande. Allegati, piani, zip, output degli strumenti e turni "
          "precedenti dell'assistente possono essere in un'altra lingua; ciò non deve cambiare la "
          "lingua della TUA risposta. Il codice, i percorsi, gli identificatori e gli argomenti "
          "degli strumenti restano come sono.",
}

_MISMATCH_NUDGE: Dict[str, str] = {
    "en": (
        "[Harness check — automatic runtime message, not a new user request] "
        "Your narration to the user is in the wrong language. The user is writing in English. "
        "Rewrite the user-facing reply in English now. Keep code, paths and tool arguments as they "
        "are. Do not repeat tool calls that already succeeded; do not keep answering in the previous "
        "language just because earlier turns or attached plans used it."
    ),
    "es": (
        "[Harness check — mensaje automático del runtime, no es una petición nueva del usuario] "
        "Tu narración al usuario está en el idioma equivocado. El usuario escribe en español. "
        "Reescribe la respuesta al usuario en español ahora. Código, rutas y argumentos de "
        "herramientas se quedan. No repitas herramientas que ya tuvieron éxito; no sigas en el "
        "idioma anterior solo porque turnos previos o planes adjuntos lo usaban."
    ),
    "fr": (
        "[Harness check — message automatique du runtime, pas une nouvelle demande] "
        "Ta narration est dans la mauvaise langue. L'utilisateur écrit en français. "
        "Réécris la réponse à l'utilisateur en français maintenant."
    ),
    "de": (
        "[Harness check — automatische Runtime-Nachricht, keine neue Nutzeranfrage] "
        "Deine Erklärung ist in der falschen Sprache. Der Nutzer schreibt auf Deutsch. "
        "Schreibe die Antwort an den Nutzer jetzt auf Deutsch."
    ),
    "pt": (
        "[Harness check — mensagem automática do runtime, não é um novo pedido] "
        "A tua narração está no idioma errado. O utilizador escreve em português. "
        "Reescreve a resposta ao utilizador em português agora."
    ),
    "it": (
        "[Harness check — messaggio automatico del runtime, non è una nuova richiesta] "
        "La narrazione è nella lingua sbagliata. L'utente scrive in italiano. "
        "Riscrivi ora la risposta all'utente in italiano."
    ),
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


def instruction_text_for_language(text: Any) -> str:
    """The user's own words, without appended upload bodies or media chrome.

    Attachment contents are reference data (plans, patches, images), not the
    language the user is speaking in. Including them made English instructions
    with Spanish plan zips pin the reply-language directive to Spanish.
    """
    value = visible_text(text) if not isinstance(text, str) else str(text or "")
    match = _ATTACHMENT_BODY_START_RE.search(value)
    if match:
        value = value[: match.start()]
    value = _ATTACHMENT_CHROME_LINE_RE.sub("", value)
    return value.strip()


def language_of(text: Any) -> Optional[str]:
    """The language one message settles, or ``None`` when it settles none."""
    text = instruction_text_for_language(text)
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
    """One current reminder after tool results, never a growing prompt tail.

    Placed BEFORE the final message, not after it. This runs at the top of a
    round, when the last thing in the list is whatever the previous round
    ended with -- tool results, or a runtime instruction as pointed as "the
    tests FAILED, fix them". Appending put a standing style requirement
    after that instruction, so the last thing the model read before
    answering was boilerplate about which language to write in rather than
    the thing it had to do. A reminder is a condition on the answer; it is
    not the next step, and it should not be read as one.

    "Before the final message" must never mean inside a tool exchange. When
    the list ends with tool results, the final message is a ``tool`` reply,
    and putting the reminder right before it separated the assistant's call
    from its answer: ``assistant(call) → user(reminder) → tool(result)``.
    Seen live on a 27B: the model read its own call as unanswered and made
    the identical call again, every time, before answering. The exchange is
    kept whole and the reminder goes in front of it.
    """
    messages[:] = [m for m in messages if m.get("_agent_injected") != "reply_language_continuity"]
    if hint:
        messages.insert(_continuation_slot(messages),
                        {**hint, "_agent_injected": "reply_language_continuity"})


def _continuation_slot(messages: List[Dict[str, Any]]) -> int:
    """Index for the reminder: before the last message, or -- when the list
    ends in tool results -- before the assistant turn that asked for them."""
    idx = len(messages)
    while idx > 0 and messages[idx - 1].get("role") == "tool":
        idx -= 1
    if idx == len(messages):
        return max(0, len(messages) - 1)
    if idx > 0 and messages[idx - 1].get("role") == "assistant":
        return idx - 1
    return idx


#: Used when the conversation settles no language of its own. Empty keeps the
#: old behaviour exactly: say nothing and let the model choose.
DEFAULT_LANGUAGE_SETTING = "reply_language_default"


def fallback_language() -> Optional[str]:
    """The language to use when the conversation settles none.

    Found by using the app. "Prueba de envio numero dos" was answered in
    Portuguese. Nothing was broken: written without accents, its only function
    word is "de", which four of the six languages share, so the signal came in
    under the threshold and no language was pinned at all. The model was left
    to guess from a sentence that, stripped of its accents, really does look
    Portuguese -- and people type without accents constantly.

    Staying silent is the right default for a server that does not know its
    user. It is the wrong one for a personal install where every conversation
    is in the same language, so that install can name it here and stop the
    guessing. An unset or unrecognised value keeps today's behaviour.
    """
    try:
        from src.settings import get_setting
        code = str(get_setting(DEFAULT_LANGUAGE_SETTING, "") or "").strip().lower()
    except Exception:  # noqa: BLE001
        return None
    return code if code in _DIRECTIVE else None


def context_message(
    messages: Optional[Iterable[Dict[str, Any]]],
) -> Optional[Dict[str, str]]:
    """The user-role note naming the reply language, or ``None`` to stay quiet.

    A ``user`` role rather than ``system`` for the same reason the date block
    is one: local backends key their KV-cache prefix off the system message
    byte-for-byte, and this line changes whenever the user switches language.
    """
    code = conversation_language(messages) or fallback_language()
    if not code:
        return None
    return {
        "role": "user",
        "content": _FRAME + _DIRECTIVE[code],
        # Every language scan ignores injected context.  Keeping the marker on
        # the object itself makes the helper safe outside agent_loop too.
        "_agent_injected": "context",
        "_reply_language": code,
    }


def mismatch_nudge_message(required_code: str) -> Optional[Dict[str, str]]:
    """Harness user turn that forces a rewrite into ``required_code``."""
    body = _MISMATCH_NUDGE.get(required_code) or _MISMATCH_NUDGE.get("en")
    if not body:
        return None
    return {
        "role": "user",
        "content": body,
        "_agent_injected": "reply_language_mismatch",
    }


# One line, appended to a runtime nudge (`_harness_note`, loop-recovery,
# verifier/review/test-failure text and the like) that Faustus itself
# injects between rounds. Those notes are hard-coded in English at every
# call site; on a long local-model turn several of them can be the LAST
# thing in `messages` before the next round is requested, right where
# ``refresh_continuation`` already places the standing reply-language
# reminder -- a harness note appended AFTER that reminder pushes it out of
# last place. A local model then settles its own interim narration
# ("Continuing with the analysis...") on whichever cue is freshest, which
# was the runtime's own English text, not the user's language. This keeps
# every such note arguing for the right language too, however many of them
# stack up in a round.
_INTERIM_REMINDER: Dict[str, str] = {
    "en": "Any further narration before your final answer must also be in English.",
    "es": "Cualquier narración antes de tu respuesta final debe ser también en español.",
    "fr": "Toute narration avant ta réponse finale doit aussi être en français.",
    "de": "Jede weitere Erläuterung vor deiner endgültigen Antwort muss ebenfalls auf Deutsch sein.",
    "pt": "Qualquer narração antes da tua resposta final também deve ser em português.",
    "it": "Qualsiasi narrazione prima della tua risposta finale deve essere anche in italiano.",
}


def runtime_note_language_reminder(code: Optional[str]) -> Optional[str]:
    """The one-line reminder for `code`, or ``None`` when there is none.

    ``None`` for an unset or unrecognised code keeps today's behaviour: an
    install that never settles a language gets no extra text.
    """
    if not code:
        return None
    return _INTERIM_REMINDER.get(code)


def localize_runtime_note(content: str, code: Optional[str]) -> str:
    """Append the interim-language reminder to a runtime-injected note.

    A no-op (returns `content` unchanged) when `code` settles nothing or is
    a language this module does not carry a reminder for -- callers can wrap
    every harness-note / loop-recovery message unconditionally.
    """
    reminder = runtime_note_language_reminder(code)
    if not reminder or not isinstance(content, str) or not content:
        return content
    return f"{content}\n\n({reminder})"


def reply_language_mismatch(required_code: Optional[str], reply_text: Any) -> Optional[str]:
    """Return the observed wrong language, or ``None`` when there is no mismatch.

    Used by the agent harness so a local model that keeps answering in Spanish
    after an English user turn (Silhouettes project: directive present, still
    "Voy a…") gets one bounded rewrite chance.
    """
    if not required_code or required_code not in _DIRECTIVE:
        return None
    # Score the narration only: strip think/tool chrome callers may leave in.
    observed = language_of(reply_text)
    if not observed or observed == required_code:
        return None
    return observed


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
