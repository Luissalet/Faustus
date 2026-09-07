"""Per-export labels; concurrent English and Spanish exports never share state."""
from collections.abc import Mapping
from contextvars import ContextVar
from functools import wraps

_language = ContextVar("export_language", default="en")
SPANISH = {
    "user": "Usuario", "assistant": "Asistente", "system": "Sistema", "tool": "Herramienta",
    "model": "Modelo", "exported": "Exportado", "messages": "mensajes", "message": "mensaje",
    "session": "Sesión", "project": "Proyecto", "workspace": "Espacio de trabajo",
    "attachments": "Adjuntos", "empty": "(esta conversación no tiene mensajes)",
    "tool_call": "llamada a herramienta", "arguments": "argumentos", "result": "resultado",
    "truncated": "... [truncado]", "image": "[imagen]", "page": "Página ", "page_of": " de ",
    "document": "Documento", "document_comment": "Documento exportado por Faustus",
    "document_creator": "Exportación de documentos de Faustus",
}


def language(value):
    return "es" if str(value or "").lower().replace("_", "-").split("-")[0] == "es" else "en"


class Labels(Mapping):
    def __init__(self, english, *, pdf=False):
        self.english = dict(english)
        self.english.update(document="Document", document_comment="Document exported by Faustus",
                            document_creator="Faustus document export")
        self.pdf = pdf

    def __getitem__(self, key):
        if _language.get() == "es" and key in SPANISH:
            if key == "page" and self.pdf:
                return "Página %d de %d"
            return SPANISH[key]
        return self.english[key]

    def __iter__(self):
        return iter(self.english)

    def __len__(self):
        return len(self.english)


def localized_render(render):
    @wraps(render)
    def wrapped(transcript):
        token = _language.set(language((getattr(transcript, "extra", None) or {}).get("language")))
        try:
            return render(transcript)
        finally:
            _language.reset(token)
    return wrapped
